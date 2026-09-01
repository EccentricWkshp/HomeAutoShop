"""
Managing the people who can sign in (SPEC FR-ADM-2).

Until now the only way to add a second account was Django's `/admin/`, which
is not a screen you hand to a household: it exposes every table in the
application, and its user form offers `is_superuser`, `is_staff`, groups and
permissions — four ways to say a thing this application says with one field.

**Nothing here deletes a user, and that is the point.** Every reference to a
user is `on_delete=SET_NULL`, so deleting one does not remove the work they
recorded — it quietly detaches their name from it, which is worse. The audit
log exists to answer *who changed the odometer to 300,000*, and a delete
button makes that unanswerable while looking tidy. Somebody who should no
longer sign in is deactivated: they keep their history and lose their key.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeautoshop.people.models import Person

from .forms import PasswordPairMixin, password_fields
from .models import AssetAccess, Role, User, require
from .services import describe_traces, traces


def _style(form) -> None:
    for name, field in form.fields.items():
        if isinstance(field.widget, forms.CheckboxInput):
            continue
        css = "select" if isinstance(field.widget, forms.Select) else "input"
        field.widget.attrs.setdefault("class", css)


class NewUserForm(PasswordPairMixin, forms.ModelForm):
    """An account somebody else will use.

    There is no emailed invitation, deliberately: it would need SMTP working,
    a token table and an expiry policy, to replace a household conversation
    that already happens. The admin sets a first password and hands it over;
    the person changes it whenever they like.
    """

    class Meta:
        model = User
        fields = ["username", "first_name", "email", "role", "person"]
        labels = {
            "username": _("Username"),
            "first_name": _("Their name"),
            "email": _("Email"),
            "role": _("Role"),
            "person": _("Linked to"),
        }
        help_texts = {
            "username": _("What they will sign in with."),
            "email": _("Optional. Only used for password reset, and only if mail is set up."),
            "person": _("Links this login to a person, so their work is attributed to them."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.update(password_fields())
        self.fields["email"].required = False
        self.fields["person"].required = False
        self.fields["person"].queryset = Person.objects.filter(user_account__isnull=True)
        self.fields["person"].empty_label = _("Nobody in particular")
        _style(self)

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        # Case-insensitively: `Andy` and `andy` as two accounts on a
        # five-person instance is a trap rather than a feature.
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError(_("That username is taken."))
        return username


class UserProfileForm(forms.ModelForm):
    """Everything about an existing account except its password and its key."""

    class Meta:
        model = User
        fields = ["first_name", "email", "role", "person"]
        labels = {
            "first_name": _("Their name"),
            "email": _("Email"),
            "role": _("Role"),
            "person": _("Linked to"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].required = False
        self.fields["person"].required = False
        self.fields["person"].queryset = Person.objects.filter(
            Q(user_account__isnull=True) | Q(pk=self.instance.person_id)
        )
        self.fields["person"].empty_label = _("Nobody in particular")
        _style(self)


class SetPasswordForm(PasswordPairMixin, forms.Form):
    def __init__(self, *args, **kwargs):
        self.instance = kwargs.pop("instance", None)
        super().__init__(*args, **kwargs)
        self.fields.update(password_fields())
        _style(self)


# ---------------------------------------------------------------------------
# Not locking yourself out
# ---------------------------------------------------------------------------


def other_active_admins(besides) -> int:
    """How many administrators would be left without this one."""
    return (
        User.objects.filter(role=Role.ADMIN, is_active=True).exclude(pk=besides.pk).count()
    )


def last_admin(user) -> bool:
    """Is this account, **as stored**, the only way back into the instance?

    Read from the database rather than from the object, because the caller is
    usually holding a `ModelForm` instance: `_post_clean` has already written
    the submitted role onto it, so asking the object whether it is an admin
    answers "is it *about to be*" — and the guard against demoting the last
    administrator waved the demotion through.
    """
    stored_admin = User.objects.filter(pk=user.pk, role=Role.ADMIN, is_active=True).exists()
    return stored_admin and other_active_admins(user) == 0


@login_required
def user_list(request):
    """Who can sign in, and what they may do (FR-ADM-2)."""
    require(request.user, "user.manage")
    people = User.objects.select_related("person").order_by("-is_active", "username")
    return render(
        request,
        "accounts/users.html",
        {
            "people": people,
            "active_admins": User.objects.filter(role=Role.ADMIN, is_active=True).count(),
        },
    )


@login_required
def user_create(request):
    require(request.user, "user.manage")
    form = NewUserForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save(commit=False)
        user.set_password(form.cleaned_data["password1"])
        _match_django_flags(user)
        user.save()
        messages.success(
            request,
            _("%(name)s can sign in now. Give them the password you just set.")
            % {"name": user.username},
        )
        return redirect("user_list")
    return render(request, "accounts/user_form.html", {"form": form, "person": None})


@login_required
def user_detail(request, pk):
    require(request.user, "user.manage")
    person = get_object_or_404(User, pk=pk)
    form = UserProfileForm(request.POST or None, instance=person)

    if request.method == "POST" and form.is_valid():
        if _would_strand_the_shop(request, person, role=form.cleaned_data["role"]):
            return redirect("user_detail", pk=person.pk)
        person = form.save(commit=False)
        _match_django_flags(person)
        person.save()
        messages.success(request, _("Saved."))
        return redirect("user_detail", pk=person.pk)

    from homeautoshop.assets.models import Asset

    granted = person.asset_access.select_related("asset").order_by("asset__nickname")
    return render(
        request,
        "accounts/user.html",
        {
            "person": person,
            "form": form,
            "password_form": SetPasswordForm(instance=person),
            "is_last_admin": last_admin(person),
            "is_you": person.pk == request.user.pk,
            # Grants are shown for everybody, not only helpers: a role changed
            # from helper to member leaves its rows behind, and a list that
            # hides them would make changing the role back a surprise.
            "is_helper": person.role == Role.HELPER,
            # What their name is on. Empty means the account may be removed;
            # anything else is both the reason it may not be and the list of
            # things to go and deal with first.
            "traces": traces(person),
            "grants": granted,
            "grantable": Asset.objects.exclude(
                pk__in=granted.values_list("asset_id", flat=True)
            ).order_by("nickname"),
        },
    )


@require_POST
@login_required
def user_access(request, pk):
    """Grant or revoke one vehicle for one helper (FR-ADM-9, SPEC §12.2a).

    One endpoint for both directions, because a grant is a row: adding it is
    the grant and deleting it is the revocation, and there is no third state
    for a screen to get out of step with.
    """
    require(request.user, "user.manage")
    person = get_object_or_404(User, pk=pk)
    asset_id = (request.POST.get("asset") or "").strip()
    level = request.POST.get("level") or "read"

    if request.POST.get("revoke"):
        removed, _detail = AssetAccess.objects.filter(
            user=person, asset_id=asset_id
        ).delete()
        messages.success(
            request,
            _("Access removed.") if removed else _("They did not have that one."),
        )
        return redirect("user_detail", pk=person.pk)

    from homeautoshop.assets.models import Asset

    asset = Asset.objects.filter(pk=asset_id).first() if asset_id else None
    if asset is None:
        messages.warning(request, _("Choose a vehicle first."))
        return redirect("user_detail", pk=person.pk)
    if level not in ("read", "write"):
        level = "read"

    AssetAccess.objects.update_or_create(
        user=person, asset=asset, defaults={"level": level}
    )
    messages.success(
        request,
        _("%(name)s can work on %(vehicle)s.")
        % {"name": person.display_name, "vehicle": asset.nickname}
        if level == "write"
        else _("%(name)s can see %(vehicle)s.")
        % {"name": person.display_name, "vehicle": asset.nickname},
    )
    return redirect("user_detail", pk=person.pk)


@require_POST
@login_required
def user_delete(request, pk):
    """Remove an account that never did anything (FR-ADM-2, FR-ADM-8).

    Deactivation stays the answer for somebody who worked here — their name
    belongs on what they did. This is for the other kind: an account created
    by mistake or while trying the application out, which has no history to
    protect and, until now, could only be hidden. An instance used for a while
    filled up with those and the only way to tidy it was to start again.

    Refused the moment anything carries their name, and the refusal says what,
    because "this account has history" is a dead end and "3 work orders, 2
    time entries" is a list of things to go and deal with.
    """
    require(request.user, "user.manage")
    person = get_object_or_404(User, pk=pk)

    if person.pk == request.user.pk:
        messages.error(
            request,
            _("You cannot delete your own account. Ask another administrator."),
        )
        return redirect("user_detail", pk=person.pk)
    if _would_strand_the_shop(request, person, active=False):
        return redirect("user_detail", pk=person.pk)

    marks = traces(person)
    if marks:
        messages.error(
            request,
            _(
                "%(name)s has %(what)s in the shop, so the account is kept and "
                "their name stays on it. Deactivate them instead, or remove "
                "those records first."
            )
            % {"name": person.display_name, "what": describe_traces(marks)},
        )
        return redirect("user_detail", pk=person.pk)

    name = person.display_name
    person.delete()
    messages.success(
        request,
        _("Deleted %(name)s. Nothing in the shop carried their name.")
        % {"name": name},
    )
    return redirect("user_list")


@require_POST
@login_required
def user_set_active(request, pk):
    """Take away the key without taking away the history (FR-ADM-2)."""
    require(request.user, "user.manage")
    person = get_object_or_404(User, pk=pk)
    wanted = request.POST.get("active") == "1"

    if not wanted:
        if person.pk == request.user.pk:
            messages.error(
                request,
                _("You cannot deactivate your own account. Ask another administrator."),
            )
            return redirect("user_detail", pk=person.pk)
        if _would_strand_the_shop(request, person, active=False):
            return redirect("user_detail", pk=person.pk)

    person.is_active = wanted
    person.save(update_fields=["is_active"])
    messages.success(
        request,
        _("%(name)s can sign in again.") % {"name": person.username}
        if wanted
        else _("%(name)s can no longer sign in. Everything they recorded stays.")
        % {"name": person.username},
    )
    return redirect("user_detail", pk=person.pk)


@require_POST
@login_required
def user_set_password(request, pk):
    require(request.user, "user.manage")
    person = get_object_or_404(User, pk=pk)
    form = SetPasswordForm(request.POST, instance=person)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return redirect("user_detail", pk=person.pk)

    person.set_password(form.cleaned_data["password1"])
    person.save(update_fields=["password"])
    if person.pk == request.user.pk:
        # Changing your own password ends every session, this one included,
        # unless it is refreshed — which is friendlier than a silent logout.
        from django.contrib.auth import update_session_auth_hash

        update_session_auth_hash(request, person)
    messages.success(
        request, _("Password set for %(name)s. Tell them what it is.") % {"name": person.username}
    )
    return redirect("user_detail", pk=person.pk)


def _would_strand_the_shop(request, person, *, role=None, active=None) -> bool:
    """Refuse the change that leaves nobody able to administer the instance.

    The failure it prevents is unrecoverable from inside the application: no
    administrator means no user screen, no settings, and no way back except a
    shell on the host.
    """
    losing_admin = role is not None and role != Role.ADMIN
    losing_active = active is False
    if not (losing_admin or losing_active):
        return False
    if not last_admin(person):
        return False
    messages.error(
        request,
        _(
            "That would leave the shop with no administrator, and no way back "
            "in. Make somebody else an administrator first."
        ),
    )
    return True


def _match_django_flags(user) -> None:
    """Keep Django's own flags in step with the role this application uses.

    `can()` reads `role`; `/admin/` reads `is_staff` and `is_superuser`. When
    they disagree the account works until somebody clears the flag and is then
    quietly demoted — which is exactly the trap `INSTALL.md` had to warn about
    when the only way to make an account was `createsuperuser`.
    """
    is_admin = user.role == Role.ADMIN
    user.is_staff = is_admin
    user.is_superuser = is_admin
