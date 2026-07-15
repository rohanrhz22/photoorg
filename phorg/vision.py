"""
Content-based image analysis for the photo categorizer.

This is the one part of phorg that looks *inside* images instead of only at
file names.  It is optional: the rest of the app works without it.  When the
extra packages are missing, ``check_deps()`` reports what to install and the
feature stays disabled.

Everything runs locally on the user's machine — no image ever leaves the PC.

Pipeline (per image, single pass):
  * blur / shake     -> variance of the Laplacian (focus measure)
  * face count       -> OpenCV Haar cascade (single / couple / group / scenery)
  * bride recognition-> LBPH recognizer trained on the user's sample photos
  * near-duplicates  -> perceptual dHash + Hamming distance to earlier frames

Category precedence (first match wins):
    Blurry_Shaken > Duplicates > Bride_Solo > Group/Couple/Portrait > Scenery
A category that the user did not enable is skipped (the file is left in place).
"""
import os
import re

# Folder names created under the root, one per category.
CATEGORIES = {
    "screenshot": "Screenshots",
    "document":   "Documents_Scans",
    "best":       "Best_Shots",
    "blurry":     "Blurry_Shaken",
    "duplicates": "Duplicates",
    "similar":    "Similar_Extras",
    "group":      "Group_Photos",
    "couple":     "Couples",
    "single":     "Portraits_Single",
    "scenery":    "Scenery_Others",
    "video":      "Videos",
    # AI content types (needs the MobileNet classifier model)
    "animals":    "Animals",
    "food":       "Food",
    "nature":     "Nature_Scenery",
    "plants":     "Plants_Flowers",
    "vehicles":   "Vehicles",
}

# Content buckets keyed by ImageNet-1k class index ranges (the class ordering is
# stable: 0-397 are animals, food/scenery/plants sit at the end).  Each entry is
# (inclusive_start, inclusive_end, bucket_key).
SCENE_INDEX_RANGES = (
    (0, 397, "animals"),
    (924, 969, "food"),
    (970, 980, "nature"),
    (984, 998, "plants"),
)
# A few well-known vehicle class indices scattered through the object range.
VEHICLE_INDICES = frozenset({
    403, 404, 405, 407, 408, 409, 436, 444, 468, 472, 484, 510, 511, 517, 547,
    554, 555, 561, 569, 573, 575, 603, 609, 612, 625, 627, 628, 654, 656, 661,
    665, 670, 671, 675, 705, 717, 724, 734, 751, 757, 779, 803, 812, 814, 817,
    820, 829, 833, 847, 864, 866, 867, 870, 871, 874, 895, 908, 913, 914,
})


def scene_bucket_for_index(class_id):
    """Map an ImageNet-1k class index to a friendly content bucket, or None."""
    if class_id in VEHICLE_INDICES:
        return "vehicles"
    for lo, hi, key in SCENE_INDEX_RANGES:
        if lo <= class_id <= hi:
            return key
    return None


IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "bmp", "heic", "heif",
              "tif", "tiff", "gif"}
VIDEO_EXTS = {"mp4", "mkv", "avi", "mov", "3gp", "webm", "m4v",
              "flv", "wmv", "mpg", "mpeg"}


def person_folder(name):
    """Filesystem-safe folder name for a recognised person."""
    safe = re.sub(r"[^A-Za-z0-9 _-]", "", str(name or "")).strip()
    return safe.replace(" ", "_") or "Person"


def is_image(name):
    dot = name.rfind(".")
    return dot > 0 and name[dot + 1:].lower() in IMAGE_EXTS


