"""
Per-file metadata cache (SHA-1 digests, perceptual hashes, …).

Re-reading a large photo/video library on every scan is the slow part of the
duplicate finders.  ``MetaCache`` stores computed values in a small SQLite DB
under ``<root>/.phorg`` so unchanged files are never processed twice.

An entry is keyed by ``(path, size, mtime_ns)`` and is only trusted while the
size *and* nanosecond modification time still match — any edit invalidates it.
Values are an arbitrary JSON-serialisable dict, so the same cache can hold a
file's ``sha1`` for byte-dedup and its ``ph``/``dh`` perceptual hashes.

The cache fails soft: if SQLite or the disk is unavailable it simply behaves as
an empty (no-op) cache and callers keep working, just without the speed-up.
"""
import os
import json


class MetaCache:
    def __init__(self, cache_dir, name="meta.db"):
        self.conn = None
        self.mem = {}          # path -> (size, mtime_ns, data_dict)
        self._pending = False
        try:
            import sqlite3
            os.makedirs(cache_dir, exist_ok=True)
            self.conn = sqlite3.connect(os.path.join(cache_dir, name))
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS meta("
                "path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, data TEXT)")
            for path, size, mtime, data in self.conn.execute(
                    "SELECT path, size, mtime, data FROM meta"):
                try:
                    self.mem[path] = (size, mtime, json.loads(data))
                except (ValueError, TypeError):
                    pass
        except Exception:
            self.conn = None

    def stat_key(self, native_path):
        """Return (size, mtime_ns) for a file, or None if it can't be read."""
        try:
            st = os.stat(native_path)
            return st.st_size, st.st_mtime_ns
        except OSError:
            return None

    def get(self, path, size, mtime):
        rec = self.mem.get(path)
        if rec and rec[0] == size and rec[1] == mtime:
            return rec[2]
        return None

    def put(self, path, size, mtime, data):
        self.mem[path] = (size, mtime, data)
        if self.conn is not None:
            try:
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta(path, size, mtime, data) "
                    "VALUES(?,?,?,?)", (path, size, mtime, json.dumps(data)))
                self._pending = True
            except Exception:
                pass

    def close(self):
        if self.conn is not None:
            try:
                if self._pending:
                    self.conn.commit()
                self.conn.close()
            except Exception:
                pass
        self.conn = None
