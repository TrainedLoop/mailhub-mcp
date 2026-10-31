"""The /admin routes: owner sign-in and sign-out, the account status page, and linking, re-linking
and removing mailboxes.

Login and linking are Google OAuth round trips. Google's return is a cross-site navigation, so it
does not carry the SameSite=Strict session cookie: both callbacks work only from the SameSite=Lax
flow cookie (signed, bound to the session and the action) and end on a 200 page that moves on by
<meta http-equiv="refresh">, a same-site navigation that does carry the session cookie.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import math
import re
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio.to_thread
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from mailhub_mcp.admin_web import pages
from mailhub_mcp.admin_web.context import AdminContext
from mailhub_mcp.admin_web.credentials import remove_credential, write_credential
from mailhub_mcp.admin_web.identity import LoginError
from mailhub_mcp.admin_web.session import (
    FLOW_COOKIE,
    FLOW_SECONDS,
    REAUTH_SECONDS,
    SESSION_COOKIE,
    SESSION_SECONDS,
    Flow,
    Session,
    Signer,
    check_csrf,
    csrf_token,
    decode_flow,
    decode_session,
    encode_flow,
    encode_session,
    is_fresh,
    new_pkce,
    random_token,
    same_origin,
)
from mailhub_mcp.core.accounts import ALIAS_RE, RESERVED_ALIASES
from mailhub_mcp.core.errors import AccountNotFound, AuthExpired
from mailhub_mcp.providers.base import LinkError

log = logging.getLogger("mailhub-mcp")

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; "
    "form-action 'self' https://accounts.google.com; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

STYLE_CSS = (Path(__file__).parent / "style.css").read_text(encoding="utf-8")

LOGIN_CALLBACK_PATH = "/admin/auth/callback"
LINK_CALLBACK_PATH = "/admin/oauth/callback"
USED_STATES_CAP = 1000  # consumed flow states remembered at once; beyond it, callbacks fail closed
STATUS_CHECK_SECONDS = 10.0  # per-account status check; slower counts as "error"
_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_SAFE_PATH = re.compile(r"^/admin(?:[/?][A-Za-z0-9/_\-.~%?=&]*)?$")

_NO_RETURN = frozenset({"/admin/login", LOGIN_CALLBACK_PATH, LINK_CALLBACK_PATH, "/admin/logout"})

Endpoint = Callable[[Request], Awaitable[Response]]


def safe_path(value: object, default: str = "/admin") -> str:
    """`value` if it is a plain /admin path (no scheme, host, backslash, '//' or '..'), else default."""
    if not isinstance(value, str) or not _SAFE_PATH.match(value):
        return default
    path = value.split("?", 1)[0]
    if "//" in value or ".." in path.split("/"):
        return default
    return value


# --- responses ------------------------------------------------------------------------------


def _html(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status)


def done_page(message: str, to: str = "/admin") -> HTMLResponse:
    """200 page that moves on by <meta http-equiv="refresh"> (never a 302 back into /admin)."""
    return _html(pages.done_html(message, safe_path(to)))


def _forbidden() -> HTMLResponse:
    return _html(pages.message_page("Forbidden", "This request was refused. Reload the page and try again."), 403)


def _expired() -> HTMLResponse:
    return _html(pages.message_page("Sign-in expired", pages.EXPIRED_TEXT, "Sign in", "/admin/login"), 400)


def _error_page() -> HTMLResponse:
    return _html(pages.message_page("Error", "Something went wrong. Try again."), 500)


def _back_to(path: str, code: str) -> RedirectResponse:
    """303 to an /admin page showing the fixed message for `code` (see pages.MESSAGES)."""
    return RedirectResponse(f"{safe_path(path)}?msg={quote(code)}", status_code=303)


def _set_flow_cookie(resp: Response, ctx: AdminContext, flow: Flow) -> None:
    resp.set_cookie(
        FLOW_COOKIE,
        encode_flow(Signer(ctx.key), flow),
        max_age=FLOW_SECONDS,
        path="/admin",
        secure=True,
        httponly=True,
        samesite="Lax",
    )


def _clear_flow_cookie(resp: Response) -> None:
    resp.delete_cookie(FLOW_COOKIE, path="/admin", secure=True, httponly=True, samesite="Lax")


def _set_session_cookie(resp: Response, ctx: AdminContext, s: Session, now: float) -> None:
    remaining = SESSION_SECONDS - (now - s.login_at)
    resp.set_cookie(
        SESSION_COOKIE,
        encode_session(Signer(ctx.key), s),
        max_age=max(1, math.floor(remaining)),
        path="/admin",
        secure=True,
        httponly=True,
        samesite="Strict",
    )


# --- session helpers (also used by the account routes) ----------------------------------------


def _ctx(request: Request) -> AdminContext:
    return request.state.admin_ctx


def _current_session(request: Request, ctx: AdminContext) -> Session | None:
    return decode_session(
        Signer(ctx.key), request.cookies.get(SESSION_COOKIE), ctx.now(), ctx.allowed_emails
    )


def require_session(request: Request) -> Session | Response:
    """The signed-in owner's session, or a 303 to the sign-in page."""
    s = _current_session(request, _ctx(request))
    if s is None:
        return RedirectResponse("/admin", status_code=303)
    return s


