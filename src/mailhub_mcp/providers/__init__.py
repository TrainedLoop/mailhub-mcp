from collections.abc import Callable
from pathlib import Path

from mailhub_mcp.providers.gmail.authorize import authorize as gmail_authorize
from mailhub_mcp.providers.gmail.provider import GmailProvider

PROVIDERS: dict[str, type] = {"gmail": GmailProvider}
AUTHORIZERS: dict[str, Callable[[Path], str]] = {"gmail": gmail_authorize}