def perceptual_groups(native_paths, max_distance=8, progress=None, cancel=None,
                      cache_dir=None):
    """Group visually near-identical photos *and* videos (resized / re-saved /
    lightly edited copies, re-encoded clips) across a whole tree.

    Each file is reduced to two perceptual fingerprints — a DCT ``pHash`` and a
    gradient ``dHash`` — and two files are treated as near-duplicates only when
    both agree.  Videos are fingerprinted from their middle frame.  Resolution
    and sharpness are recorded so the caller can keep the best copy.

    When ``cache_dir`` is given, fingerprints are cached by (path, size, mtime)
    so re-scans of an unchanged tree are near-instant.

    Returns ``(groups, meta)`` where ``groups`` is a list of member-path lists
    (2+ only) and ``meta`` maps each path to ``{"pixels":int, "focus":float}``.
    """
    cat = Categorizer({})
    cache = None
    if cache_dir:
        try:
            from .hashcache import MetaCache
            cache = MetaCache(cache_dir)
        except Exception:
            cache = None
    hashes = []          # (path, phash, dhash)
    meta = {}            # path -> {"pixels", "focus"}
    total = len(native_paths)
    try:
        for i, p in enumerate(native_paths):
            if cancel and cancel():
                break
            try:
                sig = None
                key = cache.stat_key(p) if cache is not None else None
                if key is not None:
                    hit = cache.get(p, key[0], key[1])
                    if hit is not None and "ph" in hit:
                        sig = (hit["ph"], hit["dh"], hit.get("px", 0),
                               hit.get("fc", 0.0))
                if sig is None:
                    sig = cat._percept_sig(p)
                    if sig is not None and cache is not None and key is not None:
                        cache.put(p, key[0], key[1],
                                  {"ph": sig[0], "dh": sig[1],
                                   "px": sig[2], "fc": sig[3]})
                if sig is not None:
                    hashes.append((p, sig[0], sig[1]))
                    meta[p] = {"pixels": sig[2], "focus": sig[3]}
            except Exception:
                pass
            if progress and (i % 5 == 0 or i == total - 1):
                progress(i + 1, total)
    finally:
        if cache is not None:
            cache.close()
    n = len(hashes)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # pHash drives matching (better recall on re-compressed / edited copies);
    # dHash acts as a corroborating check, with a looser bound, to suppress
    # false positives from photos that merely share a similar tonal layout.
    dhash_limit = max_distance + (max_distance // 2) + 2
    for i in range(n):
        _p, pi, di = hashes[i]
        for j in range(i + 1, n):
            _q, pj, dj = hashes[j]
            if (bin(pi ^ pj).count("1") <= max_distance
                    and bin(di ^ dj).count("1") <= dhash_limit):
                parent[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(hashes[i][0])
    return [sorted(m) for m in groups.values() if len(m) > 1], meta


def is_video(name):
    dot = name.rfind(".")
    return dot > 0 and name[dot + 1:].lower() in VIDEO_EXTS


def exif_gps(path):
    """Return (lat, lon) from a photo's EXIF GPS, or None."""
    try:
        from PIL import Image
        exif = Image.open(path).getexif()
        gps = exif.get_ifd(0x8825)  # GPSInfo IFD
        if not gps:
            return None
        def _dms(v):
            return float(v[0]) + float(v[1]) / 60.0 + float(v[2]) / 3600.0
        lat = _dms(gps[2]); lon = _dms(gps[4])
        if str(gps.get(1, "N")).upper().startswith("S"):
            lat = -lat
        if str(gps.get(3, "E")).upper().startswith("W"):
            lon = -lon
        return round(lat, 6), round(lon, 6)
    except Exception:
        return None


def check_deps():
    """Return a list of missing pip package names ([] means ready)."""
    missing = []
    try:
        import numpy  # noqa: F401
    except Exception:
        missing.append("numpy")
    try:
        import cv2  # noqa: F401
    except Exception:
        missing.append("opencv-contrib-python")
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        missing.append("Pillow")
    return missing


def bride_supported():
    """True if OpenCV's face module (LBPH recognizer) is available."""
    try:
        import cv2
        return hasattr(cv2, "face")
    except Exception:
        return False


def _cascade_path():
    """Find the Haar face cascade, whether running from source or a frozen exe."""
    import sys
    import cv2
    candidates = []
    try:
        candidates.append(os.path.join(cv2.data.haarcascades,
                                       "haarcascade_frontalface_default.xml"))
    except Exception:
        pass
    try:
        candidates.append(os.path.join(os.path.dirname(cv2.__file__), "data",
                                       "haarcascade_frontalface_default.xml"))
    except Exception:
        pass
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(os.path.join(base, "cv2", "data",
                                       "haarcascade_frontalface_default.xml"))
        candidates.append(os.path.join(base, "phorg",
                                       "haarcascade_frontalface_default.xml"))
        candidates.append(os.path.join(base,
                                       "haarcascade_frontalface_default.xml"))
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return candidates[0] if candidates else ""


def category_for(metrics, enabled):
    """Map raw per-image metrics + enabled flags to a destination folder.

    ``metrics`` comes from Categorizer.classify(); ``enabled`` is a dict of
    category-key -> bool.  Returns a folder name or None (leave in place).
    """
    if metrics.get("unreadable"):
        return None
    if enabled.get("screenshot") and metrics.get("screenshot"):
        return CATEGORIES["screenshot"]
    if enabled.get("document") and metrics.get("document"):
        return CATEGORIES["document"]
    if enabled.get("best") and metrics.get("is_best"):
        return CATEGORIES["best"]
    if enabled.get("blurry") and metrics.get("is_blurry"):
        return CATEGORIES["blurry"]
    if enabled.get("duplicates") and metrics.get("is_dup"):
        return CATEGORIES["duplicates"]
    if enabled.get("similar") and metrics.get("is_similar"):
        return CATEGORIES["similar"]
    # a remembered person (face database) — most specific, wins first
    if metrics.get("known_person"):
        return person_folder(metrics["known_person"])
    # recognised people-group (Family / School / …) wins over generic buckets
    if metrics.get("people_group"):
        return person_folder(metrics["people_group"])
    faces = metrics.get("faces", 0)
    if enabled.get("people") and metrics.get("person") and faces <= 1:
        return person_folder(metrics["person"])
    if faces == 0:
        scene = metrics.get("scene_type")
        if scene and enabled.get(scene):
            return CATEGORIES[scene]
        return CATEGORIES["scenery"] if enabled.get("scenery") else None
    if faces == 1:
        return CATEGORIES["single"] if enabled.get("single") else None
    if faces == 2:
        return CATEGORIES["couple"] if enabled.get("couple") else None
    return CATEGORIES["group"] if enabled.get("group") else None


class Categorizer:
    """Stateful analyzer.  Create once, call classify() per image."""

    def __init__(self, options=None):
        import cv2
        import numpy as np
        self.cv2 = cv2
        self.np = np
        options = options or {}
        self.blur_threshold = float(options.get("blur_threshold", 50.0))
        self.dup_distance = int(options.get("dup_distance", 8))
        self._proc_width = 1024

        cascade_path = _cascade_path()
        self.face_cascade = cv2.CascadeClassifier(cascade_path)
        self.face_error = None
        if self.face_cascade.empty():
            self.face_error = ("Face detector could not load — single/couple/"
                               "group and bride matching will be inaccurate.")

        self._hashes = []          # kept perceptual hashes (for dup detection)
        self.people_names = []     # index == LBPH label
        self.people_recognizer = None
        self.people_error = None
        self.person_threshold = float(
            options.get("person_threshold",
                        options.get("bride_threshold", 78.0)))
        people = list(options.get("people") or [])
        # backward compatibility with the old single-"bride" option
        if options.get("bride") and options.get("bride_samples"):
            people.append({"name": "Bride",
                           "samples": options.get("bride_samples")})
        if people:
            self._train_people(people)

        # optional AI content classifier (Animals / Food / Nature / Plants /
        # Vehicles) — only loaded when asked for and the model is present.
        self.scene_enabled = bool(options.get("scene"))
        self.classifier = None
        self.classifier_error = None
        if self.scene_enabled:
            if classifier_present():
                try:
                    self.classifier = ImageNetClassifier()
                except Exception as e:
                    self.classifier_error = f"Content classifier failed: {e}"
            else:
                self.classifier_error = "Content classifier model not downloaded."

        # optional people-groups (Family / School / College …) matched by face
        self.people_groups = list(options.get("people_groups") or [])
        self.group_threshold = float(options.get("group_threshold", 0.40))
        self._group_refs = None
        self._grp_embedder = None
        self.group_error = None
        if self.people_groups:
            if cluster_api_available() and models_present():
                try:
                    self._grp_embedder = FaceEmbedder()
                    self._group_refs = embed_groups(self.people_groups) or None
                except Exception as e:
                    self.group_error = f"People groups failed: {e}"
            else:
                self.group_error = "People groups need the face models (auto-download)."

        # optional: auto-file people remembered in the face database
        self._known_refs = None
        if options.get("known_people"):
            if cluster_api_available() and models_present():
                try:
                    from . import peopledb
                    if self._grp_embedder is None:
                        self._grp_embedder = FaceEmbedder()
                    self._known_refs = peopledb.refs() or None
                except Exception as e:
                    self.group_error = f"Known people failed: {e}"
            elif not self.group_error:
                self.group_error = "Known people need the face models (auto-download)."

    # -- image loading ------------------------------------------------------
    def _load(self, path):
        """Load an image and derive resolution-independent sharpness.

        Returns (bgr_for_faces, gray_for_faces, w, h, focus, edge) or Nones.
        ``focus``/``edge`` are measured on a fixed 800px-wide copy so they are
        comparable across photos of very different original resolutions.
        """
        cv2, np = self.cv2, self.np
        img = cv2.imread(path)
        if img is None:
            try:
                from PIL import Image
                im = Image.open(path).convert("RGB")
                img = np.array(im)[:, :, ::-1].copy()  # RGB -> BGR
            except Exception:
                return None, None, 0, 0, 0.0, 0.0
        h0, w0 = img.shape[:2]

        # fixed-width sharpness measure (resolution independent)
        fw = 800
        if w0 >= fw:
            fg = cv2.resize(img, (fw, max(1, int(h0 * fw / w0))),
                            interpolation=cv2.INTER_AREA)
        else:
            fg = img
        fgray = cv2.cvtColor(fg, cv2.COLOR_BGR2GRAY)
        flap = cv2.Laplacian(fgray, cv2.CV_64F)
        focus = float(flap.var())
        edge = float(np.abs(flap).mean())

        # smaller copy for face detection
        if w0 > self._proc_width:
            s = self._proc_width / float(w0)
            img = cv2.resize(img, (int(w0 * s), int(h0 * s)))
        return img, cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), w0, h0, focus, edge

    def _load_gray(self, path):
        _bgr, gray, w, h, _f, _e = self._load(path)
        return gray, w, h

    def _detect_faces(self, gray):
        try:
            faces = self.face_cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=6, minSize=(36, 36))
            return list(faces)
        except Exception:
            return []

    # -- people recognition (LBPH, one label per person) -------------------
    def _train_people(self, people):
        cv2, np = self.cv2, self.np
        if not hasattr(cv2, "face"):
            self.people_error = ("Face recognition needs 'opencv-contrib-python' "
                                 "(you have plain opencv). People matching disabled.")
            return
        faces, labels, names, errs = [], [], [], []
        for person in people:
            name = (person.get("name") or "").strip()
            sdir = person.get("samples")
            if not name:
                continue
            if not sdir or not os.path.isdir(sdir):
                errs.append(f"{name}: sample folder not found")
                continue
            idx = len(names)
            got = 0
            for fn in sorted(os.listdir(sdir)):
                p = os.path.join(sdir, fn)
                if not os.path.isfile(p) or not is_image(fn):
                    continue
                gray, _w, _h = self._load_gray(p)
                if gray is None:
                    continue
                found = self._detect_faces(gray)
                if not found:
                    continue
                x, y, w, h = max(found, key=lambda f: f[2] * f[3])
                faces.append(cv2.resize(gray[y:y + h, x:x + w], (200, 200)))
                labels.append(idx)
                got += 1
            if got == 0:
                errs.append(f"{name}: no clear faces found in samples")
            else:
                names.append(name)
        if faces:
            rec = cv2.face.LBPHFaceRecognizer_create()
            rec.train(faces, np.array(labels))
            self.people_recognizer = rec
            self.people_names = names
        if errs:
            self.people_error = "; ".join(errs)

    def _match_person(self, gray, faces):
        """Return the name of the best-matching known person, or None."""
        if not self.people_recognizer:
            return None
        cv2 = self.cv2
        best_label, best_dist = None, 1e9
        for (x, y, w, h) in faces:
            roi = cv2.resize(gray[y:y + h, x:x + w], (200, 200))
            label, dist = self.people_recognizer.predict(roi)
            if dist < best_dist:
                best_dist, best_label = dist, label
        if (best_label is not None and best_dist < self.person_threshold
                and best_label < len(self.people_names)):
            return self.people_names[best_label]
        return None

    # -- perceptual hash for near-duplicates -------------------------------
    def _dhash(self, gray):
        small = self.cv2.resize(gray, (9, 8))
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | int(small[row, col + 1] > small[row, col])
        return bits

    def _phash(self, gray):
        """DCT-based perceptual hash (64-bit).

        More robust than the gradient dHash to JPEG re-compression, resizing
        and brightness/contrast changes, so a resaved, resized or lightly
        edited copy of the same shot still hashes close to the original.
        """
        cv2, np = self.cv2, self.np
        small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
        dct = cv2.dct(np.float32(small))
        low = dct[:8, :8]
        med = float(np.median(low))
        bits = 0
        for v in low.flatten():
            bits = (bits << 1) | int(v > med)
        return bits

    def _video_gray(self, path):
        """Grab a representative grey frame (the middle one) from a video, so
        the same perceptual hashing used for photos also works on clips."""
        cv2 = self.cv2
        cap = cv2.VideoCapture(path)
        try:
            if not cap.isOpened():
                return None
            n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if n > 1:
                cap.set(cv2.CAP_PROP_POS_FRAMES, n // 2)
            ok, frame = cap.read()
            if (not ok or frame is None) and n > 1:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = cap.read()
            if not ok or frame is None:
                return None
            return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        except Exception:
            return None
        finally:
            cap.release()

    def _percept_sig(self, path):
        """Perceptual signature for a photo or video used by the near-duplicate
        finder: ``(phash, dhash, pixels, focus)`` or ``None`` if unreadable.

        ``pixels`` (resolution) and ``focus`` (Laplacian variance = sharpness)
        let the finder pre-select the best copy to keep in each group.
        """
        cv2 = self.cv2
        if is_video(os.path.basename(path)):
            gray = self._video_gray(path)
            if gray is None:
                return None
            h, w = gray.shape[:2]
        else:
            gray, w, h = self._load_gray(path)
            if gray is None:
                return None
        try:
            focus = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        except Exception:
            focus = 0.0
        return (self._phash(gray), self._dhash(gray), int(w) * int(h), focus)


    # -- screenshot / document heuristics ----------------------------------
    def _looks_screenshot(self, path):
        """Screenshots are detected by file name — reliable, no false hits on
        ordinary photos that merely share a screen-ish aspect ratio."""
        base = os.path.basename(path).lower()
        return ("screenshot" in base or "screen shot" in base
                or "screencap" in base or "screen_shot" in base
                or base.startswith("scr-") or "screen recording" in base)

    def classify(self, path):
        bgr, gray, w, h, focus, edge = self._load(path)
        if gray is None:
            return {"unreadable": True}
        cv2, np = self.cv2, self.np

        faces = self._detect_faces(gray)

        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        sat_mean = float(hsv[:, :, 1].mean())
        val_mean = float(hsv[:, :, 2].mean())

        screenshot = self._looks_screenshot(path)
        # document/scan: no people, near-greyscale, bright paper, lots of text
        # edges — kept strict so ordinary bright photos aren't misfiled.
        document = (len(faces) == 0 and sat_mean < 22 and val_mean > 185
                    and edge > 18 and not screenshot)

        # overall keeper quality: sharpness + good exposure + has a face
        sharp = min(focus / 300.0, 1.0)
        expo = max(0.0, 1.0 - abs(val_mean - 128.0) / 128.0)
        quality = round(0.55 * sharp + 0.30 * expo + 0.15 * (1 if faces else 0), 3)

        # AI content type — only for people-free shots (refines "Scenery")
        scene_type = None
        if self.classifier is not None and len(faces) == 0 and not screenshot \
                and not document:
            scene_type, _conf = self.classifier.classify_bgr(bgr)

        # people-group match (Family / School / …) — any member's face counts
        people_group = None
        known_person = None
        if (self._group_refs or self._known_refs) and len(faces) > 0:
            try:
                embeds = self._grp_embedder._embeds_from_img(bgr)
            except Exception:
                embeds = []
            if embeds and self._group_refs:
                best_name, best_sim = None, -1.0
                for v in embeds:
                    for gname, refs in self._group_refs:
                        for r in refs:
                            s = float(np.dot(v, r))
                            if s > best_sim:
                                best_sim, best_name = s, gname
                if best_sim >= self.group_threshold:
                    people_group = best_name
            if embeds and self._known_refs:
                best_name, best_sim = None, -1.0
                for v in embeds:
                    for kname, r in self._known_refs:
                        s = float(np.dot(v, r))
                        if s > best_sim:
                            best_sim, best_name = s, kname
                if best_sim >= self.group_threshold:
                    known_person = best_name

        return {
            "focus": round(focus, 1),
            "is_blurry": focus < self.blur_threshold,
            "faces": len(faces),
            "pixels": w * h,
            "w": w, "h": h,
            "quality": quality,
            "phash": self._dhash(gray),
            "is_dup": False,
            "is_similar": False,
            "screenshot": bool(screenshot),
            "document": bool(document),
            "scene_type": scene_type,
            "people_group": people_group,
            "known_person": known_person,
            "person": self._match_person(gray, faces),
        }


# ===========================================================================
# Automatic face grouping (no sample photos needed) — OpenCV YuNet + SFace
# ===========================================================================
import urllib.request

MODELS_DIR = os.path.join(os.path.expanduser("~"), ".phorg", "models")
_YUNET = ("face_detection_yunet_2023mar.onnx",
          "https://github.com/opencv/opencv_zoo/raw/main/models/"
          "face_detection_yunet/face_detection_yunet_2023mar.onnx")
_SFACE = ("face_recognition_sface_2021dec.onnx",
          "https://github.com/opencv/opencv_zoo/raw/main/models/"
          "face_recognition_sface/face_recognition_sface_2021dec.onnx")
_MOBILENET = ("image_classification_mobilenetv2_2022apr.onnx",
              "https://github.com/opencv/opencv_zoo/raw/main/models/"
              "image_classification_mobilenet/"
              "image_classification_mobilenetv2_2022apr.onnx")


def classifier_present():
    return os.path.exists(os.path.join(MODELS_DIR, _MOBILENET[0]))


def download_classifier(progress=None):
    """Fetch the small MobileNetV2 ImageNet classifier into ~/.phorg/models."""
    os.makedirs(MODELS_DIR, exist_ok=True)
    name, url = _MOBILENET
    dst = os.path.join(MODELS_DIR, name)
    if os.path.exists(dst):
        return True
    tmp = dst + ".part"

    def _hook(blocks, bs, total):
        if progress:
            progress(name, min(blocks * bs, total) if total > 0 else 0, total)

    urllib.request.urlretrieve(url, tmp, reporthook=_hook)
    os.replace(tmp, dst)
    return True


class ImageNetClassifier:
    """MobileNetV2 (ImageNet-1k) content classifier via OpenCV DNN.

    Predicts a photo's dominant subject and maps it to a friendly content
    bucket (Animals / Food / Nature / Plants / Vehicles).  Runs fully offline
    on the CPU; the ~14 MB model is downloaded once into ~/.phorg/models.
    """

    _MEAN = (0.485, 0.456, 0.406)
    _STD = (0.229, 0.224, 0.225)

    def __init__(self):
        import cv2
        import numpy as np
        self.cv2 = cv2
        self.np = np
        self.net = cv2.dnn.readNet(os.path.join(MODELS_DIR, _MOBILENET[0]))

    def _blob(self, bgr):
        cv2, np = self.cv2, self.np
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (256, 256), interpolation=cv2.INTER_AREA)
        rgb = rgb[16:240, 16:240, :]                       # center crop 224
        x = (rgb.astype(np.float32) / 255.0 - self._MEAN) / self._STD
        return x.transpose(2, 0, 1)[np.newaxis, :, :, :].astype(np.float32)

    def classify_bgr(self, bgr, min_conf=0.30):
        """Return (bucket_key, confidence) for a BGR image, or (None, conf)."""
        np = self.np
        try:
            self.net.setInput(self._blob(bgr))
            out = self.net.forward().flatten()
        except Exception:
            return None, 0.0
        e = np.exp(out - out.max())
        probs = e / e.sum()
        cid = int(probs.argmax())
        conf = float(probs[cid])
        if conf < min_conf:
            return None, conf
        return scene_bucket_for_index(cid), conf



