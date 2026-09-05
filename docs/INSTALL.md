# Installing HomeAutoShop

From nothing to a working instance you can sign in to. Under ten minutes, most
of it waiting for three images to download. Nothing is compiled: the
application and its proxy are published for amd64 and arm64, so a Raspberry Pi
pulls the same release a desktop does.

The one thing worth knowing up front: **a new instance has no accounts.** That
is deliberate — there is no default login to forget to change — but it means
`docker compose up` alone leaves you at a sign-in page with nothing to sign in
with. Step 4 is where you fix that, and you will do it again any time you start
from an empty database volume.

## 1. Prerequisites

Two things: **Git**, and **Docker with the Compose v2 plugin**. Nothing else —
no Python, no Node, no database server. Everything the application needs is
inside the images.

**Follow only the one section below that matches your machine.** Each is
complete on its own and each ends with the same check. Skip the other two.

### Windows

Docker Desktop is free for personal use and for companies under 250 employees
and $10M revenue, which covers a home garage comfortably.

**1. Enable WSL 2.** Docker Desktop runs Linux containers inside it. Open
PowerShell **as Administrator** and run:

```powershell
wsl --install
```

Reboot when it asks. If it reports that WSL is already installed, run
`wsl --update` instead and carry on. This needs Windows 11, or Windows 10
version 2004 or later.

