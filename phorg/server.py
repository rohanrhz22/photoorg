"""
phorg web UI — a zero-dependency local server.

Wraps the existing planners/backends in a small JSON API and serves a modern
single-page interface (``ui.html``).  Uses only the Python standard library so
the project keeps its "nothing to install" promise.

Safety model is preserved end-to-end:
  * ``/api/plan`` builds the exact list of Ops and stores it server-side.
  * ``/api/apply`` runs *that stored plan* (never a re-computed one) and then
    re-counts files to prove nothing was lost.

Nothing is ever deleted — the engine only moves files and rmdirs empty folders.
"""
import os
import sys
import re
import json
import time
import uuid
import string
import socket
import posixpath
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import organizer, report
from .backends import (LocalBackend, AdbBackend, _find_adb, _first_device)
from .safety import SafetyPolicy


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

# In-memory store of previewed plans: id -> {ops, params, label}
_PLANS = {}
_PLANS_LOCK = threading.Lock()

# Undo history (last applied operation per root) + folders allowed for thumbs
_HISTORY = {}
_REDO = {}
_HISTORY_LOCK = threading.Lock()
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
# Guest sharing ("FaceFind portal") — let people on the same Wi-Fi open a
# link, submit a selfie, and get their own photos. Off by default.
# --------------------------------------------------------------------------
_SHARE = {"enabled": False, "root": None, "event": "", "token": None,
          "guests": [], "online": False, "public_url": None,
          "public_host": None}
_SHARE_LOCK = threading.Lock()
_SERVER_PORT = 8765

# Endpoints a non-local (guest) visitor is allowed to call.  Everything else
# (scan/plan/apply/undo/...) stays host-only even while sharing is on.
_GUEST_ROUTES = {
    "/api/health", "/api/share/status", "/api/share/find/start",
    "/api/facefind/selfie", "/api/cluster/progress", "/api/cluster/cancel",
}


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



def _history_file(root):
    return os.path.join(os.path.abspath(root), ".phorg", "history.json")


def _persist_history(root, lst):
    try:
        hf = _history_file(root)
        os.makedirs(os.path.dirname(hf), exist_ok=True)
        with open(hf, "w", encoding="utf-8") as f:
            json.dump(lst, f)
    except OSError:
        pass


def _save_history(be, journal, label="operation"):
    """Append the journal of a just-applied operation to the undo stack."""
    if be.name != "local" or not journal:
        return None
    ap = os.path.abspath(be.root)
    entry = {"id": uuid.uuid4().hex, "ts": int(time.time()), "label": label,
             "journal": journal,
             "moves": sum(1 for j in journal
                          if j.get("op") in ("move", "copy", "compress",
                                             "dedupe"))}
    with _HISTORY_LOCK:
        lst = list(_HISTORY.get(ap) or [])
        lst.append(entry)
        lst = lst[-20:]
        _HISTORY[ap] = lst
        _REDO.pop(ap, None)   # a new operation invalidates the redo stack
    _persist_history(be.root, lst)
    return entry


def _load_history(root):
    """Return the undo stack (list) for a root, from memory or disk."""
    ap = os.path.abspath(root)
    with _HISTORY_LOCK:
        lst = _HISTORY.get(ap)
    if lst is not None:
        return lst
    try:
        with open(_history_file(root), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):        # legacy single-record format
        data = [data]
    if not isinstance(data, list):
        return []
    with _HISTORY_LOCK:
        _HISTORY[ap] = data
    return data


# --------------------------------------------------------------------------
# Helpers to build backends / safety from a plain params dict (from the UI)
# --------------------------------------------------------------------------
def _build_backend(p):
    backend = (p.get("backend") or "local").lower()
    if backend == "adb":
        adb = p.get("adb") or _find_adb()
        serial = p.get("device") or _first_device(adb)
        if not serial:
            raise ValueError("No Android device found. Connect the phone, enable "
                             "USB debugging and tap 'Allow'.")
        root = p.get("root") or "/storage/emulated/0"
        return AdbBackend(adb, serial, root)
    root = p.get("root")
    if not root:
        raise ValueError("Please choose a folder to organise.")
    if not os.path.isdir(root):
        raise ValueError(f"Folder not found: {root}")
    return LocalBackend(root)


def _build_safety(p):
    protect = p.get("protect") or []
    if isinstance(protect, str):
        protect = [x.strip() for x in protect.split(",") if x.strip()]
    return SafetyPolicy(include_hidden=bool(p.get("include_hidden")),
                        extra_protect=protect)


def _op_dict(op):
    return {"kind": op.kind, "a": op.a, "b": op.b}


# --------------------------------------------------------------------------
# API actions
# --------------------------------------------------------------------------
def api_health(_):
    return {"ok": True, "app": "phorg", "version": 1}


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


def api_devices(p):
    adb = p.get("adb") or _find_adb()
    import subprocess
    devices = []
    try:
        out = subprocess.run([adb, "devices", "-l"], capture_output=True,
                             text=True).stdout
        for line in out.splitlines()[1:]:
            line = line.strip()
            if line and ("\tdevice" in line or line.endswith("device")):
                serial = line.split()[0]
                if serial and serial != "*":
                    model = ""
                    for tok in line.split():
                        if tok.startswith("model:"):
                            model = tok.split(":", 1)[1].replace("_", " ")
                    devices.append({"serial": serial, "model": model})
    except FileNotFoundError:
        raise ValueError(f"adb not found at '{adb}'. Install Android "
                         "platform-tools or set the adb path.")
    return {"adb": adb, "devices": devices}


def api_scan(p):
    be = _build_backend(p)
    depth = int(p.get("depth") or 3)
    tree, meta = be.scan(be.root, max_depth=depth)
    tree = sorted(tree, key=lambda n: -n["kb"])
    if be.name == "local":
        _register_root(be.root)
    return {"backend": be.name, "root": be.root, "tree": tree, "meta": meta}


_PLANNERS = {
    "junk":      lambda be, saf, p: organizer.plan_junk(
                     be, be.root, saf, recursive=bool(p.get("recursive")),
                     extra_exts=_rules_junk(p)),
    "type":      lambda be, saf, p: organizer.plan_by_type(
                     be, be.root, saf, recursive=bool(p.get("recursive")),
                     ext_overrides=_rules_ext(p)),
    "size":      lambda be, saf, p: organizer.plan_by_size(
                     be, be.root, saf, recursive=bool(p.get("recursive"))),
    "rename":    lambda be, saf, p: organizer.plan_rename(
                     be, be.root, saf, template=(p.get("template") or "{orig}"),
                     recursive=bool(p.get("recursive")),
                     start=int(p.get("start") or 1), pad=int(p.get("pad") or 3),
                     only_ext=(p.get("only_ext") or None)),
    "fix-ext":   lambda be, saf, p: organizer.plan_fix_extensions(
                     be, be.root, saf, recursive=p.get("recursive", True),
                     sort=bool(p.get("sort"))),
    "empties":   lambda be, saf, p: organizer.plan_empty_folders(be, be.root, saf),
    "date":      lambda be, saf, p: organizer.plan_by_date(
                     be, be.root, saf, granularity=p.get("granularity", "month"),
                     recursive=bool(p.get("recursive")),
                     use_exif=p.get("use_exif", True)),
    "dedupe":    lambda be, saf, p: organizer.plan_duplicate_files(be, be.root, saf),
    "similar-names": lambda be, saf, p: organizer.plan_similar_names(
                     be, be.root, saf, recursive=p.get("recursive", True)),
    "cleanup":   lambda be, saf, p: organizer.plan_cleanup(
                     be, be.root, saf, recursive=bool(p.get("recursive")),
                     extra_exts=_rules_junk(p), ext_overrides=_rules_ext(p)),
    "flatten":   lambda be, saf, p: organizer.plan_flatten(
                     be, be.root, saf, remove_emptied=p.get("remove_emptied", True)),
    "location":  lambda be, saf, p: organizer.plan_by_location(
                     be, be.root, saf, recursive=bool(p.get("recursive"))),
}


