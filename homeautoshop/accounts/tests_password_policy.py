"""`PASSWORD_POLICY` — how hard a password has to be, and whether there is one.

§12.2 argues for a length floor rather than composition rules, and that is still
the default. The setting exists because the threat model is not the same in
every installation: twelve characters per sign-in on a laptop nobody else can
reach defends against an attacker already in the room, and a toll like that gets
paid in passwords people can type.

The far end of the scale is `noauth`, which is not a password rule at all but
the absence of sign-in. It is tested here rather than somewhere else because it
is the same operator decision on the same scale, set by the same variable.
"""

from __future__ import annotations

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.contrib.auth.password_validation import validate_password
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.accounts.validators import (
    CharacterClassValidator, DEFAULT_POLICY, POLICIES, WEAK_POLICIES, validators_for,
)


def check(password: str, policy: str, user=None) -> list[str]:
    """The complaints a policy makes about a password, as plain strings."""
    with override_settings(AUTH_PASSWORD_VALIDATORS=validators_for(policy)):
        try:
            validate_password(password, user)
        except ValidationError as exc:
            return list(exc.messages)
    return []


class WhatEachPolicyAcceptsTests(TestCase):
    def test_the_default_is_the_twelve_character_floor(self):
        self.assertEqual(DEFAULT_POLICY, "12chars")

    def test_twelve_chars_refuses_a_short_one_and_takes_a_long_one(self):
        self.assertTrue(check("short", "12chars"))
        self.assertEqual(check("correct-horse-battery", "12chars"), [])

    def test_and_still_refuses_a_long_common_one(self):
        """The floor is not the only rule at the default. A row of twelve ones
        clears the length check and is exactly what a length-only rule
        invites — it is on Django's common list, and so are 189 others long
        enough to pass twelve characters."""
        self.assertTrue(check("111111111111", "12chars"))

    def test_six_chars_takes_something_you_can_type_one_handed(self):
        self.assertEqual(check("shop12", "6chars"), [])
        self.assertTrue(check("shop", "6chars"))

    def test_and_deliberately_does_not_consult_the_common_list(self):
        """At six characters a dictionary check refuses most of what somebody
        choosing this policy is trying to choose. Saying so in a test because
        it is a decision, not an omission."""
        self.assertEqual(check("monkey", "6chars"), [])

    def test_any_takes_anything_at_all(self):
        for password in ("x", "1", "password"):
            with self.subTest(password=password):
                self.assertEqual(check(password, "any"), [])

    def test_noauth_has_no_password_rules_because_it_has_no_passwords(self):
        self.assertEqual(POLICIES["noauth"], [])

    def test_complex_wants_length_and_kinds_of_character(self):
        self.assertTrue(check("alllowercaseletters", "complex"))
        self.assertEqual(check("Correct-Horse-Battery-7", "complex"), [])

    def test_and_refuses_one_that_is_only_numbers(self):
        self.assertTrue(check("64738291056473829105", "complex"))

    def test_and_one_that_looks_like_the_account_name(self):
        user = User(username="administrator", email="andy@example.com")
        self.assertTrue(check("Administrator12!", "complex", user))


class TheClassesValidatorTests(TestCase):
    """Counted as kinds *present* rather than as a list of required ones, so a
    passphrase with a capital and a digit passes and nobody has to end every
    password with an exclamation mark."""

    def setUp(self):
        self.validator = CharacterClassValidator(min_classes=3)

    def test_three_kinds_is_enough_without_a_symbol(self):
        self.assertIsNone(self.validator.validate("Correcthorse7"))

    def test_two_kinds_is_not(self):
        with self.assertRaises(ValidationError):
            self.validator.validate("correcthorse7")

    def test_a_symbol_can_be_the_third(self):
        self.assertIsNone(self.validator.validate("Correct-horse"))

    def test_but_punctuation_alone_is_not_a_second_and_third(self):
        """`-` and `!` are one kind between them, not two."""
        with self.assertRaises(ValidationError):
            self.validator.validate("correct-horse!")

    def test_it_says_what_it_wants(self):
        self.assertIn("kinds of character", self.validator.get_help_text())


