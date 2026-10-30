"""Triage preferences: a validated, file-locked store with a direction rule and out-of-band approval.

Anything that raises attention (or removes a reduction of attention) applies immediately. Anything that
lowers attention becomes a proposal that only `approve` (reachable from the admin CLI, never from an MCP
tool) can apply. Preferences are plain data; there is no free-text field except the journaled
`user_request`.
"""

import contextlib
import fcntl
import json
import logging
import os
import re
import secrets
import tempfile
import unicodedata
from collections.abc import Collection, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mailhub_mcp.core.errors import MailhubError, OrganizeError
from mailhub_mcp.core.history import undone_ids
from mailhub_mcp.core.journal import Journal

log = logging.getLogger("mailhub-mcp.preferences")

GLOBAL = "global"
SENDER_FIELDS = ("important_senders", "ignored_senders")
KEYWORD_FIELDS = ("important_keywords", "ignored_keywords")
LIST_FIELDS = SENDER_FIELDS + KEYWORD_FIELDS
FIELDS = LIST_FIELDS + ("last_triage_at",)
RAISING = {
    ("important_senders", "add"),
    ("important_keywords", "add"),
    ("ignored_senders", "remove"),
    ("ignored_keywords", "remove"),
    ("last_triage_at", "set"),
}
OPPOSITE = {
    "important_senders": "ignored_senders", "ignored_senders": "important_senders",
    "important_keywords": "ignored_keywords", "ignored_keywords": "important_keywords",
}
MAX_SENDER_LEN = 254
MAX_KEYWORD_LEN = 40
MAX_ITEMS = 200
MAX_USER_REQUEST = 500
PROPOSAL_TTL = timedelta(days=14)
CLOCK_SKEW = timedelta(minutes=5)
RECENT_DEFAULT = 20

_SENDER_RE = re.compile(r"[^@\s<>,;:\"'()\[\]\\]*@[a-z0-9]([a-z0-9.-]*[a-z0-9])?")
_BAD_CATEGORIES = {"Cc", "Cf", "Zl", "Zp"}


class PreferenceError(MailhubError, ValueError):
    pass


def is_raising(field: str, op: str) -> bool:
    return (field, op) in RAISING


def _has_bad_chars(value: str) -> bool:
    return any(unicodedata.category(c) in _BAD_CATEGORIES for c in value)


def normalize_sender(value: Any) -> str:
    if not isinstance(value, str):
        raise PreferenceError(f"Invalid sender pattern {value!r}: must be a string")
    v = value.strip().lower()
    if (
        not v or len(v) > MAX_SENDER_LEN or _has_bad_chars(v) or not _SENDER_RE.fullmatch(v)
        or "." not in v.split("@", 1)[1]
    ):
        raise PreferenceError(
            f"Invalid sender pattern {value!r}: use name@domain or @domain (lowercase, at most "
            f"{MAX_SENDER_LEN} characters)"
        )
    return v


def normalize_keyword(value: Any) -> str:
    if not isinstance(value, str):
        raise PreferenceError(f"Invalid keyword {value!r}: must be a string")
    v = value.strip()
    if not v or len(v) > MAX_KEYWORD_LEN or _has_bad_chars(v):
        raise PreferenceError(
            f"Invalid keyword {value!r}: 1-{MAX_KEYWORD_LEN} characters, no control characters or newlines"
        )
    return v


def _normalize(field: str, value: Any) -> str:
    return normalize_sender(value) if field in SENDER_FIELDS else normalize_keyword(value)


def _key(field: str, value: str) -> str:
    return value if field in SENDER_FIELDS else value.casefold()


def _empty_rules() -> dict:
    rules: dict[str, Any] = {f: [] for f in LIST_FIELDS}
    rules["last_triage_at"] = None
    return rules


def _default_state() -> dict:
    return {"global": _empty_rules(), "accounts": {}, "proposals": []}


