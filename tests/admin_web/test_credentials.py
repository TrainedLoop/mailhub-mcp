import os
import stat

import pytest

from mailhub_mcp.admin_web import credentials
from mailhub_mcp.admin_web.credentials import (
    credential_path,
    credentials_writable,
    remove_credential,
    write_credential,
)


def test_credential_path_layout(tmp_path):
    assert credential_path(tmp_path, "gmail", "work") == tmp_path / "gmail" / "work.json"


def test_write_credential_content_and_modes(tmp_path):
    path = write_credential(tmp_path, "gmail", "work", '{"a": 1}')
    assert path == tmp_path / "gmail" / "work.json"
    assert path.read_text() == '{"a": 1}'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_write_credential_replaces_existing(tmp_path):
    write_credential(tmp_path, "gmail", "work", "old")
    write_credential(tmp_path, "gmail", "work", "new")
    assert credential_path(tmp_path, "gmail", "work").read_text() == "new"
    assert list((tmp_path / "gmail").glob("*.tmp")) == []


def test_failed_replace_keeps_old_file_and_leaves_no_temp(tmp_path, monkeypatch):
    write_credential(tmp_path, "gmail", "work", "old")

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(credentials.os, "replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        write_credential(tmp_path, "gmail", "work", "new")
    assert credential_path(tmp_path, "gmail", "work").read_text() == "old"
    assert list((tmp_path / "gmail").glob("*.tmp")) == []


def test_remove_credential_missing_is_noop(tmp_path):
    remove_credential(tmp_path, "gmail", "ghost")


def test_remove_credential_removes(tmp_path):
    path = write_credential(tmp_path, "gmail", "work", "x")
    remove_credential(tmp_path, "gmail", "work")
    assert not path.exists()


def test_credentials_writable_true(tmp_path):
    assert credentials_writable(tmp_path, ["gmail"]) is True
    assert list((tmp_path / "gmail").iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_credentials_writable_false_when_read_only(tmp_path):
    folder = tmp_path / "gmail"
    folder.mkdir()
    folder.chmod(0o500)
    try:
        assert credentials_writable(tmp_path, ["gmail"]) is False
    finally:
        folder.chmod(0o700)
