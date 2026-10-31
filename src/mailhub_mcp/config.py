"""Environment-derived settings."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mailhub_mcp.core.content import MAX_BODY_CHARS


@dataclass(frozen=True)
class Settings:
    public_base_url: str
    google_client_id: str
    google_client_secret: str
    allowed_emails: frozenset[str]
    jwt_signing_key: str
    accounts_dir: Path
    journal_path: Path
    port: int
    max_changes_per_day: int = 2000
    tools_mode: Literal["all", "read-only"] = "all"
    # Gmail quota units each mailbox may spend per minute (80% of the 6,000 per-user limit).
    gmail_units_per_minute: int = 4800
    # Default length of message bodies returned by the read tools; 0 means the full body.
    max_body_chars: int = 2000

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
            accounts_dir=Path(os.environ.get("ACCOUNTS_DIR", "/secrets/accounts")),
            journal_path=Path(os.environ.get("JOURNAL_PATH", "/data/changes.jsonl")),
            port=int(os.environ.get("PORT", "8742")),
            max_changes_per_day=_int_env("MAX_CHANGES_PER_DAY", 2000),
            tools_mode=_tools_mode(os.environ.get("TOOLS", "all")),
            gmail_units_per_minute=_positive_int_env("GMAIL_UNITS_PER_MINUTE", 4800),
            max_body_chars=_body_chars_env("MAX_BODY_CHARS", 2000),
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
