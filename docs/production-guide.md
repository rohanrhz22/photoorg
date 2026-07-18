# Hapzea — Production & Delivery Guide

How to take this repository from "works on my laptop" to a product that
photographers download, install, and use at real events. Written so any
developer can follow it top to bottom and execute each step.

---

## 1. The big picture

Hapzea ships as **three pieces**. Only one of them runs in the cloud.

```
┌──────────────────┐      ┌───────────────────────┐      ┌───────────────────────┐
│   hapzea.com     │      │  Photographer's PC     │      │  cloud.hapzea.com      │
│  marketing site  │ ───▶ │  Desktop app (.exe)    │ ◀──▶ │  The HELPER (relay)    │
│  + Download page │      │  photos + AI matching  │      │  always-on, serves     │
│  (static, free)  │      │  everything stays local│      │  guests 24/7           │
└──────────────────┘      └───────────────────────┘      └───────────▲───────────┘
                                                                      │
                                                          ┌───────────┴───────────┐
                                                          │  Guest's phone         │
                                                          │  scans ONE QR, takes a │
                                                          │  selfie, gets photos   │
                                                          └───────────────────────┘
```

**The flow at an event:**

1. Photographer opens the desktop app, picks the event folder, pastes the
   cloud link (`https://cloud.hapzea.com`), clicks **Create the guest QR**.
2. Guests scan the QR. The QR points at the **helper**, never at the laptop.
3. Laptop ON → it matches faces locally and pushes results (and full-quality
   originals) to the helper. Laptop OFF → the helper matches guests itself
   against the index the laptop already synced. Same link either way.

**The key business fact:** you run **one helper for all clients**. Every event
is isolated by its own `event_id` + secret key (created automatically), so a
single server at `cloud.hapzea.com` serves every photographer who buys the app.

---

## 2. Which file is which piece

| Piece | Files | Runs where |
|---|---|---|
| Desktop app | everything in `phorg/` except `relay_server.py` (UI: `ui.html`, engine: `server.py`, `vision.py`, `backends.py`, …) | Photographer's Windows PC |
| **The helper** | `phorg/relay_server.py` (+ `relay_client.py` is the *desktop-side* client that talks to it) | Your cloud server |
| Guest page | embedded inside `relay_server.py` (`_PORTAL`) — nothing separate to deploy | Served by the helper |
| Website | does not exist yet in this repo | Static hosting (free) |

The helper is deliberately **standard-library-only Python** (http.server +
sqlite3). Deployment is: copy the folder, run one command.

---

## 3. Deploy the helper (cloud server)

### 3.1 What to buy

| Item | Recommendation | Cost |
|---|---|---|
| Server | Hetzner CX32 (4 GB RAM) or DigitalOcean 4 GB droplet, **Ubuntu 24.04** | ~€7 / $12 mo |
| Disk | 80–100 GB (originals are ~10 GB per full event) | included / +$ |
| Domain | `hapzea.com` at Cloudflare or Namecheap | ~$10 / yr |
| Backups | tick the provider's "automatic backups" box | ~$1–2 / mo |

4 GB RAM is needed because the helper also runs the face engine (for instant
matching while laptops are off). Without it a 1 GB box works, but guests who
search while the laptop is off would wait until it comes back.

**DNS:** add an `A` record → `cloud.hapzea.com` → your server's IP.

### 3.2 Install (run once, ~15 minutes)

SSH in as root, then:

```bash
# 1. basics + a non-root user for the service
apt update && apt install -y python3-venv python3-pip git
useradd -r -m -d /opt/hapzea hapzea

# 2. the code (copy this repo; git clone or scp — your choice)
git clone https://github.com/YOURORG/hapzea.git /opt/hapzea/app
python3 -m venv /opt/hapzea/venv
/opt/hapzea/venv/bin/pip install -r /opt/hapzea/app/requirements-vision.txt

# 3. storage location (SQLite DB + original photos live here)
mkdir -p /var/lib/hapzea/relay
chown -R hapzea:hapzea /opt/hapzea /var/lib/hapzea
```

### 3.3 Run it as a service (survives reboots)

Create `/etc/systemd/system/hapzea-relay.service`:

