"""Round-trip test for the always-on relay (Phase 3).

Starts the relay in-process on a free port, then exercises the full flow:
publish event -> guest registers -> host pulls -> host posts results ->
guest fetches album + downloads a photo.  No cv2 needed — a fake matcher stands
in for the real face engine.

Run with:  python -m pytest -q tests/test_relay.py
"""
import os
import base64
import tempfile
import threading
import http.server

import pytest

# Isolate the relay's storage before importing it.
_TMP = tempfile.mkdtemp()
os.environ["PHORG_RELAY_HOME"] = _TMP

from phorg import relay_server, relay_client  # noqa: E402


@pytest.fixture(scope="module")
def relay():
    httpd = relay_server.make_server(port=0, host="127.0.0.1")
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_relay_round_trip(relay):
    base = relay

    # 1) host publishes the event
    ev = relay_client.publish_event(base, "priya-arjun", "Priya & Arjun", "SECRET")
    assert ev["id"] == "priya-arjun"

    # 2) guest registers while (pretend) the host PC is offline
    reg = relay_client._post(base, "/api/register", {
        "event": "priya-arjun", "name": "Asha", "contact": "asha@example.com",
        "selfie": "data:image/png;base64," + base64.b64encode(b"selfie").decode(),
    })
    assert reg["rid"] and reg["atoken"]

    # album is pending before the host processes it
    pending = relay_client._get(base, f"/api/album?a={reg['rid']}.{reg['atoken']}")
    assert pending["status"] == "pending"

    # 3) host pulls the queue and 4) matches with a fake matcher
    seen = {}

    def matcher(selfie_bytes, r):
        seen["selfie"] = selfie_bytes
        seen["name"] = r["name"]
        return [
            {"name": "DSC01.jpg", "score": 0.88, "image_bytes": b"IMG-ONE"},
            {"name": "DSC02.jpg", "score": 0.61, "image_bytes": b"IMG-TWO"},
        ]

    processed = relay_client.sync_once(base, "priya-arjun", "SECRET", matcher)
    assert processed == 1
    assert seen["selfie"] == b"selfie" and seen["name"] == "Asha"

    # 5) guest fetches the finished album
    alb = relay_client._get(base, f"/api/album?a={reg['rid']}.{reg['atoken']}")
    assert alb["status"] == "matched"
    assert alb["count"] == 2
    assert [m["name"] for m in alb["matches"]] == ["DSC01.jpg", "DSC02.jpg"]
    assert alb["matches"][0]["has_img"] is True

    # the delivered photo bytes come back
    name, blob = relay_server.result_image(reg["rid"], reg["atoken"], 0)
    assert blob == b"IMG-ONE"

    # queue is now empty for the host
    assert relay_client.pull(base, "priya-arjun", "SECRET") == []


def test_relay_bad_key_rejected(relay):
    relay_client.publish_event(relay, "ev2", "Event Two", "KEYA")
    # wrong key on pull -> forbidden (client raises HTTPError)
    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        relay_client.pull(relay, "ev2", "WRONG")

    # re-publishing the same event with a different key is refused (403)
    with pytest.raises(urllib.error.HTTPError):
        relay_client._post(relay, "/api/event",
                           {"id": "ev2", "name": "x", "key": "WRONG"})

    # the original key still works
    ev = relay_client.publish_event(relay, "ev2", "Event Two", "KEYA")
    assert ev["id"] == "ev2"


def test_relay_tier_b_instant_match(relay):
    base = relay
    relay_client.publish_event(base, "tierb", "Tier B Event", "KEY")

    # host publishes an embedding index: photo A ~ [1,0,0], photo B ~ [0,1,0]
    photos = [
        {"pid": "pA", "name": "A.jpg", "embeds": [[1.0, 0.0, 0.0]],
         "image_bytes": b"IMG-A"},
        {"pid": "pB", "name": "B.jpg", "embeds": [[0.0, 1.0, 0.0]],
         "image_bytes": b"IMG-B"},
    ]
    assert relay_client.publish_index(base, "tierb", "KEY", photos) == 2

    st = relay_client.index_status(base, "tierb", "KEY")
    assert st["count"] == 2 and set(st["pids"]) == {"pA", "pB"}

    # guest matches instantly with a selfie embedding close to photo A
    d = relay_client._post(base, "/api/match", {
        "event": "tierb", "name": "Ravi", "selfie_embedding": [0.95, 0.05, 0.0],
        "threshold": 0.5,
    })
    assert d["count"] == 1
    assert d["matches"][0]["name"] == "A.jpg"

    # album + delivered photo resolve from the published index (PC can be off)
    alb = relay_client._get(base, f"/api/album?a={d['rid']}.{d['atoken']}")
    assert alb["status"] == "matched" and alb["count"] == 1
    name, blob = relay_server.result_image(d["rid"], d["atoken"], 0)
    assert blob == b"IMG-A"