def cluster_api_available():
    """True if this OpenCV build has the YuNet detector + SFace recognizer."""
    try:
        import cv2
        return hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF")
    except Exception:
        return False


def models_present():
    return all(os.path.exists(os.path.join(MODELS_DIR, n))
               for n, _ in (_YUNET, _SFACE))


def download_models(progress=None):
    """Fetch the (small) YuNet + SFace ONNX models into ~/.phorg/models."""
    os.makedirs(MODELS_DIR, exist_ok=True)
    for name, url in (_YUNET, _SFACE):
        dst = os.path.join(MODELS_DIR, name)
        if os.path.exists(dst):
            continue
        tmp = dst + ".part"

        def _hook(blocks, bs, total, _n=name):
            if progress:
                progress(_n, min(blocks * bs, total) if total > 0 else 0, total)

        urllib.request.urlretrieve(url, tmp, reporthook=_hook)
        os.replace(tmp, dst)
    return True


class FaceEmbedder:
    def __init__(self):
        import cv2
        self.cv2 = cv2
        y = os.path.join(MODELS_DIR, _YUNET[0])
        s = os.path.join(MODELS_DIR, _SFACE[0])
        self.det = cv2.FaceDetectorYN_create(y, "", (320, 320), 0.7, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF_create(s, "")

    def _read(self, path):
        cv2 = self.cv2
        # Decode large photos at half resolution straight from the JPEG (much
        # faster + far less memory). Faces stay big enough to detect/recognise,
        # and the working image is capped again below anyway. Only kicks in for
        # big images (>=2600px) so small photos keep full detail.
        flag = cv2.IMREAD_COLOR
        try:
            from PIL import Image
            with Image.open(path) as im:
                if max(im.size) >= 2600:
                    flag = cv2.IMREAD_REDUCED_COLOR_2
        except Exception:
            pass
        img = cv2.imread(path, flag)
        if img is None:
            try:
                from PIL import Image
                import numpy as np
                img = np.array(Image.open(path).convert("RGB"))[:, :, ::-1].copy()
            except Exception:
                return None
        return img

    def embed(self, path):
        """Return a unit-norm embedding of the largest face, or None."""
        import numpy as np
        cv2 = self.cv2
        img = self._read(path)
        if img is None:
            return None
        h, w = img.shape[:2]
        if max(h, w) > 1600:
            sc = 1600.0 / max(h, w)
            img = cv2.resize(img, (int(w * sc), int(h * sc)))
            h, w = img.shape[:2]
        try:
            self.det.setInputSize((w, h))
            _, faces = self.det.detect(img)
        except Exception:
            return None
        if faces is None or len(faces) == 0:
            return None
        faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
        try:
            aligned = self.rec.alignCrop(img, faces[0])
            feat = self.rec.feature(aligned)
        except Exception:
            return None
        v = np.asarray(feat, dtype="float32").flatten()
        n = float(np.linalg.norm(v))
        return (v / n) if n > 0 else None

    def embed_all(self, path, max_faces=12):
        """Return unit-norm embeddings for *all* detected faces (largest first).

        Needed so a guest who is only a small face in a group shot is still
        matched — ``embed`` only looks at the single largest face.
        """
        return self._embeds_from_img(self._read(path), max_faces)

    def _embeds_from_img(self, img, max_faces=12):
        """Unit-norm embeddings for every face in an already-loaded BGR image.

        Lets callers that have already decoded the image (e.g. to measure
        sharpness) avoid reading it from disk a second time.
        """
        import numpy as np
        cv2 = self.cv2
        if img is None:
            return []
        h, w = img.shape[:2]
        if max(h, w) > 1600:
            sc = 1600.0 / max(h, w)
            img = cv2.resize(img, (int(w * sc), int(h * sc)))
            h, w = img.shape[:2]
        try:
            self.det.setInputSize((w, h))
            _, faces = self.det.detect(img)
        except Exception:
            return []
        if faces is None or len(faces) == 0:
            return []
        faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)[:max_faces]
        out = []
        for f in faces:
            try:
                aligned = self.rec.alignCrop(img, f)
                feat = self.rec.feature(aligned)
            except Exception:
                continue
            v = np.asarray(feat, dtype="float32").flatten()
            n = float(np.linalg.norm(v))
            if n > 0:
                out.append(v / n)
        return out


