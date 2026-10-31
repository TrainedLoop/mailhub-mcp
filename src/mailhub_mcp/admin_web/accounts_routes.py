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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from mailhub_mcp.admin_web import pages
from mailhub_mcp.admin_web.context import AdminContext
from mailhub_mcp.admin_web.credentials import credential_path, remove_credential, write_credential
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
    admin_io,
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


@dataclass(frozen=True)
class _LinkOutcome:
    refusal: str | None = None  # a check refused it; nothing was written
    failed: bool = False  # a write failed; the earlier writes were undone
    rollback_failed: bool = False  # a write failed and undoing the earlier ones failed too
    first_address: bool = False  # a re-link that recorded the address for the first time


@dataclass(frozen=True)
class _RemoveOutcome:
    revoked: bool
    recorded: bool


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
            csrf=csrf_token(ctx.key, s), providers=providers, msg=request.query_params.get("msg"),
            email=s.email,
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

    def _refused_after_grant(text: str) -> Response:
        """Refused after Google granted access: the grant stays listed at Google, so say where to drop it."""
        return _html(pages.refused_after_grant_page("Not linked", text), 400)

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
            result = await admin_io(
                ctx.linkers[provider].complete, code, _link_redirect_uri(ctx), flow.code_verifier
            )
        except LinkError as exc:
            return _link_refused(str(exc))
        address = result.address.strip().lower() if isinstance(result.address, str) else ""
        if not address:
            return _refused_after_grant("Google did not report the mailbox address. Nothing was changed.")
        outcome = await run_in_threadpool(
            _commit_link, flow.kind, alias, provider, address, result.credential_json, owner, now
        )
        if outcome.rollback_failed:
            return _html(pages.refused_after_grant_page("Not linked", pages.LINK_UNDO_INCOMPLETE_TEXT), 500)
        if outcome.failed:
            return _html(pages.refused_after_grant_page("Not linked", pages.LINK_FAILED_TEXT), 500)
        if outcome.refusal is not None:
            return _refused_after_grant(outcome.refusal)
        note = pages.ADDRESS_RECORDED_TEXT if outcome.first_address else None
        return done_page(f"Linked {alias} ({address})", note=note)

    def _commit_link(
        kind: str, alias: str, provider: str, address: str, credential_json: str, owner: str, now: float
    ) -> _LinkOutcome:
        """Every check, then the writes, under one lock. Nothing touches the disk before every check
        has passed; if a write fails, the earlier ones are undone (see _write_link)."""
        with mutations:
            other = ctx.book.alias_for_address(address)
            if other is not None and other != alias:
                log.warning("admin %s refused: address linked as %s alias=%s address=%s by=%s",
                            kind, other, alias, address, owner)
                return _LinkOutcome(refusal=pages.MESSAGES["address-taken"])
            entry = ctx.book.get(alias)
            linked = alias in ctx.registry.aliases()
            recorded = entry.address.lower() if entry is not None and entry.address else None
            if kind == "link":
                if linked or entry is not None:
                    return _LinkOutcome(refusal=pages.MESSAGES["alias-taken"])
            else:
                if not linked and entry is None:
                    return _LinkOutcome(refusal=pages.MESSAGES["unknown-alias"])
                if recorded is not None and recorded != address:
                    log.warning("admin relink refused: different mailbox alias=%s address=%s by=%s",
                                alias, address, owner)
                    return _LinkOutcome(refusal=f"That is a different mailbox than {alias} (expected {recorded})")
                if recorded is None:
                    log.warning("admin relink without recorded address alias=%s provider=%s address=%s by=%s",
                                alias, provider, address, owner)
            written = _write_link(kind, alias, provider, address, credential_json, now)
            if written != "ok":
                return _LinkOutcome(failed=True, rollback_failed=written == "rollback-failed")
            log.info("admin %s alias=%s provider=%s address=%s by=%s", kind, alias, provider, address, owner)
            return _LinkOutcome(first_address=kind == "relink" and recorded is None)

    def _write_link(
        kind: str, alias: str, provider: str, address: str, credential_json: str, now: float
    ) -> Literal["ok", "rolled-back", "rollback-failed"]:
        """Credential file, registry, book, in that order. If a step fails, undo the earlier ones
        (the previous file byte for byte, or no file; the previous registry entry, or none):
        "rolled-back" if that worked, "rollback-failed" if the undo raised too. The book write is
        atomic, so a failed one leaves the book as it was. Caller holds the lock."""
        target = credential_path(ctx.accounts_dir, provider, alias)
        try:
            previous_file: bytes | None = target.read_bytes()
        except FileNotFoundError:
            previous_file = None
        try:
            previous_account = ctx.registry.get(alias)
        except AccountNotFound:
            previous_account = None
        step = "credential"
        try:
            path = write_credential(ctx.accounts_dir, provider, alias, credential_json)
            step = "registry"
            ctx.registry.upsert(alias, provider, path)
            step = "book"
            ctx.book.record_linked(alias, provider, address, datetime.fromtimestamp(now, UTC))
            return "ok"
        except Exception as exc:
            log.error("admin link failed alias=%s step=%s kind=%s error=%s", alias, step, kind, type(exc).__name__)
        try:
            if step != "credential":
                if previous_file is None:
                    remove_credential(ctx.accounts_dir, provider, alias)
                else:
                    write_credential(ctx.accounts_dir, provider, alias, previous_file)
            if previous_account is not None:
                ctx.registry.upsert(alias, previous_account.provider_name, previous_account.credential_path)
            elif alias in ctx.registry.aliases():
                ctx.registry.remove(alias)
        except Exception as exc:
            log.error("admin link rollback failed alias=%s error=%s", alias, type(exc).__name__)
            return "rollback-failed"
        return "rolled-back"

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
            email=s.email,
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
        outcome = await admin_io(_commit_remove, alias, act.session.email, ctx.now())  # it revokes
        if outcome is None:
            return _back_to("/admin", "not-linked")
        if not outcome.recorded:
            return _html(pages.remove_unrecorded_page(alias, revoked=outcome.revoked), 500)
        if not outcome.revoked:
            return _html(pages.revoke_failed_page(alias))
        return done_page(f"Removed {alias}")

    def _commit_remove(alias: str, owner: str, now: float) -> _RemoveOutcome | None:
        """Revoke (it needs the refresh token in the file), delete the file, drop the alias from the
        registry, mark it removed in the book. None if the alias is not linked any more; otherwise
        whether Google confirmed the revocation (a failed revocation does not stop the removal) and
        whether the removal was recorded in the book."""
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
            try:
                ctx.book.record_removed(alias, datetime.fromtimestamp(now, UTC), provider=provider)
            except Exception as exc:
                # the file is gone already: say so rather than pretend nothing happened
                log.error("admin remove: removed but not recorded alias=%s provider=%s error=%s",
                          alias, provider, type(exc).__name__)
                return _RemoveOutcome(revoked=revoked, recorded=False)
            entry = ctx.book.get(alias)
            address = entry.address if entry and entry.address else "unknown"
            log.info("admin remove alias=%s provider=%s address=%s by=%s", alias, provider, address, owner)
            if not revoked:
                log.warning("admin remove: revocation not confirmed alias=%s", alias)
            return _RemoveOutcome(revoked=revoked, recorded=True)

    return [
        route("/admin/link", get_or_post(link_form, link_start), ("GET", "POST")),
        route("/admin/accounts/{alias}/relink", relink_start, ("POST",)),
        route("/admin/accounts/{alias}/remove", get_or_post(remove_form, remove), ("GET", "POST")),
        route(LINK_CALLBACK_PATH, link_callback),
    ]
