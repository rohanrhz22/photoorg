"""
Organiser — turns scan results + rules into a list of Ops (the plan).

Every function here is a *planner*: it returns (ops, summary) and never mutates
anything itself. The caller decides whether to preview or apply. This is what
makes `--dry-run` trustworthy: the exact same plan is shown and then executed.
"""
import os
import re
import posixpath
import datetime
from collections import defaultdict

from . import classify
from .backends import Op


def _rel_segments(backend, path):
    rel = backend.relpath(path)
    return [s for s in rel.replace("\\", "/").split("/") if s and s != "."]


def _iter_candidate_files(backend, root, safety, recursive):
    """Yield (fullpath, name, size) for files eligible to be organised."""
    if recursive:
        for fp in backend.iter_files(root):
            name = posixpath.basename(fp)
            rel = backend.relpath(posixpath.dirname(fp)).replace("\\", "/")
            # segments of the containing folder, relative to root ("." == root)
            segs = [s for s in rel.split("/") if s and s != "."]
            # only apply folder-protection when the file is inside a sub-folder;
            # files sitting directly in the root have no segments to protect.
            if segs and safety.is_protected_dir("/".join(segs)):
                continue
            if safety.is_protected_file(name):
                continue
            yield fp, name, backend.size(fp)
    else:
        for name in backend.listdir(root):
            fp = backend.join(root, name)
            if not backend.isfile(fp):
                continue
            if safety.is_protected_file(name):
                continue
            yield fp, name, backend.size(fp)


# ---------------------------------------------------------------------------
# Junk sweep
# ---------------------------------------------------------------------------
def plan_junk(backend, root, safety, review_folder="Junk_Files_Review",
              recursive=False, extra_exts=None, to_trash=False):
    ops = []
    dest = backend.join(root, review_folder)
    counts = defaultdict(int)
    seen_dest = False
    # sending straight to the Recycle Bin only makes sense on the local machine
    use_trash = bool(to_trash) and getattr(backend, "name", None) == "local"
    for fp, name, size in _iter_candidate_files(backend, root, safety, recursive):
        # never sweep files already inside the review folder
        if _rel_segments(backend, fp)[:1] == [review_folder]:
            continue
        junk, why = classify.is_junk(name, size, extra_exts=extra_exts)
        if junk:
            if use_trash:
                ops.append(Op("trash", fp))
            else:
                if not seen_dest:
                    ops.append(Op("mkdir", dest)); seen_dest = True
                ops.append(Op("move", fp, backend.join(dest, name)))
            counts[why] += 1
    summary = {"folder": review_folder, "by_reason": dict(counts),
               "total": sum(counts.values()), "to_trash": use_trash}
    return ops, summary


# ---------------------------------------------------------------------------
# Organise by type / category
# ---------------------------------------------------------------------------
def plan_by_type(backend, root, safety, recursive=False, ext_overrides=None):
    ops = []
    made = set()
    counts = defaultdict(int)
    for fp, name, size in _iter_candidate_files(backend, root, safety, recursive):
        # skip files already sitting directly in a category folder at root
        segs = _rel_segments(backend, fp)
        ext = classify.ext_of(name)
        cat = classify.category_for_ext(ext, ext_overrides)
        # don't move things already in the right top bucket
        if segs[:1] and segs[0] in ("Images", "Videos", "Audio", "Documents",
                                     "Archives", "Installers", "Code", "Other"):
            continue
        destdir = backend.join(root, *cat.split("/"))
        if destdir not in made:
            ops.append(Op("mkdir", destdir)); made.add(destdir)
        ops.append(Op("move", fp, backend.join(destdir, name)))
        counts[cat] += 1
    summary = {"by_category": dict(counts), "total": sum(counts.values())}
    return ops, summary


# ---------------------------------------------------------------------------
# Bulk rename by template  ({date} {category} {n} {seq} {orig} {ext} {parent})
# ---------------------------------------------------------------------------
def _apply_template(template, vals):
    out = template
    for k, v in vals.items():
        out = out.replace("{" + k + "}", str(v))
    return out.strip()


