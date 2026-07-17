"""
FaceFind web UI — a zero-dependency local server.

Serves the FaceFind single-page interface (``ui.html``) and a small JSON API:
point it at an event folder, add a selfie, and it finds every photo a person
appears in — then copies them into a personal album.  An optional guest portal
lets people scan a QR/link and find "photos of me" from their own phone.

Uses only the Python standard library (plus the optional vision packages the
face matcher needs) so the app keeps its "nothing extra to install" promise.

The face-matching logic itself lives untouched in ``vision.py``.
"""
import os
import sys
import json
import time
import uuid
import string
import socket
import base64
import tempfile
import posixpath
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .backends import LocalBackend
from .safety import SafetyPolicy
from . import registrations

# Best-effort HEIC/HEIF support so iPhone selfies/photos preview & match.
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass


HERE = os.path.dirname(__file__)


def _resource(name):
    """Locate a bundled data file, whether running from source or a frozen
    (PyInstaller) executable."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cand = os.path.join(base, "phorg", name)
        if os.path.exists(cand):
            return cand
        cand = os.path.join(base, name)
        if os.path.exists(cand):
            return cand
    return os.path.join(HERE, name)


# Folders allowed for thumbnails / downloads (the event root + the selfie temp).
_ALLOWED_ROOTS = set()


def _register_root(root):
    try:
        _ALLOWED_ROOTS.add(os.path.abspath(root))
    except Exception:
        pass


def _under_allowed(path):
    ap = os.path.abspath(path)
    for root in _ALLOWED_ROOTS:
        try:
            if os.path.commonpath([ap, root]) == root:
                return True
        except ValueError:
            continue
    return False


# --------------------------------------------------------------------------
# Guest sharing ("FaceFind portal") — let people scan a link, submit a selfie,
# and get their own photos.  Off by default.
# --------------------------------------------------------------------------
_SHARE = {"enabled": False, "root": None, "event": "", "token": None,
          "guests": [], "online": False, "public_url": None,
          "public_host": None, "threshold": 0.45, "pin": None, "expires": 0}
_SHARE_LOCK = threading.Lock()
_SERVER_PORT = 8765

# Remembered settings from the last time sharing was enabled (event name, folder,
# online/pin choices) so re-enabling after a restart is one click.
_SHARE_CFG = os.path.join(os.path.expanduser("~"), ".phorg", "share_last.json")

# Very small per-IP rate limiter for the public guest endpoints.
_RATE = {}
_RATE_LOCK = threading.Lock()

# Endpoints a non-local (guest) visitor is allowed to call.  Everything else
# stays host-only even while sharing is on.
_GUEST_ROUTES = {
    "/api/health", "/api/share/status", "/api/share/find/start",
    "/api/facefind/selfie", "/api/cluster/progress", "/api/cluster/cancel",
    "/api/share/album",
}


def _share_expired():
    exp = _SHARE.get("expires") or 0
    return bool(exp) and time.time() > exp


def _persist_share_cfg(cfg):
    try:
        os.makedirs(os.path.dirname(_SHARE_CFG), exist_ok=True)
        with open(_SHARE_CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except OSError:
        pass


def _load_share_cfg():
    try:
        with open(_SHARE_CFG, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _rate_ok(ip, limit=25, window=60):
    """True if *ip* is under *limit* requests in the last *window* seconds."""
    now = time.time()
    with _RATE_LOCK:
        arr = [t for t in _RATE.get(ip, ()) if now - t < window]
        if len(arr) >= limit:
            _RATE[ip] = arr
            return False
        arr.append(now)
        _RATE[ip] = arr
        if len(_RATE) > 500:                      # keep the table bounded
            for k in list(_RATE.keys())[:200]:
                _RATE.pop(k, None)
        return True


def _lan_ips():
    primary = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        primary = s.getsockname()[0]
        s.close()
        if primary and primary.startswith("127."):
            primary = None
    except Exception:
        primary = None
    others = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip != primary \
                    and ip not in others:
                others.append(ip)
    except Exception:
        pass

    def rank(ip):
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        return 3          # 172.x is often a virtual WSL/Hyper-V adapter
    others.sort(key=rank)
    # the default-route interface (what guests on the same Wi-Fi actually use)
    return ([primary] if primary else []) + others


def _is_private_host(host):
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_link_local
    except ValueError:
        return False


def _share_urls(token):
    urls = [f"http://{ip}:{_SERVER_PORT}/?g={token}" for ip in _lan_ips()]
    with _SHARE_LOCK:
        pub = _SHARE.get("public_url") if _SHARE.get("online") else None
    if pub:
        urls.insert(0, f"{pub}/?g={token}")
    return urls


def _guest_can_access(fp, is_local):
    """Whether a request may read *fp*: host can read any allowed root; a guest
    may only read files under the shared event folder."""
    if is_local:
        return _under_allowed(fp)
    with _SHARE_LOCK:
        root = _SHARE["root"] if _SHARE["enabled"] else None
    if not root:
        return False
    try:
        return os.path.commonpath([os.path.abspath(fp), root]) == root
    except ValueError:
        return False


# --------------------------------------------------------------------------
# Helpers to build backends / safety from a plain params dict (from the UI)
# --------------------------------------------------------------------------
def _build_backend(p):
    root = p.get("root")
    if not root:
        raise ValueError("Please choose an event folder.")
    if not os.path.isdir(root):
        raise ValueError(f"Folder not found: {root}")
    return LocalBackend(root)


def _build_safety(p):
    protect = p.get("protect") or []
    if isinstance(protect, str):
        protect = [x.strip() for x in protect.split(",") if x.strip()]
    return SafetyPolicy(include_hidden=bool(p.get("include_hidden")),
                        extra_protect=protect)


def _count_files(be, root):
    return sum(1 for _ in be.iter_files(root))


# --------------------------------------------------------------------------
# API actions
# --------------------------------------------------------------------------
def api_health(_):
    return {"ok": True, "app": "facefind", "version": 1}


def api_browse(p):
    """List sub-directories for the local folder picker."""
    path = p.get("path") or ""
    # Windows: empty path -> list drive letters
    if not path:
        if os.name == "nt":
            drives = []
            for letter in string.ascii_uppercase:
                d = f"{letter}:\\"
                if os.path.exists(d):
                    drives.append({"name": d, "path": d})
            home = os.path.expanduser("~")
            return {"path": "", "parent": None, "drives": drives,
                    "home": home, "dirs": []}
        path = "/"
    path = os.path.abspath(path)
    dirs = []
    try:
        for name in sorted(os.listdir(path), key=str.lower):
            full = os.path.join(path, name)
            if os.path.isdir(full) and not name.startswith("."):
                dirs.append({"name": name, "path": full})
    except OSError as e:
        raise ValueError(f"Cannot open {path}: {e}")
    parent = os.path.dirname(path)
    if parent == path:  # at a drive/filesystem root
        parent = "" if os.name == "nt" else None
    return {"path": path, "parent": parent, "drives": [],
            "home": os.path.expanduser("~"), "dirs": dirs}


def api_scan(p):
    """Inventory the event folder so the UI can show a file count."""
    be = _build_backend(p)
    depth = int(p.get("depth") or 3)
    tree, meta = be.scan(be.root, max_depth=depth)
    tree = sorted(tree, key=lambda n: -n["kb"])
    if be.name == "local":
        _register_root(be.root)
    return {"backend": be.name, "root": be.root, "tree": tree, "meta": meta}


def api_open(p):
    """Open a folder (the saved album) in the OS default file manager."""
    path = p.get("path")
    reveal = bool(p.get("reveal"))
    if not path or not os.path.exists(path):
        raise ValueError("File not found.")
    try:
        if reveal and os.path.isfile(path):
            import subprocess
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        elif os.name == "nt":
            os.startfile(path)  # noqa: S606 - user-initiated, local file
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        raise ValueError(f"Could not open file: {e}")
    return {"opened": path}


# --------------------------------------------------------------------------
# Background jobs (face matching + album copy)
# --------------------------------------------------------------------------
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_PREWARM = {}          # abspath(root) -> jobId of an in-flight background index
_PREWARM_LOCK = threading.Lock()


def api_vision_status(_p):
    from . import vision
    missing = vision.check_deps()
    return {
        "available": not missing,
        "missing": missing,
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def api_vision_install(_p):
    """Install the optional face-recognition packages via pip (source only)."""
    if getattr(sys, "frozen", False):
        raise ValueError("Automatic install isn't available in the packaged app. "
                         "Run FaceFind from source to install AI support.")
    import subprocess
    pkgs = ["numpy", "opencv-contrib-python", "Pillow"]
    try:
        proc = subprocess.run([sys.executable, "-m", "pip", "install", *pkgs],
                              capture_output=True, text=True)
    except Exception as e:
        raise ValueError(f"Could not run pip: {e}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-600:]
        raise ValueError(f"pip install failed:\n{tail}")
    from . import vision
    missing = vision.check_deps()
    return {"installed": True, "missing": missing}


def api_cluster_status(_p):
    """Whether the face tools needed by FaceFind are available."""
    from . import vision
    missing = vision.check_deps()
    return {
        "available": (not missing) and vision.cluster_api_available(),
        "deps_missing": missing,
        "api": (not missing) and vision.cluster_api_available(),
        "models": (not missing) and vision.models_present(),
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def api_cluster_progress(p):
    """Poll a running FaceFind job."""
    with _JOBS_LOCK:
        job = _JOBS.get(p.get("jobId"))
    if not job:
        raise ValueError("Search job not found (it may have expired).")
    out = {k: job[k] for k in ("done", "total", "phase", "clusters",
                               "finished", "error", "result",
                               "model_done", "model_total", "model_name")}
    out["preview"] = list(job.get("preview") or [])
    return out


def api_cluster_cancel(p):
    with _JOBS_LOCK:
        job = _JOBS.get(p.get("jobId"))
    if job:
        job["cancel"] = True
    return {"ok": True}


def _moved_summary(be, journal):
    """Group applied copies by destination folder (relative to root)."""
    from collections import defaultdict
    counts = defaultdict(int)
    for j in journal:
        if j.get("op") not in ("move", "copy"):
            continue
        dst = j.get("dst") or ""
        rel = be.relpath(posixpath.dirname(dst)) if hasattr(be, "relpath") else ""
        rel = (rel or "").replace("\\", "/").strip("/") or "(root)"
        counts[rel] += 1
    items = sorted(counts.items(), key=lambda kv: -kv[1])[:12]
    return [{"folder": f, "count": c} for f, c in items]


def _build_photo_ops(p):
    """Build (backend, ops, skipped) from photo->album assignments."""
    be = _build_backend(p)
    root = os.path.abspath(be.root)
    assignments = p.get("assignments") or []
    from .backends import Op
    ops = []
    made = set()
    skipped = 0
    for a in assignments:
        path = a.get("path")
        cat = a.get("category")
        if not path or not cat:
            continue
        if not _under_allowed(path) or not os.path.isfile(path):
            skipped += 1
            continue
        name = os.path.basename(path)
        destdir = be.join(root.replace(os.sep, "/"), *cat.split("/"))
        if os.path.abspath(os.path.dirname(path)) == os.path.abspath(
                be.native(destdir)):
            continue
        if destdir not in made:
            ops.append(Op("mkdir", destdir)); made.add(destdir)
        ops.append(Op("move", path.replace(os.sep, "/"), be.join(destdir, name)))
    return be, ops, skipped


def _start_apply_job(be, ops, label, copy_mode=False):
    """Run an op-batch in a background thread with progress + cancel."""
    job_id = uuid.uuid4().hex
    job = {"done": 0, "total": len(ops), "phase": "applying",
           "finished": False, "error": None, "result": None, "cancel": False}
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        for k in list(_JOBS.keys())[:-20]:
            _JOBS.pop(k, None)

    def run():
        try:
            before = _count_files(be, be.root)
            journal = []
            log = []

            def prog(d, t):
                job["done"] = d
                job["total"] = t

            def cancelled():
                return job["cancel"]

            done = be.apply_ops(ops, log=log.append, journal=journal,
                                progress=prog, cancel=cancelled,
                                copy_mode=copy_mode)
            after = _count_files(be, be.root)
            copies = sum(1 for j in journal if j.get("op") == "copy")
            if copy_mode:
                verified = (after == before + copies)
            else:
                verified = (before == after)
            job["result"] = {
                "applied": done, "before": before, "after": after,
                "verified": verified, "log": log, "cancelled": job["cancel"],
                "merged": getattr(be, "merged_skips", 0),
                "copied": copies, "copyMode": copy_mode,
                "movedSummary": _moved_summary(be, journal),
                "undo": {"available": False, "count": 0, "root": be.root}}
            job["phase"] = "done"
        except Exception as e:  # pragma: no cover - defensive
            job["error"] = f"{type(e).__name__}: {e}"
            job["phase"] = "error"
        finally:
            job["finished"] = True

    threading.Thread(target=run, daemon=True).start()
    return {"jobId": job_id}


def api_categorize_apply_start(p):
    """Copy the chosen matches into the guest's album folder (background job)."""
    if (p.get("backend") or "local").lower() != "local":
        raise ValueError("FaceFind works on local folders only.")
    be, ops, skipped = _build_photo_ops(p)
    if not ops:
        job_id = uuid.uuid4().hex
        with _JOBS_LOCK:
            _JOBS[job_id] = {"done": 0, "total": 0, "phase": "done",
                             "finished": True, "error": None, "cancel": False,
                             "result": {"applied": 0, "verified": True,
                                        "skipped": skipped,
                                        "undo": {"available": False}}}
        return {"jobId": job_id}
    return _start_apply_job(be, ops, "album", copy_mode=bool(p.get("copy")))


