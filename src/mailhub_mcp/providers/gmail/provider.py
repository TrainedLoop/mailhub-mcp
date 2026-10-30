"""Gmail implementation of MailProvider on top of `api` and `parsing`."""

import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from mailhub_mcp.core.errors import AuthExpired, MailhubError, MessageNotFound, TransientError
from mailhub_mcp.core.models import (
    Draft, DraftSummary, Label, LabelState, Message, MessageSummary, Thread,
)
from mailhub_mcp.providers.base import GetResult, SearchResult
from mailhub_mcp.providers.gmail import api
from mailhub_mcp.providers.gmail.parsing import full, summarize

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

_FORMATS = {"summary": "metadata", "full": "full", "labels": "minimal"}


def default_build(creds: Credentials) -> Any:
    if not creds.valid:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


class GmailProvider:
    name = "gmail"
    protected_labels = frozenset({"TRASH", "SPAM", "DRAFT", "SENT", "CHAT"})
    inbox_label = "INBOX"
    unread_label = "UNREAD"
    starred_label = "STARRED"
    default_inbox_query = "in:inbox"
    query_hint = "Gmail search syntax, e.g. from:x is:unread newer_than:7d"
    reauthorize_hint = "uv run --extra authorize mailhub-authorize gmail <client_secret.json> {alias}"

    def __init__(
        self,
        alias: str,
        credential_path: Path,
        build_service: Callable[[Credentials], Any] = default_build,
    ):
        self.alias = alias
        self._credential_path = credential_path
        self._build_service = build_service
        self._creds: Credentials | None = None
        self._address: str | None = None
        self._lock = threading.Lock()

    def _service(self) -> Any:
        # httplib2 is not thread-safe: build a fresh client per call, share only credentials.
        # The lock keeps threads from loading credentials twice or refreshing them concurrently.
        with self._lock:
            if self._creds is None:
                try:
                    self._creds = Credentials.from_authorized_user_file(
                        str(self._credential_path), SCOPES
                    )
                except (FileNotFoundError, ValueError, AttributeError):  # missing or corrupt token file
                    raise AuthExpired(
                        self.alias, self.reauthorize_hint.format(alias=self.alias)
                    ) from None
            return self._build_service(self._creds)

    @contextmanager
    def _translate(self, not_found: list[str] | None = None):
        try:
            yield
        except RefreshError:
            raise AuthExpired(self.alias, self.reauthorize_hint.format(alias=self.alias)) from None
        except TransportError as exc:
            raise TransientError(f"network error: {exc}") from None
        except HttpError as exc:
            status = api.status_of(exc)
            if status == 404 and not_found:
                raise MessageNotFound(not_found) from None
            if status == 429 or (status is not None and status >= 500):
                raise TransientError(f"{status} {exc.reason}") from None
            raise MailhubError(f"{status} {exc.reason}") from None

    def address(self) -> str:
        if self._address is None:
            with self._translate():
                profile = self._service().users().getProfile(userId="me").execute()
            self._address = profile["emailAddress"]
        return self._address

    def search_report(self, query: str, limit: int) -> SearchResult:
        with self._translate():
            found, unreadable = api.search_report(self._service(), query, limit)
        return SearchResult(found=[MessageSummary(**m) for m in found], unreadable=unreadable)

    def search(self, query: str, limit: int) -> list[MessageSummary]:
        return self.search_report(query, limit).found

    def list_ids(self, query: str, limit: int) -> list[str]:
        with self._translate():
            return api.list_ids(self._service(), query, limit)

    def get_messages(self, ids: list[str], detail: Literal["summary", "full", "labels"]) -> GetResult:
        with self._translate():
            msgs, errors = api.batch_get(self._service(), ids, _FORMATS[detail])
        if detail == "labels":
            found = [LabelState(id=m["id"], labels=m.get("labelIds", [])) for m in msgs]
        elif detail == "summary":
            found = [MessageSummary(**summarize(m)) for m in msgs]
        else:
            found = [Message(**full(m)) for m in msgs]
        result = GetResult(found=found)
        for message_id, exc in errors.items():
            if api.status_of(exc) == 404:
                result.missing.append(message_id)
            elif api.is_retryable(exc):
                result.failed[message_id] = "temporary failure, retry"
            else:
                result.failed[message_id] = "could not be read"
        return result

    def get_thread(self, thread_id: str) -> Thread:
        with self._translate(not_found=[thread_id]):
            return Thread(**api.get_thread(self._service(), thread_id))

    def list_labels(self) -> list[Label]:
        with self._translate():
            return [Label(**l) for l in api.list_labels(self._service())]

    def ensure_label(self, name: str) -> Label:
        with self._translate():
            return Label(**api.ensure_label(self._service(), name))

    def modify_labels(self, ids: list[str], add: list[str], remove: list[str]) -> None:
        with self._translate():
            api.batch_modify(self._service(), ids, add, remove)

    def list_drafts(self, limit: int) -> list[DraftSummary]:
        with self._translate():
            return [DraftSummary(**d) for d in api.list_drafts(self._service(), limit)]

    def create_draft(
        self, to: str, subject: str, body: str, cc: str | None, reply_to_message_id: str | None
    ) -> Draft:
        not_found = [reply_to_message_id] if reply_to_message_id else None
        with self._translate(not_found=not_found):
            return Draft(**api.create_draft(self._service(), to, subject, body, cc, reply_to_message_id))
