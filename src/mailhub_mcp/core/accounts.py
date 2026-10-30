"""Mail accounts discovered from <accounts_dir>/<provider>/<alias>.json."""

import logging
import re
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from mailhub_mcp.core.errors import AccountNotFound, DuplicateAlias

ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

ProviderFactory = Callable[[str, Path], Any]

log = logging.getLogger(__name__)


class Account:
    def __init__(
        self, alias: str, provider_name: str, credential_path: Path, factory: ProviderFactory
    ):
        self.alias = alias
        self.provider_name = provider_name
        self.credential_path = credential_path
        self._factory = factory
        self._provider: Any = None
        self._lock = threading.Lock()

    @property
    def provider(self) -> Any:
        with self._lock:
            if self._provider is None:
                self._provider = self._factory(self.alias, self.credential_path)
            return self._provider


class AccountRegistry:
    def __init__(self, accounts_dir: Path, providers: Mapping[str, ProviderFactory]):
        self._accounts: dict[str, Account] = {}
        if not accounts_dir.is_dir():
            log.warning("accounts directory not found: %s", accounts_dir)
            return
        paths: dict[str, list[Path]] = {}
        found: dict[str, Account] = {}
        for entry in sorted(accounts_dir.iterdir()):
            if not entry.is_dir():
                log.warning(
                    "Ignoring %s: credentials belong in <provider>/<alias>.json", entry
                )
                continue
            factory = providers.get(entry.name)
            if factory is None:
                log.warning(
                    "Ignoring folder %s: unknown provider '%s' (known: %s)",
                    entry, entry.name, ", ".join(sorted(providers)) or "none",
                )
                continue
            for path in sorted(entry.glob("*.json")):
                if not ALIAS_RE.match(path.stem):
                    log.warning("Ignoring credential file with invalid alias: %s", path)
                    continue
                paths.setdefault(path.stem, []).append(path)
                found[path.stem] = Account(path.stem, entry.name, path, factory)
        for alias, alias_paths in paths.items():
            if len(alias_paths) > 1:
                raise DuplicateAlias(alias, alias_paths)
        self._accounts = found

    def all(self) -> list[Account]:
        return [self._accounts[a] for a in sorted(self._accounts)]

    def aliases(self) -> list[str]:
        return sorted(self._accounts)

    def get(self, alias: str) -> Account:
        try:
            return self._accounts[alias]
        except KeyError:
            raise AccountNotFound(alias, self.aliases()) from None