def api_apply_progress(p):
    with _JOBS_LOCK:
        job = _JOBS.get(p.get("jobId"))
    if not job:
        raise ValueError("Apply job not found (it may have expired).")
    return {k: job.get(k) for k in ("done", "total", "phase", "finished",
                                    "error", "result")}


def api_apply_cancel(p):
    with _JOBS_LOCK:
        job = _JOBS.get(p.get("jobId"))
    if job:
        job["cancel"] = True
    return {"ok": True}


# ==========================================================================
# FaceFind — find every photo a guest appears in, from one selfie
# ==========================================================================
def api_facefind_selfie(p):
    """Save an uploaded selfie (data URL) to a temp file; return its path."""
    data = p.get("data") or ""
    if "," in data:
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data)
    except Exception:
        raise ValueError("That selfie image couldn't be read.")
    if not raw or len(raw) > 25 * 1024 * 1024:
        raise ValueError("Selfie is missing or too large.")
    d = os.path.join(tempfile.gettempdir(), "phorg_facefind")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, uuid.uuid4().hex + ".png")
    with open(path, "wb") as f:
        f.write(raw)
    _register_root(d)      # allow the thumbnail endpoint to preview it
    return {"path": path}


def _selfie_list(p):
    """Collect valid on-disk selfie paths from a request (single or multiple)."""
    raw = p.get("selfies") if p.get("selfies") else [p.get("selfie")]
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    return [s for s in raw if s and os.path.isfile(s)][:5]


