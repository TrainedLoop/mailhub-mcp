"""HTML for the admin page. No JavaScript; every interpolated value goes through html.escape."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Literal
from urllib.parse import quote

from mailhub_mcp.core.account_book import BookEntry

State = Literal["ok", "credential expired", "error"]

EXPIRED_TEXT = "This sign-in link expired, start again"
NOT_ALLOWED_TEXT = "This Google account is not allowed"
READ_ONLY_TEXT = "credentials are read-only: linking is disabled"
CONSENT_REFUSED_TEXT = "Google consent was refused. Nothing was changed."
PERMISSIONS_URL = "https://myaccount.google.com/permissions"

# `?msg=<code>` on /admin shows one of these fixed texts; unknown codes are ignored.
MESSAGES: dict[str, str] = {
    "expired": EXPIRED_TEXT,
    "signed-out": "Signed out.",
    "denied": "Google consent was refused.",
    "scope": "Gmail access was not granted: tick the Gmail permission on the consent screen.",
    "different-mailbox": "That is a different mailbox than the one on record for this alias.",
    "address-taken": "That mailbox is already linked under another alias.",
    "alias-invalid": "Invalid alias: use lowercase letters, digits and dashes, up to 32 characters.",
    "alias-taken": "That alias is already taken.",
    "alias-reserved": "That alias is reserved: choose another.",
    "alias-removed": "That alias is on record from an earlier link: re-link it from the status page.",
    "provider-invalid": "Unknown provider.",
    "unknown-alias": "That alias is neither linked nor on record.",
    "not-linked": "That alias is not linked.",
    "confirm-mismatch": "The alias you typed does not match: nothing was removed.",
    "read-only": READ_ONLY_TEXT,
    "revoke-failed": "The account was removed, but Google did not confirm the revocation.",
    "linked": "Mailbox linked.",
    "relinked": "Mailbox re-linked.",
    "removed": "Mailbox removed.",
}


@dataclass(frozen=True)
class AccountRow:
    alias: str
    provider: str
    address: str | None
    state: State


def e(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def _path_part(alias: str) -> str:
    return e(quote(alias, safe=""))


def layout(title: str, body: str, head: str = "") -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{head}"
        f"<title>{e(title)}</title>\n"
        '<link rel="stylesheet" href="/admin/style.css">\n'
        "</head>\n<body>\n<main>\n"
        f"{body}\n"
        "</main>\n</body>\n</html>\n"
    )


def _notice(text: str) -> str:
    return f'<p class="notice">{e(text)}</p>'


def message_notice(code: str | None) -> str:
    text = MESSAGES.get(code or "")
    return _notice(text) if text else ""


def sign_in_page(msg: str | None = None) -> str:
    body = (
        "<h1>mailhub-mcp admin</h1>\n"
        f"{message_notice(msg)}\n"
        '<p><a class="button" href="/admin/login">Sign in with Google</a></p>'
    )
    return layout("mailhub-mcp admin", body)


def message_page(title: str, text: str, link_text: str = "Back", to: str = "/admin") -> str:
    body = (
        f"<h1>{e(title)}</h1>\n"
        f"<p>{e(text)}</p>\n"
        f'<p><a href="{e(to)}">{e(link_text)}</a></p>'
    )
    return layout(title, body)


def done_html(message: str, to: str) -> str:
    """A page that moves on by itself. `to` must already be a validated /admin path."""
    head = f'<meta http-equiv="refresh" content="0;url={e(to)}">\n'
    body = f'<h1>{e(message)}</h1>\n<p><a href="{e(to)}">Continue</a></p>'
    return layout(message, body, head)


def _csrf_input(csrf: str) -> str:
    return f'<input type="hidden" name="csrf" value="{e(csrf)}">'


def _relink_form(alias: str, csrf: str) -> str:
    return (
        f'<form class="inline" method="post" action="/admin/accounts/{_path_part(alias)}/relink">'
        f'{_csrf_input(csrf)}<button type="submit">Re-link</button></form>'
    )


def _state_class(state: State) -> str:
    return {"ok": "state-ok", "credential expired": "state-expired"}.get(state, "state-error")


def status_page(
    *,
    email: str,
    csrf: str,
    rows: list[AccountRow],
    removed: list[BookEntry],
    writable: bool,
    has_auth_time: bool,
    book_unreadable: bool = False,
    msg: str | None = None,
) -> str:
    parts = [
        "<header>",
        "<h1>mailhub-mcp admin</h1>",
        (
            f'<form class="inline" method="post" action="/admin/logout">'
            f'<span class="muted">{e(email)}</span> {_csrf_input(csrf)}'
            '<button type="submit">Sign out</button></form>'
        ),
        "</header>",
        message_notice(msg),
    ]
    if not writable:
        parts.append(_notice(READ_ONLY_TEXT))
    if not has_auth_time:
        parts.append(_notice(
            "Google did not report when you last entered your password. Enable the session age "
            "claim in the Google Auth Platform console so re-authentication proves a recent sign-in."
        ))
    if book_unreadable:
        parts.append(_notice("The account book could not be read; see the server log."))

    parts.append("<h2>Mailboxes</h2>")
    if rows:
        parts.append('<div class="table-wrap"><table>')
        parts.append("<tr><th>Alias</th><th>Provider</th><th>Address</th><th>State</th>"
                     + ("<th></th>" if writable else "") + "</tr>")
        for row in rows:
            actions = ""
            if writable:
                actions = (
                    f"<td>{_relink_form(row.alias, csrf)} "
                    f'<a class="button" href="/admin/accounts/{_path_part(row.alias)}/remove">Remove</a></td>'
                )
            parts.append(
                f"<tr>\n<td>{e(row.alias)}</td><td>{e(row.provider)}</td>"
                f'<td>{e(row.address or "")}</td>'
                f'<td class="{_state_class(row.state)}">{e(row.state)}</td>{actions}</tr>'
            )
        parts.append("</table></div>")
    else:
        parts.append('<p class="muted">No mailboxes linked.</p>')
    if writable:
        parts.append('<p><a class="button" href="/admin/link">Link a mailbox</a></p>')

    if removed:
        parts.append("<h2>Removed</h2>")
        parts.append('<div class="table-wrap"><table>')
        parts.append("<tr><th>Alias</th><th>Address</th><th>Removed</th>"
                     + ("<th></th>" if writable else "") + "</tr>")
        for entry in removed:
            actions = f"<td>{_relink_form(entry.alias, csrf)}</td>" if writable else ""
            parts.append(
                f"<tr>\n<td>{e(entry.alias)}</td><td>{e(entry.address or '')}</td>"
                f"<td>{e(entry.removed_at)}</td>{actions}</tr>"
            )
        parts.append("</table></div>")
    return layout("mailhub-mcp admin", "\n".join(p for p in parts if p))


def link_page(*, csrf: str, providers: list[tuple[str, str]], msg: str | None = None) -> str:
    """The link form: alias and provider. `providers` holds (name, label) pairs."""
    options = "".join(f'<option value="{e(name)}">{e(label)}</option>' for name, label in providers)
    body = (
        "<h1>Link a mailbox</h1>\n"
        f"{message_notice(msg)}\n"
        '<form method="post" action="/admin/link">\n'
        f"{_csrf_input(csrf)}\n"
        '<p><label>Alias<br><input type="text" name="alias" required maxlength="32" '
        'autocapitalize="none" autocomplete="off" spellcheck="false"></label><br>'
        '<span class="muted">Lowercase letters, digits and dashes, up to 32 characters.</span></p>\n'
        f'<p><label>Provider<br><select name="provider">{options}</select></label></p>\n'
        '<p><button type="submit">Continue with Google</button> <a href="/admin">Cancel</a></p>\n'
        "</form>"
    )
    return layout("Link a mailbox", body)


def remove_page(*, alias: str, address: str | None, undoable: int, csrf: str, msg: str | None = None) -> str:
    """Confirmation before removing a mailbox."""
    action = f"/admin/accounts/{_path_part(alias)}/remove"
    shown = e(address) if address else '<span class="muted">unknown address</span>'
    body = (
        f"<h1>Remove {e(alias)}</h1>\n"
        f"{message_notice(msg)}\n"
        f"<p>Mailbox: {shown}</p>\n"
        f"<p>Changes in the journal that can no longer be undone while it is removed: "
        f"<strong>{e(undoable)}</strong>.</p>\n"
        "<p>Re-linking the same mailbox under the same alias makes them undoable again. "
        "The access token is revoked at Google and the credential file is deleted.</p>\n"
        f'<form method="post" action="{action}">\n'
        f"{_csrf_input(csrf)}\n"
        '<p><label>Type the alias to confirm<br><input type="text" name="confirm" required '
        'autocapitalize="none" autocomplete="off" spellcheck="false"></label></p>\n'
        '<p><button type="submit">Remove</button> <a href="/admin">Cancel</a></p>\n'
        "</form>"
    )
    return layout(f"Remove {alias}", body)


def revoke_failed_page(alias: str) -> str:
    """Removal done, but Google did not confirm the revocation: no auto-refresh, so the link stays."""
    body = (
        f"<h1>Removed {e(alias)}</h1>\n"
        f"<p>{e(MESSAGES['revoke-failed'])} Revoke the access by hand:</p>\n"
        f'<p><a href="{e(PERMISSIONS_URL)}">{e(PERMISSIONS_URL)}</a></p>\n'
        '<p><a href="/admin">Continue</a></p>'
    )
    return layout(f"Removed {alias}", body)