def test_relay_match_multi_face_photo(relay):
    base = relay
    relay_client.publish_event(base, "grp", "Group Event", "KEY")
    # a group photo with two faces; guest matches the second face
    relay_client.publish_index(base, "grp", "KEY", [
        {"pid": "g1", "name": "group.jpg",
         "embeds": [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0]], "image_bytes": b"G"},
    ])
    d = relay_client._post(base, "/api/match", {
        "event": "grp", "selfie_embedding": [0.0, 0.0, 1.0], "threshold": 0.5})
    assert d["count"] == 1 and d["matches"][0]["name"] == "group.jpg"


def test_relay_original_quality_upgrade(relay):
    base = relay
    relay_client.publish_event(base, "orig", "Originals Event", "KEY")
    relay_client.publish_index(base, "orig", "KEY", [
        {"pid": "pX", "name": "X.jpg", "embeds": [[1.0, 0.0]],
         "image_bytes": b"MEDIUM-X"},
    ])

    # guest matches instantly while the PC is off -> medium copy is served,
    # even for a download (no original on the relay yet)
    d = relay_client._post(base, "/api/match", {
        "event": "orig", "selfie_embedding": [1.0, 0.0], "threshold": 0.5})
    assert d["count"] == 1
    _, blob = relay_server.result_image(d["rid"], d["atoken"], 0,
                                        prefer_original=True)
    assert blob == b"MEDIUM-X"

    # PC comes back on: it learns pX needs an original and uploads it
    assert relay_client.originals_needed(base, "orig", "KEY") == ["pX"]
    assert relay_client.upload_original(base, "orig", "KEY", "pX",
                                        b"ORIGINAL-X")["ok"] is True
    assert relay_client.originals_needed(base, "orig", "KEY") == []

    # same album link now downloads full quality; gallery view stays medium
    _, blob = relay_server.result_image(d["rid"], d["atoken"], 0,
                                        prefer_original=True)
    assert blob == b"ORIGINAL-X"
    _, blob = relay_server.result_image(d["rid"], d["atoken"], 0)
    assert blob == b"MEDIUM-X"

    st = relay_client.event_stats(base, "orig", "KEY")
    assert st["originals"] == 1 and st["originals_pending"] == 0

    # deleting the event removes the stored originals too
    relay_client.delete_event(base, "orig", "KEY")
    assert not os.path.isdir(relay_server._orig_dir("orig"))


def test_relay_store_forward_results_carry_pid(relay):
    base = relay
    relay_client.publish_event(base, "sfp", "SF Event", "KEY")
    reg = relay_client._post(base, "/api/register", {
        "event": "sfp", "name": "G",
        "selfie": "data:image/png;base64," + base64.b64encode(b"s").decode()})

    # PC-side matcher tags each result with the photo's pid
    def matcher(_selfie, _r):
        return [{"name": "A.jpg", "score": 0.9, "pid": "sfA",
                 "image_bytes": b"MED-A"}]

    relay_client.sync_once(base, "sfp", "KEY", matcher)
    assert relay_client.originals_needed(base, "sfp", "KEY") == ["sfA"]
    relay_client.upload_original(base, "sfp", "KEY", "sfA", b"ORIG-A")
    _, blob = relay_server.result_image(reg["rid"], reg["atoken"], 0,
                                        prefer_original=True)
    assert blob == b"ORIG-A"

    # wrong key can't push originals or list what's needed
    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        relay_client.upload_original(base, "sfp", "WRONG", "sfA", b"X")
    with pytest.raises(urllib.error.HTTPError):
        relay_client.originals_needed(base, "sfp", "WRONG")


