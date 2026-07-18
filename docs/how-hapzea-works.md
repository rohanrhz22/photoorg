# How Hapzea Works — A Developer's Guide

This document explains **the whole Hapzea app** — what it does, and how it works
under the hood — in plain language. If you're a new developer joining the
project, read this first. It assumes no prior knowledge of the codebase.

Wherever a technical term shows up for the first time, it's explained right
there. There's also a [glossary](#12-glossary) at the end.

---

## 1. What is Hapzea, in one paragraph

A wedding or event has thousands of photos. A guest only wants **the photos they
are in**. Hapzea solves that: the guest takes a **selfie** on their phone, and
Hapzea finds every photo at the event that contains their face, and gives them a
private album to download. The photographer runs a desktop app on their own
computer; guests use their phones. No photo is ever uploaded to a stranger's
cloud — the matching happens on the photographer's machine.

---

## 2. The three pieces

Hapzea is not one program. It's **three cooperating pieces**. Keeping them
separate is the single most important idea in the whole project.

```
┌────────────────────┐        ┌──────────────────────┐        ┌────────────────────┐
│  Desktop app        │        │  The Helper (relay)   │        │  Guest's phone      │
│  (the .exe)         │        │  a small always-on    │        │  a web page in the  │
│                     │        │  server in the cloud  │        │  browser            │
│  • the photographer │◀──────▶│                       │◀──────▶│                     │
│    runs it          │        │  • holds the guest    │        │  • scan QR          │
│  • has the photos   │        │    link 24/7          │        │  • take a selfie    │
│  • does face        │        │  • never sleeps       │        │  • see your photos  │
│    matching         │        │                       │        │                     │
└────────────────────┘        └──────────────────────┘        └────────────────────┘
   Files: everything in           File: phorg/relay_server.py       No install — it's
   phorg/ EXCEPT relay_server.py  (+ relay_client.py talks to it)   just a web page the
                                                                    helper serves.
```

| Piece | What it is | Where it runs | Key files |
|---|---|---|---|
| **Desktop app** | The photographer's tool. Scans photos, matches faces, makes albums. | The photographer's Windows PC | `server.py`, `ui.html`, `vision.py`, `backends.py`, `registrations.py` |
| **The Helper** (we call it the *relay*) | A tiny always-on web server so guests can use the link even when the photographer's laptop is off. | A small cloud server you rent | `relay_server.py` |
| **Guest page** | The web page a guest sees after scanning the QR. | Served by whichever machine is answering (laptop or helper) | embedded inside `relay_server.py` (and `ui.html` for the laptop-served version) |

