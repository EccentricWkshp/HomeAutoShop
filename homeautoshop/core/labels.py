"""
Printable QR labels, and the code that reads one back (SPEC FR-INV-2, C-4).

**One QR format for everything.** A label carries `{BASE_URL}/s/{uuid}/`, and
`/s/` resolves that id against storage locations, vehicles and parts. Primary
keys are UUIDv7 and unique across the whole database, so one route can answer
for any of them — which means one label design, one scanner, and a bin label
and a windshield tag that behave identically.

The spec's `location` entity carries a `qr_code` column. It is not implemented,
deliberately: a second identifier is a second thing to generate, keep unique,
and keep in sync with the row it names, and it would buy nothing. The primary
key is already unique and already permanent. What a separate code *would* buy —
a label that survives the row being deleted and recreated — is not something
anybody wants: that label should stop working.

Labels are rendered as **inline SVG**, not as image requests. A sheet of thirty
bins is then one page load rather than thirty-one, it prints at the printer's
resolution rather than the screen's, and it works with the network unplugged
once the page is open.
"""

from __future__ import annotations

import io
import re

from django.conf import settings
from django.urls import reverse

#: Error-correction level. `M` recovers about 15% of the code, which is the
#: right trade for a label that will get oil on one corner: `L` is too fragile
#: for a garage and `Q` makes the code denser for no gain at this size.
ERROR_CORRECTION = "M"


def scan_url(entity) -> str:
    """The absolute URL a label for this row encodes."""
    return f"{settings.BASE_URL.rstrip('/')}{reverse('scan_target', args=[entity.pk])}"


def qr_svg(data: str, *, size_mm: int = 24) -> str:
    """An inline-safe `<svg>` for `data`, sized in millimeters.

    The XML declaration is stripped: it is legal at the top of a standalone
    file and illegal in the middle of an HTML document, and a browser that
    meets one mid-page renders it as text.
    """
    import qrcode
    import qrcode.image.svg
    from qrcode.constants import ERROR_CORRECT_M

    code = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=2)
    code.add_data(data)
    code.make(fit=True)

    buffer = io.BytesIO()
    code.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(buffer)
    svg = buffer.getvalue().decode("utf-8")

    svg = svg[svg.index("<svg") :]
    # The library sizes the document from the module count, so a 29-module code
    # and a 37-module one print at different sizes. The viewBox is left alone
    # and only the rendered size is fixed, which keeps every label on a sheet
    # identical and scannable at a known distance.
    svg = re.sub(r'\swidth="[^"]*"', f' width="{size_mm}mm"', svg, count=1)
    svg = re.sub(r'\sheight="[^"]*"', f' height="{size_mm}mm"', svg, count=1)
    return svg


def label_for(entity, *, size_mm: int = 24) -> dict:
    """Everything one printed label needs."""
    url = scan_url(entity)
    return {
        "entity": entity,
        "url": url,
        "svg": qr_svg(url, size_mm=size_mm),
        "caption": str(entity),
    }