def _start_facefind_job(root, selfie, threshold, recursive=True, on_done=None):
    """Start a background FaceFind job over *root*; returns {'jobId'}.

    *selfie* may be a single path or a list of selfie paths (averaged).
    *on_done*, if given, is called with the finished job dict (success or
    error) so callers can persist the result.
    """
    from . import vision
    from .backends import LocalBackend
    be = LocalBackend(root)
    saf = _build_safety({})
    _register_root(be.root)
    skip_top = {"Compressed_Images", "Originals_Backup"}
    native = []
    for fp in be.iter_files(be.root):
        name = posixpath.basename(fp)
        rel = be.relpath(posixpath.dirname(fp)).replace("\\", "/")
        segs = [s for s in rel.split("/") if s and s != "."]
        if segs and (segs[0] in skip_top or segs[0].startswith("FaceFind_")
                     or saf.is_protected_dir("/".join(segs))):
            continue
        if not recursive and segs:
            continue
        if saf.is_protected_file(name) or not vision.is_image(name):
            continue
        native.append(be.native(fp))

    job_id = uuid.uuid4().hex
    job = {"done": 0, "total": len(native), "phase": "starting", "clusters": 0,
           "finished": False, "error": None, "result": None, "cancel": False,
           "model_done": 0, "model_total": 0, "model_name": "", "preview": []}
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        for k in list(_JOBS.keys())[:-20]:
            _JOBS.pop(k, None)

    def run():
        try:
            if not vision.models_present():
                job["phase"] = "downloading"

                def mprog(name, got, total):
                    job["model_name"] = name
                    job["model_done"] = got
                    job["model_total"] = total
                vision.download_models(progress=mprog)
            job["phase"] = "matching"

            def prog(done, total, n):
                job["done"] = done
                job["total"] = total
                job["clusters"] = n

            def cancelled():
                return job["cancel"]

            def on_match(m):
                # Keep a bounded, highest-first preview for live streaming.
                pv = job["preview"]
                pv.append(m)
                if len(pv) > 60:
                    pv.sort(key=lambda x: -x["score"])
                    del pv[60:]

            res = vision.facefind(native, selfie, threshold=threshold,
                                  progress=prog, cancel=cancelled,
                                  on_match=on_match,
                                  cache_dir=os.path.join(os.path.abspath(be.root),
                                                         ".phorg"))
            res["scanned"] = len(native)
            res["cancelled"] = job["cancel"]
            res["root"] = be.root
            job["result"] = res
            job["phase"] = "done"
        except Exception as e:  # pragma: no cover - defensive
            job["error"] = f"{type(e).__name__}: {e}"
            job["phase"] = "error"
        finally:
            job["finished"] = True
            if on_done:
                try:
                    on_done(job)
                except Exception:
                    pass

    threading.Thread(target=run, daemon=True).start()
    return {"jobId": job_id}