@dataclass(frozen=True)
class Action:
    """A state-changing POST that passed every check: the owner's session and the parsed form."""

    session: Session
    form: Any


async def _checked_post(request: Request, ctx: AdminContext) -> Action | Response:
    """Session and same origin first (else 403, without reading the body), then the form, then
    the CSRF token (else 403)."""
    s = _current_session(request, ctx)
    if s is None:
        return _forbidden()
    if not same_origin(ctx.public_base_url, request.headers.get("origin"), request.headers.get("referer")):
        return _forbidden()
    form = await request.form()
    token = form.get("csrf")
    if not isinstance(token, str) or not check_csrf(ctx.key, s, token):
        return _forbidden()
    return Action(s, form)


async def require_action(request: Request, ctx: AdminContext, next_path: str | None = None) -> Action | Response:
    """Gate for a state-changing POST: session, same origin and CSRF (else 403), then a recent
    Google authentication (else 303 through Google and back to `next_path`, default this path)."""
    checked = await _checked_post(request, ctx)
    if isinstance(checked, Response):
        return checked
    if not is_fresh(checked.session, ctx.now()):
        back = safe_path(next_path or request.url.path)
        return RedirectResponse(f"/admin/login?next={quote(back, safe='/')}", status_code=303)
    return checked


def admin_endpoint(ctx: AdminContext, fn: Endpoint, methods: tuple[str, ...] = ("GET",)) -> Endpoint:
    """Wrap an admin handler: context on request.state, method check, no stack traces, and the
    security headers on every response."""
    allowed = set(methods) | ({"HEAD"} if "GET" in methods else set())

    async def endpoint(request: Request) -> Response:
        request.state.admin_ctx = ctx
        try:
            if request.method not in allowed:
                resp: Response = _html(pages.message_page("Not allowed", "Method not allowed."), 405)
                resp.headers["Allow"] = ", ".join(sorted(allowed))
            else:
                resp = await fn(request)
        except Exception as exc:  # never show a stack trace; log only the type
            log.error("admin request failed path=%s error=%s", request.url.path, type(exc).__name__)
            resp = _error_page()
        for name, value in SECURITY_HEADERS.items():
            resp.headers[name] = value
        return resp

    return endpoint


# --- single use of login flows ------------------------------------------------------------


