import json
import os
import stat
from datetime import datetime, timezone

import pytest

from mailhub_mcp.core.account_book import AccountBook, BookEntry
from mailhub_mcp.core.errors import MailhubError

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
LATER = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)


def test_round_trip_of_every_field(tmp_path):
    path = tmp_path / "accounts.json"
    AccountBook(path).record_linked("work", "gmail", "me@example.com", NOW)
    entry = AccountBook(path).get("work")
    assert entry == BookEntry("work", "gmail", "me@example.com", NOW.isoformat(), None)
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert set(data["accounts"]["work"]) == {"provider", "address", "linked_at", "removed_at"}


def test_record_linked_lowercases_address(tmp_path):
    book = AccountBook(tmp_path / "accounts.json")
    book.record_linked("a", "gmail", "Me@Gmail.com", NOW)
    assert book.get("a").address == "me@gmail.com"


def test_record_address_fills_missing_but_never_overwrites(tmp_path):
    book = AccountBook(tmp_path / "accounts.json")
    book.record_address("a", "gmail", "First@x.com")
    assert book.get("a").address == "first@x.com"
    assert book.get("a").linked_at is None
    book.record_address("a", "gmail", "other@x.com")
    assert book.get("a").address == "first@x.com"


def test_record_removed_keeps_address_and_hides_from_lookup(tmp_path):
    book = AccountBook(tmp_path / "accounts.json")
    book.record_linked("a", "gmail", "me@x.com", NOW)
    assert book.alias_for_address("ME@x.com") == "a"
    book.record_removed("a", LATER)
    entry = book.get("a")
    assert entry.address == "me@x.com"
    assert entry.removed_at == LATER.isoformat()
    assert book.alias_for_address("me@x.com") is None


def test_relink_clears_removed_at(tmp_path):
    book = AccountBook(tmp_path / "accounts.json")
    book.record_linked("a", "gmail", "me@x.com", NOW)
    book.record_removed("a", LATER)
    book.record_linked("a", "gmail", "me@x.com", LATER)
    assert book.get("a").removed_at is None


def test_entries_sorted_and_missing_get(tmp_path):
    book = AccountBook(tmp_path / "accounts.json")
    book.record_linked("zed", "gmail", "z@x.com", NOW)
    book.record_linked("abe", "gmail", "a@x.com", NOW)
    assert [e.alias for e in book.entries()] == ["abe", "zed"]
    assert book.get("nope") is None


def test_missing_file_reads_empty(tmp_path):
    book = AccountBook(tmp_path / "nope" / "accounts.json")
    assert book.entries() == []
    assert book.alias_for_address("a@x.com") is None


def test_corrupt_file_raises_and_is_not_reset(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text("{not json")
    book = AccountBook(path)
    with pytest.raises(MailhubError, match="accounts.json"):
        book.entries()
    with pytest.raises(MailhubError):
        book.record_linked("a", "gmail", "a@x.com", NOW)
    assert path.read_text() == "{not json"


def test_written_file_is_mode_600(tmp_path):
    path = tmp_path / "accounts.json"
    AccountBook(path).record_linked("a", "gmail", "a@x.com", NOW)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