```ini
[Unit]
Description=Hapzea always-on helper (relay)
After=network-online.target
Wants=network-online.target

[Service]
User=hapzea
WorkingDirectory=/opt/hapzea/app
Environment=PHORG_RELAY_HOME=/var/lib/hapzea/relay
ExecStart=/opt/hapzea/venv/bin/python -m phorg relay --port 8080
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now hapzea-relay
systemctl status hapzea-relay        # should say "active (running)"
```

### 3.4 HTTPS with Caddy (2 lines of config, automatic certificates)

```bash
apt install -y caddy
```

Replace `/etc/caddy/Caddyfile` with:

```
cloud.hapzea.com {
    reverse_proxy 127.0.0.1:8080
    request_body {
        max_size 80MB
    }
}
```

```bash
systemctl reload caddy
```

Caddy fetches and renews the TLS certificate itself. The `max_size` matters:
the desktop app uploads full-quality originals up to 60 MB each.

### 3.5 Verify

```bash
curl https://cloud.hapzea.com/healthz        # → {"ok": true}
```

Then on any PC: run the desktop app, paste `https://cloud.hapzea.com` as the
cloud link, create a QR, and scan it with a phone on **mobile data** (not the
same Wi-Fi) — that proves the whole path works from the public internet.

### 3.6 Storage & "which database?"

**You already have the database.** The helper uses:

* **SQLite** (`/var/lib/hapzea/relay/relay.db`) — events, guest sign-ups,
  match results, medium-quality delivery images.
* **Plain files** (`/var/lib/hapzea/relay/originals/<event>/`) — full-quality
  originals, uploaded only for photos that matched someone.