def _thread_local_embedder(local):
    """Return this worker thread's own FaceEmbedder, creating it on first use.

    Each thread needs its own detector/recognizer: ``FaceDetectorYN`` keeps
    mutable state (``setInputSize``), so a single instance can't be shared
    safely across threads.  Reusing one embedder per thread means the models
    are loaded ``workers`` times, not once per photo.
    """
    emb = getattr(local, "emb", None)
    if emb is None:
        emb = FaceEmbedder()
        local.emb = emb
    return emb


def facefind(native_paths, selfie_path, threshold=0.40,
             progress=None, cancel=None, cache_dir=None, workers=None):
    """Find every photo in *native_paths* containing the face in *selfie_path*.

    When ``cache_dir`` is given, each photo's face embeddings are cached there
    by (path, size, mtime), so repeated searches over the same event folder
    (e.g. many guests) analyse every photo only once.

    The one-time (cache-miss) embedding pass runs across multiple CPU cores:
    OpenCV releases the GIL while decoding and running the models, so a small
    thread pool processes several photos at once.  Cache reads/writes and score
    bookkeeping stay on the calling thread (the SQLite cache and result lists
    are single-threaded), so only the heavy per-photo work is parallelised.

    Returns {"matches": [{"path", "score"}], "count"} or {"error": ...}.
    """
    import numpy as np
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ref = FaceEmbedder().embed(selfie_path)
    if ref is None:
        return {"error": "no_face_in_selfie", "matches": [], "count": 0}

    cache = None
    if cache_dir:
        try:
            from .hashcache import MetaCache
            cache = MetaCache(cache_dir, name="faces.db")
        except Exception:
            cache = None

    if workers is None:
        workers = max(1, min(8, os.cpu_count() or 2))

    matches = []
    total = len(native_paths)
    done = 0

    def best_score(vecs):
        best = -1.0
        for v in vecs:
            s = float(np.dot(v, ref))
            if s > best:
                best = s
        return best

    def record(path, vecs):
        """Score a photo's faces and keep it if it matches (main thread only)."""
        nonlocal done
        done += 1
        best = best_score(vecs)
        if best >= threshold:
            matches.append({"path": path, "score": round(best, 3)})
        if progress:
            progress(done, total, len(matches))

    local = threading.local()

    def embed_one(path):
        return path, _thread_local_embedder(local).embed_all(path)

    try:
        # Pass 1: serve cache hits inline (cheap) and collect the misses.
        pending = []          # (path, stat_key) needing a fresh embedding
        for p in native_paths:
            if cancel and cancel():
                break
            key = cache.stat_key(p) if cache is not None else None
            hit = None
            if key is not None:
                rec = cache.get(p, key[0], key[1])
                if rec is not None and "fe" in rec:
                    hit = [np.asarray(v, dtype="float32") for v in rec["fe"]]
            if hit is not None:
                record(p, hit)
            else:
                pending.append((p, key))

        # Pass 2: compute the missing embeddings in parallel across cores.
        if pending and not (cancel and cancel()):
            # OpenCV internally spreads a single op over all cores; combined with
            # our own thread pool that oversubscribes the CPU and *slows* things
            # down. Make each op single-threaded and parallelise across photos
            # instead — the right pattern for a batch of many images.
            import cv2
            prev_threads = cv2.getNumThreads()
            cv2.setNumThreads(1)
            pool = ThreadPoolExecutor(max_workers=workers)
            try:
                futs = {pool.submit(embed_one, p): key for (p, key) in pending}
                for fut in as_completed(futs):
                    key = futs[fut]
                    try:
                        path, vecs = fut.result()
                    except Exception:
                        continue
                    if cache is not None and key is not None:
                        try:
                            cache.put(path, key[0], key[1],
                                      {"fe": [v.tolist() for v in vecs]})
                        except Exception:
                            pass
                    record(path, vecs)
                    if cancel and cancel():
                        break
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
                try:
                    cv2.setNumThreads(prev_threads)
                except Exception:
                    pass
    finally:
        if cache is not None:
            cache.close()
    matches.sort(key=lambda m: -m["score"])
    return {"matches": matches, "count": len(matches)}


