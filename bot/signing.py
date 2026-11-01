"""
SunX request signing.

Implemented strictly from the documented procedure on
https://sunx.gitbook.io/sunx/developer/quick-start/authentication

Pre-signed text (steps 1-6 of the docs):
    <METHOD>\n
    <host, lower case>\n
    <path>\n
    <parameters, URL-encoded, sorted by ASCII, joined with "&">

Then (step 7):
    HmacSHA256 -> base64(HMAC-SHA256(pre_signed, secret_key))
    Ed25519    -> base64(Ed25519_sign(pre_signed, private_key))

Docs rules that this module follows:
  * "For GET request, all the parameters must be signed."
  * "For POST request, the parameters needn't be signed and they should be put
     in request body."  -> only the auth parameters are signed for POST.
  * Timestamp format "YYYY-MM-DDThh:mm:ss" (UTC), valid for 5 minutes.
  * Hex escapes in the URL encoding must be upper case (":" -> "%3A").
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote

SIGNATURE_VERSION = "2"
AUTH_PARAM_NAMES = ("AccessKeyId", "SignatureMethod", "SignatureVersion", "Timestamp")


def utc_timestamp(now: datetime | None = None) -> str:
    """Timestamp in the documented format: 2017-05-11T15:19:30 (UTC, no offset)."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _encode(value) -> str:
    # quote() already produces upper-case hex escapes, as the docs require.
    return quote(str(value), safe="-_.~")


def canonical_query(params: dict) -> str:
    """Step 4 + 5: URL-encode every value, sort by ASCII, join with '&'."""
    items = [(k, v) for k, v in params.items() if v is not None]
    return "&".join(f"{_encode(k)}={_encode(v)}" for k, v in sorted(items, key=lambda kv: kv[0]))


def pre_signed_text(method: str, host: str, path: str, params: dict) -> str:
    """Step 6: assemble the text that gets signed."""
    return "\n".join([method.upper(), host.lower(), path, canonical_query(params)])


def sign_hmac_sha256(pre_signed: str, secret_key: str) -> str:
    digest = hmac.new(secret_key.encode("utf-8"), pre_signed.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def sign_ed25519(pre_signed: str, private_key_pem_or_b64: str) -> str:
    """Ed25519 signature, base64 encoded.

    The secret may be given as a PEM private key, or as a raw 32-byte seed in
    base64 / hex form.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    text = private_key_pem_or_b64.strip()
    if "BEGIN" in text:
        key = load_pem_private_key(text.encode("utf-8"), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("The PEM key is not an Ed25519 private key.")
    else:
        compact = "".join(text.split())
        # A 32-byte seed in hex is 64 hex digits. Try that before base64: those
        # digits are also valid base64 and would decode to the wrong 48 bytes.
        if len(compact) == 64 and all(c in "0123456789abcdefABCDEF" for c in compact):
            raw = bytes.fromhex(compact)
        else:
            try:
                raw = base64.b64decode(text, validate=True)
            except Exception:
                try:
                    raw = bytes.fromhex(compact)
                except Exception as exc:
                    raise ValueError("SUNX_SECRET_KEY is not a PEM, base64 or hex Ed25519 key.") from exc
            if len(raw) == 48:          # DER-wrapped PKCS#8 seed
                raw = raw[-32:]
        if len(raw) != 32:
            raise ValueError(f"Ed25519 seed must be 32 bytes, got {len(raw)}.")
        key = Ed25519PrivateKey.from_private_bytes(raw)
    return base64.b64encode(key.sign(pre_signed.encode("utf-8"))).decode("ascii")


def build_signed_params(
    *,
    method: str,
    host: str,
    sign_path: str,
    access_key: str,
    secret_key: str,
    signature_method: str = "HmacSHA256",
    business_params: dict | None = None,
    timestamp: str | None = None,
) -> dict:
    """Return the full query-parameter dict, including 'Signature'.

    For GET, business_params are signed together with the auth parameters.
    For POST, pass business_params=None: the docs put them in the JSON body
    and do not sign them.
    """
    if signature_method not in ("HmacSHA256", "Ed25519"):
        raise ValueError("signature_method must be 'HmacSHA256' or 'Ed25519'.")
    params = {
        "AccessKeyId": access_key,
        "SignatureMethod": signature_method,
        "SignatureVersion": SIGNATURE_VERSION,
        "Timestamp": timestamp or utc_timestamp(),
    }
    if business_params:
        params.update({k: v for k, v in business_params.items() if v is not None})
    pre = pre_signed_text(method, host, sign_path, params)
    params["Signature"] = (
        sign_hmac_sha256(pre, secret_key)
        if signature_method == "HmacSHA256"
        else sign_ed25519(pre, secret_key)
    )
    return params