def _clean_rules(raw: Any, where: str) -> dict:
    rules = _empty_rules()
    if raw is None:
        return rules
    if not isinstance(raw, dict):
        raise PreferenceError(f"Preferences file is corrupt: {where} is not an object")
    for f in LIST_FIELDS:
        v = raw.get(f, [])
        if not isinstance(v, list) or not all(isinstance(i, str) for i in v):
            raise PreferenceError(f"Preferences file is corrupt: {where}.{f} is not a list of strings")
        rules[f] = list(v)
    t = raw.get("last_triage_at")
    if t is not None and not isinstance(t, str):
        raise PreferenceError(f"Preferences file is corrupt: {where}.last_triage_at is not a string")
    rules["last_triage_at"] = t
    return rules


class PreferenceStore:
    """JSON file guarded by an exclusive flock on `<path>.lock` (same stem) and replaced atomically."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(".lock")

    def load(self) -> dict:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _default_state()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PreferenceError(f"Preferences file {self.path} is not valid JSON: {exc}") from None
        if not isinstance(raw, dict):
            raise PreferenceError(f"Preferences file {self.path} is corrupt: not an object")
        accounts = raw.get("accounts") or {}
        proposals = raw.get("proposals") or []
        if not isinstance(accounts, dict) or not isinstance(proposals, list):
            raise PreferenceError(f"Preferences file {self.path} is corrupt")
        return {
            "global": _clean_rules(raw.get("global"), "global"),
            "accounts": {a: _clean_rules(r, f"accounts.{a}") for a, r in accounts.items()},
            "proposals": [p for p in proposals if isinstance(p, dict)],
        }

    @contextlib.contextmanager
    def transaction(self) -> Iterator[dict]:
        """Exclusive lock, yield a mutable state, save atomically only if the block succeeds."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = self.load()
                yield state
                self._save(state)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _save(self, state: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        with contextlib.suppress(OSError):
            dfd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _rules(state: dict, scope: str) -> dict:
    if scope == GLOBAL:
        return state["global"]
    return state["accounts"].setdefault(scope, _empty_rules())


def _peek(state: dict, scope: str) -> dict:
    return state["global"] if scope == GLOBAL else state["accounts"].get(scope) or _empty_rules()


def _expired(proposal: dict, now: datetime) -> bool:
    try:
        return _aware(datetime.fromisoformat(proposal["expires"])) <= now
    except (KeyError, TypeError, ValueError):
        return True


def _live(state: dict, now: datetime) -> list[dict]:
    return [p for p in state["proposals"] if not _expired(p, now)]


def _prune(state: dict, now: datetime) -> None:
    state["proposals"] = _live(state, now)


def _check_rules(rules: dict, scope: str, touched: Collection[str]) -> None:
    """Item cap per list and no value both important and ignored in the same scope."""
    for f in touched:
        if len(rules[f]) > MAX_ITEMS:
            raise PreferenceError(f"{f} in scope {scope!r} would hold {len(rules[f])} items; the limit is {MAX_ITEMS}")
    for a, b in (("important_senders", "ignored_senders"), ("important_keywords", "ignored_keywords")):
        if a in touched or b in touched:
            clash = {_key(a, v) for v in rules[a]} & {_key(b, v) for v in rules[b]}
            if clash:
                raise PreferenceError(
                    f"Conflict in scope {scope!r}: {sorted(clash)} cannot be both in {a} and {b}"
                )


def _apply_op(rules: dict, field: str, op: str, values: list[str]) -> list[str]:
    """Apply to `rules` in place; return the values that actually changed."""
    keys = {_key(field, v) for v in values}
    if op == "add":
        have = {_key(field, v) for v in rules[field]}
        new = [v for v in values if _key(field, v) not in have]
        rules[field] = rules[field] + new
        return new
    gone = [v for v in rules[field] if _key(field, v) in keys]
    rules[field] = [v for v in rules[field] if _key(field, v) not in keys]
    return gone


def _parse_ops(spec: Any, op: str) -> list[tuple[str, str, list[str]]]:
    if spec is None:
        return []
    if not isinstance(spec, dict):
        raise PreferenceError(f"{op} must be an object mapping field to a list of values")
    out = []
    for field, values in spec.items():
        if field == "last_triage_at":
            raise PreferenceError("last_triage_at is not a list field; use the last_triage_at argument")
        if field not in LIST_FIELDS:
            raise PreferenceError(f"Unknown field {field!r}; valid fields: {', '.join(LIST_FIELDS)}")
        if not isinstance(values, list):
            raise PreferenceError(f"{op}[{field!r}] must be a list of values")
        seen: dict[str, str] = {}
        for v in values:
            n = _normalize(field, v)
            seen.setdefault(_key(field, n), n)
        if seen:
            out.append((op, field, list(seen.values())))
    return out


def _validate_user_request(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PreferenceError("user_request is required: the user's words that asked for this change")
    v = value.strip()
    if len(v) > MAX_USER_REQUEST:
        raise PreferenceError(f"user_request is too long ({len(v)} characters; the limit is {MAX_USER_REQUEST})")
    return v


def _parse_triage_time(value: Any, now: datetime) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        moment = _aware(datetime.fromisoformat(value.strip())).astimezone(timezone.utc)
    except ValueError:
        raise PreferenceError(f"Invalid last_triage_at {value!r}: expected an ISO 8601 timestamp") from None
    if moment > now + CLOCK_SKEW:
        raise PreferenceError(f"Invalid last_triage_at {value!r}: it is in the future")
    return moment.isoformat()


def _snapshot(rules: dict, fields: Collection[str]) -> dict:
    return {f: list(rules[f]) if f in LIST_FIELDS else rules[f] for f in fields}


def _record(
    journal: Journal, scope: str, tool: str, before: dict, after: dict, user_request: str,
    now: datetime, undo_of: str | None = None,
) -> str:
    try:
        return journal.record(
            scope, tool, [], [], {}, undo_of=undo_of, kind="preferences", scope=scope,
            before=before, after=after, user_request=user_request, timestamp=now,
        )
    except Exception:
        log.error(
            "journal write failed after preferences were changed; manual recovery entry: %s",
            json.dumps({"scope": scope, "tool": tool, "before": before, "after": after,
                        "user_request": user_request}, default=str),
        )
        raise


def _proposal_view(p: dict) -> dict:
    return {
        "proposal_id": p["id"], "scope": p["scope"], "field": p["field"], "op": p["op"],
        "values": list(p["values"]), "user_request": p.get("user_request"),
        "created": p["created"], "expires": p["expires"],
    }


def set_preferences(
    store: PreferenceStore,
    journal: Journal,
    scope: str,
    add: dict[str, list[str]] | None = None,
    remove: dict[str, list[str]] | None = None,
    last_triage_at: str | None = None,
    user_request: str | None = None,
    valid_scopes: Collection[str] = (),
    now: datetime | None = None,
) -> dict:
    now = _now(now)
    request = _validate_user_request(user_request)
    if scope != GLOBAL and scope not in valid_scopes:
        raise PreferenceError(
            f"Unknown scope {scope!r}; valid scopes: {', '.join([GLOBAL, *sorted(valid_scopes)])}"
        )
    ops = _parse_ops(add, "add") + _parse_ops(remove, "remove")
    triage = None if last_triage_at is None else _parse_triage_time(last_triage_at, now)
    if not ops and triage is None:
        raise PreferenceError("No change requested")
    for _, field, values in [o for o in ops if o[0] == "add"]:
        removing = {_key(field, v) for o, f, vs in ops if o == "remove" and f == field for v in vs}
        clash = [v for v in values if _key(field, v) in removing]
        if clash:
            raise PreferenceError(f"Conflict: {clash} both added to and removed from {field}")

    with journal.lock:
        with store.transaction() as state:
            _prune(state, now)
            rules = _rules(state, scope)
            # Validate the whole request (every op applied) before changing anything.
            trial = _snapshot(rules, FIELDS)
            for op, field, values in sorted(ops, key=lambda o: o[0] != "remove"):
                _apply_op(trial, field, op, values)
            _check_rules(trial, scope, {f for _, f, _ in ops})

            before: dict[str, Any] = {}
            after: dict[str, Any] = {}
            applied: list[dict] = []
            pending: list[dict] = []
            for op, field, values in ops:
                if is_raising(field, op):
                    before.setdefault(field, list(rules[field]))
                    changed = _apply_op(rules, field, op, values)
                    after[field] = list(rules[field])
                    if changed:
                        applied.append({"scope": scope, "field": field, "op": op, "values": changed})
                else:
                    current = {_key(field, v) for v in rules[field]}
                    if op == "add":
                        todo = [v for v in values if _key(field, v) not in current]
                    else:
                        todo = [next(c for c in rules[field] if _key(field, c) == _key(field, v))
                                for v in values if _key(field, v) in current]
                    if not todo:
                        continue
                    one = _snapshot(rules, FIELDS)
                    _apply_op(one, field, op, todo)
                    _check_rules(one, scope, {field})
                    proposal = {
                        "id": "p-" + secrets.token_hex(4), "scope": scope, "field": field, "op": op,
                        "values": todo, "user_request": request, "created": now.isoformat(),
                        "expires": (now + PROPOSAL_TTL).isoformat(),
                    }
                    state["proposals"].append(proposal)
                    pending.append({"proposal_id": proposal["id"], "scope": scope, "field": field,
                                    "op": op, "values": todo})
            _check_rules(rules, scope, {f for f in after if f in LIST_FIELDS})
            if triage is not None:
                before["last_triage_at"] = rules["last_triage_at"]
                if rules["last_triage_at"] != triage:
                    applied.append({"scope": scope, "field": "last_triage_at", "op": "set", "values": [triage]})
                rules["last_triage_at"] = triage
                after["last_triage_at"] = triage
            # Fields whose value did not really change are not journaled.
            touched = [f for f in after if after[f] != before[f]]
            before = {f: before[f] for f in touched}
            after = {f: after[f] for f in touched}
        change_id = _record(journal, scope, "set_preferences", before, after, request, now) if touched else None
    return {"change_id": change_id, "applied": applied, "pending": pending}


def _effective(state: dict, account: str) -> dict:
    g, a = state["global"], _peek(state, account)
    eff = {f: list(dict.fromkeys(g[f] + a[f])) for f in LIST_FIELDS}
    eff["last_triage_at"] = a["last_triage_at"] or g["last_triage_at"]
    return eff


def _entry_status(entry: dict) -> str:
    return "approved" if entry.get("tool") == "approve_preference" else "applied"


def view(
    store: PreferenceStore, journal: Journal, account: str | None = None, now: datetime | None = None
) -> dict:
    now = _now(now)
    state = store.load()
    scopes = {GLOBAL} if account is None else {GLOBAL, account}
    pending = [_proposal_view(p) for p in _live(state, now) if account is None or p["scope"] in scopes]
    out: dict[str, Any] = {"global": state["global"]}
    if account is None:
        out["accounts"] = state["accounts"]
        floor_src = state["global"]["last_triage_at"]
    else:
        out["account"] = _peek(state, account)
        out["effective"] = _effective(state, account)
        floor_src = out["effective"]["last_triage_at"]
    entries = journal.entries()
    undone = undone_ids(entries)
    prefs = [
        e for e in entries
        if e.get("kind") == "preferences" and not e.get("undo_of")
        and (account is None or e.get("scope") in scopes)
    ]
    floor = None
    if floor_src:
        with contextlib.suppress(ValueError):
            floor = _aware(datetime.fromisoformat(floor_src))
    if floor is not None:
        recent = []
        for e in prefs:
            try:
                if _aware(datetime.fromisoformat(e["timestamp"])) >= floor:
                    recent.append(e)
            except (KeyError, TypeError, ValueError):
                continue
    else:
        recent = prefs[:RECENT_DEFAULT]
    out["pending"] = pending
    out["recent_changes"] = [
        {
            "change_id": e["change_id"], "timestamp": e.get("timestamp"), "status": _entry_status(e),
            "scope": e.get("scope"), "fields": list(e.get("after") or {}),
            "before": e.get("before"), "after": e.get("after"),
            "user_request": e.get("user_request"), "undone": e["change_id"] in undone,
        }
        for e in recent
    ]
    return out


def list_proposals(store: PreferenceStore, now: datetime | None = None) -> list[dict]:
    return [_proposal_view(p) for p in _live(store.load(), _now(now))]


def _find(state: dict, proposal_id: str, now: datetime) -> dict:
    for p in state["proposals"]:
        if p.get("id") == proposal_id:
            if _expired(p, now):
                raise PreferenceError(f"Proposal {proposal_id} has expired")
            return p
    raise PreferenceError(f"Unknown proposal: {proposal_id}")


def approve(
    store: PreferenceStore, journal: Journal, proposal_id: str, now: datetime | None = None
) -> dict:
    now = _now(now)
    with journal.lock:
        with store.transaction() as state:
            others = [p for p in state["proposals"] if p.get("id") != proposal_id]
            proposal = _find(state, proposal_id, now)
            scope, field, op = proposal["scope"], proposal["field"], proposal["op"]
            if field not in LIST_FIELDS or op not in ("add", "remove"):
                raise PreferenceError(f"Proposal {proposal_id} is malformed")
            rules = _rules(state, scope)
            before = {field: list(rules[field])}
            values = [_normalize(field, v) for v in proposal["values"]]
            trial = _snapshot(rules, FIELDS)
            _apply_op(trial, field, op, values)
            _check_rules(trial, scope, {field})  # re-validated against the current state
            changed = _apply_op(rules, field, op, values)
            state["proposals"] = [p for p in others if not _expired(p, now)]
            after = {field: list(rules[field])}
            request = proposal.get("user_request") or ""
        change_id = _record(journal, scope, "approve_preference", before, after, request, now) if changed else None
    return {
        "proposal_id": proposal_id, "change_id": change_id,
        "applied": [{"scope": scope, "field": field, "op": op, "values": changed}] if changed else [],
    }


def reject(store: PreferenceStore, proposal_id: str, now: datetime | None = None) -> dict:
    now = _now(now)
    with store.transaction() as state:
        proposal = _find(state, proposal_id, now)
        state["proposals"] = [
            p for p in state["proposals"] if p is not proposal and not _expired(p, now)
        ]
    return {"proposal_id": proposal_id, "rejected": True, "scope": proposal["scope"],
            "field": proposal["field"], "op": proposal["op"], "values": list(proposal["values"])}


def undo_entry(
    store: PreferenceStore, journal: Journal, entry: dict, now: datetime | None = None
) -> dict:
    """Restore `before` for a preference entry. Caller holds `journal.lock`.

    Refused if any touched field no longer equals `after`, or if restoring would break a rule
    (conflict, item cap). Undo never overwrites a later change.
    """
    now = _now(now)
    change_id, scope = entry["change_id"], entry.get("scope") or entry.get("account")
    if journal.is_undone(change_id):
        raise OrganizeError(f"Change {change_id} was already undone")
    before, after = entry.get("before") or {}, entry.get("after") or {}
    fields = list(after)
    if not fields or set(before) != set(after) or not set(fields) <= set(FIELDS):
        raise OrganizeError(f"Change {change_id} has no restorable preference fields")
    try:
        with store.transaction() as state:
            rules = _rules(state, scope)
            stale = [f for f in fields if rules[f] != after[f]]
            if stale:
                raise OrganizeError(
                    f"Cannot undo {change_id}: {', '.join(stale)} in scope {scope!r} changed since; "
                    "nothing was undone"
                )
            current = _snapshot(rules, fields)
            for f in fields:
                rules[f] = list(before[f]) if f in LIST_FIELDS else before[f]
            try:
                _check_rules(rules, scope, [f for f in fields if f in LIST_FIELDS])
            except PreferenceError as exc:
                raise OrganizeError(f"Cannot undo {change_id}: {exc}") from None
    except PreferenceError as exc:
        raise OrganizeError(str(exc)) from None
    undo_id = _record(
        journal, scope, "undo_changes", current, _snapshot(before, fields),
        entry.get("user_request") or "", now, undo_of=change_id,
    )
    return {"change_id": undo_id, "undone": change_id, "modified": 0, "skipped": []}
