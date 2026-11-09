package proto

// Secure envelope (spec section 5) and application layer (spec 15.4); the
// reference is cairnmesh/secure.py.
//
//	KIND|0x80 | SENDER | TARGET | SEQ | FCNT (4 B LE) | TX | E(plain[4:]) | MIC (4 B)
//	nonce = SENDER | TARGET | FCNT | KIND|0x80 | TX | 5 x 0, AAD = the 9-byte header
//
// Below it, REQUEST, REPORT, RESULT, EVENT and the REPLY to a REQUEST or RESULT
// carry the application layer under the device's own key:
//
//	KIND|APP | SENDER | TARGET | SEQ | EPOCH | KEYVER | E(payload) | AMIC (4 B)
//	nonce = SENDER | TARGET | FCNT | KIND|APP | KEYVER | 5 x 0
//	AAD   = KIND|APP | SENDER | TARGET | SEQ | EPOCH | KEYVER (+ payload for REPORT, v3)

const (
	// SecureHeader is the authenticated clear header of a sealed frame.
	SecureHeader = 9
	txOffset     = 8
	// Overhead is what sealing adds to a plain frame.
	Overhead = SecureHeader + MICLen
	// AppOverhead is what the application layer adds: KEYVER and the AMIC.
	AppOverhead = 1 + MICLen
	// TxReserve counters are reserved in persistent storage at a time.
	TxReserve = 256
	// AppExchanges REQUEST / RESULT exchanges are remembered to know which
	// REPLY carries the application layer.
	AppExchanges = 16
	// MaxAppKeys is the size of the application key table: a device holds
	// its own key in at most two versions (spec section 9).
	MaxAppKeys = 4
)

func networkNonce(header []byte) (n [NonceLen]byte) {
	n[0], n[1] = header[1], header[2]
	copy(n[2:6], header[4:8])
	n[6], n[7] = header[0], header[txOffset]
	return n
}

// SealNetwork wraps a plain frame (KIND | SENDER | TARGET | SEQ | rest) for
// the air under fcnt, as transmitted by tx. dst needs len(plain)+Overhead
// bytes and must not overlap plain. It returns the sealed length.
func SealNetwork(c CCM, dst, plain []byte, fcnt uint32, tx byte) int {
	dst[0], dst[1], dst[2], dst[3] = plain[0]|FlagSecure, plain[1], plain[2], plain[3]
	dst[4], dst[5], dst[6], dst[7] = byte(fcnt), byte(fcnt>>8), byte(fcnt>>16), byte(fcnt>>24)
	dst[txOffset] = tx
	nonce := networkNonce(dst[:SecureHeader])
	return SecureHeader + c.Seal(dst[SecureHeader:], &nonce, plain[4:], dst[:SecureHeader])
}

// OpenNetwork checks a sealed frame and writes the plain frame (the layout
// below the security layer) into dst, which needs len(sealed) bytes and must
// not overlap sealed. The replay window is the caller's.
func OpenNetwork(c CCM, dst, sealed []byte) (n int, fcnt uint32, ok bool) {
	if len(sealed) < Overhead || sealed[0]&FlagSecure == 0 {
		return 0, 0, false
	}
	header := sealed[:SecureHeader]
	nonce := networkNonce(header)
	m, ok := c.Open(dst[4:], &nonce, sealed[SecureHeader:], header)
	if !ok {
		return 0, 0, false
	}
	dst[0], dst[1], dst[2], dst[3] = header[0]&^FlagSecure, header[1], header[2], header[3]
	fcnt = uint32(header[4]) | uint32(header[5])<<8 | uint32(header[6])<<16 | uint32(header[7])<<24
	return 4 + m, fcnt, true
}

// Transmitter is the TX byte: who put this copy on the air (check the MIC first).
func Transmitter(sealed []byte) byte { return sealed[txOffset] }

// Reseal writes the same frame (SENDER, FCNT and plain bytes) as transmitted
// by tx into dst: what a relay sends for a frame it passes on unchanged.
func Reseal(c CCM, dst, sealed []byte, tx byte) (int, bool) {
	var plain [MaxFrame]byte
	n, fcnt, ok := OpenNetwork(c, plain[:], sealed)
	if !ok {
		return 0, false
	}
	return SealNetwork(c, dst, plain[:n], fcnt, tx), true
}