def _rules_ext(p):
    r = p.get("rules") or {}
    m = r.get("ext") or {}
    return {str(k).lower().lstrip("."): v for k, v in m.items() if v}


def _rules_junk(p):
    r = p.get("rules") or {}
    return set(str(x).lower().lstrip(".") for x in (r.get("junkExts") or []) if x)


def _rel_folder(root, path):
    """Folder of *path* relative to *root* ('' == the root itself)."""
    r = os.path.abspath(root).replace(os.sep, "/").rstrip("/")
    d = posixpath.dirname((path or "").replace(os.sep, "/"))
    if d == r:
        return ""
    if d.startswith(r + "/"):
        return d[len(r) + 1:]
    return d


def _source_folders(ops, root):
    """Distinct source folders of a plan's move ops, with counts."""
    from collections import defaultdict
    counts = defaultdict(int)
    for o in ops:
        if o.kind == "move":
            counts[_rel_folder(root, o.a)] += 1
    return sorted(({"folder": f, "count": c} for f, c in counts.items()),
                  key=lambda x: (-x["count"], x["folder"]))


def _filter_ops(ops, root, exclude):
    """Drop move ops whose source folder is in the excluded set."""
    if not exclude:
        return ops
    ex = set(exclude)
    out = []
    for o in ops:
        if o.kind == "move" and _rel_folder(root, o.a) in ex:
            continue
        out.append(o)
    return out


def api_plan(p):
    command = p.get("command")
    if command not in _PLANNERS:
        raise ValueError(f"Unknown operation: {command}")
    be = _build_backend(p)
    saf = _build_safety(p)
    ops, summary = _PLANNERS[command](be, saf, p)

    counts = {
        "moves": sum(1 for o in ops if o.kind == "move"),
        "mkdirs": sum(1 for o in ops if o.kind == "mkdir"),
        "rmdirs": sum(1 for o in ops if o.kind == "rmdir"),
    }
    plan_id = uuid.uuid4().hex
    with _PLANS_LOCK:
        _PLANS[plan_id] = {"ops": ops, "params": p, "label": command}
        # keep the store from growing without bound
        if len(_PLANS) > 50:
            for k in list(_PLANS.keys())[:-50]:
                _PLANS.pop(k, None)

    preview = [_op_dict(o) for o in ops[:200]]
    return {"planId": plan_id, "command": command, "summary": summary,
            "counts": counts, "total": len(ops), "preview": preview,
            "sourceFolders": _source_folders(ops, be.root),
            "backend": be.name, "root": be.root}


def api_apply(p):
    plan_id = p.get("planId")
    with _PLANS_LOCK:
        entry = _PLANS.get(plan_id)
    if not entry:
        raise ValueError("This plan expired. Please preview again before applying.")
    ops = entry["ops"]
    label = entry["label"]
    be = _build_backend(entry["params"])
    copy_mode = bool(entry["params"].get("copy"))
    ops = _filter_ops(ops, be.root, p.get("exclude"))

    log_lines = []
    before = organizer.count_files(be, be.root)
    journal = []
    done = be.apply_ops(ops, log=log_lines.append, journal=journal,
                        copy_mode=copy_mode)
    after = organizer.count_files(be, be.root)

    moves = sum(1 for j in journal if j.get("op") == "move")
    copies = sum(1 for j in journal if j.get("op") == "copy")
    if copy_mode:
        verified = (after == before + copies)
    else:
        verified = None if label == "empties" else (before == after)

    with _PLANS_LOCK:
        _PLANS.pop(plan_id, None)

    if moves or copies:
        _save_history(be, journal, label=label)
    return {"applied": done, "before": before, "after": after,
            "verified": verified, "log": log_lines,
            "merged": getattr(be, "merged_skips", 0),
            "copied": copies, "copyMode": copy_mode,
            "movedSummary": _moved_summary(be, journal),
            "undo": {"available": bool(moves or copies) and be.name == "local",
                     "count": moves or copies, "root": be.root}}


def _entry_dst_folder(root, j):
    """Top-level destination folder a journal entry landed a file in."""
    op = j.get("op")
    if op in ("move", "copy", "dedupe"):
        pth = j.get("dst")
    elif op == "compress":
        pth = j.get("written")
    else:
        return None
    f = _rel_folder(root, pth or "")
    return f.split("/")[0] if f else ""


def api_history_folders(p):
    """List the destination folders of one history entry (for partial undo)."""
    root = p.get("root")
    lst = _load_history(root or "")
    undo_id = p.get("id")
    entry = None
    if undo_id:
        entry = next((e for e in lst if e.get("id") == undo_id), None)
    elif lst:
        entry = lst[-1]
    if not entry:
        return {"folders": []}
    from collections import defaultdict
    counts = defaultdict(int)
    for j in entry.get("journal", []):
        if j.get("op") in ("move", "copy", "compress", "dedupe"):
            counts[_entry_dst_folder(root, j)] += 1
    folders = sorted(({"folder": k, "count": v} for k, v in counts.items()),
                     key=lambda x: (-x["count"], x["folder"]))
    return {"folders": folders, "id": entry.get("id"),
            "label": entry.get("label")}


def api_undo(p):
    """Reverse an applied operation for a root.  With ``folders`` given, only
    reverse the moves that landed in those destination folders (partial undo)."""
    root = p.get("root")
    if not root:
        raise ValueError("No folder specified to undo.")
    undo_id = p.get("id")
    sel = p.get("folders")
    sel_set = set(sel) if sel else None
    lst = _load_history(root)
    if not lst:
        raise ValueError("Nothing to undo for this folder.")
    idx = len(lst) - 1
    if undo_id:
        idx = next((i for i, e in enumerate(lst) if e.get("id") == undo_id), None)
        if idx is None:
            raise ValueError("That operation is no longer in the history.")
    entry = lst[idx]
    journal = entry["journal"]
    restored = 0
    log = []
    import shutil

    def reverse_one(j):
        op = j.get("op")
        if op == "compress":
            written = j.get("written", "").replace("/", os.sep)
            backup = j.get("backup", "").replace("/", os.sep)
            orig = j.get("orig", "").replace("/", os.sep)
            try:
                if written and os.path.isfile(written) and \
                        os.path.abspath(written) != os.path.abspath(backup):
                    os.remove(written)
            except OSError as e:
                log.append(f"{written}: {e}")
            try:
                if backup and os.path.isfile(backup):
                    os.makedirs(os.path.dirname(orig), exist_ok=True)
                    shutil.move(backup, orig)
                    return 1
            except OSError as e:
                log.append(f"{backup}: {e}")
            return 0
        if op == "copy":
            dst = j["dst"].replace("/", os.sep)
            try:
                if os.path.isfile(dst):
                    os.remove(dst)
                    return 1
            except OSError as e:
                log.append(f"{dst}: {e}")
            return 0
        if op == "dedupe":
            # the source was removed because a byte-identical copy already sat
            # at dst — restore it by copying that surviving copy back.
            src = j["src"].replace("/", os.sep)
            dst = j["dst"].replace("/", os.sep)
            try:
                if os.path.isfile(dst) and not os.path.exists(src):
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    shutil.copy2(dst, src)
                    return 1
            except OSError as e:
                log.append(f"{src}: {e}")
            return 0
        # move
        src = j["src"].replace("/", os.sep)
        dst = j["dst"].replace("/", os.sep)
        try:
            if os.path.exists(dst) and not os.path.exists(src):
                os.makedirs(os.path.dirname(src), exist_ok=True)
                shutil.move(dst, src)
                return 1
        except Exception as e:
            log.append(f"{dst}: {e}")
        return 0

    remaining = []
    undone = []
    for j in reversed(journal):
        op = j.get("op")
        if op in ("move", "copy", "compress", "dedupe"):
            if sel_set is not None and _entry_dst_folder(root, j) not in sel_set:
                remaining.append(j)
                continue
            if reverse_one(j):
                restored += 1
                undone.append(j)
            else:
                remaining.append(j)
        else:
            remaining.append(j)          # mkdir etc. — kept for reconstruction
    remaining = list(reversed(remaining))

    # remove any folders we created that are now empty (safe: only empties go)
    made = [j["path"].replace("/", os.sep) for j in journal
            if j.get("op") == "mkdir"]
    for d in sorted(made, key=lambda x: x.count(os.sep), reverse=True):
        try:
            os.rmdir(d)
        except OSError:
            pass
    bdir = os.path.join(os.path.abspath(root), "Originals_Backup")
    if os.path.isdir(bdir):
        for dp, _dn, _fn in os.walk(bdir, topdown=False):
            try:
                os.rmdir(dp)
            except OSError:
                pass

    ap = os.path.abspath(root)
    partial_left = any(j.get("op") in ("move", "copy", "compress", "dedupe")
                       for j in remaining)
    if sel_set is not None and partial_left:
        entry["journal"] = remaining
        entry["moves"] = sum(1 for j in remaining
                             if j.get("op") in ("move", "copy", "compress",
                                                "dedupe"))
        lst[idx] = entry
        redo_entry = {"id": uuid.uuid4().hex, "ts": entry["ts"],
                      "label": entry["label"], "journal": undone,
                      "moves": restored}
    else:
        lst = lst[:idx] + lst[idx + 1:]
        redo_entry = entry
    with _HISTORY_LOCK:
        _HISTORY[ap] = lst
        _REDO.setdefault(ap, []).append(redo_entry)
    _persist_history(root, lst)
    nxt = lst[-1] if lst else None
    return {"restored": restored, "log": log, "label": entry.get("label"),
            "remaining": len(lst), "partial": bool(sel_set),
            "undoable": ({"id": nxt["id"], "label": nxt["label"],
                          "count": nxt["moves"]} if nxt else None)}


