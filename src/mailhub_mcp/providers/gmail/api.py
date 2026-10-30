"""Stateless Gmail API operations: every function takes a client `svc`. No send."""

import base64
import json
import random
import threading
import time
from email.message import EmailMessage

import httplib2
from googleapiclient.errors import HttpError

from mailhub_mcp.providers.gmail.parsing import METADATA_HEADERS, _headers, full, summarize

BATCH_SIZE = 50
LIST_PAGE_SIZE = 500
# Gmail's per-user limit is 6,000 quota units per minute for projects created since May 2026
# (15,000 for older ones). Budget 80% of it by default: headroom for the moving window and for
# other clients of the same mailbox. GMAIL_UNITS_PER_MINUTE overrides it.
GMAIL_UNITS_PER_MINUTE_LIMIT = 6000
DEFAULT_UNITS_PER_MINUTE = 4800
DEFAULT_UNITS_PER_SECOND = DEFAULT_UNITS_PER_MINUTE / 60
# Quota units per call (https://developers.google.com/workspace/gmail/api/reference/quota).
MESSAGE_GET_UNITS = 20  # format=metadata or minimal
MESSAGE_GET_FULL_UNITS = 60  # format=full or raw
MESSAGE_LIST_UNITS = 5
THREAD_GET_UNITS = 120  # format=full, the only format used here
LABELS_LIST_UNITS = 1
BATCH_MODIFY_UNITS = 50
DRAFT_LIST_UNITS = 5
PROFILE_UNITS = 1
# Not documented in the 2026 table; assumed from the costs of comparable calls.
LABELS_CREATE_UNITS = 5
DRAFT_GET_UNITS = 20  # format=metadata, like messages.get
DRAFT_CREATE_UNITS = 10


def message_get_units(fmt: str) -> int:
    return MESSAGE_GET_FULL_UNITS if fmt in ("full", "raw") else MESSAGE_GET_UNITS
# The only retry layer (googleapiclient's own retries are off: num_retries=0). Waits before each
# retry, plus up to 1 s of random jitter. A rate limit needs the moving window to drain, so it
# backs off longer; Google recommends starting at 1 s or more, doubling, capped at 32-64 s.
SERVER_ERROR_BACKOFF = (1, 2, 4)  # 5xx and network errors
RATE_LIMIT_BACKOFF = (2, 4, 8, 16, 32)  # 429 and 403 rate-limit reasons
_RATE_LIMIT_REASONS = frozenset({"ratelimitexceeded", "userratelimitexceeded", "rate_limit_exceeded"})
_NETWORK_ERRORS = (ConnectionError, TimeoutError, httplib2.ServerNotFoundError)
_sleep = time.sleep
_pace_sleep = time.sleep
_now = time.monotonic
_random = random.random  # jitter in [0, 1)


def status_of(exc: Exception) -> int | None:
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _reason_set(entries) -> set[str]:
    if not isinstance(entries, list):
        return set()
    return {
        e["reason"].lower() for e in entries if isinstance(e, dict) and isinstance(e.get("reason"), str)
    }


def _reasons(exc: Exception) -> set[str]:
    """Lower-cased `reason` of every error entry, from every source: the library's parsed
    `error_details` (which holds `details` when the body has it, else `errors`), and the JSON body's
    `error.errors[*]` (legacy reasons such as rateLimitExceeded) and `error.details[*]`
    (google.rpc.ErrorInfo reasons such as RATE_LIMIT_EXCEEDED)."""
    reasons = _reason_set(getattr(exc, "error_details", None))
    try:
        data = json.loads(getattr(exc, "content", b"") or b"")
    except (ValueError, TypeError):
        return reasons
    if isinstance(data, list) and data:  # some endpoints wrap the error in a list
        data = data[0]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        reasons |= _reason_set(error.get("errors")) | _reason_set(error.get("details"))
    return reasons


def is_rate_limited(exc: Exception) -> bool:
    """429 (including "Too many concurrent requests for user"), or 403 whose error reason is a
    per-user rate limit (not daily limits or permissions)."""
    status = status_of(exc)
    if status == 429:
        return True
    return status == 403 and bool(_reasons(exc) & _RATE_LIMIT_REASONS)


def is_retryable(exc: Exception) -> bool:
    status = status_of(exc)
    return status is not None and (status >= 500 or is_rate_limited(exc))


