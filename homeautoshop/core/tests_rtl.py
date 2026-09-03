"""
Right-to-left layout (SPEC §5.6, roadmap R-8).

§5.6 said the layout was built for RTL and that only verification was
deferred. Verifying it found that the claim was *almost* true and worth
nothing: the stylesheet was overwhelmingly logical — twelve physical
declarations out of a few hundred — but `<html>` carried no `dir` attribute at
all, and a logical property with no declared direction resolves left-to-right.
So every `margin-inline-start` in the file was doing exactly what
`margin-left` would have done, and no amount of reading the stylesheet could
have shown that.

The same render also showed `lang="en"` on a page served in French: the i18n
context processor was never installed, so `{{ LANGUAGE_CODE }}` was empty and
the template's `|default:'en'` covered for it. That is WCAG 3.1.1, and it had
been silently wrong since the template was written.

Two halves, because RTL has two halves:

* **`check_rtl`** holds the stylesheet to logical properties. A browser is the
  only thing that can say a page *looks* right mirrored; a checker can say
  that nothing in it is nailed to one side, which is the mechanical half.
* **The render tests** below prove the direction actually reaches the page,
  which is the half the stylesheet cannot state about itself.

No RTL catalog ships — the §5.6 ship set is North America — so the render
tests add a language rather than translating one. That is the honest shape of
this: what is being verified is the *layout*, and the layout does not care
which words are in it.
"""

from __future__ import annotations

from pathlib import Path

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import translation
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.core.management.commands.check_rtl import scan_css, scan_markup

#: Arabic, added to the picker for the duration of a test. Django's own
#: `LANG_INFO` already knows it is bidirectional, which is what
#: `LANGUAGE_BIDI` reads — no catalog is needed to ask which way a page runs.
RTL_LANGUAGES = [
    ("en-us", "English (United States)"),
    ("ar", "العربية"),
]

HERE = Path("page")


def css(text: str):
    return scan_css(text, HERE)


def markup(text: str):
    return scan_markup(text, HERE)


