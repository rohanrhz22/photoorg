"""Core regression tests for phorg — pure logic, no cv2 required.

Run with:  python -m pytest -q
"""
import os
import tempfile
import shutil

import pytest

from phorg.safety import SafetyPolicy
from phorg.backends import LocalBackend, Op, _unique_local, _same_file
from phorg import organizer, vision


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _mk(root, rel, data=b"x"):
    p = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


@pytest.fixture()
def tmproot():
    d = tempfile.mkdtemp()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# safety
# --------------------------------------------------------------------------
def test_safety_defaults_protect_system_and_hidden():
    saf = SafetyPolicy()
    assert saf.is_protected_dir("Android")
    assert saf.is_protected_dir("MIUI/whatever")
    assert saf.is_protected_dir(".thumbnails")
    assert saf.is_protected_dir("")             # the root itself
    assert not saf.is_protected_dir("Holiday/Goa")
    assert saf.is_protected_file(".nomedia")
    assert saf.is_protected_file(".hidden")


def test_safety_extra_protect_and_include_hidden():
    saf = SafetyPolicy(include_hidden=True, extra_protect=["Backups"])
    assert saf.is_protected_dir("Backups")
    assert not saf.is_protected_dir(".thumbs2")   # hidden allowed now


# --------------------------------------------------------------------------
# backends helpers
# --------------------------------------------------------------------------
def test_unique_local(tmproot):
    p = _mk(tmproot, "a.txt")
    u = _unique_local(p)
    assert u.endswith("a_1.txt")


def test_same_file(tmproot):
    a = _mk(tmproot, "a.bin", b"hello")
    b = _mk(tmproot, "b.bin", b"hello")
    c = _mk(tmproot, "c.bin", b"world")
    assert _same_file(a, b)
    assert not _same_file(a, c)


def test_op_repr():
    assert "TRASH" in repr(Op("trash", "x"))
    assert "MOVE" in repr(Op("move", "a", "b"))


# --------------------------------------------------------------------------
# apply_ops: move, unique-rename collision, identical dedupe
# --------------------------------------------------------------------------
def test_apply_move_and_unique_rename(tmproot):
    be = LocalBackend(tmproot)
    _mk(tmproot, "sub/photo.jpg", b"AAA")
    _mk(tmproot, "dest/photo.jpg", b"BBB")   # different content already at dest
    ops = [Op("move", be.join(be.root, "sub/photo.jpg"),
              be.join(be.root, "dest/photo.jpg"))]
    be.apply_ops(ops)
    files = sorted(os.listdir(os.path.join(tmproot, "dest")))
    assert files == ["photo.jpg", "photo_1.jpg"]   # renamed, nothing overwritten


def test_apply_identical_dedupe_removes_source(tmproot):
    be = LocalBackend(tmproot)
    _mk(tmproot, "sub/photo.jpg", b"SAME")
    _mk(tmproot, "dest/photo.jpg", b"SAME")   # byte-identical
    journal = []
    be.apply_ops([Op("move", be.join(be.root, "sub/photo.jpg"),
                      be.join(be.root, "dest/photo.jpg"))], journal=journal)
    # redundant source removed, recorded as reversible "dedupe"
    assert not os.path.exists(os.path.join(tmproot, "sub", "photo.jpg"))
    assert any(j.get("op") == "dedupe" for j in journal)


# --------------------------------------------------------------------------
# organizer planners
# --------------------------------------------------------------------------
def test_norm_dupname():
    assert organizer._norm_dupname("IMG (1)") == organizer._norm_dupname("IMG")
    assert organizer._norm_dupname("photo-copy") == organizer._norm_dupname("photo")


def test_plan_flatten_empties_folders_with_identical_dupes(tmproot):
    be = LocalBackend(tmproot)
    saf = SafetyPolicy()
    _mk(tmproot, "A/photo.jpg", b"IDENTICAL")
    _mk(tmproot, "B/photo.jpg", b"IDENTICAL")   # same name + content
    _mk(tmproot, "B/other.jpg", b"unique")
    ops, _ = organizer.plan_flatten(be, be.root, saf)
    be.apply_ops(ops, journal=[])
    left = sorted(f for f in os.listdir(tmproot) if os.path.isfile(os.path.join(tmproot, f)))
    assert left == ["other.jpg", "photo.jpg"]
    # sub-folders fully emptied + removed
    assert not os.path.isdir(os.path.join(tmproot, "A"))
    assert not os.path.isdir(os.path.join(tmproot, "B"))


