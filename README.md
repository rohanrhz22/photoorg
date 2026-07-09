# 📱 phorg — Phone/Folder Storage Organizer

A safe, **rule-based** CLI that reorganizes a messy file tree into a clean,
reviewable structure — the same workflow used to reorganize a Xiaomi phone,
turned into reusable software.

Works on:
- **Android phones over ADB** (`--backend adb`)
- **Any local folder** on your PC (`--backend local`)

It only ever **moves** files (instant + reversible) and **rmdir**s empty
folders. It **never deletes files**, and it hard-protects OS / app-critical
folders (`Android/`, `MIUI/`, hidden dot-folders, WhatsApp `Sent`/`Private`, …).

---

## Requirements
- **Python 3.8+** (standard library only — no `pip install` needed)
- For ADB mode: **Android platform-tools** (`adb`) and a phone with USB
  debugging enabled. Auto-detected at
  `%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe`, or pass `--adb`.

## Install
Nothing to install — just run it from this folder:

```powershell
python -m phorg --help
```

Or use the launcher:

```powershell
.\phorg.bat --help
```

---

## 🖥️ The app (recommended)
Prefer clicking to typing? Launch the built-in **web UI** — a modern,
zero-dependency interface that walks you through everything:

```powershell
python -m phorg ui
```

or just double-click **`phorg-ui.bat`**. Your browser opens automatically at
`http://127.0.0.1:8765/`.

What you get:
- **Connect** to a local folder (with a built-in folder picker), or just
  **drag a folder onto the window** to connect instantly.
- **Dashboard** — used space, file counts, disk usage, a bar chart and an
  **interactive treemap** you can **click to drill into** sub-folders. It also
  has a **file search** across the whole tree (jump straight to a file's
  folder), a **“Space you could reclaim”** panel (duplicates + junk + large old
  files) and an **audit log you can export to CSV**.
- **Organize** — one click per action (Junk Sweep, **Recommended Cleanup**, by
  Type, by Size, **Bulk Rename** (with live preview), Fix Extensions, Find
  Duplicate Files, **Similar Filenames**, Flatten Folders, Sort by Location,
  Remove Empties). Every action shows a full **preview** first; nothing changes
  until you press **Apply**, and the file count is **re-verified** afterwards.
  Big applies show a **live progress bar with a Stop button** and a summary of
  **where everything went**. You can also:
  - **Copy instead of move** — keep the originals and place sorted copies.
  - Save options as a **named preset**, and **export/import** presets + rules to
    a file to share your setup.
  - Define your own **custom rules** (extension→folder maps + junk types).
  - **Auto-run on changes** (watch-folder) or **Schedule** a run (every N hours
    or daily at a set time).
  - **Review duplicates visually** — a thumbnail gallery to pick which copy to
    keep in each set.
- **Wedding** — sort a wedding photo dump into **event folders** (Muhurtham,
  Sadhya, Reception…) by grouping photos on their capture time. It detects the
  natural gaps between ceremonies, pre-fills **Kerala wedding function names**,
  and lets you rename each event before filing into numbered folders
  (`01_Muhurtham`, `02_Sadhya`, …). Fully previewed and undoable.
- **FaceFind** — a guest picks a **selfie** (file or camera) and the app scans the
  whole event folder to find **every photo they appear in**, then saves those into
  a personal `FaceFind_<name>/` album (copies — originals untouched) you can zip
  and share via WhatsApp/email. A **Match strictness** slider trades off more
  results vs. only-sure matches. All face matching runs locally.
  - **Guest self-service portal** — turn on sharing and phorg gives you a link
    (works for anyone on the **same Wi-Fi**). Guests open it on their phone, add a
    selfie, and download the photos they're in (including group shots) — no
    scrolling through thousands of images. Sharing is **off by default** and only
    exposes the guest portal (never your other files or organize tools); it stops
    when you close the app.