class RateLimiter:
    """Thread-safe pacing for one mailbox, shared by every call on it.

    The budget is in Gmail quota units. Each acquire books its slot after the previous ones, so
    concurrent tool calls on the same account add up to at most `units_per_second`.
    """

    def __init__(self, units_per_second: float, now=None, sleep=None):
        self.units_per_second = units_per_second
        self._interval = 1 / units_per_second
        self._now = now
        self._sleep = sleep
        self._lock = threading.Lock()
        self.next_free = 0.0

    def acquire(self, units: float) -> None:
        now_fn = self._now or _now
        with self._lock:
            now = now_fn()
            start = max(now, self.next_free)
            self.next_free = start + units * self._interval
        if start > now:
            (self._sleep or _pace_sleep)(start - now)


def _retry_wait(exc: Exception, server_retries: int, rate_retries: int) -> tuple[str, int] | None:
    """('rate' | 'server', base wait) for the next retry of a failed call, or None to give up."""
    if isinstance(exc, HttpError) and is_rate_limited(exc):
        return ("rate", RATE_LIMIT_BACKOFF[rate_retries]) if rate_retries < len(RATE_LIMIT_BACKOFF) else None
    if isinstance(exc, _NETWORK_ERRORS) or (isinstance(exc, HttpError) and is_retryable(exc)):
        if server_retries < len(SERVER_ERROR_BACKOFF):
            return "server", SERVER_ERROR_BACKOFF[server_retries]
    return None


def _backoff(wait: float) -> None:
    _sleep(wait + _random())


def _execute(request, limiter: RateLimiter | None, units: int, retry: bool = True):
    """Run one single request, charging `units` to the limiter on every attempt.

    5xx and network errors are retried after 1, 2, 4 s; rate limits after 2, 4, 8, 16, 32 s; each
    wait plus jitter. `retry=False` (draft creation) sends exactly once: a retried create could
    duplicate the draft.
    """
    limiter = limiter or RateLimiter(DEFAULT_UNITS_PER_SECOND)
    retries = {"server": 0, "rate": 0}
    while True:
        limiter.acquire(units)
        try:
            return request.execute(num_retries=0)
        except (HttpError, *_NETWORK_ERRORS) as exc:
            step = _retry_wait(exc, retries["server"], retries["rate"]) if retry else None
            if step is None:
                raise
        kind, wait = step
        retries[kind] += 1
        _backoff(wait)


def _run_batches(
    svc,
    ids: list[str],
    make_request,
    limiter: RateLimiter | None = None,
    units_per_id: int = MESSAGE_GET_UNITS,
) -> tuple[dict[str, dict], dict[str, Exception]]:
    """Fetch `ids` through batch HTTP requests, retrying transient failures per id."""
    ids = list(dict.fromkeys(ids))
    found: dict[str, dict] = {}
    errors: dict[str, Exception] = {}
    limiter = limiter or RateLimiter(DEFAULT_UNITS_PER_SECOND)

    def collect(request_id, response, exception):
        if exception is not None:
            errors[request_id] = exception
        else:
            found[request_id] = response

    retries = {i: {"server": 0, "rate": 0} for i in ids}
    pending = ids
    while pending:
        for start in range(0, len(pending), BATCH_SIZE):
            chunk = pending[start : start + BATCH_SIZE]
            limiter.acquire(len(chunk) * units_per_id)
            batch = svc.new_batch_http_request(callback=collect)
            for item_id in chunk:
                batch.add(make_request(item_id), request_id=item_id)
            batch.execute()
        # Each failed id follows its own schedule (rate limit or server error); a round waits for
        # the longest wait among the ids it retries.
        waits = {}
        for i in pending:
            if i in errors:
                step = _retry_wait(errors[i], retries[i]["server"], retries[i]["rate"])
                if step is not None:
                    retries[i][step[0]] += 1
                    waits[i] = step[1]
        pending = list(waits)
        if pending:
            for i in pending:
                del errors[i]
            _backoff(max(waits.values()))
    return found, errors


def batch_get(
    svc, ids: list[str], fmt: str, limiter: RateLimiter | None = None
) -> tuple[list[dict], dict[str, Exception]]:
    def make_request(message_id):
        kwargs = {"userId": "me", "id": message_id, "format": fmt}
        if fmt == "metadata":
            kwargs["metadataHeaders"] = METADATA_HEADERS
        return svc.users().messages().get(**kwargs)

    found, errors = _run_batches(svc, ids, make_request, limiter, message_get_units(fmt))
    return [found[i] for i in dict.fromkeys(ids) if i in found], errors


