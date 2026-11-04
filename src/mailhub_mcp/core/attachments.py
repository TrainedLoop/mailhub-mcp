"""Rules deciding whether an attachment may be offered for download."""

from typing import Literal

BlockReason = Literal["spam", "suspicious", "blocked_type"]

# File types Gmail itself refuses to send or receive, without the dot.
GMAIL_BLOCKED_EXTENSIONS: frozenset[str] = frozenset(
    "ade adp apk appx appxbundle bat cab chm cmd com cpl diagcab diagcfg diagpack dll dmg ex ex_ exe "
    "hta ins isp iso jar jnlp js jse lib lnk mde mjs msc msi msix msixbundle msp mst nsh pif ps1 scr "
    "sct shb sys vb vbe vbs vhd vxd wsc wsf wsh xll".split()
)


def _extension(filename: str) -> str:
    """Text after the last dot, with trailing dots and whitespace ignored, lowercased."""
    name = filename.rstrip(". \t\r\n")
    return name.rpartition(".")[2].lower() if "." in name else ""


def download_block_reason(spam: bool, suspicious: bool, filename: str) -> BlockReason | None:
    """Why this attachment must not get a download link, or None when it may."""
    if spam:
        return "spam"
    if suspicious:
        return "suspicious"
    if _extension(filename) in GMAIL_BLOCKED_EXTENSIONS:
        return "blocked_type"
    return None
