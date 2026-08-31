"""
First run (SPEC FR-ADM-1).

Until now the first minute of a new instance was a command line. `docker
compose up` left a sign-in page with no account behind it, and the way past it
was `createsuperuser` — which is Django's, knows nothing about this
application's roles, and so produced an account whose `role` said *member*
while its superuser flag said otherwise. `INSTALL.md` documented the
discrepancy and gave a second command to reconcile it. That is a reasonable
thing to write down and a poor thing to require.

So: one page, once, that creates the account properly, asks the four questions
whose answers change how every later screen reads, says what has to happen
before the phone in the garage will trust this site, and gets out of the way.

**It exists only while there is nobody.** The gate is `User.objects.exists()`
and nothing else — not a flag in the database that a restore could bring back,
not a file on disk that a rebuild could drop. An endpoint that mints an
administrator has to be impossible to reach a second time, and the only fact
that cannot drift out of step with "somebody already owns this instance" is
the accounts table itself.
"""

from __future__ import annotations

import logging

from django import forms
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth import views as auth_views
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from homeautoshop.accounts.forms import PasswordPairMixin, password_fields
from homeautoshop.accounts.models import Role, User

from . import runtime
from .settings_registry import BY_KEY

log = logging.getLogger(__name__)

#: The questions worth asking before anything else, because every later screen
#: is read through them: a date, a distance and a price all render differently.
SETTINGS_FIELDS = ("SHOP_NAME", "UNITS", "CURRENCY_REPORTING", "TIME_ZONE", "LANGUAGE_CODE")

#: Rendered as one group, in this order.
ACCOUNT_FIELDS = ("username", "display_name", "email", "password1", "password2")


def needs_setup() -> bool:
    """Whether this instance has never had an account.

    Asked on the sign-in page and on this one, deliberately not in middleware:
    it is one indexed query, and paying it for every photo and every API call
    for the life of the instance — to answer a question that stops being
    interesting after the first minute — is not a trade worth making.
    """
    return not User.objects.exists()


class FirstRunView(auth_views.LoginView):
    """The sign-in page, which sends the first arrival somewhere useful.

    A brand-new instance used to present a login form that nobody could
    possibly pass, with no indication that the account had to be made from a
    terminal. This is the one place the check is worth doing.
    """

    def dispatch(self, request, *args, **kwargs):
        if needs_setup():
            return redirect("setup")
        return super().dispatch(request, *args, **kwargs)


