"""In-memory stand-in for a googleapiclient Gmail client.

Mimics the call chain (svc.users().messages().get(...).execute()) closely enough
for the code under test. Every API call is recorded in `calls` as
(method_name, kwargs) when the request is built.
"""

import base64
import copy
import email
import itertools

import httplib2
from googleapiclient.errors import HttpError

SYSTEM_LABELS = [
    "INBOX", "UNREAD", "STARRED", "IMPORTANT", "TRASH", "SPAM", "DRAFT", "SENT", "CHAT",
    "CATEGORY_PERSONAL", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS",
]


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def http_error(status: int, reason: str) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), reason.encode())


class _Request:
    def __init__(self, fake, fn):
        self._fake = fake
        self._fn = fn

    def execute(self, num_retries=0):
        self._fake.execute_retries.append(num_retries)
        if self._fake.fail:
            raise self._fake.fail
        return self._fn()


class _Batch:
    def __init__(self, fake, callback):
        self._fake = fake
        self._callback = callback
        self._requests: list[tuple[str, _Request]] = []

    def add(self, request, request_id=None):
        if len(self._requests) >= self._fake.batch_limit:
            raise ValueError(f"batch holds at most {self._fake.batch_limit} requests")
        self._requests.append((request_id, request))

    def execute(self):
        self._fake.calls.append(("batch.execute", {"size": len(self._requests)}))
        if self._fake.fail:
            raise self._fake.fail
        if self._fake.batch_errors:  # the whole batch request fails, before any callback
            raise self._fake.batch_errors.pop(0)
        for request_id, request in self._requests:
            try:
                response = request._fn()
            except Exception as exc:
                self._callback(request_id, None, exc)
            else:
                self._callback(request_id, response, None)


class FakeGmail:
    def __init__(self, email_address: str = "me@example.com"):
        self.email_address = email_address
        self.messages: dict[str, dict] = {}
        self.labels: list[dict] = [
            {"id": name, "name": name, "type": "system"} for name in SYSTEM_LABELS
        ]
        self.drafts: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.fail: Exception | None = None
        self.get_errors: dict[str, Exception] = {}
        self.execute_retries: list[int] = []
        self.get_error_seq: dict[str, list[Exception]] = {}
        self.draft_errors: dict[str, Exception] = {}
        self.batch_limit = 100
        self.batch_errors: list[Exception] = []
        self.label_create_errors: list[Exception] = []
        self.query_results: dict[str, list[str]] = {}
        self.page_size = 500
        self._ids = itertools.count(1)

    # -- test helpers ------------------------------------------------------

    def add_message(
        self, id, thread_id=None, labels=(), headers=None, body="", html=None,
        internal_date=0, snippet="",
    ):
        parts = []
        if body or html is None:
            parts.append({"mimeType": "text/plain", "body": {"data": _b64(body)}})
        if html is not None:
            parts.append({"mimeType": "text/html", "body": {"data": _b64(html)}})
        payload = {"headers": [{"name": k, "value": v} for k, v in (headers or {}).items()]}
        if len(parts) == 1:
            payload.update(parts[0])
        else:
            payload.update({"mimeType": "multipart/alternative", "parts": parts})
        self.messages[id] = {
            "id": id,
            "threadId": thread_id or id,
            "labelIds": list(labels),
            "snippet": snippet,
            "internalDate": str(internal_date),
            "payload": payload,
        }
        return self.messages[id]

    # -- plumbing ----------------------------------------------------------

    def _request(self, name, kwargs, fn):
        self.calls.append((name, kwargs))
        return _Request(self, fn)

    def _shape(self, msg: dict, fmt: str, metadata_headers=None) -> dict:
        out = copy.deepcopy(msg)
        if fmt == "minimal":
            out.pop("payload", None)
        elif fmt == "metadata":
            wanted = {h.lower() for h in (metadata_headers or [])}
            payload = out.get("payload", {})
            out["payload"] = {
                "headers": [h for h in payload.get("headers", []) if h["name"].lower() in wanted]
            }
        return out

    def _get_message(self, id, fmt, metadata_headers):
        if self.get_error_seq.get(id):
            raise self.get_error_seq[id].pop(0)
        if id in self.get_errors:
            raise self.get_errors[id]
        if id not in self.messages:
            raise http_error(404, "Not Found")
        return self._shape(self.messages[id], fmt, metadata_headers)

    def _page(self, items: list, max_results, page_token):
        size = min(max_results or self.page_size, self.page_size)
        start = int(page_token or 0)
        page = items[start:start + size]
        token = str(start + size) if start + size < len(items) else None
        return page, token

    # -- service surface ---------------------------------------------------

    def new_batch_http_request(self, callback=None):
        return _Batch(self, callback)

    def getProfile(self, userId="me"):
        def run():
            return {
                "emailAddress": self.email_address,
                "messagesTotal": len(self.messages),
                "threadsTotal": len({m["threadId"] for m in self.messages.values()}),
            }
        return self._request("users.getProfile", {"userId": userId}, run)

    def users(self):
        return _Users(self)


# users() returns this object because the fake's own `messages`, `labels` and
# `drafts` attributes hold state and cannot double as resource accessors.
class _Users:
    def __init__(self, fake):
        self._fake = fake

    def messages(self):
        return _Messages(self._fake)

    def labels(self):
        return _Labels(self._fake)

    def threads(self):
        return _Threads(self._fake)

    def drafts(self):
        return _Drafts(self._fake)

    def getProfile(self, userId="me"):
        return self._fake.getProfile(userId)


