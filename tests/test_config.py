import dataclasses

import pytest

from mailhub_mcp.config import DEFAULT_REDIRECT_URIS, Settings

CLAUDE = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")


def test_redirect_uris_default_to_the_claude_callbacks(monkeypatch):
    monkeypatch.delenv("ALLOWED_REDIRECT_URIS", raising=False)
    assert Settings.from_env().allowed_redirect_uris == CLAUDE == DEFAULT_REDIRECT_URIS


def test_redirect_uris_custom_list_is_trimmed_and_skips_empty_entries(monkeypatch):
    monkeypatch.setenv(
        "ALLOWED_REDIRECT_URIS", " https://app.example/cb ,, http://localhost:8080/cb , http://127.0.0.1 ,"
    )
    assert Settings.from_env().allowed_redirect_uris == (
        "https://app.example/cb", "http://localhost:8080/cb", "http://127.0.0.1",
    )


@pytest.mark.parametrize(
    "bad",
    [
        "http://app.example/cb",          # cleartext off loopback
        "http://localhost.evil.test/cb",  # not loopback
        "https://app.example",            # no path: FastMCP would accept any path on the host
        "https://app.example/",
        "https://app.example/*",          # wildcards turn the allowlist into patterns
        "https://*.example/cb",
        "https://app.example/cb?x=1",
        "https://user@app.example/cb",
        "myapp://callback",
        "javascript:alert(1)",
        "claude.ai/api/mcp/auth_callback",
        "https://app.example/a/../cb",
        "http://localhost:*/cb",
        "http://localhost:abc/cb",
    ],
)
def test_redirect_uris_invalid_entries_are_rejected_by_name(monkeypatch, bad):
    monkeypatch.setenv("ALLOWED_REDIRECT_URIS", f"https://ok.example/cb,{bad}")
    with pytest.raises(ValueError, match="ALLOWED_REDIRECT_URIS") as exc:
        Settings.from_env()
    assert repr(bad) in str(exc.value)


@pytest.mark.parametrize("raw", ["", "  ", " , "])
def test_redirect_uris_explicitly_empty_is_an_error(monkeypatch, raw):
    monkeypatch.setenv("ALLOWED_REDIRECT_URIS", raw)
    with pytest.raises(ValueError, match="ALLOWED_REDIRECT_URIS.*lock"):
        Settings.from_env()


def _register(settings, uri):
    import asyncio

    from mcp.shared.auth import OAuthClientInformationFull

    from mailhub_mcp.auth import build_auth

    auth = build_auth(settings)
    client = OAuthClientInformationFull(client_id="c1", redirect_uris=[uri], token_endpoint_auth_method="none")
    return asyncio.run(auth.register_client(client))


def _settings(monkeypatch, value=None):
    if value is None:
        monkeypatch.delenv("ALLOWED_REDIRECT_URIS", raising=False)
    else:
        monkeypatch.setenv("ALLOWED_REDIRECT_URIS", value)
    return Settings.from_env()


def test_default_allowlist_accepts_claude_and_refuses_others(monkeypatch):

    s = _settings(monkeypatch)
    _register(s, CLAUDE[0])
    _register(s, CLAUDE[1])
    with pytest.raises(Exception, match="not allowed"):
        _register(s, "https://other.example/callback")
    with pytest.raises(Exception, match="not allowed"):
        _register(s, "https://claude.ai/api/mcp/other")


def test_listed_non_claude_callback_is_accepted_and_claude_no_longer_is(monkeypatch):
    s = _settings(monkeypatch, "https://other.example/callback")
    _register(s, "https://other.example/callback")
    with pytest.raises(Exception, match="not allowed"):
        _register(s, CLAUDE[0])
    with pytest.raises(Exception, match="not allowed"):
        _register(s, "https://other.example/callback2")


def test_loopback_entry_without_port_matches_any_port(monkeypatch):
    s = _settings(monkeypatch, "http://localhost/cb")
    _register(s, "http://localhost:5555/cb")
    with pytest.raises(Exception, match="not allowed"):
        _register(s, "http://localhost:5555/other")
