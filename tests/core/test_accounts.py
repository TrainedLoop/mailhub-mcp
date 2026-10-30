import logging

import pytest

from mailhub_mcp.core.accounts import AccountRegistry
from mailhub_mcp.core.errors import AccountNotFound, DuplicateAlias


class StubProvider:
    def __init__(self, alias, path):
        self.alias = alias
        self.path = path


def providers(*names):
    return {n: (lambda alias, path: StubProvider(alias, path)) for n in names}


def touch(directory, *names):
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text("{}")


def test_discovers_by_provider_folder(tmp_path):
    touch(tmp_path / "gmail", "b.json", "a.json")
    reg = AccountRegistry(tmp_path, providers("gmail"))
    assert reg.aliases() == ["a", "b"] and reg.get("a").provider_name == "gmail"
    assert reg.get("a").credential_path == tmp_path / "gmail" / "a.json"


def test_duplicate_alias_across_providers_fails(tmp_path):
    touch(tmp_path / "gmail", "x.json")
    touch(tmp_path / "other", "x.json")
    with pytest.raises(DuplicateAlias, match="x"):
        AccountRegistry(tmp_path, providers("gmail", "other"))


def test_unknown_provider_folder_warns(tmp_path, caplog):
    touch(tmp_path / "Gmail", "a.json")
    touch(tmp_path / "outlook", "b.json")
    reg = AccountRegistry(tmp_path, providers("gmail"))
    assert reg.aliases() == [] and "Gmail" in caplog.text and "outlook" in caplog.text


def test_root_level_file_warns(tmp_path, caplog):
    touch(tmp_path, "alice.json")
    assert AccountRegistry(tmp_path, providers("gmail")).aliases() == []
    assert "alice.json" in caplog.text


def test_missing_dir_warns(tmp_path, caplog):
    missing = tmp_path / "nope"
    with caplog.at_level(logging.WARNING):
        assert AccountRegistry(missing, providers("gmail")).all() == []
    assert str(missing) in caplog.text


def test_invalid_alias_warns(tmp_path, caplog):
    touch(tmp_path / "gmail", "Alice.json", "has space.json", "-x.json", "ok.json", "notes.txt")
    assert AccountRegistry(tmp_path, providers("gmail")).aliases() == ["ok"]
    for name in ("Alice.json", "has space.json", "-x.json"):
        assert name in caplog.text


def test_empty_dir_has_no_accounts(tmp_path):
    assert AccountRegistry(tmp_path, providers("gmail")).all() == []


def test_get_unknown_lists_valid(tmp_path):
    touch(tmp_path / "gmail", "unknown-alias.json")
    with pytest.raises(AccountNotFound, match="unknown-alias"):
        AccountRegistry(tmp_path, providers("gmail")).get("zzz")


def test_provider_built_once(tmp_path):
    touch(tmp_path / "gmail", "a.json")
    calls = []

    def factory(alias, path):
        calls.append(alias)
        return StubProvider(alias, path)

    acc = AccountRegistry(tmp_path, {"gmail": factory}).get("a")
    assert acc.provider is acc.provider and calls == ["a"]


def test_concurrent_provider_access_builds_it_once(tmp_path):
    import threading
    import time

    from mailhub_mcp.core.accounts import Account

    calls = []

    def factory(alias, path):
        calls.append(1)
        time.sleep(0.05)
        return StubProvider(alias, path)

    account = Account("a", "gmail", tmp_path / "a.json", factory)
    seen = []
    threads = [threading.Thread(target=lambda: seen.append(account.provider)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert len({id(p) for p in seen}) == 1
