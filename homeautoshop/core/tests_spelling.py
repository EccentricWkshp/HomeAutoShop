"""US English is the source language, and the suite is what keeps it so.

`locale/README.md` says it plainly — en-US is the source, and `locale/en_CA`
exists to render the ten strings Canadian English spells differently. The
arrangement is easy to read backwards, and it was: a release note shipped
saying `colour`, and the sweep that followed found 152 more in comments,
docstrings and documentation.

A rule nobody enforces is a rule that holds until the next person writes a
sentence, so it joins `check_rtl` and `check_translations` as a gate.

The tests below are mostly about what the gate must **not** do, because that is
where the damage was. A blind version of this pass wanted to rename
`PurchaseStatus.CANCELLED = "cancelled"` — a value in the database, not a
spelling — and to edit two applied migrations. A subtler version rewrote
`{% if rollup.labour_hours %}` in two templates while `core/budget.py` went on
calling the property `labour_hours`; Django resolves a missing attribute to the
empty string, so both pages rendered a blank number and the whole suite still
passed. Neither is a spelling mistake. Both are what a spelling pass turns into
when it stops telling prose from tokens.
"""

from __future__ import annotations

import pathlib

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase

from homeautoshop.core.management.commands.check_spelling import (
    ALLOWED,
    _found,
    prose_of,
)


def prose(name: str, text: str) -> str:
    return prose_of(pathlib.Path(name), text)


class TheGateTests(TestCase):
    def test_the_repository_is_us_english(self):
        call_command("check_spelling")

    def test_it_catches_a_british_msgid_in_a_template(self):
        found = _found(prose("x.html", '{% translate "Colour" %}'))
        self.assertEqual(found, [("Colour", "color")])

    def test_it_catches_a_british_word_in_a_python_comment(self):
        found = _found(prose("x.py", "# the colour of the card\nx = 1\n"))
        self.assertEqual(found, [("colour", "color")])

    def test_it_catches_a_british_word_in_a_docstring(self):
        found = _found(prose("x.py", '"""Pick a colour."""\n'))
        self.assertEqual(found, [("colour", "color")])


class WhatItMustNotTouchTests(TestCase):
    """Every one of these is a mistake the pass actually made."""

    def test_a_template_attribute_lookup_is_not_prose(self):
        """`{% if rollup.labour_hours %}` is an attribute, and renaming it in
        the markup alone renders a blank number in silence."""
        self.assertEqual(_found(prose("x.html", "{% if rollup.labour_hours %}")), [])
        self.assertEqual(
            _found(prose("x.html", "{% blocktranslate with h=b.labour_hours %}")), []
        )

    def test_a_python_string_that_is_not_a_docstring_is_data(self):
        """`CANCELLED = "cancelled"` is a stored value. The msgid beside it is
        already `Canceled`, which a blind pass would have broken too."""
        source = 'class S:\n    CANCELLED = "cancelled", _("Canceled")\n'
        self.assertEqual(_found(prose("x.py", source)), [])

    def test_a_dict_key_is_not_a_docstring(self):
        """The heuristic that made this necessary.

        Inside a dict literal written one entry per line, every key starts a
        line — so "a string that starts a line" read a table of British-to-US
        pairs as a run of docstrings, and the sweep sharing that heuristic
        rewrote a sentence about how Canadian spells the unit `metre` into one
        that said nothing.
        """
        source = 'PAIRS = {\n    "colour": "color",\n    "labour": "labor",\n}\n'
        self.assertEqual(_found(prose("x.py", source)), [])

    def test_an_identifier_in_code_is_not_prose(self):
        self.assertEqual(_found(prose("x.py", "labour_hours = 3\n")), [])

    def test_a_markdown_code_span_is_not_prose(self):
        """Documentation naming an identifier is naming it, not spelling it."""
        self.assertEqual(_found(prose("x.md", "Use `labour_hours` here.")), [])
        self.assertEqual(_found(prose("x.md", "```\ncolour = 1\n```")), [])

    def test_an_html_attribute_is_not_prose(self):
        self.assertEqual(_found(prose("x.html", '<div class="colour-swatch">')), [])

    def test_the_locale_directory_is_never_read(self):
        """`locale/en_CA` exists to *produce* `labour` and `catalogue`.

        Correcting it would delete the only reason that catalog is there, and
        `tests_locales` asserts the renderings this must leave alone.
        """
        from homeautoshop.core.management.commands.check_spelling import SKIP_DIRS

        self.assertIn("locale", SKIP_DIRS)
        self.assertIn("migrations", SKIP_DIRS)


class TheWordListIsCompleteEnoughTests(TestCase):
    """The gate's other failure mode, and the one it actually had.

    A hand-written table is only as complete as whoever typed it. The first
    version held `summarise` and `summarised` but not `summarising`, `realise`
    but not `realising`, `organisation` but not `localisation`, `labelled` but
    not `unlabelled` — so a gate that had just swept 152 occurrences reported
    the repository clean while `localisation` sat in `parts/models.py` and
    `quantised` sat in the spec. Thirty-two more turned up the moment the
    inflections were generated instead of typed.
    """

    def test_a_stem_covers_every_inflection_of_itself(self):
        from homeautoshop.core.management.commands.check_spelling import PAIRS

        for word, fixed in (
            ("localise", "localize"),
            ("localised", "localized"),
            ("localising", "localizing"),
            ("localisation", "localization"),
            ("localisable", "localizable"),
        ):
            with self.subTest(word=word):
                self.assertEqual(PAIRS.get(word), fixed)

    def test_a_word_that_is_ise_on_both_sides_is_not_a_stem(self):
        """Why the stems are curated by hand and not read off a suffix.

        Every one of these ends in `-ise` in US English too, and a rule that
        inflected them would rewrite ordinary prose into misspellings — which
        is the objection the module docstring raises to suffix rules, and it
        still stands. The expansion only inflects words a human already ruled
        on.
        """
        from homeautoshop.core.management.commands.check_spelling import PAIRS

        for word in (
            "advertise", "exercise", "surprise", "promise", "revise",
            "compromise", "supervise", "disguise", "improvise", "franchise",
        ):
            with self.subTest(word=word):
                self.assertNotIn(word, PAIRS)

    def test_analyses_is_deliberately_absent(self):
        """It is the British verb *and* the American plural of `analysis`,
        spelled the same. The fluids module is full of the noun."""
        from homeautoshop.core.management.commands.check_spelling import PAIRS

        self.assertIn("analyse", PAIRS)
        self.assertNotIn("analyses", PAIRS)

    def test_an_identifier_is_still_a_token_however_british_it_looks(self):
        """`work/views.py` holds a local named `quantised`, and the comment in
        `parts/models.py` that used to say `Quantised` was prose. The sweep
        corrected one of them, which is the correct count."""
        self.assertEqual(_found(prose("x.py", "quantised = value.normalize()\n")), [])
        self.assertEqual(
            _found(prose("x.py", "# Quantised, because the column refuses it\n")),
            [("Quantised", "quantized")],
        )


class ExceptionsAreWrittenDownTests(TestCase):
    def test_every_allowed_file_exists_and_carries_a_reason(self):
        """An exemption for a file nobody has any more is a hole in the net."""
        root = pathlib.Path(settings.BASE_DIR)
        for suffix, reason in ALLOWED.items():
            with self.subTest(path=suffix):
                self.assertTrue(reason.strip(), "an exception needs its reason")
                self.assertTrue(
                    any(p.as_posix().endswith(suffix) for p in root.rglob("*.py")),
                    f"{suffix} is exempted and does not exist",
                )
