"""build_admin_context: the single place the server turns settings into an AdminContext."""

import dataclasses
import logging

import pytest

from mailhub_mcp.admin_web.context import build_admin_context
from mailhub_mcp.admin_web.identity import GoogleIdentity
from mailhub_mcp.admin_web.session import derive_key
from mailhub_mcp.config import Settings
from mailhub_mcp.core.accounts import AccountRegistry
from mailhub_mcp.core.journal import Journal
from mailhub_mcp.providers import provider_factories
from mailhub_mcp.providers.gmail.link import GmailLinker


def _settings(tmp_path):
    return dataclasses.replace(
        Settings.from_env(),
        accounts_dir=tmp_path / "accounts",
        journal_path=tmp_path / "data" / "changes.jsonl",
    )


def _build(settings):
    registry = AccountRegistry(settings.accounts_dir, provider_factories())
    journal = Journal(settings.journal_path)
    return build_admin_context(settings, registry, journal), registry, journal


def test_context_is_built_from_settings(tmp_path):
    settings = _settings(tmp_path)
    ctx, registry, journal = _build(settings)
    assert ctx.public_base_url == settings.public_base_url
    assert ctx.allowed_emails == settings.allowed_emails
    assert ctx.key == derive_key(settings.jwt_signing_key)
    assert ctx.registry is registry and ctx.journal is journal
    assert ctx.accounts_dir == settings.accounts_dir
    assert ctx.book.path == tmp_path / "data" / "accounts.json"  # next to the journal
    assert isinstance(ctx.identity, GoogleIdentity)
    assert set(ctx.linkers) == {"gmail"} and isinstance(ctx.linkers["gmail"], GmailLinker)
    assert ctx.writable is True
    assert (tmp_path / "accounts" / "gmail").is_dir()


def test_read_only_accounts_dir_disables_linking_with_a_warning(tmp_path, caplog):
    settings = _settings(tmp_path)
    settings.accounts_dir.mkdir()
    settings.accounts_dir.chmod(0o500)  # no gmail folder and no way to create it
    try:
        with caplog.at_level(logging.WARNING, logger="mailhub-mcp"):
            ctx, _, _ = _build(settings)
    finally:
        settings.accounts_dir.chmod(0o700)
    assert ctx.writable is False
    assert any("read-only" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def test_writable_accounts_dir_logs_no_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="mailhub-mcp"):
        _build(_settings(tmp_path))
    assert not [r for r in caplog.records if "read-only" in r.getMessage()]