def _safe_name(s):
    s = re.sub(r'[\\/:*?"<>|]+', "_", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s or "file"


def plan_rename(backend, root, safety, template="{orig}", recursive=False,
                start=1, pad=3, only_ext=None):
    ops = []
    files = list(_iter_candidate_files(backend, root, safety, recursive))
    files.sort(key=lambda t: t[1].lower())
    if only_ext:
        only_ext = only_ext.lower().lstrip(".")
    n = int(start)
    examples = []
    used = set()
    for fp, name, size in files:
        ext = classify.ext_of(name)
        if only_ext and ext != only_ext:
            continue
        stem = name[: name.rfind(".")] if ext else name
        seq = str(n).zfill(int(pad))
        try:
            dt = datetime.datetime.fromtimestamp(backend.mtime(fp))
            date = dt.strftime("%Y-%m-%d")
        except Exception:
            date = ""
        cat = classify.category_for_ext(ext).split("/")[0]
        parent = re.split(r"[\\/]", posixpath.dirname(fp).rstrip("\\/"))[-1]
        newstem = _safe_name(_apply_template(template, {
            "orig": stem, "n": seq, "seq": seq, "date": date,
            "category": cat, "parent": parent, "ext": ext}))
        newname = f"{newstem}.{ext}" if ext else newstem
        n += 1
        destdir = posixpath.dirname(fp)
        target = backend.join(destdir, newname)
        if target.lower() == fp.lower():
            continue
        k = 1
        while target in used:
            alt = f"{newstem}_{k}.{ext}" if ext else f"{newstem}_{k}"
            target = backend.join(destdir, alt)
            k += 1
        used.add(target)
        ops.append(Op("move", fp, target))
        if len(examples) < 12:
            examples.append([name, posixpath.basename(target)])
    summary = {"total": len(ops), "examples": examples, "template": template}
    return ops, summary


# ---------------------------------------------------------------------------
# Organise by size tier (within a single folder, non-recursive by default)
# ---------------------------------------------------------------------------
def plan_by_size(backend, root, safety, recursive=False):
    ops = []
    made = set()
    counts = defaultdict(int)
    for fp, name, size in _iter_candidate_files(backend, root, safety, recursive):
        segs = _rel_segments(backend, fp)
        if segs[:1] and segs[0] in classify.SIZE_TIER_ORDER:
            continue
        tier = classify.size_tier(size)
        destdir = backend.join(root, tier)
        if destdir not in made:
            ops.append(Op("mkdir", destdir)); made.add(destdir)
        ops.append(Op("move", fp, backend.join(destdir, name)))
        counts[tier] += 1
    summary = {"by_tier": dict(counts), "total": sum(counts.values())}
    return ops, summary


# ---------------------------------------------------------------------------
# Fix broken / missing extensions via magic bytes
# ---------------------------------------------------------------------------
def plan_fix_extensions(backend, root, safety, recursive=True, sort=False):
    ops = []
    made = set()
    counts = defaultdict(int)
    for fp, name, size in _iter_candidate_files(backend, root, safety, recursive):
        cur_ext = classify.ext_of(name)
        # only touch files whose extension is missing, or ends with a bare dot
        needs = (cur_ext == "" or name.endswith("."))
        head = backend.head(fp, 32)
        detected = classify.detect_type(head)
        if detected == "zip":
            detected = classify.refine_zip(backend.zip_names(fp))
        if not detected:
            continue
        # if it already has the right extension, skip
        if cur_ext == detected:
            continue
        # build a corrected name
        stem = name[:-1] if name.endswith(".") else \
            (name if cur_ext == "" else name[: name.rfind(".")])
        stem = stem.rstrip(".")
        newname = f"{stem}.{detected}"
        if not needs and cur_ext:
            # extension present but wrong — only rename if clearly a mislabel
            # (keep conservative: skip unless original had no usable ext)
            continue
        destdir = backend.join(root, *classify.category_for_ext(detected).split("/")) \
            if sort else posixpath.dirname(fp)
        if sort and destdir not in made:
            ops.append(Op("mkdir", destdir)); made.add(destdir)
        ops.append(Op("move", fp, backend.join(destdir, newname)))
        counts[detected] += 1
    summary = {"by_type": dict(counts), "total": sum(counts.values()),
               "sorted": sort}
    return ops, summary


# ---------------------------------------------------------------------------
# Remove empty folders (recursively fileless), skipping protected trees
# ---------------------------------------------------------------------------
def plan_empty_folders(backend, root, safety):
    ops = []
    empties = []
    # gather all dirs; a dir is removable if it contains zero files anywhere
    # beneath it and is not protected.
    all_dirs = _all_dirs(backend, root)
    # process deepest-first so parents that become empty are also caught
    for d in sorted(all_dirs, key=lambda x: x.count("/"), reverse=True):
        rel = backend.relpath(d)
        if safety.is_protected_dir(rel):
            continue
        has_file = any(True for _ in _first(backend.iter_files(d)))
        if not has_file:
            empties.append(d)
            ops.append(Op("rmdir", d))
    return ops, {"folders": empties, "total": len(empties)}


def _first(iterable):
    for x in iterable:
        yield x
        return


def _all_dirs(backend, root):
    dirs = []
    stack = [root]
    while stack:
        cur = stack.pop()
        for name in backend.listdir(cur):
            fp = backend.join(cur, name)
            if backend.isdir(fp):
                dirs.append(fp)
                stack.append(fp)
    return dirs


# ---------------------------------------------------------------------------
# Verification — count files before/after must match
# ---------------------------------------------------------------------------
def count_files(backend, root):
    return sum(1 for _ in backend.iter_files(root))


# ---------------------------------------------------------------------------
# Content-based image categorisation (optional — needs vision deps)
# ---------------------------------------------------------------------------
def plan_categorize_images(backend, root, safety, options,
                           progress=None, cancel=None):
    """Analyse images by content and file them into category folders.

    ``options`` carries the enabled categories and thresholds. ``progress`` is
    an optional callback(done, total, tally_dict); ``cancel`` an optional
    callable returning True to stop early. Local backend only.
    """
    from . import vision

    enabled = options.get("enabled", {})
    category_folders = set(vision.CATEGORIES.values())
    for person in (options.get("people") or []):
        if person.get("name"):
            category_folders.add(vision.person_folder(person["name"]))
    for grp in (options.get("people_groups") or []):
        if grp.get("name"):
            category_folders.add(vision.person_folder(grp["name"]))
    if options.get("known_people"):
        try:
            from . import peopledb
            for _p in peopledb.summary():
                category_folders.add(vision.person_folder(_p["name"]))
        except Exception:
            pass
    recursive = bool(options.get("recursive"))

    # gather image files first so we know the total (for progress)
    to_native = getattr(backend, "native", lambda p: p)
    want_video = bool(enabled.get("video"))
    files = []
    video_files = []
    for fp, name, _size in _iter_candidate_files(backend, root, safety, recursive):
        segs = _rel_segments(backend, fp)
        if segs[:1] and segs[0] in category_folders:
            continue  # already sorted into a category folder
        if vision.is_image(name):
            files.append(fp)
        elif want_video and vision.is_video(name):
            video_files.append(fp)

    total = len(files)
    limit = options.get("limit")
    if limit:
        files = files[:int(limit)]
        total = len(files)
    cz = vision.Categorizer(options)

    # analysis cache (instant re-runs) — local backend only
    import json as _json
    import hashlib as _hashlib
    use_cache = backend.name == "local" and not limit
    people_enabled = bool(enabled.get("people"))
    scene_enabled = bool(options.get("scene"))
    _psig_src = (_json.dumps([{"n": p.get("name"), "s": p.get("samples")}
                             for p in (options.get("people") or [])],
                             sort_keys=True)
                 + _json.dumps([{"n": g.get("name"), "s": g.get("samples")}
                               for g in (options.get("people_groups") or [])],
                               sort_keys=True)
                 + str(options.get("person_threshold"))
                 + ("|scene1" if scene_enabled else "|scene0")
                 + ("|known1" if options.get("known_people") else "|known0"))
    analysis_sig = _hashlib.md5(_psig_src.encode()).hexdigest()
    cache_file = os.path.join(to_native(root), ".phorg", "analysis_cache.json") \
        if use_cache else None
    cache = {}
    if cache_file and os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                cache = _json.load(f)
        except (OSError, ValueError):
            cache = {}
    cache_dirty = False

    tally = defaultdict(int)
    analyzed = []           # (fp, metrics)
    done = 0
    for fp in files:
        if cancel and cancel():
            break
        metrics = None
        st_size = st_mtime = 0
        if use_cache:
            try:
                stt = os.stat(to_native(fp))
                st_size, st_mtime = stt.st_size, int(stt.st_mtime)
            except OSError:
                st_size = st_mtime = 0
            ce = cache.get(fp)
            if (ce and ce.get("size") == st_size and ce.get("mtime") == st_mtime
                    and ce.get("sig") == analysis_sig):
                metrics = dict(ce["m"])
        if metrics is None:
            metrics = cz.classify(to_native(fp))
            if use_cache and not metrics.get("unreadable"):
                cache[fp] = {"size": st_size, "mtime": st_mtime,
                             "sig": analysis_sig, "m": metrics}
                cache_dirty = True
        # blur/dup/similar depend on live thresholds — recompute from raw focus
        if not metrics.get("unreadable"):
            metrics["is_blurry"] = metrics.get("focus", 1e9) < cz.blur_threshold
            metrics["is_dup"] = False
            metrics["is_similar"] = False
            metrics["is_best"] = metrics.get("quality", 0) >= float(
                options.get("best_threshold", 0.72))
        analyzed.append((fp, metrics))
        # provisional category (before dup grouping) for live progress
        prov = None if metrics.get("unreadable") else vision.category_for(metrics, enabled)
        if prov:
            tally[prov] += 1
        done += 1
        if progress:
            progress(done, total, dict(tally))

    # keep-best duplicate grouping: within a near-duplicate cluster, keep the
    # sharpest / highest-resolution frame and mark the rest as duplicates.
    if enabled.get("duplicates"):
        dist = int(options.get("dup_distance", 8))
        groups = []
        for i, (fp, m) in enumerate(analyzed):
            h = m.get("phash")
            if h is None or m.get("unreadable"):
                continue
            placed = False
            for g in groups:
                if bin(h ^ analyzed[g[0]][1]["phash"]).count("1") <= dist:
                    g.append(i); placed = True; break
            if not placed:
                groups.append([i])
        for g in groups:
            if len(g) < 2:
                continue
            best = max(g, key=lambda i: analyzed[i][1].get("focus", 0)
                       * max(1, analyzed[i][1].get("pixels", 1)))
            for i in g:
                if i != best:
                    analyzed[i][1]["is_dup"] = True

    # similar-shot grouping: looser threshold groups same-scene bursts; keep the
    # best of each group and mark the rest as "similar extras".
    if enabled.get("similar"):
        sdist = int(options.get("similar_distance", 16))
        groups = []
        for i, (fp, m) in enumerate(analyzed):
            h = m.get("phash")
            if h is None or m.get("unreadable") or m.get("is_dup"):
                continue
            placed = False
            for g in groups:
                if bin(h ^ analyzed[g[0]][1]["phash"]).count("1") <= sdist:
                    g.append(i); placed = True; break
            if not placed:
                groups.append([i])
        for g in groups:
            if len(g) < 2:
                continue
            best = max(g, key=lambda i: analyzed[i][1].get("focus", 0)
                       * max(1, analyzed[i][1].get("pixels", 1)))
            for i in g:
                if i != best:
                    analyzed[i][1]["is_similar"] = True

    # final category decision (now that duplicates are known)
    tally = defaultdict(int)
    results = []
    for fp, m in analyzed:
        cat = None if m.get("unreadable") else vision.category_for(m, enabled)
        results.append((fp, cat))
        if cat:
            tally[cat] += 1

    # videos: filed straight into Videos/ (no content analysis)
    for vfp in video_files:
        analyzed.append((vfp, {"video": True}))
        results.append((vfp, vision.CATEGORIES["video"]))
        tally[vision.CATEGORIES["video"]] += 1

    ops = []
    made = set()
    for fp, cat in results:
        if not cat:
            continue
        destdir = backend.join(root, cat)
        if destdir not in made:
            ops.append(Op("mkdir", destdir)); made.add(destdir)
        ops.append(Op("move", fp, backend.join(destdir, posixpath.basename(fp))))

    items = [{"path": fp, "name": posixpath.basename(fp), "category": cat,
              "focus": m.get("focus"), "blurry": bool(m.get("is_blurry")),
              "faces": m.get("faces"), "dup": bool(m.get("is_dup")),
              "similar": bool(m.get("is_similar")),
              "screenshot": bool(m.get("screenshot")),
              "document": bool(m.get("document")),
              "quality": m.get("quality"), "video": bool(m.get("video")),
              "person": m.get("person"), "w": m.get("w"), "h": m.get("h")}
             for (fp, cat), (_afp, m) in zip(results, analyzed)]
    if cache_file and cache_dirty:
        try:
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                _json.dump(cache, f)
        except OSError:
            pass
    summary = {"by_category": dict(tally), "total": sum(tally.values()),
               "analyzed": done, "found": total, "items": items,
               "cached": use_cache,
               "people_error": getattr(cz, "people_error", None)}
    return ops, summary


# ---------------------------------------------------------------------------
# Whole-tree exact duplicate finder (any file type) — content hashing
# ---------------------------------------------------------------------------
def _open_hash_cache(backend, root):
    """A persistent SHA-1 cache for local trees (see phorg.hashcache)."""
    if getattr(backend, "name", None) != "local":
        return None
    from .hashcache import MetaCache
    return MetaCache(os.path.join(backend.native(root), ".phorg"))


def _file_hash(backend, fp, chunk=1 << 20, cache=None):
    import hashlib
    native = backend.native(fp) if hasattr(backend, "native") else fp
    key = None
    if cache is not None:
        key = cache.stat_key(native)
        if key is not None:
            hit = cache.get(native, key[0], key[1])
            if hit is not None and "sha1" in hit:
                return hit["sha1"]
    hh = hashlib.sha1()
    try:
        with open(native, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                hh.update(b)
    except OSError:
        return None
    digest = hh.hexdigest()
    if cache is not None and key is not None:
        cache.put(native, key[0], key[1], {"sha1": digest})
    return digest


def plan_duplicate_files(backend, root, safety, review_folder="Duplicates_Review"):
    """Find byte-for-byte duplicate files anywhere under root and move the
    redundant copies into a review folder (the first copy is kept in place)."""
    by_size = defaultdict(list)
    for fp in backend.iter_files(root):
        name = posixpath.basename(fp)
        rel = backend.relpath(posixpath.dirname(fp))
        segs = [s for s in rel.replace("\\", "/").split("/") if s and s != "."]
        if segs and safety.is_protected_dir("/".join(segs)):
            continue
        if safety.is_protected_file(name):
            continue
        if segs[:1] == [review_folder]:
            continue
        by_size[backend.size(fp)].append(fp)

    ops = []
    dest = backend.join(root, review_folder)
    seen_dest = False
    groups = 0
    dupes = 0
    wasted_kb = 0
    cache = _open_hash_cache(backend, root)
    try:
        for size, paths in by_size.items():
            if len(paths) < 2 or size == 0:
                continue
            digests = defaultdict(list)
            for fp in paths:
                d = _file_hash(backend, fp, cache=cache)
                if d:
                    digests[d].append(fp)
            for d, fps in digests.items():
                if len(fps) < 2:
                    continue
                groups += 1
                keep = min(fps, key=lambda p: (p.count("/"), len(p)))
                for fp in fps:
                    if fp == keep:
                        continue
                    if not seen_dest:
                        ops.append(Op("mkdir", dest)); seen_dest = True
                    ops.append(Op("move", fp, backend.join(dest, posixpath.basename(fp))))
                    dupes += 1
                    wasted_kb += size // 1024
    finally:
        if cache is not None:
            cache.close()
    summary = {"groups": groups, "duplicates": dupes, "wasted_kb": wasted_kb,
               "folder": review_folder}
    return ops, summary


# ---------------------------------------------------------------------------
# Flatten / un-nest — pull files out of sub-folders into the root
# ---------------------------------------------------------------------------
def plan_flatten(backend, root, safety, remove_emptied=True):
    ops = []
    moved = 0
    for fp in backend.iter_files(root):
        name = posixpath.basename(fp)
        dirp = posixpath.dirname(fp)
        rel = backend.relpath(dirp)
        segs = [s for s in rel.replace("\\", "/").split("/") if s and s != "."]
        if not segs:
            continue  # already at root
        if safety.is_protected_dir(rel) or safety.is_protected_file(name):
            continue
        ops.append(Op("move", fp, backend.join(root, name)))
        moved += 1
    if remove_emptied:
        dirs = _all_dirs(backend, root)
        for d in sorted(dirs, key=lambda x: x.count("/"), reverse=True):
            rel = backend.relpath(d)
            if safety.is_protected_dir(rel):
                continue
            ops.append(Op("rmdir", d))
    summary = {"moved": moved}
    return ops, summary


# ---------------------------------------------------------------------------
# Sort geotagged photos by location (EXIF GPS) into Places/<lat>_<lon>
# ---------------------------------------------------------------------------
def plan_by_location(backend, root, safety, recursive=False, precision=3):
    from . import vision
    to_native = getattr(backend, "native", lambda p: p)
    ops = []
    made = set()
    counts = defaultdict(int)
    no_gps = 0
    for fp, name, _size in _iter_candidate_files(backend, root, safety, recursive):
        if not vision.is_image(name):
            continue
        segs = _rel_segments(backend, fp)
        if segs[:1] == ["Places"]:
            continue
        gps = vision.exif_gps(to_native(fp))
        if not gps:
            no_gps += 1
            continue
        lat, lon = gps
        sub = ["Places", f"{round(lat, precision)}_{round(lon, precision)}"]
        for i in range(len(sub)):
            d = backend.join(root, *sub[:i + 1])
            if d not in made:
                ops.append(Op("mkdir", d)); made.add(d)
        ops.append(Op("move", fp, backend.join(root, *sub, name)))
        counts["/".join(sub)] += 1
    summary = {"by_place": dict(counts), "total": sum(counts.values()),
               "no_gps": no_gps}
    return ops, summary


def list_image_files(backend, root, safety, recursive=False):
    """List image files under root (respecting safety), skipping category folders."""
    from . import vision
    out = []
    skip = set(vision.CATEGORIES.values())
    for fp, name, _size in _iter_candidate_files(backend, root, safety, recursive):
        if not vision.is_image(name):
            continue
        segs = _rel_segments(backend, fp)
        if segs[:1] and segs[0] in skip:
            continue
        out.append(fp)
    return out


# ---------------------------------------------------------------------------
# Duplicate groups (for the review gallery) — byte-identical sets
# ---------------------------------------------------------------------------
def duplicate_groups(backend, root, safety, review_folder="Duplicates_Review",
                     limit=200):
    by_size = defaultdict(list)
    for fp in backend.iter_files(root):
        name = posixpath.basename(fp)
        rel = backend.relpath(posixpath.dirname(fp))
        segs = [s for s in rel.replace("\\", "/").split("/") if s and s != "."]
        if segs and safety.is_protected_dir("/".join(segs)):
            continue
        if safety.is_protected_file(name) or segs[:1] == [review_folder]:
            continue
        by_size[backend.size(fp)].append(fp)
    out = []
    cache = _open_hash_cache(backend, root)
    try:
        for size, paths in by_size.items():
            if len(paths) < 2 or size == 0:
                continue
            digests = defaultdict(list)
            for fp in paths:
                d = _file_hash(backend, fp, cache=cache)
                if d:
                    digests[d].append(fp)
            for d, fps in digests.items():
                if len(fps) < 2:
                    continue
                keep = min(fps, key=lambda p: (p.count("/"), len(p)))
                out.append({"size": size, "kb": size // 1024,
                            "keep": keep, "members": fps})
                if len(out) >= limit:
                    return sorted(out, key=lambda g: -g["size"])
    finally:
        if cache is not None:
            cache.close()
    return sorted(out, key=lambda g: -g["size"])


# ---------------------------------------------------------------------------
# Similar-filename grouping — "IMG (1).jpg", "IMG-copy.jpg" ... keep one
# ---------------------------------------------------------------------------
_DUP_RE = re.compile(
    r"(\s*\(\d+\)|\s*-\s*cop(?:y|ie)[a-z0-9 ]*|\s*_cop(?:y|ie)[a-z0-9 ]*"
    r"|\s+cop(?:y|ie)[a-z0-9 ]*|\s*-\s*\d+)\s*$", re.I)


def _norm_dupname(stem):
    s = stem
    prev = None
    while s != prev:
        prev = s
        s = _DUP_RE.sub("", s).strip()
    return s.lower() or stem.lower()


def plan_similar_names(backend, root, safety, recursive=True,
                       review_folder="Similar_Names_Review"):
    groups = defaultdict(list)
    for fp, name, size in _iter_candidate_files(backend, root, safety, recursive):
        if _rel_segments(backend, fp)[:1] == [review_folder]:
            continue
        ext = classify.ext_of(name)
        stem = name[: name.rfind(".")] if ext else name
        groups[(_norm_dupname(stem), ext)].append((fp, name, size))
    ops = []
    made = False
    dest = backend.join(root, review_folder)
    n = 0
    ex = []
    ngroups = 0
    for key, items in groups.items():
        if len(items) < 2:
            continue
        ngroups += 1
        items.sort(key=lambda t: (len(t[1]), t[1]))   # keep the shortest name
        keep = items[0]
        for fp, name, size in items[1:]:
            if not made:
                ops.append(Op("mkdir", dest)); made = True
            ops.append(Op("move", fp, backend.join(dest, name)))
            n += 1
            if len(ex) < 10:
                ex.append([name, keep[1]])
    summary = {"total": n, "groups": ngroups, "examples": ex,
               "folder": review_folder}
    return ops, summary


# ---------------------------------------------------------------------------
# One-click recommended cleanup — junk -> duplicates -> by type
# ---------------------------------------------------------------------------
def plan_cleanup(backend, root, safety, recursive=False, extra_exts=None,
                 ext_overrides=None):
    used = set()
    ops = []

    def _add(sub_ops):
        added = 0
        for o in sub_ops:
            if o.kind == "move":
                if o.a in used:
                    continue
                used.add(o.a)
                added += 1
            ops.append(o)
        return added

    j_ops, j_sum = plan_junk(backend, root, safety, recursive=recursive,
                             extra_exts=extra_exts)
    j = _add(j_ops)
    d_ops, d_sum = plan_duplicate_files(backend, root, safety)
    du = _add(d_ops)
    t_ops, t_sum = plan_by_type(backend, root, safety, recursive=recursive,
                                ext_overrides=ext_overrides)
    t = _add(t_ops)
    summary = {"total": sum(1 for o in ops if o.kind == "move"),
               "junk": j, "duplicates": du, "typed": t,
               "steps": ["Junk sweep", "Duplicate files", "Organize by type"]}
    return ops, summary


# ---------------------------------------------------------------------------
# Sort photos/videos by capture date (EXIF when available, else file time)
# ---------------------------------------------------------------------------
def _exif_datetime(native_path):
    try:
        from PIL import Image
        exif = Image.open(native_path).getexif()
        val = exif.get(306)  # DateTime
        try:
            sub = exif.get_ifd(0x8769)  # Exif IFD
            val = sub.get(36867) or sub.get(36868) or val  # DateTimeOriginal
        except Exception:
            pass
        if val:
            return datetime.datetime.strptime(str(val)[:19], "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None
    return None


def _file_datetime(backend, fp, use_exif):
    if use_exif and hasattr(backend, "native"):
        d = _exif_datetime(backend.native(fp))
        if d:
            return d
    ts = backend.mtime(fp) if hasattr(backend, "mtime") else 0
    if ts:
        try:
            return datetime.datetime.fromtimestamp(ts)
        except (OSError, ValueError):
            return None
    return None


def _date_subpath(dt, granularity):
    if dt is None:
        return ["Undated"]
    y = f"{dt.year:04d}"
    ym = f"{dt.year:04d}-{dt.month:02d}"
    ymd = f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
    if granularity == "year":
        return [y]
    if granularity == "day":
        return [y, ym, ymd]
    return [y, ym]


def plan_by_date(backend, root, safety, granularity="month", recursive=False,
                 use_exif=True):
    """File images/videos into YYYY / YYYY-MM (/ YYYY-MM-DD) folders by date."""
    ops = []
    made = set()
    counts = defaultdict(int)
    for fp, name, _size in _iter_candidate_files(backend, root, safety, recursive):
        if classify.category_for_ext(classify.ext_of(name)) not in ("Images", "Videos"):
            continue
        sub = _date_subpath(_file_datetime(backend, fp, use_exif), granularity)
        segs = _rel_segments(backend, fp)
        if segs[:len(sub)] == sub:
            continue  # already filed there
        for i in range(len(sub)):
            d = backend.join(root, *sub[:i + 1])
            if d not in made:
                ops.append(Op("mkdir", d)); made.add(d)
        destdir = backend.join(root, *sub)
        ops.append(Op("move", fp, backend.join(destdir, name)))
        counts["/".join(sub)] += 1
    summary = {"by_period": dict(counts), "total": sum(counts.values())}
    return ops, summary