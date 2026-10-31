"""Account book: remembers each alias's mailbox address so a re-link can only attach the same mailbox.

Entries survive removal (the address is kept), which is what lets the admin page refuse a different
mailbox under an existing alias. An unreadable file raises instead of resetting, since resetting would
drop that identity guard.
"""

import contextlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mailhub_mcp.core.errors import MailhubError

VERSION = 1


@dataclass(frozen=True)
class BookEntry:
    alias: str
    provider: str
    address: str | None
    linked_at: str | None
    removed_at: str | None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


class AccountBook:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def get(self, alias: str) -> BookEntry | None:
        with self._lock:
            return self._entry(alias, self._load().get(alias))

    def entries(self) -> list[BookEntry]:
        with self._lock:
            accounts = self._load()
            return [self._entry(alias, accounts[alias]) for alias in sorted(accounts)]

    def record_linked(self, alias: str, provider: str, address: str, now: datetime) -> None:
        with self._lock:
            accounts = self._load()
            accounts[alias] = {
                "provider": provider,
                "address": address.lower(),
                "linked_at": _iso(now),
                "removed_at": None,
            }
            self._save(accounts)

    def record_address(self, alias: str, provider: str, address: str) -> None:
        with self._lock:
            accounts = self._load()
            record = accounts.get(alias)
            if record and record.get("address"):
                return
            accounts[alias] = {
                "provider": provider,
                "address": address.lower(),
                "linked_at": record.get("linked_at") if record else None,
                "removed_at": record.get("removed_at") if record else None,
            }
            self._save(accounts)

    def record_removed(self, alias: str, now: datetime, provider: str | None = None) -> None:
        """Mark `alias` removed, keeping its address. An alias the book never saw gets an entry
        without an address when `provider` is given, so a removal always leaves a record."""
        with self._lock:
            accounts = self._load()
            record = accounts.get(alias)
            if record is None:
                if provider is None:
                    return
                record = accounts[alias] = {
                    "provider": provider, "address": None, "linked_at": None, "removed_at": None,
                }
            record["removed_at"] = _iso(now)
            self._save(accounts)

    def alias_for_address(self, address: str) -> str | None:
        wanted = address.lower()
        with self._lock:
            accounts = self._load()
            for alias in sorted(accounts):
                record = accounts[alias]
                if record.get("address") == wanted and not record.get("removed_at"):
                    return alias
        return None

    @staticmethod
    def _entry(alias: str, record: dict | None) -> BookEntry | None:
        if record is None:
            return None
        return BookEntry(
            alias=alias,
            provider=record.get("provider", ""),
            address=record.get("address"),
            linked_at=record.get("linked_at"),
            removed_at=record.get("removed_at"),
        )

    def _load(self) -> dict[str, dict]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise MailhubError(f"Cannot read the account book at {self.path}: {e}") from e
        try:
            data = json.loads(raw)
            accounts = data["accounts"]
            if not isinstance(accounts, dict) or not all(isinstance(v, dict) for v in accounts.values()):
                raise TypeError("accounts must map aliases to objects")
        except (ValueError, KeyError, TypeError) as e:
            raise MailhubError(f"The account book at {self.path} is unreadable ({e}); fix or remove it") from e
        return accounts

    def _save(self, accounts: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": VERSION, "accounts": accounts}, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
