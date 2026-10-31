"""HTML for the admin page. No JavaScript; every interpolated value goes through html.escape."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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
GRANT_HINT_TEXT = "Google still lists mailhub as allowed for that account; you can remove it at"
NO_ADDRESS_NOTE = "No address on record: this re-link will accept any mailbox once."
ADDRESS_RECORDED_TEXT = "Its address is now recorded: later re-links must use this same mailbox."
LINK_FAILED_TEXT = "Saving the link failed, so it was undone. Nothing was changed."

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
    linked_at: str | None = None


# (class, text) per state: the text is always shown, the colour only backs it up
_BADGES: dict[str, str] = {"ok": "badge-ok", "credential expired": "badge-warn", "error": "badge-error"}


def e(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def _path_part(alias: str) -> str:
    return e(quote(alias, safe=""))


def _date(iso: str | None) -> str:
    """`<time>` with the ISO value as its datetime and a readable UTC date ("1 Oct 2026") as text."""
    if not iso:
        return ""
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return e(iso)
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return f'<time datetime="{e(iso)}">{moment.day} {moment:%b %Y}</time>'


def _csrf_input(csrf: str) -> str:
    return f'<input type="hidden" name="csrf" value="{e(csrf)}">'


def _masthead(account: tuple[str, str] | None) -> str:
    """Product name, and for a signed-in page the owner's email with Sign out. `account` is (email, csrf)."""
    who = ""
    if account is not None:
        email, csrf = account
        who = (
            '<form class="who" method="post" action="/admin/logout">'
            f'<span class="who-email">{e(email)}</span>{_csrf_input(csrf)}'
            '<button class="btn btn-quiet btn-small" type="submit">Sign out</button></form>'
        )
    return (
        '<header class="masthead"><div class="masthead-inner">'
        '<a class="brand" href="/admin">mailhub-mcp <span class="brand-tag">admin</span></a>'
        f"{who}</div></header>"
    )


def layout(title: str, body: str, head: str = "", account: tuple[str, str] | None = None,
           narrow: bool = False) -> str:
    main_class = "page page-narrow" if narrow else "page"
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"{head}"
        f"<title>{e(title)}</title>\n"
        '<link rel="stylesheet" href="/admin/style.css">\n'
        "</head>\n<body>\n"
        '<div class="airmail" aria-hidden="true"></div>\n'
        f"{_masthead(account)}\n"
        f'<main class="{main_class}">\n'
        f"{body}\n"
        "</main>\n</body>\n</html>\n"
    )


def _notice(text: str, kind: str = "info") -> str:
    return f'<p class="notice notice-{kind}">{e(text)}</p>'


def message_notice(code: str | None) -> str:
    text = MESSAGES.get(code or "")
    if not text:
        return ""
    good = code in ("linked", "relinked", "removed", "signed-out")
    return _notice(text, "ok" if good else "warn")


def _sheet(inner: str, kind: str = "") -> str:
    return f'<section class="sheet{" sheet-" + kind if kind else ""}">\n{inner}\n</section>'


def sign_in_page(msg: str | None = None) -> str:
    body = _sheet(
        "<h1>Sign in</h1>\n"
        f"{message_notice(msg)}\n"
        '<p class="lede">Link, re-link and remove the mailboxes this server reads. '
        "Only allowed Google accounts can sign in.</p>\n"
        '<p class="actions"><a class="btn btn-primary" href="/admin/login">Sign in with Google</a></p>'
    )
    return layout("mailhub-mcp admin", body, narrow=True)


def message_page(title: str, text: str, link_text: str = "Back", to: str = "/admin") -> str:
    body = _sheet(
        f"<h1>{e(title)}</h1>\n"
        f"<p>{e(text)}</p>\n"
        f'<p class="actions"><a class="btn btn-secondary" href="{e(to)}">{e(link_text)}</a></p>'
    )
    return layout(title, body, narrow=True)


def done_html(message: str, to: str, note: str | None = None) -> str:
    """A page that moves on by itself; with a `note` it stays so the note can be read.
    `to` must already be a validated /admin path."""
    head = "" if note else f'<meta http-equiv="refresh" content="0;url={e(to)}">\n'
    shown = f"<p>{e(note)}</p>\n" if note else ""
    body = _sheet(
        f"<h1>{e(message)}</h1>\n{shown}"
        f'<p class="actions"><a class="btn btn-primary" href="{e(to)}">Continue</a></p>'
    )
    return layout(message, body, head, narrow=True)


def _grant_hint() -> str:
    return f'<p>{e(GRANT_HINT_TEXT)} <a href="{e(PERMISSIONS_URL)}">{e(PERMISSIONS_URL)}</a>.</p>'


