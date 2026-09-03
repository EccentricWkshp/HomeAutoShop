# HomeAutoShop Caddy

The reverse-proxy half of
[HomeAutoShop](https://github.com/EccentricWkshp/HomeAutoShop): Caddy with
DNS-01 provider modules compiled in.

**This image is not useful on its own.** It exists because the stock `caddy`
image cannot answer a DNS-01 challenge — every provider is a separate Go module
and Caddy has no runtime plugin loading — and DNS-01 is the *only* challenge a
LAN instance can pass. HTTP-01 and TLS-ALPN-01 both require the certificate
authority to reach port 80 or 443 from the internet, which is precisely what a
local-first instance must not offer.

## What is compiled in

| Module | Why it is here |
| --- | --- |
| [`acmedns`](https://github.com/caddy-dns/acmedns) | Works with any provider at all. It uses CNAME delegation, so the credential the instance holds controls exactly one TXT record and cannot touch the rest of the zone — which is the part of DNS-01 worth being uncomfortable about. |
| [`cloudflare`](https://github.com/caddy-dns/cloudflare) | Common enough that most people will not have to rebuild. |
| [`desec`](https://github.com/caddy-dns/desec) | As above. |
| [`duckdns`](https://github.com/caddy-dns/duckdns) | A free dynamic-DNS name does as well here as a domain you own. |

The set is a starting point, not an endorsement. Anyone else supplies their own
from [caddy-dns](https://github.com/caddy-dns) — a list of about a hundred —
with a single build argument:

```bash
docker compose build --build-arg CADDY_DNS_MODULES=github.com/caddy-dns/hetzner proxy
```

The build fails rather than succeeding empty if nothing compiled in, so a
mistake here surfaces at build time instead of at the first renewal.

## Tags

`0.7.0` and the like are exact releases, `0.7` follows a series, and `latest`
is the newest release. Each is a single manifest covering **linux/amd64** and
**linux/arm64**, built natively on both.

## Use

It is configured by the `proxy` service in HomeAutoShop's `docker-compose.yml`,
which mounts the `Caddyfile` and the TLS mode files and passes `SITE_ADDRESS`,
`TLS_MODE` and the ACME settings. See
[docs/INSTALL.md](https://github.com/EccentricWkshp/HomeAutoShop/blob/main/docs/INSTALL.md).

## Licence

AGPL-3.0-or-later. Source, and every issue worth opening:
<https://github.com/EccentricWkshp/HomeAutoShop>
