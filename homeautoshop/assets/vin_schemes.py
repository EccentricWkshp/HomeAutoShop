"""
Pre-1981 VIN numbering schemes, transcribed (SPEC FR-VEH-12, §8.1a).

Source: LMC Truck's chassis-identification sheets, in `Artifacts/VIN Decoding/`.
Each scheme records the file it came from so a disputed entry can be checked
against the page it was read off rather than argued about.

**This is data, deliberately.** `vindecode.py` is a generic fixed-width matcher;
everything that differs between a 1953 Ford and a 1978 Dodge lives here, where
it can be corrected by somebody holding the door plate and reading the sheet.
Adding a make means adding a dict, not writing code.

A scheme is:

    id              stable key
    label           what to call it on screen
    make            for grouping and for filling in a blank make
    years           the era it covers, inclusive
    source          the file it was transcribed from
    fields          fixed-width, in order; widths must sum to the VIN's length
    tables          role -> {code: reading}
    serial_blocks   where the year is carried by the production run
    notes           anything the reader deserves to know about the transcription

A table entry is a string, or a list of `{"text": ..., "years": [from, to]}`
where one code means different things in different years — Ford's `H` is a 390
through 1976 and a 351M from 1977, and both are correct.

**Two sheets per era, and they do not always agree.** Alongside each
`*_VIN-Chassis_ID.pdf` there is an `*_Engine_ID.pdf` listing every engine code
against the years and models it was fitted to. It is the only independent check
these tables have, and reading the two against each other found the engine
table for 1953–56 claiming the 239 V8 was a 1955 only when it ran from 1954 —
which made a real 1954 truck decode as a contradiction and be refused. Where a
scheme has been checked this way it names both sheets, `source` and `also`.

Two rules govern a disagreement, and they differ because the costs differ.
**On which years a code was offered, take the union.** Being a year too broad
costs one more check; being a year too narrow refuses a vehicle that exists.
**On a detail the two simply contradict — a carburettor, a horsepower — print
neither**, or print the year-split sheet's figure and record the conflict beside
it. None of it decodes anything; a disputed number is worth less than a blank.

**On the examples printed by LMC**: several do not decode under their own
scheme — `CCS148F100043` carries a manufacturer position the sheet says is 1972
only, alongside a model-year digit meaning 1968. They are illustrations of the
layout, not real VINs, so the `example` on each scheme here is one that has been
checked to decode, and `tests_vindecode.py` checks every one of them on every
run. That is the guard that keeps a transcription slip from becoming a confident
wrong answer about somebody's truck.

Coverage, and the gaps in it. Ford trucks, Bronco and vans 1948-80; Chevrolet
and GMC trucks and vans 1947-80; Dodge trucks and vans 1971-80. Not here:

* **GMC 1951-55 1st series** - the model-year position exists only from 1954
  and the plant codes only from 1952, so the same characters mean different
  things at three different lengths within one sheet. Transcribing that
  safely needs the production-number charts below, which are not transcribed.
* **The production-number charts** several sheets carry - ranges of a running
  number that narrow a year the codes leave open. They are legible and would
  sharpen three schemes above, GMC 1960-66 most of all. They are not here
  because a year read off them depends on the plant, the drive and the tonnage
  at once, and this file has no way to say that a block applies only when
  three other positions read a certain way. That mechanism is the next thing
  worth building here.
* **1981 and later** - 17-character VINs, which vPIC decodes with more detail
  than these sheets carry (§8.1).

GMC 1960-66 and Chevrolet 1953-55 were both listed here as impossible and were
not: the first was said to have a vehicle number of no fixed length, when it is
four digits followed by a GVW letter, and the second was said to be too short
to identify, when its two-digit model year is the most identifying thing on any
of these sheets. Both are transcribed. The note is left in because a wrong
reason for leaving something out lives exactly as long as nobody rereads it.
"""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _

# ---------------------------------------------------------------------------
# Ford — FA_VIN-Chassis_ID.pdf, FB_VIN-Chassis_ID.pdf, FC_VIN-Chassis_ID.pdf,
# FBR_VIN-Chassis_ID.pdf
# ---------------------------------------------------------------------------

#: Ford's truck series codes are stable across 1957–79 apart from what the
#: second and third digits are said to mean, so each era carries its own copy
#: rather than sharing one that would have to be right for all of them.
#: The blocks Ford ran its consecutive unit numbers through, shared by the
#: trucks and the vans and printed on both sheets — which is why they are one
#: list here. The van sheet carries 1980 as well, in the same series. The two
#: documents agreeing on 1975–79 is the only independent check available on
#: any of this, and they do agree.
_FORD_BLOCKS = [
    {"from": "Q00001", "to": "S60000", "year": 1973},
    {"from": "S60001", "to": "V20000", "year": 1974},
    {"from": "V20001", "to": "X40000", "year": 1975},
    {"from": "A00001", "to": "D25000", "year": 1976},
    {"from": "000001", "to": "099999", "year": 1977},
    {"from": "X80001", "to": "Z25000", "year": 1977},
    {"from": "AE0001", "to": "CK9999", "year": 1978},
    {"from": "DC0001", "to": "FK9000", "year": 1979},
    {"from": "GA0001", "to": "KE9999", "year": 1980},
]

_FORD_PLANTS_1953 = {
    "A": _("Atlanta, GA"), "B": _("Buffalo, NY"), "C": _("Chester, PA"),
    "D": _("Dallas, TX"), "E": _("Edgewater, NJ"), "F": _("Dearborn, MI"),
    "G": _("Chicago, IL"), "K": _("Kansas City, MO"), "L": _("Long Beach, CA"),
    "M": _("Memphis, TN"), "N": _("Norfolk, VA"), "P": _("St. Paul, MN"),
    "R": _("Richmond, CA"), "S": _("Somerville, MA"), "U": _("Louisville, KY"),
}

_FORD_PLANTS_1961 = {
    "A": _("Atlanta, GA"), "C": _("Ontario, Canada"), "D": _("Dallas, TX"),
    "E": _("Mahwah, NJ"), "G": _("Chicago, IL"), "H": _("Lorain, OH"),
    "J": _("Los Angeles, CA"), "K": _("Kansas City, MO"),
    "L": _("Michigan Truck"), "N": _("Norfolk, VA"), "P": _("Twin Cities, MN"),
    "R": _("San Jose, CA"), "S": _("Allen Park, MI"), "U": _("Louisville, KY"),
    "V": _("Kentucky Truck"), "Y": _("Wixom, MI"), "Z": _("St. Louis, MO"),
}

_FORD_SERIES_1961 = {
    "F10": _("F-100 2WD"), "F11": _("F-100 4WD"), "F25": _("F-250 2WD"),
    "F26": _("F-250 4WD"), "F35": _("F-350"),
}

