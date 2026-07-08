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
}

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


def perceptual_groups(native_paths, max_distance=8, progress=None, cancel=None):
    """Group visually near-identical images (resized / re-saved / lightly
    edited copies) across a whole tree.  Each image is reduced to two
    perceptual fingerprints — a DCT ``pHash`` (robust to JPEG re-compression
    and brightness/contrast changes) and a gradient ``dHash`` (robust to
    structure).  Two photos are treated as near-duplicates only when *both*
    fingerprints agree, which catches more genuine copies while rejecting
    coincidental look-alikes.  Returns a list of member-path lists (2+ only)."""
    cat = Categorizer({})
    hashes = []
    total = len(native_paths)
    for i, p in enumerate(native_paths):
        if cancel and cancel():
            break
        try:
            gray, _w, _h = cat._load_gray(p)
            if gray is not None:
                hashes.append((p, cat._phash(gray), cat._dhash(gray)))
        except Exception:
            pass
        if progress and (i % 5 == 0 or i == total - 1):
            progress(i + 1, total)
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
    return [sorted(m) for m in groups.values() if len(m) > 1]


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
    faces = metrics.get("faces", 0)
    if enabled.get("people") and metrics.get("person") and faces <= 1:
        return person_folder(metrics["person"])
    if faces == 0:
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
        img = cv2.imread(path)
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
        import numpy as np
        cv2 = self.cv2
        img = self._read(path)
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


def facefind(native_paths, selfie_path, threshold=0.40,
             progress=None, cancel=None):
    """Find every photo in *native_paths* containing the face in *selfie_path*.

    Returns {"matches": [{"path", "score"}], "count"} or {"error": ...}.
    """
    import numpy as np
    emb = FaceEmbedder()
    ref = emb.embed(selfie_path)
    if ref is None:
        return {"error": "no_face_in_selfie", "matches": [], "count": 0}
    matches = []
    total = len(native_paths)
    done = 0
    for p in native_paths:
        if cancel and cancel():
            break
        done += 1
        best = -1.0
        for v in emb.embed_all(p):
            s = float(np.dot(v, ref))
            if s > best:
                best = s
        if progress:
            progress(done, total, len(matches))
        if best >= threshold:
            matches.append({"path": p, "score": round(best, 3)})
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
