"""Removing an account that never did anything (SPEC FR-ADM-2, FR-ADM-8).

Reported as: *"testing is going to leave an instance littered with fragments
and the only current way to fix that is to wipe it and start over."*

Deactivation was the only answer, and it is the right answer for somebody who
worked here — their name belongs on what they did. It is the wrong answer for
an account created by mistake or while trying the application out, which has
no history to protect and could previously only be hidden, never removed.

The gate is the narrowest one that solves it: **nothing in the shop may carry
their name.** Risk is then near zero by construction, because there is nothing
to lose. It is the same shape FR-ADM-8 already uses for vendors and locations.

The load-bearing test here is `test_a_created_by_field_alone_is_enough`.
`User` has 57 relations and about fifty are `created_by` audit fields declared
`related_name="+"` — hidden relations, absent from `_meta.related_objects` and
invisible to a grep. They are also precisely the record that somebody did
something. A hand-written list of what to check would have missed every one.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import ApiToken, AssetAccess, Role, User
from homeautoshop.accounts.services import traces
from homeautoshop.assets.models import Asset
from homeautoshop.parts.models import Part
from homeautoshop.work.models import TimeEntry, WorkOrder


class Base(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.spare = User.objects.create_user(
            username="second", password="x" * 16, role=Role.ADMIN
        )
        self.stray = User.objects.create_user(
            username="typo", password="x" * 16, role=Role.MEMBER
        )
        self.client.force_login(self.admin)

    def remove(self, person=None):
        return self.client.post(
            reverse("user_delete", args=[(person or self.stray).pk]), follow=True
        )


class AnAccountThatDidNothingTests(Base):
    def test_is_deleted(self):
        self.remove()
        self.assertFalse(User.objects.filter(username="typo").exists())

    def test_and_says_why_that_was_safe(self):
        self.assertContains(self.remove(), "Nothing in the shop carried their name")

    def test_its_own_belongings_go_with_it(self):
        """A token, a reminder channel and a vehicle grant describe the login
        rather than the shop, so none of them is a reason to refuse."""
        asset = Asset.objects.create(nickname="Aero")
        AssetAccess.objects.create(user=self.stray, asset=asset, level="read")
        raw, prefix, digest = ApiToken.generate()
        ApiToken.objects.create(
            user=self.stray, name="script", prefix=prefix, token_hash=digest
        )

        self.assertEqual(traces(self.stray), [])
        self.remove()
        self.assertFalse(User.objects.filter(username="typo").exists())

    def test_the_page_offers_it(self):
        page = self.client.get(reverse("user_detail", args=[self.stray.pk]))
        self.assertContains(page, reverse("user_delete", args=[self.stray.pk]))


class AnAccountThatDidSomethingTests(Base):
    def test_a_created_by_field_alone_is_enough(self):
        """The case a hand-written list of relations would have missed.

        `Part.created_by` is declared `related_name="+"`, so it is a hidden
        relation: it does not appear in `_meta.related_objects` and a grep for
        the user model never finds it. It is also exactly the record that this
        person did something here.
        """
        Part.objects.create(name="Oil filter", created_by=self.stray)

        self.remove()

        self.assertTrue(User.objects.filter(username="typo").exists())

    def test_the_refusal_names_what_is_holding_them(self):
        """"This account has history" is a dead end. A list of records is
        something somebody can go and act on."""
        asset = Asset.objects.create(nickname="Aero")
        order = WorkOrder.objects.create(asset=asset, title="Brakes")
        TimeEntry.objects.create(work_order=order, user=self.stray, minutes=30)

        response = self.remove()

        self.assertContains(response, "time entries")

    def test_work_in_the_trash_still_counts(self):
        """It is recoverable, and it would come back with its author already
        deleted — which is the state this exists to prevent."""
        asset = Asset.objects.create(nickname="Aero")
        order = WorkOrder.objects.create(
            asset=asset, title="Brakes", created_by=self.stray
        )
        order.delete()

        self.assertTrue(traces(self.stray))
        self.remove()
        self.assertTrue(User.objects.filter(username="typo").exists())

    def test_the_page_says_what_rather_than_offering_a_button(self):
        Part.objects.create(name="Oil filter", created_by=self.stray)

        page = self.client.get(reverse("user_detail", args=[self.stray.pk]))

        self.assertNotContains(page, reverse("user_delete", args=[self.stray.pk]))
        self.assertContains(page, "cannot be deleted")

    def test_one_row_referenced_twice_is_counted_once(self):
        """A time entry both created by and worked by the same person is one
        time entry. Two where there is one makes the whole message untrusted."""
        asset = Asset.objects.create(nickname="Aero")
        order = WorkOrder.objects.create(asset=asset, title="Brakes")
        TimeEntry.objects.create(
            work_order=order, user=self.stray, created_by=self.stray, minutes=30
        )

        counts = dict((label, n) for label, n in traces(self.stray))

        self.assertEqual(counts.get("time entries"), 1)


class GuardsTests(Base):
    def test_you_cannot_delete_yourself(self):
        response = self.remove(self.admin)

        self.assertTrue(User.objects.filter(username="andy").exists())
        self.assertContains(response, "cannot delete your own account")

    def test_the_last_administrator_is_kept(self):
        """An instance with no way in is a restore from backup."""
        self.spare.delete()
        alone = User.objects.create_user(
            username="only", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(alone)
        User.objects.filter(pk=self.admin.pk).delete()

        self.remove(alone)

        self.assertTrue(User.objects.filter(username="only").exists())

    def test_a_member_cannot_delete_anybody(self):
        member = User.objects.create_user(
            username="pat", password="x" * 16, role=Role.MEMBER
        )
        self.client.force_login(member)

        self.assertEqual(
            self.client.post(reverse("user_delete", args=[self.stray.pk])).status_code,
            403,
        )
        self.assertTrue(User.objects.filter(username="typo").exists())

    def test_it_takes_a_post(self):
        self.assertEqual(
            self.client.get(reverse("user_delete", args=[self.stray.pk])).status_code,
            405,
        )

    def test_it_needs_a_login(self):
        self.client.logout()

        self.client.post(reverse("user_delete", args=[self.stray.pk]))

        self.assertTrue(User.objects.filter(username="typo").exists())


class WhatCountsTests(Base):
    def test_the_check_reads_the_models_rather_than_a_list(self):
        """The reason it is exhaustive. Every relation Django knows about is
        consulted, including the ones declared today and the ones added next
        year, which is what a written-out list cannot promise."""
        relations = [
            rel
            for rel in User._meta._get_fields(
                forward=False, reverse=True, include_hidden=True
            )
            if getattr(rel, "field", None) is not None
        ]

        self.assertGreater(len(relations), 40)

    def test_an_account_with_nothing_has_no_traces(self):
        self.assertEqual(traces(self.stray), [])
