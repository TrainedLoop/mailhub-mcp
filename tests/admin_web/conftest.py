"""Shared fixtures for the admin web routes: fake identity, fake linker, a context and a client."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mailhub_mcp.admin_web.context import AdminContext
from mailhub_mcp.admin_web.identity import LoginError, OwnerLogin
from mailhub_mcp.admin_web.routes import admin_routes
from mailhub_mcp.admin_web.session import FLOW_COOKIE, SESSION_COOKIE, derive_key
from mailhub_mcp.core.account_book import AccountBook
from mailhub_mcp.core.accounts import AccountRegistry
from mailhub_mcp.core.journal import Journal
from mailhub_mcp.providers.base import LinkResult

PUBLIC = "https://example.test"
CLIENT_BASE = "http://testserver:8742"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"


class FakeIdentity:
    """Answers like Google: each code works once, then the exchange fails."""

    def __init__(self) -> None:
        self.email = "me@gmail.com"
        self.auth_time: float | None = None
        self.fail: str | None = None
        self.calls: list[dict] = []
        self._used: set[str] = set()

    def authorization_url(self, redirect_uri, state, nonce, code_challenge, *, max_age=None):
        params = {"redirect_uri": redirect_uri, "state": state, "nonce": nonce,
                  "code_challenge": code_challenge}
        if max_age is not None:
            params["max_age"] = str(max_age)
        q = urlencode(params)
        return f"{GOOGLE_AUTH}?{q}"

    def complete(self, code, redirect_uri, code_verifier, nonce):
        self.calls.append({"code": code, "redirect_uri": redirect_uri,
                           "code_verifier": code_verifier, "nonce": nonce})
        if self.fail:
            raise LoginError(self.fail)
        if code in self._used:
            raise LoginError("Google sign-in failed")
        self._used.add(code)
        return OwnerLogin(self.email, self.auth_time)


class FakeLinker:
    label: ClassVar[str] = "Gmail"

    def __init__(self) -> None:
        self.result = LinkResult(credential_json='{"refresh_token": "r"}', address="box@example.test")
        self.revoked: list[Path] = []

    def authorization_url(self, redirect_uri, state, code_challenge):
        return f"{GOOGLE_AUTH}?{urlencode({'redirect_uri': redirect_uri, 'state': state})}"

    def complete(self, code, redirect_uri, code_verifier):
        return self.result

    def revoke(self, credential_path):
        self.revoked.append(credential_path)
        return True


class FakeProvider:
    reauthorize_hint = "mailhub-mcp auth"

    def __init__(self, alias: str, address: str | None = None, error: Exception | None = None):
        self.alias = alias
        self._address = address
        self._error = error

    def address(self) -> str:
        if self._error is not None:
            raise self._error
        return self._address


@dataclass
class Clock:
    t: float = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


@dataclass
class Env:
    ctx: AdminContext
    client: TestClient
    identity: FakeIdentity
    linker: FakeLinker
    clock: Clock
    providers: dict[str, FakeProvider] = field(default_factory=dict)

    def add_account(self, alias: str, address: str | None = None, error: Exception | None = None):
        self.providers[alias] = FakeProvider(alias, address, error)
        path = self.ctx.accounts_dir / "gmail" / f"{alias}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        self.ctx.registry.upsert(alias, "gmail", path)


def make_env(tmp_path: Path, writable: bool = True) -> Env:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    providers: dict[str, FakeProvider] = {}

    def factory(alias, path):
        return providers[alias]

    clock = Clock()
    identity = FakeIdentity()
    linker = FakeLinker()
    ctx = AdminContext(
        public_base_url=PUBLIC,
        allowed_emails=frozenset({"me@gmail.com"}),
        key=derive_key("k" * 32),
        registry=AccountRegistry(accounts_dir, {"gmail": factory}),
        book=AccountBook(tmp_path / "accounts.json"),
        journal=Journal(tmp_path / "changes.jsonl"),
        accounts_dir=accounts_dir,
        identity=identity,
        linkers={"gmail": linker},
        writable=writable,
        now=clock,
    )
    client = TestClient(Starlette(routes=admin_routes(ctx)), base_url=CLIENT_BASE)
    client.identity = identity  # type: ignore[attr-defined]  # used by `login`
    return Env(ctx, client, identity, linker, clock, providers)


@pytest.fixture
def env(tmp_path) -> Env:
    return make_env(tmp_path)


def set_cookie_headers(resp) -> list[str]:
    return resp.headers.get_list("set-cookie")


def cookie_header(resp, name: str) -> str | None:
    for h in set_cookie_headers(resp):
        if h.split("=", 1)[0].strip() == name:
            return h
    return None


def cookie_value(resp, name: str) -> str | None:
    h = cookie_header(resp, name)
    if h is None:
        return None
    return h.split("=", 1)[1].split(";", 1)[0].strip().strip('"')


def start_login(client: TestClient, next_: str | None = None):
    """GET /admin/login; returns (response, flow cookie value, query of the Google URL)."""
    url = "/admin/login" + (f"?{urlencode({'next': next_})}" if next_ is not None else "")
    resp = client.get(url, follow_redirects=False)
    flow = cookie_value(resp, FLOW_COOKIE)
    query = parse_qs(urlsplit(resp.headers.get("location", "")).query)
    return resp, flow, query


def callback(client: TestClient, flow: str | None, state: str, code: str = "code-1", **extra):
    client.cookies.clear()
    if flow is not None:
        client.cookies.set(FLOW_COOKIE, flow)
    params = {"state": state, "code": code, **extra}
    return client.get(f"/admin/auth/callback?{urlencode(params)}", follow_redirects=False)


def login(client: TestClient, email: str = "me@gmail.com", auth_time: float | None = None) -> str:
    """Run the whole sign-in flow and leave only the session cookie on the client. Returns it."""
    identity: FakeIdentity = client.identity  # type: ignore[attr-defined]
    identity.email = email
    identity.auth_time = auth_time
    client.login_count = getattr(client, "login_count", 0) + 1  # type: ignore[attr-defined]
    _, flow, query = start_login(client)
    resp = callback(client, flow, query["state"][0], code=f"login-code-{client.login_count}")
    token = cookie_value(resp, SESSION_COOKIE)
    assert token, resp.text
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token)
    return token
