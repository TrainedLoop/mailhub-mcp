"""Credential files on disk: `<accounts_dir>/<provider>/<alias>.json`.

Callers validate provider and alias; nothing here sanitises them.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path


def credential_path(accounts_dir: Path, provider: str, alias: str) -> Path:
    return accounts_dir / provider / f"{alias}.json"


def _private_dir(path: Path) -> None:
    """Create `path` with mode 0o700 if missing; existing directories are left alone."""
    if path.is_dir():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=0o700)
    path.chmod(0o700)  # mkdir's mode is masked by the umask


def write_credential(accounts_dir: Path, provider: str, alias: str, content: str) -> Path:
    """Write the credential atomically (temp file in the same folder, then replace)."""
    target = credential_path(accounts_dir, provider, alias)
    _private_dir(accounts_dir)
    _private_dir(target.parent)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink(missing_ok=True)
        raise
    return target


def remove_credential(accounts_dir: Path, provider: str, alias: str) -> None:
    credential_path(accounts_dir, provider, alias).unlink(missing_ok=True)


def credentials_writable(accounts_dir: Path, providers: Iterable[str]) -> bool:
    """True if a probe file can be created and removed in every provider folder."""
    try:
        _private_dir(accounts_dir)
        for provider in providers:
            folder = accounts_dir / provider
            _private_dir(folder)
            fd, probe = tempfile.mkstemp(dir=folder, prefix=".probe.", suffix=".tmp")
            os.close(fd)
            Path(probe).unlink(missing_ok=True)
    except OSError:
        return False
    return True
