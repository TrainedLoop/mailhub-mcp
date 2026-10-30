"""The interface every mail provider implements."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal, Protocol

from mailhub_mcp.core.models import Draft, DraftSummary, Label, MessageSummary, Thread


@dataclass
class GetResult:
    found: list = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)  # id -> readable reason


@dataclass
class SearchResult:
    found: list[MessageSummary] = field(default_factory=list)
    unreadable: int = 0


class MailProvider(Protocol):
    name: ClassVar[str]
    protected_labels: ClassVar[frozenset[str]]
    inbox_label: ClassVar[str]
    unread_label: ClassVar[str]
    starred_label: ClassVar[str]
    default_inbox_query: ClassVar[str]
    query_hint: ClassVar[str]
    reauthorize_hint: ClassVar[str]

    def address(self) -> str: ...
    def search(self, query: str, limit: int) -> list[MessageSummary]: ...
    def search_report(self, query: str, limit: int) -> SearchResult: ...
    def list_ids(self, query: str, limit: int, label_ids: list[str] | None = None) -> list[str]:
        """Matching ids, newest first. With `label_ids`, only messages carrying every one of them
        (a cheap way to learn label state: organize relies on it for query-based changes)."""
        ...
    def get_messages(self, ids: list[str], detail: Literal["summary", "full", "labels"]) -> GetResult: ...
    def get_thread(self, thread_id: str) -> Thread: ...
    def list_labels(self) -> list[Label]: ...
    def ensure_label(self, name: str) -> Label: ...
    def modify_labels(self, ids: list[str], add: list[str], remove: list[str]) -> None: ...
    def list_drafts(self, limit: int) -> list[DraftSummary]: ...
    def create_draft(
        self, to: str, subject: str, body: str, cc: str | None, reply_to_message_id: str | None
    ) -> Draft: ...


ProviderFactory = Callable[[str, Path], MailProvider]