def cluster_faces(native_paths, threshold=0.363, person_refs=None,
                  match_threshold=0.40, progress=None, cancel=None):
    """Greedy cosine clustering of the main face in each image.

    If ``person_refs`` (list of (name, unit_embedding)) is given, each cluster
    whose centroid matches a known person is tagged with a suggested name.

    Returns a list of clusters sorted by size:
        [{"id", "count", "rep", "members": [paths], "suggested"}]
    """
    import numpy as np
    emb = FaceEmbedder()
    clusters = []   # each: {"centroid", "sum", "members"}
    total = len(native_paths)
    done = 0
    for p in native_paths:
        if cancel and cancel():
            break
        v = emb.embed(p)
        done += 1
        if progress:
            progress(done, total, len(clusters))
        if v is None:
            continue
        best_sim, best_i = -1.0, -1
        for i, c in enumerate(clusters):
            sim = float(np.dot(v, c["centroid"]))
            if sim > best_sim:
                best_sim, best_i = sim, i
        if best_i >= 0 and best_sim >= threshold:
            c = clusters[best_i]
            c["members"].append(p)
            c["sum"] = c["sum"] + v
            nrm = float(np.linalg.norm(c["sum"]))
            c["centroid"] = c["sum"] / nrm if nrm > 0 else c["centroid"]
        else:
            clusters.append({"centroid": v, "sum": v.copy(), "members": [p]})
    clusters.sort(key=lambda c: len(c["members"]), reverse=True)
    out = []
    for i, c in enumerate(clusters):
        suggested = ""
        if person_refs:
            bn, bs = "", -1.0
            for name, ref in person_refs:
                sim = float(np.dot(c["centroid"], ref))
                if sim > bs:
                    bs, bn = sim, name
            if bs >= match_threshold:
                suggested = bn
        out.append({"id": i, "count": len(c["members"]), "rep": c["members"][0],
                    "members": c["members"], "suggested": suggested})
    return out


