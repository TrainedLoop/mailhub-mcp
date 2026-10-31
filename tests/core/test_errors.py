from pathlib import Path

from mailhub_mcp.core.errors import (
    AccountNotFound,
    AuthExpired,
    DuplicateAlias,
    MailhubError,
    MessageNotFound,
    OrganizeError,
    TransientError,
)


def test_error_messages():
    assert str(AuthExpired("a", "mailhub-authorize gmail <client_secret.json> a")) == (
        "invalid or revoked credentials: re-link a on the admin page (/admin), "
        "or run mailhub-authorize gmail <client_secret.json> a again"
    )
    assert "b, c" in str(AccountNotFound("z", ["b", "c"]))
    assert isinstance(OrganizeError("x"), ValueError) and isinstance(OrganizeError("x"), MailhubError)


def test_other_errors():
    e = DuplicateAlias("a", [Path("/x/gmail/a.json"), Path("/x/other/a.json")])
    assert "a" in str(e) and "/x/gmail/a.json" in str(e) and e.alias == "a"
    assert "m1" in str(MessageNotFound(["m1", "m2"]))
    assert "boom" in str(TransientError("boom"))
    for cls in (AccountNotFound, DuplicateAlias, AuthExpired, MessageNotFound, TransientError):
        assert issubclass(cls, MailhubError)
