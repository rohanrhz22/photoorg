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
import sqlite3
import threading
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_MAX_IMG = 10 * 1024 * 1024          # 10 MB per uploaded image
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
  image  BLOB,
  PRIMARY KEY(reg_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_reg_event  ON registrations(event_id);
CREATE INDEX IF NOT EXISTS idx_reg_status ON registrations(status);
"""


def init():
    with _LOCK:
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()


def _now():
    return int(time.time())


def _uid(n=12):
    return base64.urlsafe_b64encode(os.urandom(n)).decode().rstrip("=")


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
                    "INSERT OR REPLACE INTO results(reg_id,idx,name,score,image)"
                    " VALUES(?,?,?,?,?)",
                    (rid, i, m.get("name") or f"photo_{i}.jpg",
                     float(m.get("score") or 0), blob))
            conn.execute(
                "UPDATE registrations SET status=?,count=?,matched_at=? WHERE id=?",
                (status or "matched", count if count is not None
                 else len(matches or []), _now(), rid))
            conn.commit()
            return True
        finally:
            conn.close()


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
                "SELECT idx,name,score,(image IS NOT NULL) AS has_img "
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


def result_image(rid, atoken, idx):
    with _LOCK:
        conn = _connect()
        try:
            ok = conn.execute(
                "SELECT 1 FROM registrations WHERE id=? AND atoken=?",
                (rid, atoken)).fetchone()
            if not ok:
                return None, None
            row = conn.execute(
                "SELECT name,image FROM results WHERE reg_id=? AND idx=?",
                (rid, int(idx))).fetchone()
            if not row or row["image"] is None:
                return None, None
            return row["name"], row["image"]
        finally:
            conn.close()


# --------------------------------------------------------------------------
# guest portal (compact, self-contained)
# --------------------------------------------------------------------------
_PORTAL = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>FaceFind</title><style>
body{margin:0;font-family:'Segoe UI',Roboto,Arial,sans-serif;background:#0b1020;color:#e8ecf8}
.wrap{max-width:620px;margin:18px auto;padding:0 16px}
.card{background:#161f3d;border:1px solid #28345c;border-radius:16px;padding:18px;margin-top:14px}
h1{font-size:1.4rem}label{display:block;margin:12px 0 4px;color:#9aa6c7;font-size:.9rem}
input{width:100%;padding:10px;border-radius:10px;border:1px solid #28345c;background:#0d1424;color:#e8ecf8}
.btn{margin-top:14px;padding:11px 16px;border:0;border-radius:10px;font-weight:600;cursor:pointer;
background:linear-gradient(90deg,#6ea8fe,#7ef0c2);color:#0b1020}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-top:14px}
.grid a{display:block;border:1px solid #28345c;border-radius:12px;overflow:hidden}
.grid img{width:100%;height:130px;object-fit:cover;display:block}
.callout{padding:12px;border-radius:10px;background:#12203a;border:1px solid #28345c;margin-top:12px}
.mut{color:#9aa6c7;font-size:.85rem}</style></head><body><div class=wrap id=app></div>
<script>
const qs=new URLSearchParams(location.search);const app=document.getElementById('app');
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(path,opts){const r=await fetch(path,opts);const d=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(d.error||'error');return d;}
function fileB64(f){return new Promise((res,rej)=>{const rd=new FileReader();
  rd.onload=()=>res(rd.result);rd.onerror=()=>rej(new Error('read'));rd.readAsDataURL(f);});}
async function showAlbum(a){
  let d;try{d=await api('/api/album?a='+encodeURIComponent(a));}catch(e){d=null;}
  if(!d){app.innerHTML='<div class=card><div class=callout>This album link isn\\'t valid.</div></div>';return;}
  const m=d.matches||[];
  app.innerHTML='<h1>📸 '+esc(d.event)+'</h1><div class=card>'+
    (d.status!=='matched'?'<div class=callout>⏳ Your photos aren\\'t ready yet. We\\'ll have them shortly — check back soon.</div>':
      (m.length? '<div class=callout>✅ Found <b>'+m.length+'</b> photo(s) you\\'re in.</div><div class=grid>'+
        m.map(x=>'<a href="/api/photo?a='+encodeURIComponent(a)+'&i='+x.i+'&dl=1" download>'+
          (x.has_img?'<img loading=lazy src="/api/photo?a='+encodeURIComponent(a)+'&i='+x.i+'">':'')+'</a>').join('')+'</div>'
        :'<div class=callout>No matches found. Try registering again with a clearer selfie.</div>'))+
    '</div>';
}
function showRegister(ev){
  app.innerHTML='<h1>📸 '+esc(ev.name||'Find your photos')+'</h1>'+
    '<p class=mut>Add a selfie to find every photo you\\'re in. Matched privately.</p><div class=card>'+
    '<label>Your name (optional)</label><input id=n placeholder="e.g. Anjali">'+
    '<label>Email or phone (optional) — so we can send your photos</label><input id=c placeholder="you@email.com">'+
    '<label>Your selfie</label><input id=f type=file accept="image/*">'+
    '<button class=btn id=go>🔎 Find my photos</button><div id=out></div></div>';
  document.getElementById('go').onclick=async()=>{
    const f=document.getElementById('f').files[0];const out=document.getElementById('out');
    if(!f){out.innerHTML='<div class=callout>Please add a selfie.</div>';return;}
    out.innerHTML='<div class=callout>Uploading…</div>';
    try{const selfie=await fileB64(f);
      const d=await api('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({event:ev.id,name:document.getElementById('n').value,
          contact:document.getElementById('c').value,selfie:selfie})});
      const link=location.origin+'/?a='+d.rid+'.'+d.atoken;
      out.innerHTML='<div class=callout>✅ You\\'re registered! We\\'ll find your photos.<br><br>'+
        '🔖 Save your private album link:<br><input readonly value="'+esc(link)+'"></div>';
      history.replaceState(null,'','/?a='+d.rid+'.'+d.atoken);
      setTimeout(()=>showAlbum(d.rid+'.'+d.atoken),1200);
    }catch(e){out.innerHTML='<div class=callout>'+esc(e.message)+'</div>';}
  };
}
(async()=>{
  const a=qs.get('a');
  if(a){showAlbum(a);return;}
  const ev=qs.get('e');
  if(ev){try{const d=await api('/api/event/public?e='+encodeURIComponent(ev));showRegister({id:ev,name:d.name});}
    catch(e){app.innerHTML='<div class=card><div class=callout>Event not found.</div></div>';}return;}
  app.innerHTML='<div class=card><div class=callout>Open the event link your host shared with you.</div></div>';
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
        if path == "/api/album":
            rid, tok = _split_ref((qs.get("a") or [""])[0])
            d = album(rid, tok)
            self._json(200 if d else 404, d or {"error": "not found"})
            return
        if path == "/api/photo":
            rid, tok = _split_ref((qs.get("a") or [""])[0])
            try:
                idx = int((qs.get("i") or ["0"])[0])
            except ValueError:
                idx = 0
            name, blob = result_image(rid, tok, idx)
            if blob is None:
                self._json(404, {"error": "not found"})
                return
            dl = (qs.get("dl") or [""])[0] == "1"
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
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
        self._json(404, {"error": "not found"})


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
    print(f"\n  FaceFind relay running on http://{host}:{port}/")
    print(f"  Data dir: {_home()}")
    print("  Keep this always on so guest links never die. Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopping relay ...")
    finally:
        httpd.server_close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="FaceFind always-on relay")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args(argv)
    serve(args.port, args.host)


if __name__ == "__main__":
    main()
