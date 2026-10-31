"""Everything the admin routes need, built once at startup."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mailhub_mcp.admin_web.identity import OwnerIdentity
from mailhub_mcp.core.account_book import AccountBook
from mailhub_mcp.core.accounts import AccountRegistry
from mailhub_mcp.core.journal import Journal
from mailhub_mcp.providers.base import Linker


@dataclass
class AdminContext:
    public_base_url: str
    allowed_emails: frozenset[str]
    key: bytes
    registry: AccountRegistry
    book: AccountBook
    journal: Journal
    accounts_dir: Path
    identity: OwnerIdentity
    linkers: dict[str, Linker]
    writable: bool
    now: Callable[[], float] = time.time
