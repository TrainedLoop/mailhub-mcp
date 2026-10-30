"""Guarded, journaled label changes. Nothing here deletes, trashes or sends mail."""

import json
import logging
from types import SimpleNamespace
from typing import Any

from mailhub_mcp.core.errors import OrganizeError
from mailhub_mcp.core.journal import Journal, inverse_ops

ORG_LABEL = "claude/organized"
MAX_IDS = 1000
MAX_QUERY_APPLY = 500
PREVIEW_SAMPLE = 10
QUERY_HINT_ALREADY_CHANGED = "query still matches messages already changed; refine the query"

log = logging.getLogger("mailhub-mcp.organize")


def _resolve(labels: list, protected: frozenset[str], value: str) -> str:
    if value.upper() in protected:
        raise OrganizeError(f"Label {value!r} cannot be changed")
    match = next((l for l in labels if l.id == value), None)
    if match is None:
        match = next((l for l in labels if l.name.lower() == value.lower()), None)
    if match is None:
        raise OrganizeError(f"Unknown label: {value!r}")
    if match.id in protected or match.name.upper() in protected:
        raise OrganizeError(f"Label {value!r} cannot be changed")
    return match.id


def _record(
    journal: Journal, account: str, tool: str, added, removed, prior, changed: bool = True, **extra
) -> str:
    try:
        return journal.record(account, tool, added, removed, prior, **extra)
    except Exception:
        what = "after labels were changed" if changed else "after undo with no label changes"
        log.error(
            "journal write failed %s; manual recovery entry: %s",
            what,
            json.dumps(
                {"account": account, "tool": tool, "added": added, "removed": removed,
                 "prior": prior, **extra},
                default=str,
            ),
        )
        raise


def _read_labels(provider: Any, ids: list[str]) -> dict[str, list[str]]:
    """Current labels for every id, or OrganizeError if any id is missing, failed or unaccounted for."""
    res = provider.get_messages(ids, "labels")
    if res.missing:
        raise OrganizeError(f"Message IDs not found in this account: {res.missing}")
    current = {m.id: m.labels for m in res.found}
    unreadable = set(res.failed) | (set(ids) - set(current) - set(res.missing))
    if unreadable:
        raise OrganizeError(f"Could not read message labels (retry if the failure is temporary): {sorted(unreadable)}")
    return current


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _plan(provider, add_labels, remove_labels, archive, mark_read, star):
    labels = provider.list_labels()
    protected = provider.protected_labels
    add = [_resolve(labels, protected, v) for v in add_labels]
    remove = [_resolve(labels, protected, v) for v in remove_labels]
    if archive:
        remove.append(provider.inbox_label)
    if mark_read is True:
        remove.append(provider.unread_label)
    elif mark_read is False:
        add.append(provider.unread_label)
    if star is True:
        add.append(provider.starred_label)
    elif star is False:
        remove.append(provider.starred_label)
    add, remove = _dedupe(add), _dedupe(remove)

    org = next((l for l in labels if l.name.lower() == ORG_LABEL.lower()), None)
    if org and org.id in remove:
        raise OrganizeError(f"Label {ORG_LABEL!r} cannot be removed")
    both = set(add) & set(remove)
    if both:
        raise OrganizeError(f"Label(s) both added and removed: {sorted(both)}")
    return labels, add, remove


def modify(
    provider: Any,
    journal: Journal,
    account: str,
    ids: list[str],
    add_labels: list[str],
    remove_labels: list[str],
    archive: bool,
    mark_read: bool | None,
    star: bool | None,
    tool: str = "modify_emails",
    query: str | None = None,
) -> dict:
    ids = _dedupe(ids)
    if not ids:
        raise OrganizeError("No message IDs given")
    if len(ids) > MAX_IDS:
        raise OrganizeError(f"At most {MAX_IDS} message IDs per call")
    if not (add_labels or remove_labels or archive or mark_read is not None or star is not None):
        raise OrganizeError("No change requested")

    labels, add, remove = _plan(provider, add_labels, remove_labels, archive, mark_read, star)
    found = _read_labels(provider, ids)

    # Validation done; first write.
    add = _dedupe(add + [provider.ensure_label(ORG_LABEL).id])
    touched = set(add) | set(remove)
    prior = {i: [l for l in found[i] if l in touched] for i in ids}
    provider.modify_labels(ids, add, remove)
    change_id = _record(journal, account, tool, add, remove, prior, query=query)
    return {"change_id": change_id, "modified": len(ids)}