FORD = [
    {
        "id": "ford-truck-1948-1951",
        "also": "FA_Engine_ID.pdf",
        "label": _("Ford truck, 1948–51"),
        "make": "Ford",
        "years": (1948, 1951),
        "source": "FA_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "87HC139260",
        "fields": [
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "engine", "width": 2, "label": _("Engine")},
            {"role": "line", "width": 1, "label": _("Model line")},
            {"role": "sequence", "width": 6, "label": _("Production sequence")},
        ],
        "tables": {
            "year": {"8": 1948, "9": [1949, 1950, 1951]},
            # Years from FA_Engine_ID.pdf, which lists each code against the
            # trucks it was fitted to. Both were gone after 1950, so a 9 in the
            # year position — which alone means 1949, 1950 or 1951 — narrows to
            # the first two.
            "engine": {
                "7H": {
                    "text": _("226 CID 6-cyl, 1-bbl, 90 hp"),
                    "years": [1948, 1950],
                },
                "8R": {
                    "text": _("239 CID V8, 2-bbl, 100 hp"),
                    "years": [1948, 1950],
                },
            },
            "line": {
                "C": _("1/2 ton pickup"),
                "T": _("1 ton pickup"),
                "Y": _("3/4 ton pickup / 1 ton"),
            },
        },
        "notes": _(
            "1949, 1950 and 1951 all continue the 1949 numbering, so a 9 here "
            "cannot be narrowed to one year from the VIN alone."
        ),
    },
    {
        "id": "ford-truck-1951-1952",
        "also": "FA_Engine_ID.pdf",
        "model_from": "series",
        "label": _("Ford truck, 1951–52"),
        "make": "Ford",
        "years": (1951, 1952),
        "source": "FA_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "F1D2LU100001",
        "fields": [
            {"role": "series", "width": 2, "label": _("Series")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "plant", "width": 2, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Production sequence")},
        ],
        "tables": {
            "series": {
                "F1": _("F-1, 1/2 ton"), "F2": _("F-2, 3/4 ton"),
                "F3": _("F-3, 3/4 ton heavy duty / 1 ton"),
                "F4": _("F-4, 1-1/4 ton"),
            },
            # Years and outputs from FA_Engine_ID.pdf.
            "engine": {
                "D": {
                    "text": _("215 CID 6-cyl, 1-bbl, 101 hp"),
                    "years": [1952, 1954],
                },
                "H": {
                    "text": _("226 CID 6-cyl, 1-bbl, 95 hp"),
                    "years": [1951, 1951],
                },
                "R": {
                    "text": _("239 CID V8, 2-bbl, 110 hp"),
                    "years": [1951, 1953],
                },
            },
            # The sheet documents only 1952 here, so 1951 is left unmapped
            # rather than assumed to be a 1.
            "year": {"2": 1952},
            "plant": {
                "AT": _("Atlanta, GA"), "BF": _("Buffalo, NY"),
                "CH": _("Chicago, IL"), "CS": _("Chester, PA"),
                "DA": _("Dearborn, MI"), "DL": _("Dallas, TX"),
                "EG": _("Edgewater, NJ"), "HM": _("Highland Park, MI"),
                "KC": _("Kansas City, MO"), "LB": _("Long Beach, CA"),
                "LU": _("Louisville, KY"), "MP": _("Memphis, TN"),
                "NR": _("Norfolk, VA"), "RH": _("Richmond, CA"),
                "SP": _("St. Paul, MN"), "SR": _("Somerville, MA"),
            },
        },
    },
    {
        "id": "ford-truck-1953-1956",
        "also": "FA_Engine_ID.pdf",
        "model_from": "series",
        "label": _("Ford truck, 1953–56"),
        "make": "Ford",
        "years": (1953, 1956),
        "source": "FA_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "F25D3U100001",
        "fields": [
            {"role": "series", "width": 3, "label": _("Series")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Production sequence")},
        ],
        "tables": {
            "series": {
                "F10": _("F-100, 1/2 ton"),
                "F25": _("F-250, 3/4 ton"),
                "F35": [
                    {"text": _("F-350, 3/4 ton"), "years": [1953, 1955]},
                    {"text": _("F-350, 1 ton"), "years": [1956, 1956]},
                ],
            },
            # Corrected against FA_Engine_ID.pdf, which lists every code
            # against the years it was actually fitted. **The 239 V8 was wrong
            # here**: this table gave code V as 1955 only, and the engine sheet
            # has it in 1954 as well, so a genuine 1954 truck was being read as
            # a contradiction and refused. The two open-ended entries are now
            # bounded, which is what lets the engine narrow a year rather than
            # merely agree with one.
            "engine": {
                "D": [
                    {"text": _("215 CID 6-cyl, 1-bbl, 101 hp"), "years": [1952, 1954]},
                    {"text": _("223 CID 6-cyl, 1-bbl, 114 hp"), "years": [1955, 1956]},
                ],
                "R": {
                    "text": _("239 CID V8, 2-bbl, 110 hp"),
                    "years": [1951, 1953],
                },
                "V": [
                    {"text": _("239 CID V8, 2-bbl, 106 hp"), "years": [1954, 1955]},
                    {"text": _("272 CID V8, 2-bbl, 167 hp"), "years": [1956, 1956]},
                ],
                "Z": {"text": _("256 CID V8, 2-bbl, 140 hp"), "years": [1955, 1955]},
            },
            "year": {"3": 1953, "4": 1954, "5": 1955, "6": 1956},
            "plant": _FORD_PLANTS_1953,
        },
    },
    {
        "id": "ford-truck-1957-1958",
        "also": "FB_Engine_ID.pdf",
        "model_from": "series",
        "label": _("Ford truck, 1957–58"),
        "make": "Ford",
        "years": (1957, 1958),
        # FB_VIN-Chassis_ID.pdf and FB_Engine_ID.pdf disagree about this era's
        # 272 V8: the VIN sheet gives K as 145 hp across 1957-58 and L as 153
        # hp in 1957, while the engine sheet gives K as 176 hp in 1957 and 145
        # in 1958, and L as 145 in 1957. The engine sheet is taken because it
        # splits by year where the VIN sheet does not — which is both why it is
        # likelier right and how the disagreement became visible. Neither is
        # load-bearing: horsepower decodes nothing.
        "source": "FB_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "F25J7U100001",
        "fields": [
            {"role": "series", "width": 3, "label": _("Series")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Production sequence")},
        ],
        "tables": {
            "series": {
                "F10": _("F-100"), "F11": _("F-100 light duty"),
                "F25": _("F-250"), "F26": _("F-250 light duty"),
                "F35": _("F-350"), "F36": _("F-350 light duty"),
            },
            # The two sheets disagree here, and this takes the engine one —
            # see the scheme's note. It splits by year where the VIN sheet does
            # not, which is the reason for preferring it and also what makes
            # the disagreement visible in the first place.
            "engine": {
                "J": {
                    "text": _("223 CID 6-cyl, 1-bbl, 126 hp"),
                    "years": [1957, 1959],
                },
                "K": [
                    {"text": _("272 CID V8, 2-bbl, 176 hp"), "years": [1957, 1957]},
                    {"text": _("272 CID V8, 2-bbl, 145 hp"), "years": [1958, 1958]},
                ],
                "L": {
                    "text": _("272 CID V8, 2-bbl, 145 hp"),
                    "years": [1957, 1957],
                },
            },
            "year": {"7": 1957, "8": 1958},
            # The sheet's two plant columns are interleaved by the text
            # extraction and come out as "P St. Paul, CA" and "R San Jose, MN".
            # Read against the 1959–60 column beside it, which is intact, and
            # against geography: P is St. Paul, MN and R is San Jose, CA.
            "plant": {
                "A": _("Atlanta, GA"), "D": _("Dallas, TX"), "E": _("Mahwah, NJ"),
                "G": _("Chicago, IL"), "H": _("Detroit, MI"),
                "K": _("Kansas City, MO"), "L": _("Long Beach, CA"),
                "M": _("Memphis, TN"), "N": _("Norfolk, VA"),
                "P": _("St. Paul, MN"), "R": _("San Jose, CA"),
                "U": _("Louisville, KY"),
            },
        },
    },
    {
        "id": "ford-truck-1959-1960",
        "also": "FB_Engine_ID.pdf",
        "model_from": "series",
        "label": _("Ford truck, 1959–60"),
        "make": "Ford",
        "years": (1959, 1960),
        "source": "FB_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "F25J9U100001",
        "fields": [
            {"role": "series", "width": 3, "label": _("Series")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Production sequence")},
        ],
        "tables": {
            "series": {
                "F10": _("F-100"), "F11": _("F-100 4WD"), "F25": _("F-250"),
                "F26": _("F-250 4WD"), "F35": _("F-350"),
                "F36": _("F-350 light duty"),
            },
            # Split by year from FB_Engine_ID.pdf. The 292 changed output
            # between the two years and the four-barrel was a 1959 only, so a
            # C or a D here now says which year it is rather than merely
            # agreeing with whichever the year digit gave.
            "engine": {
                "J": _("223 CID 6-cyl, 1-bbl, 126 hp"),
                "C": [
                    {"text": _("292 CID V8, 2-bbl, 158 hp"), "years": [1959, 1959]},
                    {"text": _("292 CID V8, 2-bbl, 146 hp"), "years": [1960, 1960]},
                ],
                # The sheets disagree twice about this one, and the two are
                # handled differently on purpose. On **years** the union is
                # taken — the VIN sheet has it in 1959–60 and the engine sheet
                # only in 1959, and being a year too broad costs a check while
                # being a year too narrow refuses somebody's actual truck. On
                # the **carburettor** they simply contradict each other, 2-bbl
                # against 4-bbl, so neither is printed: a disputed detail is
                # worth less than no detail.
                "D": {
                    "text": _("292 CID V8, 160 hp"),
                    "years": [1959, 1960],
                },
            },
            "year": {"9": 1959, "0": 1960},
            "plant": {
                "A": _("Atlanta, GA"), "D": _("Dallas, TX"), "E": _("Mahwah, NJ"),
                "G": _("Chicago, IL"), "K": _("Kansas City, MO"),
                "L": _("Long Beach, CA"), "M": _("Memphis, TN"),
                "N": _("Norfolk, VA"), "P": _("St. Paul, MN"),
                "R": _("San Jose, CA"), "S": _("Allen Park, MI"),
                "U": _("Louisville, KY"),
            },
        },
    },
    {
        "id": "ford-truck-1961-1966",
        "also": "FB_Engine_ID.pdf",
        "model_from": "series",
        "label": _("Ford truck, 1961–66"),
        "make": "Ford",
        "years": (1961, 1966),
        "source": "FB_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "F25BR350001",
        "fields": [
            {"role": "series", "width": 3, "label": _("Series")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Consecutive unit number")},
        ],
        "tables": {
            "series": _FORD_SERIES_1961,
            "engine": {
                "J": {"text": _("223 CID 6-cyl, 1-bbl"), "years": [1961, 1964]},
                "A": {"text": _("240 CID 6-cyl, 1-bbl"), "years": [1966, 1966]},
                "B": [
                    {"text": _("262 CID 6-cyl, 1-bbl"), "years": [1963, 1964]},
                    {"text": _("300 CID 6-cyl, 1-bbl"), "years": [1965, 1966]},
                ],
                "C": {"text": _("292 CID V8, 2-bbl"), "years": [1961, 1964]},
                "D": [
                    {"text": _("292 CID V8, 4-bbl"), "years": [1961, 1963]},
                    {"text": _("352 CID V8, 2-bbl"), "years": [1965, 1966]},
                ],
            },
            "plant": _FORD_PLANTS_1961,
        },
        "notes": _(
            "No model-year position, and the sheet gives no serial blocks for "
            "this era, so the year narrows only as far as the engine code does."
        ),
    },
    {
        "id": "ford-truck-1967-1972",
        "also": "FB_Engine_ID.pdf",
        "model_from": "series",
        "label": _("Ford truck, 1967–72"),
        "make": "Ford",
        "years": (1967, 1972),
        "source": "FB_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "F25BR746001",
        "fields": [
            {"role": "series", "width": 3, "label": _("Series")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Consecutive unit number")},
        ],
        "tables": {
            "series": _FORD_SERIES_1961,
            "engine": {
                "A": _("240 CID 6-cyl, 1-V"),
                "B": _("300 CID 6-cyl, 1-V"),
                "G": {"text": _("302 CID V8, 2-V"), "years": [1970, 1972]},
                "Y": [
                    {"text": _("352 CID V8, 2-V"), "years": [1967, 1967]},
                    {"text": _("360 CID V8, 2-V"), "years": [1968, 1972]},
                ],
                "H": {"text": _("390 CID V8, 2-V"), "years": [1968, 1972]},
            },
            "plant": {
                "C": _("Ontario, Canada"), "D": _("Dallas, TX"),
                "E": _("Mahwah, NJ"), "G": _("Chicago, IL"),
                "H": _("Lorain, OH"), "K": _("Kansas City, MO"),
                "L": _("Michigan Truck"), "N": _("Norfolk, VA"),
                "P": _("Twin Cities, MN"), "R": _("San Jose, CA"),
                "S": _("Allen Park, MI"), "U": _("Louisville, KY"),
                "V": _("Kentucky Truck"),
            },
        },
    },
    {
        "id": "ford-truck-1973-1979",
        "also": "FC_Engine_ID.pdf",
        "model_from": "series",
        "label": _("Ford truck, 1973–79"),
        "make": "Ford",
        "years": (1973, 1979),
        "source": "FC_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "F26SVAE1234",
        "fields": [
            {"role": "series", "width": 3, "label": _("Series")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Consecutive unit number")},
        ],
        "tables": {
            "series": {
                "F10": _("F-100 2WD"), "X10": _("F-100 2WD Super Cab"),
                "F11": _("F-100 4WD"), "F14": _("F-150 4WD"),
                "X14": _("F-150 4WD Super Cab"), "F15": _("F-150 2WD"),
                "X15": _("F-150 2WD Super Cab"), "F25": _("F-250 2WD"),
                "X25": _("F-250 2WD Super Cab"), "F26": _("F-250 4WD"),
                "X26": _("F-250 4WD Super Cab"), "F35": _("F-350 2WD"),
                "X35": _("F-350 Super Cab"), "F36": _("F-350 4WD"),
                "U15": _("U-150 4WD Bronco Wagon"),
            },
            "engine": {
                "A": {"text": _("240 CID 4.0L 6-cyl, 1-bbl"), "years": [1973, 1974]},
                "B": _("300 CID 4.9L 6-cyl, 1-bbl"),
                "G": _("302 CID 5.0L V8, 2-bbl"),
                "H": [
                    {"text": _("351M CID 5.8L V8, 2-bbl"), "years": [1977, 1979]},
                    {"text": _("390 CID 6.4L V8, 2-bbl"), "years": [1973, 1976]},
                ],
                "Y": {"text": _("360 CID 5.9L V8, 2-bbl"), "years": [1973, 1976]},
                "M": {"text": _("390 CID 6.4L V8, 4-bbl"), "years": [1974, 1976]},
                "S": {"text": _("400 CID 6.6L V8, 2-bbl"), "years": [1977, 1979]},
                "J": _("460 CID 7.5L V8, 4-bbl"),
            },
            "plant": {
                "B": _("Oakville, Ontario, Canada"), "C": _("Ontario, Canada"),
                "E": _("Mahwah, NJ"), "H": _("Lorain, OH"),
                "I": _("Highland Park, MI"), "K": _("Kansas City, MO"),
                "L": _("Michigan Truck"), "N": _("Norfolk, VA"),
                "P": _("Twin Cities, MN"), "R": _("San Jose, CA"),
                "S": _("Allen Park, MI"), "U": _("Louisville, KY"),
                "V": _("Kentucky Truck"),
            },
        },
        # There is no model-year position at all: the year is which block of
        # the production run the unit number falls in.
        "serial_blocks": _FORD_BLOCKS,
    },
    {
        "id": "ford-bronco-1966-1977",
        "model_from": "series",
        "label": _("Ford Bronco, 1966–77"),
        "make": "Ford",
        "years": (1966, 1977),
        "source": "FBR_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "U15SLQ00500",
        "fields": [
            {"role": "series", "width": 3, "label": _("Series")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Production sequence")},
        ],
        "tables": {
            "series": {
                "U13": _("U-100 Roadster"), "U14": _("U-100 Pickup"),
                "U15": _("U-100 Wagon"),
            },
            "engine": {
                "F": {"text": _("170 CID 2.8L 6-cyl, 1-bbl"), "years": [1966, 1972]},
                "S": {"text": _("200 CID 3.3L 6-cyl, 1-bbl"), "years": [1973, 1974]},
                "A": {"text": _("240 CID 3.9L 6-cyl, 1-bbl"), "years": [1968, 1968]},
                "N": {"text": _("289 CID 4.7L V8, 2-bbl"), "years": [1966, 1968]},
                "G": {"text": _("302 CID 5.0L V8, 2-bbl"), "years": [1968, 1977]},
            },
            "plant": {
                "H": _("Lorain, OH"), "L": _("Michigan Truck"),
                "R": _("San Jose, CA"), "S": _("Allen Park, MI"),
            },
        },
        "serial_blocks": [
            {"from": "732001", "to": "914000", "year": 1966},
            {"from": "A00001", "to": "B82000", "year": 1967},
            {"from": "C00001", "to": "D82000", "year": 1968},
            {"from": "D82001", "to": "G30000", "year": 1969},
            {"from": "G30001", "to": "J70000", "year": 1970},
            {"from": "J70001", "to": "M30000", "year": 1971},
            {"from": "M40001", "to": "Q00000", "year": 1972},
            {"from": "Q00001", "to": "S60000", "year": 1973},
            {"from": "S60001", "to": "V00000", "year": 1974},
            {"from": "V00001", "to": "X40000", "year": 1975},
            {"from": "A00001", "to": "C75000", "year": 1976},
            {"from": "000001", "to": "Z20000", "year": 1977},
        ],
        "notes": _(
            "The blocks overlap: A00,001 starts both 1967 and 1976, and the "
            "1977 block spans the whole field. Where two years fit, both are "
            "reported — the engine code often settles it."
        ),
        # LMC prints U15SLAE0010 here, which does not decode: AE0,010 is in the
        # 1967 and 1976 blocks while engine S was only offered in 1973–74. The
        # example above is a consistent one.
    },
]



FORD_LATER = [
    {
        "id": "ford-truck-1980",
        "also": "FD_Engine_ID.pdf",
        "model_from": "series",
        "label": _("Ford truck, 1980"),
        "make": "Ford",
        "years": (1980, 1980),
        "source": "FD_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "F10EU100001",
        "fields": [
            {"role": "series", "width": 3, "label": _("Series")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Consecutive unit number")},
        ],
        "tables": {
            "series": {
                "F10": _("F-100 2WD"), "X10": _("F-100 2WD Super Cab"),
                "F11": _("F-100 4WD"), "F15": _("F-150 2WD"),
                "X15": _("F-150 2WD Super Cab"), "F14": _("F-150 4WD"),
                "X14": _("F-150 4WD Super Cab"), "F25": _("F-250 2WD"),
                "X25": _("F-250 2WD Super Cab"), "F26": _("F-250 4WD"),
                "X26": _("F-250 4WD Super Cab"), "F35": _("F-350 2WD"),
                "X35": _("F-350 2WD Super Cab"), "F36": _("F-350 4WD"),
                "U15": _("U-150 4WD Bronco Wagon"),
            },
            # A completely different set from 1973–79, which is what separates
            # a 1980 from the seven years before it that share its shape.
            "engine": {
                "E": _("300 CID 4.9L 6-cyl, 1-bbl"),
                "F": _("302 CID 5.0L V8, 2-bbl"),
                "W": _("351M CID 5.8L V8, 2-bbl"),
                "Z": _("400 CID 6.6L V8, 2-bbl"),
            },
            "plant": {
                "B": _("Oakville, Ontario, Canada"), "C": _("Ontario, Canada"),
                "E": _("Mahwah, NJ"), "H": _("Lorain, OH"),
                "I": _("Highland Park, MI"), "J": _("Monterrey, Mexico"),
                "K": _("Kansas City, MO"), "L": _("Michigan Truck"),
                "N": _("Norfolk, VA"), "P": _("Twin Cities, MN"),
                "R": _("San Jose, CA"), "S": _("Allen Park, MI"),
                "U": _("Louisville, KY"), "V": _("Kentucky Truck"),
            },
        },
        "notes": _(
            "The last year before the 17-character VIN. The sheet gives no "
            "serial blocks for it, and none are needed: the scheme covers one "
            "model year."
        ),
    },
    {
        "id": "ford-van-1975-1980",
        "model_from": "series",
        "label": _("Ford van, 1975–80"),
        "make": "Ford",
        "years": (1975, 1980),
        "source": "ford-van-vin.pdf",
        "vehicle_class": "truck",
        "example": "E04JKAE0021",
        "fields": [
            {"role": "series", "width": 3, "label": _("Series")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Consecutive unit number")},
        ],
        "tables": {
            "series": {
                "E01": _("E-100 Club Wagon, 5 passenger"),
                "E02": _("E-100 Club Wagon, 8 passenger"),
                "E04": _("E-100 Cargo Van"),
                "E05": _("E-100 Window Van"),
                "E06": _("E-100 Display Van"),
                "E11": _("E-150 Club Wagon, 5 passenger"),
                "E12": _("E-150 Club Wagon, 8 passenger"),
                "E14": _("E-150 Cargo Van"),
                "E15": _("E-150 Window Van"),
                "E18": _("E-150 Display Van"),
                "E20": _("E-250 Club Wagon, 11 passenger"),
                "E21": _("E-250 Club Wagon, 5 passenger"),
                "E22": _("E-250 Club Wagon, 8 passenger"),
                "E23": _("E-250 Club Wagon, 12 passenger"),
                "E24": _("E-250 Cargo Van"),
                "E25": _("E-250 Window Van"),
                "E26": _("E-250 Display Van"),
                "E34": _("E-350 Cargo Van"),
                "E35": _("E-350 Window Van"),
                "E36": _("E-350 Display Van"),
                "S11": _("E-150 Club Wagon, 5 passenger, Super Cab"),
                "S12": _("E-150 Club Wagon, 8 passenger, Super Cab"),
                "S14": _("E-150 Cargo Van, Super Cab"),
                "S15": _("E-150 Window Van, Super Cab"),
                "S16": _("E-150 Display Van, Super Cab"),
                "S20": _("E-250 Club Wagon, 11 passenger, Super Cab"),
                "S21": _("E-250 Club Wagon, 5 passenger, Super Cab"),
                "S22": _("E-250 Super Club Wagon, 8 passenger"),
                "S23": _("E-250 Super Club Wagon, 12 passenger"),
                "S25": _("E-250 Window Van, Super Van"),
                "S26": _("E-250 Display Van, Super Van"),
                "S29": _("E-250 Super Club Wagon, 15 passenger"),
                "S30": _("E-350 Super Club Wagon, 11 passenger"),
                "S31": _("E-350 Super Club Wagon, 5 passenger"),
                "S32": _("E-350 Super Club Wagon, 8 passenger"),
                "S33": _("E-350 Super Club Wagon, 12 passenger"),
                "S34": _("E-350 Cargo Van, Super Van"),
                "S35": _("E-350 Window Van, Super Van"),
                "S36": _("E-350 Display Van, Super Van"),
                "S39": _("E-350 Super Club Wagon, 15 passenger"),
            },
            "engine": {
                "B": _("300 CID 4.9L 6-cyl, 1-bbl"),
                "K": {
                    "text": _("300 CID 4.9L 6-cyl, 1-bbl, heavy duty"),
                    "years": [1978, 1980],
                },
                "G": _("302 CID 5.0L V8, 2-bbl"),
                "H": {"text": _("351W CID 5.8L V8, 2-bbl"), "years": [1977, 1980]},
                "J": _("460 CID 7.5L V8, 4-bbl"),
            },
            "plant": {
                "B": _("Oakville, Ontario, Canada"), "C": _("Ontario, Canada"),
                "E": _("Mahwah, NJ"), "H": _("Lorain, OH"),
                "I": _("Highland Park, MI"), "K": _("Kansas City, MO"),
                "L": _("Michigan Truck"), "N": _("Norfolk, VA"),
                "P": _("Twin Cities, MN"), "S": _("Allen Park, MI"),
                "U": _("Louisville, KY"), "V": _("Kentucky Truck"),
            },
        },
        "serial_blocks": _FORD_BLOCKS,
        "notes": _(
            "This sheet was scanned rather than typeset, and the S-prefixed "
            "series codes came through with the S read as other symbols. They "
            "are transcribed as S because every neighbour is one and the "
            "descriptions survived intact; the E-prefixed codes needed no such "
            "reading."
        ),
        # Two entries carry a real doubt rather than a resolved one, and are
        # left as they are rather than smoothed over. E18 is printed here where
        # the 1981–92 column prints E16 for the same "E150 Display Van", and
        # 6/8 is exactly the confusion this scan makes elsewhere; it is
        # transcribed as printed. S24 is absent from this column and present in
        # the next, and is left out rather than assumed into existence.
    },
]


# ---------------------------------------------------------------------------
# Chevrolet and GMC — CBE_VIN-Chassis_ID.pdf, CB_VIN-Chassis_ID.pdf,
# CC_VIN-Chassis_ID.pdf
# ---------------------------------------------------------------------------

#: The engine letters GM ran through its light trucks and vans, printed as a
#: table per model year on every one of these sheets. Shared here because they
#: really are the same table — `CC_VIN-Chassis_ID.pdf` prints it typeset and
#: legible, and the van sheet prints the same codes through a scan that broke
#: them into pieces ("T X = = 2 3 0 0 2 7 6 V C 8 y l"). Taking the legible
#: copy is reading the same table twice, not guessing at the broken one.
_GM_ENGINES_1973 = {
    "Q": [
        {"text": _("250 CID 6-cyl"), "years": [1973, 1975]},
        {"text": _("305 CID V8, 2-bbl"), "years": [1976, 1976]},
    ],
    "T": _("292 CID 6-cyl"),
    "X": {"text": _("307 CID V8"), "years": [1973, 1975]},
    "V": {"text": _("350 CID V8, 2-bbl"), "years": [1973, 1976]},
    "Y": {"text": _("350 CID V8, 4-bbl"), "years": [1973, 1975]},
    "D": {"text": _("250 CID 6-cyl"), "years": [1976, 1980]},
    "L": {"text": _("350 CID V8, 4-bbl"), "years": [1976, 1980]},
    "U": [
        {"text": _("400 CID V8, 4-bbl"), "years": [1973, 1976]},
        {"text": _("305 CID V8"), "years": [1977, 1979]},
    ],
    "R": {"text": _("400 CID V8"), "years": [1977, 1979]},
    "S": [
        {"text": _("454 CID V8, 4-bbl"), "years": [1976, 1976]},
        {"text": _("454 CID V8"), "years": [1977, 1979]},
    ],
    "Z": [
        {"text": _("454 CID V8, 4-bbl"), "years": [1973, 1975]},
        {"text": _("350 CID V8 diesel"), "years": [1978, 1980]},
    ],
    "G": {"text": _("305 CID V8"), "years": [1980, 1980]},
    "M": {"text": _("350 CID V8, 4-bbl"), "years": [1980, 1980]},
    "W": {"text": _("454 CID V8"), "years": [1980, 1980]},
}

_GM_YEARS_1973 = {
    "3": 1973, "4": 1974, "5": 1975, "6": 1976, "7": 1977,
    "8": 1978, "9": 1979, "A": 1980,
}

_GM_PLANTS = {
    "A": _("Atlanta, GA"), "B": _("Baltimore, MD"), "F": _("Flint, MI"),
    "J": _("Janesville, WI"), "K": _("Kansas City, MO"), "N": _("Norwood, OH"),
    "P": _("Pontiac, MI"), "S": _("St. Louis, MO"), "T": _("Tarrytown, NY"),
    "Z": _("Fremont, CA"), "1": _("Oshawa, Ontario"),
}

_GM_SERIES_1960 = {
    "14": _("1/2 ton shortbed, 115 in wheelbase"),
    "15": _("1/2 ton longbed, 127 in wheelbase"),
    "25": _("3/4 ton longbed, 127 in wheelbase"),
}

_GM_CHASSIS = {"C": _("2WD"), "K": _("4WD")}

CHEVROLET = [
    {
        "id": "chevrolet-truck-1960-1964",
        "label": _("Chevrolet truck, 1960–64"),
        "make": "Chevrolet",
        "years": (1960, 1964),
        "source": "CBE_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "1C154F103455",
        "fields": [
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "chassis", "width": 1, "label": _("Drive")},
            {"role": "series", "width": 2, "label": _("Series")},
            {"role": "body", "width": 1, "label": _("Truck type")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Vehicle number")},
        ],
        "tables": {
            "year": {"0": 1960, "1": 1961, "2": 1962, "3": 1963, "4": 1964},
            "chassis": _GM_CHASSIS,
            "series": _GM_SERIES_1960,
            "body": {"4": _("Pickup")},
            "plant": _GM_PLANTS,
        },
    },
    {
        "id": "chevrolet-truck-1965-1966",
        "label": _("Chevrolet truck, 1965–66"),
        "make": "Chevrolet",
        "years": (1965, 1966),
        "source": "CBE_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "C1446S107722",
        "fields": [
            {"role": "chassis", "width": 1, "label": _("Drive")},
            {"role": "series", "width": 2, "label": _("Series")},
            {"role": "body", "width": 1, "label": _("Truck type")},
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Vehicle number")},
        ],
        "tables": {
            "chassis": _GM_CHASSIS,
            "series": _GM_SERIES_1960,
            "body": {"4": _("Pickup")},
            "year": {"5": 1965, "6": 1966},
            "plant": _GM_PLANTS,
        },
    },
    {
        "id": "chevrolet-truck-1967-1971",
        "label": _("Chevrolet truck, 1967–71"),
        "make": "Chevrolet",
        "years": (1967, 1971),
        "source": "CB_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "CS148F100043",
        "fields": [
            {"role": "chassis", "width": 1, "label": _("Chassis")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "gvw", "width": 1, "label": _("GVW range")},
            {"role": "body", "width": 1, "label": _("Model type")},
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Vehicle number")},
        ],
        "tables": {
            "chassis": {"C": _("Conventional"), "K": _("4WD")},
            "engine": {"S": _("6-cylinder"), "E": _("V8")},
            "gvw": {
                "1": _("3,900–5,800 lb, 1/2 ton"),
                "2": _("5,200–7,500 lb, 3/4 ton"),
                "3": _("6,600–14,000 lb, 1 ton"),
            },
            "body": {"4": _("Pickup"), "8": _("Blazer"), "6": _("Suburban")},
            "year": {"7": 1967, "8": 1968, "9": 1969, "0": 1970, "1": 1971},
            "plant": _GM_PLANTS,
        },
        "notes": _(
            "The manufacturer letter in front appears from 1972 only, which is "
            "why 1972 is a separate scheme one character longer."
        ),
    },
    {
        "id": "chevrolet-gmc-truck-1972",
        "label": _("Chevrolet / GMC truck, 1972"),
        "make": "Chevrolet",
        "years": (1972, 1972),
        "source": "CB_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "TCS142S500121",
        "fields": [
            {"role": "division", "width": 1, "label": _("Make")},
            {"role": "chassis", "width": 1, "label": _("Chassis")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "gvw", "width": 1, "label": _("GVW range")},
            {"role": "body", "width": 1, "label": _("Model type")},
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Vehicle number")},
        ],
        "tables": {
            "division": {"C": _("Chevrolet"), "T": _("GMC")},
            "chassis": {"C": _("2WD"), "K": _("4WD")},
            "engine": {
                "S": _("inline 6-cylinder"), "M": _("V6"), "E": _("V8"),
            },
            "gvw": {
                "1": _("3,900–5,800 lb, 1/2 ton"),
                "2": _("5,200–7,500 lb, 3/4 ton"),
                "3": _("6,600–14,000 lb, 1 ton"),
            },
            "body": {"4": _("Pickup"), "8": _("Jimmy / Blazer"), "6": _("Suburban")},
            "year": {"2": 1972},
            "plant": _GM_PLANTS,
        },
    },
    {
        "id": "chevrolet-gmc-truck-1973-1980",
        "label": _("Chevrolet / GMC truck, 1973–80"),
        "make": "Chevrolet",
        "years": (1973, 1980),
        "source": "CC_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "CCL148Z100327",
        "fields": [
            {"role": "division", "width": 1, "label": _("Make")},
            {"role": "chassis", "width": 1, "label": _("Drive")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "series", "width": 1, "label": _("Series")},
            {"role": "body", "width": 1, "label": _("Body type")},
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Build sequence")},
        ],
        "tables": {
            "division": {"C": _("Chevrolet"), "T": _("GMC")},
            "chassis": {"C": _("2WD"), "K": _("4WD")},
            "engine": _GM_ENGINES_1973,
            "series": {"1": _("1/2 ton"), "2": _("3/4 ton"), "3": _("1 ton")},
            # The Suburban and Blazer share this scheme and differ only here,
            # per CSB_VIN-Chassis_ID.pdf. They were Flint-only, which this does
            # not enforce: refusing a plant the sheet happens not to list would
            # reject a real vehicle to make a point.
            "body": {
                "4": _("Pickup"),
                "6": _("Suburban"),
                "8": _("Blazer / Jimmy"),
            },
            "year": _GM_YEARS_1973,
            "plant": {
                "A": _("Lakewood, GA"), "B": _("Baltimore, MD"),
                "F": _("Flint, MI"), "J": _("Janesville, WI"),
                "S": _("St. Louis, MO"), "V": _("Pontiac, MI"),
                "Z": _("Fremont, CA"),
            },
        },
        "notes": _(
            "The series digit is the GM 10/20/30 convention, read from the "
            "1981–89 column printed beside this one; the 1973–80 column names "
            "the position without listing its codes."
        ),
        # LMC prints CCY148Z100327, which does not decode: engine Y was offered
        # 1973–75 and the year digit 8 is 1978. The example above swaps in L,
        # a 350 offered in 1978.
    },
]


# ---------------------------------------------------------------------------
# Dodge — DC_VIN-Chassis_ID.pdf
# ---------------------------------------------------------------------------

DODGE = [
    {
        "id": "dodge-truck-1972-1980",
        "label": _("Dodge truck, 1972–80"),
        "make": "Dodge",
        "years": (1972, 1980),
        "source": "DC_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "D14AE5S000105",
        "fields": [
            {"role": "model", "width": 1, "label": _("Model")},
            {"role": "series", "width": 1, "label": _("Chassis rating")},
            {"role": "body", "width": 1, "label": _("Body")},
            {"role": "gvw", "width": 1, "label": _("GVWR")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Sequence number")},
        ],
        "tables": {
            "model": {
                "A": _("4WD SUV"), "D": _("2WD truck"),
                "E": _("2WD SUV"), "W": _("4WD truck"),
            },
            "series": {
                "A": _("1/2 ton Plymouth"), "1": _("1/2 ton Dodge"),
                "2": _("3/4 ton"), "3": _("1 ton"),
            },
            "body": {
                "0": _("Ramcharger SUV"),
                "3": _("Standard cab Utiline (stepside)"),
                "4": _("Standard cab Sweptline (fleetside)"),
                "5": _("Crew cab Utiline (stepside)"),
                "6": _("Crew cab Sweptline (fleetside)"),
                "7": _("Club cab Sweptline (fleetside)"),
            },
            "gvw": {
                "A": _("under 6,000 lb"), "B": _("6,001–10,000 lb"),
            },
            "engine": {
                "A": {"text": _("440 CID V8, 3-bbl"), "years": [1972, 1978]},
                "B": _("225 CID 6-cyl, 1-bbl"),
                "C": _("225 CID 6-cyl, 2-bbl"),
                "D": {"text": _("440 CID V8, 1-bbl"), "years": [1972, 1978]},
                "E": _("318 CID V8, 1-bbl"),
                "F": _("360 CID V8, 1-bbl"),
                "G": {"text": _("318 CID V8, 3-bbl"), "years": [1971, 1978]},
                "J": {"text": _("400 CID V8"), "years": [1972, 1978]},
                "K": {"text": _("360 CID V8, 3-bbl"), "years": [1974, 1980]},
                "P": {"text": _("318 CID V8, 1-bbl"), "years": [1978, 1980]},
                "S": {"text": _("360 CID V8"), "years": [1978, 1979]},
                "T": {"text": _("360 CID V8"), "years": [1974, 1980]},
            },
            "year": {
                "2": 1972, "3": 1973, "4": 1974, "5": 1975, "6": 1976,
                "7": 1977, "8": 1978, "9": 1979, "A": 1980,
            },
            "plant": {
                "J": _("Windsor, Ontario, Canada"),
                "K": _("Windsor, Ontario, Canada"),
                "S": _("Warren, MI"), "T": _("Warren, MI"), "V": _("Warren, MI"),
                "U": _("St. Louis, MO"), "X": _("St. Louis, MO"),
            },
        },
    },
]


_GM_PLANTS_1953 = {
    "A": _("Atlanta, GA"), "B": _("Baltimore, MD"), "F": _("Flint, MI"),
    "J": _("Janesville, WI"), "K": _("Kansas City, MO"), "L": _("Los Angeles, CA"),
    "N": _("Norwood, OH"), "O": _("Oakland, CA"), "P": _("Pontiac, MI"),
    "S": _("St. Louis, MO"), "T": _("Tarrytown, NY"), "W": _("Willow Run, MI"),
}

GM_EARLY = [
    {
        "id": "chevrolet-truck-1947-1952",
        "model_from": "series",
        "label": _("Chevrolet truck, 1947–52"),
        "make": "Chevrolet",
        "years": (1947, 1952),
        "source": "CA_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "5GRB292",
        "fields": [
            {"role": "plant", "width": 1, "label": _("Factory")},
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "series", "width": 1, "label": _("Series")},
            {"role": "month", "width": 1, "label": _("Month built")},
            # The production number ran as long as it needed to, so this takes
            # whatever is left rather than a count the sheet never gives.
            {"role": "sequence", "width": 0, "label": _("Production number")},
        ],
        "tables": {
            "plant": {
                "1": _("Flint, MI"), "2": _("Tarrytown, NY"),
                "3": _("St. Louis, MO"), "5": _("Kansas City, MO"),
                "6": _("Oakland, CA"), "8": _("Atlanta, GA"),
                "9": _("Norwood, OH"),
            },
            "year": {
                "E": 1947, "F": 1948, "G": 1949, "H": 1950, "J": 1951, "K": 1952,
            },
            "series": {
                "P": _("3100, 1/2 ton pickup, 116 in wheelbase"),
                "R": _("3600, 3/4 ton pickup, 125.25 in wheelbase"),
            },
            "month": {
                "A": _("January"), "B": _("February"), "C": _("March"),
                "D": _("April"), "E": _("May"), "F": _("June"), "G": _("July"),
                "H": _("August"), "I": _("September"), "J": _("October"),
                "K": _("November"), "L": _("December"),
            },
        },
        "notes": _(
            "The two-digit factory codes this sheet also lists — 12 Buffalo, "
            "14 Baltimore, 20 Los Angeles, 21 Janesville — cannot be told from "
            "a one-digit code followed by a year letter, and are left out."
        ),
    },
    {
        "id": "chevrolet-truck-1955-1959",
        "model_from": "series",
        "label": _("Chevrolet truck, 1955–59"),
        "make": "Chevrolet",
        "years": (1955, 1959),
        "source": "CA_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "3E57S7552",
        "fields": [
            {"role": "series", "width": 2, "label": _("Series")},
            {"role": "year", "width": 2, "label": _("Model year")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 0, "label": _("Production number")},
        ],
        "tables": {
            "series": {
                "H2": _("3100 (1955 2nd series)"),
                "M2": _("3200 (1955 2nd series)"),
                "J2": _("3600 (1955 2nd series)"),
                "3A": _("3100"), "3B": _("3200"), "3E": _("3600"),
            },
            "year": {
                "53": 1953, "54": 1954, "55": 1955, "56": 1956, "57": 1957,
                "58": 1958, "59": 1959,
            },
            "plant": _GM_PLANTS_1953,
        },
        "notes": _(
            "A leading V marks a V8 and is read by the scheme below. The "
            "one-letter series codes for 1953–55 1st series (H, J) are left "
            "out: four characters and a running number is too little to "
            "identify, and it would match half the shelf."
        ),
    },
    {
        "id": "chevrolet-truck-1955-1959-v8",
        "model_from": "series",
        "label": _("Chevrolet truck V8, 1955–59"),
        "make": "Chevrolet",
        "years": (1955, 1959),
        "source": "CA_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "V3E57S7552",
        "fields": [
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "series", "width": 2, "label": _("Series")},
            {"role": "year", "width": 2, "label": _("Model year")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 0, "label": _("Production number")},
        ],
        "tables": {
            # Blank for the standard six, which is why this is a scheme of its
            # own: a position that is sometimes absent is a different length,
            # not a different value.
            "engine": {"V": _("V8")},
            "series": {
                "H2": _("3100 (1955 2nd series)"),
                "M2": _("3200 (1955 2nd series)"),
                "J2": _("3600 (1955 2nd series)"),
                "3A": _("3100"), "3B": _("3200"), "3E": _("3600"),
            },
            "year": {
                "53": 1953, "54": 1954, "55": 1955, "56": 1956, "57": 1957,
                "58": 1958, "59": 1959,
            },
            "plant": _GM_PLANTS_1953,
        },
    },
    {
        "id": "gmc-truck-1947-1950",
        "label": _("GMC truck, 1947–50"),
        "make": "GMC",
        "years": (1947, 1950),
        "source": "CA_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "FC15225889",
        "fields": [
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "body", "width": 1, "label": _("Cab style")},
            {"role": "series", "width": 2, "label": _("Chassis rating")},
            {"role": "wheelbase", "width": 1, "label": _("Wheelbase")},
            {"role": "sequence", "width": 0, "label": _("Production number")},
        ],
        "tables": {
            "year": {"F": [1947, 1948, 1949, 1950]},
            "body": {"C": _("Conventional cab")},
            "series": {"10": _("1/2 ton"), "15": _("3/4 ton")},
            "wheelbase": {"1": _("116 in"), "2": _("125.25 in")},
        },
        "notes": _(
            "One code covers all four years. The sheet narrows it with charts "
            "of production-number ranges per plant per year, which are ranges "
            "of a running number rather than codes, and are not transcribed."
        ),
    },
    {
        "id": "gmc-truck-1955-1959",
        "label": _("GMC truck, 1955–59 2nd series"),
        "make": "GMC",
        "years": (1955, 1959),
        "source": "CA_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "152PT5935",
        "fields": [
            {"role": "series", "width": 2, "label": _("Chassis rating")},
            {"role": "wheelbase", "width": 1, "label": _("Wheelbase")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "sequence", "width": 0, "label": _("Production number")},
        ],
        "tables": {
            "series": {"10": _("1/2 ton"), "15": _("3/4 ton")},
            "wheelbase": {"1": _("114 in"), "2": _("123.25 in")},
            "plant": {"P": _("Pontiac, MI"), "C": _("Oakland, CA")},
            "year": {
                "Y": [1955], "X": [1956], "T": [1957], "S": [1958, 1959],
            },
        },
        "notes": _(
            "An 8 before the plant marks a V8 and is read by the scheme below; "
            "the position is blank for a six, which makes it a length rather "
            "than a value."
        ),
    },
    {
        "id": "gmc-truck-1955-1959-v8",
        "label": _("GMC truck V8, 1955–59 2nd series"),
        "make": "GMC",
        "years": (1955, 1959),
        "source": "CA_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "1528PT5935",
        "fields": [
            {"role": "series", "width": 2, "label": _("Chassis rating")},
            {"role": "wheelbase", "width": 1, "label": _("Wheelbase")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "sequence", "width": 0, "label": _("Production number")},
        ],
        "tables": {
            "series": {"10": _("1/2 ton"), "15": _("3/4 ton")},
            "wheelbase": {"1": _("114 in"), "2": _("123.25 in")},
            "engine": {"8": _("V8")},
            "plant": {"P": _("Pontiac, MI"), "C": _("Oakland, CA")},
            "year": {
                "Y": [1955], "X": [1956], "T": [1957], "S": [1958, 1959],
            },
        },
    },
    {
        "id": "gmc-truck-1967-1971",
        "label": _("GMC truck, 1967–71"),
        "make": "GMC",
        "years": (1967, 1971),
        "source": "CB_VIN-Chassis_ID.pdf",
        "vehicle_class": "truck",
        "example": "CE134S113045",
        "fields": [
            {"role": "chassis", "width": 1, "label": _("Drive")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "gvw", "width": 1, "label": _("GVW range")},
            {"role": "body", "width": 2, "label": _("Model type")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Vehicle number")},
        ],
        "tables": {
            "chassis": {"C": _("2WD"), "K": _("4WD")},
            "engine": {
                "S": _("inline 6-cylinder"), "M": _("V6"), "E": _("V8"),
            },
            "gvw": {
                "1": _("3,900–5,800 lb, 1/2 ton"),
                "2": _("5,200–7,500 lb, 3/4 ton"),
                "3": _("6,600–14,000 lb, 1 ton"),
            },
            "body": {
                "0C": _("Stepside (1967–69)"),
                "04": _("Stepside (1970–71)"),
                "0D": _("Fleetside (1967–69)"),
                "34": _("Fleetside (1970–71)"),
                "0K": _("Suburban with cargo doors (1967–69)"),
                "06": _("Suburban with cargo doors (1970–71)"),
                "0L": _("Suburban with tailgate (1967–69)"),
                "16": _("Suburban with tailgate (1970–71)"),
                "14": _("Jimmy"),
            },
            "plant": _GM_PLANTS,
        },
        "notes": _(
            "**No model year is reported, because the sheet gives no way to "
            "read one.** There is no year position, and the serial rule it "
            "offers instead — 1967 starts at 1001, 1968 at 10001, 1969 at "
            "10001 — does not separate 1968 from 1969. What the number does "
            "say is said; the year is left to the reader."
        ),
        # GMC prints a position between the model type and the plant reserved
        # for Pennsylvania, Maryland and New York designations, shown as a dash
        # where it is not used — which is nearly always. It is not a field
        # here: `normalize` strips dashes so that a VIN can be typed with them,
        # so the ordinary truck arrives twelve characters long. One carrying a
        # real state letter will not decode, and that is the honest failure.
    },
]


#: GMC's model code names a span rather than a year. The sheet narrows N and F
#: further with charts of vehicle-number ranges per plant, per drive and per
#: tonnage — legible, and not transcribed here: they are the only place in this
#: file where a year would depend on three other positions at once, and the
#: mechanism for that does not exist yet. The span is reported instead.
_GMC_MODEL_YEARS = {
    "N": [1960, 1961],
    "J": [1962],
    "G": [1963],
    "F": [1964, 1965, 1966],
    "D": [1966],
}

_GMC_1960_FIELDS = [
    {"role": "series", "width": 2, "label": _("Series")},
    {"role": "wheelbase", "width": 2, "label": _("Wheelbase")},
    {"role": "plant", "width": 1, "label": _("Assembly plant")},
    {"role": "year", "width": 1, "label": _("Model code")},
    {"role": "sequence", "width": 4, "label": _("Vehicle number")},
    {"role": "gvw", "width": 1, "label": _("GVW rating")},
]

_GMC_1960_TABLES = {
    "series": {"10": _("1/2 ton"), "15": _("3/4 ton")},
    "wheelbase": {"01": _("115 in"), "02": _("127 in")},
    "plant": {
        "P": _("Pontiac, MI"), "H": _("Oakland, CA"),
        "C": _("Oakland, CA"), "Z": _("Fremont, CA"),
    },
    "year": _GMC_MODEL_YEARS,
    "gvw": {"A": _("5,000 lb"), "B": _("7,000 lb")},
}

GM_LAST = [
    {
        "id": "gmc-truck-1960-1966",
        "label": _("GMC truck 2WD, 1960–66"),
        "make": "GMC",
        "vehicle_class": "truck",
        "years": (1960, 1966),
        "source": "CBE_VIN-Chassis_ID.pdf",
        "example": "1502PN2611A",
        "fields": _GMC_1960_FIELDS,
        "tables": _GMC_1960_TABLES,
        "notes": _(
            "The leading position is blank on a two-wheel-drive truck, which "
            "makes the drive a length rather than a value — the two schemes "
            "below carry it."
        ),
    },
    {
        "id": "gmc-truck-1960-1966-4wd",
        "label": _("GMC truck 4WD, 1960–66"),
        "make": "GMC",
        "vehicle_class": "truck",
        "years": (1960, 1966),
        "source": "CBE_VIN-Chassis_ID.pdf",
        "example": "K1502PN2611A",
        "fields": [
            {"role": "chassis", "width": 1, "label": _("Drive")},
            *_GMC_1960_FIELDS,
        ],
        "tables": {"chassis": {"K": _("4WD")}, **_GMC_1960_TABLES},
    },
    {
        "id": "gmc-truck-1960-1966-six",
        "label": _("GMC truck inline-six, 1960–66"),
        "make": "GMC",
        "vehicle_class": "truck",
        "years": (1960, 1966),
        "source": "CBE_VIN-Chassis_ID.pdf",
        "example": "I1502PN2611A",
        "fields": [
            {"role": "engine", "width": 1, "label": _("Engine")},
            *_GMC_1960_FIELDS,
        ],
        "tables": {"engine": {"I": _("inline 6-cylinder")}, **_GMC_1960_TABLES},
    },
    {
        "id": "chevrolet-truck-1953-1955",
        "label": _("Chevrolet truck, 1953–55 1st series"),
        "make": "Chevrolet",
        "vehicle_class": "truck",
        "model_from": "series",
        "years": (1953, 1955),
        "source": "CA_VIN-Chassis_ID.pdf",
        "example": "H53S7552",
        "fields": [
            {"role": "series", "width": 1, "label": _("Series")},
            {"role": "year", "width": 2, "label": _("Model year")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 0, "label": _("Production number")},
        ],
        "tables": {
            "series": {"H": _("3100"), "J": _("3600")},
            "year": {"53": 1953, "54": 1954, "55": 1955},
            "plant": _GM_PLANTS_1953,
        },
        "notes": _(
            "Its series code is one letter where the 2nd series uses two, so "
            "the two are different lengths and read as different schemes. A "
            "leading V marks a V8, as it does on the later sheet."
        ),
    },
    {
        "id": "chevrolet-truck-1953-1955-v8",
        "label": _("Chevrolet truck V8, 1953–55 1st series"),
        "make": "Chevrolet",
        "vehicle_class": "truck",
        "model_from": "series",
        "years": (1953, 1955),
        "source": "CA_VIN-Chassis_ID.pdf",
        "example": "VH53S7552",
        "fields": [
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "series", "width": 1, "label": _("Series")},
            {"role": "year", "width": 2, "label": _("Model year")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 0, "label": _("Production number")},
        ],
        "tables": {
            "engine": {"V": _("V8")},
            "series": {"H": _("3100"), "J": _("3600")},
            "year": {"53": 1953, "54": 1954, "55": 1955},
            "plant": _GM_PLANTS_1953,
        },
    },
]


GM_VANS = [
    {
        "id": "chevrolet-gmc-van-1971-1972",
        "label": _("Chevrolet / GMC van, 1971–72"),
        "make": "Chevrolet",
        "years": (1971, 1972),
        "source": "chevy-van-vin.pdf",
        "vehicle_class": "truck",
        "example": "CGS251F100001",
        "fields": [
            {"role": "division", "width": 1, "label": _("Make")},
            {"role": "line", "width": 1, "label": _("Vehicle type")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "series", "width": 1, "label": _("Series")},
            {"role": "body", "width": 1, "label": _("Body type")},
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Sequence number")},
        ],
        "tables": {
            "division": {"C": _("Chevrolet"), "T": _("GMC")},
            "line": {"G": _("Chevy Van / Sport Van / Vandura / Rally Wagon")},
            "engine": {"S": _("6-cylinder"), "E": _("V8")},
            "series": {"1": _("1/2 ton"), "2": _("3/4 ton"), "3": _("1 ton")},
            "body": {
                "5": _("Van and panel"),
                "6": _("Sport Van / Rally Wagon"),
                "7": _("Motor home chassis"),
            },
            "year": {"1": 1971, "2": 1972},
            "plant": {
                "B": _("Baltimore, MD"), "F": _("Flint, MI"),
                "U": _("Lordstown, OH"),
            },
        },
    },
    {
        "id": "chevrolet-gmc-van-1973-1980",
        "label": _("Chevrolet / GMC van, 1973–80"),
        "make": "Chevrolet",
        "years": (1973, 1980),
        "source": "chevy-van-vin.pdf",
        "vehicle_class": "truck",
        "example": "CGL2594100001",
        "fields": [
            {"role": "division", "width": 1, "label": _("Make")},
            {"role": "line", "width": 1, "label": _("Vehicle type")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "series", "width": 1, "label": _("Series")},
            {"role": "body", "width": 1, "label": _("Body type")},
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Sequence number")},
        ],
        "tables": {
            "division": {"C": _("Chevrolet"), "T": _("GMC")},
            "line": {"G": _("Chevy Van / Sport Van / Vandura / Rally Wagon")},
            "engine": _GM_ENGINES_1973,
            "series": {
                "1": _("1/2 ton"), "2": _("3/4 ton"), "3": _("1 ton"),
                "4": _("1/2 ton with heavy-duty suspension"),
            },
            "body": {
                "5": _("Van and panel"),
                "6": _("Sport Van / Rally Wagon"),
                "7": _("Motor home chassis"),
            },
            "year": _GM_YEARS_1973,
            "plant": {
                "F": _("Flint, MI"), "U": _("Lordstown, OH"),
                "4": _("Scarborough, Ontario, Canada"),
            },
        },
        "notes": _(
            "The engine table on this scan broke apart in the scanning and is "
            "taken from the truck sheet, which prints the same codes legibly."
        ),
    },
]


DODGE_VAN = [
    {
        "id": "dodge-van-1971-1980",
        "label": _("Dodge van, 1971–80"),
        "make": "Dodge",
        "years": (1971, 1980),
        "source": "dodge-van-vin.pdf",
        "vehicle_class": "truck",
        "example": "B12AB2U100001",
        "fields": [
            {"role": "line", "width": 1, "label": _("Vehicle type")},
            {"role": "series", "width": 1, "label": _("Series")},
            {"role": "body", "width": 1, "label": _("Body type")},
            {"role": "gvw", "width": 1, "label": _("GVWR")},
            {"role": "engine", "width": 1, "label": _("Engine")},
            {"role": "year", "width": 1, "label": _("Model year")},
            {"role": "plant", "width": 1, "label": _("Assembly plant")},
            {"role": "sequence", "width": 6, "label": _("Sequence number")},
        ],
        "tables": {
            "line": {"B": _("Van")},
            "series": {"1": _("1/2 ton"), "2": _("3/4 ton"), "3": _("1 ton")},
            # Printed as three tables, one per era, and the same digit means
            # different bodies in each.
            "body": {
                "1": [
                    {"text": _("Tradesman Van"), "years": [1972, 1980]},
                ],
                "2": [
                    {"text": _("Tradesman Van"), "years": [1971, 1971]},
                    {"text": _("Sportsman Wagon"), "years": [1972, 1980]},
                ],
                "3": [
                    {"text": _("Low Line Wagon"), "years": [1971, 1971]},
                    {"text": _("Custom Sportsman Wagon"), "years": [1972, 1974]},
                ],
                "4": [
                    {"text": _("High Line Wagon"), "years": [1971, 1971]},
                    {"text": _("Royal Sportsman Wagon"), "years": [1972, 1974]},
                ],
                "5": [
                    {"text": _("Mid Line Wagon"), "years": [1971, 1971]},
                    {"text": _("Tradesman Maxivan"), "years": [1972, 1980]},
                ],
                "6": {"text": _("Sportsman Maxiwagon"), "years": [1972, 1980]},
                "7": {
                    "text": _("Custom Sportsman Maxiwagon"),
                    "years": [1972, 1974],
                },
                "8": {
                    "text": _("Royal Sportsman Maxiwagon"),
                    "years": [1972, 1974],
                },
            },
            "gvw": {"A": _("under 6,000 lb"), "B": _("6,001–10,000 lb")},
            "engine": {
                "A": [
                    {"text": _("198 CID 6-cyl"), "years": [1971, 1971]},
                    {"text": _("440 CID V8, 3-bbl"), "years": [1972, 1977]},
                ],
                "B": _("225 CID 6-cyl, 1-bbl"),
                "C": {"text": _("225 CID 6-cyl, 2-bbl"), "years": [1971, 1978]},
                "D": {"text": _("440 CID V8, 1-bbl"), "years": [1972, 1978]},
                "E": _("318 CID V8, 1-bbl"),
                "F": _("360 CID V8, 1-bbl"),
                "G": {"text": _("318 CID V8, 3-bbl"), "years": [1971, 1978]},
                "J": {"text": _("400 CID V8"), "years": [1972, 1977]},
                "K": {"text": _("360 CID V8, 3-bbl"), "years": [1978, 1980]},
                "P": {"text": _("318 CID V8, 1-bbl"), "years": [1978, 1980]},
                "S": {"text": _("360 CID V8"), "years": [1978, 1980]},
                "T": {"text": _("360 CID V8"), "years": [1974, 1980]},
            },
            # The van sheet's own model-year table did not survive scanning —
            # it comes through as "NODRADBIR@XOP". These codes are the ones
            # `DC_VIN-Chassis_ID.pdf` prints legibly for the Dodge trucks of
            # the same years, which are the same scheme family, extended by
            # `1 = 1971` for the year the vans start and the trucks do not.
            "year": {
                "1": 1971, "2": 1972, "3": 1973, "4": 1974, "5": 1975,
                "6": 1976, "7": 1977, "8": 1978, "9": 1979, "A": 1980,
            },
            "plant": {
                "J": _("Windsor, Ontario, Canada"),
                "K": _("Windsor, Ontario, Canada"),
                "S": _("Warren, MI"), "T": _("Warren, MI"), "V": _("Warren, MI"),
                "U": {"text": _("St. Louis, MO"), "years": [1971, 1972]},
                "X": {"text": _("St. Louis, MO"), "years": [1973, 1980]},
            },
        },
        "notes": _(
            "Read from a scan, and two of its tables did not survive it. The "
            "model years are taken from the Dodge truck sheet of the same "
            "years, which prints them legibly and uses the same codes; the "
            "engine letters were cross-checked against it the same way."
        ),
    },
]


#: Every scheme the matcher knows, in no meaningful order — `decode` ranks.
SCHEMES = [
    *FORD, *FORD_LATER,
    *GM_EARLY, *GM_LAST, *CHEVROLET, *GM_VANS,
    *DODGE, *DODGE_VAN,
]