def api_facefind_start(p):
    if (p.get("backend") or "local").lower() != "local":
        raise ValueError("FaceFind works on local folders only.")
    from . import vision
    missing = vision.check_deps()
    if missing:
        raise ValueError("Missing packages: " + ", ".join(missing))
    if not vision.cluster_api_available():
        raise ValueError("Your OpenCV build lacks the face modules needed "
                         "(needs opencv-contrib-python).")
    selfies = _selfie_list(p)
    if not selfies:
        raise ValueError("Please add a clear selfie photo first.")
    be = _build_backend(p)
    return _start_facefind_job(be.root, selfies, float(p.get("threshold") or 0.44),
                               bool(p.get("recursive", True)))


def _gather_event_images(root, recursive=True):
    """Return (backend, [native image paths]) for an event folder, applying the
    same skips FaceFind uses (backup/album/protected folders, non-images)."""
    from . import vision
    from .backends import LocalBackend
    be = LocalBackend(root)
    saf = _build_safety({})
    skip_top = {"Compressed_Images", "Originals_Backup"}
    native = []
    for fp in be.iter_files(be.root):
        name = posixpath.basename(fp)
        rel = be.relpath(posixpath.dirname(fp)).replace("\\", "/")
        segs = [s for s in rel.split("/") if s and s != "."]
        if segs and (segs[0] in skip_top or segs[0].startswith("FaceFind_")
                     or saf.is_protected_dir("/".join(segs))):
            continue
        if not recursive and segs:
            continue
        if saf.is_protected_file(name) or not vision.is_image(name):
            continue
        native.append(be.native(fp))
    return be, native


def api_facefind_prewarm(p):
    """Kick off a background pass that embeds & caches every face in the event
    folder, so the first real search is fast instead of slow.  Safe to call on
    every folder open: it de-dupes per folder and skips already-cached photos."""
    if (p.get("backend") or "local").lower() != "local":
        return {"ok": False}
    root = p.get("root")
    if not root or not os.path.isdir(root):
        return {"ok": False}
    from . import vision
    if vision.check_deps() or not vision.cluster_api_available():
        return {"ok": False, "reason": "deps"}
    ap = os.path.abspath(root)
    # If an index for this folder is already running, reuse it.
    with _PREWARM_LOCK:
        existing = _PREWARM.get(ap)
    if existing:
        with _JOBS_LOCK:
            job = _JOBS.get(existing)
        if job and not job.get("finished"):
            return {"ok": True, "jobId": existing, "total": job.get("total", 0)}

    be, native = _gather_event_images(ap, True)
    _register_root(ap)

    job_id = uuid.uuid4().hex
    job = {"done": 0, "total": len(native), "phase": "indexing", "clusters": 0,
           "finished": False, "error": None, "result": None, "cancel": False,
           "model_done": 0, "model_total": 0, "model_name": ""}
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        for k in list(_JOBS.keys())[:-20]:
            _JOBS.pop(k, None)
    with _PREWARM_LOCK:
        _PREWARM[ap] = job_id

    def run():
        try:
            if not vision.models_present():
                job["phase"] = "downloading"

                def mprog(name, got, total):
                    job["model_name"] = name
                    job["model_done"] = got
                    job["model_total"] = total
                vision.download_models(progress=mprog)
            job["phase"] = "indexing"

            def prog(done, total, n):
                job["done"] = done
                job["total"] = total
                job["clusters"] = n

            def cancelled():
                return job["cancel"]

            res = vision.index_faces(
                native, progress=prog, cancel=cancelled,
                cache_dir=os.path.join(ap, ".phorg"))
            job["result"] = res
            job["phase"] = "done"
        except Exception as e:  # pragma: no cover - defensive
            job["error"] = f"{type(e).__name__}: {e}"
            job["phase"] = "error"
        finally:
            job["finished"] = True
            with _PREWARM_LOCK:
                if _PREWARM.get(ap) == job_id:
                    _PREWARM.pop(ap, None)

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "jobId": job_id, "total": len(native)}