def embed_people(people):
    """Compute a mean unit embedding per named person from their sample folder.

    ``people`` is a list of {"name", "samples"}. Returns [(name, embedding)].
    """
    import numpy as np
    emb = FaceEmbedder()
    refs = []
    for person in people or []:
        name = (person.get("name") or "").strip()
        sdir = person.get("samples")
        if not name or not sdir or not os.path.isdir(sdir):
            continue
        vecs = []
        for fn in sorted(os.listdir(sdir)):
            p = os.path.join(sdir, fn)
            if not os.path.isfile(p) or not is_image(fn):
                continue
            v = emb.embed(p)
            if v is not None:
                vecs.append(v)
        if vecs:
            m = np.mean(vecs, axis=0)
            n = float(np.linalg.norm(m))
            if n > 0:
                refs.append((name, m / n))
    return refs


def mean_embedding_of(native_paths):
    """Mean unit-norm face embedding (largest face per photo) across the given
    photos — used to remember a named person from their grouped shots.
    Returns a plain list of floats, or None if no face was found."""
    import numpy as np
    emb = FaceEmbedder()
    vecs = []
    for p in native_paths or []:
        v = emb.embed(p)
        if v is not None:
            vecs.append(v)
    if not vecs:
        return None
    m = np.mean(vecs, axis=0)
    n = float(np.linalg.norm(m))
    return (m / n).tolist() if n > 0 else None


