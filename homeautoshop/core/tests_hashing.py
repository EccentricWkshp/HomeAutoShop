"""Password hashing is fast in the suite and slow in production (SPEC §12.2).

Making the test suite fast meant weakening the one setting whose whole value is
being slow, so the trade needs a check on both halves — otherwise "tests use
MD5" quietly becomes "we use MD5".

The production half cannot be asserted from `django.conf.settings`, because by
the time a test runs the runner has already replaced the value. So it is read
off `config/settings.py` as written on disk, which is the thing that actually
ships.
"""

from __future__ import annotations

import ast
from pathlib import Path

from django.conf import settings
from django.test import TestCase

from homeautoshop.accounts.models import User

SETTINGS_FILE = Path(settings.BASE_DIR) / "config" / "settings.py"


def assignments(name: str) -> list:
    """Every module-level assignment of `name` in the settings source."""
    tree = ast.parse(SETTINGS_FILE.read_text(encoding="utf-8"))
    found = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    found.append(ast.literal_eval(node.value))
    return found


def declared(name: str):
    """The value assigned to `name` in the settings module's source."""
    found = assignments(name)
    if not found:
        raise AssertionError(f"{name} is not assigned at module level in settings.py")
    return found[0]


class ProductionHashingTests(TestCase):
    def test_a_deployment_still_hashes_with_argon2(self):
        """The runner's override is scoped to the runner; this is what ships."""
        self.assertEqual(
            declared("PASSWORD_HASHERS")[0],
            "django.contrib.auth.hashers.Argon2PasswordHasher",
        )

    def test_no_fast_hasher_is_reachable_in_production(self):
        """A hasher only has to be *listed* to be accepted on an existing hash,
        so a fast one left in the production list would let anybody who could
        write a hash choose how cheaply it is checked."""
        for hasher in declared("PASSWORD_HASHERS"):
            self.assertNotIn("MD5", hasher)
            self.assertNotIn("Unsalted", hasher)
            self.assertNotIn("CryptPasswordHasher", hasher)

    def test_the_speed_up_is_actually_in_effect(self):
        """Otherwise this file documents a saving nobody is getting."""
        self.assertEqual(settings.PASSWORD_HASHERS, ["django.contrib.auth.hashers.MD5PasswordHasher"])

    def test_the_runner_that_does_it_is_the_configured_one(self):
        self.assertEqual(declared("TEST_RUNNER"), "homeautoshop.core.testrunner.Runner")

    def test_settings_names_exactly_one_test_runner(self):
        """A second assignment silently wins, and the loser here would be the
        guard that keeps the suite off the network — a failure that shows up as
        tests passing, which is the kind nobody goes looking for. Adding one is
        how the hashing change was nearly made, so it is worth a check."""
        self.assertEqual(len(assignments("TEST_RUNNER")), 1)

    def test_the_network_guard_still_belongs_to_that_runner(self):
        """The hashing override was folded into the existing runner rather than
        shipped as a second one, so the two behaviors cannot be separated by
        accident."""
        from homeautoshop.core.testrunner import Runner

        self.assertTrue(hasattr(Runner, "setup_test_environment"))
        self.assertIn("NetworkUsedInTests", Path(
            settings.BASE_DIR, "homeautoshop", "core", "testrunner.py"
        ).read_text(encoding="utf-8"))

    def test_passwords_still_round_trip(self):
        """A cheaper hash is still a hash — logging in has to keep working, or
        the saving has been bought by turning the auth tests into no-ops."""
        user = User.objects.create_user(username="andy", password="correct horse battery")
        self.assertTrue(user.check_password("correct horse battery"))
        self.assertFalse(user.check_password("something else"))
        self.assertNotIn("correct horse battery", user.password)