**2. Install Git.** Download [Git for Windows](https://git-scm.com/download/win)
and accept every default.

**3. Install Docker Desktop.** Download
[Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/)
and run the installer, leaving **Use WSL 2 instead of Hyper-V** ticked.

**4. Start it and wait.** Launch Docker Desktop from the Start menu, accept the
service agreement, and wait until the whale icon in the system tray stops
animating and the dashboard says **Engine running**. The first start takes a
minute or two. Commands run before this fail with `error during connect`.

**5. Two settings that matter.** If Docker Desktop offers **Switch to Linux
containers**, select it — these are Linux images and will not run in
Windows-container mode. If Windows Firewall asks whether to allow Docker
Desktop, allow it on **Private networks** so phones and tablets on the LAN can
reach ports 80 and 443. Public-network access is not needed and should stay
unticked.

You do **not** need to install Docker Engine inside a separate WSL
distribution. Docker Desktop makes `docker` available directly in PowerShell.

**6. Check it worked.** In a normal (non-admin) PowerShell:

```powershell
git --version
docker version
docker compose version
```

All three must print a version. Then go to step 2.

### macOS

Docker Desktop is free for personal use and for companies under 250 employees
and $10M revenue.

**1. Install Git.** macOS ships Apple's Command Line Tools, which include Git,
but not until you ask for them:

```bash
xcode-select --install
```

A dialog appears; accept it and wait. If Git is already present the command
says so, which is also fine.

**2. Find out which Mac you have.** The two Docker Desktop downloads are not
interchangeable:

```bash
uname -m
```

`arm64` means Apple silicon (M1 and later). `x86_64` means Intel.

**3. Install Docker Desktop.** Download the matching build from
[Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/),
open the `.dmg`, and drag **Docker** into **Applications**.

**4. Start it and wait.** Open Docker from Applications. It asks for your
password once to install its privileged helper, then shows the service
agreement. Wait until the whale icon in the menu bar stops animating and the
dashboard says **Engine running**.

If macOS asks whether to allow incoming network connections, allow it — that is
what lets other devices on the LAN reach ports 80 and 443.

**5. Check it worked.**

```bash
git --version
docker version
docker compose version
```

All three must print a version. Then go to step 2.

> Apple silicon needs no special handling: the images are published for arm64
> as well as amd64, so nothing runs under emulation.

### Linux

**Do not install the distribution's Docker packages.** Debian's and Ubuntu's
`docker.io` is usually old and ships no Compose v2 at all, and the symptom is
unhelpful — the command simply does not exist:

```text
$ docker compose version
docker: unknown command: docker compose
```

Install Docker's own packages instead, which include Engine, Compose v2 and
Buildx together.

**1. Remove anything the distribution installed**, if this machine has had
Docker on it before. On a genuinely fresh machine this does nothing and is
still safe to run:

```bash
for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
  sudo apt-get remove -y $pkg
done
```

**2. Add Docker's repository and install.** These are the Debian and Ubuntu
commands. **On Debian, change both occurrences of `ubuntu` to `debian`:**

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
```

> **If `apt-get update` reports 404 against `download.docker.com`**, Docker has
> not published packages for your release yet. This is normal on Ubuntu's
> interim releases for a while after launch, and the codename in the line above
> is read from your own system, so it asks for a directory that does not exist.
> Edit `/etc/apt/sources.list.d/docker.list` and replace the codename with the
> LTS yours is based on — `noble` for any 24.x, `jammy` for 22.x — then
> `sudo apt-get update` again.

On Fedora, RHEL and their relatives:

```bash
sudo dnf -y install git dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
sudo dnf -y install docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
```

For anything else, [Docker's install guide](https://docs.docker.com/engine/install/)
has a page per distribution. Arch's own `docker` and `docker-compose` packages
are current and fine.

**3. Start the daemon and make it start at boot.** The packages do not do this
for you on every distribution, and a machine that forgets Docker after a power
cut is a shop that is down until somebody notices:

```bash
sudo systemctl enable --now docker
```

**4. Let your user run Docker without `sudo`.** This is the step most guides
bury on a separate page, and skipping it means `permission denied while trying
to connect to the Docker daemon socket` on the very next command:

```bash
sudo usermod -aG docker $USER
newgrp docker
```

`newgrp` fixes the current shell. **Log out and back in** so every future shell
gets it too.

> Adding a user to the `docker` group grants root-equivalent access to the
> machine, because that user can start a container that mounts the host's
> filesystem. On a single-admin home server that is the normal trade. Prefer
> typing `sudo` in front of every `docker` command if it is not.

**5. Make sure ports 80 and 443 are free.** A distribution that came with
Apache or nginx is already holding them, and Compose will fail later with
`address already in use`:

```bash
sudo ss -lntp '( sport = :80 or sport = :443 )'
```

Anything listed must be stopped and disabled — `sudo systemctl disable --now
apache2` or `nginx` — or moved to another port.

**6. Check it worked.**

```bash
git --version
docker version
docker compose version
```

All three must print a version, and `docker version` must show a **Server**
section as well as a Client. If it shows only a Client, the daemon is not
running; go back to step 3.

## 2. Get and configure HomeAutoShop

Clone the repository and enter it:

```bash
git clone https://github.com/EccentricWkshp/HomeAutoShop.git
cd HomeAutoShop
```

In Windows PowerShell the equivalent is:

```powershell
git clone https://github.com/EccentricWkshp/HomeAutoShop.git
Set-Location .\HomeAutoShop
```

Keep the repository directory. The Compose stack reads `docker-compose.yml`,
the `Caddyfile` and the TLS mode files out of it — those are configuration, not
application code, which is why a clone is still needed even though nothing is
compiled here any more.

Upgrading later is both halves, in this order:

```bash
git pull                   # the compose file and Caddy configuration
docker compose pull        # the images themselves
docker compose up -d
```

`docker compose pull` alone is usually enough, and is all you need for a
release that changed nothing outside the images. Doing both is the habit that
never bites you, since a release that adds a service or an environment variable
needs the file as well.

Copy the example configuration:

```bash
cp .env.example .env
```

In Windows PowerShell:

```powershell
Copy-Item .env.example .env
notepad .env
```

Three values matter before first boot. The rest have working defaults.

| | |
| --- | --- |
| `SECRET_KEY` | Long and random. Sessions and signed values depend on it, so changing it later logs everyone out. |
| `POSTGRES_PASSWORD` | Anything, but not the shipped default. |
| `SITE_ADDRESS` | The hostname you will type in a browser — `shop.home.arpa` by default. Caddy issues a certificate for exactly this name, so a mismatch here is the most common reason the site will not load. |

`ALLOWED_HOSTS` must contain whatever you set `SITE_ADDRESS` to.

Any secret can be supplied out of a file instead by pointing `<NAME>_FILE` at a path.

**Almost everything else in this file is edited in the application instead.**
The shop name, units and currency, Offline Mode, the integration addresses and
their keys, the reminder settings and the backup schedule all live under
*Settings* in the account menu, and a value set there wins over the one in
`.env`. Set the three above, start it, and change the rest from a browser.

Three more are worth knowing about, for different reasons:

| | |
| --- | --- |
| `CREDENTIAL_KEY` | Encrypts the integration keys you enter in the UI. A key kept in the database it protects is not a key, so this one genuinely cannot move into *Settings*. Leave it blank and it is derived from `SECRET_KEY` — that works, and it is weaker, because then one secret protects two things. Changing it invalidates every stored credential at once, which is the intended emergency behavior. |
| `TESSERACT_LANGS` | Which OCR languages the application asks Tesseract for. `eng` by default, and **the published image carries English only**. Asking for one it does not have costs a line in the log rather than broken OCR: the application narrows to what is installed, because Tesseract fails the whole call for a single missing language rather than skipping it. To actually *have* another language, build the image yourself (step 3) — there this same variable also decides which packs are installed, so the two halves cannot drift. Once running, the language list is editable under *Settings → Media* like anything else. |
| `HOMEAUTOSHOP_TAG` | Which release to run. Unset means `latest`, which is right while the interfaces are still moving. Set it to a version — `0.7.1` — to pin, and nothing changes under you until you edit it. |

## 3. Pull and start it

All three images are published and pre-built: the application and its
DNS-enabled Caddy proxy on Docker Hub, PostgreSQL on its own registry. Each is
one manifest covering **amd64 and arm64**, so a Pi or an ARM NAS pulls the
right build without being told which it is.

```bash
docker compose pull
docker compose up -d
```

Those commands are the same in Bash and PowerShell. `pull` is separate only to
make the download visible; `up` would fetch whatever was missing anyway.

> **Building them yourself instead.** There are three reasons to want that —
> your own changes to the source, an OCR language the published image does not
> carry, or a DNS provider that is not one of the four compiled into the proxy
> — and `docker-compose.build.yml` puts the build back:
>
> ```bash
> docker compose -f docker-compose.yml -f docker-compose.build.yml build
> docker compose -f docker-compose.yml -f docker-compose.build.yml up -d
> ```
>
> Put `COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml` in `.env` and
> plain `docker compose` does it from then on. It costs minutes of apt, pip and
> a Go compile against one download, which is why it is no longer the default.

Four containers start. The `app` container runs migrations and seeds the
built-in templates before gunicorn starts, so the first boot takes longer than
later ones. Static files are already collected into the image, so nothing is
built at boot. Watch it settle:

```bash
docker compose ps          # all should reach healthy
docker compose logs -f app
```

## 4. Create the first account

Open the site in a browser. An instance with no accounts sends you straight to
**Set up your shop**, which asks for four things and takes about a minute:

- the administrator account — username, password, and optionally your name
- the shop name, units, currency, timezone and language
- whether to install the starter data (maintenance schedules, inspection
  templates, scan-tool profiles, manual libraries — nothing about your
  vehicles)

It also tells you what has to happen before the phone in the garage will trust
this site, which depends on your `TLS_MODE` and is the step people miss.

The page exists **only while there are no accounts**. The moment the first one
is created it redirects to sign-in for good, so it cannot be used a second time
to mint an owner. There is no flag to reset and nothing to turn off.

Everyone else you add from **Administration → Users**.

<details>
<summary>Making the first account from a terminal instead</summary>

You do not need this, and it is here for a headless install or a recovery:

```bash
docker compose exec app python manage.py createsuperuser
```

`createsuperuser` is Django's and knows nothing about this application's
roles, so it leaves `role = member` while setting the superuser flag.
Everything works — authorization short-circuits on the superuser flag — but the
two disagree, and clearing the flag later would quietly demote the account. The
setup page sets both; doing it this way, reconcile them yourself:

```bash
docker compose exec app python manage.py shell -c \
  "from homeautoshop.accounts.models import User; User.objects.filter(username='YOU').update(role='admin')"
```

</details>

## 5. Reach it from a browser

The stack listens on ports **80 and 443** — not 8000, which is the development
server's port and nothing to do with Compose. Requests arrive at Caddy, which
terminates TLS and forwards to the app.

TLS is not decoration here: service workers, the camera used for VIN barcode
scanning, and Web Serial all refuse to run outside a secure context, so there
is no plain-HTTP mode to fall back to.

### If ports 80 or 443 are already taken

A machine that already runs something on those ports — a NAS web interface,
another reverse proxy, IIS on Windows — fails at `docker compose up` with:

```text
failed to bind host port 0.0.0.0:80/tcp: address already in use
```

Set both ports in `.env` and start again:

```ini
HTTP_PORT=8080
HTTPS_PORT=8443
BASE_URL=https://shop.home.arpa:8443
```

```bash
docker compose up -d
```

**Only the host side moves.** Caddy still listens on 80 and 443 inside the
container, so there is no Caddy configuration to edit, no new certificate, and
nothing about TLS changes. The site is simply at `https://shop.home.arpa:8443`.

`BASE_URL` is the part worth getting right, and the reason it is listed above.
It is what **printed QR labels** for bins and vehicles encode, and what
notification links use — so a wrong port there is discovered by somebody
standing at a shelf months later, scanning a label that goes nowhere. The proxy
checks the two agree and says so at startup if they do not:

```text
warning: BASE_URL says port 443, but HTTPS is published on 8443.
```

Everything else follows automatically. `CSRF_TRUSTED_ORIGINS` defaults to
`BASE_URL`, and the `http://` → `https://` redirect is given the port so it
does not send browsers to a port nothing is listening on.

> Pick ports above 1024. Below that, Linux and macOS need root to bind, which
> is a separate problem from the one you are solving. 8080 and 8443 are the
> conventional pair.

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
common enough to name: see *Running Docker Engine inside your own WSL
distribution* below.

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
`acmedns`. Any of the ~100 others at <https://github.com/caddy-dns> means
building the proxy yourself, since a Caddy module cannot be loaded at runtime:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml \
  build --build-arg CADDY_DNS_MODULES=github.com/caddy-dns/hetzner proxy
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d proxy
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

After the proxy has started, copy its public root certificate into the current
directory. This command works unchanged in Bash and PowerShell and does not
copy any private key:

```bash
docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

Keychain Access on macOS (set to *Always Trust*);
`/usr/local/share/ca-certificates/` plus `update-ca-certificates` on Debian and
Ubuntu; and on Windows, in an administrator PowerShell:

```powershell
Import-Certificate -FilePath caddy-root.crt -CertStoreLocation Cert:\LocalMachine\Root
```

Firefox keeps its own trust store and needs the certificate added separately,
under Settings → Privacy & Security → Certificates.

##### Android phones and tablets

Copy `caddy-root.crt` to the device over a connection you trust, such as a USB
cable or your own local file share. Do not install a certificate sent from an
unknown source: trusting a root certificate allows it to authenticate sites to
that device.

Android menu names vary by manufacturer. On current Pixel devices, open
**Settings → Security & privacy → More security settings → Encryption &
credentials → Install a certificate → CA certificate**, then select
`caddy-root.crt` and approve the warning. Samsung and other devices commonly
put the same action under **Security → More security settings → Install from
device storage**. A persistent notice that the network may be monitored is
normal after installing a user-controlled CA.

Open `https://<SITE_ADDRESS>` in Chrome or Edge and confirm there is no
certificate warning before adding HomeAutoShop to the home screen. A managed
phone or tablet may prohibit user-installed CAs; its administrator must install
the root, or the instance must use `acme-dns` or another publicly trusted
certificate.

##### iPhone and iPad

Transfer `caddy-root.crt` with AirDrop, Files, or another channel you trust,
then tap it. iOS or iPadOS reports that a profile was downloaded. Complete both
of Apple's required steps:

1. Open **Settings**, tap **Profile Downloaded** (or open **General → VPN &
   Device Management**), select the downloaded Caddy certificate profile, and
   tap **Install**.
2. Open **Settings → General → About → Certificate Trust Settings** and turn
   on **Enable Full Trust for Root Certificates** for that root.

Installing the profile without the second trust switch does not enable it for
HTTPS. Open `https://<SITE_ADDRESS>` in Safari and confirm there is no warning
before adding HomeAutoShop to the Home Screen.

The root remains the same while the `caddy-data` volume exists, including
normal container rebuilds and upgrades. Export and install a new copy if that
volume is deleted or the internal CA is deliberately replaced.

#### custom

Put `fullchain.pem` and `privkey.pem` in `config/caddy/certs` and set
`TLS_MODE=custom`. Renewal is yours to arrange.

Now open your instance and sign in.

### Running Docker Engine inside your own WSL distribution

This section is for Docker Engine installed inside a Linux distribution under
WSL. It does not apply to the recommended Windows setup above, where Docker
Desktop publishes the ports for you.

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

## 7. Bringing an existing shop onto this machine

Moving from another machine, or rebuilding after a disk died. Finish everything
above first — this puts your data onto a working instance, it does not replace
setting one up.

A backup is a **folder**, not a file:

```text
20260903-233128/
  database.dump      the database
  media/             every photo and document
  manifest.json      what this folder is, and what it was taken from
```

`restore` reads all three, and the manifest is the one people arrive without.
It is the only part that cannot be downloaded on its own, and without it the
command refuses rather than restoring a dump it has no way to identify.

### If you can copy the whole folder

The clean path. On the old machine:

```bash
docker compose exec app python manage.py backup
docker compose exec app ls /data/backups
docker compose cp app:/data/backups/20260903-233128 ./20260903-233128
```

Copy that folder to the new machine — into `./backup/`, say — and then mount it
in. **The repository directory is not mounted into the container**, so a path
like `./backup/20260903-233128` does not exist inside it, which is what
`is not a directory` means if you hit it:

```bash
docker compose stop app worker
docker compose run --rm -v "$PWD/backup:/restore:ro" app python manage.py restore /restore/20260903-233128 --dry-run
docker compose run --rm -v "$PWD/backup:/restore:ro" app python manage.py restore /restore/20260903-233128
docker compose start app worker
```

Stop `app` and `worker` first. `pg_restore` drops and recreates every object and
needs exclusive locks; the worker polls the queue continuously and will fight it.

The dry run reports the backup's date, the dump size and the media count, then
stops without touching anything. Run it every time.

Add `--force` only if it says the instance already contains data — an admin
account alone does not count, since it checks for vehicles and work orders.

**If it stops on `Permission denied: .../manifest.json`**, the container runs as
uid 10001 and cannot read files that came off the other machine as root:

```bash
sudo chmod -R a+rX backup
```

Do not run the container as root to get past this. Media would then be restored
root-owned into a volume the application reads and writes as uid 10001 — trading
a restore that fails loudly for uploads that fail afterwards.

### If all you have are the downloads

The Backup screen hands out the database file and the export ZIP separately —
the media tree is gigabytes and is meant to be copied rather than downloaded —
and the manifest lives in the folder, so it is in neither. **Settings → Backup →
Restoring a backup from another machine** takes those two files back and
rebuilds the folder they came out of:

1. Upload `database.dump` (required) and `export-….zip` (optional, and where
   your photos are).
2. It writes a `…-uploaded` folder into the backup directory and prints the
   restore command for it.
3. Run that command, as above.

Uploading changes nothing by itself. Restore stays a command you run, for the
reason the screen gives: swapping the database out from underneath a running
application is not something a web request should attempt.

Without the export there is no media to restore, and no way to tell which
schema version the dump was taken under — it is recorded as this build's, which
means `restore` cannot catch a dump too old to apply. The screen says so rather
than leaving you to find out.

### Afterwards

```bash
docker compose exec app python manage.py migrate
```

Needed when the backup came from an older build, harmless when it did not.

Two things deliberately do not come back:

- **Integration keys.** They are excluded from the dump, so a backup carried on
  a USB stick is not a credential leak. *Settings* lists which ones need
  entering again.
- **Nothing else** — but if previews or thumbnails look missing,
  `docker compose exec app python manage.py rederive` regenerates them.

Check the shop is really there before deleting anything on the old machine:
open a vehicle, confirm its photos load, and look at *Instance health* to see
which machine you are actually on.

## 8. Worth doing next

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
- **Decide how hard a password has to be.** The default is twelve characters
  plus a check against the well-known ones, because length is what resists
  guessing. On an instance reachable only from a room you control, that is a
  toll rather than a defense — set `PASSWORD_POLICY` in `.env` to something
  else and restart:

  | Value | What it asks for |
  | --- | --- |
  | `12chars` | The default. Twelve characters, not a well-known one. |
  | `complex` | Sixteen characters, three of the four character kinds, not well known, not all digits, not like the account name. |
  | `6chars` | Six characters. Nothing else. |
  | `any` | Sign-in, and any password at all. |
  | `noauth` | **No sign-in at all.** |

  `noauth` means anyone who can reach the site has full access to everything in
  it, acting as the oldest administrator account. It is for a private network
  you control and never for anything behind a port forward. The site says
  *Sign-in is off* on every page while it is set, the health screen names it,
  and the container log says so at startup. With no administrator account yet
  it changes nothing — the first-run setup page still appears, because turning
  sign-in off cannot conjure the account it would sign you in as.

  A value that is not one of those five refuses to start rather than falling
  back to a default, so a typo cannot leave you believing something untrue
  about your own instance.

- **Know that the trash never empties itself.** A deleted record is kept so it
  can be restored, and nothing removes it on a schedule — the 30-day window is
  how long a restore is promised, not how long the row lives. When you want the
  space or the tidiness back:

  ```bash
  docker compose exec app python manage.py purge_trash          # reports only
  docker compose exec app python manage.py purge_trash --yes    # deletes
  ```

  Account menu → *Trash* shows the same thing per record, with a **Delete
  permanently** button. `/admin/` has a **Trash** page that spans every table.

## 9. Installing it on the phone

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

## 10. Reading a scan tool

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

### Reading codes straight off the car

Vehicle → *Diagnostics* → *Read the car directly* talks to an ELM327 adapter
from the browser, with nothing to install. The server never touches the
adapter: it runs in a container with no Bluetooth, and the phone standing next
to the car is the thing actually in range.

Chrome or Edge, over HTTPS. Safari and Firefox have not implemented either of
the browser APIs this needs, and neither one runs over plain HTTP.

Four things before the first read, in this order:

1. **Plug the adapter into the OBD-II socket** — under the dash on the
   driver's side on virtually everything since 1996.
2. **Pair it in the system Bluetooth settings**, not on this page. The browser
   can only offer an adapter the operating system has already paired. The
   usual code is `1234` or `0000`.
3. **On Android, give Chrome the “Nearby devices” permission** — *Settings →
   Apps → Chrome → Permissions → Nearby devices → Allow*. Android 12 put every
   Bluetooth device behind this, and Chrome cannot list one without it. Nothing
   reports the refusal: the chooser opens empty, which looks exactly like an
   adapter that will not pair. If Chrome has never asked you for it, it has not
   got it.
4. **Switch the ignition on.** This is the one that catches people. The socket
   is powered straight from the battery, so the adapter lights up, pairs and
   answers questions about itself whether or not the car is awake — it is the
   *car* that stays silent. If the page says the adapter is working but the car
   did not answer, this is why. A few cars only answer with the engine running.

Then press *Read codes* and pick the adapter from the browser's list.

**How the adapter connects** has two settings, because no single browser API
reaches every adapter:

- *Cable, or a paired Bluetooth adapter* — the common case, and what an
  OBDLink MX+ or any other Bluetooth Classic adapter needs.
- *Bluetooth LE adapter* — only for adapters that pair as Bluetooth LE. Those
  never appear in the first list, and the first kind never appears in this one.

#### When the list comes up empty

The browser says *No compatible devices found*, and then reports that no
adapter was chosen — it cannot tell an empty list from a canceled one, so it
blames the person either way. Work through:

- The adapter is paired, plugged in, and close enough to be awake.
- On a phone, Chrome has the **Nearby devices** permission (step 3 above —
  this is the commonest cause by a distance) and is version 137 or newer, since
  earlier ones cannot offer Bluetooth adapters at all. *Menu → Settings → About
  Chrome* gives the version.
- **If it appears on a desktop but not on a phone**, the adapter hides its
  serial service behind the maker's own identifier rather than the standard
  one. A desktop offers it regardless, because the operating system maps it to
  a serial port first; a phone maps nothing and withholds anything it has not
  been told to expect. Connect it once on the desktop, where the page prints
  `Bluetooth service:` followed by the identifier, then add that to `.env`:

  ```bash
  ELM327_BLUETOOTH_SERVICE_UUIDS=00001101-0000-1000-8000-00805f9b34fb,<the one it printed>
  ```

  and `docker compose up -d`. The first entry is the standard profile, which
  most adapters — the OBDLink MX+ among them — use; keep it.

#### What you get

Codes land as a **draft**, like a PDF report, and reach the vehicle's history
only once you have looked at them. A $12 dongle on a corroded connector
misreads at least as often as a parser does.

*Clear codes* is behind a confirmation that names the vehicle, because it also
resets the readiness monitors — and a car with unset monitors fails an
emissions test on the spot, sometimes for a hundred miles of driving
afterwards. Clear after the repair, not before the test.

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
networking — see *Running Docker Engine inside your own WSL distribution*
above. `Get-NetTCPConnection
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
404 page.

`collectstatic` does **not** run at boot — it runs during the image build, so
the files are baked in and the app container never writes them. That rules the
usual suspect out and leaves two: a `STATIC_HASHED` that disagrees with the
image (compose sets `true`, matching how the image was built, and overriding it
in `.env` asks templates for hashed filenames nothing wrote), or a proxy that
is not reaching the app at all, which the `app` logs will show as no request
arriving.

### Backups fail with a version mismatch

```
pg_dump: error: aborting because of server version mismatch
```

`pg_dump` refuses to read a server newer than itself. The application image
installs a client matching `PG_MAJOR` in the Dockerfile, and the published
image is built against the `db` image `docker-compose.yml` ships with — so
leaving both alone keeps them in step.

Raising the `db` image on your own is what breaks it: the published application
image still carries the older client, and no amount of pulling changes that.
Either wait for a release that moves both together, or build the application
image yourself with a matching `PG_MAJOR`:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml \
  build --build-arg PG_MAJOR=19 app
```

Nothing else breaks when these drift — the shop runs perfectly and only the
nightly backup fails — which is what makes it worth checking deliberately.

### You have no account

You have not run step 4, or the database volume was recreated since you did.
Open the site: with the accounts table empty, **Set up your shop** comes back
on its own and you can make the account again there.

### A deleted parts order still says it was already imported

An order deleted before this was corrected left its lines behind — a soft
delete removes no rows, so nothing cascaded — and the reader went on
recognizing the order from records no screen would show. Clear one order out
completely:

```bash
docker compose exec app python manage.py purge_order 205-1234567-0000001
docker compose exec app python manage.py purge_order 205-1234567-0000001 --yes
```

The first form reports the purchase, its lines, the stock it received, any
tooling expense and the provenance rows, and changes nothing. Parts, vendors
and fitments are deliberately kept: a part outlives the order that first
stocked it, and another order may have stocked it too.

It refuses to delete a received lot whose stock has since moved — used on a
job, adjusted, scrapped — because removing it would change what the shop
believes it has and what it believes that cost. Un-receive it first, or pass
`--force-stock` to accept that the inventory history will no longer explain
itself.