def api_redo(p):
    """Re-apply the most recently undone operation for a root."""
    root = p.get("root")
    if not root:
        raise ValueError("No folder specified to redo.")
    ap = os.path.abspath(root)
    with _HISTORY_LOCK:
        stack = _REDO.get(ap) or []
        entry = stack.pop() if stack else None
    if not entry:
        raise ValueError("Nothing to redo.")
    journal = entry["journal"]
    import shutil
    redone = 0
    log = []
    for j in journal:
        if j.get("op") == "mkdir":
            try:
                os.makedirs(j["path"].replace("/", os.sep), exist_ok=True)
            except OSError:
                pass
        elif j.get("op") == "copy":
            src = j["src"].replace("/", os.sep)
            dst = j["dst"].replace("/", os.sep)
            try:
                if os.path.exists(src) and not os.path.exists(dst):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    redone += 1
            except Exception as e:
                log.append(f"{src}: {e}")
        elif j.get("op") == "move":
            src = j["src"].replace("/", os.sep)
            dst = j["dst"].replace("/", os.sep)
            try:
                if os.path.exists(src) and not os.path.exists(dst):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.move(src, dst)
                    redone += 1
            except Exception as e:
                log.append(f"{src}: {e}")
        elif j.get("op") == "dedupe":
            # re-apply the dedupe: drop the redundant source again, but only
            # while the identical copy at dst still exists.
            src = j["src"].replace("/", os.sep)
            dst = j["dst"].replace("/", os.sep)
            try:
                if os.path.isfile(src) and os.path.isfile(dst):
                    os.remove(src)
                    redone += 1
            except Exception as e:
                log.append(f"{src}: {e}")
    with _HISTORY_LOCK:
        lst = list(_HISTORY.get(ap) or [])
        lst.append(entry)
        _HISTORY[ap] = lst[-20:]
    _persist_history(root, lst)
    return {"redone": redone, "log": log, "label": entry.get("label")}


def api_history(p):
    ap = os.path.abspath(p.get("root") or "")
    lst = _load_history(p.get("root") or "")
    items = [{"id": e["id"], "ts": e["ts"], "label": e["label"],
              "moves": e["moves"]} for e in lst]
    with _HISTORY_LOCK:
        redo = list(_REDO.get(ap) or [])
    return {"items": items,
            "undoableId": items[-1]["id"] if items else None,
            "redoable": ({"label": redo[-1]["label"], "count": redo[-1]["moves"]}
                         if redo else None)}


def api_report(p):
    be = _build_backend(p)
    depth = int(p.get("depth") or 3)
    tree, meta = be.scan(be.root, max_depth=depth)
    outs = p.get("out")
    if not outs:
        default = os.path.join(os.path.expanduser("~"), "Desktop",
                               "phone_storage_report.html")
        try:
            os.makedirs(os.path.dirname(default), exist_ok=True)
        except OSError:
            default = os.path.abspath("phone_storage_report.html")
        outs = [default]
    if isinstance(outs, str):
        outs = [outs]
    written, rmeta = report.build_report(tree, meta, outs, root_label=be.root)
    return {"written": [os.path.abspath(w) for w in written],
            "meta": {k: rmeta[k] for k in ("nL1", "nL2", "nL3", "generated")
                     if k in rmeta}}


def api_verify(p):
    be = _build_backend(p)
    n = organizer.count_files(be, be.root)
    return {"count": n, "root": be.root}


def api_open(p):
    """Open a generated report (or its folder) in the OS default app.
    With reveal=True, highlight the file inside its folder."""
    path = p.get("path")
    reveal = bool(p.get("reveal"))
    if not path or not os.path.exists(path):
        raise ValueError("File not found.")
    try:
        if reveal and os.path.isfile(path):
            if os.name == "nt":
                import subprocess
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                import subprocess
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
# Photo categorizer (content-based image sorting) — background job + progress
# --------------------------------------------------------------------------
_JOBS = {}
_JOBS_LOCK = threading.Lock()


