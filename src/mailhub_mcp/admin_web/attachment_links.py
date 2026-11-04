"""Signed, stateless attachment download links and Content-Disposition."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mailhub_mcp.core.attachments import ascii_filename, served_filename

from .session import Signer

LINK_SECONDS = 14 * 24 * 3600
FILES_PATH = "/admin/files/"
_KEYS = frozenset({"a", "m", "p", "exp"})


def derive_link_key(jwt_signing_key: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"mailhub-attachment-link",
    ).derive(jwt_signing_key.encode("utf-8"))


@dataclass(frozen=True)
class LinkTarget:
    alias: str
    message_id: str
    part_id: str


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class AttachmentLinks:
    def __init__(
        self, key: bytes, public_base_url: str, now: Callable[[], float] = time.time
    ) -> None:
        self._signer = Signer(key)
        self._base = public_base_url.rstrip("/")
        self._now = now

    def token(self, alias: str, message_id: str, part_id: str) -> str:
        return self._signer.dumps(
            {"a": alias, "m": message_id, "p": part_id, "exp": self._now() + LINK_SECONDS}
        )

    def url(self, alias: str, message_id: str, part_id: str) -> str:
        return f"{self._base}{FILES_PATH}{self.token(alias, message_id, part_id)}"

    def decode(self, token: str) -> LinkTarget | Literal["expired"] | None:
        data = self._signer.loads(token)
        if data is None or set(data) != _KEYS:
            return None
        if not (
            isinstance(data["a"], str)
            and isinstance(data["m"], str)
            and isinstance(data["p"], str)
            and _is_number(data["exp"])
        ):
            return None
        if self._now() >= data["exp"]:
            return "expired"
        return LinkTarget(data["a"], data["m"], data["p"])


def content_disposition(filename: str) -> str:
    """Header naming the file exactly as `download_block_reason` checked it (see core.attachments)."""
    name = served_filename(filename)
    return f"attachment; filename=\"{ascii_filename(name)}\"; filename*=UTF-8''{quote(name, safe='')}"
