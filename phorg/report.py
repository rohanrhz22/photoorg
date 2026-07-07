"""
HTML report generator.

Takes the (tree, meta) produced by any backend's scan(), tags each top-level
folder with a coarse category (for colour-coding), and injects the data into
the interactive template.
"""
import os
import sys
import json
import datetime


def _template_path():
    """Locate report_template.html from source or a frozen executable."""
    name = "report_template.html"
    base = getattr(sys, "_MEIPASS", None)
    if base:
        for cand in (os.path.join(base, "phorg", name), os.path.join(base, name)):
            if os.path.exists(cand):
                return cand
    return os.path.join(os.path.dirname(__file__), name)


CATMAP = {
    # exact top-level name -> (category, note)
    "android": ("system", "System / app-critical — not modified"),
    "miui": ("system", "System / app-critical — not modified"),
    "ringtones": ("system", "System / app-critical — not modified"),
    ".config": ("system", "System / app-critical — not modified"),
    "whatsapp_media": ("extracted", "Extracted from app storage"),
    "telegram_media": ("extracted", "Extracted from app storage"),
    "documents": ("documents", "Documents"),
    "subtitles": ("documents", "Documents"),
}


def _guess(name):
    n = name.lower()
    if name.startswith(".") or n in ("android", "miui", "ringtones", "notifications", "alarms"):
        return "system", "System / app-critical — not modified"
    if "whatsapp" in n or "telegram" in n:
        return "extracted", "Extracted from app storage"
    if any(k in n for k in ("download", "large", "1gb", "backup")):
        return "reclaim", "Large — back up then delete to reclaim space"
    if any(k in n for k in ("dcim", "picture", "image", "movie", "video", "photo", "insta", "camera")):
        return "media", "Organised media library"
    if any(k in n for k in ("document", "subtitle", "preset", "pdf")):
        return "documents", "Documents"
    return "personal", "Personal / misc"


def _tag(name):
    return CATMAP.get(name.lower(), _guess(name))


def _clean(node, top=False):
    o = {"name": node["name"], "kb": node["kb"], "files": node["files"],
         "subs": node.get("subs", 0),
         "children": [_clean(c) for c in node.get("children", [])]}
    if top:
        cat, note = _tag(node["name"])
        o["cat"] = cat
        o["note"] = note
    return o


def build_report(tree, meta, out_paths, root_label="/"):
    tpl_path = _template_path()
    with open(tpl_path, encoding="utf-8") as f:
        tpl = f.read()

    tree = sorted(tree, key=lambda n: -n["kb"])
    out_tree = [_clean(n, top=True) for n in tree]

    nL1 = len(tree)
    nL2 = sum(len(n.get("children", [])) for n in tree)
    nL3 = sum(len(c.get("children", [])) for n in tree for c in n.get("children", []))

    meta = dict(meta)
    meta.setdefault("rootKb", sum(n["kb"] for n in tree))
    meta.setdefault("rootFiles", sum(n["files"] for n in tree))
    meta.setdefault("rootLoose", 0)
    for k in ("diskTotalKb", "diskUsedKb", "diskFreeKb"):
        meta.setdefault(k, 0)
    meta.update({"nL1": nL1, "nL2": nL2, "nL3": nL3,
                 "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")})

    html = (tpl.replace("__TREE__", json.dumps(out_tree, ensure_ascii=False))
               .replace("__META__", json.dumps(meta, ensure_ascii=False)))

    written = []
    for p in out_paths:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(html)
        written.append(p)
    return written, meta
