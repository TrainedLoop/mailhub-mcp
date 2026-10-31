"""Environment-derived settings."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from mailhub_mcp.core.content import MAX_BODY_CHARS
from mailhub_mcp.core.organize import DEFAULT_ORG_LABEL

# Callbacks of claude.ai custom connectors; the default allowlist when ALLOWED_REDIRECT_URIS is unset.
DEFAULT_REDIRECT_URIS: tuple[str, ...] = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)

MAX_ORG_LABEL_CHARS = 100
# Labels that exist in every mailbox or that the server refuses to change; none may be the organized label.
_RESERVED_LABELS = frozenset(
    {"INBOX", "SENT", "DRAFT", "DRAFTS", "SPAM", "TRASH", "CHAT", "UNREAD", "STARRED", "IMPORTANT",
     "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS"}
)


@dataclass(frozen=True)
class Settings:
    public_base_url: str
    google_client_id: str
    google_client_secret: str
    allowed_emails: frozenset[str]
    jwt_signing_key: str
    allowed_redirect_uris: tuple[str, ...]
    org_label: str
    accounts_dir: Path
    journal_path: Path
    port: int
    max_changes_per_day: int = 2000
    tools_mode: Literal["all", "read-only"] = "all"
    # Gmail quota units each mailbox may spend per minute (80% of the 6,000 per-user limit).
    gmail_units_per_minute: int = 4800
    # Default length of message bodies returned by the read tools; 0 means the full body.
    max_body_chars: int = 2000
    # Serve the owner admin page at /admin (ADMIN=on|off).
    admin: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            public_base_url=os.environ["PUBLIC_BASE_URL"].rstrip("/"),
            google_client_id=os.environ["GOOGLE_CLIENT_ID"],
            google_client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            allowed_emails=frozenset(
                e.strip().lower() for e in os.environ["ALLOWED_EMAILS"].split(",") if e.strip()
            ),
            jwt_signing_key=os.environ["JWT_SIGNING_KEY"],
            allowed_redirect_uris=_redirect_uris_env("ALLOWED_REDIRECT_URIS"),
            org_label=_org_label_env("ORG_LABEL"),
            accounts_dir=Path(os.environ.get("ACCOUNTS_DIR", "/secrets/accounts")),
            journal_path=Path(os.environ.get("JOURNAL_PATH", "/data/changes.jsonl")),
            port=int(os.environ.get("PORT", "8742")),
            max_changes_per_day=_int_env("MAX_CHANGES_PER_DAY", 2000),
            tools_mode=_tools_mode(os.environ.get("TOOLS", "all")),
            gmail_units_per_minute=_positive_int_env("GMAIL_UNITS_PER_MINUTE", 4800),
            max_body_chars=_body_chars_env("MAX_BODY_CHARS", 2000),
            admin=_on_off_env("ADMIN", default=True),
        )


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a non-negative integer, got {raw!r}") from None
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {raw!r}")
    return value


def _body_chars_env(name: str, default: int) -> int:
    value = _int_env(name, default)
    if value > MAX_BODY_CHARS:
        raise ValueError(f"{name} must be at most {MAX_BODY_CHARS} (0 means the full body), got {value!r}")
    return value


def _positive_int_env(name: str, default: int) -> int:
    value = _int_env(name, default)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return value


def _tools_mode(raw: str) -> Literal["all", "read-only"]:
    value = raw.strip().lower() or "all"
    if value == "all":
        return "all"
    if value == "read-only":
        return "read-only"
    raise ValueError(f"TOOLS must be 'all' or 'read-only', got {raw!r}")


def _on_off_env(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name, "")
    value = raw.strip().lower()
    if not value:
        return default
    if value in ("on", "off"):
        return value == "on"
    raise ValueError(f"{name} must be 'on' or 'off', got {raw!r}")


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})


def _check_redirect_uri(name: str, entry: str) -> None:
    """Accept only exact, unambiguous callbacks.

    FastMCP treats entries as patterns (`*` wildcards; an entry with an empty or "/" path matches every
    path on that host; a loopback entry without a port matches any port), so entries that would silently
    widen the allowlist are refused here.
    """
    bad = ValueError(
        f"{name} entry {entry!r} is not valid: use an absolute https:// callback URL with a path "
        "(or http://localhost / http://127.0.0.1 with an optional port), without wildcards, user info, "
        "query or fragment"
    )
    if any(c in entry for c in "*?[]\\") or any(c.isspace() or ord(c) < 32 for c in entry):
        raise bad
    try:
        parts = urlsplit(entry)
        host = parts.hostname
        parts.port  # raises ValueError on a malformed port
    except ValueError:
        raise bad from None
    if not host or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise bad
    if "#" in entry or "?" in entry:
        raise bad
    if any(seg in (".", "..") for seg in parts.path.split("/")) or "%" in parts.path:
        raise bad
    if parts.scheme == "https":
        if host in _LOOPBACK_HOSTS or parts.path in ("", "/"):
            raise bad
    elif parts.scheme == "http":
        if host not in _LOOPBACK_HOSTS:
            raise bad
    else:
        raise bad


def _redirect_uris_env(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return DEFAULT_REDIRECT_URIS
    entries = tuple(dict.fromkeys(e.strip() for e in raw.split(",") if e.strip()))
    if not entries:
        raise ValueError(
            f"{name} is set but empty, which would lock every MCP client out; list at least one "
            "callback URL or unset it to use the Claude defaults"
        )
    for entry in entries:
        _check_redirect_uri(name, entry)
    return entries


def _org_label_env(name: str) -> str:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return DEFAULT_ORG_LABEL
    value = raw.strip()
    if len(value) > MAX_ORG_LABEL_CHARS:
        raise ValueError(f"{name} must be at most {MAX_ORG_LABEL_CHARS} characters, got {len(value)}")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{name} must not contain control characters, got {value!r}")
    if value.upper() in _RESERVED_LABELS:
        raise ValueError(f"{name} must not be a system label name, got {value!r}")
    if value.startswith("/") or value.endswith("/") or "//" in value:
        raise ValueError(f"{name} must not start or end with '/' or contain '//', got {value!r}")
    return value