def api_vision_status(_p):
    from . import vision
    missing = vision.check_deps()
    return {
        "available": not missing,
        "missing": missing,
        "bride_supported": (not missing) and vision.bride_supported(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "categories": vision.CATEGORIES,
    }


def api_vision_install(_p):
    """Install the optional image-analysis packages via pip (source runs only)."""
    if getattr(sys, "frozen", False):
        raise ValueError("Automatic install isn't available in the packaged app. "
                         "Run phorg from source to use photo categorization.")
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


def _vision_options(p):
    cats = ("blurry", "single", "couple", "group", "scenery", "duplicates",
            "similar", "screenshot", "document", "best", "video")
    enabled = {c: bool(p.get(c)) for c in cats}
    people = []
    for person in (p.get("people") or []):
        name = (person.get("name") or "").strip()
        samples = person.get("samples")
        if name and samples:
            people.append({"name": name, "samples": samples})
    # backward compatibility with the old single-"bride" field
    if p.get("bride") and p.get("bride_samples"):
        people.append({"name": "Bride", "samples": p.get("bride_samples")})
    enabled["people"] = bool(people)
    return {
        "enabled": enabled,
        "recursive": bool(p.get("recursive")),
        "blur_threshold": float(p.get("blur_threshold") or 50.0),
        "dup_distance": int(p.get("dup_distance") or 8),
        "people": people,
        "person_threshold": float(p.get("person_threshold") or 78.0),
        "limit": int(p["limit"]) if p.get("limit") else None,
        "similar_distance": int(p.get("similar_distance") or 16),
        "best_threshold": float(p.get("best_threshold") or 0.72),
    }


def api_categorize_start(p):
    if (p.get("backend") or "local").lower() != "local":
        raise ValueError("Photo categorization currently works on local folders "
                         "only (analysing thousands of phone files over USB is "
                         "too slow). Copy the photos to your PC first.")
    from . import vision
    missing = vision.check_deps()
    if missing:
        raise ValueError("Missing packages: " + ", ".join(missing))
    be = _build_backend(p)
    saf = _build_safety(p)
    options = _vision_options(p)
    if not any(options["enabled"].values()):
        raise ValueError("Select at least one category to sort into.")
    _register_root(be.root)

    job_id = uuid.uuid4().hex
    job = {"done": 0, "total": 0, "tally": {}, "phase": "starting",
           "finished": False, "error": None, "result": None, "cancel": False}
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        for k in list(_JOBS.keys())[:-20]:  # cap history
            _JOBS.pop(k, None)

    def run():
        try:
            def prog(done, total, tally):
                job["done"] = done
                job["total"] = total
                job["tally"] = tally
                job["phase"] = "analyzing"

            def cancelled():
                return job["cancel"]

            ops, summary = organizer.plan_categorize_images(
                be, be.root, saf, options, progress=prog, cancel=cancelled)

            plan_id = uuid.uuid4().hex
            with _PLANS_LOCK:
                _PLANS[plan_id] = {"ops": ops, "params": p, "label": "photos"}
            counts = {
                "moves": sum(1 for o in ops if o.kind == "move"),
                "mkdirs": sum(1 for o in ops if o.kind == "mkdir"),
                "rmdirs": 0,
            }
            job["result"] = {
                "planId": plan_id, "summary": summary, "counts": counts,
                "total": len(ops),
                "preview": [_op_dict(o) for o in ops[:200]],
                "cancelled": job["cancel"],
            }
            job["phase"] = "done"
        except Exception as e:  # pragma: no cover - defensive
            job["error"] = f"{type(e).__name__}: {e}"
            job["phase"] = "error"
        finally:
            job["finished"] = True

    threading.Thread(target=run, daemon=True).start()
    return {"jobId": job_id}


def api_categorize_progress(p):
    with _JOBS_LOCK:
        job = _JOBS.get(p.get("jobId"))
    if not job:
        raise ValueError("Analysis job not found (it may have expired).")
    return {k: job[k] for k in ("done", "total", "tally", "phase",
                                "finished", "error", "result")}


def api_categorize_cancel(p):
    with _JOBS_LOCK:
        job = _JOBS.get(p.get("jobId"))
    if job:
        job["cancel"] = True
    return {"ok": True}


def _build_photo_ops(p):
    """Build (backend, ops, skipped) from photo->category assignments."""
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


def _moved_summary(be, journal):
    """Group applied moves/copies by destination folder (relative to root)."""
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
            before = organizer.count_files(be, be.root)
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
            after = organizer.count_files(be, be.root)
            moves = sum(1 for j in journal if j.get("op") == "move")
            copies = sum(1 for j in journal if j.get("op") == "copy")
            if moves or copies:
                _save_history(be, journal, label=label)
            if copy_mode:
                verified = (after == before + copies)
            else:
                verified = None if label == "empties" else (before == after)
            job["result"] = {
                "applied": done, "before": before, "after": after,
                "verified": verified, "log": log, "cancelled": job["cancel"],
                "merged": getattr(be, "merged_skips", 0),
                "copied": copies, "copyMode": copy_mode,
                "movedSummary": _moved_summary(be, journal),
                "undo": {"available": bool(moves or copies) and be.name == "local",
                         "count": moves or copies, "root": be.root}}
            job["phase"] = "done"
        except Exception as e:  # pragma: no cover - defensive
            job["error"] = f"{type(e).__name__}: {e}"
            job["phase"] = "error"
        finally:
            job["finished"] = True

    threading.Thread(target=run, daemon=True).start()
    return {"jobId": job_id}


def api_apply_start(p):
    plan_id = p.get("planId")
    with _PLANS_LOCK:
        entry = _PLANS.get(plan_id)
    if not entry:
        raise ValueError("This plan expired. Please preview again before applying.")
    be = _build_backend(entry["params"])
    with _PLANS_LOCK:
        _PLANS.pop(plan_id, None)
    ops = _filter_ops(entry["ops"], be.root, p.get("exclude"))
    return _start_apply_job(be, ops, entry["label"],
                            copy_mode=bool(entry["params"].get("copy")))


def api_categorize_apply_start(p):
    if (p.get("backend") or "local").lower() != "local":
        raise ValueError("Photo categorization works on local folders only.")
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
    return _start_apply_job(be, ops, "photos", copy_mode=bool(p.get("copy")))


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


def api_categorize_apply(p):
    """Synchronous apply of photo->category assignments (small batches)."""
    be, ops, skipped = _build_photo_ops(p)
    if not ops:
        return {"applied": 0, "verified": True, "undo": {"available": False},
                "skipped": skipped}
    log = []
    before = organizer.count_files(be, be.root)
    journal = []
    done = be.apply_ops(ops, log=log.append, journal=journal)
    after = organizer.count_files(be, be.root)
    moves = sum(1 for j in journal if j.get("op") == "move")
    if moves:
        _save_history(be, journal, label="photos")
    return {"applied": done, "before": before, "after": after,
            "verified": before == after, "log": log, "skipped": skipped,
            "undo": {"available": bool(moves), "count": moves, "root": be.root}}



# --------------------------------------------------------------------------
# Automatic face grouping (no sample photos) — background job
# --------------------------------------------------------------------------
def api_cluster_status(_p):
    from . import vision
    missing = vision.check_deps()
    return {
        "available": (not missing) and vision.cluster_api_available(),
        "deps_missing": missing,
        "api": (not missing) and vision.cluster_api_available(),
        "models": (not missing) and vision.models_present(),
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def api_cluster_start(p):
    if (p.get("backend") or "local").lower() != "local":
        raise ValueError("Auto-grouping works on local folders only.")
    from . import vision
    missing = vision.check_deps()
    if missing:
        raise ValueError("Missing packages: " + ", ".join(missing))
    if not vision.cluster_api_available():
        raise ValueError("Your OpenCV build lacks the face modules needed for "
                         "auto-grouping (needs opencv-contrib-python).")
    be = _build_backend(p)
    saf = _build_safety(p)
    _register_root(be.root)
    files = organizer.list_image_files(be, be.root, saf, bool(p.get("recursive")))
    native = [be.native(f) for f in files]

    # optional: named people to auto-label matching clusters
    people = []
    for person in (p.get("people") or []):
        name = (person.get("name") or "").strip()
        samples = person.get("samples")
        if name and samples:
            people.append({"name": name, "samples": samples})

    job_id = uuid.uuid4().hex
    job = {"done": 0, "total": len(native), "phase": "starting", "clusters": 0,
           "finished": False, "error": None, "result": None, "cancel": False,
           "model_done": 0, "model_total": 0, "model_name": ""}
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
            refs = vision.embed_people(people) if people else None
            job["phase"] = "clustering"

            def prog(done, total, nclusters):
                job["done"] = done
                job["total"] = total
                job["clusters"] = nclusters

            def cancelled():
                return job["cancel"]

            clusters = vision.cluster_faces(
                native, threshold=float(p.get("threshold") or 0.363),
                person_refs=refs, progress=prog, cancel=cancelled)
            job["result"] = {"clusters": clusters, "analyzed": len(native)}
            job["phase"] = "done"
        except Exception as e:  # pragma: no cover - defensive
            job["error"] = f"{type(e).__name__}: {e}"
            job["phase"] = "error"
        finally:
            job["finished"] = True

    threading.Thread(target=run, daemon=True).start()
    return {"jobId": job_id}


def api_cluster_progress(p):
    with _JOBS_LOCK:
        job = _JOBS.get(p.get("jobId"))
    if not job:
        raise ValueError("Grouping job not found (it may have expired).")
    return {k: job[k] for k in ("done", "total", "phase", "clusters",
                                "finished", "error", "result",
                                "model_done", "model_total", "model_name")}


def api_cluster_cancel(p):
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
    import base64
    import tempfile
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


def _start_facefind_job(root, selfie, threshold, recursive=True):
    """Start a background FaceFind job over *root*; returns {'jobId'}."""
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
           "model_done": 0, "model_total": 0, "model_name": ""}
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

            res = vision.facefind(native, selfie, threshold=threshold,
                                  progress=prog, cancel=cancelled)
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
    selfie = p.get("selfie")
    if not selfie or not os.path.isfile(selfie):
        raise ValueError("Please add a clear selfie photo first.")
    be = _build_backend(p)
    return _start_facefind_job(be.root, selfie, float(p.get("threshold") or 0.40),
                               bool(p.get("recursive", True)))


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
                         "(open Photo AI once and install AI support).")
    _register_root(root)
    online = bool(p.get("online"))
    public_url = public_host = None
    if online:
        from . import tunnel
        info = tunnel.start(_SERVER_PORT)     # raises on failure
        public_url = info["url"]
        public_host = info["host"]
    token = uuid.uuid4().hex[:10]
    with _SHARE_LOCK:
        _SHARE.update({"enabled": True, "root": os.path.abspath(root),
                       "event": (p.get("event") or "Our Event").strip()[:80],
                       "token": token, "guests": [], "online": online,
                       "public_url": public_url, "public_host": public_host})
        event = _SHARE["event"]
    ips = _lan_ips()
    return {"ok": True, "token": token, "event": event, "port": _SERVER_PORT,
            "ips": ips, "urls": _share_urls(token), "online": online,
            "public_url": public_url}


