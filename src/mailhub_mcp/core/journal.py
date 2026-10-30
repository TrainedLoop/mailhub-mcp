import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


log = logging.getLogger("mailhub-mcp.journal")


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        # Held by callers that need check-then-write sequences (undo) to be atomic.
        self.lock = threading.Lock()

    def record(
        self,
        account: str,
        tool: str,
        added: list[str],
        removed: list[str],
        prior: dict[str, list[str]],
        query: str | None = None,
        undo_of: str | None = None,
        *,
        kind: str | None = None,
        scope: str | None = None,
        before: dict | None = None,
        after: dict | None = None,
        user_request: str | None = None,
        timestamp: datetime | None = None,
    ) -> str:
        change_id = uuid.uuid4().hex[:12]
        entry = {
            "change_id": change_id,
            "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
            "account": account,
            "tool": tool,
            "added": added,
            "removed": removed,
            "prior": prior,
            "query": query,
            "undo_of": undo_of,
        }
        if kind is not None:  # label entries keep their exact shape: no `kind` key
            entry.update(kind=kind, scope=scope, before=before, after=after, user_request=user_request)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        log.info(
            "change %s account=%s tool=%s added=%s removed=%s messages=%d",
            change_id, account, tool, added, removed, len(prior),
        )
        return change_id

    def _entries(self) -> list[dict]:
        with self._lock:
            if not self.path.exists():
                return []
            lines = self.path.read_text(encoding="utf-8").splitlines()
        entries = []
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                entry = None
            if not isinstance(entry, dict) or "change_id" not in entry:
                log.warning("skipping unreadable journal line %d in %s", number, self.path)
                continue
            entries.append(entry)
        return entries

    def entries(self, since: datetime | None = None) -> list[dict]:
        """Valid entries, newest first, by reversed file order.

        Append order is causal (writes are serialized), so it is trusted over wall-clock time. `since`
        only filters, by timestamp, inclusively; naive times are UTC.
        """
        entries = list(reversed(self._entries()))
        if since is None:
            return entries
        floor = _aware(since)
        kept = []
        for entry in entries:
            try:
                ts = _aware(datetime.fromisoformat(entry["timestamp"]))
            except (KeyError, TypeError, ValueError):
                log.warning("skipping journal entry %s with unreadable timestamp", entry["change_id"])
                continue
            if ts >= floor:
                kept.append(entry)
        return kept

    def get(self, change_id: str) -> dict:
        for entry in self._entries():
            if entry["change_id"] == change_id:
                return entry
        raise KeyError(change_id)

    def is_undone(self, change_id: str) -> bool:
        return any(e.get("undo_of") == change_id for e in self._entries())


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def inverse_ops(entry: dict) -> list[dict]:
    groups: dict[tuple, list[str]] = {}
    for mid, prior in entry["prior"].items():
        had = set(prior)
        remove = tuple(l for l in entry["added"] if l not in had)
        add = tuple(l for l in entry["removed"] if l in had)
        if remove or add:
            groups.setdefault((add, remove), []).append(mid)
    return [{"ids": ids, "add": list(a), "remove": list(r)} for (a, r), ids in groups.items()]
