"""
Face analysis engine for Hapzea.

This is the part of the app that looks *inside* images: it detects faces and
computes a compact embedding for each, so a guest's selfie can be matched
against every photo in an event folder.  It is optional — when the extra
packages are missing, ``check_deps()`` reports what to install.

Everything runs locally on the user's machine — no image ever leaves the PC.

Models (downloaded once into ~/.phorg/models):
  * YuNet  — face detector
  * SFace  — face recognition / embedding

Public entry points used by the server:
  * ``facefind``     — match a selfie against a set of photos (cached)
  * ``index_faces``  — pre-embed a folder so the first search is instant
"""
import os

# Best-effort HEIC/HEIF support (iPhone photos & selfies).  Optional — if the
# package isn't installed, HEIC simply falls back to whatever the platform can
# decode.  Registered once at import so PIL's Image.open() handles .heic files.
try:
    import pillow_heif  # noqa: F401
    pillow_heif.register_heif_opener()
except Exception:
    pass


IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "bmp", "heic", "heif",
              "tif", "tiff", "gif"}
VIDEO_EXTS = {"mp4", "mkv", "avi", "mov", "3gp", "webm", "m4v",
              "flv", "wmv", "mpg", "mpeg"}


def is_image(name):
    dot = name.rfind(".")
    return dot > 0 and name[dot + 1:].lower() in IMAGE_EXTS


def is_video(name):
    dot = name.rfind(".")
    return dot > 0 and name[dot + 1:].lower() in VIDEO_EXTS


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


# ===========================================================================
# Face detection + recognition models — OpenCV YuNet + SFace
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
        # Detection runs at <=1600px on the long edge (see embed/_embeds_from_img),
        # so decoding a 24MP photo at full resolution just to shrink it is wasted
        # work.  Decode big JPEGs at a reduced libjpeg scale that still stays
        # >=1600px — the detector sees identical input, but decoding is several
        # times faster and uses far less memory.  Accuracy is unchanged.
        flag = cv2.IMREAD_COLOR
        try:
            from PIL import Image
            with Image.open(path) as im:
                mx = max(im.size)
            if mx >= 6400:
                flag = cv2.IMREAD_REDUCED_COLOR_4     # /4 -> long edge >=1600
            elif mx >= 3200:
                flag = cv2.IMREAD_REDUCED_COLOR_2     # /2 -> long edge >=1600
        except Exception:
            flag = cv2.IMREAD_COLOR
        img = cv2.imread(path, flag)
        if img is None and flag != cv2.IMREAD_COLOR:
            img = cv2.imread(path)          # reduced decode unsupported for this file
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


def facefind(native_paths, selfie_path, threshold=0.44,
             progress=None, cancel=None, cache_dir=None, workers=None,
             on_match=None):
    """Find every photo in *native_paths* containing the face in *selfie_path*.

    ``selfie_path`` may be a single path or a list of selfies of the same
    person — several are averaged into one reference embedding for more robust
    matching (better recall, fewer false positives).

    When ``cache_dir`` is given, each photo's face embeddings are cached there
    by (path, size, mtime), so repeated searches over the same event folder
    (e.g. many guests) analyse every photo only once.

    The one-time (cache-miss) embedding pass runs across multiple CPU cores:
    OpenCV releases the GIL while decoding and running the models, so a small
    thread pool processes several photos at once.  Cache reads/writes and score
    bookkeeping stay on the calling thread (the SQLite cache and result lists
    are single-threaded), so only the heavy per-photo work is parallelised.

    ``on_match`` (optional) is called with each match dict as it is found, so a
    caller can stream results to the UI while the scan is still running.

    Returns {"matches": [{"path", "score", "mtime", "faces"}], "count"} or
    {"error": ...}.
    """
    import numpy as np
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Accept one selfie or several; average them into a single unit reference.
    selfies = selfie_path if isinstance(selfie_path, (list, tuple)) else [selfie_path]
    emb0 = FaceEmbedder()
    refs = [v for v in (emb0.embed(sp) for sp in selfies if sp) if v is not None]
    if not refs:
        return {"error": "no_face_in_selfie", "matches": [], "count": 0}
    ref = np.mean(refs, axis=0)
    _rn = float(np.linalg.norm(ref))
    if _rn > 0:
        ref = ref / _rn

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
        # Photos crammed with faces (posters / collages) are a common source of
        # false matches, so require a slightly stronger score for them.
        thr = threshold + (0.08 if len(vecs) >= 12 else 0.0)
        if best >= thr:
            try:
                mt = int(os.path.getmtime(path))
            except OSError:
                mt = 0
            m = {"path": path, "score": round(best, 3), "mtime": mt,
                 "faces": len(vecs)}
            matches.append(m)
            if on_match:
                try:
                    on_match(m)
                except Exception:
                    pass
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


def index_faces(native_paths, progress=None, cancel=None, cache_dir=None,
                workers=None):
    """Pre-compute and cache face embeddings for *native_paths* — no matching.

    This warms the very same cache ``facefind`` uses (``faces.db``, keyed by
    path+size+mtime), so a later search over the folder is near-instant because
    every photo is already embedded.  Meant to run in the background as soon as
    an event folder is opened, turning the slow "first search" into a fast one.

    Returns {"indexed": <photos seen>, "cached": <newly embedded>}.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not cache_dir:
        return {"indexed": 0, "cached": 0}
    try:
        from .hashcache import MetaCache
        cache = MetaCache(cache_dir, name="faces.db")
    except Exception:
        return {"indexed": 0, "cached": 0}

    if workers is None:
        workers = max(1, min(8, os.cpu_count() or 2))

    total = len(native_paths)
    done = 0
    newly = 0
    local = threading.local()

    def embed_one(path):
        return path, _thread_local_embedder(local).embed_all(path)

    try:
        # Skip photos already cached; collect the misses.
        pending = []
        for p in native_paths:
            if cancel and cancel():
                break
            key = cache.stat_key(p)
            if key is not None and cache.get(p, key[0], key[1]) is not None:
                done += 1
                if progress:
                    progress(done, total, newly)
            else:
                pending.append((p, key))

        if pending and not (cancel and cancel()):
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
                        done += 1
                        continue
                    if key is not None:
                        try:
                            cache.put(path, key[0], key[1],
                                      {"fe": [v.tolist() for v in vecs]})
                        except Exception:
                            pass
                    newly += 1
                    done += 1
                    if progress:
                        progress(done, total, newly)
                    if cancel and cancel():
                        break
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
                try:
                    cv2.setNumThreads(prev_threads)
                except Exception:
                    pass
    finally:
        cache.close()
    return {"indexed": done, "cached": newly}