// ReplayWindow accepts each frame counter once, tolerating reordering within
// 32 frames: the highest counter and a bitmap below it (spec section 5).
type ReplayWindow struct {
	last uint32
	bits uint32 // bit i set: counter last-i was seen
	used bool
}

// RestoredWindow treats every counter up to last as seen (after a reboot).
func RestoredWindow(last uint32) ReplayWindow {
	return ReplayWindow{last: last, bits: 0xFFFFFFFF, used: true}
}

// Last is the highest counter accepted; ok is false before the first one.
func (w *ReplayWindow) Last() (last uint32, ok bool) { return w.last, w.used }

// Check reports whether fcnt would be new, without recording it.
func (w *ReplayWindow) Check(fcnt uint32) bool {
	if !w.used || fcnt > w.last {
		return true
	}
	d := w.last - fcnt
	return d < 32 && w.bits&(1<<d) == 0
}

// Accept records fcnt. Call it only after the MIC verified.
func (w *ReplayWindow) Accept(fcnt uint32) {
	switch {
	case !w.used:
		w.last, w.bits, w.used = fcnt, 1, true
	case fcnt > w.last:
		d := fcnt - w.last
		if d >= 32 {
			w.bits = 0
		} else {
			w.bits <<= d
		}
		w.bits |= 1
		w.last = fcnt
	default:
		w.bits |= 1 << (w.last - fcnt)
	}
}

// AppKeys holds application keys by node and version (secure.AppKeys): a
// device its own, the controller every device's. A frame's key belongs to
// the end that is not the controller: TARGET when it is in the table, else
// SENDER. New frames use the node's current version; any version held is
// accepted.
type AppKeys struct {
	entries [MaxAppKeys]appKey
}

type appKey struct {
	key     [16]byte
	node    byte
	version byte
	used    bool
	current bool
}

// Add stores a key and makes it the node's current version; false when the
// table is full.
func (a *AppKeys) Add(node, version byte, key *[16]byte) bool {
	slot := -1
	for i := range a.entries {
		e := &a.entries[i]
		if e.used && e.node == node {
			e.current = false
			if e.version == version {
				slot = i
			}
		}
	}
	for i := range a.entries {
		if slot < 0 && !a.entries[i].used {
			slot = i
		}
	}
	if slot < 0 {
		return false
	}
	a.entries[slot] = appKey{key: *key, node: node, version: version, used: true, current: true}
	return true
}

func (a *AppKeys) holds(node byte) bool {
	for i := range a.entries {
		if a.entries[i].used && a.entries[i].node == node {
			return true
		}
	}
	return false
}

func (a *AppKeys) nodeOf(sender, target byte) byte {
	if a.holds(target) {
		return target
	}
	return sender
}

func (a *AppKeys) find(node, version byte) *appKey {
	for i := range a.entries {
		e := &a.entries[i]
		if e.used && e.node == node && e.version == version {
			return e
		}
	}
	return nil
}

func (a *AppKeys) current(node byte) *appKey {
	for i := range a.entries {
		e := &a.entries[i]
		if e.used && e.node == node && e.current {
			return e
		}
	}
	return nil
}

func appNonce(head []byte, fcnt uint32) (n [NonceLen]byte) {
	n[0], n[1] = head[1], head[2]
	n[2], n[3], n[4], n[5] = byte(fcnt), byte(fcnt>>8), byte(fcnt>>16), byte(fcnt>>24)
	n[6], n[7] = head[0], head[5]
	return n
}

// Reject says why Secure dropped an incoming frame (the stats counters of
// secure.SecureTransport).
type Reject uint8

const (
	Accepted Reject = iota
	AuthFailed
	Replayed
	PlainDropped
	AppFailed
	AppMissing
	AppNoKey
)

// SecureStats counts what Secure did.
type SecureStats struct {
	Sealed, Opened, AuthFailed, Replayed, PlainDropped    uint32
	AppSealed, AppOpened, AppFailed, AppMissing, AppNoKey uint32
	ReserveFailed                                         uint32
}

// CounterStore persists the frame counter (spec section 5): ReserveTx
// writes the bound up to which counters may be used and reports whether it
// is stored (written and read back). A counter is used only once reserved.
type CounterStore interface {
	ReserveTx(bound uint32) bool
}

// MarkStore persists the replay marks (spec section 5): Mark stores the
// highest counter accepted from a sender and reports whether it is stored.
type MarkStore interface {
	Mark(sender byte, last uint32) bool
}

// MarkVisitor takes the marks EachMark lists; false stops it.
type MarkVisitor interface {
	Visit(sender byte, last uint32) bool
}

