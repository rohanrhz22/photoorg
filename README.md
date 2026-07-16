# 🙋 FaceFind — find every photo a guest is in, from one selfie

FaceFind is a **local, private** desktop app for photographers and event hosts.
Point it at an event folder, add a **selfie**, and it finds **every photo that
person appears in** — then copies them into a personal album ready to share.

The whole flow is **3 clicks**:

1. **Open the event folder** (browse, type a path, or drag it onto the window)
2. **Add a selfie** of the person you're finding
3. **Find their photos** → review the matches → **Save album**

All face matching runs **100% on your PC** — nothing is ever uploaded, and your
original photos are never moved (matches are **copied** into
`FaceFind_<name>/`).

---

## ✨ What it does

- **FaceFind** — from a single selfie, scan the whole event tree and surface
  every photo that guest is in (including group shots). A **Match strictness**
  slider trades off "more photos" vs. "only sure matches". Untick any wrong
  matches, then **Save album** — the app copies them into `FaceFind_<name>/`
  (originals untouched) so you can zip and send via WhatsApp / email.

- **Shareable guest link + QR** — turn on the **guest self-service portal** and
  FaceFind gives you a link and a **QR code**:
  - **Same‑Wi‑Fi link** — guests on your network scan the QR, add a selfie on
    their own phone, and download the photos they're in.
  - **Internet link (optional)** — one toggle creates a secure **HTTPS** relay
    (via Cloudflare) so guests can open it **from any phone, anywhere** — no
    shared Wi‑Fi needed. This is the moment that matters: guests find *"photos
    of me"* instantly, without scrolling thousands of images.

  Sharing is **off by default**. When on, it exposes **only** the guest portal
  (never your file system), and it stops the moment you close the app. You can
  watch guests search live and preview what each one matched.

---

## 🖥️ Run it

FaceFind is a desktop app — running it opens the interface in your browser.

**Requirements**
- **Python 3.8+**
- The face‑recognition packages (one‑time):
  ```powershell
  pip install -r requirements-vision.txt
  ```
  (or click **“Install AI support”** in the app when running from source)

**Start it**
```powershell
python -m phorg
```
or double‑click **`phorg-ui.bat`** / **`phorg.bat`**. Your browser opens at
`http://127.0.0.1:8765/`.

Options: `--port N` to choose a port, `--no-browser` to skip auto‑opening.

---

## 📦 Build a standalone .exe (optional)

No Python needed to *run* the result — just to build it:

```powershell
.\build_app.bat
```
Output: `dist\phorg.exe`. Double‑click it and FaceFind opens in the browser.

---

## 🔒 Privacy & safety

- **Local‑only matching.** Faces are compared on your PC. Selfies are held in a
  temp folder just long enough to run the search.
- **Originals are never moved.** Saving an album makes **copies**.
- **Loopback‑locked by default.** The API only answers `127.0.0.1` until you
  explicitly enable guest sharing; even then, LAN/relay visitors can reach
  **only** the guest portal endpoints.

---

## 🧩 Project layout

```
phorg/
  server.py     # local HTTP server + FaceFind/guest/share JSON API
  ui.html       # the FaceFind single-page interface
  vision.py     # face detection & matching (the FaceFind engine)
  backends.py   # local (and ADB) file access + safe apply
  safety.py     # protected-folder rules
  tunnel.py     # optional Cloudflare relay for the internet share link
  peopledb.py   # saved-faces helper used by the matcher
  hashcache.py  # per-folder cache for fast re-scans
run_phorg.py    # entry point (python run_phorg.py)
```
