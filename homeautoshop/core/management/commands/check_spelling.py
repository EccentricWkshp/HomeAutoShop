"""US English is the source language, and this is what keeps it that way.

`locale/README.md` is explicit: **en-US is the source language and has no
catalogue.** Every `msgid` is US English, and `locale/en_CA` exists to render
the ten strings Canadian English spells differently — `labour`, `catalogue`,
`licence plate`, `Cancelled`, `Totalled`, `grey-market`.

That arrangement is easy to read backwards. `docs/DEVELOPMENT.md` says the
source is "already correct Canadian English", which is true only because most
words are spelled the same, and somebody reading that line alone will write
`colour` into a `msgid` and into the prose around it. That is exactly what
happened: a release note went out saying `colour`, and the sweep that followed
found 152 more in comments, docstrings and documentation.

So the rule gets a gate, beside `check_rtl` and `check_translations`.

**It checks prose, never tokens.** The distinction is the whole design, because
the first attempt at this cleanup did not make it and wanted to rewrite
`PurchaseStatus.CANCELLED = "cancelled"` — which is not a spelling, it is a
value in the database — and to edit two applied migrations. A spelling gate
that needs a data migration has stopped being a spelling gate.

Four things are therefore never read: `locale/` (whose en_CA catalogue exists
to *produce* these spellings), applied migrations, captured third-party pages,
and any string in Python that is not a docstring. What is left — comments,
docstrings, Markdown, template text and `msgid`s — is prose, and prose is US
English.

`ALLOWED` records the deliberate exceptions, each with its reason, so that an
exception is a decision somebody wrote down rather than a hole in the net.
"""

from __future__ import annotations

import ast
import io
import pathlib
import re
import tokenize

from django.conf import settings
from django.core.management.base import BaseCommand

#: British verbs in `-ise` whose every inflection is British too.
#:
#: **This is not the suffix rule the module docstring refuses.** That rule
#: would guess whether an *unknown* word is British and would be wrong about
#: `advertise`, `exercise`, `surprise`, `promise`, `revise`, `compromise` and
#: `supervise`, all of which end in `-ise` on both sides of the Atlantic. This
#: list is curated by hand, one entry per verb, and the expansion below only
#: inflects a word already known to be British — which cannot invent a false
#: positive, because the judgment was made before the mechanism ran.
#:
#: The reason it is worth having: the hand-written table below shipped with
#: `summarise` and `summarised` but not `summarising`, `realise` but not
#: `realising`, and `organisation` but not `localisation` — so a gate that had
#: just swept 152 occurrences walked past `localisation` in `parts/models.py`
#: and `quantised` in the spec. A table that needs updating in seven places to
#: add one word gets updated in one of them.
ISE_STEMS = (
    "apologise", "authorise", "capitalise", "categorise", "centralise",
    "characterise", "colonise", "computerise", "criticise", "customise",
    "decentralise", "digitise", "emphasise", "familiarise", "finalise",
    "formalise", "generalise", "harmonise", "hospitalise", "initialise",
    "itemise", "legalise", "localise", "materialise", "maximise", "memorise",
    "minimise", "mobilise", "modernise", "neutralise", "normalise",
    "optimise", "organise", "penalise", "personalise", "pluralise",
    "prioritise", "publicise", "quantise", "rasterise", "rationalise",
    "realise", "recognise", "sanitise", "serialise", "specialise",
    "stabilise", "standardise", "summarise", "synchronise", "utilise",
    "visualise",
)


def _inflected(stem: str) -> dict[str, str]:
    """`organise` and the six forms of it, each mapped to its US spelling.

    Some of what comes out is not a word — `criticisation` is nothing anybody
    has written — and that costs exactly nothing: an entry that never matches
    is an entry that never fires. Completeness is worth more here than economy,
    because the failure mode of this table is silence.
    """
    american = stem[:-3] + "ize"
    root = stem[:-1]
    return {
        stem: american,
        stem + "s": american + "s",
        root + "ed": american[:-1] + "ed",
        root + "ing": american[:-1] + "ing",
        root + "ation": american[:-1] + "ation",
        root + "ations": american[:-1] + "ations",
        root + "able": american[:-1] + "able",
    }


