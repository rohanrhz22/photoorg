"""Always-on store-and-forward relay (Phase 3 of always-on availability).

This is the small piece that must never sleep.  It is deliberately independent
of the desktop app: run it on any always-on host (a tiny VPS, a container, a
Raspberry Pi) and it keeps the guest link alive even when the photographer's PC
is off.

What it does
------------
* Serves a stable guest portal (register + album) that never dies.
* **Queues** guest sign-ups (selfie + contact) durably, working while the host
  PC is offline.
* Lets the host **pull** the queued sign-ups, match them locally, and **post**
  the results back — so heavy face matching never runs in the cloud.
* Serves each guest their private album (delivered photos) from a signed link.

Only the Python standard library is used (``http.server`` + ``sqlite3``), so it
deploys with nothing to install.

Storage lives under ``$PHORG_RELAY_HOME`` (default ``~/.phorg/relay``).

Run it::

    python -m phorg.relay_server --port 8080
    # or:  python -m phorg relay --port 8080
"""
from __future__ import annotations

import os
import io
import json
import time
import hmac
import base64
import shutil
import sqlite3
import mimetypes
import threading
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_MAX_IMG = 10 * 1024 * 1024          # 10 MB per uploaded image
_MAX_ORIG = 60 * 1024 * 1024         # 60 MB per full-quality original
_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
def _home():
    d = os.environ.get("PHORG_RELAY_HOME") \
        or os.path.join(os.path.expanduser("~"), ".phorg", "relay")
    os.makedirs(d, exist_ok=True)
    return d


def _db_path():
    return os.path.join(_home(), "relay.db")


