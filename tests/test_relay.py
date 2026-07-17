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