class FirstRunForm(PasswordPairMixin, forms.Form):
    """The administrator, and the four answers everything else is read through."""

    username = forms.CharField(
        label=_("Username"),
        max_length=150,
        help_text=_("What you will sign in with. Changeable later."),
    )
    display_name = forms.CharField(
        label=_("Your name"),
        max_length=150,
        required=False,
        help_text=_("Shown on work you record. Optional."),
    )
    email = forms.EmailField(
        label=_("Email"),
        required=False,
        help_text=_("Only used for password reset, and only if you set up mail."),
    )

    install_starter_data = forms.BooleanField(
        label=_("Install the starter data"),
        required=False,
        initial=True,
        help_text=_(
            "Maintenance schedules, inspection templates, scan-tool profiles and "
            "manual libraries. Nothing about your vehicles — you add those."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.update(password_fields())
        # The shop questions come from the settings registry rather than being
        # declared again here, so that a choice added there — another language,
        # another unit system — turns up on this page without a second edit.
        for key in SETTINGS_FIELDS:
            entry = BY_KEY[key]
            current = runtime.current(key)
            if entry.choices:
                self.fields[key] = forms.ChoiceField(
                    label=entry.label,
                    choices=entry.choices,
                    initial=current,
                    help_text=entry.help,
                )
            else:
                self.fields[key] = forms.CharField(
                    label=entry.label,
                    initial=current,
                    required=False,
                    help_text=entry.help,
                )

        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                continue
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            field.widget.attrs.setdefault("class", css)

        self.fields["username"].widget.attrs.setdefault("autocomplete", "username")
        for name in ("password1", "password2"):
            self.fields[name].widget.attrs.setdefault("autocomplete", "new-password")

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        # Case-insensitively, because "Andy" and "andy" being two accounts on a
        # five-person instance is a trap rather than a feature.
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError(_("That username is taken."))
        return username



@require_http_methods(["GET", "POST"])
def setup(request):
    """Create the first administrator, or get out of the way."""
    if not needs_setup():
        # Not an error: somebody bookmarked this, or came back to it after
        # finishing. There is nothing here any more and the sign-in page is
        # what they actually wanted.
        return redirect("login")

    form = FirstRunForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            user, restart_needed = _apply(form.cleaned_data, request)
        except ValidationError as exc:
            for key, problem in (getattr(exc, "message_dict", {}) or {}).items():
                form.add_error(None, f"{BY_KEY[key].label if key in BY_KEY else key}: {problem[0]}")
            if not hasattr(exc, "message_dict"):
                form.add_error(None, exc.messages[0])
        else:
            login(request, user)
            messages.success(request, _("Your shop is set up. This is yours now."))
            if restart_needed and not runtime.restart_web():
                messages.info(
                    request,
                    _(
                        "The language and timezone need a restart before they take "
                        "effect: docker compose restart app worker"
                    ),
                )
            return redirect("dashboard")

    return render(
        request,
        "core/setup.html",
        {
            "form": form,
            "trust": _tls_trust(),
            "account_fields": [form[name] for name in ACCOUNT_FIELDS],
            "shop_fields": [form[name] for name in SETTINGS_FIELDS],
        },
    )


@transaction.atomic
def _apply(cleaned: dict, request) -> tuple[User, bool]:
    """Everything, or nothing.

    One transaction because the halves are worthless apart: an administrator
    on an instance whose settings were rejected is an account nobody chose the
    units for, and settings saved against a user that failed to create is an
    instance configured by nobody.
    """
    user = User.objects.create_superuser(
        username=cleaned["username"],
        email=cleaned.get("email") or "",
        password=cleaned["password1"],
        # The reason this page exists rather than a documented second command:
        # `createsuperuser` leaves this saying `member`.
        role=Role.ADMIN,
        first_name=cleaned.get("display_name") or "",
    )

    changed = runtime.save(
        {key: cleaned.get(key, "") for key in SETTINGS_FIELDS},
        user=user,
        source="setup",
    )

    if cleaned.get("install_starter_data"):
        # Idempotent, and already run on every boot under Compose. Offered
        # anyway because an instance run without Compose has never had it, and
        # because being told what was installed is worth more than assuming.
        call_command("seed")

    log.info("first-run setup completed by %s", user.username)
    return user, any(BY_KEY[key].applies == "restart" for key in changed if key in BY_KEY)


def _tls_trust() -> dict:
    """What has to happen before the phone in the garage trusts this site.

    Whoever is reading this page already got past it on this device, one way
    or another. The part still ahead of them is every *other* device, and that
    is a different answer depending on how the certificate was issued.
    """
    from django.conf import settings as django_settings

    mode = (getattr(django_settings, "TLS_MODE", "") or "internal").strip().lower()

    if mode in ("acme", "acme-dns"):
        return {
            "mode": mode,
            "heading": _("Nothing to install"),
            "detail": _(
                "This instance has a certificate from a public authority, so any "
                "browser and any phone will trust it already."
            ),
        }
    if mode == "custom":
        return {
            "mode": mode,
            "heading": _("Your own certificate"),
            "detail": _(
                "You supplied the certificate, so whether a device trusts it depends "
                "on who issued it. If you issued it yourself, each device needs your "
                "root installed."
            ),
        }
    return {
        "mode": "internal",
        "heading": _("Each device needs the root certificate"),
        "detail": _(
            "The certificate was issued by this instance itself, so every browser "
            "will refuse it until the root is installed — including the phone you "
            "will use in the garage. Install it once per device."
        ),
        "command": (
            "docker compose cp "
            "proxy:/data/caddy/pki/authorities/local/root.crt ./root.crt"
        ),
    }
