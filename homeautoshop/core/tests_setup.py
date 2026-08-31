"""
First run (SPEC FR-ADM-1).

Most of this file is about the gate rather than the form, and deliberately so.
A page that creates an administrator with no authentication in front of it is
the most dangerous route in the application: reachable once, it is the setup
wizard; reachable twice, it is a way for anyone who can see the site to make
themselves an owner of it. So the tests that matter are the ones that try to
reach it a second time, by every route that exists.

The rest covers what the wizard is *for*: an account whose role and superuser
flag agree, without the second command `INSTALL.md` used to have to give.
"""

from __future__ import annotations

from django.test import TestCase
from django.test import override_settings
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.core.runtime import conf

GOOD = {
    "username": "andy",
    "display_name": "Andy",
    "email": "andy@example.invalid",
    "password1": "correct horse battery staple",
    "password2": "correct horse battery staple",
    "SHOP_NAME": "Eccentric Workshop",
    "UNITS": "imperial",
    "CURRENCY_REPORTING": "USD",
    "TIME_ZONE": "America/Toronto",
    "LANGUAGE_CODE": "en-us",
    "install_starter_data": "",
}


class TheGateTests(TestCase):
    """It has to be impossible to reach a second time."""

    def test_it_is_offered_while_there_is_nobody(self):
        self.assertEqual(self.client.get(reverse("setup")).status_code, 200)

    def test_it_is_gone_the_moment_an_account_exists(self):
        User.objects.create_user(username="someone", password="x" * 16)
        response = self.client.get(reverse("setup"))
        self.assertRedirects(response, reverse("login"))

    def test_posting_to_it_afterwards_creates_nobody(self):
        """The check has to be on the POST too, not only on the page."""
        User.objects.create_user(username="someone", password="x" * 16)
        self.client.post(reverse("setup"), dict(GOOD, username="intruder"))
        self.assertFalse(User.objects.filter(username="intruder").exists())

    def test_a_disabled_account_still_closes_it(self):
        """Deactivating the only user must not reopen the door.

        Users are deactivated rather than deleted (FR-ADM-2), so a gate that
        asked about *active* accounts would swing open the first time somebody
        locked themselves out.
        """
        User.objects.create_user(username="someone", password="x" * 16, is_active=False)
        self.assertRedirects(self.client.get(reverse("setup")), reverse("login"))

    def test_finishing_closes_it_behind_itself(self):
        self.client.post(reverse("setup"), GOOD)
        self.client.logout()
        self.assertRedirects(self.client.get(reverse("setup")), reverse("login"))

    def test_two_people_racing_cannot_both_become_owners(self):
        """The second submission of a form both of them loaded."""
        first = self.client.post(reverse("setup"), GOOD)
        self.assertEqual(first.status_code, 302)

        other = self.client_class()
        other.post(reverse("setup"), dict(GOOD, username="second"))
        self.assertFalse(User.objects.filter(username="second").exists())
        self.assertEqual(User.objects.count(), 1)


class SignInTests(TestCase):
    """A form nobody can pass is not the right first screen."""

    def test_a_fresh_instance_sends_you_to_the_wizard(self):
        self.assertRedirects(self.client.get(reverse("login")), reverse("setup"))

    def test_once_set_up_the_sign_in_page_is_the_sign_in_page(self):
        User.objects.create_user(username="someone", password="x" * 16)
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in")


class TheAccountTests(TestCase):
    def setUp(self):
        self.response = self.client.post(reverse("setup"), GOOD)
        self.user = User.objects.get(username="andy")

    def test_the_role_and_the_flag_agree(self):
        """The whole reason this is not two documented commands.

        `createsuperuser` sets the superuser flag and leaves `role` saying
        member. Everything works until somebody clears the flag, at which
        point the account is quietly demoted.
        """
        self.assertEqual(self.user.role, Role.ADMIN)
        self.assertTrue(self.user.is_superuser)
        self.assertTrue(self.user.is_staff)

    def test_the_password_is_stored_as_a_hash(self):
        self.assertNotIn("correct horse", self.user.password)
        self.assertTrue(self.user.check_password("correct horse battery staple"))

    def test_you_are_signed_in_when_it_finishes(self):
        self.assertRedirects(self.response, reverse("dashboard"))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_your_name_is_kept(self):
        self.assertEqual(self.user.first_name, "Andy")


