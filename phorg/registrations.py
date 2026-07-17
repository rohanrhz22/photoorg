"""Durable guest-registration queue (Phase 1 of always-on availability).

Guest sign-ups used to live only in memory (``_SHARE["guests"]``) and their
match results only in ``_JOBS`` — so closing the app forgot everyone and lost
any in-flight search.  This module persists both to a small SQLite database the
moment they happen, copies each selfie somewhere durable, and lets the server
resume unfinished matches on the next launch.

Nothing here talks to the network; it is the local foundation the later relay
phases build on.  Standard-library only (``sqlite3``), so the "nothing extra to
install" promise holds.
"""
from __future__ import annotations

import os
import json
import time
import shutil
import sqlite3
import threading

_LOCK = threading.Lock()


def _base_dir():
    d = os.path.join(os.path.expanduser("~"), ".phorg")
    os.makedirs(d, exist_ok=True)
    return d


def _db_path():
    return os.path.join(_base_dir(), "registrations.db")


def selfie_dir():
    d = os.path.join(_base_dir(), "events", "selfies")
    os.makedirs(d, exist_ok=True)
    return d


def _connect():
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS registrations(
  id         TEXT PRIMARY KEY,
  event      TEXT,
  root       TEXT,
  token      TEXT,
  cid        TEXT,
  name       TEXT,
  contact    TEXT,
  selfies    TEXT,                     -- JSON list of durable selfie paths
  atoken     TEXT,                     -- per-guest secret for the signed album link
  threshold  REAL,
  status     TEXT DEFAULT 'pending',   -- pending | matched | error | expired
  count      INTEGER,
  scanned    INTEGER,
  error      TEXT,
  notified   TEXT,                     -- sent | <reason> when delivery attempted
  job        TEXT,
  created_at INTEGER,
  matched_at INTEGER
);
CREATE TABLE IF NOT EXISTS results(
  reg_id TEXT,
  path   TEXT,
  score  REAL,
  PRIMARY KEY(reg_id, path)
);
CREATE INDEX IF NOT EXISTS idx_reg_root   ON registrations(root);
CREATE INDEX IF NOT EXISTS idx_reg_status ON registrations(status);
"""


def init():
    """Create the database and folders if they do not exist yet (idempotent)."""
    with _LOCK:
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            # Forward-compat: add columns introduced after the first release.
            for col in ("atoken TEXT", "notified TEXT"):
                try:
                    conn.execute(f"ALTER TABLE registrations ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        finally:
            conn.close()
    selfie_dir()


def persist_selfies(reg_id, paths):
    """Copy each selfie into a durable folder so it survives temp-cleanup and a
    restart.  Returns the list of new, durable paths."""
    out = []
    dst_dir = selfie_dir()
    for i, src in enumerate(paths or []):
        try:
            if not src or not os.path.isfile(src):
                continue
            ext = os.path.splitext(src)[1] or ".png"
            dst = os.path.join(dst_dir, f"{reg_id}_{i}{ext}")
            shutil.copy2(src, dst)
            out.append(dst)
        except OSError:
            continue
    return out


def add(reg_id, *, event, root, token, cid, name, contact, selfies,
        threshold, atoken=None, job=None):
    """Insert a new pending registration.  Written immediately so it survives a
    shutdown."""
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO registrations"
                "(id,event,root,token,cid,name,contact,selfies,atoken,threshold,"
                " status,count,scanned,error,job,created_at,matched_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?, 'pending',NULL,NULL,NULL,?,?,NULL)",
                (reg_id, event or "", os.path.abspath(root) if root else "",
                 token or "", cid or "", name or "Guest", contact or "",
                 json.dumps(list(selfies or [])), atoken or "",
                 float(threshold or 0.44),
                 job, int(time.time())))
            conn.commit()
        finally:
            conn.close()


def set_job(reg_id, job_id):
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("UPDATE registrations SET job=? WHERE id=?",
                         (job_id, reg_id))
            conn.commit()
        finally:
            conn.close()


def save_results(reg_id, result):
    """Store a finished match: status, counts and the matched photo paths."""
    matches = (result or {}).get("matches") or []
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("DELETE FROM results WHERE reg_id=?", (reg_id,))
            conn.executemany(
                "INSERT OR REPLACE INTO results(reg_id,path,score) VALUES(?,?,?)",
                [(reg_id, m.get("path"), float(m.get("score") or 0))
                 for m in matches if m.get("path")])
            conn.execute(
                "UPDATE registrations SET status='matched',count=?,scanned=?,"
                "error=NULL,matched_at=? WHERE id=?",
                (result.get("count"), result.get("scanned"),
                 int(time.time()), reg_id))
            conn.commit()
        finally:
            conn.close()


def save_error(reg_id, message):
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE registrations SET status='error',error=?,matched_at=? "
                "WHERE id=?", (str(message)[:300], int(time.time()), reg_id))
            conn.commit()
        finally:
            conn.close()


def set_notified(reg_id, status):
    """Record the delivery outcome ('sent' or a short reason) for the dashboard."""
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("UPDATE registrations SET notified=? WHERE id=?",
                         (str(status)[:60], reg_id))
            conn.commit()
        finally:
            conn.close()


def _row_to_dict(row, matches=None):
    d = dict(row)
    try:
        d["selfies"] = json.loads(d.get("selfies") or "[]")
    except (ValueError, TypeError):
        d["selfies"] = []
    if matches is not None:
        d["matches"] = matches
    return d


def list_for_root(root):
    """All registrations for an event folder, newest first, each with its
    matched photo paths (for the host dashboard)."""
    if not root:
        return []
    root = os.path.abspath(root)
    with _LOCK:
        conn = _connect()
        try:
            regs = conn.execute(
                "SELECT * FROM registrations WHERE root=? "
                "ORDER BY created_at DESC", (root,)).fetchall()
            out = []
            for r in regs:
                paths = [row["path"] for row in conn.execute(
                    "SELECT path FROM results WHERE reg_id=? ORDER BY score DESC",
                    (r["id"],)).fetchall()]
                out.append(_row_to_dict(r, matches=paths))
            return out
        finally:
            conn.close()


def pending():
    """Registrations whose match never finished (app closed mid-scan)."""
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM registrations WHERE status='pending' "
                "ORDER BY created_at").fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()


def album(reg_id, token):
    """Return a guest's album (registration + matched photos with scores) when
    the signed *token* matches, else ``None``.  Used by the durable album link."""
    if not reg_id or not token:
        return None
    with _LOCK:
        conn = _connect()
        try:
            r = conn.execute(
                "SELECT * FROM registrations WHERE id=? AND atoken=?",
                (reg_id, token)).fetchone()
            if not r:
                return None
            matches = [{"path": row["path"], "score": row["score"]}
                       for row in conn.execute(
                           "SELECT path,score FROM results WHERE reg_id=? "
                           "ORDER BY score DESC", (reg_id,)).fetchall()]
            return _row_to_dict(r, matches=matches)
        finally:
            conn.close()


def purge_expired(ttl_days=14):
    """Delete registrations (and their selfies/results) older than *ttl_days*."""
    cutoff = int(time.time()) - int(ttl_days) * 86400
    with _LOCK:
        conn = _connect()
        try:
            old = conn.execute(
                "SELECT id,selfies FROM registrations WHERE created_at<?",
                (cutoff,)).fetchall()
            for r in old:
                try:
                    for s in json.loads(r["selfies"] or "[]"):
                        if os.path.isfile(s):
                            os.remove(s)
                except (ValueError, TypeError, OSError):
                    pass
            ids = [r["id"] for r in old]
            if ids:
                q = ",".join("?" * len(ids))
                conn.execute(f"DELETE FROM results WHERE reg_id IN ({q})", ids)
                conn.execute(f"DELETE FROM registrations WHERE id IN ({q})", ids)
                conn.commit()
            return len(ids)
        finally:
            conn.close()