// MarkInterval: the replay marks are written at most this often (spec 5;
// secure.RX_FLUSH_INTERVAL). A power cut loses about the last interval's.
const MarkInterval = Second

type exchange struct {
	peer byte
	id   uint16
}

// Secure seals outgoing and opens incoming frames of one node: one FCNT
// counter, one replay window per sender, the application keys
// (secure.SecureTransport). The windows are indexed by sender, so none is
// ever evicted (an evicted window would let a recorded frame in again).
type Secure struct {
	network CCM
	cipher  Cipher
	Keys    AppKeys
	// App turns the application layer on (secure.SecureTransport app=...).
	App      bool
	fcnt     uint32
	reserved uint32
	store    CounterStore
	windows  [256]ReplayWindow
	marks    MarkStore
	// marked is the one sender whose mark is persisted (the controller,
	// spec 5) once markOne is set; every other window lives in RAM.
	marked  byte
	markOne bool
	// top is the controller's persisted mark: the highest FCNT of its frames
	// this node opened or its relay heard (NoteHeard), spec 5.
	top    uint32
	topSet bool
	// dirty: bit s set, sender s's mark moved since it was stored.
	dirty     [8]uint32
	anyDirty  bool
	markedAt  Time
	markedYet bool
	// exchanges is oldest first; the REPLY to one of these carries the layer.
	exchanges [AppExchanges]exchange
	nExchange int
	scratch   [MaxFrame]byte
	// LastApp: the frame OpenFrame last accepted carried the application
	// layer (a RESULT's acknowledgement does, an EVENT's does not: spec 19).
	LastApp bool
	Stats   SecureStats
}

// Init sets the network key (kept at a fixed address by the caller), the
// last counter used (the stored bound after a reboot) and the store, which
// may be nil (no persistence: tests).
func (s *Secure) Init(c Cipher, networkKey *[16]byte, lastFcnt uint32, store CounterStore) {
	s.cipher = c
	s.network = CCM{C: c, Key: networkKey}
	s.fcnt, s.reserved, s.store = lastFcnt, lastFcnt, store
}

// Fcnt is the counter of the last frame sealed.
func (s *Secure) Fcnt() uint32 { return s.fcnt }

// Network is the network-key CCM (a relay reseals with it).
func (s *Secure) Network() CCM { return s.network }

// Window is the replay window of one sender (for persisting its marks).
func (s *Secure) Window(sender byte) *ReplayWindow { return &s.windows[sender] }

// SetMarks makes FlushMarks store the replay mark of controller in m (nil:
// RAM only). A device persists the controller's mark only (spec 5: flash
// wear): every other sender's window starts empty after a reboot.
func (s *Secure) SetMarks(m MarkStore, controller byte) {
	s.marks, s.marked, s.markOne = m, controller, true
	s.top, s.topSet = s.windows[controller].Last()
}

// NoteHeard: the relay heard a frame of the controller's (spec 5), maybe
// for another node. Its FCNT is persisted too, without moving the replay
// window: a later frame for this node may legitimately carry a lower one.
func (s *Secure) NoteHeard(sender byte, fcnt uint32) { s.raiseTop(sender, fcnt) }

// Mark is the persisted mark of sender: for the controller the higher of
// its window and what the relay heard.
func (s *Secure) Mark(sender byte) (uint32, bool) {
	last, ok := s.windows[sender].Last()
	if s.markOne && sender == s.marked && s.topSet && (!ok || s.top > last) {
		return s.top, true
	}
	return last, ok
}

func (s *Secure) raiseTop(sender byte, fcnt uint32) {
	if s.marks == nil || !s.markOne || sender != s.marked || s.topSet && fcnt <= s.top {
		return
	}
	s.top, s.topSet = fcnt, true
	s.dirty[sender/32] |= 1 << (sender % 32)
	s.anyDirty = true
}

// RestoreMark puts back a stored mark: every counter up to last is refused.
func (s *Secure) RestoreMark(sender byte, last uint32) {
	if w := &s.windows[sender]; !w.used || last > w.last {
		*w = RestoredWindow(last)
	}
}

// EachMark lists the persisted marks (a store compacts with them): the
// controller's after SetMarks, before it every sender heard from; false
// when v stopped it.
func (s *Secure) EachMark(v MarkVisitor) bool {
	for i := range s.windows {
		if s.markOne && byte(i) != s.marked {
			continue
		}
		if last, ok := s.Mark(byte(i)); ok && !v.Visit(byte(i), last) {
			return false
		}
	}
	return true
}