The desktop app and the helper talk to each other over the internet using
`relay_client.py` (the desktop's "phone line" to the helper).

---

## 3. The features (what a user can actually do)

**For the photographer (desktop app):**

- **Open an event folder** — browse to it, type the path, or drag it onto the window.
- **Share one guest QR / link** — the main feature. Guests scan it to find their photos.
- **Find one guest yourself** — add a selfie on the desktop and search manually.
- **Use up to 3 selfies** of the same person for more accurate matching.
- **Live results** — matches stream in on screen while the search is still running.
- **Confidence labels** — each match is tagged *Sure / Likely / Maybe*; weak ones start unticked.
- **Save an album** — copies the chosen photos into a new folder ready to share (never moves or deletes originals).
- **Guest dashboard** — see who searched, how many photos each got, and whether their album was emailed.
- **Light / dark theme**, first-run tips, iPhone HEIC photo support.

**For the guest (phone web page):**

- **Take a selfie** with the phone camera, or choose one from the gallery.
- **See their private album instantly** and download photos in full quality.
- **A permanent album link** they can reopen anytime — even days later.
- Works **whether or not the photographer's laptop is currently on**.

**Behind the scenes (platform features):**

- **Always-on availability** — the guest link keeps working after the laptop is shut down.
- **Full-quality delivery** — originals are delivered, not just small previews.
- **Auto-expiry** — all guest data is deleted automatically after a time the photographer sets.
- **Optional email delivery** and **optional PIN** protection per event.

---

## 4. The heart of it: how face matching works

This is the core technology. It lives in `vision.py` and runs **only on the
photographer's computer** (and optionally on the helper). Three steps:

### Step 1 — Detect faces
Given a photo, find where the faces are. Hapzea uses a small pre-built model
called **YuNet** (part of OpenCV, a popular image library). It returns a box
around each face.

### Step 2 — Turn each face into numbers (an "embedding")
For each detected face, a second model called **SFace** produces a list of
numbers — a **face embedding**. Think of it as a "fingerprint" of the face: a
list of (say) 128 numbers that captures what that face looks like. Two photos of
the *same* person produce two very *similar* number-lists; two different people
produce very *different* ones.

These fingerprints are **unit-norm** (a math detail: each list is scaled to
"length 1"). That makes the next step simple and fast.

### Step 3 — Compare fingerprints
To check if two faces are the same person, compare their fingerprints using
**cosine similarity** — basically "how aligned are these two number-lists?" The
result is a score from about 0 to 1:

- **1.0** = identical direction (same face)
- **~0** = unrelated

Because the fingerprints are unit-norm, cosine similarity is just a **dot
product** (multiply the two lists together, position by position, and add it
up). If the score is above a **threshold** (default **0.44**, guests **0.45**),
Hapzea calls it a match.

> **The selfie side:** a guest can send up to 3 selfies. Hapzea makes a
> fingerprint for each and **averages** them into one cleaner reference
> fingerprint. More selfies → fewer mistakes.

### The two models are downloaded once
The first time face matching is used, the two model files (YuNet + SFace) are
downloaded into `~/.phorg/models`. After that everything is offline. The app
runs its web interface fine without them; only the *matching* waits until
they're present. (`check_deps()` and `models_present()` report readiness.)

---

## 5. The desktop app, explained

### It's a local website
Here's a surprise for newcomers: the desktop app is actually a **tiny web
server** that talks to your **browser**. When the photographer launches it:

1. `server.py` starts an HTTP server (Python's built-in `http.server`).
2. It opens the browser to `http://127.0.0.1:8765`.
3. The browser loads `ui.html` — a single-page app (all the buttons and screens).
4. Every button click calls a small **JSON API** (e.g. `POST /api/facefind/start`).

So the "desktop app" is: a Python backend + an HTML/JavaScript front-end,
stitched together locally. No internet needed for the photographer's own work.

### Why a browser instead of a native window?
Zero extra libraries. The whole thing uses only Python's standard library plus
the optional vision packages. That keeps the promise: **nothing extra to
install**. (When packaged as an `.exe`, `_resource()` in `server.py` knows how
to find `ui.html` and friends inside the bundle.)

### The API, in brief
`server.py` defines a `ROUTES` table mapping API paths to Python functions.
The important ones:

| What the user does | API call | What happens |
|---|---|---|
| Open a folder | `/api/scan` | Counts the photos in the folder |
| Install the AI | `/api/vision/install` | pip-installs the vision packages (from source) |
| Add a selfie | `/api/facefind/selfie` | Saves the selfie, returns a path |
| Find a guest's photos | `/api/facefind/start` | Kicks off a background matching **job** |
| Check progress | `/api/cluster/progress` | Returns "checked 812 of 2000 (40%)" + live matches |
| Save the album | `/api/categorize/apply/start` | Copies chosen photos into a new folder |
| Turn on guest sharing | `/api/share/enable` | Creates the event on the helper, returns the QR link |

Long tasks (like scanning 3000 photos) run as **background jobs**: the API
starts the job and returns immediately, and the UI polls `/api/cluster/progress`
every second to update the progress bar. This keeps the interface responsive.

### Scanning is careful and safe
`backends.py` walks the event folder. `safety.py` decides what to skip: it never
touches OS/app system folders, hidden folders, or Hapzea's own output folders.
**Hapzea never deletes or moves your originals** — making an album *copies*
files into a new folder. This "do no harm" rule is a core value of the app.

### Two speed tricks
Matching 3000 photos would be slow if done naively. Two things make it fast:

1. **Multi-core matching.** The heavy work (decoding a photo + running the
   models) is spread across several CPU cores at once using a thread pool.
   OpenCV "releases the GIL" during this work — meaning Python can genuinely run
   these in parallel — so a scan uses the whole processor, not one core.

2. **Embedding cache + pre-warming.** Each photo's fingerprints are saved in a
   small SQLite cache (`hashcache.py`, keyed by file path + size + modified
   time). So the *second* guest who searches the same event doesn't re-analyze
   the photos — they're already fingerprinted. On top of that, `index_faces`
   ("pre-warm") quietly fingerprints the whole folder in the background right
   after you open it, so the very first search feels instant.

### Nothing is lost if the app closes
`registrations.py` keeps a durable record (in `~/.phorg/registrations.db`) of
every guest sign-up and its results, and copies each selfie somewhere safe. If
the app crashes or is closed mid-search, it **resumes** unfinished matches on
next launch. Guests aren't forgotten.

---

## 6. The Helper (relay), explained

This is the piece that makes "the link works even when the laptop is off"
possible. It's `relay_server.py` — a small, independent web server you run on a
cheap always-on machine in the cloud.

### Why it exists
The photographer's laptop goes to sleep, gets packed into a bag, loses Wi-Fi.
If the guest QR pointed at the laptop, the link would die. So the QR points at
the **helper** instead. The helper never sleeps, so the guest link always works.

### It's deliberately simple
The helper uses **only Python's standard library** — `http.server` for the web
part and `sqlite3` for storage. Nothing to install. You can run it with one
command: `python -m phorg relay --port 8080`.

### What it stores
The helper keeps its data under `~/.phorg/relay` (or a folder you choose via the
`PHORG_RELAY_HOME` setting):

- **`relay.db`** (SQLite) — events, guest sign-ups, match results, and
  medium-size delivery images.
- **`originals/<event>/`** (plain files) — full-quality photo files, kept only
  for photos that actually matched a guest (so it doesn't waste space).

> **Why SQLite + plain files instead of a "real" database?** For this app's
> size (a handful of events at a time), it's the *correct* choice, not a
> shortcut: zero setup, zero cost, and backing up is just copying one folder.

### Every event is isolated
Each event has an **id** and a secret **key**. Only the photographer's app knows
the key. The helper checks the key (using a constant-time comparison so it can't
be guessed by timing) before letting anyone publish photos, pull sign-ups, or
delete an event. Guests never see the key — they get their own unguessable
**album token** instead. So one event can never read or damage another's data.

### The guest page it serves
Open the helper's URL and it returns a single self-contained web page (the
`_PORTAL` string in `relay_server.py`). That page lets the guest register (take
a selfie), then shows their album. It's plain HTML/JavaScript with no external
libraries, styled to look like elegant wedding stationery.

---

## 7. The star feature: one link, laptop on OR off

This is the clever part. Guests always use **one link** (the helper's). What
changes behind the scenes is *who does the face matching*.

### Mode A — Laptop is ON (best quality, "store-and-forward")
1. Guest opens the link, submits a selfie. The helper **queues** the sign-up
   (saves it durably) and immediately tells the guest "finding your photos…".
2. The photographer's app is running a loop (`_relay_loop` in `server.py`, every
   ~20 seconds). It **pulls** the queued sign-ups from the helper.
3. The laptop matches faces **locally** (all the heavy AI stays on the
   photographer's machine) and **posts the results back** to the helper —
   including medium-size images for the gallery.
4. The guest's page updates itself and shows the album.

### Mode B — Laptop is OFF ("Tier B" instant match)
For this to work, the laptop must have **synced an index ahead of time**. While
it was on, the same loop published (every ~5 minutes) an **embedding index** to
the helper: for every event photo, its face fingerprints plus a medium-size
image. That's `_relay_publish_index` on the desktop → `/api/index` on the
helper.

So when the laptop is off:
1. Guest submits a selfie. The helper itself computes the selfie's fingerprint
   (it has the vision packages installed too) and compares it against the stored
   index — the same cosine-similarity math from Section 4.
2. It finds the matches instantly and shows the album. **The laptop was never
   involved.**

If the helper *doesn't* have the vision packages, it gracefully falls back to
Mode A (queue the guest and wait for the laptop).

### Getting full quality even in Mode B
Mode B shows medium-size images (that's what the index carries). But guests want
**originals**. So the laptop, whenever it's on, also uploads the full-quality
files for matched photos in the background (`_relay_upload_originals` →
`/api/original`). When a guest taps *download*, the helper serves the original
if it has it, otherwise the medium copy — and the **same link** upgrades to full
quality automatically once the laptop has uploaded it. The guest does nothing;
they just refresh.

> **The mental model:** the guest always talks to the helper. The helper is
> either relaying the laptop's answers (Mode A) or answering from its own
> synced copy (Mode B). Either way: one link, always works, ends up full
> quality.

---

## 8. A guest's journey, start to finish

Putting it all together, here's what actually happens when a guest uses Hapzea:

1. **Scan** the QR at the venue → phone opens `https://cloud.hapzea.com/?e=<eventid>`.
2. The page asks the helper "what's this event?" (`/api/event/public`) and shows
   a **Take a selfie** button.
3. Guest snaps a selfie. The phone **shrinks it** to ~1280px first (faster on
   crowded venue Wi-Fi).
4. The page tries **instant match** (`/api/match`). If the helper can answer
   (Mode B), the album appears in a second. If not, it **registers** the guest
   (`/api/register`) and shows "finding your photos…".
5. The guest gets a **private album link** to save. The page auto-refreshes
   until the photos are ready.
6. Guest taps photos to **download in full quality**. Done.

At no point did the guest install anything, and their selfie was used once, only
to find their photos.

---

## 9. Where all the data lives

| Data | Where | On whose machine |
|---|---|---|
| The event photos (originals) | The photographer's chosen folder | Photographer's PC |
| Face fingerprint cache | `~/.phorg/models` (models), `faces.db` cache in the event folder area | Photographer's PC |
| Guest sign-ups & results (durable) | `~/.phorg/registrations.db` | Photographer's PC |
| Copied selfies | `~/.phorg/events/selfies` | Photographer's PC |
| Events, guests, results, medium images | `relay.db` (SQLite) | The helper (cloud) |
| Full-quality originals (matched only) | `originals/<event>/` | The helper (cloud) |
| Downloaded albums | The guest's phone | Guest's phone |

The **originals never leave the photographer's PC** except for the medium copies
and the matched full-size files that must live on the helper so guests can be
served when the laptop is off. All heavy AI work stays local.

---

## 10. Security model (in plain terms)

- **The desktop app only answers itself.** Its web server rejects requests
  unless they're addressed to `localhost` — this blocks a malicious website from
  secretly driving the app through your browser (a "DNS-rebinding" attack). When
  guest sharing is on, only the small guest whitelist is opened to the local
  network.
- **Event keys** gate every sensitive helper action (publishing photos, pulling
  sign-ups, deleting an event). They're compared in constant time so they can't
  be guessed by measuring response speed.
- **Album links are unguessable** random tokens. There's no way to browse to
  someone else's album.
- **Event IDs are validated** so two events can never collide on disk (this
  closed a bug where a crafted event ID could delete another event's originals).
- **Auto-expiry** deletes an event's guests, results, and originals after the
  time the photographer sets.
- **Optional PIN** per event for an extra gate.

---

## 11. The tech stack, at a glance

| Layer | Technology | Why |
|---|---|---|
| Desktop backend | Python standard library (`http.server`) | No install; ships as a small `.exe` |
| Desktop front-end | Plain HTML + CSS + JavaScript (`ui.html`) | Self-contained single page |
| Face detection | OpenCV **YuNet** model | Small, fast, offline |
| Face recognition | OpenCV **SFace** model | Produces the face fingerprints |
| Number crunching | **numpy** | Fast fingerprint math |
| Local storage | **SQLite** (`sqlite3`) + plain files | Durable, zero-setup |
| The helper | Python standard library (`http.server` + `sqlite3`) | One-command deploy, no dependencies |
| HTTPS in production | **Caddy** in front of the helper | Automatic certificates |
| Photo delivery | Medium images in SQLite; originals as files | Fast gallery, full-quality download |
| iPhone photos | `pillow-heif` (optional) | Reads HEIC/HEIF |

For how to actually deploy and package all this, see
[production-guide.md](production-guide.md).

---

## 12. Glossary

- **Embedding / fingerprint** — a list of numbers describing a face. Same person
  → similar lists.
- **Cosine similarity** — a score (≈0 to 1) for how alike two fingerprints are.
  The match test.
- **Threshold** — the score above which two faces count as the same person
  (default 0.44).
- **The helper / relay** — the always-on cloud server (`relay_server.py`) that
  keeps the guest link alive.
- **Store-and-forward (Mode A)** — laptop on: helper queues guests, laptop
  matches and posts results back.
- **Tier B / instant match (Mode B)** — laptop off: helper matches guests itself
  against a pre-synced index.
- **Index** — the pre-computed fingerprints + medium images the laptop uploads so
  the helper can match without it.
- **Original** — the full-quality photo file (vs. the medium preview).
- **Event id / key** — the public name and the secret password of an event on
  the helper.
- **Album token** — a guest's private, unguessable key to their own album.
- **Job** — a long background task on the desktop (like a scan) that the UI polls
  for progress.
- **Prewarm** — fingerprinting the whole folder in the background so the first
  search is instant.

---

## 13. Repository map

| File | What it does |
|---|---|
| `phorg/server.py` | The desktop app: local web server, JSON API, jobs, the relay-sync loop |
| `phorg/ui.html` | The desktop app's entire interface (HTML/CSS/JS) |
| `phorg/vision.py` | The face engine: detect, embed, match; caching; pre-warm |
| `phorg/backends.py` | Walks the event folder and reads files |
| `phorg/safety.py` | Rules for which folders/files must never be touched |
| `phorg/hashcache.py` | The SQLite cache of face fingerprints |
| `phorg/registrations.py` | Durable store of guest sign-ups & results (survives restart) |
| `phorg/relay_server.py` | **The helper**: standalone always-on server + the guest page |
| `phorg/relay_client.py` | The desktop's client for talking to the helper |
| `phorg/notify.py` | Optional email (SMTP) delivery of album links |
| `phorg/tunnel.py` | Optional Cloudflare quick-tunnel (an earlier sharing path) |
| `phorg/cli.py` | Command-line entry: launches the app, or `relay` for the helper |
| `tests/test_relay.py` | Tests for the helper (round-trip, isolation, matching, originals) |
| `tests/test_core.py` | Core logic tests |
| `docs/production-guide.md` | How to package the `.exe` and deploy the helper |

---

*If anything here drifts from the code, the code is the source of truth — please
update this file when you change how a piece works.*
