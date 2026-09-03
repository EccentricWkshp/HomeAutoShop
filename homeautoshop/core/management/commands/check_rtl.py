"""
Fail the build when the layout stops being direction-neutral (SPEC §5.6, R-8).

§5.6 promised "CSS logical properties (`margin-inline-start`, not
`margin-left`) throughout, so RTL is a stylesheet concern rather than a
rewrite", and R-8 deferred only the *verification*. Verification found three
kinds of thing, which is the argument for running it rather than asserting it:

* Twelve physical declarations in `app.css`, including `text-align: left` on
  every table cell — the one that would have made every table in the
  application read the wrong way round.
* Two inline `style` attributes in templates, which no stylesheet review
  would have looked at.
* **No `dir` on `<html>` at all.** This is the one that mattered. Logical
  properties resolve against the element's direction, and with no direction
  declared they all resolve to left-to-right — so the discipline the section
  described was real and bought nothing, and could not have been noticed by
  reading the stylesheet.

What is checked, and why each one:

* **Physical inline-axis properties.** `margin-left`, `padding-right`,
  `border-left-color`, a bare `left:`/`right:`, the physical corner radii.
  Each has a logical spelling that is the same length and does the same thing
  in English.
* **Directional keywords.** `text-align: left`, `float: right`, `clear: left`.
  `start`/`end`/`inline-start`/`inline-end` say what was meant.
* **Four-value shorthands whose horizontal values differ.** `padding: 0 0
  .75rem 1rem` is a `padding-left` wearing a disguise, and grepping for
  `-left` never finds it. A four-value shorthand whose second and fourth
  values agree is symmetric and therefore fine, which is what keeps this rule
  from crying wolf over `inset: auto 0 0 0`.
* **Every HTML document declares a direction.** Stated, not omitted — an
  omitted `dir` is indistinguishable from a forgotten one, and it is exactly
  what was forgotten.

Deliberately not checked:

* **The block axis.** `top`, `bottom`, `margin-top`, `border-bottom` are
  untouched by right-to-left text. Rewriting them as `inset-block-start` and
  friends buys nothing until a vertical writing mode is in scope, and a rule
  that demands churn for no behavior is a rule somebody switches off.
* **Whether a translation reads well right-to-left.** No script can, and no
  right-to-left catalog ships (§5.6 ship set). This checks that the layout
  would turn round, which is the half that is mechanical.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

#: Comments in both syntaxes. Blanked rather than stripped so reported line
#: numbers stay true, and because a comment explaining one of these rules must
#: not be reported as breaking it.
COMMENT = re.compile(
    r"/\*.*?\*/|\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}|\{#.*?#\}|<!--.*?-->",
    re.S,
)

#: `margin-left`, `border-right-color`, `scroll-padding-left`, … The lookbehind
#: keeps `border-inline-start-color` and a `--custom-left` property out.
PHYSICAL_PROPERTY = re.compile(
    r"(?<![-\w])(?:margin|padding|border|inset|scroll-margin|scroll-padding)"
    r"-(?:left|right)(?:-(?:width|style|color))?\s*:",
    re.I,
)

#: A bare `left:` / `right:` used for positioning.
PHYSICAL_INSET = re.compile(r"(?<![-\w])(?:left|right)\s*:", re.I)

#: `border-top-left-radius` and its three siblings.
PHYSICAL_RADIUS = re.compile(
    r"(?<![-\w])border-(?:top|bottom)-(?:left|right)-radius\s*:", re.I
)

#: `text-align: left`, `float: right`, `clear: left`.
PHYSICAL_KEYWORD = re.compile(
    r"(?<![-\w])(?:text-align|float|clear)\s*:\s*(?:left|right)\b", re.I
)

#: Shorthands that take up to four sides in physical order.
SHORTHAND = re.compile(
    r"(?<![-\w])(margin|padding|inset|border-width|border-style|border-color)"
    r"\s*:\s*([^;{}]+)",
    re.I,
)

#: The document element of an HTML page.
HTML_TAG = re.compile(r"<html\b([^>]*)>", re.I)

#: An inline style attribute. Templates have no stylesheet to review, so this
#: is the only way the rules above reach them.
INLINE_STYLE = re.compile(r"\bstyle\s*=\s*\"([^\"]*)\"|\bstyle\s*=\s*'([^']*)'", re.I)

REPLACEMENTS = {
    "margin-left": "margin-inline-start",
    "margin-right": "margin-inline-end",
    "padding-left": "padding-inline-start",
    "padding-right": "padding-inline-end",
    "border-left": "border-inline-start",
    "border-right": "border-inline-end",
    "left": "inset-inline-start",
    "right": "inset-inline-end",
    "text-align: left": "text-align: start",
    "text-align: right": "text-align: end",
    "float: left": "float: inline-start",
    "float: right": "float: inline-end",
}


class Finding:
    __slots__ = ("path", "line", "what", "why")

    def __init__(self, path: Path, line: int, what: str, why: str) -> None:
        self.path, self.line, self.what, self.why = path, line, what, why

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.why}: {self.what.strip()[:70]}"


def _blank_comments(text: str) -> str:
    def blank(match) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    return COMMENT.sub(blank, text)


def _line(text: str, offset: int) -> int:
    return text[:offset].count("\n") + 1


def _values(declaration: str) -> list[str]:
    """Split a shorthand's value on whitespace, keeping `calc(a + b)` whole."""
    out: list[str] = []
    depth = 0
    current = ""
    for char in declaration.strip():
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char.isspace() and depth == 0:
            if current:
                out.append(current)
                current = ""
            continue
        current += char
    if current:
        out.append(current)
    return out