def refused_after_grant_page(title: str, text: str) -> str:
    """A refusal after Google already granted access: the grant stays listed at Google."""
    body = _sheet(
        f"<h1>{e(title)}</h1>\n<p>{e(text)}</p>\n{_grant_hint()}\n"
        '<p class="actions"><a class="btn btn-secondary" href="/admin">Back</a></p>'
    )
    return layout(title, body, narrow=True)


def _revoke_by_hand() -> str:
    return (
        f"<p>{e(MESSAGES['revoke-failed'])} Revoke the access by hand:</p>\n"
        f'<p><a href="{e(PERMISSIONS_URL)}">{e(PERMISSIONS_URL)}</a></p>\n'
    )


def remove_unrecorded_page(alias: str, *, revoked: bool) -> str:
    """The mailbox is gone but the book did not take the removal: no auto-refresh, the owner must know."""
    body = _sheet(
        f"<h1>Removed {e(alias)}</h1>\n"
        '<p class="notice notice-error">The mailbox was removed, but its record could not be saved: the alias '
        "no longer remembers its address, so a later re-link will not be checked against it. "
        "See the server log.</p>\n"
        f"{'' if revoked else _revoke_by_hand()}"
        '<p class="actions"><a class="btn btn-primary" href="/admin">Continue</a></p>'
    )
    return layout(f"Removed {alias}", body, narrow=True)


def revoke_failed_page(alias: str) -> str:
    """Removal done, but Google did not confirm the revocation: no auto-refresh, so the link stays."""
    body = _sheet(
        f"<h1>Removed {e(alias)}</h1>\n"
        f"{_revoke_by_hand()}"
        '<p class="actions"><a class="btn btn-primary" href="/admin">Continue</a></p>'
    )
    return layout(f"Removed {alias}", body, narrow=True)


def _relink_form(alias: str, csrf: str) -> str:
    return (
        f'<form method="post" action="/admin/accounts/{_path_part(alias)}/relink">'
        f'{_csrf_input(csrf)}<button class="btn btn-secondary btn-small" type="submit">Re-link</button></form>'
    )


def _badge(state: State) -> str:
    return f'<span class="badge {_BADGES.get(state, "badge-error")}">{e(state)}</span>'


def _breakable(address: str) -> str:
    """The escaped address with break opportunities after "@" and before each ".", so a long one
    wraps at those points and never inside a word."""
    return e(address).replace("@", "@<wbr>").replace(".", "<wbr>.")


def _address_cell(address: str | None, writable: bool) -> str:
    """The address, or "not on record" and, where re-link is offered, what that re-link will accept."""
    if not address:
        note = f'<span class="row-note">{e(NO_ADDRESS_NOTE)}</span>' if writable else ""
        return f'<td class="addr" data-label="Address"><span class="muted">not on record</span>{note}</td>'
    return f'<td class="addr" data-label="Address"><span class="addr-text">{_breakable(address)}</span></td>'


def _section_head(title: str, count: int, action: str = "") -> str:
    return (
        f'<div class="section-head"><h2>{e(title)} <span class="count">{count}</span></h2>{action}</div>'
    )


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
    provider_labels: dict[str, str] | None = None,
) -> str:
    labels = provider_labels or {}
    parts = [message_notice(msg)]
    if not writable:
        parts.append(_notice(READ_ONLY_TEXT, "warn"))
    if not has_auth_time:
        parts.append(_notice(
            "Google did not report when you last entered your password. Enable the session age "
            "claim in the Google Auth Platform console so re-authentication proves a recent sign-in."
        ))
    if book_unreadable:
        parts.append(_notice("The account book could not be read; see the server log.", "error"))

    link = '<a class="btn btn-primary" href="/admin/link">Link a mailbox</a>' if writable else ""
    parts.append('<section class="block">')
    parts.append(_section_head("Mailboxes", len(rows), link))
    if rows:
        parts.append('<table class="boxes">')
        parts.append('<thead><tr><th scope="col">Alias</th><th scope="col">Provider</th>'
                     '<th scope="col">Address</th><th scope="col">State</th>'
                     + ('<th scope="col"><span class="sr">Actions</span></th>' if writable else "")
                     + "</tr></thead><tbody>")
        for row in rows:
            since = f'<span class="since">since {_date(row.linked_at)}</span>' if row.linked_at else ""
            actions = ""
            if writable:
                actions = (
                    '<td class="act"><div class="act-row">'
                    f"{_relink_form(row.alias, csrf)}"
                    f'<a class="btn btn-danger-quiet btn-small" '
                    f'href="/admin/accounts/{_path_part(row.alias)}/remove">Remove</a></div></td>'
                )
            parts.append(
                f'<tr>\n<th scope="row">{e(row.alias)}{since}</th>'
                f'<td data-label="Provider">{e(labels.get(row.provider, row.provider))}</td>'
                f"{_address_cell(row.address, writable)}"
                f'<td class="state" data-label="State">{_badge(row.state)}</td>{actions}</tr>'
            )
        parts.append("</tbody></table>")
    else:
        parts.append('<p class="empty">No mailboxes linked yet.'
                     + (" Link one to let the server read it." if writable else "") + "</p>")
    parts.append("</section>")

    if removed:
        parts.append('<section class="block block-quiet">')
        parts.append(_section_head("Removed", len(removed)))
        parts.append('<p class="section-note">Kept on record so a re-link can only attach the same mailbox.</p>')
        parts.append('<table class="boxes boxes-removed">')
        parts.append('<thead><tr><th scope="col">Alias</th><th scope="col">Address</th>'
                     '<th scope="col">Removed</th>'
                     + ('<th scope="col"><span class="sr">Actions</span></th>' if writable else "")
                     + "</tr></thead><tbody>")
        for entry in removed:
            actions = (
                f'<td class="act"><div class="act-row">{_relink_form(entry.alias, csrf)}</div></td>'
            ) if writable else ""
            parts.append(
                f'<tr>\n<th scope="row">{e(entry.alias)}</th>'
                f"{_address_cell(entry.address, writable)}"
                f'<td data-label="Removed">{_date(entry.removed_at) or "unknown"}</td>{actions}</tr>'
            )
        parts.append("</tbody></table></section>")
    return layout("mailhub-mcp admin", "\n".join(p for p in parts if p), account=(email, csrf))