def test_duplicate_groups_and_hash_cache(tmproot):
    be = LocalBackend(tmproot)
    saf = SafetyPolicy()
    blob = b"D" * 5000
    _mk(tmproot, "a.bin", blob)
    _mk(tmproot, "b.bin", blob)      # identical
    _mk(tmproot, "c.bin", b"E" * 5000)
    groups = organizer.duplicate_groups(be, be.root, saf)
    assert len(groups) == 1
    assert len(groups[0]["members"]) == 2
    # the hash cache DB was created under .phorg
    assert os.path.isdir(os.path.join(tmproot, ".phorg"))


def test_plan_junk_to_trash_produces_trash_ops(tmproot):
    be = LocalBackend(tmproot)
    saf = SafetyPolicy()
    _mk(tmproot, "thumbs.db", b"junk")
    _mk(tmproot, "keep.jpg", b"data")
    ops, summary = organizer.plan_junk(be, be.root, saf, to_trash=True)
    kinds = {o.kind for o in ops}
    assert summary["to_trash"] is True
    assert kinds == {"trash"} or kinds == set()  # only trash ops, never move


# --------------------------------------------------------------------------
# vision — pure helpers (no cv2)
# --------------------------------------------------------------------------
def test_is_image_video():
    assert vision.is_image("a.JPG")
    assert vision.is_image("b.heic")
    assert not vision.is_image("c.txt")
    assert vision.is_video("clip.MP4")
    assert not vision.is_video("d.png")


def test_scene_bucket_index_ranges():
    assert vision.scene_bucket_for_index(1) == "animals"
    assert vision.scene_bucket_for_index(950) == "food"
    assert vision.scene_bucket_for_index(975) == "nature"
    assert vision.scene_bucket_for_index(990) == "plants"
    assert vision.scene_bucket_for_index(817) == "vehicles"
    assert vision.scene_bucket_for_index(600) is None


def test_haversine_distance():
    # ~ same point -> ~0; two far cities -> large
    assert vision._haversine_m((10.0, 76.0), (10.0, 76.0)) < 1.0
    d = vision._haversine_m((10.0, 76.0), (12.97, 77.59))  # Kochi -> Bengaluru
    assert 300000 < d < 450000


def test_person_folder_safe():
    assert vision.person_folder("Amma / Family!") == "Amma__Family"
    assert vision.person_folder("") == "Person"


# --------------------------------------------------------------------------
# hashcache
# --------------------------------------------------------------------------
def test_metacache_roundtrip_and_invalidation(tmproot):
    from phorg.hashcache import MetaCache
    f = _mk(tmproot, "x.bin", b"hello")
    c = MetaCache(os.path.join(tmproot, ".phorg"))
    key = c.stat_key(f)
    assert key is not None
    assert c.get(f, key[0], key[1]) is None
    c.put(f, key[0], key[1], {"sha1": "abc"})
    assert c.get(f, key[0], key[1]) == {"sha1": "abc"}
    # a different mtime invalidates
    assert c.get(f, key[0], key[1] + 1) is None
    c.close()


# --------------------------------------------------------------------------
# peopledb (needs numpy)
# --------------------------------------------------------------------------
def test_peopledb_roundtrip(tmproot, monkeypatch):
    pytest.importorskip("numpy")
    from phorg import peopledb
    monkeypatch.setattr(peopledb, "_DIR", os.path.join(tmproot, ".phorg"))
    monkeypatch.setattr(peopledb, "_PATH",
                        os.path.join(tmproot, ".phorg", "people.json"))
    assert peopledb.summary() == []
    peopledb.remember("Amma", [1.0, 0.0, 0.0, 0.0])
    names = [x["name"] for x in peopledb.summary()]
    assert names == ["Amma"]
    refs = peopledb.refs()
    assert len(refs) == 1 and refs[0][0] == "Amma"
    peopledb.forget("Amma")
    assert peopledb.summary() == []
