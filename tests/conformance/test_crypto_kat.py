"""AES-CCM as the stack uses it: Wycheproof vectors
(``aes_ccm_test.json``, C2SP/wycheproof, Apache-2.0), an independent CCM
implementation run against ``cryptography``, and the spec's byte layout."""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

import crypto_kat  # noqa: E402


def test_wycheproof_vectors():
    out = crypto_kat.wycheproof()
    assert out["vectors"] > 500 and not out["lib_mismatch"] and not out["ours_mismatch"], out


@pytest.mark.parametrize("check", [crypto_kat.differential, crypto_kat.layout])
def test_independent_ccm_and_layout(check):
    assert check(2_000)["mismatch"] == 0


@pytest.mark.long
@pytest.mark.parametrize("check", [crypto_kat.differential, crypto_kat.layout])
def test_independent_ccm_and_layout_long(check):
    assert check(200_000)["mismatch"] == 0
