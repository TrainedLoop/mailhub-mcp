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
