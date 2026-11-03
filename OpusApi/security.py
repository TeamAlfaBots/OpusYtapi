"""API-key + download-token cryptography (pure Python, no framework imports)."""
import base64
import hashlib
import hmac
import logging
import secrets
import time

from . import config

log = logging.getLogger("opusapi.security")


# ── API keys ─────────────────────────────────────────────────
def generate_api_key() -> str:
    return "opus_" + secrets.token_urlsafe(24)


def hash_key(key: str) -> str:
    """SHA-256 of the key. Indexed in MongoDB so the API can look keys up."""
    return hashlib.sha256(key.encode()).hexdigest()


_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet

        if not config.KEY_ENC_SECRET:
            raise RuntimeError("KEY_ENC_SECRET is not set")
        derived = base64.urlsafe_b64encode(hashlib.sha256(config.KEY_ENC_SECRET.encode()).digest())
        _fernet = Fernet(derived)
    return _fernet


def encrypt_key(key: str) -> str:
    """Reversible encryption so the bot can show the key again."""
    return _get_fernet().encrypt(key.encode()).decode()


def decrypt_key(token):
    """Returns the key, or None if missing / wrong KEY_ENC_SECRET."""
    if not token:
        return None
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except Exception:
        return None


# ── stateless download tokens (survive restarts) ─────────────
_fallback_secret = None


def _secret() -> bytes:
    global _fallback_secret
    secret = config.TOKEN_SECRET or config.KEY_ENC_SECRET
    if secret:
        return secret.encode()
    if _fallback_secret is None:
        _fallback_secret = secrets.token_bytes(32)
        log.warning("TOKEN_SECRET not set: using a random one, tokens die on restart")
    return _fallback_secret


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_token(tg_id: int, video_id: str, type_: str, ttl=None) -> str:
    exp = int(time.time()) + int(ttl or config.TOKEN_TTL)
    payload = f"{tg_id}|{video_id}|{type_}|{exp}".encode()
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()[:16]
    return f"{_b64e(payload)}.{_b64e(sig)}"


def verify_token(token: str):
    """-> (claims | None, error | None); error is 'invalid' or 'expired'."""
    try:
        p, s = token.split(".", 1)
        payload, sig = _b64d(p), _b64d(s)
    except Exception:
        return None, "invalid"
    expect = hmac.new(_secret(), payload, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(sig, expect):
        return None, "invalid"
    try:
        tg_id, video_id, type_, exp = payload.decode().split("|")
        claims = {"tg_id": int(tg_id), "video_id": video_id, "type": type_, "exp": int(exp)}
    except Exception:
        return None, "invalid"
    if time.time() > claims["exp"]:
        return None, "expired"
    return claims, None
