"""
Classification rules — pure functions that decide where a file belongs.

These encode the rule-based logic used during the manual reorg:
  * type/category buckets by extension
  * magic-byte detection for files with broken / missing extensions
  * size tiers
  * junk detection
"""
import posixpath

# ---------------------------------------------------------------------------
# Extension -> destination sub-path (relative folders created under the root)
# ---------------------------------------------------------------------------
EXT_CATEGORY = {}
def _reg(cat, exts):
    for e in exts.split():
        EXT_CATEGORY[e] = cat

_reg("Images",            "jpg jpeg png gif webp bmp heic heif tiff tif svg ico")
_reg("Videos",            "mp4 mkv avi mov 3gp webm m4v flv wmv mpg mpeg")
_reg("Audio",             "mp3 wav m4a aac opus ogg flac amr wma")
_reg("Documents/PDFs",    "pdf")
_reg("Documents/Office",  "doc docx xls xlsx ppt pptx odt ods odp")
_reg("Documents/Other",   "txt rtf csv md json xml log html htm epub mobi")
_reg("Archives",          "zip rar 7z tar gz bz2 xz")
_reg("Installers",        "apk exe msi appx deb rpm dmg")
_reg("Code",              "py js ts java c cpp cs go rb php sh bat ps1 sql yml yaml")

CATEGORY_ORDER = [
    "Images", "Videos", "Audio", "Documents/PDFs", "Documents/Office",
    "Documents/Other", "Archives", "Installers", "Code", "Other",
]

# ---------------------------------------------------------------------------
# Magic-byte signatures (offset, hex-prefix, extension)
# ---------------------------------------------------------------------------
MAGIC = [
    (0, "89504e47", "png"),
    (0, "ffd8ff",   "jpg"),
    (0, "474946",   "gif"),
    (0, "25504446", "pdf"),
    (0, "49492a00", "tiff"),
    (0, "4d4d002a", "tiff"),
    (0, "504b0304", "zip"),     # also docx/xlsx/pptx/apk (OOXML) — refined below
    (0, "d0cf11e0", "doc"),     # legacy OLE (doc/xls/ppt)
    (0, "4d5a",     "exe"),
    (0, "494433",   "mp3"),
    (0, "fffb",     "mp3"),
    (0, "1f8b",     "gz"),
    (0, "526172211a", "rar"),
    (0, "377abcaf271c", "7z"),
    (0, "3c3f786d6c", "xml"),
    (0, "3c68746d6c", "html"),
    (0, "3c21444f43", "html"),  # <!DOC
]

# bytes 4..8 == 'ftyp' -> mp4 container
def _is_mp4(head):
    return len(head) >= 8 and head[4:8] == b"ftyp"


def detect_type(head_bytes):
    """Return a best-guess extension from the first bytes, or None."""
    if not head_bytes:
        return None
    hexs = head_bytes.hex()
    if _is_mp4(head_bytes):
        return "mp4"
    for off, pref, ext in MAGIC:
        if hexs.startswith(pref):
            return ext
    return None


def refine_zip(names):
    """Given the entry names inside a ZIP/OOXML container, refine the type."""
    joined = "\n".join(names)
    if "word/" in joined:
        return "docx"
    if "xl/" in joined:
        return "xlsx"
    if "ppt/" in joined:
        return "pptx"
    if "AndroidManifest.xml" in joined or "classes.dex" in joined:
        return "apk"
    return "zip"


def ext_of(name):
    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1:
        return ""
    return name[dot + 1:].lower()


def category_for_ext(ext, overrides=None):
    ext = ext.lower()
    if overrides and ext in overrides:
        return overrides[ext]
    return EXT_CATEGORY.get(ext, "Other")


# ---------------------------------------------------------------------------
# Size tiers
# ---------------------------------------------------------------------------
def size_tier(size_bytes):
    mb = size_bytes / (1024 * 1024)
    if mb >= 50:
        return "1_Huge_over_50MB"
    if mb >= 20:
        return "2_Large_20_50MB"
    if mb >= 5:
        return "3_Medium_5_20MB"
    if mb >= 1:
        return "4_Small_1_5MB"
    return "5_Tiny_under_1MB"


SIZE_TIER_ORDER = [
    "1_Huge_over_50MB", "2_Large_20_50MB", "3_Medium_5_20MB",
    "4_Small_1_5MB", "5_Tiny_under_1MB",
]


# ---------------------------------------------------------------------------
# Junk detection
# ---------------------------------------------------------------------------
JUNK_EXTS = {"tmp", "temp", "log", "cache", "crdownload", "part", "partial",
             "bak", "old", "dmp", "thumb"}


def is_junk(name, size_bytes, extra_exts=None):
    """Heuristic: obvious throwaway files. Never flags media/docs."""
    if size_bytes == 0:
        return True, "zero-byte"
    ext = ext_of(name)
    if ext in JUNK_EXTS or (extra_exts and ext in extra_exts):
        return True, f".{ext}"
    low = name.lower()
    if low.startswith("~$") or low.endswith(".tmp"):
        return True, "temp"
    return False, ""
