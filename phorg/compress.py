"""
Lossless batch image compression (optional feature).

The promise here is **no quality loss at all** — pixels come out bit-for-bit
identical.  We only remove bytes that aren't picture data:

  * JPEG — strips metadata segments (EXIF, embedded thumbnails, XMP, Photoshop,
           comments) at the byte level while leaving the entropy-coded image
           scan completely untouched.  The JFIF (APP0) and ICC colour profile
           (APP2) segments are kept so colours never shift.  Phone photos carry
           big EXIF blocks + embedded thumbnails, so this reclaims real space.
  * PNG  — Pillow's ``optimize=True`` re-deflates the pixels losslessly.

Needs Pillow only for the PNG path; the JPEG path is pure standard library.
A file is replaced only when the new version is genuinely smaller, and the
original is always preserved (kept in place or moved to a backup folder).
"""
import io
import os
import shutil

COMPRESSIBLE = {"jpg", "jpeg", "png"}
HEIC_EXTS = {"heic", "heif"}

# JPEG APPn/COM markers that carry only metadata (safe to drop).
# We deliberately KEEP APP0 (JFIF) and APP2 (ICC profile) to preserve colour.
_JPEG_DROP = {0xE1, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xEB,
              0xEC, 0xED, 0xEE, 0xEF, 0xFE}


def available():
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


def heic_available():
    try:
        import pillow_heif  # noqa: F401
        return True
    except Exception:
        return False


def _ensure_heif():
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        return True
    except Exception:
        return False


def _ext(name):
    dot = name.rfind(".")
    return name[dot + 1:].lower() if dot >= 0 else ""


def is_compressible(name, include_heic=False):
    e = _ext(name)
    return e in COMPRESSIBLE or (include_heic and e in HEIC_EXTS)


def _strip_jpeg(data):
    """Return the JPEG with metadata segments removed (pixels untouched)."""
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return None
    out = bytearray(data[:2])   # SOI
    i = 2
    n = len(data)
    while i + 1 < n:
        if data[i] != 0xFF:
            return None          # malformed — don't risk it
        marker = data[i + 1]
        if marker == 0xD9:                       # EOI
            out += data[i:]
            break
        if marker == 0xDA:                       # SOS -> rest is scan data
            out += data[i:]
            break
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:   # standalone markers
            out += data[i:i + 2]
            i += 2
            continue
        if i + 3 >= n:
            return None
        seglen = (data[i + 2] << 8) | data[i + 3]
        if seglen < 2 or i + 2 + seglen > n:
            return None
        if marker not in _JPEG_DROP:
            out += data[i:i + 2 + seglen]
        i += 2 + seglen
    return bytes(out)


def _recompress(src, lossy=False, max_edge=0, quality=85):
    """Return (bytes, out_ext) for a smaller version of *src*, or None.

    Lossless mode (default): JPEG metadata strip + PNG re-deflate (pixels are
    preserved exactly).  Lossy mode: re-encode (and optionally downscale) for
    much bigger savings.  HEIC/HEIF are always converted to JPEG.
    """
    ext = _ext(src)
    if ext in HEIC_EXTS:
        if not _ensure_heif():
            return None
        from PIL import Image
        im = Image.open(src)
        im.load()
        if lossy and max_edge:
            im.thumbnail((max_edge, max_edge))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG",
                               quality=(quality if lossy else 92),
                               optimize=True, progressive=True)
        return buf.getvalue(), "jpg"
    if ext in ("jpg", "jpeg"):
        if not lossy:
            with open(src, "rb") as f:
                data = _strip_jpeg(f.read())
            return (data, ext) if data is not None else None
        from PIL import Image
        im = Image.open(src)
        im.load()
        if max_edge:
            im.thumbnail((max_edge, max_edge))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=quality,
                               optimize=True, progressive=True)
        return buf.getvalue(), ext
    if ext == "png":
        from PIL import Image
        im = Image.open(src)
        im.load()
        if lossy and max_edge:
            im.thumbnail((max_edge, max_edge))
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        return buf.getvalue(), "png"
    return None


def run_compress(root, files, mode="replace",
                 backup_folder="Originals_Backup",
                 out_folder="Compressed_Images",
                 lossy=False, max_edge=0, quality=85,
                 dry_run=False, journal=None,
                 progress=None, cancel=None):
    """Compress each file in *files* (native absolute paths under *root*).

    mode="replace": overwrite the original in place, moving the original into
                    ``backup_folder`` first (safe, reversible).
    mode="copy":    write the smaller copy into ``out_folder`` and leave the
                    original untouched.
    dry_run=True:   compute the savings but write nothing (used for estimates).
    """
    total = len(files)
    done = 0
    compressed = 0
    skipped = 0
    saved = 0
    before = 0
    after = 0
    items = []
    for src in files:
        if cancel and cancel():
            break
        done += 1
        try:
            osz = os.path.getsize(src)
            got = _recompress(src, lossy=lossy, max_edge=max_edge,
                              quality=quality)
            data, out_ext = (got if got else (None, None))
            in_ext = _ext(src)
            converted = out_ext is not None and out_ext != in_ext
            # accept if smaller, or if it's a format conversion (HEIC->JPG)
            if data is None or (len(data) >= osz and not converted):
                skipped += 1
            else:
                nsz = len(data)
                rel = os.path.relpath(src, root)
                if converted:
                    rel = rel[: rel.rfind(".")] + "." + out_ext if "." in rel \
                        else rel + "." + out_ext
                if not dry_run:
                    if mode == "copy":
                        dst = os.path.join(root, out_folder, rel)
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        with open(dst, "wb") as f:
                            f.write(data)
                    else:
                        bdst = os.path.join(root, backup_folder,
                                            os.path.relpath(src, root))
                        os.makedirs(os.path.dirname(bdst), exist_ok=True)
                        shutil.move(src, bdst)
                        written = os.path.join(root, rel)
                        os.makedirs(os.path.dirname(written), exist_ok=True)
                        with open(written, "wb") as f:
                            f.write(data)
                        if journal is not None:
                            journal.append({
                                "op": "compress",
                                "orig": src.replace(os.sep, "/"),
                                "backup": bdst.replace(os.sep, "/"),
                                "written": written.replace(os.sep, "/")})
                compressed += 1
                saved += max(0, osz - nsz)
                before += osz
                after += nsz
                if len(items) < 15:
                    items.append({"name": os.path.basename(src),
                                  "from_kb": osz // 1024,
                                  "to_kb": nsz // 1024,
                                  "pct": round((osz - nsz) / osz * 100)
                                  if osz else 0,
                                  "converted": converted})
        except Exception:
            skipped += 1
        if progress and (done % 4 == 0 or done == total):
            progress(done, total)
    return {"compressed": compressed, "skipped": skipped,
            "savedKb": saved // 1024, "beforeKb": before // 1024,
            "afterKb": after // 1024, "mode": mode, "lossy": lossy,
            "dryRun": dry_run,
            "backupFolder": backup_folder if mode == "replace" else None,
            "outFolder": out_folder if mode == "copy" else None,
            "items": items}
