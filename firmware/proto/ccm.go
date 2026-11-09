package proto

import "crypto/subtle"

// Cipher is AES-128 block encryption, by the hardware accelerator (hw/aeshw,
// one key register: it reloads the key when a different one comes) or by a
// software AES (the host tests). A key is named by its address: keep keys in
// fixed places and tell the cipher when one changes in place.
type Cipher interface {
	Encrypt(key *[16]byte, dst, src *[16]byte)
}

// CCM parameters (RFC 3610, spec section 5): 4-byte MIC, 2-byte length
// field, 13-byte nonce.
const (
	MICLen   = 4
	NonceLen = 13
	ccmL     = 2
)

// CCM is AES-CCM under Key built from single block encryptions, so that it
// runs on the ECB-only hardware driver and on a software AES alike. It is a
// plain value (no interface conversion per call, which would allocate under
// TinyGo).
type CCM struct {
	C   Cipher
	Key *[16]byte
}

// mac computes the CBC-MAC over B0, the AAD and the message.
func (c CCM) mac(nonce *[NonceLen]byte, aad, msg []byte) (x [16]byte) {
	var blk [16]byte
	blk[0] = 0x40*boolByte(len(aad) > 0) | (MICLen-2)/2<<3 | (ccmL - 1)
	copy(blk[1:], nonce[:])
	blk[14], blk[15] = byte(len(msg)>>8), byte(len(msg))
	c.C.Encrypt(c.Key, &x, &blk)

	if len(aad) > 0 {
		// The AAD starts with its 16-bit length.
		blk = [16]byte{byte(len(aad) >> 8), byte(len(aad))}
		i := 2
		for _, b := range aad {
			blk[i] = b
			i++
			if i == 16 {
				c.xorEncrypt(&x, &blk)
				blk, i = [16]byte{}, 0
			}
		}
		if i > 0 {
			c.xorEncrypt(&x, &blk)
		}
	}
	for len(msg) > 0 {
		blk = [16]byte{}
		n := copy(blk[:], msg)
		msg = msg[n:]
		c.xorEncrypt(&x, &blk)
	}
	return x
}

func (c CCM) xorEncrypt(x, blk *[16]byte) {
	for i := range x {
		x[i] ^= blk[i]
	}
	c.C.Encrypt(c.Key, x, x)
}

// ctr XORs src with the key stream from counter block 1 on, into dst.
func (c CCM) ctr(nonce *[NonceLen]byte, dst, src []byte) {
	var a, s [16]byte
	a[0] = ccmL - 1
	copy(a[1:], nonce[:])
	for i := uint16(1); len(src) > 0; i++ {
		a[14], a[15] = byte(i>>8), byte(i)
		c.C.Encrypt(c.Key, &s, &a)
		n := len(src)
		if n > 16 {
			n = 16
		}
		for j := 0; j < n; j++ {
			dst[j] = src[j] ^ s[j]
		}
		dst, src = dst[n:], src[n:]
	}
}

// tagMask returns S0, the key stream block that encrypts the MIC.
func (c CCM) tagMask(nonce *[NonceLen]byte) (s [16]byte) {
	var a [16]byte
	a[0] = ccmL - 1
	copy(a[1:], nonce[:])
	c.C.Encrypt(c.Key, &s, &a)
	return s
}

// Seal encrypts plain into dst and appends the MIC. dst must hold
// len(plain)+MICLen bytes and may start at plain itself. It returns the
// length written.
func (c CCM) Seal(dst []byte, nonce *[NonceLen]byte, plain, aad []byte) int {
	t := c.mac(nonce, aad, plain)
	s0 := c.tagMask(nonce)
	n := len(plain)
	c.ctr(nonce, dst[:n], plain)
	for i := 0; i < MICLen; i++ {
		dst[n+i] = t[i] ^ s0[i]
	}
	return n + MICLen
}

// Open checks and decrypts sealed (ciphertext || MIC) into dst and returns
// the plain length; ok is false for a short input or a wrong MIC. dst may
// start at sealed itself.
func (c CCM) Open(dst []byte, nonce *[NonceLen]byte, sealed, aad []byte) (n int, ok bool) {
	if len(sealed) < MICLen {
		return 0, false
	}
	n = len(sealed) - MICLen
	var mic [MICLen]byte
	copy(mic[:], sealed[n:])
	c.ctr(nonce, dst[:n], sealed[:n])
	t := c.mac(nonce, aad, dst[:n])
	s0 := c.tagMask(nonce)
	for i := range mic {
		t[i] ^= s0[i]
	}
	if subtle.ConstantTimeCompare(t[:MICLen], mic[:]) != 1 {
		return 0, false
	}
	return n, true
}

func boolByte(b bool) byte {
	if b {
		return 1
	}
	return 0
}
