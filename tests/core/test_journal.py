import pytest

from mailhub_mcp.core.journal import Journal, inverse_ops


def _norm(ops):
    return {(tuple(sorted(o["ids"])), tuple(sorted(o["add"])), tuple(sorted(o["remove"]))) for o in ops}


def test_inverse_respects_prior_state():
    entry = {
        "added": ["STARRED", "L_org"],
        "removed": ["INBOX"],
        "prior": {"m1": ["INBOX"], "m2": ["STARRED", "INBOX"], "m3": []},
    }
    assert _norm(inverse_ops(entry)) == {
        (("m1",), ("INBOX",), ("L_org", "STARRED")),
        (("m2",), ("INBOX",), ("L_org",)),
        (("m3",), (), ("L_org", "STARRED")),
    }


def test_inverse_groups_identical_and_skips_noop():
    entry = {
        "added": ["X"],
        "removed": [],
        "prior": {"m1": [], "m2": [], "m3": ["X"]},
    }
    ops = inverse_ops(entry)
    assert _norm(ops) == {(("m1", "m2"), (), ("X",))}


def test_journal_roundtrip_and_undone_flag(tmp_path):
    j = Journal(tmp_path / "sub" / "c.jsonl")
    cid = j.record("a", "modify_emails", ["X"], [], {"m1": []})
    e = j.get(cid)
    assert e["account"] == "a" and e["tool"] == "modify_emails" and e["timestamp"]
    assert not j.is_undone(cid)
    j.record("a", "undo_change", [], ["X"], {"m1": ["X"]}, undo_of=cid)
    assert j.is_undone(cid)
    assert Journal(tmp_path / "sub" / "c.jsonl").is_undone(cid)


def test_journal_unknown_id(tmp_path):
    j = Journal(tmp_path / "c.jsonl")
    with pytest.raises(KeyError):
        j.get("nope")
    j.record("a", "t", [], [], {})
    with pytest.raises(KeyError):
        j.get("nope")


def test_truncated_last_line_is_skipped(tmp_path, caplog):
    p = tmp_path / "j.jsonl"
    j = Journal(p)
    cid = j.record("a", "modify_emails", ["X"], [], {"m1": []})
    p.write_text(p.read_text() + '{"change_id": "tru')
    assert j.get(cid)["account"] == "a" and "line 2" in caplog.text


def test_non_entry_json_lines_are_skipped(tmp_path, caplog):
    p = tmp_path / "j.jsonl"
    j = Journal(p)
    cid = j.record("a", "modify_emails", ["X"], [], {"m1": []})
    p.write_text(p.read_text() + '[1, 2]\n{"foo": 1}\n"str"\n')
    assert j.get(cid)["account"] == "a" and not j.is_undone(cid)
    assert "line 2" in caplog.text and "line 3" in caplog.text and "line 4" in caplog.text


def test_record_logs_summary_at_info(tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="mailhub-mcp.journal")
    j = Journal(tmp_path / "c.jsonl")
    cid = j.record("acc", "modify_emails", ["A"], ["B"], {"m1": [], "m2": ["B"]})
    rec = next(r for r in caplog.records if r.levelno == logging.INFO)
    text = rec.getMessage()
    for part in (cid, "acc", "modify_emails", "['A']", "['B']", "2"):
        assert part in text
    assert "prior" not in text


def _write_entries(path, rows):
    import json
    path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))


def _row(cid, ts, **kw):
    return {"change_id": cid, "timestamp": ts, "account": "a", "tool": "t", "added": [], "removed": [],
            "prior": {}, "query": None, "undo_of": None, **kw}


def test_entries_newest_first_with_ties_in_reverse_file_order(tmp_path):
    p = tmp_path / "c.jsonl"
    _write_entries(p, [
        _row("old", "2026-01-01T00:00:00+00:00"),
        _row("tie1", "2026-01-02T00:00:00+00:00"),
        _row("tie2", "2026-01-02T00:00:00+00:00"),
        _row("new", "2026-01-03T00:00:00+00:00"),
    ])
    assert [e["change_id"] for e in Journal(p).entries()] == ["new", "tie2", "tie1", "old"]


def test_entries_since_is_inclusive_and_handles_naive_and_aware(tmp_path):
    from datetime import datetime, timedelta, timezone
    p = tmp_path / "c.jsonl"
    _write_entries(p, [
        _row("a", "2026-01-01T00:00:00+00:00"),
        _row("b", "2026-01-02T00:00:00+00:00"),
        _row("c", "2026-01-03T00:00:00+00:00"),
    ])
    j = Journal(p)
    assert [e["change_id"] for e in j.entries(datetime(2026, 1, 2))] == ["c", "b"]  # naive = UTC
    assert [e["change_id"] for e in j.entries(datetime(2026, 1, 2, tzinfo=timezone.utc))] == ["c", "b"]
    east = timezone(timedelta(hours=2))
    # 03T01:00+02:00 is 02T23:00Z (after b, before c); 03T02:00+02:00 is exactly c
    assert [e["change_id"] for e in j.entries(datetime(2026, 1, 3, 1, tzinfo=east))] == ["c"]
    assert [e["change_id"] for e in j.entries(datetime(2026, 1, 3, 2, tzinfo=east))] == ["c"]


def test_entries_skips_malformed_lines(tmp_path):
    p = tmp_path / "c.jsonl"
    _write_entries(p, [_row("a", "2026-01-01T00:00:00+00:00"), "not json", "[1]", '{"x": 1}', "",
                       _row("b", "2026-01-02T00:00:00+00:00")])
    assert [e["change_id"] for e in Journal(p).entries()] == ["b", "a"]
    assert Journal(tmp_path / "missing.jsonl").entries() == []
