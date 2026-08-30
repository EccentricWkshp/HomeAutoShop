"""
Fail the build on the accessibility mistakes that are actually checkable.

SPEC §9.5 targets WCAG 2.1 AA. Most of that is a judgment call a script cannot
make — whether alternative text is *useful*, whether a heading order matches the
document's real structure. This checks the handful that are unambiguous, for the
same reason `check_translations` exists: a discipline nobody can verify is a
discipline that quietly stops being true.

What it checks, and why each one:

* **Every form control has an accessible name.** A `placeholder` is not a label:
  it vanishes the moment somebody types, and a screen reader may or may not
  announce it. This is the single most common real failure in a hand-written
  form, and it is the one that makes a page unusable rather than merely awkward.

  A Django-rendered widget (`{{ field }}`) is never matched here, because the
  pattern below looks for a literal `<input>` tag and a template variable is
  not one. There used to be a clause exempting tags that contained `{{`, on the
  theory that those were widgets; every tag it actually exempted was a
  hand-written control with a template value, which is precisely the case worth
  catching. It hid three unlabeled interval boxes on the schedule screen.
* **Every image has an `alt`.** Decorative images need `alt=""` — stated, not
  omitted, because omission is indistinguishable from forgetting.
* **No `positive` tabindex.** A `tabindex="2"` reorders the whole page's focus
  sequence and the damage shows up on a page nobody edited.
* **No `user-scalable=no` / `maximum-scale`.** Blocking zoom on a phone UI is a
  particularly bad idea in a garage.

Deliberately not checked: color contrast (the palette is tokens, checked once
by eye against the AA ratios), and text-node prose. Both produce false positives,
and a check that cries wolf is a check that gets switched off.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

CONTROL = re.compile(r"<(input|select|textarea)\b([^>]*)>", re.I | re.S)
IMAGE = re.compile(r"<img\b([^>]*)>", re.I | re.S)
LABEL_FOR = re.compile(r"<label[^>]*\bfor=[\"']([^\"']+)[\"']", re.I)
WRAPPING_LABEL = re.compile(r"<label\b[^>]*>(?:(?!</label>).)*?<(input|select|textarea)\b", re.I | re.S)
ID_ATTR = re.compile(r"\bid=[\"']([^\"']+)[\"']", re.I)
TYPE_ATTR = re.compile(r"\btype=[\"']([^\"']+)[\"']", re.I)
POSITIVE_TABINDEX = re.compile(r"\btabindex=[\"']\s*[1-9]", re.I)
BLOCKS_ZOOM = re.compile(r"user-scalable\s*=\s*no|maximum-scale\s*=\s*1", re.I)

#: Types that are not interactive controls and need no name of their own.
SILENT_TYPES = {"hidden", "submit", "reset", "button", "image"}

#: Attributes that give a control a name without a `<label>`.
NAMING_ATTRS = ("aria-label", "aria-labelledby", "title")


class Finding:
    __slots__ = ("path", "line", "what", "why")

    def __init__(self, path: Path, line: int, what: str, why: str) -> None:
        self.path, self.line, self.what, self.why = path, line, what, why

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.why}: {self.what[:70]}"


class Command(BaseCommand):
    help = "Check templates against the checkable half of WCAG 2.1 AA (SPEC §9.5)."

    def handle(self, *args, **options):
        root = Path(settings.BASE_DIR)
        findings: list[Finding] = []
        for path in sorted((root / "templates").rglob("*.html")):
            findings.extend(self._scan(path, root))

        if not findings:
            self.stdout.write(self.style.SUCCESS("Templates pass the checkable accessibility rules."))
            return

        for finding in findings:
            self.stderr.write(self.style.ERROR(str(finding)))
        self.stderr.write("")
        raise SystemExit(
            f"{len(findings)} accessibility problem(s). Give every control a label "
            "or an aria-label, and every image an alt."
        )

    def _scan(self, path: Path, root: Path) -> list[Finding]:
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(root)
        findings: list[Finding] = []

        labeled_ids = set(LABEL_FOR.findall(text))
        # A control wrapped in its own <label> is named by it, with no `for`
        # needed. Common for checkboxes, and correct.
        wrapped_spans = [m.span() for m in WRAPPING_LABEL.finditer(text)]

        for match in CONTROL.finditer(text):
            attrs = match.group(2)
            kind = (TYPE_ATTR.search(attrs).group(1).lower() if TYPE_ATTR.search(attrs) else "")
            if kind in SILENT_TYPES:
                continue
            if any(attr in attrs for attr in NAMING_ATTRS):
                continue
            found_id = ID_ATTR.search(attrs)
            if found_id and found_id.group(1) in labeled_ids:
                continue
            if any(start <= match.start() <= end + 400 for start, end in wrapped_spans):
                continue
            findings.append(
                Finding(relative, _line(text, match.start()), match.group(0), "control has no name")
            )

        for match in IMAGE.finditer(text):
            if "alt=" not in match.group(1):
                findings.append(
                    Finding(relative, _line(text, match.start()), match.group(0), "image has no alt")
                )

        for pattern, why in (
            (POSITIVE_TABINDEX, "positive tabindex reorders the page"),
            (BLOCKS_ZOOM, "zoom is blocked"),
        ):
            for match in pattern.finditer(text):
                findings.append(
                    Finding(relative, _line(text, match.start()), match.group(0), why)
                )
        return findings


def _line(text: str, offset: int) -> int:
    return text[:offset].count("\n") + 1
