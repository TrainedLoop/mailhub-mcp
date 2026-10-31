"""Signed, stateless admin session and OAuth flow cookies, CSRF and freshness."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SESSION_COOKIE = "mailhub_admin"
FLOW_COOKIE = "mailhub_admin_flow"
SESSION_SECONDS = 8 * 3600
REAUTH_SECONDS = 300
FLOW_SECONDS = 600

_FLOW_KINDS = ("login", "link", "relink")
_DEFAULT_PORTS = {"https": 443, "http": 80}


def derive_key(jwt_signing_key: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"mailhub-admin-session",
    ).derive(jwt_signing_key.encode("utf-8"))


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    raw = text.encode("ascii")  # non-ASCII raises UnicodeEncodeError (a ValueError)
    return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))


class Signer:
    def __init__(self, key: bytes) -> None:
        self._key = key

    def _mac(self, payload: str) -> str:
        return _b64e(hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).digest())

    def dumps(self, obj: dict) -> str:
        payload = _b64e(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        return f"{payload}.{self._mac(payload)}"

    def loads(self, token: str) -> dict | None:
        try:
            payload, sig = token.split(".")
            if not hmac.compare_digest(self._mac(payload).encode("ascii"), sig.encode("ascii")):
                return None
            obj = json.loads(_b64d(payload))
        except (ValueError, TypeError, AttributeError, binascii.Error):
            return None
        return obj if isinstance(obj, dict) else None


@dataclass(frozen=True)
class Session:
    email: str
    sid: str
    login_at: float
    auth_at: float
    has_auth_time: bool = False


@dataclass(frozen=True)
class Flow:
    kind: Literal["login", "link", "relink"]
    state: str
    nonce: str
    code_verifier: str
    created_at: float
    return_to: str = "/admin"
    sid: str | None = None
    email: str | None = None
    alias: str | None = None
    provider: str | None = None


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _from_dict(cls: type, data: dict) -> Any:
    """Build a dataclass strictly: unknown/missing fields or wrong types -> None."""
    fields = {f.name: f for f in dataclasses.fields(cls)}
    required = {
        n for n, f in fields.items()
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    }
    if set(data) - set(fields) or required - set(data):
        return None
    return data


def encode_session(signer: Signer, s: Session) -> str:
    return signer.dumps(dataclasses.asdict(dataclasses.replace(s, email=s.email.lower())))


def decode_session(
    signer: Signer, token: str | None, now: float, allowed: frozenset[str]
) -> Session | None:
    if not token:
        return None
    data = signer.loads(token)
    if data is None or _from_dict(Session, data) is None:
        return None
    if not (
        isinstance(data["email"], str)
        and isinstance(data["sid"], str)
        and _is_number(data["login_at"])
        and _is_number(data["auth_at"])
        and isinstance(data.get("has_auth_time", False), bool)
    ):
        return None
    s = Session(**data)
    if now - s.login_at >= SESSION_SECONDS:
        return None
    if s.email.lower() not in allowed:
        return None
    return s


def encode_flow(signer: Signer, f: Flow) -> str:
    return signer.dumps(dataclasses.asdict(f))


def decode_flow(signer: Signer, token: str | None, now: float) -> Flow | None:
    if not token:
        return None
    data = signer.loads(token)
    if data is None or _from_dict(Flow, data) is None:
        return None
    strs = ("state", "nonce", "code_verifier", "return_to")
    opts = ("sid", "email", "alias", "provider")
    if data["kind"] not in _FLOW_KINDS or not _is_number(data["created_at"]):
        return None
    if not all(isinstance(data[k], str) for k in ("state", "nonce", "code_verifier")):
        return None
    if not all(isinstance(data.get(k, ""), str) for k in strs):
        return None
    if not all(data.get(k) is None or isinstance(data[k], str) for k in opts):
        return None
    f = Flow(**data)
    if now - f.created_at >= FLOW_SECONDS:
        return None
    return f


def is_fresh(s: Session, now: float) -> bool:
    return now - s.auth_at < REAUTH_SECONDS


def csrf_token(key: bytes, s: Session) -> str:
    return _b64e(hmac.new(key, f"csrf:{s.sid}".encode("utf-8"), hashlib.sha256).digest())


def check_csrf(key: bytes, s: Session, token: str | None) -> bool:
    if not token:
        return False
    try:
        return hmac.compare_digest(csrf_token(key, s).encode("ascii"), token.encode("ascii"))
    except UnicodeEncodeError:
        return False


def _origin_tuple(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        if scheme not in _DEFAULT_PORTS or not host:
            return None
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError:
        return None
    return scheme, host.lower(), port


def same_origin(public_base_url: str, origin: str | None, referer: str | None) -> bool:
    candidate = origin if origin is not None else referer
    if not candidate:
        return False
    got = _origin_tuple(candidate)
    return got is not None and got == _origin_tuple(public_base_url)


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = _b64e(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def random_token() -> str:
    return secrets.token_urlsafe(32)
