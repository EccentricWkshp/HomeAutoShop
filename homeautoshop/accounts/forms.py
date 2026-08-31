"""
Setting a password, in one place (SPEC FR-ADM-1, FR-ADM-2).

Two screens set passwords — the first-run wizard and the user screen — and
they must apply the same rule, because "the password I was given on Tuesday
is now refused" is a bug report nobody can act on. Django's own validators do
the deciding; this only makes sure both callers ask them.
"""

from __future__ import annotations

from django import forms
from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


class PasswordPairMixin:
    """A password and its confirmation, checked the way Django checks them.

    Expects fields named `password1` and `password2`. Add them with
    `password_fields()` unless the form needs them optional, which is the
    case on a screen that edits somebody who already has one.
    """

    def clean(self):
        cleaned = super().clean()
        first, second = cleaned.get("password1"), cleaned.get("password2")
        if not first and not second:
            return cleaned
        if first != second:
            self.add_error("password2", _("The two passwords did not match."))
            return cleaned
        try:
            # The same validators the password-change screen will apply later,
            # so a password accepted here is not refused tomorrow.
            password_validation.validate_password(first, getattr(self, "instance", None))
        except ValidationError as exc:
            self.add_error("password1", exc)
        return cleaned


def password_fields(*, required: bool = True) -> dict:
    """The two fields, labelled and told not to autofill the browser."""
    return {
        "password1": forms.CharField(
            label=_("Password"),
            required=required,
            widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        ),
        "password2": forms.CharField(
            label=_("Password again"),
            required=required,
            widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        ),
    }
