"""Stateless Gmail API operations: every function takes a client `svc`. No send."""

import base64
import time
from email.message import EmailMessage

from mailhub_mcp.providers.gmail.parsing import METADATA_HEADERS, _headers, full, summarize

BATCH_SIZE = 50
LIST_PAGE_SIZE = 500
MAX_ATTEMPTS = 3
_sleep = time.sleep


def status_of(exc: Exception) -> int | None:
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def is_retryable(exc: Exception) -> bool:
    status = status_of(exc)
    return status is not None and (status == 429 or status >= 500)


def _run_batches(svc, ids: list[str], make_request) -> tuple[dict[str, dict], dict[str, Exception]]:
    """Fetch `ids` through batch HTTP requests, retrying transient failures per id."""
    ids = list(dict.fromkeys(ids))
    found: dict[str, dict] = {}
    errors: dict[str, Exception] = {}

    def collect(request_id, response, exception):
        if exception is not None:
            errors[request_id] = exception
        else:
            found[request_id] = response

    pending = ids
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            _sleep(2 ** (attempt - 1))
        for start in range(0, len(pending), BATCH_SIZE):
            batch = svc.new_batch_http_request(callback=collect)
            for item_id in pending[start : start + BATCH_SIZE]:
                batch.add(make_request(item_id), request_id=item_id)
            batch.execute()
        pending = [i for i in pending if i in errors and is_retryable(errors[i])]
        if not pending or attempt == MAX_ATTEMPTS - 1:
            break
        for i in pending:
            del errors[i]
    return found, errors


def batch_get(svc, ids: list[str], fmt: str) -> tuple[list[dict], dict[str, Exception]]:
    def make_request(message_id):
        kwargs = {"userId": "me", "id": message_id, "format": fmt}
        if fmt == "metadata":
            kwargs["metadataHeaders"] = METADATA_HEADERS
        return svc.users().messages().get(**kwargs)

    found, errors = _run_batches(svc, ids, make_request)
    return [found[i] for i in dict.fromkeys(ids) if i in found], errors


def list_ids(svc, query: str, limit: int) -> list[str]:
    ids: list[str] = []
    page_token = None
    while len(ids) < limit:
        resp = (
            svc.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=min(limit - len(ids), LIST_PAGE_SIZE),
                pageToken=page_token,
            )
            .execute()
        )
        ids.extend(ref["id"] for ref in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids[:limit]


def search_report(svc, query: str, max_results: int) -> tuple[list[dict], int]:
    """Summaries of the readable matches, and how many matches could not be read."""
    ids = list_ids(svc, query, max_results)
    msgs, errors = batch_get(svc, ids, "metadata")
    # A 404 means the message vanished between list and fetch: not a read failure.
    unreadable = sum(1 for exc in errors.values() if status_of(exc) != 404)
    return [summarize(m) for m in msgs], unreadable


def search(svc, query: str, max_results: int) -> list[dict]:
    return search_report(svc, query, max_results)[0]


def get_message(svc, message_id: str) -> dict:
    msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
    return full(msg)


def get_thread(svc, thread_id: str) -> dict:
    thread = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = [full(m) for m in thread.get("messages", [])]
    return {"messages": messages, "replied_by_me": any("SENT" in m["labels"] for m in messages)}


def list_labels(svc) -> list[dict]:
    resp = svc.users().labels().list(userId="me").execute()
    return [{"id": l["id"], "name": l["name"], "type": l.get("type", "")} for l in resp.get("labels", [])]


def list_drafts(svc, max_results: int) -> list[dict]:
    ids: list[str] = []
    page_token = None
    while len(ids) < max_results:
        resp = (
            svc.users()
            .drafts()
            .list(
                userId="me",
                maxResults=min(max_results - len(ids), LIST_PAGE_SIZE),
                pageToken=page_token,
            )
            .execute()
        )
        ids.extend(ref["id"] for ref in resp.get("drafts", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    ids = ids[:max_results]
    found, errors = _run_batches(
        svc, ids, lambda i: svc.users().drafts().get(userId="me", id=i, format="metadata")
    )
    for exc in errors.values():
        if status_of(exc) != 404:  # a draft deleted since listing is simply skipped
            raise exc
    return [{"draft_id": found[i]["id"], **summarize(found[i]["message"])} for i in ids if i in found]


def create_draft(
    svc,
    to: str,
    subject: str,
    body: str,
    cc: str | None,
    reply_to_message_id: str | None,
) -> dict:
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc

    thread_id = None
    if reply_to_message_id:
        original = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=reply_to_message_id,
                format="metadata",
                metadataHeaders=["Subject", "Message-ID", "References"],
            )
            .execute()
        )
        h = _headers(original["payload"])
        thread_id = original["threadId"]
        if h.get("message-id"):
            msg["In-Reply-To"] = h["message-id"]
            msg["References"] = f"{h.get('references', '')} {h['message-id']}".strip()
        if not subject:
            original_subject = h.get("subject", "")
            subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"

    msg["Subject"] = subject
    msg.set_content(body)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    message = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    draft = svc.users().drafts().create(userId="me", body={"message": message}).execute()
    return {"draft_id": draft["id"], "message_id": draft["message"]["id"], "thread_id": thread_id}


def ensure_label(svc, name: str) -> dict:
    for label in list_labels(svc):
        if label["name"].lower() == name.lower():
            return label
    created = svc.users().labels().create(userId="me", body={"name": name}).execute()
    return {"id": created["id"], "name": created["name"], "type": created.get("type", "user")}


def batch_modify(svc, ids: list[str], add: list[str], remove: list[str]) -> None:
    for start in range(0, len(ids), 1000):
        body = {"ids": ids[start:start + 1000], "addLabelIds": add, "removeLabelIds": remove}
        svc.users().messages().batchModify(userId="me", body=body).execute()
