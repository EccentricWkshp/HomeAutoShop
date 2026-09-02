"""
Reading a published trouble-code list, whoever it came from (SPEC §17 R-1).

One file per manufacturer, holding one or more **documents** — a summary of the
whole badge, and beside it a particular vehicle's service manual — because
those are different claims and `precedence` says which answers first. Keeping
them in one file per make is what lets a shop install *Ford* rather than
install three documents and be expected to know how they relate.

**This is the validator, and it is the whole trust model.** `catalog.install`
calls it and gets no privileged path: a file fetched from the published catalog
is checked exactly as one an operator pastes in would be. So the rules are
written for a hostile file rather than a friendly one.

* **Nothing may claim to be the standard.** A list whose scope is `iso_sae` is
  presented as authoritative — the caller may state it as fact, because an
  ISO/SAE code means the same thing on every vehicle ever built. Those ship in
  the image and are not installable. A file arriving here saying it is the
  standard is refused rather than quietly downgraded, because the difference
  between "Ford says" and "the standard says" is the one this module exists to
  keep.
* **Every code must parse.** A table with `Cylinder 4` in the key column is not
  a code table, whatever it says on the front.
* **Every document must say where it came from.** Every definition is quoted
  beside who says so, and an unattributed definition on a diagnostic screen is
  worth less than no definition at all.
* **Every definition must say something.** §8.3c refuses invented wording; an
  empty string offered as a definition is the same failure with less effort.
* **Unknown keys are refused**, so a file cannot smuggle a field that a later
  version of this reader might start honouring.

Characters are repaired on the way in by :mod:`homeautoshop.diagnostics.transcription`,
so an installed list is held to the same standard as a bundled one rather than
to whatever its publisher's extractor managed.
"""

from __future__ import annotations

import json

from django.utils.translation import gettext_lazy as _

from . import dtc, transcription

#: A generous ceiling that is still a ceiling. Ford's own group list is 3,093
#: codes and about 200 KB, and a make covered by several documents is larger.
#: This is here so that a runaway file fails as a refusal rather than as memory.
MAX_BYTES = 8 * 1024 * 1024
MOST_CODES = 50_000
MOST_DOCUMENTS = 20

TOP_LEVEL = {"make", "aliases", "version", "documents", "description", "author"}
PER_DOCUMENT = {"source", "precedence", "codes", "scope", "read_from"}


class CodeListInvalid(ValueError):
    """The file is not a code list this will install."""


def parse(text: str) -> dict:
    """Validate one published list and return it, or refuse with the reason."""
    if len(text.encode("utf-8", "ignore")) > MAX_BYTES:
        raise CodeListInvalid(_("That file is too large to be a code list."))
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CodeListInvalid(
            _("That is not readable JSON: %(detail)s") % {"detail": exc}
        )
    if not isinstance(data, dict):
        raise CodeListInvalid(_("A code list is a mapping of fields, not a list."))

    unknown = sorted(set(data) - TOP_LEVEL)
    if unknown:
        raise CodeListInvalid(
            _("A code list has no field %(field)s.") % {"field": unknown[0]}
        )

    make = str(data.get("make") or "").strip()
    if not make:
        raise CodeListInvalid(_("A code list needs the make it covers."))

    version = data.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise CodeListInvalid(_("A code list's version is a whole number from 1."))

    aliases = data.get("aliases") or []
    if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
        raise CodeListInvalid(_("Aliases are a list of other names for this make."))

    documents = data.get("documents")
    if not isinstance(documents, list) or not documents:
        raise CodeListInvalid(_("A code list needs at least one document."))
    if len(documents) > MOST_DOCUMENTS:
        raise CodeListInvalid(_("That file names more documents than this will read."))

    return {
        "make": make[:60],
        "aliases": [a.strip()[:60] for a in aliases if a.strip()],
        "version": version,
        "description": str(data.get("description") or "")[:500],
        "author": str(data.get("author") or "")[:80],
        "documents": [_document(d, index) for index, d in enumerate(documents, 1)],
    }


def _document(raw, index: int) -> dict:
    if not isinstance(raw, dict):
        raise CodeListInvalid(
            _("Document %(n)d is not a mapping of fields.") % {"n": index}
        )
    unknown = sorted(set(raw) - PER_DOCUMENT)
    if unknown:
        raise CodeListInvalid(
            _("Document %(n)d has no field %(field)s.")
            % {"n": index, "field": unknown[0]}
        )

    scope = str(raw.get("scope") or "make").strip()
    if scope != "make":
        raise CodeListInvalid(
            _(
                "Document %(n)d says it is the ISO/SAE standard's own list. Those "
                "ship with the application; an installable list covers one "
                "manufacturer."
            )
            % {"n": index}
        )

    source = str(raw.get("source") or "").strip()
    if not source:
        raise CodeListInvalid(
            _("Document %(n)d does not say where it came from.") % {"n": index}
        )

    precedence = raw.get("precedence", 0)
    if not isinstance(precedence, int) or isinstance(precedence, bool):
        raise CodeListInvalid(
            _("Document %(n)d's precedence is a whole number.") % {"n": index}
        )

    codes = raw.get("codes")
    if not isinstance(codes, dict) or not codes:
        raise CodeListInvalid(_("Document %(n)d defines no codes.") % {"n": index})
    if len(codes) > MOST_CODES:
        raise CodeListInvalid(
            _("Document %(n)d holds more codes than this will read.") % {"n": index}
        )

    read = {}
    for code, definition in codes.items():
        parsed = dtc.parse(str(code))
        if parsed is None:
            raise CodeListInvalid(
                _("Document %(n)d lists %(code)s, which is not a trouble code.")
                % {"n": index, "code": str(code)[:20]}
            )
        if not isinstance(definition, str):
            raise CodeListInvalid(
                _("Document %(n)d's %(code)s is not written as text.")
                % {"n": index, "code": parsed["code"]}
            )
        text = transcription.tidy(definition)
        if not text:
            raise CodeListInvalid(
                _("Document %(n)d gives %(code)s no definition.")
                % {"n": index, "code": parsed["code"]}
            )
        read[parsed["code"]] = text

    return {
        "source": source[:200],
        "precedence": precedence,
        "codes": read,
        "read_from": [str(v)[:120] for v in (raw.get("read_from") or [])][:20],
    }


def load(text: str, *, user=None):
    """Install one published list, replacing any earlier version of it.

    Replacing rather than adding, because two copies of Ford's list differing
    only in version is not a make covered by two documents — it is the same
    document twice, and a lookup would quote whichever happened to sort first.
    """
    from .models import InstalledCodeList

    data = parse(text)
    held = InstalledCodeList.objects.filter(make__iexact=data["make"]).first()
    if held is None:
        held = InstalledCodeList(make=data["make"], created_by=user)
    held.make = data["make"]
    held.aliases = data["aliases"]
    held.version = data["version"]
    held.description = data["description"]
    held.author = data["author"]
    held.documents = data["documents"]
    held.save()
    return held