def test_relay_rejects_unsafe_event_id_collision(relay):
    """An attacker must not be able to create an event whose id collapses to
    the same on-disk originals directory as a real event (which would let them
    delete/expire someone else's stored originals)."""
    base = relay
    import urllib.error
    # a legitimate event with path-safe id works
    relay_client.publish_event(base, "real-event-abc123", "Real", "K")
    # an id that only differs by a path-stripped char is refused outright,
    # so it can never share "real-event-abc123"'s originals folder
    with pytest.raises(urllib.error.HTTPError):
        relay_client._post(base, "/api/event",
                           {"id": "real-event-abc123!", "name": "x", "key": "attacker"})
    with pytest.raises(urllib.error.HTTPError):
        relay_client._post(base, "/api/event",
                           {"id": "../../etc/passwd", "name": "x", "key": "attacker"})
    # the colliding id is refused, so no second event can ever share the real
    # event's originals directory and delete/expire it out from under it
    assert relay_server._valid_event_id("real-event-abc123")
    assert not relay_server._valid_event_id("real-event-abc123!")
    assert not relay_server._valid_event_id("../../etc/passwd")


def test_relay_match_scores_matches_scalar():
    """The vectorised matcher must be numerically identical to the pure-Python
    path, so instant-match thresholds behave the same with or without numpy."""
    import random
    random.seed(7)

    for _ in range(30):
        d = random.choice([64, 128, 512])
        queries = [[random.gauss(0, 1) for _ in range(d)]
                   for _ in range(random.randint(1, 3))]
        photos = [[[random.gauss(0, 1) for _ in range(d)]
                   for _ in range(random.randint(0, 4))]
                  for _ in range(random.randint(1, 6))]
        fast = relay_server._match_scores(queries, photos)
        slow = [relay_server._best_cos(queries, embeds) for embeds in photos]
        assert len(fast) == len(slow)
        assert all(abs(a - b) < 1e-9 for a, b in zip(fast, slow))
    # degenerate inputs
    assert relay_server._match_scores([], [[[1.0, 0.0]]]) == [0.0]
    assert relay_server._match_scores([[1.0, 0.0]], []) == []
    assert relay_server._match_scores([[1.0, 0.0]], [[]]) == [0.0]


def test_relay_event_stats(relay):
    base = relay
    relay_client.publish_event(base, "stats", "Stats Event", "KEY")
    relay_client.publish_index(base, "stats", "KEY", [
        {"pid": "p1", "name": "1.jpg", "embeds": [[1.0, 0.0]], "image_bytes": b"1"},
        {"pid": "p2", "name": "2.jpg", "embeds": [[0.0, 1.0]], "image_bytes": b"2"},
    ])
    relay_client._post(base, "/api/register", {
        "event": "stats", "name": "G",
        "selfie": "data:image/png;base64," + base64.b64encode(b"s").decode()})
    st = relay_client.event_stats(base, "stats", "KEY")
    assert st["registrations"] == 1 and st["pending"] == 1 and st["photos"] == 2


def test_relay_lifecycle_expiry_purges(relay):
    base = relay
    relay_client.publish_event(base, "temp", "Temp Event", "KEY")
    reg = relay_client._post(base, "/api/register", {
        "event": "temp", "name": "G",
        "selfie": "data:image/png;base64," + base64.b64encode(b"s").decode()})

    # set expiry in the past, then purge
    relay_client.set_lifecycle(base, "temp", "KEY", expires_at=1)
    assert relay_server.purge_expired_events() >= 1

    # event + guest data are gone: registering now fails, album is gone
    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        relay_client.event_stats(base, "temp", "KEY")   # event no longer exists
    with pytest.raises(urllib.error.HTTPError):
        relay_client._get(base, f"/api/album?a={reg['rid']}.{reg['atoken']}")


def test_relay_delete_event(relay):
    base = relay
    relay_client.publish_event(base, "del", "Delete Me", "KEY")
    reg = relay_client._post(base, "/api/register", {
        "event": "del", "name": "G",
        "selfie": "data:image/png;base64," + base64.b64encode(b"s").decode()})
    assert relay_client.delete_event(base, "del", "KEY")["ok"] is True
    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        relay_client._get(base, f"/api/album?a={reg['rid']}.{reg['atoken']}")

    # wrong key can't delete
    relay_client.publish_event(base, "keep", "Keep", "RIGHT")
    with pytest.raises(urllib.error.HTTPError):
        relay_client.delete_event(base, "keep", "WRONG")
