# HomeAutoShop — single image, three entrypoints (app, worker, migrate).
FROM python:3.14-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings

# pg_dump, which the backup job shells out to, must be at least the major
# version of the server: it refuses outright to read a newer one. Debian ships
# whatever major it froze on (17 on trixie), so a server upgrade would silently
# break nightly backups while everything else kept working. Take the client
# from PGDG and keep PG_MAJOR in step with docker-compose.yml.
ARG PG_MAJOR=18

# Which OCR language packs to install (FR-DOC-5). Space-separated Tesseract
# codes, which double as Debian package suffixes: `eng fra spa deu`. It is
# passed from docker-compose.yml, which hands the same list to OCR_LANGUAGES
# at run time. One variable for both halves on purpose: a pack installed and
# never asked for is dead weight in the image, and a language asked for but
# never installed is a failure on a background job nobody is watching.
ARG TESSERACT_LANGS="eng"

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client-${PG_MAJOR} libjpeg62-turbo libwebp7 gettext \
        tesseract-ocr \
    && apt-get install -y --no-install-recommends \
        $(for lang in ${TESSERACT_LANGS}; do echo "tesseract-ocr-$lang"; done) \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt gunicorn

COPY . .

# Run unprivileged. The data volume is chowned in compose.
RUN useradd --system --uid 10001 --create-home shop \
    && mkdir -p /data/media /data/backups /app/staticfiles \
    && chown -R shop:shop /data /app/staticfiles
USER shop

# Collected at build time, not on every start. Static files cannot change at
# runtime — they are baked into the image — so doing this per boot only delays
# the first request. STATIC_HASHED must match what compose sets at runtime, or
# the templates would look for hashed names that were never written.
RUN STATIC_HASHED=true python manage.py collectstatic --noinput --clear

# Build-time identity. Declared down here rather than beside the other ARGs on
# purpose: VCS_REF changes with every commit, and an ARG invalidates the build
# cache from the line it appears on downward. At the top of the file it would
# rebuild the apt and pip layers on every push to name a label.
ARG VERSION=0.0.0+unknown
ARG VCS_REF=""

# The commit the image was built from, readable by the application as
# settings.APP_REVISION. The version is deliberately not passed the same way:
# it is in the VERSION file the image already carries, and a number read from
# two places is a number that will eventually disagree with itself.
ENV APP_REVISION=${VCS_REF}

# One instruction per label, which is only a style choice, and readable diffs
# are worth more here than one long continued line nobody re-reads.
#
# `source` and `revision` are the load-bearing pair: they are what makes the
# AGPL's source offer answerable from the image itself, so anyone who pulls
# this can find the exact tree it was built from without being told where to
# look. Everything else is convenience.
LABEL org.opencontainers.image.title="HomeAutoShop"
LABEL org.opencontainers.image.description="Self-hosted, local-first shop management for a home garage."
LABEL org.opencontainers.image.version="${VERSION}"
LABEL org.opencontainers.image.revision="${VCS_REF}"
LABEL org.opencontainers.image.source="https://github.com/EccentricWkshp/HomeAutoShop"
LABEL org.opencontainers.image.url="https://github.com/EccentricWkshp/HomeAutoShop"
LABEL org.opencontainers.image.documentation="https://github.com/EccentricWkshp/HomeAutoShop/blob/main/docs/INSTALL.md"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"

EXPOSE 8080
# `--pid` is not decoration: it is what lets the settings screen reload the web
# tier after a restart-class change (SPEC §17.2). SIGHUP to the master retires
# workers gracefully — in-flight requests finish — so it is a reload, not an
# outage. Without it the pending-restart banner names the command instead of
# offering a button that would quietly do nothing.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8080", "--workers", "3", "--timeout", "120", \
     "--pid", "/tmp/gunicorn.pid", \
     "--access-logfile", "-", "--error-logfile", "-"]
