"""
Persistent people database — remember faces by name across runs.

When you name someone (via auto-grouping), their average face embedding is
stored here so future scans recognise them automatically, with no need to point
at sample folders again.  It's a single small JSON file in the user's home so it
works across every folder and workspace, and it never leaves the machine.
"""
import os
import json
import threading

_DIR = os.path.join(os.path.expanduser("~"), ".phorg")
_PATH = os.path.join(_DIR, "people.json")
_LOCK = threading.Lock()


def load():
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(db):
    try:
        os.makedirs(_DIR, exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f)
        os.replace(tmp, _PATH)
    except OSError:
        pass


def summary():
    """List of {name, n} (n = how many photos taught this face)."""
    db = load()
    return [{"name": k, "n": int(v.get("n", 0))}
            for k, v in sorted(db.items())]


def remember(name, embedding, count=1):
    """Add or merge a person's unit-norm face embedding (running average)."""
    import numpy as np
    name = (name or "").strip()
    if not name or embedding is None:
        return
    v = np.asarray(embedding, dtype="float32").flatten()
    nrm = float(np.linalg.norm(v))
    if nrm <= 0:
        return
    v = v / nrm
    with _LOCK:
        db = load()
        if name in db and db[name].get("emb"):
            old = np.asarray(db[name]["emb"], dtype="float32")
            oc = max(1, int(db[name].get("n", 1)))
            merged = old * oc + v * max(1, int(count))
            mn = float(np.linalg.norm(merged))
            if mn > 0:
                merged = merged / mn
            db[name] = {"emb": merged.tolist(), "n": oc + max(1, int(count))}
        else:
            db[name] = {"emb": v.tolist(), "n": max(1, int(count))}
        _save(db)


def forget(name):
    with _LOCK:
        db = load()
        if db.pop((name or "").strip(), None) is not None:
            _save(db)


def refs():
    """[(name, unit_embedding)] for matching / auto-suggesting names."""
    import numpy as np
    out = []
    for k, v in load().items():
        e = v.get("emb")
        if e:
            out.append((k, np.asarray(e, dtype="float32")))
    return out
