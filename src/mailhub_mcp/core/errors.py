"""Errors shared by the core and all providers."""

from pathlib import Path


class MailhubError(Exception):
    pass


class AccountNotFound(MailhubError):
    def __init__(self, alias: str, valid: list[str]):
        self.alias = alias
        self.valid = valid
        super().__init__(f"unknown account '{alias}'; valid accounts: {', '.join(valid)}")


class DuplicateAlias(MailhubError):
    def __init__(self, alias: str, paths: list[Path]):
        self.alias = alias
        self.paths = paths
        super().__init__(
            f"alias '{alias}' defined more than once: {', '.join(str(p) for p in paths)}"
        )


class AuthExpired(MailhubError):
    def __init__(self, alias: str, hint: str):
        self.alias = alias
        self.hint = hint
        super().__init__(f"invalid or revoked credentials: run {hint} again")


class MessageNotFound(MailhubError):
    def __init__(self, ids: list[str]):
        self.ids = ids
        super().__init__(f"message(s) not found: {', '.join(ids)}")


class TransientError(MailhubError):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class OrganizeError(MailhubError, ValueError):
    pass