def preview_query(provider: Any, query: str) -> dict:
    ids = provider.list_ids(query, MAX_QUERY_APPLY + 1)
    sample = provider.get_messages(ids[:PREVIEW_SAMPLE], "summary").found if ids else []
    return {
        "matched": len(ids),
        "capped": len(ids) > MAX_QUERY_APPLY,
        "sample": sample,
    }


def apply_query(provider: Any, journal: Journal, account: str, query: str, **changes) -> dict:
    ids = provider.list_ids(query, MAX_QUERY_APPLY + 1)
    if not ids:
        return {"change_id": None, "modified": 0, "remaining": False}
    batch = ids[:MAX_QUERY_APPLY]
    if any(changes.get(k) for k in ("add_labels", "remove_labels", "archive")) or any(
        changes.get(k) is not None for k in ("mark_read", "star")
    ):
        _, add, remove = _plan(provider, **changes)
        current = _read_labels(provider, batch)
        batch = [
            i for i in batch
            if not (set(add) <= set(current[i]) and not set(remove) & set(current[i]))
        ]
        if not batch:
            return {
                "change_id": None, "modified": 0, "remaining": len(ids) > MAX_QUERY_APPLY,
                "hint": QUERY_HINT_ALREADY_CHANGED,
            }
    result = modify(provider, journal, account, batch, tool="modify_by_query", query=query, **changes)
    return {**result, "remaining": len(ids) > MAX_QUERY_APPLY}


def undo(provider: Any, journal: Journal, account: str, change_id: str) -> dict:
    with journal.lock:  # check-is_undone -> apply -> record must not interleave per journal
        return _undo(provider, journal, account, change_id)


def _undo(provider: Any, journal: Journal, account: str, change_id: str) -> dict:
    try:
        entry = journal.get(change_id)
    except KeyError:
        raise OrganizeError(f"Unknown change_id: {change_id}") from None
    if entry["account"] != account:
        raise OrganizeError(f"Change {change_id} belongs to another account")
    if journal.is_undone(change_id):
        raise OrganizeError(f"Change {change_id} was already undone")

    ops = inverse_ops(entry)
    all_ids = _dedupe([i for op in ops for i in op["ids"]])
    res = provider.get_messages(all_ids, "labels") if all_ids else SimpleNamespace(found=[], missing=[], failed={})
    current = {m.id: m.labels for m in res.found}
    unreadable = set(res.failed) | (set(all_ids) - set(current) - set(res.missing))
    if unreadable:
        raise OrganizeError(f"Could not read message(s) {sorted(unreadable)}; nothing was undone, retry")
    skipped = [i for i in all_ids if i in set(res.missing)]
    touched = {l for op in ops for l in op["add"] + op["remove"]}
    for op in ops:
        op["ids"] = [i for i in op["ids"] if i in current]
        if op["ids"]:
            provider.modify_labels(op["ids"], op["add"], op["remove"])
    applied = [op for op in ops if op["ids"]]
    done = _dedupe([i for op in applied for i in op["ids"]])
    prior = {i: [l for l in current[i] if l in touched] for i in done}
    undo_id = _record(
        journal, account, "undo_changes",
        _dedupe([l for op in applied for l in op["add"]]),
        _dedupe([l for op in applied for l in op["remove"]]),
        prior, changed=bool(applied), undo_of=change_id,
    )
    return {"change_id": undo_id, "undone": change_id, "modified": len(done), "skipped": skipped}