#: British form -> the US form the source language uses. Explicit pairs rather
#: than a suffix rule, because `programmed`, `analysis`, `parameter`,
#: `practice` (the noun) and `Realistically` all look like matches and are not.
PAIRS = {
    "colour": "color", "colours": "colors", "coloured": "colored",
    "colouring": "coloring", "colourful": "colorful",
    "behaviour": "behavior", "behaviours": "behaviors",
    "behavioural": "behavioral",
    "favour": "favor", "favours": "favors", "favoured": "favored",
    "favourite": "favorite", "flavour": "flavor", "honour": "honor",
    "honoured": "honored", "humour": "humor",
    "labour": "labor", "labours": "labors",
    "neighbour": "neighbor", "neighbours": "neighbors",
    "odour": "odor", "rumour": "rumor", "savour": "savor",
    "armour": "armor", "endeavour": "endeavor", "vapour": "vapor",
    "vigour": "vigor", "harbour": "harbor",
    "centre": "center", "centres": "centers", "centred": "centered",
    "metre": "meter", "metres": "meters",
    "millimetre": "millimeter", "millimetres": "millimeters",
    "centimetre": "centimeter", "centimetres": "centimeters",
    "kilometre": "kilometer", "kilometres": "kilometers",
    "millilitre": "milliliter", "millilitres": "milliliters",
    "litre": "liter", "litres": "liters",
    "fibre": "fiber", "fibres": "fibers", "theatre": "theater",
    "calibre": "caliber", "manoeuvre": "maneuver",
    # The whole `-ise` family comes from `ISE_STEMS` above, inflected. Adding
    # one here instead would add one form of it.
    #
    # `-yse` stays here, because `_inflected` would make `analize` of it — and
    # because **`analyses` is deliberately absent**. It is the British verb and
    # the American plural of `analysis`, spelled identically, and the fluids
    # module is full of the noun.
    "analyse": "analyze", "analysed": "analyzed", "analysing": "analyzing",
    "paralyse": "paralyze", "paralysed": "paralyzed",
    "licence": "license", "licences": "licenses",
    "defence": "defense", "offence": "offense", "pretence": "pretense",
    "practise": "practice", "practised": "practiced",
    "catalogue": "catalog", "catalogues": "catalogs",
    "catalogued": "cataloged", "cataloguing": "cataloging",
    "analogue": "analog", "dialogue": "dialog",
    "travelling": "traveling", "travelled": "traveled",
    "cancelling": "canceling", "cancelled": "canceled",
    "modelling": "modeling", "modelled": "modeled",
    "labelling": "labeling", "labelled": "labeled",
    "unlabelled": "unlabeled",
    "signalling": "signaling", "signalled": "signaled",
    "fuelling": "fueling", "fuelled": "fueled",
    "refuelling": "refueling", "refuelled": "refueled",
    "totalling": "totaling", "totalled": "totaled",
    "dialling": "dialing", "dialled": "dialed",
    "levelling": "leveling", "levelled": "leveled",
    "unravelling": "unraveling", "unravelled": "unraveled",
    "counselling": "counseling", "counselled": "counseled",
    "marvellous": "marvelous", "jeweller": "jeweler",
    # The mirror image: British drops the doubled `l` where US keeps it.
    "enrolment": "enrollment", "fulfil": "fulfill",
    "fulfils": "fulfills", "fulfilment": "fulfillment",
    "instalment": "installment", "instalments": "installments",
    "skilful": "skillful", "wilful": "willful",
    "distil": "distill", "instil": "instill", "appal": "appall",
    # `un-` forms are their own words: the table had `recognised` and walked
    # past `unrecognised` in the Caddyfile for the same reason it had
    # `labelled` and walked past `unlabelled` in the spec.
    "unrecognised": "unrecognized", "unrecognisable": "unrecognizable",
    "unauthorised": "unauthorized", "unorganised": "unorganized",
    "uncategorised": "uncategorized", "unsynchronised": "unsynchronized",
    "uninitialised": "uninitialized", "unsanitised": "unsanitized",
    "grey": "gray", "greyed": "grayed", "greyish": "grayish",
    "tyre": "tire", "tyres": "tires", "aluminium": "aluminum",
    # Chemistry, which this application does have opinions about: the fluids
    # module reads lab reports and a lab may spell either way.
    "sulphate": "sulfate", "sulphates": "sulfates",
    "sulphide": "sulfide", "sulphides": "sulfides",
    "mould": "mold", "moulds": "molds", "moulded": "molded",
    "moulding": "molding", "smoulder": "smolder",
    "sceptic": "skeptic", "sceptical": "skeptical",
    "judgement": "judgment", "judgements": "judgments",
    "programme": "program", "programmes": "programs",
    "storey": "story", "plough": "plow", "draught": "draft", "kerb": "curb",
    "whilst": "while", "amongst": "among",
    "enquire": "inquire", "enquiry": "inquiry", "enquiries": "inquiries",
    "aeroplane": "airplane", "sulphur": "sulfur", "moustache": "mustache",
    "cheque": "check", "cheques": "checks",
    "speciality": "specialty", "artefact": "artifact",
    "artefacts": "artifacts",
}

# Merged rather than written into the literal, so a stem is added in one place.
# The explicit table wins on a collision: it is where the judgment calls live.
for _stem in ISE_STEMS:
    PAIRS = {**_inflected(_stem), **PAIRS}

