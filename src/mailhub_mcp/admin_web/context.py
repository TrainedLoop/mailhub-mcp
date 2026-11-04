"""Everything the admin routes need, built once at startup."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mailhub_mcp.admin_web.attachment_links import AttachmentLinks, derive_link_key
from mailhub_mcp.admin_web.credentials import credentials_writable
from mailhub_mcp.admin_web.identity import GoogleIdentity, OwnerIdentity
from mailhub_mcp.admin_web.session import derive_key
from mailhub_mcp.config import Settings
from mailhub_mcp.core.account_book import AccountBook
from mailhub_mcp.core.accounts import AccountRegistry
from mailhub_mcp.core.journal import Journal
from mailhub_mcp.providers import PROVIDERS, linkers
from mailhub_mcp.providers.base import Linker

log = logging.getLogger("mailhub-mcp")


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
    tool_names: frozenset[str]  # MCP tool names, for the download endpoint's suspicion check
    links: AttachmentLinks
    now: Callable[[], float] = time.time

    def __setattr__(self, name: str, value: object) -> None:
        # Normalised once, here, on construction and on any later assignment: every check can then
        # compare a lowercased email with plain set membership.
        if name == "allowed_emails":
            value = frozenset(str(email).strip().lower() for email in value)  # type: ignore[union-attr]
        super().__setattr__(name, value)


def build_admin_context(
    settings: Settings, registry: AccountRegistry, journal: Journal, tool_names: frozenset[str]
) -> AdminContext:
    """The admin page's context for a running server: Google sign-in and linking on the Web client,
    the account book next to the journal, a probe of whether credentials can be written, and the
    attachment links the download endpoint checks."""
    writable = credentials_writable(settings.accounts_dir, PROVIDERS)
    if not writable:
        log.warning(
            "credentials are read-only in %s: the admin page shows status only and linking is disabled",
            settings.accounts_dir,
        )
    return AdminContext(
        public_base_url=settings.public_base_url,
        allowed_emails=settings.allowed_emails,
        key=derive_key(settings.jwt_signing_key),
        registry=registry,
        book=AccountBook(settings.journal_path.parent / "accounts.json"),
        journal=journal,
        accounts_dir=settings.accounts_dir,
        identity=GoogleIdentity(settings.google_client_id, settings.google_client_secret),
        linkers=linkers(settings.google_client_id, settings.google_client_secret),
        writable=writable,
        tool_names=tool_names,
        links=AttachmentLinks(derive_link_key(settings.jwt_signing_key), settings.public_base_url),
    )
