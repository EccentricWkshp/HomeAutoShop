"""
Serving an uploaded file back to a browser (SPEC §5.1, FR-DOC-8, §12.3).

This module exists because the obvious approach did not work and could not be
made to work without asking the operator to build something.

Object storage hands out **presigned URLs**, and a presigned URL is signed
against the endpoint the application talks to — inside Compose that is
`http://storage:9000`, a hostname that exists only on the container network. So
every photo on every page was a link to a host the browser cannot resolve. The
image did not render and the link went nowhere.

The three ways out, and why this one:

* **Publish MinIO's port and sign against a public address.** Works, and costs
  the operator a second hostname, a second certificate, and an object store
  exposed to the network. Too much to ask of a home shop, and it is not the
  default in `docker-compose.yml` — MinIO deliberately publishes nothing.
* **Reverse-proxy the object store under the site's own name.** Also works, and
  depends on the operator's Caddyfile being exactly right, including passing the
  signed `Host` upstream. A silent breakage waiting for the first person who
  edits it.
* **Serve the bytes through the application.** No configuration at all, correct
  on every deployment including one running without Compose, and — the part
  that turns out to matter more — it is the only one of the three where reading
  a photo requires **being signed in**. A presigned URL is a bearer token in a
  querystring: copied out of a browser's address bar, it works for anyone who
  has it until it expires.

The cost is real and is accepted: photos pass through gunicorn rather than
straight from the store. At ten users and a garage's worth of vehicles
(NFR-P-*) that is not the bottleneck, and an operator who has done the work to
expose their object store can set `STORAGE_PUBLIC_ENDPOINT` and get the
redirect instead.
"""

from __future__ import annotations

import logging
import mimetypes

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.views.decorators.http import require_GET

from .models import Media

log = logging.getLogger(__name__)

#: Which derivative to hand back. The original is the default because a link
#: with no variant is what somebody clicked to see the full-size picture.
VARIANTS = ("original", "thumb", "preview")


def _public_endpoint() -> str:
    """The address a browser can reach the object store on, if there is one."""
    options = settings.STORAGES.get("default", {}).get("OPTIONS", {})
    public = options.get("public_endpoint", "")
    return public if public and public != options.get("endpoint_url", "") else ""


@require_GET
@login_required
def media_file(request, pk, variant: str = "original"):
    """Hand back one uploaded file, to somebody who is signed in."""
    if variant not in VARIANTS:
        raise Http404

    media = Media.objects.filter(pk=pk).first()
    if media is None:
        raise Http404

    handle = {
        "original": media.file,
        "thumb": media.thumb or media.file,
        "preview": media.preview or media.file,
    }[variant]
    if not handle:
        raise Http404

    # An operator who has published their object store gets the direct route,
    # which is what MinIO is for. Everyone else gets the bytes from here.
    if _public_endpoint():
        return HttpResponseRedirect(handle.url)

    try:
        stream = handle.open("rb")
    except (FileNotFoundError, OSError) as exc:
        # The row outlives the object if a bucket is emptied by hand. A 404 is
        # the honest answer; a 500 would read as the application being broken.
        log.warning("media %s (%s) is not in storage: %s", media.pk, variant, exc)
        raise Http404 from exc

    content_type = media.mime or mimetypes.guess_type(handle.name)[0] or "application/octet-stream"
    response = FileResponse(stream, content_type=content_type)
    # Shown inline — this is a photo on a page, not a download. The original
    # filename is offered for anyone who does save it.
    response["Content-Disposition"] = f'inline; filename="{media.original_filename or handle.name}"'
    # Private, because the response is only correct for the person who asked
    # for it: a shared cache holding it would serve it to somebody signed out.
    response["Cache-Control"] = "private, max-age=3600"
    return response
