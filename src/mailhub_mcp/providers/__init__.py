from collections.abc import Callable
from functools import partial
from pathlib import Path

from mailhub_mcp.providers.base import ProviderFactory
from mailhub_mcp.providers.gmail.api import DEFAULT_UNITS_PER_MINUTE
from mailhub_mcp.providers.gmail.authorize import authorize as gmail_authorize
from mailhub_mcp.providers.gmail.provider import GmailProvider

PROVIDERS: dict[str, type] = {"gmail": GmailProvider}
AUTHORIZERS: dict[str, Callable[[Path], str]] = {"gmail": gmail_authorize}


def provider_factories(gmail_units_per_minute: int = DEFAULT_UNITS_PER_MINUTE) -> dict[str, ProviderFactory]:
    """Factories for the account registry, with the configured per-mailbox quota budgets."""
    return {"gmail": partial(GmailProvider, units_per_minute=gmail_units_per_minute)}
