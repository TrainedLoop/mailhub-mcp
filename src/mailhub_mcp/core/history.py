"""Read-only views over the change journal."""


def undone_ids(entries: list[dict]) -> set[str]:
    return {e["undo_of"] for e in entries if e.get("undo_of")}


def summarize_entry(entry: dict, undone: bool, label_names: dict[str, str]) -> dict:
    def names(ids: list[str]) -> list[str]:
        return [label_names.get(i, i) for i in ids]

    if entry.get("kind") == "preferences":
        return {
            "change_id": entry["change_id"],
            "timestamp": entry.get("timestamp"),
            "tool": entry.get("tool"),
            "kind": "preferences",
            "scope": entry.get("scope"),
            "fields": list(entry.get("after") or {}),
            "user_request": entry.get("user_request"),
            "undone": undone,
            "undo_of": entry.get("undo_of"),
        }
    return {
        "change_id": entry["change_id"],
        "timestamp": entry.get("timestamp"),
        "account": entry.get("account"),
        "tool": entry.get("tool"),
        "query": entry.get("query"),
        "messages": len(entry.get("prior") or {}),
        "added": names(entry.get("added") or []),
        "removed": names(entry.get("removed") or []),
        "undone": undone,
        "undo_of": entry.get("undo_of"),
    }