def _connect():
    conn = sqlite3.connect(_db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id         TEXT PRIMARY KEY,
  name       TEXT,
  key        TEXT,
  expires_at INTEGER DEFAULT 0,        -- 0 = never; else purge after this time
  created_at INTEGER,
  updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS registrations(
  id         TEXT PRIMARY KEY,
  event_id   TEXT,
  name       TEXT,
  contact    TEXT,
  atoken     TEXT,
  selfie     BLOB,
  status     TEXT DEFAULT 'pending',   -- pending | matched | error
  count      INTEGER,
  error      TEXT,
  created_at INTEGER,
  matched_at INTEGER
);
CREATE TABLE IF NOT EXISTS results(
  reg_id TEXT,
  idx    INTEGER,
  name   TEXT,
  score  REAL,
  pid    TEXT,
  image  BLOB,
  PRIMARY KEY(reg_id, idx)
);
CREATE TABLE IF NOT EXISTS photos(
  event_id   TEXT,
  pid        TEXT,
  name       TEXT,
  embeds     TEXT,                     -- JSON list of unit-norm face vectors
  image      BLOB,                     -- medium deliverable JPEG
  created_at INTEGER,
  PRIMARY KEY(event_id, pid)
);
CREATE INDEX IF NOT EXISTS idx_reg_event  ON registrations(event_id);
CREATE INDEX IF NOT EXISTS idx_reg_status ON registrations(status);
CREATE INDEX IF NOT EXISTS idx_photo_event ON photos(event_id);
"""


def init():
    with _LOCK:
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            # Forward-compat for relays created before later phases.
            for col in ("ALTER TABLE results ADD COLUMN pid TEXT",
                        "ALTER TABLE events ADD COLUMN expires_at INTEGER "
                        "DEFAULT 0"):
                try:
                    conn.execute(col)
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        finally:
            conn.close()


def _now():
    return int(time.time())


def _uid(n=12):
    return base64.urlsafe_b64encode(os.urandom(n)).decode().rstrip("=")


def _safe_component(s):
    """A string safe to use as a single path component (no traversal)."""
    return "".join(c for c in str(s or "") if c.isalnum() or c in "-_")[:80]


# Full-quality originals live on the filesystem (they can be 10-30 MB each);
# the DB keeps only the medium deliverables so it stays small and fast.
def _orig_dir(event_id, create=False):
    d = os.path.join(_home(), "originals", _safe_component(event_id))
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def _orig_path(event_id, pid):
    return os.path.join(_orig_dir(event_id), _safe_component(pid))


def _original_bytes(event_id, pid):
    p = _orig_path(event_id, pid)
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        return None


# --------------------------------------------------------------------------
# data operations
# --------------------------------------------------------------------------
def upsert_event(event_id, name, key):
    """Create the event, or verify the key if it already exists.  Returns the
    event dict, or None when the key does not match an existing event."""
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM events WHERE id=?",
                               (event_id,)).fetchone()
            if row:
                if not hmac.compare_digest(str(row["key"]), str(key)):
                    return None
                conn.execute("UPDATE events SET name=?,updated_at=? WHERE id=?",
                             (name or row["name"], _now(), event_id))
            else:
                conn.execute(
                    "INSERT INTO events(id,name,key,created_at,updated_at)"
                    " VALUES(?,?,?,?,?)",
                    (event_id, name or "", key, _now(), _now()))
            conn.commit()
            return {"id": event_id, "name": name or "", "key": key}
        finally:
            conn.close()


def _event_key_ok(conn, event_id, key):
    row = conn.execute("SELECT key FROM events WHERE id=?", (event_id,)).fetchone()
    return bool(row) and hmac.compare_digest(str(row["key"]), str(key or ""))


def _delete_event_data(conn, event_id):
    """Remove every trace of an event (registrations, results, photos, event)."""
    regids = [r["id"] for r in conn.execute(
        "SELECT id FROM registrations WHERE event_id=?", (event_id,)).fetchall()]
    if regids:
        q = ",".join("?" * len(regids))
        conn.execute(f"DELETE FROM results WHERE reg_id IN ({q})", regids)
    conn.execute("DELETE FROM registrations WHERE event_id=?", (event_id,))
    conn.execute("DELETE FROM photos WHERE event_id=?", (event_id,))
    conn.execute("DELETE FROM events WHERE id=?", (event_id,))
    shutil.rmtree(_orig_dir(event_id), ignore_errors=True)


def _purge_if_expired(event_id):
    """If the event's expiry has passed, delete all its data.  Returns True when
    it was purged."""
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT expires_at FROM events WHERE id=?",
                               (event_id,)).fetchone()
            if not row:
                return False
            exp = row["expires_at"] or 0
            if exp and _now() > exp:
                _delete_event_data(conn, event_id)
                conn.commit()
                return True
            return False
        finally:
            conn.close()


def purge_expired_events():
    """Delete every event whose expiry has passed.  Returns how many were
    purged.  Called on a timer by the running relay and lazily on access."""
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT id FROM events WHERE expires_at>0 AND expires_at<?",
                (_now(),)).fetchall()
            for r in rows:
                _delete_event_data(conn, r["id"])
            conn.commit()
            return len(rows)
        finally:
            conn.close()


def set_lifecycle(event_id, key, expires_at=0):
    """Host: set/clear the event's auto-purge time (epoch seconds; 0 = never)."""
    with _LOCK:
        conn = _connect()
        try:
            if not _event_key_ok(conn, event_id, key):
                return None
            conn.execute("UPDATE events SET expires_at=?,updated_at=? WHERE id=?",
                         (int(expires_at or 0), _now(), event_id))
            conn.commit()
            return {"expires_at": int(expires_at or 0)}
        finally:
            conn.close()


def event_stats(event_id, key):
    """Host: registration/match/photo counts for the dashboard."""
    with _LOCK:
        conn = _connect()
        try:
            if not _event_key_ok(conn, event_id, key):
                return None
            ev = conn.execute(
                "SELECT name,expires_at FROM events WHERE id=?",
                (event_id,)).fetchone()

            def c(sql):
                return conn.execute(sql, (event_id,)).fetchone()[0]

            matched_pids = {r["pid"] for r in conn.execute(
                "SELECT DISTINCT res.pid AS pid FROM results res "
                "JOIN registrations r ON r.id=res.reg_id "
                "WHERE r.event_id=? AND res.pid IS NOT NULL",
                (event_id,)).fetchall()}
            out = {
                "event": ev["name"], "expires_at": ev["expires_at"] or 0,
                "registrations": c("SELECT COUNT(*) FROM registrations "
                                   "WHERE event_id=?"),
                "matched": c("SELECT COUNT(*) FROM registrations "
                             "WHERE event_id=? AND status='matched'"),
                "pending": c("SELECT COUNT(*) FROM registrations "
                             "WHERE event_id=? AND status='pending'"),
                "photos": c("SELECT COUNT(*) FROM photos WHERE event_id=?"),
            }
        finally:
            conn.close()
    d = _orig_dir(event_id)
    have = set(os.listdir(d)) if os.path.isdir(d) else set()
    out["originals"] = len(have)
    out["originals_pending"] = len(
        [p for p in matched_pids if _safe_component(p) not in have])
    return out


def delete_event(event_id, key):
    """Host: immediately delete an event and all its guest data."""
    with _LOCK:
        conn = _connect()
        try:
            if not _event_key_ok(conn, event_id, key):
                return None
            _delete_event_data(conn, event_id)
            conn.commit()
            return True
        finally:
            conn.close()


def event_public(event_id):
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT id,name FROM events WHERE id=?",
                               (event_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def add_registration(event_id, name, contact, selfie_bytes):
    """Queue a guest sign-up.  Works whether or not the host is online."""
    _purge_if_expired(event_id)
    rid = _uid()
    atoken = _uid(16)
    with _LOCK:
        conn = _connect()
        try:
            if not conn.execute("SELECT 1 FROM events WHERE id=?",
                                (event_id,)).fetchone():
                return None
            conn.execute(
                "INSERT INTO registrations"
                "(id,event_id,name,contact,atoken,selfie,status,created_at)"
                " VALUES(?,?,?,?,?,?, 'pending', ?)",
                (rid, event_id, name or "Guest", contact or "", atoken,
                 selfie_bytes, _now()))
            conn.commit()
            return {"rid": rid, "atoken": atoken}
        finally:
            conn.close()


def pull_pending(event_id, key, limit=25):
    """Host: fetch queued sign-ups still needing a match (selfie base64)."""
    with _LOCK:
        conn = _connect()
        try:
            if not _event_key_ok(conn, event_id, key):
                return None
            rows = conn.execute(
                "SELECT id,name,contact,atoken,selfie,created_at FROM "
                "registrations WHERE event_id=? AND status='pending' "
                "ORDER BY created_at LIMIT ?", (event_id, int(limit))).fetchall()
            out = []
            for r in rows:
                out.append({
                    "rid": r["id"], "name": r["name"], "contact": r["contact"],
                    "atoken": r["atoken"], "created_at": r["created_at"],
                    "selfie_b64": base64.b64encode(r["selfie"]).decode()
                    if r["selfie"] else "",
                })
            return out
        finally:
            conn.close()


def save_results(event_id, key, rid, status, count, matches):
    """Host: post the match results (with delivered photo bytes) back."""
    with _LOCK:
        conn = _connect()
        try:
            if not _event_key_ok(conn, event_id, key):
                return False
            owner = conn.execute(
                "SELECT event_id FROM registrations WHERE id=?", (rid,)).fetchone()
            if not owner or owner["event_id"] != event_id:
                return False
            conn.execute("DELETE FROM results WHERE reg_id=?", (rid,))
            for i, m in enumerate(matches or []):
                img = m.get("image_b64")
                blob = base64.b64decode(img) if img else None
                if blob and len(blob) > _MAX_IMG:
                    blob = None
                conn.execute(
                    "INSERT OR REPLACE INTO results(reg_id,idx,name,score,pid,image)"
                    " VALUES(?,?,?,?,?,?)",
                    (rid, i, m.get("name") or f"photo_{i}.jpg",
                     float(m.get("score") or 0), m.get("pid") or None, blob))
            conn.execute(
                "UPDATE registrations SET status=?,count=?,matched_at=? WHERE id=?",
                (status or "matched", count if count is not None
                 else len(matches or []), _now(), rid))
            conn.commit()
            return True
        finally:
            conn.close()


def save_original(event_id, key, pid, data):
    """Host: store the full-quality file for one matched photo.  Guests get it
    on download; the medium copy keeps serving the gallery view."""
    if not pid or not data or len(data) > _MAX_ORIG:
        return {"error": "bad original"}
    with _LOCK:
        conn = _connect()
        try:
            if not _event_key_ok(conn, event_id, key):
                return None
        finally:
            conn.close()
    p = _orig_path(event_id, pid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, p)
    return {"ok": True}


def originals_needed(event_id, key):
    """Host: matched photo ids that still lack a full-quality file, so the PC
    can upgrade albums created while it was off."""
    with _LOCK:
        conn = _connect()
        try:
            if not _event_key_ok(conn, event_id, key):
                return None
            rows = conn.execute(
                "SELECT DISTINCT res.pid AS pid FROM results res "
                "JOIN registrations r ON r.id=res.reg_id "
                "WHERE r.event_id=? AND res.pid IS NOT NULL",
                (event_id,)).fetchall()
        finally:
            conn.close()
    d = _orig_dir(event_id)
    have = set(os.listdir(d)) if os.path.isdir(d) else set()
    return [r["pid"] for r in rows if _safe_component(r["pid"]) not in have]


def album(rid, atoken):
    with _LOCK:
        conn = _connect()
        try:
            r = conn.execute(
                "SELECT r.*, e.name AS event_name FROM registrations r "
                "JOIN events e ON e.id=r.event_id WHERE r.id=? AND r.atoken=?",
                (rid, atoken)).fetchone()
            if not r:
                return None
            items = conn.execute(
                "SELECT idx,name,score,"
                "(image IS NOT NULL OR pid IS NOT NULL) AS has_img "
                "FROM results WHERE reg_id=? ORDER BY score DESC", (rid,)
            ).fetchall()
            return {
                "event": r["event_name"] or "the event",
                "name": r["name"], "status": r["status"], "count": r["count"],
                "matches": [{"i": it["idx"], "name": it["name"],
                             "score": it["score"], "has_img": bool(it["has_img"])}
                            for it in items],
            }
        finally:
            conn.close()


def result_image(rid, atoken, idx, prefer_original=False):
    with _LOCK:
        conn = _connect()
        try:
            reg = conn.execute(
                "SELECT event_id FROM registrations WHERE id=? AND atoken=?",
                (rid, atoken)).fetchone()
            if not reg:
                return None, None
            row = conn.execute(
                "SELECT name,image,pid FROM results WHERE reg_id=? AND idx=?",
                (rid, int(idx))).fetchone()
            if not row:
                return None, None
            medium = row["image"]
            if medium is None and row["pid"]:    # Tier B: image lives on the photo
                ph = conn.execute(
                    "SELECT image FROM photos WHERE event_id=? AND pid=?",
                    (reg["event_id"], row["pid"])).fetchone()
                medium = ph["image"] if ph else None
        finally:
            conn.close()
    # Downloads get the full-quality file when the PC has sent it; the gallery
    # view keeps the fast medium copy.
    if prefer_original and row["pid"]:
        orig = _original_bytes(reg["event_id"], row["pid"])
        if orig is not None:
            return row["name"], orig
    if medium is not None:
        return row["name"], medium
    return None, None


# --------------------------------------------------------------------------
# Tier B: instant matching from a published embedding index
# --------------------------------------------------------------------------
def _cos(a, b):
    s = da = db = 0.0
    for x, y in zip(a, b):
        s += x * y
        da += x * x
        db += y * y
    if da <= 0 or db <= 0:
        return 0.0
    return s / ((da ** 0.5) * (db ** 0.5))


def _as_query_vectors(x):
    if not x:
        return []
    if isinstance(x, (list, tuple)) and x and isinstance(x[0], (list, tuple)):
        return [list(v) for v in x]
    if isinstance(x, (list, tuple)):
        return [list(x)]
    return []


def _relay_embed(selfie_bytes):
    """Embed a guest selfie on the relay when the vision stack is installed.
    Returns None when instant matching isn't available (caller falls back to
    store-and-forward), [] when no face was found, else a list of vectors."""
    try:
        from phorg import vision
        if vision.check_deps() or not vision.models_present():
            return None
    except Exception:
        return None
    import tempfile
    import uuid as _uuid
    d = os.path.join(tempfile.gettempdir(), "phorg_relay_sel")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, _uuid.uuid4().hex + ".jpg")
    try:
        with open(p, "wb") as f:
            f.write(selfie_bytes)
        vecs = vision.FaceEmbedder().embed_all(p)
        return [v.tolist() for v in vecs] if vecs else []
    except Exception:
        return []
    finally:
        try:
            os.remove(p)
        except OSError:
            pass


def index_photos(event_id, key, photos):
    """Host: publish (or refresh) the event's face-embedding index."""
    with _LOCK:
        conn = _connect()
        try:
            if not _event_key_ok(conn, event_id, key):
                return None
            n = 0
            for ph in photos or []:
                img = ph.get("image_b64")
                blob = base64.b64decode(img) if img else None
                if blob and len(blob) > _MAX_IMG:
                    blob = None
                conn.execute(
                    "INSERT OR REPLACE INTO photos"
                    "(event_id,pid,name,embeds,image,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (event_id, ph.get("pid"), ph.get("name") or "",
                     json.dumps(ph.get("embeds") or []), blob, _now()))
                n += 1
            conn.commit()
            return n
        finally:
            conn.close()