#: Deliberate exceptions, as `path suffix -> reason`. Each is a place where the
#: British spelling *is* the subject rather than a lapse.
ALLOWED = {
    "core/tests_locales.py":
        "asserts what en-CA renders; en-CA exists precisely to differ here",
    "fluids/analytes.py":
        "`aluminium` is a parse alias for labs that spell it that way — the "
        "canonical key and the label are already `aluminum`",
    "purchasing/models.py":
        "`cancelled` is a value stored in the database, not a spelling; the "
        "msgid beside it is already `Canceled`",
    "work/tests_parts_needed.py":
        "the same stored value, in a test that asserts against it",
    "scantools/tests.py":
        "fixture words standing in for text on a scanned page",
    "management/commands/check_spelling.py":
        "this file, which has to name the British spellings in order to refuse "
        "them",
    "core/tests_spelling.py":
        "the tests for this file, which quote the mistakes they are about",
}

SKIP_DIRS = {
    ".git", "venv", "staticfiles", "node_modules", "__pycache__",
    # The en_CA catalogue's whole job is to produce these spellings.
    "locale",
    # Applied migrations are history; captures are somebody else's pages.
    "migrations", "dtc-lists", "scan-reports", "backups",
}

SUFFIXES = {".py", ".md", ".js", ".css", ".html", ".txt"}
EXTRA = {".env.example", "Caddyfile"}

WORD = re.compile(r"[A-Za-z]+")
JS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
MD_MASK = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.S)
HTML_TAG = re.compile(r"<[^>]*>")

#: A Django template tag or variable. Masked out like an HTML tag, and for the
#: same reason — `{% if rollup.labour_hours %}` is an attribute lookup, not a
#: sentence. Missing this cost two templates: a sweep renamed the attribute in
#: the markup and not in `core/budget.py`, and Django resolves a missing one to
#: the empty string, so the pages rendered a blank number and the whole suite
#: still passed.
DJANGO_TAG = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.S)

#: The exception inside that: a quoted string in a template tag is a `msgid`,
#: which is prose and is exactly what this command is for.
TAG_STRING = re.compile(r"\"[^\"]*\"|'[^']*'")


def _found(text: str) -> list[tuple[str, str]]:
    out = []
    for word in WORD.findall(text):
        fixed = PAIRS.get(word.lower())
        if fixed is not None:
            out.append((word, fixed))
    return out


def _prose_of_python(source: str) -> str:
    """Comments and docstrings. Every other string is data until proven prose.

    Docstrings come from `ast`, never from "a string that starts a line". The
    cheap version of that test did real damage: inside a dict literal written
    one entry per line, *every key* starts a line — so a table of British-to-US
    pairs reads as a run of docstrings. The sweep that shared the heuristic
    duly rewrote a sentence explaining that Canadian spells the unit `metre`
    into one that said nothing at all.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return ""
    prose = [token.string for token in tokens if token.type == tokenize.COMMENT]
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "\n".join(prose)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            found = ast.get_docstring(node, clean=False)
            if found:
                prose.append(found)
    return "\n".join(prose)


def prose_of(path: pathlib.Path, text: str) -> str:
    if path.suffix == ".py":
        return _prose_of_python(text)
    if path.suffix in {".js", ".css"}:
        return "\n".join(JS_COMMENT.findall(text))
    if path.suffix == ".md":
        return MD_MASK.sub(" ", text)
    if path.suffix == ".html":
        # Template tags reduce to the strings inside them, so a `msgid` is
        # still read and an attribute lookup is not.
        kept = DJANGO_TAG.sub(
            lambda m: " ".join(TAG_STRING.findall(m.group(0))), text
        )
        return HTML_TAG.sub(" ", kept)
    return text


class Command(BaseCommand):
    help = "Refuse British spellings in project prose (en-US is the source language)."

    def handle(self, *args, **options):
        root = pathlib.Path(settings.BASE_DIR)
        problems = []
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if any(part in SKIP_DIRS for part in rel.parts) or not path.is_file():
                continue
            if path.suffix not in SUFFIXES and path.name not in EXTRA:
                continue
            posix = rel.as_posix()
            if any(posix.endswith(allowed) for allowed in ALLOWED):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for word, fixed in _found(prose_of(path, text)):
                problems.append((posix, word, fixed))

        if problems:
            self.stderr.write("British spellings in prose (en-US is the source language):")
            for posix, word, fixed in problems[:40]:
                self.stderr.write(f"  {posix}: {word} -> {fixed}")
            if len(problems) > 40:
                self.stderr.write(f"  … and {len(problems) - 40} more")
            self.stderr.write(
                "\nIf a spelling is deliberate — a stored value, a parse alias, or an "
                "assertion about what en-CA renders — name the file in `ALLOWED` with "
                "the reason."
            )
            raise SystemExit(1)
        self.stdout.write("Prose is US English.")
