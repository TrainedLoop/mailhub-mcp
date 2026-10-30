"""Guarded, journaled label changes. Nothing here deletes, trashes or sends mail."""

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from mailhub_mcp.core.aggregate import describe_error
from mailhub_mcp.core.errors import OrganizeError
from mailhub_mcp.core.history import summarize_entry, undone_ids
from mailhub_mcp.core.journal import Journal, inverse_ops
from mailhub_mcp.core.preferences import GLOBAL, PreferenceStore, UndoRejectedError, undo_entry

ORG_LABEL = "claude/organized"
MAX_IDS = 1000
MAX_QUERY_APPLY = 500
PREVIEW_SAMPLE = 10
MAX_UNDO_SINCE = 50
MAX_UNDO_SINCE_ATTEMPTS = 200
CAP_WINDOW = timedelta(hours=24)
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


def changes_in_window(journal: Journal, account: str, now: datetime | None = None) -> int:
    """Messages changed on `account` by label entries (undo excluded) in the last 24 hours."""
    now = now or datetime.now(timezone.utc)
    entries = journal.entries(now - CAP_WINDOW)
    return sum(
        len(e.get("prior") or {})
        for e in entries
        if e.get("account") == account and not e.get("undo_of")
    )


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
    max_per_day: int | None = None,
    now: datetime | None = None,
) -> dict:
    ids = _dedupe(ids)
    if not ids:
        raise OrganizeError("No message IDs given")
    if len(ids) > MAX_IDS:
        raise OrganizeError(f"At most {MAX_IDS} message IDs per call")
    if not (add_labels or remove_labels or archive or mark_read is not None or star is not None):
        raise OrganizeError("No change requested")

    labels, add, remove = _plan(provider, add_labels, remove_labels, archive, mark_read, star)

    # The journal lock makes check -> read -> write -> record atomic, so parallel calls cannot overshoot
    # the cap and `prior` cannot go stale between the read and the write.
    with journal.lock:
        found = _read_labels(provider, ids)
        if max_per_day is not None:
            remaining = max(0, max_per_day - changes_in_window(journal, account, now))
            if len(ids) > remaining:
                raise OrganizeError(
                    f"{len(ids)} message(s) requested would exceed the daily change cap: only {remaining} "
                    f"of {max_per_day} changes remain in the last 24 hours for this account; nothing was changed"
                )
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


def apply_query(
    provider: Any, journal: Journal, account: str, query: str, max_per_day: int | None = None, **changes
) -> dict:
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
    result = modify(provider, journal, account, batch, tool="modify_by_query", query=query, max_per_day=max_per_day, **changes)
    return {**result, "remaining": len(ids) > MAX_QUERY_APPLY}


def undo(
    provider: Any, journal: Journal, account: str, change_id: str,
    store: PreferenceStore | None = None, now: datetime | None = None,
) -> dict:
    with journal.lock:  # check-is_undone -> apply -> record must not interleave per journal
        return _undo(provider, journal, account, change_id, store, now)


def _undo_preferences(journal: Journal, entry: dict, store: PreferenceStore | None, now) -> dict:
    if store is None:
        raise OrganizeError("Preferences are not available; cannot undo a preference change")
    return undo_entry(store, journal, entry, now)


def _undo(
    provider: Any, journal: Journal, account: str, change_id: str,
    store: PreferenceStore | None = None, now: datetime | None = None,
) -> dict:
    try:
        entry = journal.get(change_id)
    except KeyError:
        raise OrganizeError(f"Unknown change_id: {change_id}") from None
    is_pref = entry.get("kind") == "preferences"
    if (entry.get("scope") not in (GLOBAL, account)) if is_pref else entry["account"] != account:
        raise OrganizeError(f"Change {change_id} belongs to another account")
    if entry.get("undo_of"):
        # Undoing an undo would be a redo that skips the checks of a fresh change (daily cap, proposals).
        raise OrganizeError("this change is an undo; to redo, make the change again")
    if is_pref:
        return _undo_preferences(journal, entry, store, now)
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