For this product's scale (a handful of simultaneous events) this is the
*correct* production setup, not a shortcut: zero maintenance, zero extra cost,
trivially backed up (it's one folder). Events auto-delete after the expiry the
photographer sets (retention is built in — Phase 5).

**Upgrade path, only when you have dozens of concurrent events:** move
metadata to PostgreSQL and images to S3-compatible object storage
(Cloudflare R2 is ideal — zero egress fees, and guests downloading albums is
pure egress). Do not build this on day one.

### 3.7 Updating the helper

```bash
cd /opt/hapzea/app && git pull
systemctl restart hapzea-relay       # guests see ~2 seconds of downtime
```

---

## 4. Build the Windows desktop app (.exe)

### 4.1 How it works

* **PyInstaller** freezes Python + the `phorg` package into a distributable
  folder. The code is already frozen-aware: `server.py → _resource()` checks
  `sys._MEIPASS`, so `ui.html`, `qrcode.min.js` and `logo.png` load correctly
  inside a bundle.
* **Inno Setup** wraps that folder into a single `HapzeaSetup.exe` with an
  install wizard, Start-Menu shortcut, and uninstaller.

### 4.2 One-time setup on your build PC

```powershell
pip install pyinstaller
pip install -r requirements-vision.txt   # bundle the AI in (recommended)
# Inno Setup: download from jrsoftware.org/isinfo.php
```

**Decision — bundle the AI or not?** Bundling `opencv`, `numpy` etc. makes the
installer large (several hundred MB) but the app *just works* offline.
Recommended. (The in-app "Install it now" button uses pip, which does not
exist inside a frozen exe — so for the exe, bundling is the way.)

### 4.3 Entry point

Create `build/launcher.py`:

```python
from phorg.cli import main

if __name__ == "__main__":
    main(["--no-browser"] if False else None)   # default: opens the browser
```

### 4.4 Build command

```powershell
pyinstaller --noconfirm --name Hapzea --windowed `
  --icon build\hapzea.ico `
  --add-data "phorg\ui.html;phorg" `
  --add-data "phorg\qrcode.min.js;phorg" `
  --add-data "phorg\logo.png;phorg" `
  --add-data "phorg\haarcascade_frontalface_default.xml;phorg" `
  --collect-all cv2 --collect-all numpy --collect-all PIL `
  build\launcher.py
```

Output lands in `dist\Hapzea\`. Test it on a machine **without Python
installed** before shipping. Use folder mode (the default here), not
`--onefile` — folder mode starts faster and avoids temp-extraction issues
with the large AI libraries.

### 4.5 Installer script (Inno Setup)

`build/hapzea.iss`:

```ini
[Setup]
AppName=Hapzea
AppVersion=1.0.0
AppPublisher=Hapzea
DefaultDirName={autopf}\Hapzea
OutputBaseFilename=HapzeaSetup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "..\dist\Hapzea\*"; DestDir: "{app}"; Flags: recursesubdirs

[Icons]
Name: "{autoprograms}\Hapzea"; Filename: "{app}\Hapzea.exe"
Name: "{autodesktop}\Hapzea"; Filename: "{app}\Hapzea.exe"

[Run]
Filename: "{app}\Hapzea.exe"; Description: "Start Hapzea"; Flags: postinstall nowait
```

Compile it in Inno Setup → you get `HapzeaSetup.exe`. That's the file
photographers download.

### 4.6 Pre-fill the cloud link (recommended product touch)

The desktop app reads the env var `HAPZEA_PRODUCTION_URL` as a default cloud
link. Add one line to the Inno `[Registry]` section (or ship a tiny config)
setting it to `https://cloud.hapzea.com`, and photographers never type a URL
at all — they install, pick a folder, click Create QR. Done.

### 4.7 Code signing (do this once you charge money)

Unsigned installers trigger Windows SmartScreen ("unknown publisher").
Buy an OV code-signing certificate (~$100–400/yr, e.g. Certum, SSL.com),
then sign both exes:

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 HapzeaSetup.exe
```

You can launch without it (tell early users to click "More info → Run
anyway"), but sign before wide release.

---

## 5. The website (hapzea.com)

| What | Where | Cost |
|---|---|---|
| Marketing site (static HTML) | Cloudflare Pages or Netlify | free |
| The installer file (~large) | Cloudflare R2 (free egress) or GitHub Releases | free |
| DNS + domain | Cloudflare | ~$10/yr |

Pages to build: **Home** (hero + phone mockup of the guest page),
**How it works** (the 3 steps), **Pricing**, **Download** (big button →
`https://downloads.hapzea.com/HapzeaSetup.exe` on R2).

Never serve the big .exe from the static-site host; link to R2/Releases.

---

## 6. Security model (already built — just know it)

* Each event has a **secret key**; only the photographer's app holds it.
  Guests never see it — they get per-guest signed album tokens.
* Guest albums are unlisted URLs (`rid.atoken`); no browsing other albums.
* Optional **PIN** per event, and **auto-expiry** purges all guest data and
  originals after the time the photographer sets.
* HTTPS everywhere via Caddy. The helper never stores the event key in URLs.

---

## 7. Launch checklist

- [ ] Buy `hapzea.com`, point `cloud.hapzea.com` at the new server
- [ ] Deploy the helper (§3) and pass the phone-on-mobile-data test
- [ ] Build `HapzeaSetup.exe` (§4), test on a clean Windows machine
- [ ] Pre-fill the cloud link (§4.6)
- [ ] Put up the website + R2 download (§5)
- [ ] Turn on server backups
- [ ] Dry-run a full fake event end to end: install → folder → QR →
      guest selfie on phone → album → shut the laptop → guest searches again
- [ ] Later: code-signing certificate (§4.7)

**Running cost at launch: ~$10–15/month** (server + domain). Everything else
in this stack is free.

---

## 8. FAQ for developers

**Why one helper for everyone, not one per client?**
Events are isolated by id + key in SQLite; the helper is stateless beyond its
storage folder. One box serves many events. Split it only when disk or CPU
says so.

**What if the helper dies mid-event?**
Guests can't search while it's down (the QR points at it). This is why you
use a real VPS with backups, not a Pi under a desk. Restore = restore the
storage folder, restart the service; all links keep working.

**Can we run the helper on Docker/Render/Railway instead?**
Yes — it's one Python process. Just guarantee a **persistent volume** for
`PHORG_RELAY_HOME` (Render's free tier has no persistent disk — don't use it).

**Mac version of the desktop app?**
PyInstaller builds Mac apps too, but only *on* a Mac. Ship Windows first;
that's where the photographer market is.
