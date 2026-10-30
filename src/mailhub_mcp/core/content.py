"""Provider-agnostic handling of untrusted email text.

Sanitizing (hidden text, invisible characters), delimiting (per-response nonce
markers) and heuristic suspicion flags. The flags are an aid, not a guarantee.
"""

import re
import secrets
from html.parser import HTMLParser

HIDDEN_TEXT_THRESHOLD = 200

_INVISIBLE = re.compile(
    "[​-‏‪-‮⁠-⁤⁦-⁩﻿"
    "\x00-\x08\x0b-\x1f\x7f-\x9f]"
)
_BLANK_RUNS = re.compile(r"\n(?:[ \t]*\n){2,}")


def clean_text(s: str) -> str:
    """Remove invisible/control characters and collapse runs of blank lines."""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _INVISIBLE.sub("", s)
    return _BLANK_RUNS.sub("\n\n", s)


_VOID = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)
_BLOCK = frozenset(
    "address article aside blockquote body dd div dl dt fieldset figcaption figure "
    "footer form h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table "
    "tbody td tfoot th thead tr ul br".split()
)
_ALWAYS_DROPPED = frozenset({"head", "script", "style", "template", "noscript"})
_NUM = re.compile(r"^([0-9]*\.?[0-9]+)([a-z%]*)$")


def _style_hides(style: str) -> bool:
    props: dict[str, str] = {}
    for part in style.split(";"):
        key, sep, value = part.partition(":")
        if sep:
            props[key.strip().lower()] = value.replace("!important", "").strip().lower()
    if props.get("display") == "none" or props.get("visibility") == "hidden":
        return True
    if props.get("opacity") in ("0", "0.0", "0%"):
        return True
    m = _NUM.match(props.get("font-size", ""))
    if m:
        size, unit = float(m.group(1)), m.group(2)
        if size == 0 or (unit == "px" and size <= 1):
            return True
    m = _NUM.match(props.get("max-height", ""))
    if m and float(m.group(1)) == 0 and props.get("overflow") == "hidden":
        return True
    return False


def _attr_hides(attrs: list[tuple[str, str | None]]) -> bool:
    for name, value in attrs:
        if name == "hidden":
            return True
        if name == "aria-hidden" and (value or "").strip().lower() == "true":
            return True
        if name == "style" and value and _style_hides(value):
            return True
    return False


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.hidden_removed = 0
        # open non-void elements: (tag, hides)
        self.stack: list[tuple[str, bool]] = []
        self.dropped = 0  # open elements currently hiding content
        self.in_style = 0  # style content is dropped but not counted

    def _count(self, data: str) -> None:
        if not self.in_style:
            self.hidden_removed += len("".join(data.split()))

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            if tag in _BLOCK and not self.dropped:
                self.out.append("\n")
            return
        hides = tag in _ALWAYS_DROPPED or _attr_hides(attrs)
        self.stack.append((tag, hides))
        if hides:
            self.dropped += 1
        if tag == "style":
            self.in_style += 1
        if tag in _BLOCK and not self.dropped:
            self.out.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _VOID or tag in _BLOCK:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        idx = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if idx is None:
            return
        while len(self.stack) > idx:
            name, hides = self.stack.pop()
            if hides:
                self.dropped -= 1
            if name == "style":
                self.in_style -= 1
        if tag in _BLOCK and not self.dropped:
            self.out.append("\n")

    def handle_data(self, data):
        if self.dropped:
            self._count(data)
        else:
            self.out.append(data)

    def handle_comment(self, data):
        self._count(data)


def html_to_safe_text(html: str) -> tuple[str, int]:
    """Return (visible text, number of hidden characters removed).

    Drops head/script/style/template/noscript, comments, and elements hidden by
    inline style, the hidden attribute or aria-hidden. Style blocks are dropped
    without being counted, so ordinary newsletters do not look suspicious.
    """
    parser = _Extractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.out)
    text = "\n".join(line.strip() for line in text.splitlines())
    return clean_text(text).strip(), parser.hidden_removed


def new_nonce() -> str:
    return secrets.token_hex(6)


_MARKER = re.compile(r"\[(/?)untrusted:", re.IGNORECASE)


def wrap_untrusted(text: str, nonce: str) -> str:
    defanged = _MARKER.sub(lambda m: f"[{m.group(1)}untrusted_:", text)
    return f"[untrusted:{nonce}]\n{defanged}\n[/untrusted:{nonce}]"


_OVERRIDE = re.compile(
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)?\s*instructions"
    r"|disregard"
    r"|you\s+are\s+(?:now\s+)?(?:an?\s+)?(?:ai|assistant|claude)\b"
    r"|ignor[ae]\s+(?:todas\s+)?(?:as\s+)?instru[cç][õo]es"
    r"|voc[eê]\s+[eé]\s+(?:um|o)\s+assistente",
    re.IGNORECASE,
)
_ADDRESSES = re.compile(r"\bassistant\s*:|\bai\s+assistant\b|\bclaude\s*,", re.IGNORECASE)
_ACTION = re.compile(
    r"\bforward\b"
    r"|\b(?:create|make|write|save)\s+(?:a\s+|an\s+|the\s+)?draft\b"
    r"|\b(?:change|update|modify|edit|set)\s+(?:your\s+|my\s+|the\s+)?(?:preferences|settings)\b"
    r"|\breply\s+to\b[^\n]{0,120}?\bwith\b",
    re.IGNORECASE,
)


def assess(fields: list[str], hidden_removed: int, tool_names: frozenset[str]) -> list[str]:
    """Heuristic reason codes for likely injection attempts, in a fixed order."""
    text = "\n".join(fields)
    lowered = text.lower()
    reasons: list[str] = []
    if any(name.lower() in lowered for name in tool_names):
        reasons.append("mentions_tools")
    if _OVERRIDE.search(text):
        reasons.append("instruction_override")
    if _ADDRESSES.search(text):
        reasons.append("addresses_assistant")
    if _ACTION.search(text):
        reasons.append("requests_action")
    if hidden_removed > HIDDEN_TEXT_THRESHOLD:
        reasons.append("hidden_text")
    return reasons