# ==========================================================================
# Guest sharing endpoints
# ==========================================================================
def api_share_enable(p):
    root = p.get("root")
    if not root or not os.path.isdir(root):
        raise ValueError("Choose the event folder to share first.")
    from . import vision
    if vision.check_deps() or not vision.cluster_api_available():
        raise ValueError("Face matching needs the AI tools installed "
                         "(click 'Install AI support' once).")
    _register_root(root)
    online = bool(p.get("online"))
    public_url = public_host = None
    if online:
        from . import tunnel
        info = tunnel.start(_SERVER_PORT)     # raises on failure
        public_url = info["url"]
        public_host = info["host"]
    token = uuid.uuid4().hex[:10]
    try:
        thr = float(p.get("threshold"))
    except (TypeError, ValueError):
        thr = 0.45
    thr = min(0.60, max(0.30, thr))
    pin = (str(p.get("pin") or "").strip())[:12] or None
    try:
        exp_min = int(p.get("expiry_minutes") or 0)
    except (TypeError, ValueError):
        exp_min = 0
    expires = int(time.time()) + exp_min * 60 if exp_min > 0 else 0
    with _SHARE_LOCK:
        _SHARE.update({"enabled": True, "root": os.path.abspath(root),
                       "event": (p.get("event") or "Our Event").strip()[:80],
                       "token": token, "guests": [], "online": online,
                       "public_url": public_url, "public_host": public_host,
                       "threshold": thr, "pin": pin, "expires": expires})
        event = _SHARE["event"]
    _persist_share_cfg({"root": os.path.abspath(root), "event": event,
                        "online": online, "pin": pin or "",
                        "expiry_minutes": exp_min})
    ips = _lan_ips()
    return {"ok": True, "token": token, "event": event, "port": _SERVER_PORT,
            "ips": ips, "urls": _share_urls(token), "online": online,
            "public_url": public_url, "pin": pin or "", "expires": expires}


def api_share_disable(_p):
    with _SHARE_LOCK:
        was_online = _SHARE.get("online")
        _SHARE.update({"enabled": False, "root": None, "event": "",
                       "token": None, "guests": [], "online": False,
                       "public_url": None, "public_host": None,
                       "threshold": 0.45, "pin": None, "expires": 0})
    if was_online:
        try:
            from . import tunnel
            tunnel.stop()
        except Exception:
            pass
    return {"ok": True, "enabled": False}


def api_share_threshold(p):
    """Host-only: adjust how strict guest face matching is (higher = stricter,
    fewer false matches). Applies to every guest search from now on."""
    try:
        thr = float(p.get("threshold"))
    except (TypeError, ValueError):
        raise ValueError("Invalid strictness value.")
    thr = min(0.60, max(0.30, thr))
    with _SHARE_LOCK:
        _SHARE["threshold"] = thr
    return {"ok": True, "threshold": thr}


def api_share_status(p):
    token = p.get("token")
    if _share_expired():
        api_share_disable({})
    with _SHARE_LOCK:
        enabled = _SHARE["enabled"]
        event = _SHARE["event"]
        cur = _SHARE["token"]
        online = _SHARE.get("online")
        public_url = _SHARE.get("public_url")
        threshold = _SHARE.get("threshold", 0.45)
        pin = _SHARE.get("pin")
        expires = _SHARE.get("expires", 0)
    if not enabled:
        return {"enabled": False, "last": _load_share_cfg()}
    if token is not None:                     # a guest checking their link
        if token != cur:
            return {"enabled": False}
        if pin and (str(p.get("pin") or "").strip() != pin):
            return {"enabled": True, "pin_required": True, "event": ""}
        return {"enabled": True, "event": event, "expires": expires}
    ips = _lan_ips()                          # host asking for the link
    return {"enabled": True, "event": event, "token": cur,
            "port": _SERVER_PORT, "ips": ips, "urls": _share_urls(cur),
            "online": bool(online), "public_url": public_url,
            "threshold": threshold, "pin": pin or "", "expires": expires}


def api_share_find_start(p):
    token = p.get("token")
    if _share_expired():
        api_share_disable({})
    with _SHARE_LOCK:
        if not _SHARE["enabled"] or token != _SHARE["token"]:
            raise ValueError("This photo link is no longer active.")
        root = _SHARE["root"]
        event = _SHARE.get("event") or ""
        pin = _SHARE.get("pin")
        thr = _SHARE.get("threshold", 0.45)
    if pin and (str(p.get("pin") or "").strip() != pin):
        raise ValueError("Wrong PIN for this event.")
    selfies = _selfie_list(p)
    if not selfies:
        raise ValueError("Please add a clear selfie first.")
    cid = (p.get("cid") or "").strip()[:64]
    # A guest who reloads or searches again from the same browser replaces their
    # previous search: cancel the old (still-running) job so it doesn't keep
    # burning CPU on the host or linger forever as a stuck "searching" row.
    if cid:
        with _SHARE_LOCK:
            prev_jobs = [g.get("job") for g in (_SHARE.get("guests") or [])
                         if g.get("cid") == cid]
        for jid in prev_jobs:
            with _JOBS_LOCK:
                job = _JOBS.get(jid)
            if job and not job.get("finished"):
                job["cancel"] = True
    name = (p.get("name") or "").strip()[:60] or "Guest"
    contact = (p.get("contact") or "").strip()[:120]
    # Persist the sign-up durably *before* matching so a shutdown never loses it.
    reg_id = uuid.uuid4().hex
    atoken = uuid.uuid4().hex          # per-guest secret for the album link
    durable = registrations.persist_selfies(reg_id, selfies) or selfies
    registrations.add(reg_id, event=event, root=root, token=token, cid=cid,
                      name=name, contact=contact, selfies=durable,
                      threshold=thr, atoken=atoken)

    def _on_done(job, _rid=reg_id, _at=atoken, _contact=contact, _event=event,
                 _name=name):
        if job.get("error"):
            registrations.save_error(_rid, job["error"])
            return
        res = job.get("result") or {}
        registrations.save_results(_rid, res)
        # Async delivery: if the guest left a contact and we found photos, send
        # them their private album link (no-op if no notifier is configured).
        if _contact and (res.get("count") or 0) > 0:
            try:
                from . import notify
                out = notify.send(_contact, _event, _album_url(_rid, _at),
                                  name=_name)
                registrations.set_notified(
                    _rid, "sent" if out.get("sent")
                    else (out.get("reason") or "failed"))
            except Exception:
                registrations.set_notified(_rid, "failed")

    r = _start_facefind_job(root, durable, thr, True, on_done=_on_done)
    registrations.set_job(reg_id, r["jobId"])
    with _SHARE_LOCK:
        guests = _SHARE.setdefault("guests", [])
        if cid:                         # drop this browser's old entry
            guests[:] = [g for g in guests if g.get("cid") != cid]
        guests.append({"name": name, "ts": int(time.time()),
                       "job": r["jobId"], "cid": cid, "reg": reg_id})
        _SHARE["guests"] = guests[-200:]
    return {"jobId": r["jobId"], "rid": reg_id, "atoken": atoken}