// MarksDue is when FlushMarks next has a mark to store (Never: none).
func (s *Secure) MarksDue() Time {
	if s.marks == nil || !s.anyDirty {
		return Never
	}
	if !s.markedYet {
		return 0
	}
	return after(s.markedAt, MarkInterval)
}

// FlushMarks stores the marks that moved, at most once per MarkInterval;
// force ignores the interval (before a reboot). A mark not stored is tried
// again next time.
func (s *Secure) FlushMarks(now Time, force bool) {
	if !force && now < s.MarksDue() || s.marks == nil || !s.anyDirty {
		return
	}
	s.anyDirty = false
	for i := range s.dirty {
		for bits := s.dirty[i]; bits != 0; bits &= bits - 1 {
			b := uint32(0)
			for bits&(1<<b) == 0 {
				b++
			}
			sender := byte(i*32) + byte(b)
			last, _ := s.Mark(sender)
			if s.marks.Mark(sender, last) {
				s.dirty[i] &^= 1 << b
			} else {
				s.anyDirty = true
			}
		}
	}
	s.markedAt, s.markedYet = now, true
}

// SealFrame seals a plain frame (the layout above this layer, route trailer
// included) under the next counter into dst, which needs
// len(plain)+Overhead+AppOverhead bytes. ok is false when the counter could
// not be reserved or is exhausted: the frame must not go out.
func (s *Secure) SealFrame(dst, plain []byte) (int, bool) {
	if s.fcnt == 0xFFFFFFFF {
		return 0, false
	}
	next := s.fcnt + 1
	if s.store != nil && next > s.reserved {
		bound := next + TxReserve
		if bound < next || !s.store.ReserveTx(bound) {
			s.Stats.ReserveFailed++
			return 0, false
		}
		s.reserved = bound
	}
	s.fcnt = next
	data := plain
	if s.App {
		if n := s.appSeal(s.scratch[:], plain, next); n > 0 {
			data = s.scratch[:n]
		}
	}
	s.Stats.Sealed++
	return SealNetwork(s.network, dst, data, next, data[1]), true
}

// OpenFrame opens a frame off the air into dst (len(raw) bytes) and returns
// the plain frame's length (the layout above this layer), or why it is
// dropped. Order as secure.SecureTransport._on_frame: MIC, replay check,
// application layer, and only then the window moves.
func (s *Secure) OpenFrame(dst, raw []byte) (int, Reject) {
	if len(raw) == 0 {
		return 0, AuthFailed
	}
	if raw[0]&FlagSecure == 0 {
		s.Stats.PlainDropped++
		return 0, PlainDropped
	}
	n, fcnt, ok := OpenNetwork(s.network, s.scratch[:], raw)
	if !ok {
		s.Stats.AuthFailed++
		return 0, AuthFailed
	}
	plain := s.scratch[:n]
	window := &s.windows[plain[1]]
	if !window.Check(fcnt) {
		s.Stats.Replayed++
		return 0, Replayed
	}
	s.LastApp = s.App && plain[0]&FlagApp != 0
	if s.App {
		m, why := s.appOpen(dst, plain, fcnt)
		if why != Accepted {
			switch why {
			case AppFailed:
				s.Stats.AppFailed++
			case AppMissing:
				s.Stats.AppMissing++
			case AppNoKey:
				s.Stats.AppNoKey++
			}
			return 0, why
		}
		n = m
	} else {
		copy(dst, plain)
	}
	window.Accept(fcnt)
	s.raiseTop(plain[1], fcnt)
	s.Stats.Opened++
	return n, Accepted
}

func (s *Secure) remember(peer byte, frame []byte) {
	id := MessageID(frame)
	for i := 0; i < s.nExchange; i++ {
		if s.exchanges[i].peer == peer && s.exchanges[i].id == id {
			copy(s.exchanges[i:], s.exchanges[i+1:s.nExchange])
			s.nExchange--
			break
		}
	}
	if s.nExchange == AppExchanges {
		copy(s.exchanges[:], s.exchanges[1:])
		s.nExchange--
	}
	s.exchanges[s.nExchange] = exchange{peer, id}
	s.nExchange++
}