def list_ids(svc, query: str, limit: int, limiter: RateLimiter | None = None) -> list[str]:
    limiter = limiter or RateLimiter(DEFAULT_UNITS_PER_SECOND)
    ids: list[str] = []
    page_token = None
    while len(ids) < limit:
        request = (
            svc.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=min(limit - len(ids), LIST_PAGE_SIZE),
                pageToken=page_token,
            )
        )
        resp = _execute(request, limiter, MESSAGE_LIST_UNITS)
        ids.extend(ref["id"] for ref in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids[:limit]


def search_report(
    svc, query: str, max_results: int, limiter: RateLimiter | None = None
) -> tuple[list[dict], int]:
    """Summaries of the readable matches, and how many matches could not be read."""
    limiter = limiter or RateLimiter(DEFAULT_UNITS_PER_SECOND)
    ids = list_ids(svc, query, max_results, limiter)
    msgs, errors = batch_get(svc, ids, "metadata", limiter=limiter)
    # A 404 means the message vanished between list and fetch: not a read failure.
    unreadable = sum(1 for exc in errors.values() if status_of(exc) != 404)
    return [summarize(m) for m in msgs], unreadable


def search(svc, query: str, max_results: int, limiter: RateLimiter | None = None) -> list[dict]:
    return search_report(svc, query, max_results, limiter)[0]


def get_message(svc, message_id: str, limiter: RateLimiter | None = None) -> dict:
    request = svc.users().messages().get(userId="me", id=message_id, format="full")
    msg = _execute(request, limiter, MESSAGE_GET_FULL_UNITS)
    return full(msg)


def get_thread(svc, thread_id: str, limiter: RateLimiter | None = None) -> dict:
    request = svc.users().threads().get(userId="me", id=thread_id, format="full")
    thread = _execute(request, limiter, THREAD_GET_UNITS)
    messages = [full(m) for m in thread.get("messages", [])]
    return {"messages": messages, "replied_by_me": any("SENT" in m["labels"] for m in messages)}


def list_labels(svc, limiter: RateLimiter | None = None) -> list[dict]:
    resp = _execute(svc.users().labels().list(userId="me"), limiter, LABELS_LIST_UNITS)
    return [{"id": l["id"], "name": l["name"], "type": l.get("type", "")} for l in resp.get("labels", [])]


def list_drafts(svc, max_results: int, limiter: RateLimiter | None = None) -> list[dict]:
    limiter = limiter or RateLimiter(DEFAULT_UNITS_PER_SECOND)
    ids: list[str] = []
    page_token = None
    while len(ids) < max_results:
        request = (
            svc.users()
            .drafts()
            .list(
                userId="me",
                maxResults=min(max_results - len(ids), LIST_PAGE_SIZE),
                pageToken=page_token,
            )
        )
        resp = _execute(request, limiter, DRAFT_LIST_UNITS)
        ids.extend(ref["id"] for ref in resp.get("drafts", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    ids = ids[:max_results]
    found, errors = _run_batches(
        svc,
        ids,
        lambda i: svc.users().drafts().get(userId="me", id=i, format="metadata"),
        limiter,
        DRAFT_GET_UNITS,
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
    limiter: RateLimiter | None = None,
) -> dict:
    limiter = limiter or RateLimiter(DEFAULT_UNITS_PER_SECOND)
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc

    thread_id = None
    if reply_to_message_id:
        request = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=reply_to_message_id,
                format="metadata",
                metadataHeaders=["Subject", "Message-ID", "References"],
            )
        )
        original = _execute(request, limiter, MESSAGE_GET_UNITS)
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
    request = svc.users().drafts().create(userId="me", body={"message": message})
    draft = _execute(request, limiter, DRAFT_CREATE_UNITS, retry=False)
    return {"draft_id": draft["id"], "message_id": draft["message"]["id"], "thread_id": thread_id}


def ensure_label(svc, name: str, limiter: RateLimiter | None = None) -> dict:
    limiter = limiter or RateLimiter(DEFAULT_UNITS_PER_SECOND)
    for label in list_labels(svc, limiter):
        if label["name"].lower() == name.lower():
            return label
    request = svc.users().labels().create(userId="me", body={"name": name})
    created = _execute(request, limiter, LABELS_CREATE_UNITS)
    return {"id": created["id"], "name": created["name"], "type": created.get("type", "user")}


def batch_modify(
    svc, ids: list[str], add: list[str], remove: list[str], limiter: RateLimiter | None = None
) -> None:
    limiter = limiter or RateLimiter(DEFAULT_UNITS_PER_SECOND)
    for start in range(0, len(ids), 1000):
        body = {"ids": ids[start:start + 1000], "addLabelIds": add, "removeLabelIds": remove}
        request = svc.users().messages().batchModify(userId="me", body=body)
        _execute(request, limiter, BATCH_MODIFY_UNITS)


def get_profile(svc, limiter: RateLimiter | None = None) -> dict:
    return _execute(svc.users().getProfile(userId="me"), limiter, PROFILE_UNITS)