class DirectionReachesThePageTests(TestCase):
    """The half a stylesheet cannot state about itself."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        # These cases render pages in several languages, and
        # `LocaleMiddleware` activates one per request without ever
        # deactivating — so the last one would outlive the case and be
        # inherited by whatever ran next.
        self.addCleanup(translation.deactivate)

    def _root_tag(self, language: str) -> str:
        page = self.client.get(
            reverse("asset_list"), headers={"accept-language": language}
        ).content.decode()
        return page[page.index("<html") : page.index(">", page.index("<html")) + 1]

    def test_a_left_to_right_page_says_so(self):
        self.assertIn('dir="ltr"', self._root_tag("en-us"))

    @override_settings(LANGUAGES=RTL_LANGUAGES)
    def test_a_right_to_left_language_turns_the_document_round(self):
        """Without this the logical properties are decoration."""
        self.assertIn('dir="rtl"', self._root_tag("ar"))

    def test_the_page_declares_the_language_it_is_actually_in(self):
        """It declared `lang="en"` in French. WCAG 3.1.1, and invisible."""
        self.assertIn('lang="fr-ca"', self._root_tag("fr-ca"))
        self.assertIn('lang="es-mx"', self._root_tag("es-mx"))

    def test_the_shipped_languages_are_all_left_to_right(self):
        """None of the four is bidirectional, so none may claim to be."""
        for code in ("en-us", "en-ca", "fr-ca", "es-mx"):
            with self.subTest(language=code):
                self.assertIn('dir="ltr"', self._root_tag(code))


class TheCheckerCatchesItTests(TestCase):
    """A gate nobody has watched fail is a gate nobody knows is open."""

    def test_a_physical_margin(self):
        found = css(".a { margin-left: 1rem; }")
        self.assertEqual(len(found), 1)
        self.assertIn("margin-inline-start", found[0].why)

    def test_a_physical_border_keeps_its_suffix_in_the_advice(self):
        found = css(".a { border-left-color: red; }")
        self.assertEqual(len(found), 1)
        self.assertIn("border-inline-start-color", found[0].why)

    def test_a_physical_text_alignment(self):
        """The one that would have turned every table in the app round."""
        found = css("th, td { text-align: left; }")
        self.assertEqual(len(found), 1)
        self.assertIn("text-align: start", found[0].why)

    def test_a_bare_positioning_inset(self):
        found = css(".skip { position: absolute; left: -9999px; }")
        self.assertEqual(len(found), 1)
        self.assertIn("inset-inline-start", found[0].why)

    def test_a_physical_corner_radius(self):
        self.assertEqual(len(css(".a { border-top-left-radius: 4px; }")), 1)

    def test_a_four_value_shorthand_hiding_a_padding_left(self):
        """`padding: 0 0 .75rem 1rem` is the one grep never finds."""
        found = css(".a { padding: 0 0 .75rem 1rem; }")
        self.assertEqual(len(found), 1)
        self.assertIn("four-value padding", found[0].why)

    def test_an_inline_style_in_a_template(self):
        found = markup('<td style="padding-left:1.25rem">x</td>')
        self.assertEqual(len(found), 1)
        self.assertIn("inline style", found[0].why)

    def test_a_document_with_no_direction(self):
        found = markup('<!doctype html>\n<html lang="en">\n<body></body></html>')
        self.assertEqual(len(found), 1)
        self.assertIn("no dir", found[0].why)

    def test_the_reported_line_survives_a_comment_above_it(self):
        """Blanked, not stripped, so the number still points at the fault."""
        found = css("/* one\ntwo\nthree */\n.a { margin-left: 1rem; }")
        self.assertEqual(found[0].line, 4)


class TheCheckerIsQuietWhereItShouldBeTests(TestCase):
    """A check that cries wolf is a check somebody switches off."""

    def test_logical_properties_pass(self):
        self.assertEqual(
            css(
                ".a { margin-inline-start: 1rem; border-inline-end: 1px solid red;"
                " padding-inline-start: 2px; inset-inline-start: 0; text-align: start; }"
            ),
            [],
        )

    def test_the_block_axis_is_not_the_business_of_this_check(self):
        """`top` and `margin-bottom` mean the same thing mirrored."""
        self.assertEqual(
            css(".a { top: 1rem; margin-bottom: .5rem; border-bottom: 1px solid red; }"),
            [],
        )

    def test_a_symmetric_four_value_shorthand_passes(self):
        """`inset: auto 0 0 0` says the same on both sides. It is fine."""
        self.assertEqual(css(".bar { inset: auto 0 0 0; }"), [])

    def test_a_shorthand_with_fewer_than_four_values_passes(self):
        self.assertEqual(css(".a { padding: 1rem 1rem 6rem; margin: 0 auto; }"), [])

    def test_calc_is_not_split_into_extra_values(self):
        """Three values, not five — the parenthesis has to hold together."""
        self.assertEqual(
            css(".a { padding: .25rem .25rem calc(.25rem + env(safe-area-inset-bottom)); }"),
            [],
        )

    def test_a_comment_explaining_a_rule_does_not_break_it(self):
        """Otherwise the fix is to mangle the explanation."""
        self.assertEqual(css("/* margin-left: 1rem is wrong here */\n.a { color: red; }"), [])
        self.assertEqual(
            markup('{# never write style="text-align:left" #}\n<p>x</p>'), []
        )

    def test_a_template_fragment_needs_no_html_element(self):
        """Most templates extend `base.html` and have no document of their own."""
        self.assertEqual(markup("{% extends 'base.html' %}\n{% block main %}x{% endblock %}"), [])


class TheRepositoryPassesTests(TestCase):
    """The gate, over what actually ships."""

    def test_nothing_in_the_stylesheet_or_the_templates_is_nailed_to_one_side(self):
        call_command("check_rtl")
