import os
import stat

import pytest

from mailhub_mcp import cli
from mailhub_mcp.core.accounts import ALIAS_RE
from mailhub_mcp.providers import AUTHORIZERS, PROVIDERS


@pytest.fixture
def fake_gmail(monkeypatch):
    calls = []

    def fake(client_secret):
        calls.append(client_secret)
        return '{"token": "x"}'

    monkeypatch.setitem(AUTHORIZERS, "gmail", fake)
    return calls


def run(tmp_path, *args):
    return cli.main([*args, "--accounts-dir", str(tmp_path)])


def test_registries_have_same_keys():
    assert set(PROVIDERS) == set(AUTHORIZERS)


def test_unknown_provider(tmp_path, capsys, fake_gmail):
    assert run(tmp_path, "outlook", "cs.json", "x") == 2
    assert "outlook" in capsys.readouterr().err


def test_invalid_alias(tmp_path, capsys, fake_gmail):
    assert run(tmp_path, "gmail", "cs.json", "Alice") == 2
    assert ALIAS_RE.pattern in capsys.readouterr().err
    assert fake_gmail == []


def test_refuses_alias_in_other_provider(tmp_path, capsys, fake_gmail, monkeypatch):
    (tmp_path / "gmail").mkdir()
    (tmp_path / "gmail" / "x.json").write_text("{}")
    monkeypatch.setitem(AUTHORIZERS, "outlook", lambda p: "{}")
    assert run(tmp_path, "outlook", "cs.json", "x") == 2
    assert "gmail" in capsys.readouterr().err
    assert not (tmp_path / "outlook").exists()


def test_writes_credential_with_mode_600(tmp_path, capsys, fake_gmail):
    assert run(tmp_path, "gmail", "cs.json", "alice-personal") == 0
    out = tmp_path / "gmail" / "alice-personal.json"
    assert out.read_text() == '{"token": "x"}'
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert str(out) in capsys.readouterr().out


def test_relink_overwrites(tmp_path, fake_gmail, monkeypatch):
    run(tmp_path, "gmail", "cs.json", "alice-personal")
    monkeypatch.setitem(AUTHORIZERS, "gmail", lambda p: '{"token": "y"}')
    assert run(tmp_path, "gmail", "cs.json", "alice-personal") == 0
    out = tmp_path / "gmail" / "alice-personal.json"
    assert out.read_text() == '{"token": "y"}'
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_creates_account_dirs_with_mode_700(tmp_path, fake_gmail):
    root = tmp_path / "secrets" / "accounts"
    old = os.umask(0o022)
    try:
        assert cli.main(["gmail", "cs.json", "x", "--accounts-dir", str(root)]) == 0
    finally:
        os.umask(old)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "gmail").stat().st_mode) == 0o700


def test_existing_dirs_are_left_alone(tmp_path, fake_gmail):
    (tmp_path / "gmail").mkdir(mode=0o755)
    (tmp_path / "gmail").chmod(0o755)
    tmp_path.chmod(0o755)
    assert run(tmp_path, "gmail", "cs.json", "x") == 0
    assert stat.S_IMODE((tmp_path / "gmail").stat().st_mode) == 0o755


def test_refuses_reserved_alias_global(tmp_path, capsys, fake_gmail):
    assert run(tmp_path, "gmail", "cs.json", "global") == 2
    assert "global" in capsys.readouterr().err
    assert fake_gmail == []