def index_status(event_id, key):
    with _LOCK:
        conn = _connect()
        try:
            if not _event_key_ok(conn, event_id, key):
                return None
            rows = conn.execute(
                "SELECT pid FROM photos WHERE event_id=?", (event_id,)).fetchall()
            pids = [r["pid"] for r in rows]
            return {"count": len(pids), "pids": pids}
        finally:
            conn.close()


def match_guest(event_id, name, contact, selfie_bytes=None,
                selfie_embed=None, threshold=0.44):
    """Instant match a guest against the published index (Tier B).  Creates a
    matched registration so the normal album link works."""
    queries = _as_query_vectors(selfie_embed)
    if not queries and selfie_bytes is not None:
        emb = _relay_embed(selfie_bytes)
        if emb is None:
            return {"error": "instant_unavailable", "need_fallback": True}
        queries = emb
    if not queries:
        return {"error": "no_face"}
    _purge_if_expired(event_id)
    with _LOCK:
        conn = _connect()
        try:
            if not conn.execute("SELECT 1 FROM events WHERE id=?",
                                (event_id,)).fetchone():
                return {"error": "no such event"}
            rows = conn.execute(
                "SELECT pid,name,embeds FROM photos WHERE event_id=?",
                (event_id,)).fetchall()
            matches = []
            for r in rows:
                try:
                    embeds = json.loads(r["embeds"] or "[]")
                except (ValueError, TypeError):
                    embeds = []
                best = 0.0
                for q in queries:
                    for e in embeds:
                        c = _cos(q, e)
                        if c > best:
                            best = c
                if best >= threshold:
                    matches.append((r["pid"], r["name"], best))
            matches.sort(key=lambda x: -x[2])
            rid = _uid()
            atoken = _uid(16)
            conn.execute(
                "INSERT INTO registrations(id,event_id,name,contact,atoken,"
                "selfie,status,count,created_at,matched_at)"
                " VALUES(?,?,?,?,?,?, 'matched', ?, ?, ?)",
                (rid, event_id, name or "Guest", contact or "", atoken, None,
                 len(matches), _now(), _now()))
            for i, (pid, nm, score) in enumerate(matches):
                conn.execute(
                    "INSERT OR REPLACE INTO results(reg_id,idx,name,score,pid,image)"
                    " VALUES(?,?,?,?,?,NULL)", (rid, i, nm, float(score), pid))
            conn.commit()
            return {"rid": rid, "atoken": atoken, "count": len(matches),
                    "matches": [{"i": i, "name": nm, "score": sc, "has_img": True}
                                for i, (pid, nm, sc) in enumerate(matches)]}
        finally:
            conn.close()


