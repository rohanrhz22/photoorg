"""
Safety rules — decide which paths must never be touched.

The whole point of this tool is to reorganise files *without* breaking apps.
That means we hard-protect OS / app-critical folders, hidden folders, and any
path the user explicitly marks as protected.
"""

# Top-level folder names that hold OS / app-critical data. Never moved, never
# entered for reorganisation. (Case-insensitive match on the first path segment.)
DEFAULT_PROTECTED_TOP = {
    "android",          # app sandboxes + databases
    "miui",             # Xiaomi system
    "ringtones",
    "notifications",
    "alarms",
    "lost.dir",
    "system",
    "data",
    "obb",
}

# Folder names anywhere in the path that must be left alone.
DEFAULT_PROTECTED_ANY = {
    ".thumbnails",
    ".globaltrash",     # gallery recycle bin
    "sent",             # WhatsApp/Telegram "Sent" (user's outgoing originals)
    "private",          # WhatsApp "Private"
}

# File names that are structural and should stay where they are.
PROTECTED_FILENAMES = {
    ".nomedia",         # tells the gallery to ignore a folder
}


class SafetyPolicy:
    def __init__(self, protected_top=None, protected_any=None,
                 include_hidden=False, extra_protect=None):
        self.protected_top = set(DEFAULT_PROTECTED_TOP if protected_top is None
                                 else protected_top)
        self.protected_any = set(DEFAULT_PROTECTED_ANY if protected_any is None
                                 else protected_any)
        self.include_hidden = include_hidden
        for p in (extra_protect or []):
            self.protected_top.add(p.lower())

    def _segments(self, rel_path):
        return [s for s in rel_path.replace("\\", "/").split("/") if s]

    def is_protected_dir(self, rel_path):
        """rel_path is POSIX-style, relative to the scan root."""
        segs = self._segments(rel_path)
        if not segs:
            return True  # the root itself
        if segs[0].lower() in self.protected_top:
            return True
        for s in segs:
            low = s.lower()
            if low in self.protected_any:
                return True
            if not self.include_hidden and s.startswith("."):
                return True
        return False

    def is_protected_file(self, name):
        if name in PROTECTED_FILENAMES:
            return True
        if not self.include_hidden and name.startswith("."):
            return True
        return False
