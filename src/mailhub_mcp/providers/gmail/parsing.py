"""Gmail message parsing: pure functions over API message dicts."""

import base64

from mailhub_mcp.core.content import clean_text, html_to_safe_text

MAX_BODY_CHARS = 20_000
METADATA_HEADERS = ["From", "To", "Subject", "Date", "List-Unsubscribe", "Reply-To"]
CATEGORIES = {
    "CATEGORY_PERSONAL": "primary",
    "CATEGORY_PROMOTIONS": "promotions",
    "CATEGORY_SOCIAL": "social",
    "CATEGORY_UPDATES": "updates",
    "CATEGORY_FORUMS": "forums",
}


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def _walk_parts(payload: dict):
    yield payload
    for part in payload.get("parts", []):
        yield from _walk_parts(part)


def _extract_body(payload: dict) -> tuple[str, list[dict], int]:
    plain, html, attachments = [], [], []
    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if part.get("filename"):
            attachments.append(
                {"filename": clean_text(part["filename"]), "mime_type": mime, "size": body.get("size", 0)}
            )
        elif mime == "text/plain" and body.get("data"):
            plain.append(_decode(body["data"]))
        elif mime == "text/html" and body.get("data"):
            html.append(_decode(body["data"]))

    hidden_removed = 0
    if plain:
        text = clean_text("\n".join(plain))
    else:
        text, hidden_removed = html_to_safe_text("\n".join(html))
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + "\n[... truncated ...]"
    return text, attachments, hidden_removed


def _headers(payload: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def summarize(msg: dict) -> dict:
    h = _headers(msg.get("payload", {}))
    labels = msg.get("labelIds", [])
    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "from": clean_text(h.get("from", "")),
        "to": clean_text(h.get("to", "")),
        "reply_to": clean_text(h.get("reply-to", "")),
        "subject": clean_text(h.get("subject", "")),
        "date": h.get("date", ""),
        "snippet": clean_text(msg.get("snippet", "")),
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
    body, attachments, hidden_removed = _extract_body(payload)
    return {
        **summarize(msg),
        "cc": clean_text(h.get("cc", "")),
        "body": body,
        "hidden_removed": hidden_removed,
        "attachments": attachments,
    }
