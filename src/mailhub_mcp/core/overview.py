from collections import Counter
from email.utils import parseaddr


def build_overview(summaries: list[dict], top_n: int = 30) -> dict:
    categories: Counter = Counter()
    senders: dict[str, dict] = {}
    unread = newsletters = 0
    for s in summaries:
        categories[s.get("category") or "none"] += 1
        unread += bool(s.get("unread"))
        newsletters += bool(s.get("is_newsletter"))
        name, addr = parseaddr(s.get("from") or "")
        addr = addr.lower()
        entry = senders.setdefault(
            addr, {"email": addr, "name": name, "count": 0, "unread": 0, "newsletters": 0}
        )
        if not entry["name"]:
            entry["name"] = name
        entry["count"] += 1
        entry["unread"] += bool(s.get("unread"))
        entry["newsletters"] += bool(s.get("is_newsletter"))
    top = sorted(senders.values(), key=lambda e: (-e["count"], e["email"]))[:top_n]
    return {
        "total": len(summaries),
        "unread": unread,
        "newsletters": newsletters,
        "categories": dict(categories),
        "top_senders": top,
    }
