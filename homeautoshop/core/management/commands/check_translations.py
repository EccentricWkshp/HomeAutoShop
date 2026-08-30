"""
Fail the build on user-facing strings that skipped the message catalog.

SPEC §5.6 makes localization a commit-one discipline, and the only thing that
keeps such a discipline alive is a check that fails. This is deliberately a
**precise** check rather than an exhaustive one: it looks at the places where an
untranslated string is unambiguous — a literal handed to `messages.*()`, a
`help_text`/`verbose_name`, or a template that renders text without loading i18n
— because a check that cries wolf gets suppressed, and a suppressed check is
worse than none.

What it does not attempt: proving that every text node in every template is
wrapped. That needs a real template parser and still produces false positives on
punctuation, numbers, and icons.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

# Calls whose first string argument is shown to a person.
MESSAGE_CALLS = {"success", "error", "warning", "info", "debug", "add_message"}

# ...except on a logger, which shares every one of those names. Log lines are
# for the operator reading a container's output and are deliberately English
# and untranslated (NFR-R-3). Without this the check fires on `log.info("...")`
# and starts crying wolf, which is precisely how a check gets switched off.
LOGGER_RECEIVERS = {"log", "logger", "logging", "LOG", "_log"}
# Model/form kwargs that render as visible labels.
LABEL_KWARGS = {"help_text", "verbose_name", "verbose_name_plural", "label"}

TEMPLATE_TEXT = re.compile(r">\s*([A-Z][A-Za-z][^<>{}\n]{6,})\s*<")
SKIP_TEMPLATE_TEXT = re.compile(r"^[\W\d_]+$")


class Finding:
    __slots__ = ("path", "line", "text", "why")

    def __init__(self, path: Path, line: int, text: str, why: str) -> None:
        self.path, self.line, self.text, self.why = path, line, text, why

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.why}: {self.text[:70]!r}"


class Command(BaseCommand):
    help = "Check that user-facing strings go through gettext (SPEC §5.6)."

    def add_arguments(self, parser):
        parser.add_argument("--strict", action="store_true", help="Also scan template text nodes.")

    def handle(self, *args, **options):
        root = Path(settings.BASE_DIR)
        findings: list[Finding] = []

        for path in sorted((root / "homeautoshop").rglob("*.py")):
            if "migrations" in path.parts or path.name.startswith("test"):
                continue
            findings.extend(self._scan_python(path))

        for path in sorted((root / "templates").rglob("*.html")):
            findings.extend(self._scan_template(path, strict=options["strict"]))

        if not findings:
            self.stdout.write(self.style.SUCCESS("All user-facing strings go through gettext."))
            return

        for finding in findings:
            self.stderr.write(self.style.ERROR(str(finding)))
        self.stderr.write("")
        raise SystemExit(
            f"{len(findings)} unwrapped user-facing string(s). "
            "Wrap them in gettext (`_(...)`) or `{% translate %}`."
        )

    # -- python ----------------------------------------------------------

    def _scan_python(self, path: Path) -> list[Finding]:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            return []

        findings: list[Finding] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            name = ""
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
            elif isinstance(node.func, ast.Name):
                name = node.func.id

            if name in MESSAGE_CALLS and not self._is_logger(node.func):
                # messages.success(request, "Saved.") -> the message is arg 1.
                for arg in node.args[1:2] or node.args[:1]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if self._is_prose(arg.value):
                            findings.append(
                                Finding(path, arg.lineno, arg.value, "message not translated")
                            )

            for kw in node.keywords:
                if kw.arg in LABEL_KWARGS and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str) and self._is_prose(kw.value.value):
                        findings.append(
                            Finding(path, kw.value.lineno, kw.value.value, f"{kw.arg} not translated")
                        )
        return findings

    @staticmethod
    def _is_logger(func: ast.expr) -> bool:
        """Whether `x.warning(...)` is a logger rather than the message framework."""
        if not isinstance(func, ast.Attribute):
            return False
        receiver = func.value
        if isinstance(receiver, ast.Name):
            return receiver.id in LOGGER_RECEIVERS
        # `self.log.info(...)`, `self.stdout.write(...)`-adjacent shapes.
        if isinstance(receiver, ast.Attribute):
            return receiver.attr in LOGGER_RECEIVERS
        return False

    # -- templates -------------------------------------------------------

    def _scan_template(self, path: Path, *, strict: bool) -> list[Finding]:
        text = path.read_text(encoding="utf-8")
        findings: list[Finding] = []

        renders_text = "{% translate" in text or "{% blocktranslate" in text
        if renders_text and "{% load i18n" not in text and "{% extends" not in text:
            findings.append(Finding(path, 1, path.name, "uses translate without loading i18n"))

        if strict:
            for match in TEMPLATE_TEXT.finditer(text):
                candidate = match.group(1).strip()
                if SKIP_TEMPLATE_TEXT.match(candidate) or "{" in candidate:
                    continue
                line = text[: match.start()].count("\n") + 1
                findings.append(Finding(path, line, candidate, "literal text node"))
        return findings

    @staticmethod
    def _is_prose(value: str) -> bool:
        """Only flag things a person would actually read."""
        stripped = value.strip()
        if len(stripped) < 6 or " " not in stripped:
            return False
        # Format strings, keys, and paths are not prose.
        return not (stripped.startswith(("http", "/", "{")) or stripped.isupper())