def api_share_disable(_p):
    with _SHARE_LOCK:
        was_online = _SHARE.get("online")
        _SHARE.update({"enabled": False, "root": None, "event": "",
                       "token": None, "guests": [], "online": False,
                       "public_url": None, "public_host": None})
    if was_online:
        try:
            from . import tunnel
            tunnel.stop()
        except Exception:
            pass
    return {"ok": True, "enabled": False}


def api_share_status(p):
    token = p.get("token")
    with _SHARE_LOCK:
        enabled = _SHARE["enabled"]
        event = _SHARE["event"]
        cur = _SHARE["token"]
        online = _SHARE.get("online")
        public_url = _SHARE.get("public_url")
    if not enabled:
        return {"enabled": False}
    if token is not None:                     # a guest checking their link
        ok = (token == cur)
        return {"enabled": ok, "event": event if ok else ""}
    ips = _lan_ips()                          # host asking for the link
    return {"enabled": True, "event": event, "token": cur,
            "port": _SERVER_PORT, "ips": ips, "urls": _share_urls(cur),
            "online": bool(online), "public_url": public_url}


def api_share_find_start(p):
    token = p.get("token")
    with _SHARE_LOCK:
        if not _SHARE["enabled"] or token != _SHARE["token"]:
            raise ValueError("This photo link is no longer active.")
        root = _SHARE["root"]
    selfie = p.get("selfie")
    if not selfie or not os.path.isfile(selfie):
        raise ValueError("Please add a clear selfie first.")
    r = _start_facefind_job(root, selfie, float(p.get("threshold") or 0.40),
                            True)
    name = (p.get("name") or "").strip()[:60] or "Guest"
    with _SHARE_LOCK:
        _SHARE.setdefault("guests", []).append(
            {"name": name, "ts": int(time.time()), "job": r["jobId"]})
        _SHARE["guests"] = _SHARE["guests"][-200:]
    return r


def api_share_guests(_p):
    """Host-only: who has searched, and how many photos each found."""
    with _SHARE_LOCK:
        guests = list(_SHARE.get("guests") or [])
    out = []
    for g in guests:
        cnt = None
        done = False
        with _JOBS_LOCK:
            job = _JOBS.get(g.get("job"))
        if job and job.get("finished"):
            done = True
            cnt = (job.get("result") or {}).get("count")
        out.append({"name": g["name"], "ts": g["ts"],
                    "count": cnt, "done": done})
    out.reverse()
    return {"guests": out, "count": len(out)}


# ==========================================================================
# Rename live-preview sample, duplicate groups, search
# ==========================================================================
def api_rename_sample(p):
    be = _build_backend(p)
    saf = _build_safety(p)
    recursive = bool(p.get("recursive"))
    only = (p.get("only_ext") or "").lower().lstrip(".")
    import datetime as _dt
    out = []
    for fp, name, size in organizer._iter_candidate_files(
            be, be.root, saf, recursive):
        ext = organizer.classify.ext_of(name)
        if only and ext != only:
            continue
        stem = name[: name.rfind(".")] if ext else name
        try:
            date = _dt.datetime.fromtimestamp(be.mtime(fp)).strftime("%Y-%m-%d")
        except Exception:
            date = ""
        cat = organizer.classify.category_for_ext(ext).split("/")[0]
        parent = re.split(r"[\\/]", posixpath.dirname(fp).rstrip("\\/"))[-1]
        out.append({"orig": stem, "ext": ext, "date": date,
                    "category": cat, "parent": parent, "name": name})
        if len(out) >= 6:
            break
    return {"samples": out}


def api_dupe_groups(p):
    be = _build_backend(p)
    saf = _build_safety(p)
    groups = organizer.duplicate_groups(be, be.root, saf)
    total_extra_kb = sum(g["kb"] * (len(g["members"]) - 1) for g in groups)
    return {"groups": groups, "count": len(groups),
            "wastedKb": total_extra_kb, "root": be.root}