- **Compress** — shrink images **losslessly** (no quality loss at all): JPEGs
  are slimmed by stripping bulky metadata & embedded thumbnails while the picture
  data stays byte-for-byte identical, and PNGs are re-packed losslessly. You can
  **estimate savings first**, optionally use a **smaller/resize** mode, convert
  **HEIC→JPG** (iPhone photos), and **find visually near-duplicate** images.
  Originals are kept safe in `Originals_Backup/` (one-click **Restore** or
  **Delete backup**), or you can save smaller copies to `Compressed_Images/`.
- **Report** — generate and open the interactive HTML storage report.
- **Safety** — the guarantees, global options, **accessibility** (larger text,
  high-contrast, **accent colour**), and presets/rules backup.
- **Command palette** — press **Ctrl+K** to jump to any tab or action instantly.
- **Recipes** — save a chain of actions (e.g. *junk → duplicates → by type*) as a
  named workflow and run it in one click.
- **Keyboard shortcuts** — **1–7** jump between tabs, **Ctrl+K** command palette,
  **Ctrl+Z**/**Ctrl+Y** undo & redo, **Enter** runs the primary action, and **A**
  selects every photo in the review grid.
- **Undo *any* past step** — not just the latest; every entry in the activity
  log has its own Undo (copies are undone too).
- **Pick which folders to include** — the preview lists the source folders
  involved so you can **untick any you'd rather leave alone** before applying.
- **Partial undo** — after organizing, move back just **one or two folders**
  (e.g. undo only the `Images` folder) instead of the whole operation.
- **Smart merge on move** — if an identical file already exists at the
  destination, it's skipped instead of piling up `_1` copies.

Everything runs locally on your machine — no data leaves your PC, and it still
uses only the Python standard library.

---

## 🖼️ Categorize photos by content (optional AI)
The app can also sort a photo dump by *what's in each picture* — perfect for a
huge wedding/event collection. In **Organize → Categorize Photos (AI)** it files
images into:

- **Named people** — add anyone (Bride, Groom, a friend…), give each a folder of
  2–5 sample photos, and their **solo** shots are filed into a folder per person
- **Portraits_Single / Couples / Group_Photos** — by how many people are in frame
- **Scenery_Others** — decor, venue, objects (no people)
- **Blurry_Shaken** — out-of-focus / motion-blurred shots
- **Duplicates** — near-identical burst frames

It analyses images **100% locally** (nothing is uploaded) and still only *moves*
files after you preview the plan. This feature needs a few extra packages:

```powershell
pip install -r requirements-vision.txt
```

The app has an **“Install AI support”** button that does this for you (when run
from source). Runs on the local backend (copy phone photos to your PC first —
when you're connected to a phone, the Photo AI tab has a one-click **“Copy phone
photos to PC”** button that pulls them over and switches to that folder).

**Photo AI extras:**
- **Thumbnail review grid** — after analysis, see every photo grouped by category
  with a dropdown to re-file or skip any one before applying. **Tick photos** to
  move them in bulk, use a group's “set all”, or **click any photo** for a
  full-size **lightbox** with details (sharpness, faces, resolution, matched person).
- **Auto-group by face** — no samples needed: finds recurring faces automatically
  and lets you name each person (one-time ~37 MB face-model download). You can
  **merge** people the model split apart, and if you've added sample folders it
  **suggests names** for matching groups automatically. Open a group's
  **Preview all** to flip through its photos and **click ✕ to drop any photo that
  isn't that person** so it won't be filed into their folder.
- **Keep-best duplicates** — within a burst of near-identical shots it keeps the
  sharpest/highest-resolution frame and only files the rest under `Duplicates/`.
- **Similar-shot grouping** — an optional looser pass that groups same-moment
  shots and keeps the best of each into `Similar_Extras/`.
- **Keep-best shots** — an optional **Best_Shots ⭐** bucket that surfaces the
  sharpest, best-exposed keepers (quality score shown in the lightbox).
- **Videos 🎬** — optionally include video files, with real thumbnails (first
  frame) filed under `Videos/`.
- **Screenshots & documents** — optional buckets for screen captures and photos
  of paper/receipts/whiteboards.
- **Filter** the review grid by file name; open a **lightbox** to inspect details.
- **Presets** — save the categories, people, thresholds &amp; protected folders as a
  named preset (stored in your browser) and re-apply it in one click.
- **Fast re-runs** — results are **cached locally** (`.phorg/analysis_cache.json`),
  so re-analyzing the same folder is near-instant.
- **Fine-tuning sliders** — adjust blur sensitivity and face-match strictness.
- **Test on 20 photos** — dry-run a quick sample to check your settings first.
- **Sort Photos by Date** (Organize tab) — file photos/videos into
  `YYYY / YYYY-MM` folders using their EXIF capture date (falls back to file date).
- **Sort by Location** (Organize tab) — group geotagged photos into `Places/`
  folders by their GPS coordinates from EXIF.

### 🧠 More AI features (all local, all previewed)
- **Content types** — sort people-free photos by *what's in them*: **Animals**,
  **Food**, **Nature/Scenery**, **Plants & Flowers**, **Vehicles**, using a small
  offline MobileNet classifier (downloaded once, ~14 MB). Categories are grouped
  into **Wedding** and **Other** sets with a toggle to select a whole set at once.
- **Remembered people (face database)** — name someone once in *Auto-group* and
  phorg **remembers their face forever** (`~/.phorg/people.json`). Next time it
  auto-suggests the name, and a **“Auto-sort my remembered people”** toggle files
  their photos into per-person folders on any Analyze run — no samples needed.
  You can **back up / restore** the database and ask **“Who's in a photo?”** to
  name everyone in a single picture.
- **People groups** — define groups like **Family / School / College / Home** from
  a folder of member faces; any photo containing a member is filed into that group.
- **Auto-discover social groups** — no input needed: phorg finds circles of people
  who appear **together** and lets you name each once (family, a friend group, a
  class), then files their photos.
- **AI wedding sorting** — a smart mode that detects the **couple**, labels events
  by content (Group Photos / Couple Portraits / Portraits), splits by **venue
  (GPS)**, and picks the **sharpest cover** for each event.
- **Near-duplicate detection for photos *and* videos** — a robust DCT **pHash** +
  gradient **dHash** finder (cached for instant re-scans) that also catches
  re-encoded video copies; the review gallery keeps the sharpest/highest-res copy.

### 🗓️ Memories & 🧹 cleanup
- **Memories / trips** — group any photo dump into **events** by capture time
  **and** GPS location; a new memory starts after a long gap or a place change.
  Name each and file it.
- **Cull blurry & duplicates → Recycle Bin** — on the Dashboard's *reclaim* panel,
  scan for blurry shots + near-duplicate extras, review them in a grid, and send
  the rest to the **OS Recycle Bin** (recoverable) in one click.
- **Junk → Recycle Bin** — Junk Sweep can send junk straight to the Recycle Bin
  (native, no dependency) instead of a review folder.
- **Video thumbnails** — galleries show a real **middle-frame** thumbnail (with a
  ▶ badge), not a black first frame.

## ↩️ Undo & redo
Every apply is journalled into a history stack, so you can **Undo** recent
operations (most-recent-first) from the Dashboard's *Recent activity* panel or
the button shown right after applying — files go back exactly where they were.
You can also **Redo** an operation you just undid (Dashboard button or
**Ctrl+Y**).

---

## Safety model (read this first)
- **Dry-run by default.** Every command shows a plan and changes *nothing*
  until you add `--apply`.
- **File-count verification.** After applying, it recounts files and confirms
  the total is unchanged (proves no data was lost).
- **Protected paths** are never entered: `Android`, `MIUI`, `Ringtones`,
  system dirs, any hidden `.*` folder, and `Sent`/`Private`. Add more with
  `--protect NAME`.

---

## Commands

| Command | What it does |
|---|---|
| `ui` | Launch the friendly web interface (folder picker, previews, one-click actions). |
| `scan` | Inventory the tree: per-folder size / file-count / sub-count. |
| `junk` | Move temp / zero-byte / cache / `.tmp` files into `Junk_Files_Review/`. |
| `organize --mode type` | Sort files into `Images/ Videos/ Audio/ Documents/{PDFs,Office,Other} Archives/ Installers/ …`. |
| `organize --mode size` | Sort into size tiers (`1_Huge_over_50MB` … `5_Tiny_under_1MB`). |
| `fix-ext` | Detect real file type from **magic bytes** and repair broken/missing extensions. Add `--sort` to also file them by type. |
| `empties` | Remove recursively-empty folders (skips protected/hidden). |
| `report` | Generate the interactive **HTML storage report**. |
| `verify` | Print the total file count (run before/after any external change). |

The web UI also offers, in **Organize**: **Find Duplicate Files** (byte-for-byte
duplicates anywhere in the tree → a review folder) and **Flatten Folders**
(pull files out of every sub-folder into one place), plus a **Dashboard storage
treemap** and a **light/dark theme** toggle.

---

## Examples

**Preview what a junk sweep would do on your Downloads folder:**
```powershell
python -m phorg --backend local --root "C:\Users\me\Downloads" junk
```

**Actually organize a local folder by type (with confirmation):**
```powershell
python -m phorg --backend local --root "D:\Dump" organize --mode type --apply
```

**Scan a connected phone:**
```powershell
python -m phorg --backend adb scan
```

**Size-sort a phone's video folder (auto-confirm):**
```powershell
python -m phorg --backend adb --root /storage/emulated/0/DCIM/Camera/Videos `
    organize --mode size --apply --yes
```

**Fix broken extensions in a WhatsApp docs folder and file them by type:**
```powershell
python -m phorg --backend adb --root /storage/emulated/0/WhatsApp_Media/Documents `
    fix-ext --sort --recursive --apply
```

**Remove empty folders on the phone:**
```powershell
python -m phorg --backend adb empties --apply
```

**Generate the HTML report (phone), writing to two places:**
```powershell
python -m phorg --backend adb report `
    --out "$env:USERPROFILE\Desktop\phone_storage_report.html" `
    --out ".\report.html"
```

---

## How it maps to the manual reorg
| Manual phase | Command |
|---|---|
| Junk cleanup | `junk` |
| Type detection for broken files | `fix-ext` |
| Category reorg (images/docs/…) | `organize --mode type` |
| Size-tier sorting (videos, images) | `organize --mode size` |
| Empty-folder cleanup | `empties` |
| Interactive HTML report | `report` |

Content-based photo renaming (looking *inside* images) is intentionally **not**
included — that step needs a vision model and was done manually.

---

## Project layout
```
phone_organizer/
├── phorg/
│   ├── __main__.py        # entry point (python -m phorg)
│   ├── cli.py             # argparse CLI + dry-run/apply/verify flow
│   ├── server.py          # zero-dependency web server for the UI
│   ├── ui.html            # the web app (single-page interface)
│   ├── backends.py        # LocalBackend + AdbBackend (Op batch model)
│   ├── safety.py          # protected-path policy
│   ├── classify.py        # type/size/junk rules + magic-byte detection
│   ├── organizer.py       # planners: junk / type / size / fix-ext / empties / photos
│   ├── vision.py          # optional AI image analysis (blur/faces/content/dupes)
│   ├── peopledb.py        # persistent face database (remembered people)
│   ├── hashcache.py       # per-file SHA-1 / perceptual-hash cache
│   ├── report.py          # HTML report builder
│   └── report_template.html
├── tests/                 # pytest regression suite (python -m pytest -q)
├── phorg.bat              # Windows launcher (CLI)
├── phorg-ui.bat           # Windows launcher (web UI)
└── README.md
```

## Testing
A pytest suite covers the core logic (safety rules, planners, dedupe/flatten,
hash cache, people database, vision helpers). Run it with:

```powershell
python -m pytest -q
```
