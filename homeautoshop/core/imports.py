"""Reading a pasted or uploaded template (SPEC §8.1b, FR-INT-7, FR-DVI-13).

Parser profiles established the shape — choose a file *or* paste the text —
and schedules and checklists copied it, which made three near-identical
readers. Three is where that stops being a coincidence: a fourth kind would
have been a fourth copy, and each was free to drift in what it accepted or
how it failed.

Small on purpose. What each import *does* with the text differs completely
and belongs in its own validator; what they share is only how the text gets
off the form, which is exactly this.
"""

from __future__ import annotations

from django.utils.translation import gettext as _

#: An upload larger than this is not a template somebody wrote. The validators
#: each impose their own cap on the parsed text; this one stops a very large
#: file being read into memory to find that out.
MAX_UPLOAD_BYTES = 1024 * 1024


class NothingToImport(ValueError):
    """No file was chosen and nothing was pasted."""


def text_from(request, field: str) -> str:
    """The template text this form is offering, from either control.

    The file wins when both are given: somebody who picked a file and left a
    half-typed paste behind meant the file, and silently importing the paste
    would be the least expected of the two outcomes.
    """
    upload = request.FILES.get(field)
    if upload is not None:
        raw = upload.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise NothingToImport(_("That file is too large to be a template."))
        text = raw.decode("utf-8", errors="replace")
    else:
        text = request.POST.get("yaml", "")

    if not text.strip():
        raise NothingToImport(_("Choose a file or paste one in."))
    return text