def undo_since(
    provider_for: Callable[[str], Any],
    journal: Journal,
    since: datetime,
    accounts: list[str] | None,
    confirm: bool,
    label_names_for: Callable[[str], dict[str, str]] | None = None,
    store: PreferenceStore | None = None,
    now: datetime | None = None,
) -> dict:
    """Undo every not-yet-undone change since `since`, newest first.

    Newest first makes overlapping changes on one message restore its exact original labels. If a change
    fails, older candidates touching the same messages are blocked (not undone) so a later retry of the
    failed one still restores exactly. Preview shows up to MAX_UNDO_SINCE candidates. Confirm attempts
    candidates until MAX_UNDO_SINCE are undone or MAX_UNDO_SINCE_ATTEMPTS were tried (failures and
    blocked changes count as attempts); `remaining` means candidates were left unattempted. The journal
    lock is taken per change inside `undo`, never across the loop. `label_names_for(account)` optionally
    maps label ids to names for the preview. Preference entries (account = their scope, so `global` ones
    are included only without an account filter) are undone through `store`; their blocking key is
    (scope, field) instead of (account, message id). A preference undo that would lower attention is
    proposed instead (see `undo_entry`): it is reported under `proposed`, is not counted as undone, and
    does not block older candidates (they then usually fail as "changed since", which does block).
    """
    entries = journal.entries(since)
    undone = undone_ids(entries)
    wanted = set(accounts) if accounts else None
    candidates = [
        e for e in entries
        if not e.get("undo_of") and e["change_id"] not in undone
        and (wanted is None or e.get("account") in wanted)
    ]
    if not confirm:
        names: dict[str, dict[str, str]] = {}
        changes = []
        for e in candidates[:MAX_UNDO_SINCE]:
            if label_names_for and e.get("kind") != "preferences" and e["account"] not in names:
                try:
                    names[e["account"]] = label_names_for(e["account"])
                except Exception:
                    names[e["account"]] = {}
            changes.append(summarize_entry(e, False, names.get(e["account"], {})))
        return {"changes": changes, "total": len(candidates), "remaining": len(candidates) > MAX_UNDO_SINCE}

    done: list[dict] = []
    proposed: list[dict] = []
    failed: dict[str, str] = {}
    blocked_by: dict[tuple[str, str], str] = {}  # (account, message id) -> failed newer change
    rejected: set[str] = set()  # failed changes whose undo the owner rejected
    attempts = 0
    for entry in candidates:
        if len(done) >= MAX_UNDO_SINCE or attempts >= MAX_UNDO_SINCE_ATTEMPTS:
            break
        attempts += 1
        change_id, account = entry["change_id"], entry["account"]
        is_pref = entry.get("kind") == "preferences"
        if is_pref:
            pairs = [(entry.get("scope") or account, f) for f in entry.get("after") or {}]
        else:
            pairs = [(account, mid) for mid in entry.get("prior") or {}]
        blocker = next((blocked_by[p] for p in pairs if p in blocked_by), None)
        if blocker is None:
            try:
                if is_pref:
                    with journal.lock:
                        res = _undo_preferences(journal, entry, store, now)
                else:
                    res = undo(provider_for(account), journal, account, change_id)
            except Exception as exc:
                failed[change_id] = f"{account}: {describe_error(exc, account)}"
                log.warning("undo_since: change %s not undone: %s", change_id, failed[change_id])
                blocker = change_id
                if isinstance(exc, UndoRejectedError):
                    rejected.add(change_id)
            else:
                if res.get("proposed"):
                    proposed.append({"change_id": change_id, "proposed": res["proposed"]})
                if res["change_id"] is not None:
                    done.append({
                        "change_id": change_id, "undo_change_id": res["change_id"],
                        "modified": res["modified"], "skipped": res["skipped"],
                    })
                continue
        else:
            if blocker in rejected:
                failed[change_id] = f"{account}: blocked by newer change {blocker} whose undo was rejected by the owner"
            else:
                failed[change_id] = f"{account}: blocked by failed newer change {blocker}; retry after fixing it"
        for p in pairs:
            blocked_by.setdefault(p, blocker)
    return {"undone": done, "proposed": proposed, "failed": failed, "remaining": attempts < len(candidates)}
