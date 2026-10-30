"""Environment-derived settings."""

import os
from dataclasses import dataclass
from pathlib import Path


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
        )
