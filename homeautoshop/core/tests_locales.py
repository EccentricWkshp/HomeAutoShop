"""
The translation catalogs (SPEC §5.6, NFR-A-*).

Four languages were offered in the picker and none of them was translated, so
choosing français (Canada) delivered English. These hold down the parts of
that being fixed which a script can actually check.

Two of them matter more than the rest:

* **Placeholders.** Django interpolates `%(name)s` at render time, so a
  translation that drops one is a `KeyError` on a live page in a language the
  author does not read. This is the failure that does not announce itself.
* **`.mo` against `.po`.** The compiled catalog is what ships — `.po` is
  excluded from the image by `.dockerignore` — so an edited `.po` that nobody
  recompiled means the running instance disagrees with the repository.

Nothing here asserts that a translation is *good*; no test can. The
catalogs say plainly that they are machine-drafted and want review.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.utils import translation
from django.utils.translation import gettext, ngettext

LOCALE_ROOT = Path(settings.BASE_DIR) / "locale"

#: The source language. Its catalog is deliberately absent: with no entries
#: to translate, gettext falls through to the msgid, which is already en-US.
SOURCE_LANGUAGE = "en-us"

PLACEHOLDER = re.compile(r"%\([^)]+\)[a-z]|%[sd]")


def forms(value) -> tuple[str, ...]:
    """Plural forms as a tuple, whichever container Babel used."""
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def catalogs():
    """Every `.po` on disk, read with Babel so no gettext binary is needed."""
    from babel.messages.pofile import read_po

    for path in sorted(LOCALE_ROOT.glob("*/LC_MESSAGES/django.po")):
        with path.open(encoding="utf-8") as handle:
            yield path, read_po(handle)


class CatalogsExistTests(TestCase):
    def test_every_language_offered_can_be_delivered(self):
        """A picker entry with no catalog behind it is a lie."""
        offered = {code for code, _name in settings.LANGUAGES}
        present = {path.parent.parent.name.lower().replace("_", "-") for path, _ in catalogs()}
        missing = offered - present - {SOURCE_LANGUAGE}
        self.assertFalse(missing, f"offered with no catalog: {sorted(missing)}")

    def test_the_source_language_has_no_catalog_of_its_own(self):
        """It would be 1,777 empty entries restated, and churn on every extract."""
        self.assertFalse((LOCALE_ROOT / "en_US").exists())

    def test_each_catalog_declares_its_plural_rule(self):
        """Getting this wrong is silent: the wrong branch, forever."""
        for path, catalog in catalogs():
            with self.subTest(catalog=path.parent.parent.name):
                self.assertTrue(catalog.plural_expr, "no Plural-Forms header")
                self.assertEqual(catalog.num_plurals, 2)


class PlaceholderTests(TestCase):
    """The failure that reaches a user as a 500, in a language you cannot read."""

    def test_no_translation_invents_or_drops_a_placeholder(self):
        for path, catalog in catalogs():
            for message in catalog:
                if not message.id or not any(forms(message.string)):
                    continue
                sources = message.id if isinstance(message.id, (list, tuple)) else [message.id]
                targets = (
                    message.string
                    if isinstance(message.string, (list, tuple))
                    else [message.string]
                )
                # A plural takes its arguments from `msgid_plural`, which is
                # what gettext holds every form to.
                expected = sorted(PLACEHOLDER.findall(sources[-1]))
                for index, target in enumerate(targets):
                    if not target:
                        continue
                    with self.subTest(catalog=path.parent.parent.name, msgid=sources[0]):
                        self.assertEqual(
                            sorted(PLACEHOLDER.findall(target)),
                            expected,
                            f"form {index} of {sources[0]!r}",
                        )

    def test_no_translation_mangles_a_brace_token(self):
        """`{plate}` and `{region}` are filled in by us, not by gettext."""
        brace = re.compile(r"\{[a-z_]+\}")
        for path, catalog in catalogs():
            for message in catalog:
                if not message.id or not message.string:
                    continue
                if isinstance(message.id, (list, tuple)):
                    continue
                with self.subTest(catalog=path.parent.parent.name, msgid=message.id):
                    self.assertEqual(
                        sorted(brace.findall(message.string)),
                        sorted(brace.findall(message.id)),
                    )


class CompiledCatalogTests(TestCase):
    """`.mo` is what ships; `.po` is what a person edits."""

    def test_every_catalog_is_compiled(self):
        for path, _catalog in catalogs():
            with self.subTest(catalog=path.parent.parent.name):
                self.assertTrue(
                    path.with_suffix(".mo").exists(),
                    "run: docker compose exec app python manage.py compilemessages",
                )

    def test_the_compiled_catalog_matches_its_source(self):
        """An edited `.po` nobody recompiled ships the previous translation."""
        from babel.messages.mofile import read_mo

        for path, catalog in catalogs():
            with path.with_suffix(".mo").open("rb") as handle:
                compiled = read_mo(handle)

            for message in catalog:
                if not message.id or "fuzzy" in message.flags:
                    continue
                # An untranslated plural reads as `('', '')` — a tuple
                # that is true. Ask whether any form has content.
                if not any(forms(message.string)):
                    continue
                key = message.id[0] if isinstance(message.id, (list, tuple)) else message.id
                with self.subTest(catalog=path.parent.parent.name, msgid=key):
                    self.assertIn(key, compiled, "in the .po and not in the .mo")
                    # Babel hands back a tuple from a .po and a list from a
                    # .mo for the same plural forms; compare the content.
                    self.assertEqual(forms(compiled[key].string), forms(message.string))

    def test_nothing_ships_marked_fuzzy(self):
        """A fuzzy entry is dropped by msgfmt, so it reads as untranslated.

        Which is fine as a working state and wrong as a shipped one: the
        picker offers the language either way.
        """
        for path, catalog in catalogs():
            fuzzy = [m.id for m in catalog if m.id and "fuzzy" in m.flags]
            with self.subTest(catalog=path.parent.parent.name):
                self.assertEqual(fuzzy, [], "needs review, then the flag removed")


class ItActuallyTranslatesTests(TestCase):
    """End to end, through the same call the templates make."""

    def test_a_word_from_every_corner_of_the_application(self):
        expected = {
            "fr-ca": {
                "Vehicles": "Véhicules",
                "Work orders": "Bons de travail",
                "Waiting on parts": "En attente de pièces",
                "Sign in": "Se connecter",
            },
            "es-mx": {
                "Vehicles": "Vehículos",
                "Work orders": "Órdenes de trabajo",
                "Waiting on parts": "Esperando refacciones",
                "Sign in": "Iniciar sesión",
            },
        }
        for code, pairs in expected.items():
            with translation.override(code):
                for source, target in pairs.items():
                    with self.subTest(language=code, source=source):
                        self.assertEqual(gettext(source), target)

    def test_canadian_english_differs_only_where_the_spelling_does(self):
        with translation.override("en-ca"):
            self.assertEqual(gettext("Totaled"), "Totalled")
            # The Canadian spelling is the subject of this assertion, not a
            # lapse: en-CA exists precisely to differ here. A repo-wide sweep
            # to American spelling must not touch it, which is what this line
            # caught the one time somebody tried.
            self.assertEqual(gettext("Not cataloged"), "Not catalogued")
            # Everything else falls through to the source, which is already
            # correct Canadian English.
            self.assertEqual(gettext("Work orders"), "Work orders")

    def test_meter_is_not_turned_into_the_unit(self):
        """Canadian keeps `meter` for a gauge and `metre` for the unit.

        Every use in this application is an odometer or an hour meter, so a
        blanket spelling rule would have been wrong.
        """
        with translation.override("en-ca"):
            self.assertEqual(gettext("Meter reading"), "Meter reading")

    def test_each_language_counts_the_way_it_counts(self):
        """French makes 0 singular; Spanish does not. The header decides."""
        with translation.override("fr-ca"):
            self.assertEqual(
                ngettext("%(counter)s vehicle", "%(counter)s vehicles", 0) % {"counter": 0},
                "0 véhicule",
            )
        with translation.override("es-mx"):
            self.assertEqual(
                ngettext("%(counter)s vehicle", "%(counter)s vehicles", 0) % {"counter": 0},
                "0 vehículos",
            )

    def test_a_rendered_page_comes_back_translated(self):
        from django.urls import reverse

        from homeautoshop.accounts.models import Role, User

        user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(user)
        for code, marker in (("fr-ca", "Véhicules"), ("es-mx", "Vehículos")):
            with self.subTest(language=code):
                page = self.client.get(
                    reverse("asset_list"), headers={"accept-language": code}
                ).content.decode()
                self.assertIn(marker, page)


class MachineDraftedTests(TestCase):
    """The catalogs have to say what they are."""

    def test_each_one_says_it_wants_review(self):
        for path, _catalog in catalogs():
            head = path.read_text(encoding="utf-8")[:2000]
            with self.subTest(catalog=path.parent.parent.name):
                self.assertIn("review", head.lower())

    def test_no_catalog_carries_a_personal_email(self):
        """These ship in an open repository."""
        address = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
        for path, _catalog in catalogs():
            found = {
                hit
                for hit in address.findall(path.read_text(encoding="utf-8"))
                if not hit.endswith(("example.com", "example.invalid", "ejemplo.com"))
            }
            with self.subTest(catalog=path.parent.parent.name):
                self.assertEqual(found, set())