def api_search(p):
    be = _build_backend(p)
    saf = _build_safety(p)
    q = (p.get("query") or "").strip().lower()
    if not q:
        return {"results": [], "count": 0}
    results = []
    truncated = False
    for fp in be.iter_files(be.root):
        name = posixpath.basename(fp)
        rel = be.relpath(posixpath.dirname(fp))
        segs = [s for s in rel.replace("\\", "/").split("/") if s and s != "."]
        if segs and saf.is_protected_dir("/".join(segs)):
            continue
        if q in name.lower():
            results.append({"name": name, "path": fp,
                            "folder": rel.replace("\\", "/"),
                            "kb": be.size(fp) // 1024})
            if len(results) >= 300:
                truncated = True
                break
    results.sort(key=lambda r: r["name"].lower())
    return {"results": results, "count": len(results), "truncated": truncated}


def api_wedding_sessions(p):
    """Group a folder's photos into time-based event 'sessions' (a wedding day
    naturally splits into ceremonies separated by gaps).  Suggests Kerala
    wedding function names in order; the user renames before filing."""
    if (p.get("backend") or "local").lower() != "local":
        raise ValueError("Wedding sorting works on local folders only.")
    be = _build_backend(p)
    saf = _build_safety(p)
    recursive = bool(p.get("recursive", True))
    gap_min = max(2, int(p.get("gap_minutes") or 40))
    from . import vision
    skip_top = {"Compressed_Images", "Originals_Backup"}
    items = []
    for fp in be.iter_files(be.root):
        name = posixpath.basename(fp)
        rel = be.relpath(posixpath.dirname(fp)).replace("\\", "/")
        segs = [s for s in rel.split("/") if s and s != "."]
        if segs and (segs[0] in skip_top or saf.is_protected_dir("/".join(segs))):
            continue
        if not recursive and segs:
            continue
        if saf.is_protected_file(name) or not vision.is_image(name):
            continue
        native = be.native(fp)
        dt = organizer._exif_datetime(native)
        ts = dt.timestamp() if dt else float(be.mtime(fp) or 0)
        items.append((ts, fp))
    items.sort(key=lambda x: x[0])

    gap = gap_min * 60
    sessions = []
    cur = []
    last = None
    for ts, fp in items:
        if last is not None and (ts - last) > gap and cur:
            sessions.append(cur)
            cur = []
        cur.append((ts, fp))
        last = ts
    if cur:
        sessions.append(cur)

    NAMES = ["Getting Ready", "Nishchayam (Engagement)", "Muhurtham (Ceremony)",
             "Thalikettu / Ring Exchange", "Sadhya (Feast)", "Group Photos",
             "Nalangu", "Reception", "Sendoff"]
    out = []
    for i, sess in enumerate(sessions):
        tss = [s[0] for s in sess]
        out.append({
            "id": i, "count": len(sess),
            "start": int(min(tss)), "end": int(max(tss)),
            "rep": sess[len(sess) // 2][1],
            "members": [s[1] for s in sess],
            "suggested": NAMES[i] if i < len(NAMES) else f"Session {i + 1}",
        })
    return {"sessions": out, "count": len(out), "photos": len(items),
            "root": be.root}


def api_pull_start(p):
    """Copy image/video files off an Android phone into a PC folder so the
    local Photo-AI can sort them (AI needs the pixels on the PC)."""
    if (p.get("backend") or "").lower() != "adb":
        raise ValueError("Copy-from-phone needs an ADB connection.")
    be = _build_backend(p)
    dest = p.get("dest")
    if not dest:
        raise ValueError("Choose a PC folder to copy the photos into.")
    dest = os.path.abspath(dest)
    from . import vision
    job_id = uuid.uuid4().hex
    job = {"done": 0, "total": 0, "phase": "listing", "finished": False,
           "error": None, "result": None, "cancel": False}
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        for k in list(_JOBS.keys())[:-20]:
            _JOBS.pop(k, None)

    def run():
        try:
            files = [f for f in be.iter_files(be.root)
                     if vision.is_image(f) or vision.is_video(f)]
            job["total"] = len(files)
            job["phase"] = "applying"
            os.makedirs(dest, exist_ok=True)
            pulled = 0
            for i, f in enumerate(files):
                if job["cancel"]:
                    break
                name = posixpath.basename(f)
                rel = be.relpath(posixpath.dirname(f)).replace("..", "_")
                parts = [s for s in rel.split("/") if s and s not in (".", "")]
                outdir = os.path.join(dest, *parts) if parts else dest
                os.makedirs(outdir, exist_ok=True)
                be._run(["pull", "-a", f, os.path.join(outdir, name)])
                pulled += 1
                job["done"] = i + 1
            job["result"] = {"pulled": pulled, "dest": dest,
                             "cancelled": job["cancel"]}
            job["phase"] = "done"
        except Exception as e:  # pragma: no cover - defensive
            job["error"] = f"{type(e).__name__}: {e}"
            job["phase"] = "error"
        finally:
            job["finished"] = True

    threading.Thread(target=run, daemon=True).start()
    return {"jobId": job_id}


# ==========================================================================
# Lossless image compression (optional — needs Pillow)
# ==========================================================================
def api_compress_status(_p):
    from . import compress
    return {"available": compress.available(), "heic": compress.heic_available()}


def _gather_compress_files(be, saf, recursive, include_heic=False):
    from . import compress
    skip_top = {"Compressed_Images", "Originals_Backup"}
    files = []
    if recursive:
        for fp in be.iter_files(be.root):
            name = posixpath.basename(fp)
            rel = be.relpath(posixpath.dirname(fp)).replace("\\", "/")
            segs = [s for s in rel.split("/") if s and s != "."]
            if segs and (segs[0] in skip_top or saf.is_protected_dir("/".join(segs))):
                continue
            if saf.is_protected_file(name) or not compress.is_compressible(name, include_heic):
                continue
            files.append(be.native(fp))
    else:
        for name in be.listdir(be.root):
            fp = be.join(be.root, name)
            if not be.isfile(fp):
                continue
            if saf.is_protected_file(name) or not compress.is_compressible(name, include_heic):
                continue
            files.append(be.native(fp))
    return files


def api_compress_estimate(p):
    if (p.get("backend") or "local").lower() != "local":
        raise ValueError("Image compression works on local folders only.")
    from . import compress
    if not compress.available():
        raise ValueError("Image compression needs Pillow.")
    be = _build_backend(p)
    saf = _build_safety(p)
    recursive = bool(p.get("recursive", True))
    lossy = bool(p.get("lossy"))
    include_heic = bool(p.get("heic")) and compress.heic_available()
    files = _gather_compress_files(be, saf, recursive, include_heic)
    total_files = len(files)
    total_bytes = 0
    for f in files:
        try:
            total_bytes += os.path.getsize(f)
        except OSError:
            pass

    # Re-encoding every image just to estimate is slow, so when asked we
    # measure a representative sample and extrapolate the savings ratio.
    sample = int(p.get("sample") or 0)
    sampled = bool(sample) and total_files > sample
    subset = files[:sample] if sampled else files
    res = compress.run_compress(
        be.root, subset, lossy=lossy,
        max_edge=int(p.get("max_edge") or 0),
        quality=int(p.get("quality") or 85), dry_run=True)

    if sampled and res["beforeKb"] > 0:
        ratio = res["savedKb"] / res["beforeKb"]
        frac = res["compressed"] / max(1, len(subset))
        res["savedKb"] = int(total_bytes // 1024 * ratio)
        res["beforeKb"] = total_bytes // 1024
        res["afterKb"] = res["beforeKb"] - res["savedKb"]
        res["compressed"] = int(round(total_files * frac))
    res["candidates"] = total_files
    res["sampled"] = sampled
    return res


def api_compress_start(p):
    if (p.get("backend") or "local").lower() != "local":
        raise ValueError("Image compression works on local folders only.")
    from . import compress
    if not compress.available():
        raise ValueError("Image compression needs Pillow. Install AI support "
                         "(or run: pip install pillow).")
    be = _build_backend(p)
    saf = _build_safety(p)
    recursive = bool(p.get("recursive", True))
    mode = "copy" if (p.get("mode") == "copy") else "replace"
    lossy = bool(p.get("lossy"))
    max_edge = int(p.get("max_edge") or 0)
    quality = int(p.get("quality") or 85)
    include_heic = bool(p.get("heic")) and compress.heic_available()
    files = _gather_compress_files(be, saf, recursive, include_heic)

    job_id = uuid.uuid4().hex
    job = {"done": 0, "total": len(files), "phase": "applying",
           "finished": False, "error": None, "result": None, "cancel": False}
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        for k in list(_JOBS.keys())[:-20]:
            _JOBS.pop(k, None)

    def run():
        try:
            def prog(d, t):
                job["done"] = d
                job["total"] = t

            def cancelled():
                return job["cancel"]

            journal = [] if mode == "replace" else None
            res = compress.run_compress(
                be.root, files, mode=mode, lossy=lossy, max_edge=max_edge,
                quality=quality, journal=journal,
                progress=prog, cancel=cancelled)
            if journal:
                _save_history(be, journal, label="compress")
                res["undo"] = {"available": be.name == "local",
                               "count": len(journal), "root": be.root}
            res["cancelled"] = job["cancel"]
            res["root"] = be.root
            job["result"] = res
            job["phase"] = "done"
        except Exception as e:  # pragma: no cover - defensive
            job["error"] = f"{type(e).__name__}: {e}"
            job["phase"] = "error"
        finally:
            job["finished"] = True

    threading.Thread(target=run, daemon=True).start()
    return {"jobId": job_id}


def api_compress_restore(p):
    """Move everything in Originals_Backup/ back into place (undo compression)."""
    if (p.get("backend") or "local").lower() != "local":
        raise ValueError("Local folders only.")
    be = _build_backend(p)
    root = be.root
    backup = os.path.join(root, "Originals_Backup")
    if not os.path.isdir(backup):
        return {"restored": 0}
    import shutil
    restored = 0
    for dp, _dn, fn in os.walk(backup):
        for f in fn:
            src = os.path.join(dp, f)
            rel = os.path.relpath(src, backup)
            dst = os.path.join(root, rel)
            # remove the compressed replacement (possibly a different ext)
            stem = dst[: dst.rfind(".")] if "." in os.path.basename(dst) else dst
            for cand in (dst, stem + ".jpg", stem + ".jpeg"):
                try:
                    if os.path.isfile(cand) and os.path.abspath(cand) != os.path.abspath(src):
                        os.remove(cand)
                except OSError:
                    pass
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                shutil.move(src, dst)
                restored += 1
            except OSError:
                pass
    shutil.rmtree(backup, ignore_errors=True)
    return {"restored": restored}


def api_compress_clear_backup(p):
    """Delete the Originals_Backup/ folder to reclaim the space."""
    if (p.get("backend") or "local").lower() != "local":
        raise ValueError("Local folders only.")
    be = _build_backend(p)
    backup = os.path.join(be.root, "Originals_Backup")
    freed = 0
    if os.path.isdir(backup):
        for dp, _dn, fn in os.walk(backup):
            for f in fn:
                try:
                    freed += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        import shutil
        shutil.rmtree(backup, ignore_errors=True)
    return {"freedKb": freed // 1024}


def api_compress_backup_status(p):
    be = _build_backend(p)
    backup = os.path.join(be.root, "Originals_Backup")
    if not os.path.isdir(backup):
        return {"exists": False}
    count = 0
    size = 0
    for dp, _dn, fn in os.walk(backup):
        for f in fn:
            count += 1
            try:
                size += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return {"exists": count > 0, "count": count, "kb": size // 1024}


# ==========================================================================
# Perceptual near-duplicate finder (whole tree) — needs vision deps
# ==========================================================================
def api_phash_start(p):
    if (p.get("backend") or "local").lower() != "local":
        raise ValueError("Near-duplicate scan works on local folders only.")
    from . import vision
    if vision.check_deps():
        raise ValueError("This needs the image-analysis packages (install AI "
                         "support / pip install -r requirements-vision.txt).")
    be = _build_backend(p)
    saf = _build_safety(p)
    recursive = bool(p.get("recursive", True))
    dist = int(p.get("distance") or 8)
    files = []
    skip_top = {"Compressed_Images", "Originals_Backup"}
    for fp in be.iter_files(be.root):
        name = posixpath.basename(fp)
        rel = be.relpath(posixpath.dirname(fp)).replace("\\", "/")
        segs = [s for s in rel.split("/") if s and s != "."]
        if segs and (segs[0] in skip_top or saf.is_protected_dir("/".join(segs))):
            continue
        if not recursive and segs:
            continue
        if saf.is_protected_file(name) or not vision.is_image(name):
            continue
        files.append(be.native(fp))

    job_id = uuid.uuid4().hex
    job = {"done": 0, "total": len(files), "phase": "applying",
           "finished": False, "error": None, "result": None, "cancel": False}
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        for k in list(_JOBS.keys())[:-20]:
            _JOBS.pop(k, None)

    def run():
        try:
            def prog(d, t):
                job["done"] = d
                job["total"] = t

            def cancelled():
                return job["cancel"]

            groups = vision.perceptual_groups(files, max_distance=dist,
                                              progress=prog, cancel=cancelled)
            out = []
            for m in groups:
                sizes = []
                for pth in m:
                    try:
                        sizes.append(os.path.getsize(pth))
                    except OSError:
                        sizes.append(0)
                keep = m[sizes.index(max(sizes))] if sizes else m[0]
                extra = sum(sorted(sizes)[:-1])
                out.append({"members": m, "keep": keep,
                            "kb": (max(sizes) if sizes else 0) // 1024,
                            "extraKb": extra // 1024})
            out.sort(key=lambda g: -g["extraKb"])
            job["result"] = {"groups": out, "count": len(out),
                             "cancelled": job["cancel"],
                             "wastedKb": sum(g["extraKb"] for g in out),
                             "root": be.root}
            job["phase"] = "done"
        except Exception as e:  # pragma: no cover - defensive
            job["error"] = f"{type(e).__name__}: {e}"
            job["phase"] = "error"
        finally:
            job["finished"] = True

    threading.Thread(target=run, daemon=True).start()
    return {"jobId": job_id}


# ==========================================================================
# Reclaim summary (duplicates + junk + large old files)
# ==========================================================================
def api_reclaim(p):
    be = _build_backend(p)
    saf = _build_safety(p)
    root = be.root

    def bytes_of(ops):
        return sum(be.size(o.a) for o in ops if o.kind == "move")

    dup_ops, _ = organizer.plan_duplicate_files(be, root, saf)
    junk_ops, _ = organizer.plan_junk(be, root, saf, recursive=True)

    import time
    cutoff = time.time() - 180 * 86400
    large = []
    large_bytes = 0
    for fp in be.iter_files(root):
        rel = be.relpath(posixpath.dirname(fp))
        name = posixpath.basename(fp)
        if saf.is_protected_dir(rel) or saf.is_protected_file(name):
            continue
        try:
            sz = be.size(fp)
            mt = be.mtime(fp)
        except OSError:
            continue
        if sz >= 20 * 1024 * 1024 and mt and mt < cutoff:
            large_bytes += sz
            if len(large) < 25:
                large.append({"name": name, "kb": sz // 1024,
                              "path": fp, "mtime": mt})

    dup_bytes = bytes_of(dup_ops)
    junk_bytes = bytes_of(junk_ops)
    dup_moves = sum(1 for o in dup_ops if o.kind == "move")
    junk_moves = sum(1 for o in junk_ops if o.kind == "move")
    large.sort(key=lambda x: -x["kb"])
    return {
        "duplicates": {"count": dup_moves, "kb": dup_bytes // 1024},
        "junk": {"count": junk_moves, "kb": junk_bytes // 1024},
        "largeOld": {"count": len(large), "kb": large_bytes // 1024,
                     "items": large},
        "totalKb": (dup_bytes + junk_bytes) // 1024,
        "root": root,
    }


# ==========================================================================
# Audit log export (flattened per-move history)
# ==========================================================================
def api_audit(p):
    root = p.get("root")
    if not root:
        raise ValueError("No folder specified.")
    lst = _load_history(root)
    rows = []
    for e in lst:
        for j in e.get("journal", []):
            if j.get("op") == "move":
                rows.append({"ts": e.get("ts"), "label": e.get("label"),
                             "src": j.get("src"), "dst": j.get("dst")})
            elif j.get("op") == "copy":
                rows.append({"ts": e.get("ts"), "label": e.get("label"),
                             "src": j.get("src"), "dst": j.get("dst")})
            elif j.get("op") == "compress":
                rows.append({"ts": e.get("ts"), "label": e.get("label"),
                             "src": j.get("orig"), "dst": j.get("backup")})
    return {"rows": rows, "count": len(rows), "root": root}


# ==========================================================================
# Watch folder — auto-run an operation when the folder changes
# ==========================================================================
_WATCHERS = {}
_WATCH_LOCK = threading.Lock()


def _dir_signature(be, root):
    cnt = 0
    total = 0
    latest = 0
    for fp in be.iter_files(root):
        cnt += 1
        try:
            st = os.stat(be.native(fp))
            total += st.st_size
            latest = max(latest, int(st.st_mtime))
        except OSError:
            pass
    return (cnt, total, latest)


def api_watch_start(p):
    if (p.get("backend") or "local").lower() != "local":
        raise ValueError("Watch-folder works on local folders only.")
    be = _build_backend(p)
    root = os.path.abspath(be.root)
    command = p.get("command")
    if command not in _PLANNERS:
        raise ValueError(f"Unknown watch action: {command}")
    with _WATCH_LOCK:
        cur = _WATCHERS.get(root)
        if cur and not cur["stop"]:
            return {"watching": True, "already": True, "command": cur["command"]}
    state = {"stop": False, "runs": 0, "last": None, "command": command,
             "params": p, "error": None}

    def loop():
        import time
        saf = _build_safety(p)
        try:
            last_sig = _dir_signature(be, root)
        except Exception:
            last_sig = None
        while not state["stop"]:
            time.sleep(4)
            if state["stop"]:
                break
            try:
                sig = _dir_signature(be, root)
            except Exception:
                continue
            if sig == last_sig:
                continue
            # wait for the folder to settle before acting
            time.sleep(3)
            if state["stop"]:
                break
            sig2 = _dir_signature(be, root)
            last_sig = sig2
            if sig2 != sig:
                continue
            try:
                ops, _ = _PLANNERS[command](be, saf, p)
                if ops:
                    journal = []
                    be.apply_ops(ops, journal=journal)
                    moves = sum(1 for j in journal if j.get("op") == "move")
                    if moves:
                        _save_history(be, journal, label=command)
                        state["runs"] += 1
                        state["last"] = time.time()
                    last_sig = _dir_signature(be, root)
            except Exception as e:  # pragma: no cover - defensive
                state["error"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=loop, daemon=True)
    state["thread"] = t
    with _WATCH_LOCK:
        _WATCHERS[root] = state
    t.start()
    return {"watching": True, "command": command}


def api_watch_stop(p):
    root = os.path.abspath(p.get("root") or "")
    with _WATCH_LOCK:
        st = _WATCHERS.get(root)
        if st:
            st["stop"] = True
    return {"watching": False}


def api_watch_status(p):
    root = os.path.abspath(p.get("root") or "")
    with _WATCH_LOCK:
        st = _WATCHERS.get(root)
        if not st or st["stop"]:
            return {"watching": False}
        return {"watching": True, "command": st["command"],
                "runs": st["runs"], "last": st["last"], "error": st["error"]}


# ==========================================================================
# Scheduled runs — run an operation on an interval or daily at HH:MM
# ==========================================================================
_SCHEDULES = {}
_SCHED_LOCK = threading.Lock()


def api_schedule_start(p):
    if (p.get("backend") or "local").lower() != "local":
        raise ValueError("Scheduled runs work on local folders only.")
    be = _build_backend(p)
    root = os.path.abspath(be.root)
    command = p.get("command")
    if command not in _PLANNERS:
        raise ValueError(f"Unknown scheduled action: {command}")
    mode = p.get("mode") or "interval"     # "interval" | "daily"
    try:
        interval_h = max(0.05, float(p.get("interval_hours") or 24))
    except (TypeError, ValueError):
        interval_h = 24.0
    daily_at = str(p.get("at") or "18:00")

    with _SCHED_LOCK:
        cur = _SCHEDULES.get(root)
        if cur and not cur["stop"]:
            return {"scheduled": True, "already": True, "command": cur["command"]}

    state = {"stop": False, "runs": 0, "last": None, "next": None,
             "command": command, "params": p, "error": None,
             "mode": mode, "interval_h": interval_h, "at": daily_at}

    def _next_daily():
        import time
        now = time.localtime()
        try:
            hh, mm = [int(x) for x in daily_at.split(":")[:2]]
        except ValueError:
            hh, mm = 18, 0
        import datetime as _dt
        n = _dt.datetime.now()
        tgt = n.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if tgt <= n:
            tgt += _dt.timedelta(days=1)
        return tgt.timestamp()

    def loop():
        import time
        saf = _build_safety(p)
        nxt = (_next_daily() if mode == "daily"
               else time.time() + interval_h * 3600)
        state["next"] = nxt
        while not state["stop"]:
            time.sleep(2)
            if state["stop"]:
                break
            if time.time() < nxt:
                continue
            try:
                ops, _ = _PLANNERS[command](be, saf, p)
                if ops:
                    journal = []
                    be.apply_ops(ops, journal=journal,
                                 copy_mode=bool(p.get("copy")))
                    moves = sum(1 for j in journal
                                if j.get("op") in ("move", "copy"))
                    if moves:
                        _save_history(be, journal, label=command)
                state["runs"] += 1
                state["last"] = time.time()
            except Exception as e:  # pragma: no cover - defensive
                state["error"] = f"{type(e).__name__}: {e}"
            nxt = (_next_daily() if mode == "daily"
                   else time.time() + interval_h * 3600)
            state["next"] = nxt

    t = threading.Thread(target=loop, daemon=True)
    state["thread"] = t
    with _SCHED_LOCK:
        _SCHEDULES[root] = state
    t.start()
    return {"scheduled": True, "command": command, "mode": mode}


def api_schedule_stop(p):
    root = os.path.abspath(p.get("root") or "")
    with _SCHED_LOCK:
        st = _SCHEDULES.get(root)
        if st:
            st["stop"] = True
    return {"scheduled": False}


def api_schedule_status(p):
    root = os.path.abspath(p.get("root") or "")
    with _SCHED_LOCK:
        st = _SCHEDULES.get(root)
        if not st or st["stop"]:
            return {"scheduled": False}
        return {"scheduled": True, "command": st["command"], "mode": st["mode"],
                "interval_h": st["interval_h"], "at": st["at"],
                "runs": st["runs"], "last": st["last"], "next": st["next"],
                "error": st["error"]}


ROUTES = {
    "/api/health": api_health,
    "/api/browse": api_browse,
    "/api/devices": api_devices,
    "/api/scan": api_scan,
    "/api/plan": api_plan,
    "/api/apply": api_apply,
    "/api/apply/start": api_apply_start,
    "/api/apply/progress": api_apply_progress,
    "/api/apply/cancel": api_apply_cancel,
    "/api/undo": api_undo,
    "/api/redo": api_redo,
    "/api/history": api_history,
    "/api/history/folders": api_history_folders,
    "/api/report": api_report,
    "/api/verify": api_verify,
    "/api/open": api_open,
    "/api/reclaim": api_reclaim,
    "/api/audit": api_audit,
    "/api/search": api_search,
    "/api/rename/sample": api_rename_sample,
    "/api/dupe/groups": api_dupe_groups,
    "/api/pull/start": api_pull_start,
    "/api/wedding/sessions": api_wedding_sessions,
    "/api/compress/status": api_compress_status,
    "/api/compress/start": api_compress_start,
    "/api/compress/estimate": api_compress_estimate,
    "/api/compress/restore": api_compress_restore,
    "/api/compress/clear-backup": api_compress_clear_backup,
    "/api/compress/backup-status": api_compress_backup_status,
    "/api/phash/start": api_phash_start,
    "/api/watch/start": api_watch_start,
    "/api/watch/stop": api_watch_stop,
    "/api/watch/status": api_watch_status,
    "/api/schedule/start": api_schedule_start,
    "/api/schedule/stop": api_schedule_stop,
    "/api/schedule/status": api_schedule_status,
    "/api/vision/status": api_vision_status,
    "/api/vision/install": api_vision_install,
    "/api/categorize/start": api_categorize_start,
    "/api/categorize/progress": api_categorize_progress,
    "/api/categorize/cancel": api_categorize_cancel,
    "/api/categorize/apply": api_categorize_apply,
    "/api/categorize/apply/start": api_categorize_apply_start,
    "/api/cluster/status": api_cluster_status,
    "/api/cluster/start": api_cluster_start,
    "/api/cluster/progress": api_cluster_progress,
    "/api/cluster/cancel": api_cluster_cancel,
    "/api/facefind/selfie": api_facefind_selfie,
    "/api/facefind/start": api_facefind_start,
    "/api/share/enable": api_share_enable,
    "/api/share/disable": api_share_disable,
    "/api/share/status": api_share_status,
    "/api/share/find/start": api_share_find_start,
    "/api/share/guests": api_share_guests,
}


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "phorg/1.0"

    # Only accept requests addressed to the local loopback host.  This blocks
    # DNS-rebinding attacks where a malicious website resolves its domain to
    # 127.0.0.1 and tries to drive this file-moving API from the browser.
    # When guest-sharing is ON, LAN (private-IP) visitors are also allowed —
    # but only for the small guest whitelist (see do_POST / do_GET).
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
                ok, frame = cap.read()
                cap.release()
                if not ok:
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
        """Zip up all matches from a finished FaceFind job (Download all)."""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        job = (qs.get("job") or [""])[0]
        token = (qs.get("token") or [""])[0]
        local = self._is_local_req()
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
        if not self._is_local_req() and path not in _GUEST_ROUTES:
            self._send(403, {"error": "Not available"})
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


def serve(host="127.0.0.1", port=8765, open_browser=True):
    global _SERVER_PORT
    # Bind on all interfaces so the guest-sharing portal can be reached from
    # phones on the same Wi-Fi.  Access stays loopback-only until the host
    # explicitly turns sharing on (see Handler._host_ok).
    bind_host = "0.0.0.0"
    port = _find_free_port(bind_host, port)
    _SERVER_PORT = port
    httpd = ThreadingHTTPServer((bind_host, port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print("\n  phorg UI is running.")
    print(f"  Open in your browser:  {url}")
    print("  Keep this window open while you use the app.")
    print("  Press Ctrl+C (or close this window) to stop.\n")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopping phorg UI ...")
    finally:
        try:
            from . import tunnel
            tunnel.stop()
        except Exception:
            pass
        httpd.server_close()