# --------------------------------------------------------------------------
# guest portal (compact, self-contained)
# --------------------------------------------------------------------------
_PORTAL = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Your photographs · Hapzea</title><style>
body{margin:0;color:#2a231b;font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
background:#f6f1e7;background-image:radial-gradient(900px 420px at 50% -8%,#fffdf7,rgba(255,253,247,0))}
.wrap{max-width:560px;margin:0 auto;padding:8px 18px 40px}
.brand{text-align:center;padding-top:24px;font-family:Georgia,'Times New Roman',serif;
letter-spacing:.34em;font-size:12px;color:#8a6b30}
.hdr{text-align:center}
.eyebrow{font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:#a3803c;
margin:26px 0 0;font-weight:700}
h1.ev{font-family:Georgia,'Times New Roman',serif;font-style:italic;font-weight:500;
font-size:clamp(26px,6vw,34px);margin:8px 0 10px;color:#241d15}
.lead{color:#6f6452;font-size:15.5px;line-height:1.55;margin:0 0 6px}
.card{background:#fffdf9;border:1px solid #e8dfd0;border-radius:18px;padding:22px;
margin-top:18px;box-shadow:0 10px 30px rgba(60,45,20,.07);text-align:left}
.selwrap{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.selring{width:118px;height:118px;border-radius:50%;border:2px dashed #cdbd9a;flex:none;
display:flex;align-items:center;justify-content:center;color:#b3a685;
background:#fbf7ee center/cover no-repeat;cursor:pointer}
.selring.set{border:3px solid #b08d4a;color:transparent}
.selbtns{display:flex;flex-direction:column;gap:9px;flex:1;min-width:180px}
.selok{color:#6b7f4f;font-size:13px;font-weight:600;margin-top:10px}
.btn{appearance:none;border:0;cursor:pointer;border-radius:999px;padding:12px 20px;
font-size:15px;font-weight:600;background:#28211a;color:#f8f3e9;letter-spacing:.01em}
.btn.ghost{background:transparent;border:1px solid #d5c7ac;color:#4a4133}
.btn.wide{display:block;width:100%;margin-top:18px;padding:14px}
.btn.sm{padding:9px 14px;font-size:13.5px}
.hr{height:1px;background:#e9dfcb;margin:20px 0}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:460px){.two{grid-template-columns:1fr}}
label{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#8a7c66;
display:block;margin:0 0 6px}
.opt{color:#b3a685;text-transform:none;letter-spacing:0}
input{width:100%;box-sizing:border-box;padding:12px;border-radius:12px;
border:1px solid #ddd0b8;background:#fdfaf3;color:#26211a;font-size:16px}
.note{background:#f7efdd;border:1px solid #e6d7b5;border-radius:12px;padding:12px 14px;
font-size:14.5px;margin-top:14px;line-height:1.5}
.fine{color:#a4977f;font-size:12.5px;margin:16px 0 0;text-align:center;line-height:1.5}
.keep{background:#f4ecdc;border:1px solid #e2d5b8;border-radius:14px;padding:12px 14px;margin-top:16px}
.keeplbl{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#8a6b30;
font-weight:700;margin-bottom:8px}
.keeprow{display:flex;gap:8px}
.keeprow input{font-size:13px;padding:9px 10px}
.waitrow{display:flex;gap:12px;align-items:flex-start}
.mut2{color:#6f6452;font-size:14px;line-height:1.5}
.pulse{width:10px;height:10px;border-radius:50%;background:#b08d4a;flex:none;margin-top:5px;
animation:pu 1.2s ease-in-out infinite}
@keyframes pu{50%{opacity:.25;transform:scale(.8)}}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin:18px 0 4px}
.tile{position:relative;display:block;border-radius:14px;overflow:hidden;background:#eee5d2;
aspect-ratio:1/1;border:1px solid #e6dac2}
.tile img{width:100%;height:100%;object-fit:cover;display:block}
.tile .dl{position:absolute;right:8px;bottom:8px;width:30px;height:30px;border-radius:50%;
background:rgba(38,33,26,.82);color:#f7f0e2;display:flex;align-items:center;justify-content:center}
.foot{text-align:center;color:#a4977f;font-size:11px;letter-spacing:.16em;
text-transform:uppercase;margin:30px 0 8px}
.hide{display:none}
</style></head><body><div class=wrap><div id=app></div></div>
<script>
const app=document.getElementById("app");
const el=x=>document.getElementById(x);
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const IC_CAM='<svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8.5h2.8l1.6-2.3h7.2l1.6 2.3H20a.9.9 0 0 1 .9.9V18a.9.9 0 0 1-.9.9H4a.9.9 0 0 1-.9-.9V9.4a.9.9 0 0 1 .9-.9z"/><circle cx="12" cy="13.4" r="3.4"/></svg>';
const IC_DL='<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11"/><path d="m7 11 5 5 5-5"/><path d="M5 20h14"/></svg>';
async function api(path,opts){const r=await fetch(path,opts);const d=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(d.error||"Something went wrong");return d;}
function paint(inner){app.innerHTML='<div class=brand>HAPZEA</div>'+inner+
  '<p class=foot>Your photos, found privately</p>';}
function hdr(eyebrow,title,lead){return '<div class=hdr><p class=eyebrow>'+esc(eyebrow)+'</p>'+
  '<h1 class=ev>'+esc(title)+'</h1>'+(lead?'<p class=lead>'+lead+'</p>':'')+'</div>';}
// Shrink the selfie on the phone before upload: faster on venue Wi-Fi and the
// matcher never needs more than ~1280px.
function prep(file){return new Promise((res,rej)=>{
  const img=new Image();const url=URL.createObjectURL(file);
  img.onload=()=>{const mx=1280,r=Math.min(1,mx/Math.max(img.width,img.height));
    const c=document.createElement("canvas");
    c.width=Math.round(img.width*r);c.height=Math.round(img.height*r);
    c.getContext("2d").drawImage(img,0,0,c.width,c.height);
    URL.revokeObjectURL(url);res(c.toDataURL("image/jpeg",0.85));};
  img.onerror=()=>{URL.revokeObjectURL(url);
    const rd=new FileReader();rd.onload=()=>res(rd.result);
    rd.onerror=()=>rej(new Error("That photo could not be read"));rd.readAsDataURL(file);};
  img.src=url;});}
function linkBox(ref){const link=location.origin+"/?a="+ref;
  return '<div class=keep><div class=keeplbl>Your private album link — save it</div>'+
    '<div class=keeprow><input readonly id=klink value="'+esc(link)+'">'+
    '<button class="btn ghost sm" id=kcopy>Copy</button></div></div>';}
function wireCopy(){const b=el("kcopy");if(!b)return;
  b.onclick=()=>{const i=el("klink");i.select();
    try{navigator.clipboard.writeText(i.value);}catch(e){document.execCommand("copy");}
    b.textContent="Copied";setTimeout(()=>b.textContent="Copy",1600);};}
let SELFIE=null;
function showRegister(ev){
  paint(hdr("Find your photographs",ev.name||"Our event",
    "One selfie is all it takes — we gather every photo you appear in.")+
  '<div class=card>'+
    '<div class=selwrap>'+
      '<div class=selring id=ring>'+IC_CAM+'</div>'+
      '<div class=selbtns>'+
        '<button class=btn id=take>Take a selfie</button>'+
        '<button class="btn ghost" id=pick>Choose from gallery</button>'+
        '<div class="selok hide" id=selok>Selfie added — looking good</div>'+
      '</div>'+
    '</div>'+
    '<input type=file id=cam accept="image/*" capture="user" class=hide>'+
    '<input type=file id=gal accept="image/*" class=hide>'+
    '<div class=hr></div>'+
    '<div class=two>'+
      '<div><label for=n>Name <span class=opt>· optional</span></label>'+
        '<input id=n placeholder="Anjali" autocomplete=name></div>'+
      '<div><label for=c>Email or phone <span class=opt>· optional</span></label>'+
        '<input id=c placeholder="you@email.com" autocomplete=email></div>'+
    '</div>'+
    '<button class="btn wide" id=go>Find my photographs</button>'+
    '<div id=out></div>'+
    '<p class=fine>Your selfie is used once, only to find your photos.<br>'+
    'It is never shared or posted anywhere.</p>'+
  '</div>');
  const use=async f=>{if(!f)return;
    try{SELFIE=await prep(f);}catch(e){el("out").innerHTML='<div class=note>'+esc(e.message)+'</div>';return;}
    const r=el("ring");r.style.backgroundImage="url("+SELFIE+")";r.classList.add("set");
    el("selok").classList.remove("hide");el("out").innerHTML="";};
  el("cam").onchange=e=>use(e.target.files[0]);
  el("gal").onchange=e=>use(e.target.files[0]);
  el("take").onclick=()=>el("cam").click();
  el("pick").onclick=()=>el("gal").click();
  el("ring").onclick=()=>el(SELFIE?"gal":"cam").click();
  el("go").onclick=async()=>{
    const out=el("out");
    if(!SELFIE){out.innerHTML='<div class=note>Please add a selfie first — take one or pick from your gallery.</div>';return;}
    out.innerHTML='<div class=note><span class=waitrow><span class=pulse></span>'+
      '<span>Looking through the album…</span></span></div>';
    const payload={event:ev.id,name:el("n").value,contact:el("c").value,selfie:SELFIE};
    let d;
    try{
      try{d=await api("/api/match",{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});}
      catch(e){d=await api("/api/register",{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});}
    }catch(e){out.innerHTML='<div class=note>'+esc(e.message)+'</div>';return;}
    const ref=d.rid+"."+d.atoken;
    history.replaceState(null,"","/?a="+ref+"&e="+encodeURIComponent(ev.id));
    showAlbum(ref);
  };
}
async function showAlbum(ref){
  let d;try{d=await api("/api/album?a="+encodeURIComponent(ref));}catch(e){d=null;}
  if(!d){paint('<div class=card><div class=note>This album link is not valid. '+
    'Please use the link or QR your event host shared with you.</div></div>');return;}
  if(d.status!=="matched"){
    paint(hdr("One moment",d.event,"")+
      '<div class=card><div class=waitrow><span class=pulse></span>'+
      '<div><b>We are gathering your photographs.</b><br>'+
      '<span class=mut2>This page updates by itself — nothing to do. '+
      'If you leave, your album will be waiting at the link below.</span></div></div>'+
      linkBox(ref)+'</div>');
    wireCopy();
    setTimeout(()=>showAlbum(ref),7000);
    return;
  }
  const m=d.matches||[];
  if(!m.length){
    const e=new URLSearchParams(location.search).get("e");
    paint(hdr("Your private album",d.event,"")+
      '<div class=card><div class=note>We could not find you this time. '+
      'A straight-on selfie in good light works best.</div>'+
      (e?'<button class="btn wide" id=retry>Try another selfie</button>':'')+'</div>');
    if(e)el("retry").onclick=()=>{location.href="/?e="+encodeURIComponent(e);};
    return;
  }
  paint(hdr("Your private album",d.event,
      "You appear in <b>"+m.length+"</b> photograph"+(m.length>1?"s":"")+
      ". Tap any photo to save it in full quality.")+
    '<div class=grid>'+m.map(x=>{
      const u="/api/photo?a="+encodeURIComponent(ref)+"&i="+x.i;
      return '<a class=tile href="'+u+'&dl=1" download>'+
        (x.has_img?'<img loading=lazy src="'+u+'" alt="">':'')+
        '<span class=dl>'+IC_DL+'</span></a>';}).join("")+'</div>'+
    linkBox(ref));
  wireCopy();
}
(async()=>{
  const qs=new URLSearchParams(location.search);
  const a=qs.get("a");
  if(a){showAlbum(a);return;}
  const e=qs.get("e");
  if(e){try{const d=await api("/api/event/public?e="+encodeURIComponent(e));
      showRegister({id:e,name:d.name});}
    catch(err){paint('<div class=card><div class=note>This event link is not active yet. '+
      'Please ask your host for a fresh link.</div></div>');}return;}
  paint('<div class=card><div class=note>Open the link or QR code your event host shared with you.</div></div>');
})();
</script></body></html>"""


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
class RelayHandler(BaseHTTPRequestHandler):
    server_version = "facefind-relay/1.0"

    def log_message(self, *_):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw or b"{}")
        except (ValueError, TypeError):
            return {}

    def _qs(self):
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(self.path).query)

    def _key(self):
        return self.headers.get("X-Event-Key") or ""

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        qs = self._qs()
        if path in ("/", "/index.html"):
            body = _PORTAL.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/healthz":
            self._json(200, {"ok": True})
            return
        if path == "/api/event/public":
            ev = event_public((qs.get("e") or [""])[0])
            if not ev:
                self._json(404, {"error": "no such event"})
            else:
                self._json(200, ev)
            return
        if path == "/api/pull":
            ev = (qs.get("event") or [""])[0]
            data = pull_pending(ev, self._key())
            if data is None:
                self._json(403, {"error": "bad event key"})
            else:
                self._json(200, {"registrations": data})
            return
        if path == "/api/index/status":
            ev = (qs.get("event") or [""])[0]
            st = index_status(ev, self._key())
            if st is None:
                self._json(403, {"error": "bad event key"})
            else:
                self._json(200, st)
            return
        if path == "/api/event/stats":
            ev = (qs.get("event") or [""])[0]
            st = event_stats(ev, self._key())
            if st is None:
                self._json(403, {"error": "bad event key"})
            else:
                self._json(200, st)
            return
        if path == "/api/album":
            rid, tok = _split_ref((qs.get("a") or [""])[0])
            d = album(rid, tok)
            self._json(200 if d else 404, d or {"error": "not found"})
            return
        if path == "/api/originals/needed":
            ev = (qs.get("event") or [""])[0]
            pids = originals_needed(ev, self._key())
            if pids is None:
                self._json(403, {"error": "bad event key"})
            else:
                self._json(200, {"pids": pids})
            return
        if path == "/api/photo":
            rid, tok = _split_ref((qs.get("a") or [""])[0])
            try:
                idx = int((qs.get("i") or ["0"])[0])
            except ValueError:
                idx = 0
            dl = (qs.get("dl") or [""])[0] == "1"
            name, blob = result_image(rid, tok, idx, prefer_original=dl)
            if blob is None:
                self._json(404, {"error": "not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type",
                             mimetypes.guess_type(name or "")[0] or "image/jpeg")
            if dl:
                safe = "".join(c for c in name if c.isalnum() or c in "._- ")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{safe or "photo.jpg"}"')
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return
        self._json(404, {"error": "not found"})

    # ---- POST ----
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/original":     # raw bytes, not JSON (files are big)
            self._post_original()
            return
        body = self._read_json()
        if path == "/api/event":
            ev = upsert_event(body.get("id") or _uid(8),
                              body.get("name") or "", body.get("key") or _uid(16))
            if ev is None:
                self._json(403, {"error": "event exists with a different key"})
            else:
                self._json(200, {"id": ev["id"], "name": ev["name"],
                                 "key": ev["key"]})
            return
        if path == "/api/register":
            selfie = _data_url_bytes(body.get("selfie"))
            if not selfie:
                self._json(400, {"error": "missing selfie"})
                return
            if len(selfie) > _MAX_IMG:
                self._json(400, {"error": "selfie too large"})
                return
            r = add_registration(body.get("event"), body.get("name"),
                                 body.get("contact"), selfie)
            if r is None:
                self._json(404, {"error": "no such event"})
            else:
                self._json(200, r)
            return
        if path == "/api/results":
            ok = save_results(body.get("event"), self._key(), body.get("rid"),
                              body.get("status"), body.get("count"),
                              body.get("matches") or [])
            self._json(200 if ok else 403, {"ok": ok})
            return
        if path == "/api/index":
            n = index_photos(body.get("event"), self._key(),
                             body.get("photos") or [])
            if n is None:
                self._json(403, {"error": "bad event key"})
            else:
                self._json(200, {"ok": True, "count": n})
            return
        if path == "/api/event/lifecycle":
            r = set_lifecycle(body.get("event"), self._key(),
                              body.get("expires_at") or 0)
            self._json(200 if r else 403, r or {"error": "bad event key"})
            return
        if path == "/api/event/delete":
            r = delete_event(body.get("event"), self._key())
            self._json(200 if r else 403, {"ok": bool(r)}
                       if r else {"error": "bad event key"})
            return
        if path == "/api/match":
            selfie = _data_url_bytes(body.get("selfie"))
            try:
                thr = float(body.get("threshold") or 0.44)
            except (TypeError, ValueError):
                thr = 0.44
            d = match_guest(body.get("event"), body.get("name"),
                            body.get("contact"), selfie_bytes=selfie,
                            selfie_embed=body.get("selfie_embedding"),
                            threshold=thr)
            if d.get("need_fallback"):
                self._json(501, d)
            elif d.get("error"):
                self._json(400, d)
            else:
                self._json(200, d)
            return
        self._json(404, {"error": "not found"})

    def _post_original(self):
        qs = self._qs()
        ev = (qs.get("event") or [""])[0]
        pid = (qs.get("pid") or [""])[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > _MAX_ORIG:
            self._json(413 if n > _MAX_ORIG else 400, {"error": "bad size"})
            return
        data = self.rfile.read(n)
        r = save_original(ev, self._key(), pid, data)
        if r is None:
            self._json(403, {"error": "bad event key"})
        elif r.get("error"):
            self._json(400, r)
        else:
            self._json(200, r)


def _split_ref(ref):
    ref = ref or ""
    if "." in ref:
        a, b = ref.split(".", 1)
        return a, b
    return ref, ""


def _data_url_bytes(s):
    if not s:
        return None
    if "," in s:
        s = s.split(",", 1)[1]
    try:
        return base64.b64decode(s)
    except Exception:
        return None


def make_server(port=8080, host="0.0.0.0"):
    init()
    return ThreadingHTTPServer((host, port), RelayHandler)


def serve(port=8080, host="0.0.0.0"):
    httpd = make_server(port, host)
    # Retention: purge expired events on a timer so guest data doesn't linger.
    def _purge_loop():
        while True:
            try:
                purge_expired_events()
            except Exception:
                pass
            time.sleep(300)
    threading.Thread(target=_purge_loop, daemon=True).start()
    print(f"\n  Hapzea relay running on http://{host}:{port}/")
    print(f"  Data dir: {_home()}")
    print("  Keep this always on so guest links never die. Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopping relay ...")
    finally:
        httpd.server_close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Hapzea always-on relay")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args(argv)
    serve(args.port, args.host)


if __name__ == "__main__":
    main()