def embed_groups(groups):
    """Build face references for named people-groups (Family, School, …).

    Each group is ``{name, samples}`` where ``samples`` is a folder holding
    clear photos of that group's members.  *All* faces found across the folder
    become references, so any member appearing in a photo matches the group.
    Returns ``[(name, [unit_embeddings])]``.
    """
    emb = FaceEmbedder()
    refs = []
    for g in groups or []:
        name = (g.get("name") or "").strip()
        sdir = g.get("samples")
        if not name or not sdir or not os.path.isdir(sdir):
            continue
        vecs = []
        for fn in sorted(os.listdir(sdir)):
            p = os.path.join(sdir, fn)
            if not os.path.isfile(p) or not is_image(fn):
                continue
            vecs.extend(emb.embed_all(p))
        if vecs:
            refs.append((name, vecs))
    return refs


def social_groups(native_paths, cluster_threshold=0.363, min_together=2,
                  progress=None, cancel=None):
    """Discover social circles automatically from who appears *together*.

    People who repeatedly show up in the same photos form a community (a family,
    a friend group, …).  We cluster every face into people, build a
    "co-appearance" graph, split it into communities via weighted label
    propagation, then assign each photo to the community most of its faces
    belong to.  The user names each discovered circle (Family / College / …).

    Returns ``{"groups": [{id, count, people, rep, members}], "photos", "persons"}``.
    """
    import numpy as np
    from collections import defaultdict, Counter
    import random

    emb = FaceEmbedder()
    total = len(native_paths)

    # 1. faces per photo
    photo_faces = []
    for i, p in enumerate(native_paths):
        if cancel and cancel():
            break
        photo_faces.append(emb.embed_all(p))
        if progress and (i % 3 == 0 or i == total - 1):
            progress(i + 1, total)

    # 2. greedy-cluster every face into a person; record who's in each photo
    persons = []            # {centroid, sum, count}
    photo_persons = []      # per photo: set of person indices
    for faces in photo_faces:
        pset = set()
        for v in faces:
            best_sim, best_i = -1.0, -1
            for pi, c in enumerate(persons):
                s = float(np.dot(v, c["centroid"]))
                if s > best_sim:
                    best_sim, best_i = s, pi
            if best_i >= 0 and best_sim >= cluster_threshold:
                c = persons[best_i]
                c["sum"] = c["sum"] + v
                nrm = float(np.linalg.norm(c["sum"]))
                c["centroid"] = c["sum"] / nrm if nrm > 0 else c["centroid"]
                c["count"] += 1
                pset.add(best_i)
            else:
                persons.append({"centroid": v, "sum": v.copy(), "count": 1})
                pset.add(len(persons) - 1)
        photo_persons.append(pset)
    n = len(persons)

    # 3. co-appearance edges (how often two people share a photo)
    edge = defaultdict(int)
    for pset in photo_persons:
        pl = sorted(pset)
        for a in range(len(pl)):
            for b in range(a + 1, len(pl)):
                edge[(pl[a], pl[b])] += 1
    adj = defaultdict(list)
    for (a, b), w in edge.items():
        if w >= min_together:
            adj[a].append((b, w))
            adj[b].append((a, w))

    # 4. communities via weighted label propagation (deterministic seed)
    labels = list(range(n))
    rng = random.Random(0)
    order = list(range(n))
    for _ in range(20):
        rng.shuffle(order)
        changed = False
        for node in order:
            if not adj[node]:
                continue
            wl = defaultdict(float)
            for nbr, w in adj[node]:
                wl[labels[nbr]] += w
            best = max(wl.items(), key=lambda kv: (kv[1], -kv[0]))[0]
            if labels[node] != best:
                labels[node] = best
                changed = True
        if not changed:
            break

    comm_persons = defaultdict(list)
    for pi, lab in enumerate(labels):
        comm_persons[lab].append(pi)

    # 5. assign each photo to the community most of its faces belong to
    comm_photos = defaultdict(list)
    for idx, pset in enumerate(photo_persons):
        if not pset:
            continue
        lab = Counter(labels[pi] for pi in pset).most_common(1)[0][0]
        comm_photos[lab].append(idx)

    out = []
    for lab, photos in comm_photos.items():
        if len(photos) < 2:
            continue
        rep = max(photos, key=lambda i: sum(
            1 for pi in photo_persons[i] if labels[pi] == lab))
        out.append({"id": lab, "count": len(photos),
                    "people": len(comm_persons.get(lab, [])),
                    "rep": native_paths[rep],
                    "members": [native_paths[i] for i in photos]})
    out.sort(key=lambda g: -g["count"])
    for i, g in enumerate(out):
        g["id"] = i
    return {"groups": out, "photos": total, "persons": n}


