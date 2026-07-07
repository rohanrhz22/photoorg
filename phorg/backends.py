"""
Storage backends.

Both backends expose the same small interface so the organiser and reporter
don't care whether they're talking to a local folder or an Android phone over
ADB.  All mutating work is expressed as a list of `Op` objects and applied in
one batch — for ADB that becomes a single pushed shell script (fast + robust),
for local it's a plain Python loop.

Design rule inherited from the manual reorg: we only ever **move** files
(reversible, same-filesystem = instant) and **rmdir** empty folders. We never
delete files.
"""
import os
import shutil
import subprocess
import posixpath
import base64
import tempfile


class Op:
    __slots__ = ("kind", "a", "b")

    def __init__(self, kind, a, b=None):
        self.kind = kind      # 'mkdir' | 'move' | 'rmdir'
        self.a = a
        self.b = b

    def __repr__(self):
        if self.kind == "move":
            return f"MOVE  {self.a}  ->  {self.b}"
        if self.kind == "mkdir":
            return f"MKDIR {self.a}"
        if self.kind == "rmdir":
            return f"RMDIR {self.a}"
        return f"{self.kind} {self.a} {self.b}"


def _b64(s):
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _unique_local(path):
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base}_{i}{ext}"):
        i += 1
    return f"{base}_{i}{ext}"


def _same_file(a, b):
    """True if two files are byte-identical (size first, then sha1)."""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
    except OSError:
        return False
    import hashlib

    def _h(path):
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.digest()
    try:
        return _h(a) == _h(b)
    except OSError:
        return False


# ===========================================================================
# Local filesystem backend
# ===========================================================================
class LocalBackend:
    name = "local"

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.sep = "/"

    # --- path helpers ---
    def join(self, *parts):
        return posixpath.join(*parts) if "/" in self.root or True \
            else os.path.join(*parts)

    def native(self, p):
        return p.replace("/", os.sep)

    def relpath(self, p):
        return os.path.relpath(p, self.root).replace(os.sep, "/")

    # --- queries ---
    def isdir(self, p):
        return os.path.isdir(self.native(p))

    def isfile(self, p):
        return os.path.isfile(self.native(p))

    def listdir(self, p):
        try:
            return sorted(os.listdir(self.native(p)))
        except OSError:
            return []

    def size(self, p):
        try:
            return os.path.getsize(self.native(p))
        except OSError:
            return 0

    def head(self, p, n=32):
        try:
            with open(self.native(p), "rb") as f:
                return f.read(n)
        except OSError:
            return b""

    def zip_names(self, p, limit=40):
        import zipfile
        try:
            with zipfile.ZipFile(self.native(p)) as z:
                return z.namelist()[:limit]
        except Exception:
            return []

    def iter_files(self, p):
        for dp, _dn, fn in os.walk(self.native(p)):
            for f in fn:
                yield os.path.join(dp, f).replace(os.sep, "/")

    # --- mutation ---
    def apply_ops(self, ops, log=lambda *_: None, journal=None,
                  progress=None, cancel=None, merge_identical=True,
                  copy_mode=False):
        done = 0
        total = len(ops)
        seen = 0
        self.merged_skips = 0
        self.copied = 0
        for op in ops:
            if cancel and cancel():
                break
            seen += 1
            try:
                if op.kind == "mkdir":
                    existed = os.path.isdir(self.native(op.a))
                    os.makedirs(self.native(op.a), exist_ok=True)
                    if journal is not None and not existed:
                        journal.append({"op": "mkdir", "path": op.a})
                elif op.kind == "move":
                    dst = self.native(op.b)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    if os.path.exists(dst):
                        if merge_identical and _same_file(self.native(op.a), dst):
                            # identical copy already at destination — don't
                            # duplicate it with a "_1" suffix; leave source be
                            self.merged_skips += 1
                            log(f"  = identical already there, skipped: {op.b}")
                            if progress and (seen % 25 == 0 or seen == total):
                                progress(seen, total)
                            continue
                        dst = _unique_local(dst)
                    if copy_mode:
                        shutil.copy2(self.native(op.a), dst)
                        self.copied += 1
                        done += 1
                        if journal is not None:
                            journal.append({"op": "copy", "src": op.a,
                                            "dst": dst.replace(os.sep, "/")})
                    else:
                        shutil.move(self.native(op.a), dst)
                        done += 1
                        if journal is not None:
                            journal.append({"op": "move", "src": op.a,
                                            "dst": dst.replace(os.sep, "/")})
                elif op.kind == "rmdir":
                    try:
                        os.rmdir(self.native(op.a))
                        done += 1
                        if journal is not None:
                            journal.append({"op": "rmdir", "path": op.a})
                    except OSError:
                        pass
            except Exception as e:
                log(f"  ! {op}: {e}")
            if progress and (seen % 25 == 0 or seen == total):
                progress(seen, total)
        return done

    def mtime(self, p):
        try:
            return int(os.path.getmtime(self.native(p)))
        except OSError:
            return 0

    def disk(self):
        u = shutil.disk_usage(self.root)
        return u.total // 1024, u.used // 1024, u.free // 1024

    # --- scanning ---
    def scan(self, root, max_depth=3, cap_dirs=("Android",)):
        tree = _local_scan(root, max_depth, set(x.lower() for x in cap_dirs), 1)
        total_bytes, total_files = _dir_stats(self.native(root))
        total_kb = total_bytes // 1024
        loose = sum(1 for e in os.scandir(self.native(root))
                    if e.is_file()) if os.path.isdir(self.native(root)) else 0
        dt, du, df = self.disk()
        meta = {"rootKb": total_kb, "rootFiles": total_files, "rootLoose": loose,
                "diskTotalKb": dt, "diskUsedKb": du, "diskFreeKb": df}
        return tree, meta


