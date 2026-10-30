"""Link a mail account: mailhub-authorize <provider> <client_secret.json> <alias>."""

import argparse
import os
import sys
from pathlib import Path

from mailhub_mcp.core.accounts import ALIAS_RE, RESERVED_ALIASES
from mailhub_mcp.providers import AUTHORIZERS


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def _private_dir(path: Path) -> None:
    """Create `path` with mode 0o700 if missing; directories that already exist are left alone."""
    if path.is_dir():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=0o700)
    path.chmod(0o700)  # mkdir's mode is masked by the umask


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mailhub-authorize", description=__doc__)
    parser.add_argument("provider")
    parser.add_argument("client_secret", type=Path)
    parser.add_argument("alias")
    parser.add_argument("--accounts-dir", type=Path, default=Path("./secrets/accounts"))
    args = parser.parse_args(argv)

    if args.provider not in AUTHORIZERS:
        return _fail(
            f"unknown provider {args.provider!r}: choose one of {', '.join(sorted(AUTHORIZERS))}"
        )
    if not ALIAS_RE.fullmatch(args.alias):
        return _fail(f"invalid alias {args.alias!r}: must match {ALIAS_RE.pattern}")
    if args.alias in RESERVED_ALIASES:
        return _fail(f"alias {args.alias!r} is reserved (it names the global preferences scope)")
    for other in sorted(AUTHORIZERS):
        if other != args.provider and (args.accounts_dir / other / f"{args.alias}.json").exists():
            return _fail(f"alias {args.alias!r} already exists under provider {other!r}")

    credential = AUTHORIZERS[args.provider](args.client_secret)

    out = args.accounts_dir / args.provider / f"{args.alias}.json"
    _private_dir(args.accounts_dir)
    _private_dir(out.parent)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(credential)
    out.chmod(0o600)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
