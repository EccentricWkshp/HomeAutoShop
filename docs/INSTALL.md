# Installing HomeAutoShop

From nothing to a working instance you can sign in to. About fifteen minutes,
most of it waiting for images to pull.

The one thing worth knowing up front: **a new instance has no accounts.** That
is deliberate — there is no default login to forget to change — but it means
`docker compose up` alone leaves you at a sign-in page with nothing to sign in
with. Step 4 is where you fix that, and you will do it again any time you start
from an empty database volume.

## 1. Prerequisites

Docker with the **Compose v2 plugin**. On Debian and Ubuntu the distribution's
`docker.io` package does not include it, and the symptom is unhelpful:

```
$ docker compose version
docker: unknown command: docker compose
```

Install `docker-compose-v2` (or Docker's own `docker-compose-plugin`). Verify
before going further:

```bash
docker compose version
```

## 2. Configure

```bash
cp .env.example .env
```

Four values matter before first boot. The rest have working defaults.

| | |
| --- | --- |
| `SECRET_KEY` | Long and random. Sessions and signed values depend on it, so changing it later logs everyone out. |
| `POSTGRES_PASSWORD` | Anything, but not the shipped default. |
| `STORAGE_SECRET_KEY` | Same. |
| `SITE_ADDRESS` | The hostname you will type in a browser — `shop.home.arpa` by default. Caddy issues a certificate for exactly this name, so a mismatch here is the most common reason the site will not load. |

`ALLOWED_HOSTS` must contain whatever you set `SITE_ADDRESS` to.

Any secret can be supplied out of a file instead by pointing `<NAME>_FILE` at a path.

**Almost everything else in this file is edited in the application instead.**
The shop name, units and currency, Offline Mode, the integration addresses and
their keys, the reminder settings and the backup schedule all live under
*Settings* in the account menu, and a value set there wins over the one in
`.env`. Set the four above, start it, and change the rest from a browser.

Two things stay here because they cannot live in the database:

| | |
| --- | --- |
| `CREDENTIAL_KEY` | Encrypts the integration keys you enter in the UI. A key kept in the database it protects is not a key. Leave it blank and it is derived from `SECRET_KEY` — that works, and it is weaker, because then one secret protects two things. Changing it invalidates every stored credential at once, which is the intended emergency behaviour. |
| `TESSERACT_LANGS` | Which OCR language packs go **into the image**, so it is a build-time choice: `eng` by default, and `TESSERACT_LANGS="eng fra spa" docker compose build` to add more. It also sets what the application asks for, so the two halves cannot drift. |

## 3. Start it

```bash
docker compose up -d
```

Five containers. The `app` container runs migrations, collects static files and
seeds the built-in templates before gunicorn starts, so the first boot takes
longer than later ones. Watch it settle:

```bash
docker compose ps          # all should reach healthy
docker compose logs -f app
```

## 4. Create the first account

```bash
docker compose exec app python manage.py createsuperuser
```

It prompts for a username, an email and a password. This is the only account
that exists, so keep the password somewhere real.

One wrinkle worth knowing: `createsuperuser` is Django's, and knows nothing
about this application's roles, so it leaves `role = member` while setting the
superuser flag. Everything works — authorization short-circuits on the
superuser flag — but the two disagree, and clearing the flag later would
quietly demote the account. To make them agree:

```bash
docker compose exec app python manage.py shell -c \
  "from homeautoshop.accounts.models import User; User.objects.filter(username='YOU').update(role='admin')"
```

Everyone else you add from **Administration → Users** in the UI, where the role
field is the one that counts.

## 5. Reach it from a browser

The stack listens on ports **80 and 443** — not 8000, which is the development
server's port and nothing to do with Compose. Requests arrive at Caddy, which
terminates TLS and forwards to the app.

TLS is not decoration here: service workers, the camera used for VIN barcode
scanning, and Web Serial all refuse to run outside a secure context, so there
is no plain-HTTP mode to fall back to.

**Make the name resolve — to the host's LAN address.** Caddy answers for
`SITE_ADDRESS` and nothing else, so `https://localhost` fails even though the
port is open.

Point the name at the Docker host's real address (`192.168.1.58`, say) in
whatever serves DNS on your network: a router's local records, a Pi-hole, or
your own resolver. One entry then works from every device, which is the point —
this application expects to be opened on a phone in the garage.

A hosts file entry pointing at `127.0.0.1` also "works", on exactly one
machine, and is worth avoiding **as a substitute for DNS**. It overrides correct
resolution, so it keeps working after the real setup breaks and hides the
failure that matters: an instance nothing else on the network can reach. Set up
DNS first and confirm a second device can reach the instance; then a loopback
entry is a local convenience rather than a mask.

There is one case where it is the answer rather than a workaround, and it is
common enough to name: see *Running Docker inside WSL* below.

If DNS is not yours to configure, a hosts entry per device is the fallback —
`/etc/hosts` on Linux and macOS, `C:\Windows\System32\drivers\etc\hosts` on
Windows as administrator — pointing at the **host's LAN address**, not
loopback.

**Get a certificate.** `TLS_MODE` picks how, and the choice is worth two
minutes because one option costs you nothing afterwards and the other costs you
something on every device, forever.

| `TLS_MODE` | What it costs | What it needs |
| --- | --- | --- |
| `acme-dns` | nothing — every device already trusts Let's Encrypt | a public domain name whose DNS has an API |
| `internal` | install a root certificate on every phone, tablet and laptop | nothing |
| `custom` | you renew it yourself | a certificate you already have |

#### acme-dns — recommended

A real Let's Encrypt certificate, proved by writing a DNS record rather than by
answering on port 80. **The instance is never reachable from the internet**, and
the hostname does not even need a public address record — keep the `A` record in
your local DNS and the machine's address stays private.

The prerequisite is a public domain name. If you do not own one, a free
dynamic-DNS name works exactly as well: DuckDNS and deSEC both hand out a
subdomain with an API, and both are compiled in.

```ini
SITE_ADDRESS=shop.example.com          # a real domain, not .home.arpa
BASE_URL=https://shop.example.com
ALLOWED_HOSTS=shop.example.com
TLS_MODE=acme-dns
ACME_EMAIL=you@example.com
ACME_DNS_PROVIDER=cloudflare           # or duckdns, desec, acmedns
ACME_DNS_TOKEN=...                     # scoped to this zone only
```

Point staging at it first — the production endpoint allows five identical
certificates a week, and a wrong token spends them quickly:

```ini
ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory
```

A staging certificate is untrusted on purpose. Your browser complaining about
an unknown issuer *is* the success condition: it means the DNS challenge passed
and issuance worked. Comment the line out and restart for the real one.

```bash
docker compose up -d proxy
docker compose logs -f proxy      # "certificate obtained successfully"
```

**Four providers are compiled in** — `cloudflare`, `duckdns`, `desec`, and
`acmedns`. For any of the ~100 others at <https://github.com/caddy-dns>:

```bash
docker compose build --build-arg CADDY_DNS_MODULES=github.com/caddy-dns/hetzner proxy
```

A provider that is not compiled in fails immediately at startup with
`module not registered`, not at the first renewal.

`acmedns` is worth a look if handing a home server an API token for your whole
domain makes you uneasy. It delegates one `_acme-challenge` record by CNAME, so
the credential controls that record and nothing else. It is configured by file
rather than token: put the JSON in `config/caddy/dns` and set
`ACME_DNS_TOKEN=/etc/caddy/dns/acmedns.json`.

Two things to know before choosing it. Renewal needs outbound access to the CA
and to your DNS provider every 60 days or so, so a fully air-gapped instance
cannot use it. And every issued name is published to Certificate Transparency
logs — `shop.example.com` becomes publicly known, though nothing about the
machine behind it does.

#### internal — no prerequisites, but a cost per device

Caddy signs the certificate itself. Nothing trusts that CA until you install
its root on each device, which is the reason it is not the recommendation: it
is one install per phone, tablet and laptop, repeated whenever any of them is
replaced, and on iOS it is buried behind a second *trust* toggle in Settings
that almost nobody finds unaided.

```bash
docker run --rm -v homeautoshop_caddy-data:/d alpine \
  cat /d/caddy/pki/authorities/local/root.crt > caddy-root.crt
```

Keychain Access on macOS (set to *Always Trust*);
`/usr/local/share/ca-certificates/` plus `update-ca-certificates` on Debian and
Ubuntu; and on Windows, in an administrator PowerShell:

```powershell
Import-Certificate -FilePath caddy-root.crt -CertStoreLocation Cert:\LocalMachine\Root
```

Firefox keeps its own trust store and needs the certificate added separately,
under Settings → Privacy & Security → Certificates.

#### custom

Put `fullchain.pem` and `privkey.pem` in `config/caddy/certs` and set
`TLS_MODE=custom`. Renewal is yours to arrange.

Now open your instance and sign in.

### Running Docker inside WSL

**Check the networking mode first.** It decides whether anything but this one
machine can reach the instance, and the default answer is no.

WSL2 defaults to **NAT**: the VM sits behind a private address, and Windows
forwards published ports to `127.0.0.1` only. The instance then works in a
browser on the Docker host and is invisible to every other device — including
the phone this application is largely built for. Nothing reports an error,
because from the host's point of view everything is fine.

That failure is easy to misread, because a hosts file entry pointing at
`127.0.0.1` makes it go away locally while leaving the LAN just as unreachable.
Test the address a phone would actually use, not loopback:

```powershell
Test-NetConnection -ComputerName 192.168.1.58 -Port 443   # the host's LAN IP
```

Switch to **mirrored** networking, where WSL shares the Windows network stack
and published ports land on the host's real interfaces. In
`%USERPROFILE%\.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
```

**The file is only read when the WSL VM boots**, so this changes nothing until:

```powershell
wsl --shutdown
```

and you open a WSL shell again. Docker restarts with the distribution if its
service is enabled (`systemctl is-enabled docker`), and containers marked
`restart: unless-stopped` come back on their own. Confirm it took effect — in
mirrored mode WSL sees the host's addresses rather than a `172.x` private one:

```bash
ip -4 addr show | grep inet     # expect the LAN IP, e.g. 192.168.1.58
```

Mirrored mode adds a Hyper-V firewall in front of the VM, and it blocks inbound
by default. Two targeted rules are better than opening the VM entirely:

```powershell
$vm = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'
New-NetFirewallHyperVRule -Name Shop-HTTPS -DisplayName 'Shop (WSL) HTTPS' `
  -Direction Inbound -VMCreatorId $vm -Protocol TCP -LocalPorts 443 -Action Allow
New-NetFirewallHyperVRule -Name Shop-HTTP -DisplayName 'Shop (WSL) HTTP' `
  -Direction Inbound -VMCreatorId $vm -Protocol TCP -LocalPorts 80 -Action Allow
```

Traffic that is *dropped* rather than refused shows up in a browser as "taking
too long to respond", which is easily mistaken for a server problem. It is
worth testing a port before assuming the application is at fault.

#### The Docker host cannot reach the instance, but everything else can

A specific and confusing symptom of mirrored networking. Other devices load the
site; the machine running Docker times out on the very same URL.

This is not a firewall, a certificate or a DNS fault. In mirrored mode the LAN
address exists on **both** stacks — Windows and the VM. When Windows connects to
it, its own stack handles the connection locally, finds nothing listening
(the listener is inside the VM) and drops the packet. `hostAddressLoopback=true`
in `.wslconfig` is documented for this and is worth trying first, but it does
not resolve it on every build.

Confirm the diagnosis before treating it, because the distinction matters:

```powershell
Test-NetConnection 127.0.0.1 -Port 443        # succeeds
Test-NetConnection <the host's LAN IP> -Port 443   # times out
```

If loopback works and the LAN address does not — while another device loads the
site — the fix is a hosts entry **on the Docker host alone**:

```
127.0.0.1   shop.example.com
```

This is the case where a loopback entry is correct rather than a mask. With
`TLS_MODE=acme-dns` the certificate is a real one issued for that name, so
loopback serves it with full chain validation and no browser warning; nothing
is installed, and no other device is affected. DNS stays authoritative
everywhere else, and you have already proved it works by loading the site on
something other than the Docker host.

If restarting WSL is not convenient, forward the ports instead — same effect,
no restart, and it must be repeated for each port (admin PowerShell):

```powershell
netsh interface portproxy add v4tov4 listenaddress=192.168.1.58 listenport=443 connectaddress=127.0.0.1 connectport=443
netsh interface portproxy add v4tov4 listenaddress=192.168.1.58 listenport=80  connectaddress=127.0.0.1 connectport=80
```

With either in place, point DNS — a router, or a Pi-hole local record — at the
host's LAN address, and install the certificate on **Windows** and on every
other device that will use the instance. A hosts file entry is then a debugging
tool, not part of the setup. Leave a `127.0.0.1` one out: it silently overrides
correct DNS and hides exactly this problem.

Finally, if a connection is refused from the host itself, check the port too:
`8000` is the development server, `443` is the stack.

## 6. Confirm it is actually working

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' https://shop.home.arpa/healthz   # 200
docker compose exec app python manage.py send_reminders --dry-run          # prints, sends nothing
```

Take a backup before you rely on it, so you find out now rather than later
whether the destination is writable:

```bash
docker compose exec app python manage.py backup
```

## 7. Worth doing next

- **Add your vehicles**, then run the VIN lookup on each to fill in the details.
- **Set a schedule** per vehicle from a built-in template, so *Due* has
  something to tell you.
- **Bring the spreadsheet along.** Account menu → *Import a spreadsheet* takes
  vehicles, parts or service history as they are and asks you which column is
  which. It rehearses before it writes, and re-running the same file writes
  nothing twice.
- **Turn on reminders** — off by default, and a channel has to exist and be
  enabled before anything is delivered.
- **Take a backup and look at it.** Account menu → *Backup* → **Back up now**.
  The same screen lists what is held with sizes, lets you download one, and
  prints the restore command with this instance's real paths already in it —
  which is worth reading once now rather than for the first time during an
  actual restore.
- **Check the backup destination** has room, and that something is copying it
  off this machine. A backup on the same disk as the database is not a backup.
  Note what a backup deliberately does *not* contain: the integration keys you
  entered. A restored instance says which ones need typing in again.

## 8. Installing it on the phone

Open the site in Chrome or Safari on the phone and use *Add to home screen*. It
then runs without browser chrome, remembers pages you have visited, and keeps
anything you capture while out of signal until it can send it — the counter in
the header says how many are waiting, and tapping it shows exactly what they
are.

Two things this unlocks that only work over HTTPS, which is why TLS is not
optional here:

- **The camera**, one tap from any work order.
- **Web Serial**, for reading trouble codes off an ELM327 adapter — Chrome or
  Edge only; Safari and Firefox have not implemented it.

**Notifications are separate and off.** Account menu → *Reminders* → *Notify
this device*. Worth knowing before you turn it on: web push does not reach your
browser directly, it goes through Google, Mozilla or Apple depending on which
browser this is. That is the one place this instance has to talk to a large
cloud service, so it is opt-in per device, disabled entirely by Offline Mode,
and the message it sends says only that something is due — never which vehicle
or what. That detail stays behind the tap.

## 9. Reading a scan tool

Vehicle → *Diagnostics* takes a PDF report from a scan tool, or a CSV or text
export if yours makes one. Prefer the export where you have it: it is cheaper
to read and far less likely to be misread.

What happens next is deliberately two steps. The report is read, and then it
sits in a queue as a **draft** until you have looked at it. Nothing reaches the
vehicle's history until you confirm — a misread VIN or odometer would poison
the record and every cost-per-mile figure that comes from it, and nothing
afterwards would show you it had happened.

If no profile recognizes the file, that is not a failure: you are offered a
mapping screen, and saving the mapping means the next report from that tool
reads itself.

The XTOOL D8 is supported out of the box. Adding another tool is usually a
YAML profile rather than a code change — Account menu → *Integrations* →
*Parser profiles* imports and exports them.

## Troubleshooting

### `dependency failed to start: container homeautoshop-db-1 is unhealthy`

Almost always a Postgres major version change against an existing volume. The
logs say so plainly:

```bash
docker compose logs db | head -20
```

Postgres will not start on a data directory written by a different major
version, and from 18 onward the official image also expects the volume mounted
at `/var/lib/postgresql` rather than `/var/lib/postgresql/data`. For a test
instance with nothing to keep, start over:

```bash
docker compose down
docker volume rm homeautoshop_db-data
docker compose up -d
```

**That destroys the database.** To keep the data, take a portable export first
(`manage.py export_data`) on the old version, or perform a proper `pg_upgrade`.

### The browser says connection refused

Three causes, cheapest first.

**Wrong port.** `8000` is `runserver`; the Compose stack is on `443`.

**The proxy is not running.** `docker compose ps` shows it.

**It works on the Docker host but nowhere else, or only via a hosts entry.**
That is not a DNS fault, even when DNS is plainly answering. Confirm what
resolution returns and then test that address directly:

```powershell
nslookup shop.home.arpa                 # bypasses the hosts file
Test-NetConnection -ComputerName <the address it returned> -Port 443
```

If the name resolves correctly but the address does not answer, nothing is
listening on the interface DNS is pointing at. Under WSL that means NAT
networking — see *Running Docker inside WSL* above. `Get-NetTCPConnection
-State Listen -LocalPort 443` showing only `127.0.0.1` and `::1` is the
signature.

### The browser says the certificate is not trusted

On `TLS_MODE=internal`, that is step 5: the root was not installed, or went
into the current user's store rather than the machine's, or Firefox is being
used and keeps its own.

On `acme-dns`, check whether `ACME_CA` still points at staging — staging
certificates are untrusted by design. `docker compose logs proxy` names the
issuer it used.

### The certificate never arrives on acme-dns

`docker compose logs proxy` says which step failed.

**`module not registered: dns.providers.x`** — that provider is not in the
image. Rebuild with it (see step 5) or pick one that is.

**The challenge times out waiting for propagation** — the usual cause is a
local resolver serving a split-horizon view of the zone, which cannot see the
TXT record that was just written. `config/caddy/tls-acme-dns.conf` already asks
public resolvers for this reason; if you changed that line, change it back.

**The provider rejects the credential** — the token is wrong, or scoped to a
zone other than the one being issued for.

**`SITE_ADDRESS` is a special-use name** — `.home.arpa`, `.local`, `.internal`
and friends cannot be issued by any public CA. It has to be a real domain.

### The page loads but is unstyled

Static files are being served, but the browser is refusing them. Check the
console: a MIME type complaint means a request for `/static/…` returned an HTML
404 page. Confirm `collectstatic` ran at boot in `docker compose logs app`.

### Backups fail with a version mismatch

```
pg_dump: error: aborting because of server version mismatch
```

`pg_dump` refuses to read a server newer than itself. The image installs a
client matching `PG_MAJOR` in the Dockerfile — if you raise the `db` image in
`docker-compose.yml`, raise `PG_MAJOR` with it and rebuild. Nothing else breaks
when these drift, which is what makes it worth checking deliberately.

### You have no account

You have not run step 4, or the database volume was recreated since you did.
Run `createsuperuser` again.
