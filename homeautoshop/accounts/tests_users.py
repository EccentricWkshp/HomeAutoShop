"""
Managing who can sign in (SPEC FR-ADM-2).

Two things here are worth more than the rest.

**Nobody is deleted.** Every reference to a user is `on_delete=SET_NULL`, so
deleting one would not remove the work they recorded — it would detach their
name from it, silently and permanently, while the screen looked tidier
afterwards. The audit log exists to answer *who changed the odometer to
300,000*; a delete button makes that unanswerable. So there is no delete
route, and a test says so by asking the URL resolver.

**You cannot lock the shop out of itself.** Demoting or deactivating the last
administrator leaves no user screen, no settings, and no way back except a
shell on the host. That is the one failure in this file that cannot be
recovered from inside the application.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.people.models import Person


class Fixture(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN, is_superuser=True, is_staff=True
        )
        self.member = User.objects.create_user(
            username="sam", password="x" * 16, role=Role.MEMBER
        )
        self.client.force_login(self.admin)

    def make_second_admin(self) -> User:
        return User.objects.create_user(
            username="jo", password="x" * 16, role=Role.ADMIN, is_superuser=True, is_staff=True
        )


class WhoCanSeeItTests(Fixture):
    def test_an_administrator_can(self):
        self.assertEqual(self.client.get(reverse("user_list")).status_code, 200)

    def test_a_member_cannot(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("user_list")).status_code, 403)

    def test_a_member_cannot_reach_one_account_either(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("user_detail", args=[self.admin.pk]))
        self.assertEqual(response.status_code, 403)

    def test_a_member_cannot_promote_themselves(self):
        self.client.force_login(self.member)
        self.client.post(
            reverse("user_detail", args=[self.member.pk]),
            {"first_name": "", "email": "", "role": Role.ADMIN, "person": ""},
        )
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, Role.MEMBER)


class NobodyIsDeletedTests(Fixture):
    def test_there_is_no_delete_route(self):
        """Stated as a test because it is a decision, not an omission."""
        with self.assertRaises(NoReverseMatch):
            reverse("user_delete", args=[self.member.pk])

    def test_the_screen_says_why(self):
        page = self.client.get(reverse("user_detail", args=[self.member.pk])).content.decode()
        self.assertIn("no delete", page.lower())

    def test_deactivating_keeps_what_they_recorded(self):
        from homeautoshop.assets.models import Asset

        asset = Asset.objects.create(nickname="Red truck", created_by=self.member)
        self.client.post(reverse("user_set_active", args=[self.member.pk]), {"active": "0"})

        asset.refresh_from_db()
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertEqual(asset.created_by_id, self.member.pk)


class NotLockingYourselfOutTests(Fixture):
    def test_the_last_administrator_cannot_be_demoted(self):
        response = self.client.post(
            reverse("user_detail", args=[self.admin.pk]),
            {"first_name": "", "email": "", "role": Role.MEMBER, "person": ""},
            follow=True,
        )
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, Role.ADMIN)
        self.assertContains(response, "no administrator")

    def test_the_last_administrator_cannot_be_deactivated(self):
        second = self.make_second_admin()
        self.client.force_login(second)
        self.client.post(reverse("user_set_active", args=[second.pk]), {"active": "0"})
        # `second` is refused for being you; make the first one the target.
        self.client.post(reverse("user_set_active", args=[self.admin.pk]), {"active": "0"})
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.is_active)  # allowed: `second` is still an admin

        response = self.client.post(
            reverse("user_set_active", args=[second.pk]), {"active": "0"}, follow=True
        )
        second.refresh_from_db()
        self.assertTrue(second.is_active)
        self.assertContains(response, "your own account")

    def test_demoting_is_allowed_once_somebody_else_is_an_administrator(self):
        self.make_second_admin()
        self.client.post(
            reverse("user_detail", args=[self.admin.pk]),
            {"first_name": "", "email": "", "role": Role.MEMBER, "person": ""},
        )
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, Role.MEMBER)

    def test_you_cannot_deactivate_yourself(self):
        """Recoverable by somebody else, and almost always a mis-tap."""
        self.make_second_admin()
        response = self.client.post(
            reverse("user_set_active", args=[self.admin.pk]), {"active": "0"}, follow=True
        )
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)
        self.assertContains(response, "your own account")

    def test_the_button_is_disabled_for_the_last_administrator(self):
        page = self.client.get(reverse("user_detail", args=[self.admin.pk])).content.decode()
        self.assertIn("disabled", page)
        self.assertIn("only administrator", page)


class AddingSomebodyTests(Fixture):
    def post(self, **overrides):
        data = {
            "username": "kim",
            "first_name": "Kim",
            "email": "",
            "role": Role.MEMBER,
            "person": "",
            "password1": "correct horse battery staple",
            "password2": "correct horse battery staple",
        }
        data.update(overrides)
        return self.client.post(reverse("user_create"), data, follow=True)

    def test_they_can_sign_in_with_what_was_set(self):
        self.post()
        made = User.objects.get(username="kim")
        self.assertTrue(made.check_password("correct horse battery staple"))
        self.assertTrue(made.is_active)

    def test_the_password_is_never_stored_as_typed(self):
        self.post()
        self.assertNotIn("correct horse", User.objects.get(username="kim").password)

    def test_the_two_passwords_have_to_match(self):
        response = self.post(password2="something else")
        self.assertFalse(User.objects.filter(username="kim").exists())
        self.assertContains(response, "did not match")

    def test_a_weak_password_is_refused_here_as_anywhere_else(self):
        """The wizard and this screen share one rule, so neither surprises."""
        response = self.post(password1="x", password2="x")
        self.assertFalse(User.objects.filter(username="kim").exists())
        self.assertEqual(response.status_code, 200)

    def test_a_taken_username_is_refused_whatever_its_case(self):
        response = self.post(username="ANDY")
        self.assertContains(response, "taken")

    def test_they_can_be_linked_to_a_person(self):
        someone = Person.objects.create(display_name="Kim")
        self.post(person=str(someone.pk))
        self.assertEqual(User.objects.get(username="kim").person_id, someone.pk)

    def test_a_person_already_linked_is_not_offered_again(self):
        taken = Person.objects.create(display_name="Sam")
        self.member.person = taken
        self.member.save(update_fields=["person"])
        page = self.client.get(reverse("user_create")).content.decode()
        self.assertNotIn(str(taken.pk), page)


class RoleAndDjangoFlagsTests(Fixture):
    """The disagreement `INSTALL.md` had to warn about, prevented here."""

    def test_an_administrator_made_here_has_both_halves(self):
        self.client.post(
            reverse("user_create"),
            {
                "username": "kim",
                "first_name": "",
                "email": "",
                "role": Role.ADMIN,
                "person": "",
                "password1": "correct horse battery staple",
                "password2": "correct horse battery staple",
            },
        )
        made = User.objects.get(username="kim")
        self.assertEqual(made.role, Role.ADMIN)
        self.assertTrue(made.is_superuser)
        self.assertTrue(made.is_staff)

    def test_promoting_sets_the_flags_too(self):
        self.client.post(
            reverse("user_detail", args=[self.member.pk]),
            {"first_name": "", "email": "", "role": Role.ADMIN, "person": ""},
        )
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_superuser)

    def test_demoting_clears_them(self):
        self.make_second_admin()
        self.client.post(
            reverse("user_detail", args=[self.admin.pk]),
            {"first_name": "", "email": "", "role": Role.MEMBER, "person": ""},
        )
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.is_superuser)
        self.assertFalse(self.admin.is_staff)


class ResettingAPasswordTests(Fixture):
    def test_it_replaces_what_they_had(self):
        self.client.post(
            reverse("user_set_password", args=[self.member.pk]),
            {"password1": "a whole new pass phrase", "password2": "a whole new pass phrase"},
        )
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password("a whole new pass phrase"))

    def test_a_mismatch_changes_nothing(self):
        response = self.client.post(
            reverse("user_set_password", args=[self.member.pk]),
            {"password1": "a whole new pass phrase", "password2": "not that one at all"},
            follow=True,
        )
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password("x" * 16))
        self.assertContains(response, "did not match")

    def test_changing_your_own_does_not_sign_you_out(self):
        """Otherwise the next click is a login page with no explanation."""
        self.client.post(
            reverse("user_set_password", args=[self.admin.pk]),
            {"password1": "a whole new pass phrase", "password2": "a whole new pass phrase"},
        )
        self.assertEqual(self.client.get(reverse("user_list")).status_code, 200)


class TheMenuTests(Fixture):
    def test_an_administrator_is_offered_the_screen(self):
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(reverse("user_list"), page)

    def test_a_member_is_not(self):
        self.client.force_login(self.member)
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn(reverse("user_list"), page)
