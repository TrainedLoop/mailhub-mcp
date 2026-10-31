"""Mail accounts discovered from <accounts_dir>/<provider>/<alias>.json."""

import logging
import re
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from mailhub_mcp.core.errors import AccountNotFound, DuplicateAlias

ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
RESERVED_ALIASES = frozenset({"global"})  # "global" is the preferences scope that applies to every account

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
        self._providers = providers
        self._lock = threading.Lock()
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
                if path.stem in RESERVED_ALIASES:
                    log.warning("Ignoring credential file with reserved alias %r: %s", path.stem, path)
                    continue
                paths.setdefault(path.stem, []).append(path)
                found[path.stem] = Account(path.stem, entry.name, path, factory)
        for alias, alias_paths in paths.items():
            if len(alias_paths) > 1:
                raise DuplicateAlias(alias, alias_paths)
        self._accounts = found

    def upsert(self, alias: str, provider_name: str, credential_path: Path) -> Account:
        """Add or replace an account. A replaced alias gets a fresh Account, dropping cached credentials."""
        factory = self._providers[provider_name]  # KeyError for an unknown provider
        account = Account(alias, provider_name, credential_path, factory)
        with self._lock:
            self._accounts[alias] = account
        return account

    def remove(self, alias: str) -> None:
        with self._lock:
            if alias not in self._accounts:
                raise AccountNotFound(alias, sorted(self._accounts))
            del self._accounts[alias]

    def all(self) -> list[Account]:
        with self._lock:
            return [self._accounts[a] for a in sorted(self._accounts)]

    def aliases(self) -> list[str]:
        with self._lock:
            return sorted(self._accounts)

    def get(self, alias: str) -> Account:
        with self._lock:
            account = self._accounts.get(alias)
            known = sorted(self._accounts)
        if account is None:
            raise AccountNotFound(alias, known)
        return account
