"""Out-of-band administration: mailhub-admin preferences list|show|approve|reject, and mailhub-admin accounts.

This is the only way to approve or reject a preference proposal. It is run by the user inside the
container, reads only JOURNAL_PATH (preferences live next to the journal) and needs no OAuth settings.
`accounts` checks that every linked mailbox still answers (run it from cron to notice expired credentials).
"""

import argparse
import os
import sys
from pathlib import Path

from mailhub_mcp.core.accounts import AccountRegistry
from mailhub_mcp.core.content import clean_text
from mailhub_mcp.core.errors import MailhubError
from mailhub_mcp.core.journal import Journal
from mailhub_mcp.core import preferences
from mailhub_mcp.core.preferences import PreferenceStore

DEFAULT_JOURNAL = "/data/changes.jsonl"
DEFAULT_ACCOUNTS_DIR = "/secrets/accounts"


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(2)


def _safe(value) -> str:
    """Plain, single-line text with no terminal control characters."""
    return " ".join(clean_text(str(value)).split())


def _summary(p: dict) -> str:
    values = ", ".join(_safe(v) for v in p["values"])
    return f"{_safe(p['proposal_id'])}  {_safe(p['scope'])}  {_safe(p['op'])} {_safe(p['field'])}: {values}"


def _show(p: dict) -> str:
    undo = [f"Undo of:      {_safe(p['undo_of'])}"] if p.get("undo_of") else []
    return "\n".join([
        f"Proposal:     {_safe(p['proposal_id'])}",
        *undo,
        f"Scope:        {_safe(p['scope'])}",
        f"Field:        {_safe(p['field'])}",
        f"Operation:    {_safe(p['op'])}",
        f"Values:       {', '.join(_safe(v) for v in p['values'])}",
        f"User request: {_safe(p.get('user_request') or '')}",
        f"Created:      {_safe(p['created'])}",
        f"Expires:      {_safe(p['expires'])}",
    ])


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="mailhub-admin", description=__doc__)
    groups = parser.add_subparsers(dest="group", required=True)
    prefs = groups.add_parser("preferences", help="review preference proposals")
    actions = prefs.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="list pending proposals")
    for name in ("show", "approve", "reject"):
        actions.add_parser(name, help=f"{name} a proposal").add_argument("proposal_id")
    groups.add_parser("accounts", help="check that every linked account still answers")
    return parser


def _check_accounts(providers=None) -> int:
    """Print `alias  provider  ok  <address>` or `alias  provider  ERROR  <message>` per account.
    Exit 0 when all are ok, 1 when any failed, 2 when the accounts cannot be loaded."""
    accounts_dir = Path(os.environ.get("ACCOUNTS_DIR", DEFAULT_ACCOUNTS_DIR))
    if providers is None:
        from mailhub_mcp.providers import PROVIDERS as providers  # noqa: N811
    try:
        if not accounts_dir.is_dir():
            print(f"error: accounts directory not found: {_safe(accounts_dir)}", file=sys.stderr)
            return 2
        registry = AccountRegistry(accounts_dir, providers)
    except MailhubError as exc:
        print(f"error: {_safe(exc)}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot read the accounts directory {_safe(accounts_dir)}: {_safe(exc)}", file=sys.stderr)
        return 2
    accounts = registry.all()
    if not accounts:
        print(f"error: no accounts found in {_safe(accounts_dir)}", file=sys.stderr)
        return 2
    failed = 0
    for account in accounts:
        prefix = f"{_safe(account.alias)}  {_safe(account.provider_name)}"
        try:
            print(f"{prefix}  ok  {_safe(account.provider.address())}")
        except Exception as exc:
            failed += 1
            print(f"{prefix}  ERROR  {_safe(exc) or type(exc).__name__}")
    return 1 if failed else 0


def main(argv: list[str] | None = None, providers=None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else 2
    if args.group == "accounts":
        return _check_accounts(providers)
    journal_path = Path(os.environ.get("JOURNAL_PATH", DEFAULT_JOURNAL))
    store = PreferenceStore(journal_path.parent / "preferences.json")
    try:
        if args.action == "list":
            items = preferences.list_proposals(store)
            print("\n".join(_summary(p) for p in items) if items else "No pending proposals.")
        elif args.action == "show":
            match = [p for p in preferences.list_proposals(store) if p["proposal_id"] == args.proposal_id]
            if not match:
                raise preferences.PreferenceError(f"unknown or expired proposal: {_safe(args.proposal_id)}")
            print(_show(match[0]))
        elif args.action == "approve":
            res = preferences.approve(store, Journal(journal_path), args.proposal_id)
            print(f"Approved {_safe(res['proposal_id'])}.")
            for a in res["applied"]:
                print(f"  {_safe(a['op'])} {_safe(a['field'])} ({_safe(a['scope'])}): "
                      + ", ".join(_safe(v) for v in a["values"]))
            if not res["applied"]:
                print("  Nothing to change: already in the requested state.")
            if res["change_id"]:
                print(f"  change_id: {_safe(res['change_id'])}")
        else:
            res = preferences.reject(store, args.proposal_id)
            print(f"Proposal {_safe(res['proposal_id'])} rejected and removed.")
            if res.get("undo_of"):
                print(f"  The undo of {_safe(res['undo_of'])} will not be proposed again.")
    except MailhubError as exc:
        print(f"error: {_safe(exc)}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot access preferences or journal files: {_safe(exc)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
