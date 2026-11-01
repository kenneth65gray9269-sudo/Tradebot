"""Signature text matches the documented step 6, then HMAC and Ed25519 round-trip."""
import base64
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from bot.signing import (
    build_signed_params,
    canonical_query,
    pre_signed_text,
    sign_ed25519,
    sign_hmac_sha256,
    utc_timestamp,
)

DOC_QUERY = (
    "AccessKeyId=e2xxxxxx-99xxxxxx-84xxxxxx-7xxxx"
    "&SignatureMethod=HmacSHA256&SignatureVersion=2"
    "&Timestamp=2017-05-11T15%3A19%3A30&order_id=1234567890"
)
DOC_PRE = "\n".join(["GET", "api.sunx.io", "/sapi/v1/trade/order", DOC_QUERY])
# HMAC-SHA256(DOC_PRE, "test-secret"), base64. The published docs signature is not
# reproducible: the example secret was never published.
KNOWN_HMAC = "AgFiE7YjAx0vBFVLa/rbyOY1/SyTihk239vH/chlYTc="


def test_timestamp_format_is_utc_without_offset():
    now = datetime(2017, 5, 11, 15, 19, 30, tzinfo=timezone.utc)
    assert utc_timestamp(now) == "2017-05-11T15:19:30"


def test_canonical_query_sorts_encodes_and_drops_none():
    query = canonical_query({
        "order_id": 1234567890,
        "SignatureMethod": "HmacSHA256",
        "note": "a b",
        "skip": None,
        "Timestamp": "2017-05-11T15:19:30",
        "AccessKeyId": "e2xxxxxx-99xxxxxx-84xxxxxx-7xxxx",
        "SignatureVersion": 2,
    })
    assert "%3A" in query and "%3a" not in query
    assert "a%20b" in query
    assert "skip" not in query
    assert query.index("AccessKeyId") < query.index("SignatureMethod")
    assert query.index("Timestamp") < query.index("order_id")


def test_documented_step6_pre_signed_text():
    params = {
        "AccessKeyId": "e2xxxxxx-99xxxxxx-84xxxxxx-7xxxx",
        "SignatureMethod": "HmacSHA256",
        "SignatureVersion": "2",
        "Timestamp": "2017-05-11T15:19:30",
        "order_id": "1234567890",
    }
    assert pre_signed_text("get", "API.SUNX.IO", "/sapi/v1/trade/order", params) == DOC_PRE


def test_hmac_matches_known_vector_and_get_includes_business_params():
    assert sign_hmac_sha256(DOC_PRE, "test-secret") == KNOWN_HMAC
    signed = build_signed_params(
        method="GET", host="api.sunx.io", sign_path="/sapi/v1/trade/order",
        access_key="e2xxxxxx-99xxxxxx-84xxxxxx-7xxxx", secret_key="test-secret",
        business_params={"order_id": "1234567890"}, timestamp="2017-05-11T15:19:30",
    )
    assert signed["Signature"] == KNOWN_HMAC
    assert signed["order_id"] == "1234567890"


def test_post_signs_only_auth_parameters():
    signed = build_signed_params(
        method="POST", host="api.sunx.io", sign_path="/sapi/v1/trade/order",
        access_key="ak", secret_key="test-secret", business_params=None,
        timestamp="2017-05-11T15:19:30",
    )
    assert "order_id" not in signed
    pre = pre_signed_text("POST", "api.sunx.io", "/sapi/v1/trade/order",
                          {k: v for k, v in signed.items() if k != "Signature"})
    assert "order_id" not in pre
    assert signed["Signature"] == sign_hmac_sha256(pre, "test-secret")


def test_unknown_signature_method_is_rejected():
    with pytest.raises(ValueError):
        build_signed_params(method="GET", host="api.sunx.io", sign_path="/x",
                            access_key="a", secret_key="b", signature_method="MD5")


def test_ed25519_pem_base64_and_hex_round_trip():
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    seed = key.private_bytes(serialization.Encoding.Raw,
                             serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    wrapped = b"\x00" * 16 + seed
    secrets = (pem, base64.b64encode(seed).decode(), seed.hex(), base64.b64encode(wrapped).decode())
    for secret in secrets:
        signature = base64.b64decode(sign_ed25519("sunx", secret))
        key.public_key().verify(signature, b"sunx")


def test_ed25519_rejects_bad_material():
    with pytest.raises(ValueError):
        sign_ed25519("m", base64.b64encode(b"\x01" * 16).decode())
    with pytest.raises(ValueError):
        sign_ed25519("m", "not-a-key")
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = rsa_key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    with pytest.raises(ValueError, match="Ed25519"):
        sign_ed25519("m", pem)