def _album_url(rid, atoken):
    """Build the guest's private album link, preferring the public tunnel URL
    and falling back to the LAN address."""
    with _SHARE_LOCK:
        pub = _SHARE.get("public_url") if _SHARE.get("online") else None
    if pub:
        base = pub.rstrip("/")
    else:
        ips = _lan_ips()
        host = ips[0] if ips else "127.0.0.1"
        base = f"http://{host}:{_SERVER_PORT}"
    return f"{base}/?a={rid}.{atoken}"


def api_share_album(p):
    """Guest: fetch a saved album by its signed link (survives app restarts as
    long as the host is sharing)."""
    ref = (p.get("a") or "").strip()
    rid = (p.get("rid") or "").strip()
    tok = (p.get("token") or p.get("atoken") or "").strip()
    if ref and "." in ref and not rid:
        rid, tok = ref.split(".", 1)
    d = registrations.album(rid, tok)
    if not d:
        raise ValueError("This photo link is not valid.")
    return {"event": d.get("event") or "the event", "name": d.get("name"),
            "count": d.get("count"), "status": d.get("status"),
            "matches": d.get("matches") or []}


def api_share_guests(_p):
    """Host-only: who has searched, how many photos each found, and the matched
    photo paths so the host can preview them.  Backed by the durable store, so
    the list survives an app restart (Phase 1 availability)."""
    with _SHARE_LOCK:
        root = _SHARE.get("root") if _SHARE.get("enabled") else None
    if not root:
        root = (_load_share_cfg() or {}).get("root")
    regs = registrations.list_for_root(root) if root else []
    out = []
    for g in regs:
        with _JOBS_LOCK:
            job = _JOBS.get(g.get("job"))
        if job and not job.get("finished"):     # a live, in-flight search
            out.append({"name": g.get("name"), "ts": g.get("created_at"),
                        "count": None, "done": False, "scanned": None,
                        "job": g.get("job"), "matches": [],
                        "contact": g.get("contact"), "notified": None})
        else:
            out.append({"name": g.get("name"), "ts": g.get("created_at"),
                        "count": g.get("count"),
                        "done": g.get("status") in ("matched", "error"),
                        "scanned": g.get("scanned"), "job": g.get("job"),
                        "matches": g.get("matches") or [],
                        "contact": g.get("contact"),
                        "notified": g.get("notified")})
    return {"guests": out, "count": len(out)}