def _hint(what: str) -> str:
    lowered = " ".join(what.lower().rstrip(":").split())
    for physical, logical in REPLACEMENTS.items():
        if lowered == physical:
            return f" — use {logical}"
        if lowered.startswith(physical + "-"):
            # `border-left-color` keeps its suffix: `border-inline-start-color`.
            return f" — use {logical}{lowered[len(physical):]}"
    return ""


def scan_css(text: str, path: Path) -> list[Finding]:
    """The four stylesheet rules, over one block of CSS."""
    text = _blank_comments(text)
    findings: list[Finding] = []

    for pattern, why in (
        (PHYSICAL_PROPERTY, "physical property"),
        (PHYSICAL_RADIUS, "physical corner"),
        (PHYSICAL_KEYWORD, "physical direction"),
    ):
        for match in pattern.finditer(text):
            findings.append(
                Finding(path, _line(text, match.start()), match.group(0),
                        why + _hint(match.group(0)))
            )

    for match in PHYSICAL_INSET.finditer(text):
        findings.append(
            Finding(path, _line(text, match.start()), match.group(0),
                    "physical inset" + _hint(match.group(0)))
        )

    for match in SHORTHAND.finditer(text):
        values = _values(match.group(2))
        # Four values are top/right/bottom/left. Only the horizontal pair
        # turns round, so a shorthand that says the same thing on both sides
        # is already direction-neutral.
        if len(values) == 4 and values[1].lower() != values[3].lower():
            findings.append(
                Finding(
                    path, _line(text, match.start()), match.group(0),
                    f"four-value {match.group(1).lower()} sets the sides apart "
                    f"— split out the inline half",
                )
            )
    return findings


def scan_markup(text: str, path: Path) -> list[Finding]:
    """Inline styles, and the document's declared direction."""
    text = _blank_comments(text)
    findings: list[Finding] = []

    for match in INLINE_STYLE.finditer(text):
        declaration = match.group(1) if match.group(1) is not None else match.group(2)
        offset = _line(text, match.start()) - 1
        for finding in scan_css(declaration, path):
            findings.append(
                Finding(path, offset + finding.line, finding.what,
                        "inline style: " + finding.why)
            )

    for match in HTML_TAG.finditer(text):
        if not re.search(r"\bdir\s*=", match.group(1), re.I):
            findings.append(
                Finding(path, _line(text, match.start()), match.group(0),
                        "<html> declares no dir, so every logical property "
                        "resolves left-to-right")
            )
    return findings


def collect(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted((root / "static").rglob("*.css")):
        findings.extend(scan_css(path.read_text(encoding="utf-8"), path.relative_to(root)))
    for directory in ("templates", "static"):
        for path in sorted((root / directory).rglob("*.html")):
            findings.extend(
                scan_markup(path.read_text(encoding="utf-8"), path.relative_to(root))
            )
    return findings


class Command(BaseCommand):
    help = "Check that the layout would turn round under a right-to-left locale (SPEC §5.6)."

    def handle(self, *args, **options):
        findings = collect(Path(settings.BASE_DIR))
        if not findings:
            self.stdout.write(self.style.SUCCESS("The layout is direction-neutral."))
            return

        for finding in findings:
            self.stderr.write(self.style.ERROR(str(finding)))
        self.stderr.write("")
        raise SystemExit(
            f"{len(findings)} direction-bound rule(s). Logical properties only: "
            "inline-start and inline-end, start and end."
        )
