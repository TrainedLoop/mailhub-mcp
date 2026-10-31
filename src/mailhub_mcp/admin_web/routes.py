"""The /admin routes: owner sign-in, sign-out and the account status page.

Login is a Google OpenID Connect round trip. Google's return is a cross-site navigation, so it does
not carry the SameSite=Strict session cookie: the callback works only from the SameSite=Lax flow
cookie and ends on a 200 page that moves on by <meta http-equiv="refresh">, a same-site navigation
that does carry the new session cookie.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import math
import re
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from mailhub_mcp.admin_web import pages
from mailhub_mcp.admin_web.context import AdminContext
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
from mailhub_mcp.core.errors import AuthExpired

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
_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_SAFE_PATH = re.compile(r"^/admin(?:[/?][A-Za-z0-9/_\-.~%?=&]*)?$")

_NO_RETURN = frozenset({"/admin/login", LOGIN_CALLBACK_PATH, "/admin/oauth/callback", "/admin/logout"})

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


def require_action(
    request: Request, ctx: AdminContext, form: Any, next_path: str | None = None
) -> Session | Response:
    """Gate for a state-changing POST: session, same origin and CSRF (else 403), then a recent
    Google authentication (else 303 through Google and back to `next_path`, default this path)."""
    s = _current_session(request, ctx)
    if s is None:
        return _forbidden()
    if not same_origin(ctx.public_base_url, request.headers.get("origin"), request.headers.get("referer")):
        return _forbidden()
    token = form.get("csrf") if form is not None else None
    if not isinstance(token, str) or not check_csrf(ctx.key, s, token):
        return _forbidden()
    if not is_fresh(s, ctx.now()):
        back = safe_path(next_path or request.url.path)
        return RedirectResponse(f"/admin/login?next={quote(back, safe='/')}", status_code=303)
    return s


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
            resp = _html(pages.message_page("Error", "Something went wrong. Try again."), 500)
        for name, value in SECURITY_HEADERS.items():
            resp.headers[name] = value
        return resp

    return endpoint


# --- single use of login flows ------------------------------------------------------------


class _UsedStates:
    """States of login flows already consumed, so a replayed callback is refused even if the
    flow cookie comes back. Entries are dropped once the flow would have expired anyway."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def claim(self, state: str, now: float) -> bool:
        with self._lock:
            for s, until in list(self._seen.items()):
                if until <= now:
                    del self._seen[s]
            if state in self._seen:
                return False
            self._seen[state] = now + FLOW_SECONDS
            return True


# --- routes -------------------------------------------------------------------------------


def admin_routes(ctx: AdminContext) -> list[Route]:
    used_states = _UsedStates()
    signer = Signer(ctx.key)

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
            removed = [en for en in ctx.book.entries() if en.removed_at and en.alias not in linked]
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
        resp.set_cookie(
            FLOW_COOKIE,
            encode_flow(signer, flow),
            max_age=FLOW_SECONDS,
            path="/admin",
            secure=True,
            httponly=True,
            samesite="Lax",
        )
        return resp

    async def auth_callback(request: Request) -> Response:
        resp = await _finish_login(request)
        _clear_flow_cookie(resp)
        return resp

    async def _finish_login(request: Request) -> Response:
        now = ctx.now()
        flow = decode_flow(signer, request.cookies.get(FLOW_COOKIE), now)
        state = request.query_params.get("state") or ""
        if flow is None or flow.kind != "login":
            return _expired()
        if not hmac.compare_digest(flow.state.encode("ascii"), state.encode("utf-8")):
            return _expired()
        if not used_states.claim(flow.state, now):
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
        if email not in {a.lower() for a in ctx.allowed_emails}:
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
        s = _current_session(request, ctx)
        form = await request.form()
        if s is None or not same_origin(
            ctx.public_base_url, request.headers.get("origin"), request.headers.get("referer")
        ):
            return _forbidden()
        token = form.get("csrf")
        if not isinstance(token, str) or not check_csrf(ctx.key, s, token):
            return _forbidden()
        log.info("admin logout email=%s", s.email)
        resp = RedirectResponse("/admin", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/admin", secure=True, httponly=True, samesite="Strict")
        return resp

    def route(path: str, fn: Endpoint, methods: tuple[str, ...] = ("GET",)) -> Route:
        return Route(path, admin_endpoint(ctx, fn, methods), methods=_ALL_METHODS)

    return [
        route("/admin", status),
        route("/admin/style.css", style),
        route("/admin/login", login),
        route(LOGIN_CALLBACK_PATH, auth_callback),
        route("/admin/logout", logout, ("POST",)),
    ]


def _return_to(value: object) -> str:
    """Where a finished login goes: a safe /admin page that does not start another round trip."""
    path = safe_path(value)
    if path.split("?", 1)[0] in _NO_RETURN:
        return "/admin"
    return path


def _redirect_uri(ctx: AdminContext) -> str:
    return ctx.public_base_url.rstrip("/") + LOGIN_CALLBACK_PATH


async def _check_account(ctx: AdminContext, account: Any) -> pages.AccountRow:
    """One cheap provider call per account; a failing account never breaks the page."""
    state: pages.State = "ok"
    address: str | None = None
    try:
        address = await run_in_threadpool(lambda: account.provider.address())
    except AuthExpired:
        state = "credential expired"
    except Exception as exc:
        log.warning("admin status check failed alias=%s error=%s", account.alias, type(exc).__name__)
        state = "error"
    if state == "ok" and isinstance(address, str) and address:
        try:
            ctx.book.record_address(account.alias, account.provider_name, address)
        except Exception as exc:
            log.error("admin cannot record address alias=%s error=%s", account.alias, type(exc).__name__)
    else:
        address = None
        try:
            entry = ctx.book.get(account.alias)
            address = entry.address if entry else None
        except Exception as exc:
            log.error("admin cannot read the account book alias=%s error=%s", account.alias, type(exc).__name__)
    return pages.AccountRow(account.alias, account.provider_name, address, state)