class _UsedStates:
    """States of OAuth flows already consumed, so a replayed callback is refused even if the flow
    cookie comes back. Every entry lives FLOW_SECONDS, so insertion order is expiry order: pruning
    pops from the front until the first live entry. Full (`cap` live entries) means fail closed."""

    def __init__(self, cap: int | None = None) -> None:
        self._cap = USED_STATES_CAP if cap is None else cap
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)

    def claim(self, state: str, now: float) -> bool:
        with self._lock:
            while self._seen:
                _, until = next(iter(self._seen.items()))
                if until > now:
                    break
                self._seen.popitem(last=False)
            if state in self._seen:
                return False
            if len(self._seen) >= self._cap:
                log.warning("admin flow store full: refusing a callback")
                return False
            self._seen[state] = now + FLOW_SECONDS
            return True


async def _clearing_flow_cookie(request: Request, finish: Endpoint) -> Response:
    """Run an OAuth callback; whatever it returns, a 500 included, deletes the flow cookie."""
    try:
        resp = await finish(request)
    except Exception as exc:  # never show a stack trace; log only the type
        log.error("admin request failed path=%s error=%s", request.url.path, type(exc).__name__)
        resp = _error_page()
    _clear_flow_cookie(resp)
    return resp


def _normalize_alias(value: object) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _alias_rule_problem(alias: str) -> str | None:
    """The MESSAGES code for an alias that breaks the naming rule, else None."""
    if not ALIAS_RE.fullmatch(alias):  # fullmatch: "$" alone would accept a trailing newline
        return "alias-invalid"
    if alias in RESERVED_ALIASES:
        return "alias-reserved"
    return None


# --- routes -------------------------------------------------------------------------------


