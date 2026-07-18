"""Core regression tests for Hapzea — pure logic, no cv2 required.

Run with:  python -m pytest -q
"""
import os
import tempfile
import shutil

import pytest

from phorg.safety import SafetyPolicy
from phorg.backends import LocalBackend, Op, _unique_local, _same_file
from phorg import vision


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
# vision — pure helpers (no cv2)
# --------------------------------------------------------------------------
def test_is_image_video():
    assert vision.is_image("a.JPG")
    assert vision.is_image("b.heic")
    assert not vision.is_image("c.txt")
    assert vision.is_video("clip.MP4")
    assert not vision.is_video("d.png")


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
