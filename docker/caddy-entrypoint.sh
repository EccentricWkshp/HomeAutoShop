#!/bin/sh
# Select the TLS strategy before Caddy starts, and refuse an unknown one.
#
# This used to be a bind mount whose source path contained ${TLS_MODE}, on the
# theory that a wrong value would fail because the file did not exist. It does
# not: the Docker engine creates a missing bind source as a *directory*, so a
# typo left a bogus tls-typo.conf/ folder in the repository and a proxy
# crash-looping on "is a directory" — a real failure, with a message pointing
# nowhere near the cause.
#
# Checking it here costs four lines and says what is wrong.
set -eu

MODES_DIR=/etc/caddy/modes
TLS_MODE="${TLS_MODE:-internal}"
SOURCE="${MODES_DIR}/tls-${TLS_MODE}.conf"

if [ ! -f "$SOURCE" ]; then
    echo "TLS_MODE=${TLS_MODE} is not a supported strategy." >&2
    echo "Choose one of:" >&2
    for candidate in "${MODES_DIR}"/tls-*.conf; do
        [ -f "$candidate" ] || continue
        name="${candidate##*/tls-}"
        echo "  ${name%.conf}" >&2
    done
    echo "Set TLS_MODE in .env and start again." >&2
    exit 1
fi

# Copied rather than symlinked so Caddy reads it even though the source is a
# read-only mount, and so the running config cannot change under a reload.
cp "$SOURCE" /etc/caddy/tls.conf
echo "TLS strategy: ${TLS_MODE}"

if [ "$TLS_MODE" = "acme-dns" ]; then
    # Empty values here produce a certificate error minutes later, from inside
    # ACME, that reads like a provider problem. Name them now instead.
    for required in ACME_EMAIL ACME_DNS_PROVIDER ACME_DNS_TOKEN; do
        eval "value=\${$required:-}"
        if [ -z "$value" ]; then
            echo "TLS_MODE=acme-dns needs ${required} to be set." >&2
            exit 1
        fi
    done
    case "${SITE_ADDRESS:-}" in
        *.home.arpa|*.local|*.internal|*.lan|localhost)
            echo "SITE_ADDRESS=${SITE_ADDRESS} is a special-use name." >&2
            echo "No public CA can issue for it. Use a real domain, or TLS_MODE=internal." >&2
            exit 1
            ;;
    esac
fi

exec caddy "$@"