class AMisspelledPolicyTests(TestCase):
    def test_refuses_to_start_rather_than_falling_back(self):
        """In whichever direction the operator guessed, a silent fallback
        leaves them believing something untrue about their own instance."""
        with self.assertRaises(ImproperlyConfigured):
            validators_for("6char")

    def test_and_names_what_it_would_have_accepted(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            validators_for("relaxed")

        message = str(caught.exception)
        self.assertIn("relaxed", message)
        for known in POLICIES:
            self.assertIn(known, message)

    def test_the_weak_ones_are_named_in_one_place(self):
        """The health screen, the banner and the startup warning all read this,
        so they cannot drift apart about which policy is which."""
        self.assertEqual(WEAK_POLICIES, {"noauth", "any", "6chars"})
        self.assertNotIn(DEFAULT_POLICY, WEAK_POLICIES)


@override_settings(NO_AUTHENTICATION=True)
class NoAuthTests(TestCase):
    """Anyone who can reach the site is the shop. That is the whole feature."""

    def middleware(self):
        from homeautoshop.accounts.middleware import NoAuthMiddleware

        return NoAuthMiddleware(lambda request: request)

    def request_from(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return self.middleware()(request)

    def test_an_anonymous_request_becomes_the_administrator(self):
        admin = User.objects.create_user("root", password="x" * 16, role=Role.ADMIN)

        request = self.request_from(AnonymousUser())

        self.assertEqual(request.user, admin)
        self.assertTrue(request.user.is_authenticated)

    def test_a_real_session_stays_its_own_account(self):
        """Somebody who signed in before the policy changed is still them, not
        the stand-in."""
        User.objects.create_user("root", password="x" * 16, role=Role.ADMIN)
        member = User.objects.create_user("andy", password="x" * 16)

        request = self.request_from(member)

        self.assertEqual(request.user, member)

    def test_with_no_administrator_it_stays_anonymous(self):
        """Turning sign-in off cannot conjure the account it would sign you in
        as, and the first-run setup page still has to be reachable."""
        request = self.request_from(AnonymousUser())

        self.assertFalse(request.user.is_authenticated)

    def test_an_inactive_administrator_is_not_used(self):
        User.objects.create_user("root", password="x" * 16, role=Role.ADMIN, is_active=False)

        request = self.request_from(AnonymousUser())

        self.assertFalse(request.user.is_authenticated)

    def test_the_oldest_administrator_wins_so_it_is_stable(self):
        first = User.objects.create_user("root", password="x" * 16, role=Role.ADMIN)
        User.objects.create_user("second", password="x" * 16, role=Role.ADMIN)

        self.assertEqual(self.request_from(AnonymousUser()).user, first)

    def test_a_member_is_not_promoted_into_the_stand_in(self):
        User.objects.create_user("andy", password="x" * 16, role=Role.MEMBER)

        request = self.request_from(AnonymousUser())

        self.assertFalse(request.user.is_authenticated)


class TheInstanceSaysSoTests(TestCase):
    """A setting this consequential is not allowed to be invisible."""

    def setUp(self):
        self.admin = User.objects.create_user("root", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(self.admin)

    def test_the_health_screen_names_the_policy(self):
        response = self.client.get(reverse("health"))

        self.assertContains(response, "12chars")

    @override_settings(NO_AUTHENTICATION=True, PASSWORD_POLICY="noauth")
    def test_and_says_plainly_when_there_is_no_sign_in(self):
        response = self.client.get(reverse("health"))

        self.assertContains(response, "Anyone who can reach this site is the shop")

    @override_settings(NO_AUTHENTICATION=True)
    def test_every_page_carries_the_banner(self):
        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "Sign-in is off")

    def test_and_does_not_when_sign_in_is_on(self):
        response = self.client.get(reverse("dashboard"))

        self.assertNotContains(response, "Sign-in is off")
