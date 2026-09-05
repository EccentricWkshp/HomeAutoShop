"""Password rules, and the choice of how many of them there are (§12.2).

§12.2 argues for a length floor rather than composition rules, and that remains
the default and the recommendation: length is what actually resists guessing,
and composition rules mostly teach people to write `Password1!`.

It is a default rather than a law because the threat model is not the same in
every installation. A shop on a laptop behind a locked door, reachable from
nowhere, spends twelve characters of typing per sign-in to defend against an
attacker who would already be standing in the room. That is not security, it is
a toll — and a toll people pay by choosing something they can type, which is the
outcome the rule was meant to prevent. `PASSWORD_POLICY` is how an operator says
which situation theirs is.

The classes validator exists for the operator whose own policy demands
character classes. It is not the default, and §12.2 still thinks it is the
weaker choice.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.utils.translation import ngettext


class CharacterClassValidator:
    """Require several kinds of character, for a policy that insists on it.

    Counted as *kinds present* rather than as a list of required ones, so the
    rule can be satisfied without a symbol — a passphrase with a digit and a
    capital in it passes, and nobody has to end every password with `!`.
    """

    def __init__(self, min_classes: int = 3) -> None:
        self.min_classes = min_classes

    def _classes(self, password: str) -> int:
        return sum(
            (
                any(character.islower() for character in password),
                any(character.isupper() for character in password),
                any(character.isdigit() for character in password),
                any(not character.isalnum() for character in password),
            )
        )

    def validate(self, password, user=None):
        if self._classes(password) >= self.min_classes:
            return
        raise ValidationError(
            ngettext(
                "This password needs at least %(n)d kind of character "
                "(lower case, upper case, digits, symbols).",
                "This password needs at least %(n)d kinds of character "
                "(lower case, upper case, digits, symbols).",
                self.min_classes,
            ),
            code="password_too_simple",
            params={"n": self.min_classes},
        )

    def get_help_text(self):
        return ngettext(
            "Use at least %(n)d kind of character: lower case, upper case, "
            "digits, symbols.",
            "Use at least %(n)d kinds of character: lower case, upper case, "
            "digits, symbols.",
            self.min_classes,
        ) % {"n": self.min_classes}


def _length(minimum: int) -> dict:
    return {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": minimum},
    }


_COMMON = {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"}
_NUMERIC = {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"}
_SIMILAR = {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"}

#: Every policy, and what each one actually enforces.
#:
#: `noauth` is not a password policy at all — it is the absence of sign-in, and
#: it lives in this table because it is the same operator decision at the same
#: end of the same scale, and putting it anywhere else would mean two settings
#: that have to agree.
POLICIES: dict[str, list[dict]] = {
    # No sign-in. Anyone who can reach the site is the shop.
    "noauth": [],
    # Sign-in, and any password at all — including a single character.
    "any": [],
    # Short enough to type on a phone with one hand, which is the whole point.
    # No dictionary check: at six characters it would refuse most of what
    # somebody choosing this policy is trying to choose.
    "6chars": [_length(6)],
    # The default, and what §12.2 argues for.
    "12chars": [_length(12), _COMMON],
    # For an instance that is reachable from more than the room it is in, or an
    # operator whose own policy names character classes.
    "complex": [_SIMILAR, _length(16), _COMMON, _NUMERIC,
                {"NAME": "homeautoshop.accounts.validators.CharacterClassValidator",
                 "OPTIONS": {"min_classes": 3}}],
}

DEFAULT_POLICY = "12chars"

#: Policies that leave the instance open to anyone who can reach it, or nearly.
#: Named once here so the health screen, the banner and the startup warning
#: cannot drift apart about which is which.
WEAK_POLICIES = frozenset({"noauth", "any", "6chars"})


def validators_for(policy: str) -> list[dict]:
    """The `AUTH_PASSWORD_VALIDATORS` a policy name means.

    Raises on a name that is not one of them. A misspelled security setting
    that quietly falls back is the worst of both: the operator believes they
    relaxed it, or believes they tightened it, and neither is true.
    """
    from django.core.exceptions import ImproperlyConfigured

    try:
        return list(POLICIES[policy])
    except KeyError:
        # A plain string, not a translated one: this is raised while settings
        # are still loading, before the translation machinery is usable, and it
        # is read by an operator in a container log rather than shown in the UI.
        raise ImproperlyConfigured(
            f"PASSWORD_POLICY is {policy!r}, which is not one of: "
            f"{', '.join(sorted(POLICIES))}"
        ) from None