ROUTES = {
    "/api/health": api_health,
    "/api/browse": api_browse,
    "/api/scan": api_scan,
    "/api/open": api_open,
    "/api/vision/status": api_vision_status,
    "/api/vision/install": api_vision_install,
    "/api/cluster/status": api_cluster_status,
    "/api/cluster/progress": api_cluster_progress,
    "/api/cluster/cancel": api_cluster_cancel,
    "/api/facefind/selfie": api_facefind_selfie,
    "/api/facefind/start": api_facefind_start,
    "/api/facefind/prewarm": api_facefind_prewarm,
    "/api/categorize/apply/start": api_categorize_apply_start,
    "/api/apply/progress": api_apply_progress,
    "/api/apply/cancel": api_apply_cancel,
    "/api/share/enable": api_share_enable,
    "/api/share/disable": api_share_disable,
    "/api/share/status": api_share_status,
    "/api/share/threshold": api_share_threshold,
    "/api/share/find/start": api_share_find_start,
    "/api/share/album": api_share_album,
    "/api/share/guests": api_share_guests,
}


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "facefind/1.0"

    # Only accept requests addressed to the local loopback host.  This blocks
    # DNS-rebinding attacks where a malicious website resolves its domain to
    # 127.0.0.1 and tries to drive this API from the browser.  When
    # guest-sharing is ON, LAN (private-IP) visitors are also allowed — but only
    # for the small guest whitelist (see do_POST / do_GET).
    def _is_local_req(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in {"127.0.0.1", "localhost", "::1"}

    def _host_ok(self):
        if self._is_local_req():
            return True
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        with _SHARE_LOCK:
            shared = _SHARE["enabled"]
            pub_host = _SHARE.get("public_host") if _SHARE.get("online") else None
        if not shared:
            return False
        if pub_host and host == pub_host:      # our own online tunnel
            return True
        return _is_private_host(host)

    def log_message(self, *_):  # keep the console clean
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            self._send(403, b"Forbidden", "text/plain")
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(_resource("ui.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"UI file missing", "text/plain")
            return
        if path == "/qrcode.min.js":
            try:
                with open(_resource("qrcode.min.js"), "rb") as f:
                    self._send(200, f.read(), "application/javascript; charset=utf-8")
            except OSError:
                self._send(404, b"", "text/plain")
            return
        if path == "/logo.png":
            try:
                with open(_resource("logo.png"), "rb") as f:
                    self._send(200, f.read(), "image/png")
            except OSError:
                self._send(404, b"", "text/plain")
            return
        if path == "/api/health":
            self._send(200, api_health({}))
            return
        if path == "/api/thumb":
            self._serve_thumb()
            return
        if path == "/api/download":
            self._serve_download()
            return
        if path == "/api/download/zip":
            self._serve_zip()
            return
        self._send(404, {"error": "Not found"})

    def _serve_thumb(self):
        from urllib.parse import urlparse, parse_qs, unquote
        qs = parse_qs(urlparse(self.path).query)
        fp = unquote((qs.get("path") or [""])[0])
        try:
            size = max(64, min(1400, int((qs.get("size") or ["200"])[0])))
        except ValueError:
            size = 200
        if not fp or not _guest_can_access(fp, self._is_local_req()) \
                or not os.path.isfile(fp):
            self._send(404, {"error": "Not found"})
            return
        ext = fp.rsplit(".", 1)[-1].lower() if "." in fp else ""
        video_exts = {"mp4", "mkv", "avi", "mov", "3gp", "webm", "m4v",
                      "flv", "wmv", "mpg", "mpeg"}
        try:
            from PIL import Image
            import io
            if ext in video_exts:
                import cv2
                cap = cv2.VideoCapture(fp)
                # seek to the middle — the first frame is often black
                try:
                    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                    if n > 1:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, n // 2)
                except Exception:
                    pass
                ok, frame = cap.read()
                if (not ok or frame is None):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = cap.read()
                cap.release()
                if not ok or frame is None:
                    self._send(404, {"error": "no frame"})
                    return
                frame = frame[:, :, ::-1]  # BGR -> RGB
                im = Image.fromarray(frame)
            else:
                im = Image.open(fp)
                im.draft("RGB", (size, size))
            im = im.convert("RGB")
            im.thumbnail((size, size))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82)
            self._send(200, buf.getvalue(), "image/jpeg")
        except Exception:
            self._send(404, {"error": "Cannot render"})

    def _serve_download(self):
        from urllib.parse import urlparse, parse_qs, unquote
        qs = parse_qs(urlparse(self.path).query)
        fp = unquote((qs.get("path") or [""])[0])
        if not fp or not _guest_can_access(fp, self._is_local_req()) \
                or not os.path.isfile(fp):
            self._send(404, {"error": "Not found"})
            return
        try:
            with open(fp, "rb") as f:
                data = f.read()
            name = os.path.basename(fp).replace('"', "")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            self._send(404, {"error": "Cannot read"})

    def _serve_zip(self):
        """Zip up all matches from a finished FaceFind job (Download all), or the
        photos of a saved album when an ``a=<rid>.<token>`` link is used."""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        job = (qs.get("job") or [""])[0]
        token = (qs.get("token") or [""])[0]
        ref = (qs.get("a") or [""])[0]
        local = self._is_local_req()
        if ref and "." in ref:
            rid, tok = ref.split(".", 1)
            d = registrations.album(rid, tok)
            if not d:
                self._send(403, {"error": "link inactive"})
                return
            event = d.get("event") or "photos"
            matches = d.get("matches") or []
        else:
            if not local:
                with _SHARE_LOCK:
                    ok = _SHARE["enabled"] and token == _SHARE["token"]
                if not ok:
                    self._send(403, {"error": "link inactive"})
                    return
            with _SHARE_LOCK:
                event = _SHARE["event"] or "photos"
            with _JOBS_LOCK:
                j = _JOBS.get(job)
            matches = ((j or {}).get("result") or {}).get("matches") or []
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            seen = set()
            for m in matches:
                fp = m.get("path")
                if fp and _guest_can_access(fp, local) and os.path.isfile(fp):
                    arc = os.path.basename(fp)
                    if arc in seen:
                        arc = f"{len(seen)}_{arc}"
                    seen.add(arc)
                    try:
                        z.write(fp, arc)
                    except OSError:
                        pass
        data = buf.getvalue()
        safe = "".join(c for c in event if c.isalnum() or c in " _-").strip() or "photos"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{safe}.zip"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self._host_ok():
            self._send(403, {"error": "Forbidden"})
            return
        path = self.path.split("?", 1)[0]
        if not self._is_local_req():
            if path not in _GUEST_ROUTES:
                self._send(403, {"error": "Not available"})
                return
            ip = self.client_address[0] if self.client_address else "?"
            if not _rate_ok(ip):
                self._send(429, {"error": "Too many requests — please wait a moment."})
                return
        fn = ROUTES.get(path)
        if not fn:
            self._send(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            params = json.loads(raw or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "Invalid request body"})
            return
        try:
            result = fn(params)
            self._send(200, result)
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # pragma: no cover - defensive
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def _find_free_port(host, start, tries=20):
    for i in range(tries):
        port = start + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    return start


def _resume_pending():
    """Re-run any guest match that never finished because the app was closed
    mid-scan.  Only runs when the vision models are already present so we never
    trigger a surprise download at launch.  Results are written back to the
    durable store, so the host dashboard is complete on next open."""
    try:
        from . import vision
        if vision.check_deps() or not vision.cluster_api_available() \
                or not vision.models_present():
            return
    except Exception:
        return
    for g in registrations.pending():
        root = g.get("root")
        selfies = [s for s in (g.get("selfies") or []) if os.path.isfile(s)]
        if not root or not os.path.isdir(root) or not selfies:
            continue
        reg_id = g["id"]

        def _on_done(job, _rid=reg_id):
            if job.get("error"):
                registrations.save_error(_rid, job["error"])
            else:
                registrations.save_results(_rid, job.get("result") or {})

        try:
            _register_root(root)
            r = _start_facefind_job(root, selfies,
                                    float(g.get("threshold") or 0.44),
                                    True, on_done=_on_done)
            registrations.set_job(reg_id, r["jobId"])
        except Exception:
            continue


def _relay_build_matcher(root, threshold=0.44):
    """Build a matcher for the relay sync loop: given a guest's selfie bytes,
    run local face matching over *root* and return the delivered photos as
    medium JPEGs.  Heavy work stays on this PC; only results go to the relay."""
    from . import vision
    from PIL import Image
    import io as _io

    def matcher(selfie_bytes, _reg):
        d = os.path.join(tempfile.gettempdir(), "phorg_relay")
        os.makedirs(d, exist_ok=True)
        sp = os.path.join(d, uuid.uuid4().hex + ".jpg")
        with open(sp, "wb") as f:
            f.write(selfie_bytes)
        try:
            _be, native = _gather_event_images(root, True)
            res = vision.facefind(native, [sp], threshold=threshold)
            out = []
            for m in (res.get("matches") or []):
                p = m.get("path")
                if not p or not os.path.isfile(p):
                    continue
                try:
                    im = Image.open(p).convert("RGB")
                    im.thumbnail((1600, 1600))
                    buf = _io.BytesIO()
                    im.save(buf, "JPEG", quality=85)
                    out.append({"name": os.path.basename(p),
                                "score": m.get("score"),
                                "image_bytes": buf.getvalue()})
                except Exception:
                    continue
            return out
        finally:
            try:
                os.remove(sp)
            except OSError:
                pass
    return matcher


def _relay_loop(base, event_id, key, name, root, interval=20):
    """Opt-in background loop: keep the event published on the always-on relay
    and drain queued guest sign-ups by matching them locally.  Enabled via the
    PHORG_RELAY_* environment variables (see serve())."""
    from . import vision, relay_client
    if vision.check_deps():
        return
    try:
        relay_client.publish_event(base, event_id, name, key)
    except Exception:
        pass
    tier_b = str(os.environ.get("PHORG_RELAY_TIER_B", "")).lower() \
        not in ("", "0", "false", "no")
    matcher = _relay_build_matcher(root)
    i = 0
    while True:
        if tier_b and i % 15 == 0:      # refresh the instant-match index
            try:
                _relay_publish_index(base, event_id, key, root)
            except Exception:
                pass
        try:
            relay_client.sync_once(base, event_id, key, matcher)
        except Exception:
            pass
        i += 1
        time.sleep(max(5, interval))


def _relay_publish_index(base, event_id, key, root):
    """Tier B: publish face embeddings + medium images for every event photo so
    the relay can match guests instantly while this PC is off.  Incremental —
    photos already on the relay are skipped."""
    from . import vision, relay_client
    from PIL import Image
    import io as _io
    import hashlib
    if vision.check_deps() or not vision.models_present():
        return
    try:
        st = relay_client.index_status(base, event_id, key)
        have = set(st.get("pids") or [])
    except Exception:
        have = set()
    _be, native = _gather_event_images(root, True)
    emb = vision.FaceEmbedder()
    batch = []
    for p in native:
        pid = hashlib.sha1(os.path.abspath(p).encode()).hexdigest()[:16]
        if pid in have:
            continue
        try:
            vecs = emb.embed_all(p)
        except Exception:
            vecs = []
        if not vecs:
            continue
        try:
            im = Image.open(p).convert("RGB")
            im.thumbnail((1400, 1400))
            buf = _io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            img = buf.getvalue()
        except Exception:
            continue
        batch.append({"pid": pid, "name": os.path.basename(p),
                      "embeds": [v.tolist() for v in vecs],
                      "image_bytes": img})
        if len(batch) >= 20:
            try:
                relay_client.publish_index(base, event_id, key, batch)
            except Exception:
                pass
            batch = []
    if batch:
        try:
            relay_client.publish_index(base, event_id, key, batch)
        except Exception:
            pass


def serve(host="127.0.0.1", port=8765, open_browser=True):
    global _SERVER_PORT
    # Durable guest queue: create the store, drop stale sign-ups, and resume any
    # match that was interrupted by a shutdown (Phase 1 availability).
    try:
        registrations.init()
        registrations.purge_expired()
        _register_root(registrations.selfie_dir())
    except Exception:
        pass
    # Bind on all interfaces so the guest-sharing portal can be reached from
    # phones on the same Wi-Fi.  Access stays loopback-only until the host
    # explicitly turns sharing on (see Handler._host_ok).
    bind_host = "0.0.0.0"
    port = _find_free_port(bind_host, port)
    _SERVER_PORT = port
    httpd = ThreadingHTTPServer((bind_host, port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print("\n  FaceFind is running.")
    print(f"  Open in your browser:  {url}")
    print("  Keep this window open while you use the app.")
    print("  Press Ctrl+C (or close this window) to stop.\n")
    threading.Thread(target=_resume_pending, daemon=True).start()
    # Opt-in always-on relay sync (Phase 3): match queued guest sign-ups locally
    # and post results to a relay that stays up while this PC is off.
    _rb = os.environ.get("PHORG_RELAY_URL")
    _re = os.environ.get("PHORG_RELAY_EVENT")
    _rk = os.environ.get("PHORG_RELAY_KEY")
    _rr = os.environ.get("PHORG_RELAY_ROOT")
    if _rb and _re and _rk and _rr and os.path.isdir(_rr):
        _rn = os.environ.get("PHORG_RELAY_NAME", "Our Event")
        print(f"  Relay sync ON → {_rb} (event {_re})\n")
        threading.Thread(target=_relay_loop,
                         args=(_rb, _re, _rk, _rn, _rr), daemon=True).start()
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopping FaceFind ...")
    finally:
        try:
            from . import tunnel
            tunnel.stop()
        except Exception:
            pass
        httpd.server_close()