# ===========================================================================
# AI wedding sorter — couple detection, face-count labels, venue-aware splits
# ===========================================================================
def _haversine_m(a, b):
    """Great-circle distance in metres between two (lat, lon) points."""
    from math import radians, sin, cos, asin, sqrt
    lat1, lon1 = a
    lat2, lon2 = b
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    h = (sin(dlat / 2) ** 2
         + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2)
    return 2 * 6371000.0 * asin(min(1.0, sqrt(h)))


def wedding_ai(items, gap_seconds, names=None, progress=None, cancel=None,
               venue_meters=150.0, match_threshold=0.363):
    """Content-aware wedding sorting.

    ``items`` is a list of ``(ts, lat_or_None, lon_or_None, native_path)``
    sorted by ``ts``.  Each photo is analysed for faces + sharpness; the two
    people appearing in the most photos are taken to be the couple.  Sessions
    are split on time gaps *and* venue changes (GPS), each session is labelled
    from its face content (Group Photos / Couple Portraits / Portraits, else a
    positional function name), and the sharpest frame is chosen as the cover.

    Returns ``{"sessions": [...], "couple": {"count": n}, "photos": n}``.
    """
    import numpy as np
    emb = FaceEmbedder()
    names = names or []

    # -- 1. per-photo face embeddings + sharpness (single image read each) ---
    info = []            # aligned with items: {"embeds","faces","focus"}
    total = len(items)
    for i, (_ts, _la, _lo, path) in enumerate(items):
        if cancel and cancel():
            break
        rec = {"embeds": [], "faces": 0, "focus": 0.0}
        img = emb._read(path)
        if img is not None:
            try:
                gray = emb.cv2.cvtColor(img, emb.cv2.COLOR_BGR2GRAY)
                rec["focus"] = float(emb.cv2.Laplacian(
                    gray, emb.cv2.CV_64F).var())
            except Exception:
                pass
            embeds = emb._embeds_from_img(img)
            rec["embeds"] = embeds
            rec["faces"] = len(embeds)
        info.append(rec)
        if progress and (i % 3 == 0 or i == total - 1):
            progress(i + 1, total)

    # -- 2. cluster every face to find recurring identities ------------------
    clusters = []        # {"centroid","sum","photos": set(idx)}
    for idx, rec in enumerate(info):
        for v in rec["embeds"]:
            best_sim, best_c = -1.0, -1
            for ci, c in enumerate(clusters):
                sim = float(np.dot(v, c["centroid"]))
                if sim > best_sim:
                    best_sim, best_c = sim, ci
            if best_c >= 0 and best_sim >= match_threshold:
                c = clusters[best_c]
                c["sum"] = c["sum"] + v
                nrm = float(np.linalg.norm(c["sum"]))
                c["centroid"] = c["sum"] / nrm if nrm > 0 else c["centroid"]
                c["photos"].add(idx)
            else:
                clusters.append({"centroid": v, "sum": v.copy(),
                                 "photos": {idx}})
    clusters.sort(key=lambda c: len(c["photos"]), reverse=True)
    # the couple = the two identities present in the most photos (if frequent)
    couple = [c for c in clusters[:2] if len(c["photos"]) >= 3]

    def _couple_hits(rec):
        hits = 0
        for c in couple:
            if any(float(np.dot(v, c["centroid"])) >= match_threshold
                   for v in rec["embeds"]):
                hits += 1
        return hits

    # -- 3. split into sessions on time gaps and venue (GPS) changes ---------
    sessions = []
    cur = []
    last_ts = None
    last_gps = None
    for idx, (ts, la, lo, path) in enumerate(items):
        gps = (la, lo) if (la is not None and lo is not None) else None
        split = False
        if last_ts is not None and (ts - last_ts) > gap_seconds:
            split = True
        elif gps and last_gps and _haversine_m(last_gps, gps) > venue_meters:
            split = True
        if split and cur:
            sessions.append(cur)
            cur = []
        cur.append(idx)
        last_ts = ts
        if gps:
            last_gps = gps
    if cur:
        sessions.append(cur)

    # -- 4. label each session from its content + pick the sharpest cover ----
    out = []
    for si, members in enumerate(sessions):
        recs = [info[i] for i in members]
        n = len(members)
        big = sum(1 for r in recs if r["faces"] >= 5)
        both = sum(1 for r in recs if _couple_hits(r) >= 2)
        one = sum(1 for r in recs if r["faces"] == 1)
        couple_pct = round(100.0 * both / n) if n else 0
        avg_faces = round(sum(r["faces"] for r in recs) / n, 1) if n else 0
        if n and big / n >= 0.5:
            label = "Group Photos"
        elif n and both / n >= 0.5:
            label = "Couple Portraits"
        elif n and one / n >= 0.6:
            label = "Portraits"
        elif si < len(names):
            label = names[si]
        else:
            label = f"Session {si + 1}"
        best = max(members, key=lambda i: info[i]["focus"])
        tss = [items[i][0] for i in members]
        out.append({
            "id": si, "count": n,
            "start": int(min(tss)), "end": int(max(tss)),
            "rep": items[best][3],
            "members": [items[i][3] for i in members],
            "suggested": label,
            "faces": avg_faces, "couplePct": couple_pct,
        })
    return {"sessions": out, "couple": {"count": len(couple)},
            "photos": len(items)}

