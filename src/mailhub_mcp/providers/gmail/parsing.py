"""Gmail message parsing: pure functions over API message dicts."""

import base64
from html.parser import HTMLParser

MAX_BODY_CHARS = 20_000
METADATA_HEADERS = ["From", "To", "Subject", "Date", "List-Unsubscribe"]
CATEGORIES = {
    "CATEGORY_PERSONAL": "primary",
    "CATEGORY_PROMOTIONS": "promotions",
    "CATEGORY_SOCIAL": "social",
    "CATEGORY_UPDATES": "updates",
    "CATEGORY_FORUMS": "forums",
}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    lines = (line.strip() for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def _walk_parts(payload: dict):
    yield payload
    for part in payload.get("parts", []):
        yield from _walk_parts(part)


def _extract_body(payload: dict) -> tuple[str, list[dict]]:
    plain, html, attachments = [], [], []
    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if part.get("filename"):
            attachments.append(
                {"filename": part["filename"], "mime_type": mime, "size": body.get("size", 0)}
            )
        elif mime == "text/plain" and body.get("data"):
            plain.append(_decode(body["data"]))
        elif mime == "text/html" and body.get("data"):
            html.append(_decode(body["data"]))

    text = "\n".join(plain) if plain else _html_to_text("\n".join(html))
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + "\n[... truncated ...]"
    return text, attachments


def _headers(payload: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def summarize(msg: dict) -> dict:
    h = _headers(msg.get("payload", {}))
    labels = msg.get("labelIds", [])
    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "from": h.get("from", ""),
        "to": h.get("to", ""),
        "subject": h.get("subject", ""),
        "date": h.get("date", ""),
        "snippet": msg.get("snippet", ""),
        "labels": labels,
        "internal_date": int(msg.get("internalDate", 0)),
        "unread": "UNREAD" in labels,
        "starred": "STARRED" in labels,
        "is_important": "IMPORTANT" in labels,
        "category": next((CATEGORIES[l] for l in labels if l in CATEGORIES), None),
        "is_newsletter": "list-unsubscribe" in h,
    }


def full(msg: dict) -> dict:
    payload = msg.get("payload", {})
    h = _headers(payload)
    body, attachments = _extract_body(payload)
    return {
        **summarize(msg),
        "cc": h.get("cc", ""),
        "body": body,
        "attachments": attachments,
    }
