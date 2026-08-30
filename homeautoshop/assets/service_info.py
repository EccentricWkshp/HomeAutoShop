"""
Deriving one more link from a pinned one (SPEC §8.5, FR-INT-8, FR-INT-10).

§8.5 established that a service-manual URL **cannot be generated** from a VIN:
these libraries index by a catalog string that fuses model, trim, engine and
drivetrain — *"2007 Ford Truck F 150 4WD V8-5.4L VIN V Flex Fuel"* — and nothing
in a VIN decode reconstructs it. So the operator finds the vehicle once and pins
the address.

That pinned address, though, **contains** the catalog string. And LEMON and
CHARM are generated from the same source data, so within one library the section
paths are constant:

    {base}/vehicles/{catalog}/Repair and Diagnosis/A L L  Diagnostic Trouble Codes ( DTC )/index.html
           └── pinned ──────┘ └────────────── the same for every vehicle ──────────────┘

So the DTC index is reachable: keep everything up to the catalog segment, append
the known tail. That is a derivation from something the operator vouched for,
not a guess at a URL from scratch.

**What this does not promise.** Nothing is fetched to check it — FR-INT-10
forbids crawling a provider, and that rule is not bent for a convenience. A
library that has no entry for a section returns a 404, and the UI says the link
may not exist rather than implying it was verified.

One quirk worth writing down: the section segments are **double-encoded**.
`Repair and Diagnosis` appears as `Repair%2520and%2520Diagnosis` — a literal
`%20` that has itself been percent-encoded. Encoding it once produces a URL that
404s, so the stored value is kept verbatim and never re-encoded.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

#: The segment that precedes the catalog string in both libraries.
CATALOG_ROOT = "vehicles"


def vehicle_root(url: str) -> str:
    """The `/vehicles/{catalog}/` prefix of a pinned URL, or empty.

    Empty rather than a best effort: a pinned address that is not shaped like
    one of these libraries — ALLDATA, or somebody's own file server — has no
    derivable sections, and offering a link built from a shape we did not
    recognize would send the operator somewhere arbitrary.
    """
    if not url:
        return ""
    parts = urlsplit(url)
    segments = parts.path.split("/")
    try:
        index = segments.index(CATALOG_ROOT)
    except ValueError:
        return ""
    if index + 1 >= len(segments) or not segments[index + 1]:
        return ""
    kept = "/".join(segments[: index + 2])
    return urlunsplit((parts.scheme, parts.netloc, kept + "/", "", ""))


def dtc_url(link) -> str:
    """The DTC index for the vehicle behind a pinned link, or empty."""
    provider = link.provider
    if not getattr(provider, "dtc_path", ""):
        return ""
    root = vehicle_root(link.url)
    if not root:
        return ""
    # `lstrip` on the stored path only — the path is already encoded exactly as
    # the library wants it, and running it through `quote` would break it.
    return root + provider.dtc_path.lstrip("/")


def dtc_links(asset) -> list[dict]:
    """Every pinned provider that can reach a DTC index for this vehicle."""
    found = []
    for link in asset.service_info_links.select_related("provider"):
        if link.is_hidden or not link.url:
            continue
        target = dtc_url(link)
        if target:
            found.append({"provider": link.provider, "url": target})
    return found
