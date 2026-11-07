"""The /admin attachment download route: GET /admin/files/<token>.

`routes.admin_routes(ctx)` is the single entry point; it adds this route through `file_routes`.
The token (see attachment_links.py) names the mailbox, message and part; the owner session decides
who may use it. A link is usually clicked from another site, so the SameSite=Strict session cookie
is missing on the first request: the route answers with a same-site refresh page to the same URL
plus `b=1`, and only that second request, still without a session, goes through Google sign-in.

The token is in the path, so this route never lets a request path reach the log: every failure is
caught here and logged by alias and message id.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from mailhub_mcp.admin_web import pages
from mailhub_mcp.admin_web.attachment_links import (
    FILES_PATH,
    LinkTarget,
    content_disposition,
)
from mailhub_mcp.admin_web.context import AdminContext
from mailhub_mcp.admin_web.routes import (
    _current_session,
    _error_page,
    _html,
    admin_io,
    done_page,
)
from mailhub_mcp.core.attachments import download_block_reason
from mailhub_mcp.core.content import message_suspicion
from mailhub_mcp.core.errors import AccountNotFound, MessageNotFound

log = logging.getLogger("mailhub-mcp")

RouteFactory = Callable[..., Route]
BOUNCED = "b"  # query marker: this request already went through the same-site refresh page


def file_routes(ctx: AdminContext, route: RouteFactory) -> list[Route]:
    async def download(request: Request) -> Response:
        target = ctx.links.decode(request.path_params["token"])
        if target == "expired":
            return _html(pages.message_page("Link expired", pages.LINK_EXPIRED_TEXT), 410)
        if target is None:
            return _html(pages.message_page("Not found", pages.LINK_INVALID_TEXT), 404)

        path = request.url.path
        s = _current_session(request, ctx)
        if s is None:
            if request.query_params.get(BOUNCED) != "1":
                return done_page(pages.OPENING_DOWNLOAD_TEXT, f"{path}?{BOUNCED}=1")
            return RedirectResponse(f"/admin/login?next={quote(path, safe='/')}", status_code=303)

        try:
            return await _send(ctx, s.email, target)
        except Exception as exc:  # noqa: BLE001 - the admin wrapper would log the path, and with it the token
            log.error(
                "admin download failed email=%s alias=%s message=%s error=%s",
                s.email, target.alias, target.message_id, type(exc).__name__,
            )
            return _error_page()

    return [route(FILES_PATH + "{token}", download)]


async def _send(ctx: AdminContext, email: str, target: LinkTarget) -> Response:
    def refused(reason: str, file: str = "") -> None:
        log.info(
            "admin download refused email=%s alias=%s message=%s file=%r reason=%s",
            email, target.alias, target.message_id, file, reason,
        )

    try:
        account = ctx.registry.get(target.alias)
    except AccountNotFound:
        refused("mailbox-gone")
        return _html(pages.message_page("Not found", pages.MAILBOX_GONE_TEXT), 404)
    try:
        dl = await admin_io(account.provider.get_attachment, target.message_id, target.part_id)
    except MessageNotFound:
        refused("not-found")
        return _html(pages.message_page("Not found", pages.ATTACHMENT_GONE_TEXT), 404)

    filename = dl.attachment.filename
    reason = download_block_reason(
        dl.message.spam, bool(message_suspicion(dl.message, ctx.tool_names)), filename
    )
    if reason is not None:
        refused(reason, filename)
        return _html(pages.message_page("Download refused", pages.DOWNLOAD_BLOCKED_TEXT[reason]), 403)

    log.info(
        "admin download email=%s alias=%s message=%s file=%r size=%d",
        email, target.alias, target.message_id, filename, len(dl.data),
    )
    return Response(
        dl.data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": content_disposition(filename), "X-Content-Type-Options": "nosniff"},
    )