func (s *Secure) exchanged(peer byte, frame []byte) bool {
	id := MessageID(frame)
	for i := 0; i < s.nExchange; i++ {
		if s.exchanges[i].peer == peer && s.exchanges[i].id == id {
			return true
		}
	}
	return false
}

// carriesApp: frame (unrouted, KIND without flags) goes under the
// application layer; peer is the other end.
func (s *Secure) carriesApp(frame []byte, peer byte) bool {
	kind := frame[0]
	if appLen(kind) == 0 || len(frame) != appLen(kind) {
		return false
	}
	return kind != KindReply || s.exchanged(peer, frame)
}

// appSeal writes plain with its application payload sealed under fcnt into
// dst; 0 when plain does not carry the layer (sent as it is).
func (s *Secure) appSeal(dst, plain []byte, fcnt uint32) int {
	cut, ok := RouteCut(plain)
	if !ok || cut < 5 {
		return 0
	}
	routed := plain[0] & FlagRouted
	var frame [7]byte
	if cut > len(frame) {
		return 0
	}
	copy(frame[:], plain[:cut])
	frame[0] &^= FlagRouted
	f := frame[:cut]
	if !s.carriesApp(f, f[2]) {
		return 0
	}
	key := s.Keys.current(s.Keys.nodeOf(f[1], f[2]))
	if key == nil {
		s.Stats.AppNoKey++ // goes without: the receiver drops it
		return 0
	}
	if f[0] == KindRequest || f[0] == KindResult {
		s.remember(f[2], f)
	}
	dst[0] = f[0] | FlagApp // ROUTED goes in after sealing: not in the nonce or AAD
	copy(dst[1:5], f[1:5])
	dst[5] = key.version
	c := CCM{C: s.cipher, Key: &key.key}
	var n int
	if f[0] == KindReport {
		// v3: SUBJECT and EVENT in clear, authenticated.
		copy(dst[6:], f[5:])
		head := dst[:6+len(f)-5]
		nonce := appNonce(head, fcnt)
		n = len(head) + c.Seal(dst[len(head):], &nonce, nil, head)
	} else {
		nonce := appNonce(dst[:6], fcnt)
		n = 6 + c.Seal(dst[6:], &nonce, f[5:], dst[:6])
	}
	n += copy(dst[n:], plain[cut:])
	dst[0] |= routed
	s.Stats.AppSealed++
	return n
}

// appOpen writes the frame above the application layer into dst.
func (s *Secure) appOpen(dst, plain []byte, fcnt uint32) (int, Reject) {
	cut, ok := RouteCut(plain)
	if !ok || cut < 5 {
		return 0, AppFailed
	}
	routed := plain[0] & FlagRouted
	frame := plain[:cut]
	kindByte := frame[0] &^ FlagRouted
	if kindByte&FlagApp == 0 {
		var head [7]byte
		if cut <= len(head) {
			copy(head[:], frame)
			head[0] = kindByte
			if s.carriesApp(head[:cut], frame[1]) {
				return 0, AppMissing
			}
		}
		return copy(dst, plain), Accepted
	}
	kind := kindByte &^ FlagApp
	if appLen(kind) == 0 || cut != appLen(kind)+AppOverhead {
		return 0, AppFailed
	}
	key := s.Keys.find(s.Keys.nodeOf(frame[1], frame[2]), frame[5])
	if key == nil {
		return 0, AppNoKey
	}
	c := CCM{C: s.cipher, Key: &key.key}
	var head [6]byte
	copy(head[:], frame[:6])
	head[0] = kindByte
	nonce := appNonce(head[:], fcnt)
	dst[0] = kind | routed
	copy(dst[1:5], frame[1:5])
	var n int
	if kind == KindReport {
		var aad [8]byte
		copy(aad[:], frame[:cut-MICLen])
		aad[0] = kindByte
		if _, ok := c.Open(nil, &nonce, frame[cut-MICLen:], aad[:cut-MICLen]); !ok {
			return 0, AppFailed
		}
		n = 5 + copy(dst[5:], frame[6:cut-MICLen])
	} else {
		m, ok := c.Open(dst[5:], &nonce, frame[6:], head[:])
		if !ok {
			return 0, AppFailed
		}
		n = 5 + m
	}
	if kind == KindRequest || kind == KindResult {
		var opened [7]byte
		copy(opened[:], dst[:n])
		opened[0] = kind
		s.remember(frame[1], opened[:n])
	}
	n += copy(dst[n:], plain[cut:])
	s.Stats.AppOpened++
	return n, Accepted
}