@override_settings(
    SHOP_NAME="Home Shop",
    UNITS="metric",
    CURRENCY_REPORTING="CAD",
    TIME_ZONE="UTC",
    LANGUAGE_CODE="en-us",
)
class TheAnswersTests(TestCase):
    """The starting point is pinned, because `.env` is read during tests.

    `config/settings.py` calls `load_dotenv`, so without this the assertions
    below are about whatever the person running them happens to have
    configured — and "the value changed" cannot be checked against a before
    state that differs per machine.
    """

    def test_the_shop_answers_are_stored_and_in_effect(self):
        self.client.post(reverse("setup"), GOOD)
        self.assertEqual(conf.SHOP_NAME, "Eccentric Workshop")
        self.assertEqual(conf.UNITS, "imperial")
        self.assertEqual(conf.TIME_ZONE, "America/Toronto")

    def test_they_are_recorded_as_having_come_from_setup(self):
        """An audit log that says a value appeared from nowhere is worse than none."""
        from homeautoshop.core.models import AuditLog

        self.client.post(reverse("setup"), GOOD)
        entry = AuditLog.objects.filter(summary="SHOP_NAME").first()
        self.assertIsNotNone(entry, "the shop name was set by nobody, as far as the log knows")
        self.assertEqual(entry.source, "setup")
        self.assertEqual(entry.user.username, "andy")

    def test_the_form_starts_from_what_the_environment_already_says(self):
        """A `.env` that set these should not have to be retyped."""
        page = self.client.get(reverse("setup")).content.decode()
        self.assertIn('value="Home Shop"', page)
        self.assertIn('<option value="metric" selected>', page)

    def test_a_rejected_answer_creates_no_account_at_all(self):
        """Both halves or neither: an admin nobody chose the units for is worse."""
        self.client.post(reverse("setup"), dict(GOOD, TIME_ZONE="Mars/Olympus"))
        self.assertEqual(User.objects.count(), 0)

    def test_a_rejected_password_creates_no_account(self):
        self.client.post(reverse("setup"), dict(GOOD, password1="x", password2="x"))
        self.assertEqual(User.objects.count(), 0)

    def test_the_two_passwords_have_to_match(self):
        response = self.client.post(
            reverse("setup"), dict(GOOD, password2="something else entirely")
        )
        self.assertEqual(User.objects.count(), 0)
        self.assertContains(response, "did not match")


class StarterDataTests(TestCase):
    def test_it_can_be_asked_for(self):
        from homeautoshop.maintenance.models import ScheduleTemplate

        self.client.post(reverse("setup"), dict(GOOD, install_starter_data="on"))
        self.assertTrue(ScheduleTemplate.objects.exists())

    def test_it_can_be_declined(self):
        from homeautoshop.maintenance.models import ScheduleTemplate

        self.client.post(reverse("setup"), GOOD)
        self.assertFalse(ScheduleTemplate.objects.exists())


class TlsTrustTests(TestCase):
    """The last chance to say this before somebody walks out to the garage."""

    @override_settings(TLS_MODE="internal")
    def test_a_self_issued_certificate_says_each_device_needs_the_root(self):
        page = self.client.get(reverse("setup")).content.decode()
        self.assertIn("root certificate", page)
        self.assertIn("root.crt", page)

    @override_settings(TLS_MODE="acme-dns")
    def test_a_real_certificate_says_there_is_nothing_to_do(self):
        page = self.client.get(reverse("setup")).content.decode()
        self.assertIn("Nothing to install", page)
        self.assertNotIn("root.crt", page)

    @override_settings(TLS_MODE="custom")
    def test_your_own_certificate_says_it_depends_on_who_issued_it(self):
        self.assertIn("who issued it", self.client.get(reverse("setup")).content.decode())

    @override_settings(TLS_MODE="")
    def test_an_unset_mode_assumes_the_one_that_needs_explaining(self):
        """`internal` is the compose default and the only one with a step in it."""
        self.assertIn("root.crt", self.client.get(reverse("setup")).content.decode())