def admin_routes(ctx: AdminContext) -> list[Route]:
    # Separate stores: anonymous sign-in attempts can fill the login one, never the link one.
    login_states = _UsedStates()
    link_states = _UsedStates()
    signer = Signer(ctx.key)
    mutations = threading.Lock()  # one link, re-link or removal at a time: check and write together

    async def style(request: Request) -> Response:
        return Response(STYLE_CSS, media_type="text/css")

    async def status(request: Request) -> Response:
        msg = request.query_params.get("msg")
        s = _current_session(request, ctx)
        if s is None:
            return _html(pages.sign_in_page(msg))
        rows = list(await asyncio.gather(*(_check_account(ctx, a) for a in ctx.registry.all())))
        linked = {r.alias for r in rows}
        removed, book_unreadable = [], False
        try:
            entries = await run_in_threadpool(ctx.book.entries)
            # on record but not linked: removed, or a credential file deleted by hand
            removed = [en for en in entries if en.alias not in linked]
        except Exception as exc:
            log.error("admin cannot read the account book error=%s", type(exc).__name__)
            book_unreadable = True
        return _html(pages.status_page(
            email=s.email,
            csrf=csrf_token(ctx.key, s),
            rows=rows,
            removed=removed,
            writable=ctx.writable,
            has_auth_time=s.has_auth_time,
            book_unreadable=book_unreadable,
            msg=msg,
        ))

    # --- sign-in ----------------------------------------------------------------------------

    async def login(request: Request) -> Response:
        now = ctx.now()
        current = _current_session(request, ctx)
        verifier, challenge = new_pkce()
        flow = Flow(
            kind="login",
            state=random_token(),
            nonce=random_token(),
            code_verifier=verifier,
            created_at=now,
            return_to=_return_to(request.query_params.get("next")),
            sid=current.sid if current else None,
            email=current.email if current else None,
            login_at=current.login_at if current else None,
        )
        url = ctx.identity.authorization_url(
            _redirect_uri(ctx), flow.state, flow.nonce, challenge,
            max_age=REAUTH_SECONDS if current else None,
        )
        resp = RedirectResponse(url, status_code=303)
        _set_flow_cookie(resp, ctx, flow)
        return resp

    async def auth_callback(request: Request) -> Response:
        return await _clearing_flow_cookie(request, _finish_login)

    async def _finish_login(request: Request) -> Response:
        now = ctx.now()
        flow = decode_flow(signer, request.cookies.get(FLOW_COOKIE), now)
        state = request.query_params.get("state") or ""
        if flow is None or flow.kind != "login":
            return _expired()
        if not hmac.compare_digest(flow.state.encode("ascii"), state.encode("utf-8")):
            return _expired()
        if not login_states.claim(flow.state, now):
            return _expired()
        if request.query_params.get("error"):
            return _html(pages.message_page(
                "Sign-in cancelled", "Google sign-in was cancelled.", "Sign in", "/admin/login"
            ), 400)
        code = request.query_params.get("code")
        if not code:
            return _expired()
        try:
            owner = await run_in_threadpool(
                ctx.identity.complete, code, _redirect_uri(ctx), flow.code_verifier, flow.nonce
            )
        except LoginError as exc:
            return _html(pages.message_page("Sign-in failed", str(exc), "Sign in", "/admin/login"), 400)
        email = owner.email.lower()
        if email not in ctx.allowed_emails:
            log.warning("admin login refused email=%s", email)
            return _html(pages.message_page("Not allowed", pages.NOT_ALLOWED_TEXT), 403)

        reauth = (
            flow.sid is not None
            and flow.email is not None
            and flow.email.lower() == email
            and flow.login_at is not None
            and 0 <= now - flow.login_at < SESSION_SECONDS
        )
        auth_time = owner.auth_time
        has_auth_time = auth_time is not None
        if isinstance(auth_time, (int, float)) and math.isfinite(auth_time):
            auth_at = min(float(auth_time), now)
        else:
            auth_at = now
        if reauth and now - auth_at >= REAUTH_SECONDS:
            # Google ignored max_age; the completed round trip is the fallback proof (as when the
            # claim is absent), so the action does not send the owner around again forever.
            log.warning("admin reauth: google returned a stale auth_time")
            auth_at = now
        session = Session(
            email=email,
            sid=flow.sid if reauth else random_token(),
            login_at=flow.login_at if reauth else now,
            auth_at=auth_at,
            has_auth_time=has_auth_time,
        )
        log.info("admin login email=%s%s", email, " reauth" if reauth else "")
        resp = done_page("Signed in", flow.return_to)
        _set_session_cookie(resp, ctx, session, now)
        return resp

    async def logout(request: Request) -> Response:
        checked = await _checked_post(request, ctx)
        if isinstance(checked, Response):
            return checked
        log.info("admin logout email=%s", checked.session.email)
        resp = RedirectResponse("/admin", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/admin", secure=True, httponly=True, samesite="Strict")
        return resp

    # --- link and re-link -------------------------------------------------------------------

    def _start_flow(kind: str, s: Session, alias: str, provider: str) -> Response:
        verifier, challenge = new_pkce()
        flow = Flow(
            kind=kind,  # type: ignore[arg-type]
            state=random_token(),
            nonce=random_token(),
            code_verifier=verifier,
            created_at=ctx.now(),
            sid=s.sid,
            email=s.email,
            alias=alias,
            provider=provider,
        )
        url = ctx.linkers[provider].authorization_url(_link_redirect_uri(ctx), flow.state, challenge)
        resp = RedirectResponse(url, status_code=303)
        _set_flow_cookie(resp, ctx, flow)
        log.info("admin %s started alias=%s provider=%s by=%s", kind, alias, provider, s.email)
        return resp

    async def link_form(request: Request) -> Response:
        s = require_session(request)
        if isinstance(s, Response):
            return s
        if not ctx.writable:
            return _back_to("/admin", "read-only")
        providers = [(name, linker.label) for name, linker in sorted(ctx.linkers.items())]
        return _html(pages.link_page(
            csrf=csrf_token(ctx.key, s), providers=providers, msg=request.query_params.get("msg")
        ))

    async def link_start(request: Request) -> Response:
        act = await require_action(request, ctx, "/admin/link")
        if isinstance(act, Response):
            return act
        if not ctx.writable:
            return _back_to("/admin", "read-only")
        provider = act.form.get("provider")
        if not isinstance(provider, str) or provider not in ctx.linkers:
            return _back_to("/admin/link", "provider-invalid")
        alias = _normalize_alias(act.form.get("alias"))
        problem = _alias_rule_problem(alias)
        if problem:
            return _back_to("/admin/link", problem)
        if alias in ctx.registry.aliases():
            return _back_to("/admin/link", "alias-taken")
        # anything on record (removed, or linked before) comes back only through re-link, so its
        # journal never meets a different mailbox
        if await run_in_threadpool(ctx.book.get, alias) is not None:
            return _back_to("/admin/link", "alias-removed")
        return _start_flow("link", act.session, alias, provider)

    async def relink_start(request: Request) -> Response:
        act = await require_action(request, ctx, "/admin")
        if isinstance(act, Response):
            return act
        if not ctx.writable:
            return _back_to("/admin", "read-only")
        alias = request.path_params["alias"]
        if _alias_rule_problem(alias):
            return _back_to("/admin", "unknown-alias")
        provider = await run_in_threadpool(_recorded_provider, ctx, alias)
        if provider is None:
            return _back_to("/admin", "unknown-alias")
        if provider not in ctx.linkers:
            return _back_to("/admin", "provider-invalid")
        return _start_flow("relink", act.session, alias, provider)

    async def link_callback(request: Request) -> Response:
        return await _clearing_flow_cookie(request, _finish_link)

    def _link_refused(text: str, status_code: int = 400) -> Response:
        return _html(pages.message_page("Not linked", text), status_code)

    async def _finish_link(request: Request) -> Response:
        now = ctx.now()
        flow = decode_flow(signer, request.cookies.get(FLOW_COOKIE), now)
        state = request.query_params.get("state") or ""
        expired = _html(pages.message_page("Link expired", pages.EXPIRED_TEXT), 400)
        if flow is None or flow.kind not in ("link", "relink"):
            return expired
        if not hmac.compare_digest(flow.state.encode("ascii"), state.encode("utf-8")):
            return expired
        if not link_states.claim(flow.state, now):
            return expired
        owner = (flow.email or "").lower()
        if not flow.sid or owner not in ctx.allowed_emails:
            log.warning("admin %s refused: owner no longer allowed email=%s", flow.kind, owner)
            return _link_refused(pages.NOT_ALLOWED_TEXT, 403)
        alias, provider = flow.alias or "", flow.provider or ""
        if _alias_rule_problem(alias) or provider not in ctx.linkers:
            return expired
        if not ctx.writable:
            return _link_refused(pages.READ_ONLY_TEXT)
        if request.query_params.get("error"):
            if request.query_params.get("error") == "access_denied":
                return _html(pages.message_page("Consent refused", pages.CONSENT_REFUSED_TEXT), 400)
            return _link_refused("Google did not complete the request. Nothing was changed.")
        code = request.query_params.get("code")
        if not code:
            return expired
        try:
            result = await run_in_threadpool(
                ctx.linkers[provider].complete, code, _link_redirect_uri(ctx), flow.code_verifier
            )
        except LinkError as exc:
            return _link_refused(str(exc))
        address = result.address.strip().lower() if isinstance(result.address, str) else ""
        if not address:
            return _link_refused("Google did not report the mailbox address. Nothing was changed.")
        refusal = await run_in_threadpool(
            _commit_link, flow.kind, alias, provider, address, result.credential_json, owner, now
        )
        if refusal is not None:
            return _link_refused(refusal)
        return done_page(f"Linked {alias} ({address})")

    def _commit_link(
        kind: str, alias: str, provider: str, address: str, credential_json: str, owner: str, now: float
    ) -> str | None:
        """Every check, then the writes, under one lock. Returns the refusal text, or None once
        written. Nothing touches the disk before every check has passed."""
        with mutations:
            other = ctx.book.alias_for_address(address)
            if other is not None and other != alias:
                log.warning("admin %s refused: address linked as %s alias=%s address=%s by=%s",
                            kind, other, alias, address, owner)
                return pages.MESSAGES["address-taken"]
            entry = ctx.book.get(alias)
            linked = alias in ctx.registry.aliases()
            recorded = entry.address.lower() if entry is not None and entry.address else None
            if kind == "link":
                if linked or entry is not None:
                    return pages.MESSAGES["alias-taken"]
            else:
                if not linked and entry is None:
                    return pages.MESSAGES["unknown-alias"]
                if recorded is not None and recorded != address:
                    log.warning("admin relink refused: different mailbox alias=%s address=%s by=%s",
                                alias, address, owner)
                    return f"That is a different mailbox than {alias} (expected {recorded})"
                if recorded is None:
                    log.warning("admin relink without recorded address alias=%s provider=%s address=%s by=%s",
                                alias, provider, address, owner)
            path = write_credential(ctx.accounts_dir, provider, alias, credential_json)
            ctx.registry.upsert(alias, provider, path)
            ctx.book.record_linked(alias, provider, address, datetime.fromtimestamp(now, UTC))
            log.info("admin %s alias=%s provider=%s address=%s by=%s", kind, alias, provider, address, owner)
            return None

    # --- remove -----------------------------------------------------------------------------

    def _linked_account(alias: str) -> Any:
        if _alias_rule_problem(alias):
            return None
        try:
            return ctx.registry.get(alias)
        except AccountNotFound:
            return None

    async def remove_form(request: Request) -> Response:
        s = require_session(request)
        if isinstance(s, Response):
            return s
        if not ctx.writable:
            return _back_to("/admin", "read-only")
        alias = request.path_params["alias"]
        if _linked_account(alias) is None:
            return _back_to("/admin", "not-linked")
        entry = await run_in_threadpool(ctx.book.get, alias)
        undoable = await run_in_threadpool(ctx.journal.undoable_count, alias)
        return _html(pages.remove_page(
            alias=alias,
            address=entry.address if entry else None,
            undoable=undoable,
            csrf=csrf_token(ctx.key, s),
            msg=request.query_params.get("msg"),
        ))

    async def remove(request: Request) -> Response:
        alias = request.path_params["alias"]
        page = f"/admin/accounts/{quote(alias, safe='')}/remove"
        act = await require_action(request, ctx, page)
        if isinstance(act, Response):
            return act
        if not ctx.writable:
            return _back_to("/admin", "read-only")
        if _linked_account(alias) is None:
            return _back_to("/admin", "not-linked")
        if _normalize_alias(act.form.get("confirm")) != alias:
            return _back_to(page, "confirm-mismatch")
        revoked = await run_in_threadpool(_commit_remove, alias, act.session.email, ctx.now())
        if revoked is None:
            return _back_to("/admin", "not-linked")
        if not revoked:
            return _html(pages.revoke_failed_page(alias))
        return done_page(f"Removed {alias}")

    def _commit_remove(alias: str, owner: str, now: float) -> bool | None:
        """Revoke (it needs the refresh token in the file), delete the file, drop the alias from the
        registry, mark it removed in the book. None if the alias is not linked any more; otherwise
        whether Google confirmed the revocation (a failed revocation does not stop the removal)."""
        with mutations:
            account = _linked_account(alias)
            if account is None:
                return None
            provider = account.provider_name
            linker = ctx.linkers.get(provider)
            revoked = False
            if linker is not None:
                try:
                    revoked = linker.revoke(account.credential_path) is True
                except Exception as exc:
                    log.warning("admin revoke failed alias=%s error=%s", alias, type(exc).__name__)
            remove_credential(ctx.accounts_dir, provider, alias)
            ctx.registry.remove(alias)
            ctx.book.record_removed(alias, datetime.fromtimestamp(now, UTC))
            entry = ctx.book.get(alias)
            address = entry.address if entry and entry.address else "unknown"
            log.info("admin remove alias=%s provider=%s address=%s by=%s", alias, provider, address, owner)
            if not revoked:
                log.warning("admin remove: revocation not confirmed alias=%s", alias)
            return revoked

    def route(path: str, fn: Endpoint, methods: tuple[str, ...] = ("GET",)) -> Route:
        return Route(path, admin_endpoint(ctx, fn, methods), methods=_ALL_METHODS)

    def get_or_post(get_fn: Endpoint, post_fn: Endpoint) -> Endpoint:
        async def endpoint(request: Request) -> Response:
            return await (post_fn if request.method == "POST" else get_fn)(request)

        return endpoint

    return [
        route("/admin", status),
        route("/admin/style.css", style),
        route("/admin/login", login),
        route(LOGIN_CALLBACK_PATH, auth_callback),
        route("/admin/logout", logout, ("POST",)),
        route("/admin/link", get_or_post(link_form, link_start), ("GET", "POST")),
        route("/admin/accounts/{alias}/relink", relink_start, ("POST",)),
        route("/admin/accounts/{alias}/remove", get_or_post(remove_form, remove), ("GET", "POST")),
        route(LINK_CALLBACK_PATH, link_callback),
    ]


def _return_to(value: object) -> str:
    """Where a finished login goes: a safe /admin page that does not start another round trip."""
    path = safe_path(value)
    if path.split("?", 1)[0] in _NO_RETURN:
        return "/admin"
    return path


def _redirect_uri(ctx: AdminContext) -> str:
    return ctx.public_base_url.rstrip("/") + LOGIN_CALLBACK_PATH


def _link_redirect_uri(ctx: AdminContext) -> str:
    return ctx.public_base_url.rstrip("/") + LINK_CALLBACK_PATH


def _recorded_provider(ctx: AdminContext, alias: str) -> str | None:
    """The provider of a linked alias, or of one on record in the book; None if neither."""
    try:
        return ctx.registry.get(alias).provider_name
    except AccountNotFound:
        pass
    entry = ctx.book.get(alias)
    return entry.provider if entry is not None else None


async def _in_thread(fn: Callable[[], Any], timeout: float) -> Any:
    """Run blocking `fn` in a worker thread; give up after `timeout` seconds (TimeoutError). The
    thread is abandoned, not killed: it finishes on its own."""
    return await asyncio.wait_for(anyio.to_thread.run_sync(fn, abandon_on_cancel=True), timeout)


async def _check_account(ctx: AdminContext, account: Any) -> pages.AccountRow:
    """One cheap provider call per account; a failing or slow account never breaks the page."""
    state: pages.State = "ok"
    address: str | None = None
    try:
        address = await _in_thread(lambda: account.provider.address(), STATUS_CHECK_SECONDS)
    except AuthExpired:
        state = "credential expired"
    except TimeoutError:
        log.warning("admin status check timed out alias=%s", account.alias)
        state = "error"
    except Exception as exc:
        log.warning("admin status check failed alias=%s error=%s", account.alias, type(exc).__name__)
        state = "error"
    if state == "ok" and isinstance(address, str) and address:
        try:
            await run_in_threadpool(ctx.book.record_address, account.alias, account.provider_name, address)
        except Exception as exc:
            log.error("admin cannot record address alias=%s error=%s", account.alias, type(exc).__name__)
    else:
        address = None
        try:
            entry = await run_in_threadpool(ctx.book.get, account.alias)
            address = entry.address if entry else None
        except Exception as exc:
            log.error("admin cannot read the account book alias=%s error=%s", account.alias, type(exc).__name__)
    return pages.AccountRow(account.alias, account.provider_name, address, state)