class _Messages:
    def __init__(self, fake):
        self.f = fake

    def get(self, userId="me", id=None, format="full", metadataHeaders=None):
        kwargs = {"userId": userId, "id": id, "format": format}
        if metadataHeaders is not None:
            kwargs["metadataHeaders"] = metadataHeaders
        return self.f._request("messages.get", kwargs, lambda: self.f._get_message(id, format, metadataHeaders))

    def list(self, userId="me", q=None, maxResults=None, pageToken=None, labelIds=None):
        kwargs = {"userId": userId, "q": q, "maxResults": maxResults, "pageToken": pageToken}
        if labelIds is not None:
            kwargs["labelIds"] = labelIds

        def run():
            if q in self.f.query_results:
                ids = self.f.query_results[q]
            else:
                ids = sorted(
                    self.f.messages, key=lambda i: int(self.f.messages[i]["internalDate"]), reverse=True
                )
            if labelIds:  # Gmail returns messages that carry every one of labelIds
                ids = [i for i in ids if set(labelIds) <= set(self.f.messages[i]["labelIds"])]
            page, token = self.f._page(ids, maxResults, pageToken)
            resp = {
                "messages": [{"id": i, "threadId": self.f.messages[i]["threadId"]} for i in page],
                "resultSizeEstimate": len(ids),
            }
            if token:
                resp["nextPageToken"] = token
            return resp

        return self.f._request("messages.list", kwargs, run)

    def batchModify(self, userId="me", body=None):
        def run():
            ids = body.get("ids", [])
            if len(ids) > 1000:
                raise ValueError("batchModify accepts at most 1000 ids")
            add = body.get("addLabelIds", [])
            remove = set(body.get("removeLabelIds", []))
            for i in ids:
                msg = self.f.messages.get(i)
                if msg is None:
                    continue
                current = [l for l in msg["labelIds"] if l not in remove]
                msg["labelIds"] = current + [l for l in add if l not in current]
            return ""

        return self.f._request("messages.batchModify", {"userId": userId, "body": body}, run)


class _Labels:
    def __init__(self, fake):
        self.f = fake

    def list(self, userId="me"):
        return self.f._request(
            "labels.list", {"userId": userId}, lambda: {"labels": copy.deepcopy(self.f.labels)}
        )

    def create(self, userId="me", body=None):
        def run():
            if self.f.label_create_errors:
                raise self.f.label_create_errors.pop(0)
            label = {"id": f"Label_{next(self.f._ids)}", "name": body["name"], "type": "user"}
            self.f.labels.append(label)
            return dict(label)

        return self.f._request("labels.create", {"userId": userId, "body": body}, run)


class _Threads:
    def __init__(self, fake):
        self.f = fake

    def get(self, userId="me", id=None, format="full", metadataHeaders=None):
        kwargs = {"userId": userId, "id": id, "format": format}
        if metadataHeaders is not None:
            kwargs["metadataHeaders"] = metadataHeaders

        def run():
            msgs = [m for m in self.f.messages.values() if m["threadId"] == id]
            if not msgs:
                raise http_error(404, "Not Found")
            return {
                "id": id,
                "messages": [self.f._shape(m, format, metadataHeaders) for m in msgs],
            }

        return self.f._request("threads.get", kwargs, run)


class _Drafts:
    def __init__(self, fake):
        self.f = fake

    def list(self, userId="me", maxResults=None, pageToken=None):
        def run():
            ids = list(self.f.drafts)
            page, token = self.f._page(ids, maxResults, pageToken)
            resp = {"drafts": [{"id": i, "message": {"id": self.f.drafts[i]["message"]["id"]}} for i in page]}
            if token:
                resp["nextPageToken"] = token
            return resp

        return self.f._request(
            "drafts.list", {"userId": userId, "maxResults": maxResults, "pageToken": pageToken}, run
        )

    def get(self, userId="me", id=None, format="full"):
        def run():
            if id in self.f.draft_errors:
                raise self.f.draft_errors[id]
            if id not in self.f.drafts:
                raise http_error(404, "Not Found")
            draft = copy.deepcopy(self.f.drafts[id])
            if format != "full":
                draft["message"].pop("raw", None)
            return draft

        return self.f._request("drafts.get", {"userId": userId, "id": id, "format": format}, run)

    def create(self, userId="me", body=None):
        def run():
            message = dict(body["message"])
            parsed = email.message_from_bytes(base64.urlsafe_b64decode(message["raw"]))
            draft_id = f"r{next(self.f._ids)}"
            message_id = f"dm{next(self.f._ids)}"
            self.f.drafts[draft_id] = {
                "id": draft_id,
                "message": {
                    "id": message_id,
                    "threadId": message.get("threadId", message_id),
                    "labelIds": ["DRAFT"],
                    "raw": message["raw"],
                    "snippet": "",
                    "internalDate": "0",
                    "payload": {"headers": [{"name": k, "value": v} for k, v in parsed.items()]},
                },
            }
            return copy.deepcopy(self.f.drafts[draft_id])

        return self.f._request("drafts.create", {"userId": userId, "body": body}, run)
