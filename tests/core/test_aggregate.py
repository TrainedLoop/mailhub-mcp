import asyncio
from types import SimpleNamespace

import pytest

from mailhub_mcp.core.errors import AccountNotFound, AuthExpired, MailhubError
from mailhub_mcp.core.models import MessageSummary
from mailhub_mcp.core.aggregate import describe_error, fan_out, resolve, run_one


HINT = "mailhub-authorize gmail <client_secret.json> {alias}"


def msg(id, internal_date=0):
    return MessageSummary(
        id=id, thread_id=id, **{"from": "x"}, to="", subject="", date="", snippet="",
        labels=[], internal_date=internal_date, unread=False, starred=False,
        is_important=None, category=None, is_newsletter=False,
    )


def expired(alias):
    return AuthExpired(alias, HINT.format(alias=alias))


def acc(alias):
    return SimpleNamespace(alias=alias)


def test_one_failing_account_does_not_break_others():
    def fn(a):
        if a.alias == "broken":
            raise expired("broken")
        return [msg("1")]

    out = asyncio.run(fan_out([acc("ok"), acc("broken")], fn))
    assert [r.account for r in out["results"]] == ["ok"]
    assert out["errors"]["broken"] == "invalid or revoked credentials: run mailhub-authorize gmail <client_secret.json> broken again"


def test_results_merged_newest_first():
    data = {"a": [msg("1", 1), msg("5", 5)], "b": [msg("3", 3)]}
    out = asyncio.run(fan_out([acc("a"), acc("b")], lambda a: data[a.alias]))
    assert [r.internal_date for r in out["results"]] == [5, 3, 1]
    assert out["errors"] == {}


def test_results_without_internal_date_keep_order():
    from mailhub_mcp.core.models import Draft

    out = asyncio.run(
        fan_out([acc("a"), acc("b")], lambda a: [Draft(draft_id=a.alias, message_id="m", thread_id=None)])
    )
    assert [r.draft_id for r in out["results"]] == ["a", "b"]


class Registry:
    def __init__(self, aliases):
        self._a = {x: acc(x) for x in aliases}

    def all(self):
        return list(self._a.values())

    def get(self, alias):
        if alias not in self._a:
            raise AccountNotFound(alias, list(self._a))
        return self._a[alias]


def test_resolve_filters_and_rejects_unknown():
    reg = Registry(["a", "b"])
    assert [x.alias for x in resolve(reg, None)] == ["a", "b"]
    assert [x.alias for x in resolve(reg, ["b"])] == ["b"]
    with pytest.raises(AccountNotFound):
        resolve(reg, ["zzz"])


def test_resolve_dedupes_aliases_keeping_order():
    reg = Registry(["a", "b"])
    assert [x.alias for x in resolve(reg, ["a", "a"])] == ["a"]
    assert [x.alias for x in resolve(reg, ["b", "a", "b"])] == ["b", "a"]


def test_resolve_empty_list_means_all():
    reg = Registry(["a", "b"])
    assert [x.alias for x in resolve(reg, [])] == ["a", "b"]


def test_describe_mailhub_error_has_no_class_name():
    assert describe_error(expired("a"), "a") == "invalid or revoked credentials: run mailhub-authorize gmail <client_secret.json> a again"


def test_describe_other_errors():
    assert describe_error(ValueError("x"), "a") == "ValueError: x"


def test_run_one_wraps_in_core_error():
    def fn(a):
        raise expired("a")

    with pytest.raises(MailhubError) as e:
        asyncio.run(run_one(acc("a"), fn))
    assert str(e.value) == "a: invalid or revoked credentials: run mailhub-authorize gmail <client_secret.json> a again"


def test_run_one_returns_value():
    assert asyncio.run(run_one(acc("a"), lambda a: 42)) == 42
