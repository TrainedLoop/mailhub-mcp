"""Provider-agnostic handling of untrusted email text.

Sanitizing (hidden text, invisible characters), delimiting (per-response nonce
markers) and heuristic suspicion flags. The flags are an aid, not a guarantee.
"""

import re
import secrets
from collections import defaultdict
from html.parser import HTMLParser

HIDDEN_TEXT_THRESHOLD = 200

# Zero-width, bidi controls, soft hyphen, Arabic letter mark, Mongolian vowel
# separator, Unicode tag characters, and C0/C1 controls except \n and \t.
_INVISIBLE = re.compile(
    "["
    "\u200b-\u200f"
    "\u202a-\u202e"
    "\u2060-\u2064"
    "\u2066-\u2069"
    "\ufeff"
    "\u00ad\u061c\u180e"
    "\U000e0000-\U000e007f"
    "\u0000-\u0008\u000b-\u001f\u007f-\u009f"
    "]"
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
_NUM = re.compile(r"^([+-]?(?:[0-9]*\.?[0-9]+)(?:e[+-]?[0-9]+)?)([a-z%]*)$")
_IMPORTANT = re.compile(r"!\s*important")
_CSS_ESCAPE = re.compile(r"\\(?:([0-9a-f]{1,6})[ \t\n\r\f]?|(.))", re.DOTALL)
_PX_PER = {
    "px": 1.0,
    "pt": 4 / 3,
    "pc": 16.0,
    "in": 96.0,
    "cm": 37.8,
    "mm": 3.78,
    "q": 0.945,
    "em": 16.0,
    "rem": 16.0,
    "%": 0.16,
}


def _css_unescape(s: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group(1):
            cp = int(m.group(1), 16)
            return chr(cp) if 0 < cp <= 0x10FFFF else "\ufffd"
        return m.group(2)

    return _CSS_ESCAPE.sub(repl, s)


def _strip_css_comments(s: str) -> str:
    out: list[str] = []
    pos = 0
    while True:
        start = s.find("/*", pos)
        if start < 0:
            out.append(s[pos:])
            return "".join(out)
        out.append(s[pos:start])
        end = s.find("*/", start + 2)
        if end < 0:
            return "".join(out)
        pos = end + 2


def _style_hides(style: str) -> bool:
    style = _css_unescape(_strip_css_comments(style.lower()))
    props: dict[str, str] = {}
    important: set[str] = set()
    for part in style.split(";"):
        key, sep, value = part.partition(":")
        if not sep:
            continue
        key = key.strip()
        is_important = bool(_IMPORTANT.search(value))
        if key in important and not is_important:
            continue
        if is_important:
            important.add(key)
        props[key] = _IMPORTANT.sub("", value).strip()
    if props.get("display") == "none" or props.get("visibility") == "hidden":
        return True
    m = _NUM.match(props.get("opacity", ""))
    if m and float(m.group(1)) <= 0 and m.group(2) in ("", "%"):
        return True
    m = _NUM.match(props.get("font-size", ""))
    if m:
        size, unit = float(m.group(1)), m.group(2)
        if size == 0 or (unit in _PX_PER and size * _PX_PER[unit] <= 1):
            return True
    m = _NUM.match(props.get("max-height", ""))
    if m and float(m.group(1)) <= 0 and props.get("overflow") == "hidden":
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


_SAME_KIND_CLOSES = {
    "li": {"li"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "tr": {"tr"},
    "option": {"option"},
}
# Start tags that implicitly close an open <p> (HTML spec; table parts, body and
# table are deliberately absent).
_CLOSES_P = frozenset(
    "address article aside blockquote center details dialog dir div dl fieldset "
    "figcaption figure footer header hgroup main menu nav ol p pre section summary "
    "ul h1 h2 h3 h4 h5 h6 li dd dt form hr".split()
)


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.hidden_removed = 0
        # open non-void elements: (tag, hides, is_barrier)
        self.stack: list[tuple[str, bool, bool]] = []
        self.positions: defaultdict[str, list[int]] = defaultdict(list)
        # stack indices of block or hiding elements (inline end tags stop at these)
        self.barriers: list[int] = []
        self.dropped = 0  # open elements currently hiding content
        self.in_style = 0  # style content is dropped but not counted

    def _count(self, data: str) -> None:
        if not self.in_style:
            self.hidden_removed += len("".join(data.split()))

    def _push(self, tag: str, hides: bool) -> None:
        barrier = hides or tag in _BLOCK
        idx = len(self.stack)
        self.stack.append((tag, hides, barrier))
        self.positions[tag].append(idx)
        if barrier:
            self.barriers.append(idx)
        if hides:
            self.dropped += 1
        if tag == "style":
            self.in_style += 1

    def _pop(self) -> None:
        name, hides, barrier = self.stack.pop()
        self.positions[name].pop()
        if barrier:
            self.barriers.pop()
        if hides:
            self.dropped -= 1
        if name == "style":
            self.in_style -= 1

    def _implied_end(self, tag: str) -> None:
        # Minimal HTML implied end tags; everything else fails closed.
        if not self.stack:
            return
        top = self.stack[-1][0]
        if (top == "p" and tag in _CLOSES_P) or top in _SAME_KIND_CLOSES.get(tag, ()):
            self._pop()

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            if tag in _BLOCK and not self.dropped:
                self.out.append("\n")
            return
        self._implied_end(tag)
        self._push(tag, tag in _ALWAYS_DROPPED or _attr_hides(attrs))
        if tag in _BLOCK and not self.dropped:
            self.out.append("\n")

    def handle_startendtag(self, tag, attrs):
        # Browsers ignore the "/" on non-void HTML elements, so treat it as a
        # plain start tag. SVG/MathML differ (accepted edge case).
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _VOID or not self.positions[tag]:
            return
        # An inline end tag never pops past an open block or hiding element.
        if tag not in _BLOCK and self.barriers and self.barriers[-1] > self.positions[tag][-1]:
            return
        while True:
            name = self.stack[-1][0]
            self._pop()
            if name == tag:
                break
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
    defanged = _MARKER.sub(lambda m: f"[{m.group(1)}untrusted_:", clean_text(text))
    return f"[untrusted:{nonce}]\n{defanged}\n[/untrusted:{nonce}]"


_OVERRIDE = re.compile(
    r"ignore\s+(?:(?:all|any|the)\s+)?(?:(?:previous|prior|above|earlier)\s+)?instructions"
    r"|disregard"
    r"|you\s+are\s+(?:now\s+)?(?:an?\s+)?(?:ai|assistant|claude)\b"
    r"|ignor[ae]\s+(?:todas\s+)?(?:as\s+)?instru[cç][õo]es"
    r"|voc[eê]\s+[eé]\s+(?:um|o)\s+assistente",
    re.IGNORECASE,
)
_ADDRESSES = re.compile(r"\bassistant\s*:|\bai\s+assistant\b|\bclaude\s*,", re.IGNORECASE)
_ACTION = re.compile(
    r"\bforward\s+(?:this|it|the|all|every|these|emails?|messages?)\b"
    r"|\bforward\b[^\n]{0,60}\bto\s+\S+@"
    r"|\b(?:create|make|write|save)\s+(?:a\s+|an\s+|the\s+)?draft\b"
    r"|\b(?:change|modify|edit|set)\s+(?:your\s+|my\s+|the\s+)?(?:preferences|settings)\b"
    r"|\bupdate\s+(?:my\s+|the\s+)?(?:preferences|settings)\b"
    r"|\bupdate\s+your\s+settings\b"
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
