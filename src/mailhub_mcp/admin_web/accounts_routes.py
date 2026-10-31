"""The /admin account routes: linking, re-linking and removing mailboxes.

`routes.admin_routes(ctx)` is the single entry point; it adds these routes through `account_routes`.
Linking is a Google OAuth round trip whose callback works only from the SameSite=Lax flow cookie
(see routes.py for why).
"""

from __future__ import annotations

import hmac
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from mailhub_mcp.admin_web import pages
from mailhub_mcp.admin_web.context import AdminContext
from mailhub_mcp.admin_web.credentials import remove_credential, write_credential
from mailhub_mcp.admin_web.routes import (
    LINK_CALLBACK_PATH,
    Endpoint,
    _alias_rule_problem,
    _back_to,
    _clearing_flow_cookie,
    _html,
    _link_redirect_uri,
    _normalize_alias,
    _recorded_provider,
    _set_flow_cookie,
    _UsedStates,
    done_page,
    require_action,
    require_session,
)
from mailhub_mcp.admin_web.session import (
    FLOW_COOKIE,
    Flow,
    Session,
    Signer,
    csrf_token,
    decode_flow,
    new_pkce,
    random_token,
)
from mailhub_mcp.core.errors import AccountNotFound
from mailhub_mcp.providers.base import LinkError

log = logging.getLogger("mailhub-mcp")

RouteFactory = Callable[..., Route]


def account_routes(
    ctx: AdminContext, route: RouteFactory, get_or_post: Callable[[Endpoint, Endpoint], Endpoint]
) -> list[Route]:
    """The link, re-link and remove routes, built with `routes.admin_routes`'s own route helpers."""
    # Its own store, separate from the login one: anonymous sign-in attempts never churn it.
    link_states = _UsedStates()
    signer = Signer(ctx.key)
    mutations = threading.Lock()  # one link, re-link or removal at a time: check and write together

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
            ctx.book.record_removed(alias, datetime.fromtimestamp(now, UTC), provider=provider)
            entry = ctx.book.get(alias)
            address = entry.address if entry and entry.address else "unknown"
            log.info("admin remove alias=%s provider=%s address=%s by=%s", alias, provider, address, owner)
            if not revoked:
                log.warning("admin remove: revocation not confirmed alias=%s", alias)
            return revoked

    return [
        route("/admin/link", get_or_post(link_form, link_start), ("GET", "POST")),
        route("/admin/accounts/{alias}/relink", relink_start, ("POST",)),
        route("/admin/accounts/{alias}/remove", get_or_post(remove_form, remove), ("GET", "POST")),
        route(LINK_CALLBACK_PATH, link_callback),
    ]
