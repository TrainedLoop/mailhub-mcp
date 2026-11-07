"""Rules deciding whether an attachment may be offered for download, and the name it is served under.

The blocked-type check runs on the names a browser could actually save (`served_filename` and its
`ascii_filename` fallback), so whatever the download sends as a file name never ends in a blocked
extension unless `download_block_reason` said `blocked_type`.
"""

import re
import unicodedata
from typing import Literal

BlockReason = Literal["spam", "suspicious", "blocked_type"]

# File types Gmail itself refuses to send or receive, without the dot.
GMAIL_BLOCKED_EXTENSIONS: frozenset[str] = frozenset(
    ["ade", "adp", "apk", "appx", "appxbundle", "bat", "cab", "chm", "cmd", "com", "cpl", "diagcab", "diagcfg", "diagpack", "dll", "dmg", "ex", "ex_", "exe", "hta", "ins", "isp", "iso", "jar", "jnlp", "js", "jse", "lib", "lnk", "mde", "mjs", "msc", "msi", "msix", "msixbundle", "msp", "mst", "nsh", "pif", "ps1", "scr", "sct", "shb", "sys", "vb", "vbe", "vbs", "vhd", "vxd", "wsc", "wsf", "wsh", "xll"]
)

MAX_NAME = 150
FALLBACK_NAME = "attachment"


def _trimmable(c: str) -> bool:
    """Characters browsers and file systems may drop from the ends of a saved name."""
    return c == "." or c.isspace() or unicodedata.category(c)[0] in "CZ"


def _trim_end(name: str) -> str:
    end = len(name)
    while end and _trimmable(name[end - 1]):
        end -= 1
    return name[:end]


def _trim(name: str) -> str:
    start = 0
    while start < len(name) and _trimmable(name[start]):
        start += 1
    return _trim_end(name[start:])


def _extension(filename: str) -> str:
    """Text after the last dot, after NFKC and with trailing dots, whitespace and invisibles ignored."""
    name = _trim_end(unicodedata.normalize("NFKC", filename))
    return name.rpartition(".")[2].lower() if "." in name else ""


def served_filename(filename: str) -> str:
    """The file name a download is served under: NFKC, no control characters, path separators or
    quotes, no leading or trailing dots and whitespace, at most MAX_NAME characters with the last
    extension kept."""
    name = unicodedata.normalize("NFKC", filename)
    name = _trim("".join(c for c in name if unicodedata.category(c)[0] != "C" and c not in '/\\"'))
    if len(name) > MAX_NAME:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) + 1 < MAX_NAME:
            name = stem[: MAX_NAME - len(ext) - 1] + "." + ext
        else:
            name = name[:MAX_NAME]
        name = _trim(name)
    return name or FALLBACK_NAME


def ascii_filename(name: str) -> str:
    """ASCII stand-in for a served name (the Content-Disposition `filename=` fallback)."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return _trim(re.sub(r"[^A-Za-z0-9._ ()-]", "_", ascii_name)) or FALLBACK_NAME


# Suspicion reasons that do not withhold attachments: hidden text is already
# removed before the model reads the message, so alone it is no manipulation.
WEAK_SUSPICION_REASONS = frozenset({"hidden_text"})


def blocks_downloads(reasons: list[str]) -> bool:
    """Whether a message's suspicion reasons withhold its attachments."""
    return any(r not in WEAK_SUSPICION_REASONS for r in reasons)


def download_block_reason(spam: bool, suspicious: bool, filename: str) -> BlockReason | None:
    """Why this attachment must not get a download link, or None when it may."""
    if spam:
        return "spam"
    if suspicious:
        return "suspicious"
    served = served_filename(filename)
    if any(_extension(n) in GMAIL_BLOCKED_EXTENSIONS for n in (filename, served, ascii_filename(served))):
        return "blocked_type"
    return None