def link_page(*, csrf: str, providers: list[tuple[str, str]], msg: str | None = None,
              email: str | None = None) -> str:
    """The link form: alias and provider. `providers` holds (name, label) pairs."""
    options = "".join(f'<option value="{e(name)}">{e(label)}</option>' for name, label in providers)
    body = _sheet(
        "<h1>Link a mailbox</h1>\n"
        f"{message_notice(msg)}\n"
        '<p class="lede">Choose the name tools will use for this mailbox, then sign in to it at Google.</p>\n'
        '<form class="form" method="post" action="/admin/link">\n'
        f"{_csrf_input(csrf)}\n"
        '<div class="field"><label for="alias">Alias</label>'
        '<input id="alias" type="text" name="alias" required maxlength="32" aria-describedby="alias-hint" '
        'autocapitalize="none" autocomplete="off" spellcheck="false">'
        '<p class="hint" id="alias-hint">Lowercase letters, digits and dashes, up to 32 characters.</p></div>\n'
        f'<div class="field"><label for="provider">Provider</label>'
        f'<select id="provider" name="provider">{options}</select></div>\n'
        '<div class="actions"><button class="btn btn-primary" type="submit">Continue with Google</button>'
        '<a class="btn btn-quiet" href="/admin">Cancel</a></div>\n'
        "</form>"
    )
    account = (email, csrf) if email else None
    return layout("Link a mailbox", body, account=account, narrow=True)


def remove_page(*, alias: str, address: str | None, undoable: int, csrf: str, msg: str | None = None,
                email: str | None = None) -> str:
    """Confirmation before removing a mailbox."""
    action = f"/admin/accounts/{_path_part(alias)}/remove"
    shown = e(address) if address else '<span class="muted">unknown address</span>'
    body = _sheet(
        f"<h1>Remove {e(alias)}</h1>\n"
        f"{message_notice(msg)}\n"
        '<dl class="facts">'
        f"<div><dt>Mailbox</dt><dd>{shown}</dd></div>"
        f"<div><dt>Changes you cannot undo while it is removed</dt>"
        f"<dd><strong>{e(undoable)}</strong></dd></div>"
        "</dl>\n"
        "<p>The access token is revoked at Google and the credential file is deleted. "
        "Re-linking the same mailbox under the same alias makes those changes undoable again.</p>\n"
        f'<form class="form" method="post" action="{action}">\n'
        f"{_csrf_input(csrf)}\n"
        f'<div class="field"><label for="confirm">Type <strong>{e(alias)}</strong> to confirm</label>'
        '<input id="confirm" type="text" name="confirm" required '
        'autocapitalize="none" autocomplete="off" spellcheck="false"></div>\n'
        '<div class="actions"><button class="btn btn-danger" type="submit">Remove</button>'
        '<a class="btn btn-quiet" href="/admin">Cancel</a></div>\n'
        "</form>",
        "danger",
    )
    account = (email, csrf) if email else None
    return layout(f"Remove {alias}", body, account=account, narrow=True)