def _dir_stats(path):
    total = 0
    files = 0
    for _dp, _dn, fn in os.walk(path):
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(_dp, f))
                files += 1
            except OSError:
                pass
    return total, files


def _local_scan(root, max_depth, cap_dirs, depth):
    nodes = []
    native = root.replace("/", os.sep)
    try:
        entries = sorted(os.scandir(native), key=lambda e: e.name.lower())
    except OSError:
        return nodes
    for e in entries:
        if not e.is_dir(follow_symlinks=False):
            continue
        size, files = _dir_stats(e.path)
        try:
            subs = sum(1 for x in os.scandir(e.path) if x.is_dir())
        except OSError:
            subs = 0
        node = {"name": e.name, "path": e.path.replace(os.sep, "/"),
                "kb": size // 1024, "files": files, "subs": subs, "children": []}
        descend = depth < max_depth and e.name.lower() not in cap_dirs
        if descend:
            node["children"] = _local_scan(node["path"], max_depth, cap_dirs, depth + 1)
        nodes.append(node)
    return nodes


# ===========================================================================
# ADB backend
# ===========================================================================
_SCAN_SCRIPT = r'''S="__ROOT__"
enc(){ printf '%s' "$1" | base64 | tr -d '\n'; }
node(){ p=$1; lvl=$2
  kb=$(du -s "$p" 2>/dev/null | awk '{print $1}')
  files=$(find "$p" -type f 2>/dev/null | wc -l)
  subs=$(find "$p" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
  echo "$lvl|$(enc "$p")|$(enc "${p##*/}")|$kb|$files|$subs"; }
rkb=$(du -s "$S" 2>/dev/null | awk '{print $1}')
rfiles=$(find "$S" -type f 2>/dev/null | wc -l)
rloose=$(find "$S" -maxdepth 1 -type f 2>/dev/null | wc -l)
echo "ROOT|$rkb|$rfiles|$rloose"
df -k "$S" 2>/dev/null | awk 'NR==2{print "DF|"$2"|"$3"|"$4}'
find "$S" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null | while IFS= read -r -d '' d; do
  node "$d" L1
  dn=${d##*/}
  [ __MAXDEPTH__ -lt 2 ] && continue
  find "$d" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null | while IFS= read -r -d '' e; do
    node "$e" L2
    [ "$dn" = "Android" ] && continue
    [ __MAXDEPTH__ -lt 3 ] && continue
    find "$e" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null | while IFS= read -r -d '' f; do
      node "$f" L3
    done
  done
done
echo END
'''


class AdbBackend:
    name = "adb"
    DEV_TMP = "/storage/emulated/0/.phorg_tmp.sh"

    def __init__(self, adb_path, serial, root):
        self.adb = adb_path
        self.serial = serial
        self.root = root
        self.sep = "/"

    # --- low level ---
    def _run(self, args):
        return subprocess.run([self.adb, "-s", self.serial] + args,
                              capture_output=True, text=True)

    def shell(self, cmd):
        return self._run(["shell", cmd]).stdout

    def push_run(self, script):
        fd, tmp = tempfile.mkstemp(suffix=".sh")
        os.close(fd)
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        try:
            self._run(["push", tmp, self.DEV_TMP])
            out = self._run(["shell", f"sh {self.DEV_TMP}"]).stdout
            self._run(["shell", f"rm -f {self.DEV_TMP}"])
        finally:
            os.remove(tmp)
        return out

    # --- path helpers ---
    def join(self, *parts):
        return posixpath.join(*parts)

    def relpath(self, p):
        return posixpath.relpath(p, self.root)

    # --- queries ---
    def isdir(self, p):
        return self.shell(f'[ -d "{p}" ] && echo 1 || echo 0').strip() == "1"

    def isfile(self, p):
        return self.shell(f'[ -f "{p}" ] && echo 1 || echo 0').strip() == "1"

    def listdir(self, p):
        return [l for l in self.shell(f'ls -1 "{p}" 2>/dev/null').splitlines() if l]

    def size(self, p):
        out = self.shell(f'stat -c %s "{p}" 2>/dev/null').strip()
        try:
            return int(out)
        except ValueError:
            return 0

    def head(self, p, n=32):
        out = self.shell(f'dd if="{p}" bs=1 count={n} 2>/dev/null | od -An -tx1')
        hexs = "".join(out.split())
        try:
            return bytes.fromhex(hexs)
        except ValueError:
            return b""

    def zip_names(self, p, limit=40):
        # unzip may be unavailable; pull a copy and inspect locally
        import zipfile
        fd, tmp = tempfile.mkstemp(suffix=".zipprobe")
        os.close(fd)
        try:
            self._run(["pull", p, tmp])
            with zipfile.ZipFile(tmp) as z:
                return z.namelist()[:limit]
        except Exception:
            return []
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def iter_files(self, p):
        out = self.shell(f'find "{p}" -type f 2>/dev/null')
        for l in out.splitlines():
            if l:
                yield l

    # --- mutation ---
    def apply_ops(self, ops, log=lambda *_: None, journal=None,
                  progress=None, cancel=None, merge_identical=True,
                  copy_mode=False):
        self.merged_skips = 0
        self.copied = 0
        if not ops:
            return 0
        mv = "cp -f" if copy_mode else "mv -f"
        jkind = "copy" if copy_mode else "move"
        lines = [
            "#!/system/bin/sh",
            'mkd(){ d=$(printf "%s" "$1" | base64 -d); mkdir -p "$d"; }',
            ('mvf(){ s=$(printf "%s" "$1" | base64 -d); d=$(printf "%s" "$2" | base64 -d); '
             '[ -f "$s" ] || return; '
             'if [ -e "$d" ]; then b=${d%.*}; e=${d##*.}; i=1; '
             'while [ -e "${b}_${i}.${e}" ]; do i=$((i+1)); done; d="${b}_${i}.${e}"; fi; '
             f'{mv} "$s" "$d" && echo M; }}'),
            'rmd(){ d=$(printf "%s" "$1" | base64 -d); rmdir "$d" 2>/dev/null && echo R; }',
        ]
        for op in ops:
            if op.kind == "mkdir":
                lines.append(f'mkd "{_b64(op.a)}"')
            elif op.kind == "move":
                lines.append(f'mvf "{_b64(op.a)}" "{_b64(op.b)}"')
                if journal is not None:
                    journal.append({"op": jkind, "src": op.a, "dst": op.b})
            elif op.kind == "rmdir":
                lines.append(f'rmd "{_b64(op.a)}"')
        out = self.push_run("\n".join(lines) + "\n")
        return out.count("M") + out.count("R")

    def mtime(self, p):
        out = self.shell(f'stat -c %Y "{p}" 2>/dev/null').strip()
        try:
            return int(out)
        except ValueError:
            return 0

    def disk(self):
        out = self.shell(f"df -k \"{self.root}\" | awk 'NR==2{{print $2\"|\"$3\"|\"$4}}'")
        try:
            t, u, f = out.strip().split("|")
            return int(t), int(u), int(f)
        except Exception:
            return 0, 0, 0

    # --- scanning ---
    def scan(self, root, max_depth=3, cap_dirs=("Android",)):
        script = (_SCAN_SCRIPT
                  .replace("__ROOT__", root)
                  .replace("__MAXDEPTH__", str(max_depth)))
        out = self.push_run(script)
        return _parse_adb_scan(out)


def _parse_adb_scan(out):
    meta = {}
    l1s = []
    cur1 = cur2 = None
    for line in out.splitlines():
        if not line or line == "END":
            continue
        parts = line.split("|")
        tag = parts[0]
        if tag == "ROOT":
            meta["rootKb"] = int(parts[1]); meta["rootFiles"] = int(parts[2])
            meta["rootLoose"] = int(parts[3])
        elif tag == "DF":
            meta["diskTotalKb"] = int(parts[1]); meta["diskUsedKb"] = int(parts[2])
            meta["diskFreeKb"] = int(parts[3])
        elif tag in ("L1", "L2", "L3"):
            _, ep, en, kb, files, subs = parts
            node = {"name": base64.b64decode(en).decode("utf-8", "replace"),
                    "path": base64.b64decode(ep).decode("utf-8", "replace"),
                    "kb": int(kb), "files": int(files), "subs": int(subs),
                    "children": []}
            if tag == "L1":
                l1s.append(node); cur1 = node; cur2 = None
            elif tag == "L2" and cur1 is not None:
                cur1["children"].append(node); cur2 = node
            elif tag == "L3" and cur2 is not None:
                cur2["children"].append(node)
    return l1s, meta


# ===========================================================================
# Factory
# ===========================================================================
def get_backend(args):
    if args.backend == "adb":
        adb = args.adb or _find_adb()
        serial = args.device or _first_device(adb)
        if not serial:
            raise SystemExit("No ADB device found. Connect the phone, enable USB "
                             "debugging, and tap 'Allow'.")
        root = args.root or "/storage/emulated/0"
        return AdbBackend(adb, serial, root)
    else:
        if not args.root:
            raise SystemExit("--root is required for the local backend.")
        return LocalBackend(args.root)


def _find_adb():
    import shutil as sh
    p = sh.which("adb")
    if p:
        return p
    guess = os.path.expandvars(
        r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe")
    if os.path.exists(guess):
        return guess
    return "adb"


def _first_device(adb):
    try:
        out = subprocess.run([adb, "devices"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        raise SystemExit(f"adb not found at '{adb}'. Install platform-tools or pass --adb.")
    for line in out.splitlines()[1:]:
        line = line.strip()
        if line.endswith("\tdevice"):
            return line.split("\t")[0]
    return None
