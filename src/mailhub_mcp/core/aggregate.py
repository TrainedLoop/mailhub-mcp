"""Run one provider call across several accounts, isolating per-account failures."""

from collections.abc import Callable
from typing import Any

import anyio
from anyio import to_thread

from mailhub_mcp.core.accounts import Account, AccountRegistry
from mailhub_mcp.core.errors import MailhubError, RateLimited
from mailhub_mcp.core.models import with_account


def resolve(registry: AccountRegistry, aliases: list[str] | None) -> list[Account]:
    if not aliases:
        return registry.all()
    return [registry.get(alias) for alias in dict.fromkeys(aliases)]


def describe_error(exc: Exception, alias: str) -> str:
    if isinstance(exc, MailhubError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


async def fan_out(accounts: list[Account], fn: Callable[[Account], list]) -> dict:
    results: dict[str, list] = {}
    errors: dict[str, str] = {}

    async def worker(account: Account) -> None:
        try:
            results[account.alias] = await to_thread.run_sync(fn, account)
        except Exception as exc:
            errors[account.alias] = describe_error(exc, account.alias)

    async with anyio.create_task_group() as tg:
        for account in accounts:
            tg.start_soon(worker, account)

    merged = [
        # dict items (list_accounts rows) are plain dicts, not models, so stamp the key directly
        {**item, "account": account.alias} if isinstance(item, dict) else with_account(item, account.alias)
        for account in accounts
        for item in results.get(account.alias, [])
    ]
    if any(hasattr(item, "internal_date") for item in merged):
        merged.sort(key=lambda item: getattr(item, "internal_date", 0), reverse=True)
    return {"results": merged, "errors": errors}


async def run_one(account: Account, fn: Callable[[Account], Any]) -> Any:
    try:
        return await to_thread.run_sync(fn, account)
    except RateLimited as exc:  # already names the account
        raise MailhubError(str(exc)) from exc
    except Exception as exc:
        raise MailhubError(f"{account.alias}: {describe_error(exc, account.alias)}") from exc
