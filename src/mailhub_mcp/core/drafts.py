"""Draft policy: replies only, addressed to people already in the thread."""

from email.utils import getaddresses

from mailhub_mcp.core.errors import MailhubError
from mailhub_mcp.core.models import Thread


class DraftRefused(MailhubError):
    pass


def _addresses(*headers: str) -> list[str]:
    # Parse each header on its own: an empty element makes getaddresses discard the whole batch.
    found = (addr.strip().lower() for h in headers if h.strip() for _, addr in getaddresses([h]))
    return [a for a in found if "@" in a]


def thread_participants(thread: Thread, own_address: str) -> set[str]:
    """Every address in From, To, Cc and Reply-To of the thread, plus the account's own."""
    people = set(_addresses(own_address))
    for m in thread.messages:
        people.update(_addresses(m.from_, m.to, m.cc, m.reply_to))
    return people


def _allowed_text(allowed: set[str]) -> str:
    return ", ".join(sorted(allowed))


def _check(field: str, value: str, allowed: set[str]) -> list[str]:
    addrs = _addresses(value)
    if not addrs:
        raise DraftRefused(f"'{field}' has no valid email address; allowed addresses: {_allowed_text(allowed)}")
    outsiders = [a for a in addrs if a not in allowed]
    if outsiders:
        raise DraftRefused(
            f"drafts can only be addressed to people already in the thread; not allowed in '{field}': "
            f"{', '.join(outsiders)}. Allowed addresses: {_allowed_text(allowed)}"
        )
    return list(dict.fromkeys(addrs))


def resolve_recipients(
    thread: Thread, reply_to_message_id: str, own_address: str, to: str | None, cc: str | None
) -> tuple[str, str | None]:
    """Validate to/cc against the thread and return normalized (to, cc).

    Without `to`, the reply goes to the Reply-To of the replied message, else its From.
    """
    replied = next((m for m in thread.messages if m.id == reply_to_message_id), None)
    if replied is None:
        raise DraftRefused(f"message {reply_to_message_id} is not in the thread being replied to")
    allowed = thread_participants(thread, own_address)
    if to and to.strip():
        to_list = _check("to", to, allowed)
    else:
        default = _addresses(replied.reply_to) or _addresses(replied.from_)
        if not default:
            raise DraftRefused(
                f"the replied message has no Reply-To or From address; pass 'to'. Allowed addresses: {_allowed_text(allowed)}"
            )
        to_list = list(dict.fromkeys(default))
    cc_list = _check("cc", cc, allowed) if cc and cc.strip() else None
    return ", ".join(to_list), (", ".join(cc_list) if cc_list else None)
