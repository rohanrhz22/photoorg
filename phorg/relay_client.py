"""Desktop-side client for the always-on relay (Phase 3).

The photographer's app uses this to talk to a running ``relay_server``:

* :func:`publish_event` — register/refresh the event on the relay and get the
  guest link.
* :func:`pull` — fetch queued guest sign-ups that still need matching.
* :func:`post_results` — send a guest's matched photos back to the relay.
* :func:`sync_once` — one full drain: pull pending sign-ups, match each with a
  caller-supplied matcher, and post the results.  The desktop server drives
  this on a timer so the heavy face matching stays on the PC while the relay
  stays always-on.

Standard-library only (``urllib``), so it adds nothing to install.
"""
from __future__ import annotations

import json
import base64
import urllib.request
import urllib.error

_TIMEOUT = 30


def _post(base, path, obj, key=None):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(base.rstrip("/") + path, data=data,
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-Event-Key", key)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read() or b"{}")


def _get(base, path, key=None):
    req = urllib.request.Request(base.rstrip("/") + path, method="GET")
    if key:
        req.add_header("X-Event-Key", key)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read() or b"{}")


def publish_event(base, event_id, name, key):
    """Create or refresh the event on the relay.  Returns {id, name, key}."""
    return _post(base, "/api/event",
                 {"id": event_id, "name": name, "key": key})


def guest_url(base, event_id):
    return base.rstrip("/") + "/?e=" + event_id


def pull(base, event_id, key):
    """Return the list of queued sign-ups awaiting a match (may be empty)."""
    from urllib.parse import quote
    d = _get(base, "/api/pull?event=" + quote(event_id), key=key)
    return d.get("registrations") or []


def index_status(base, event_id, key):
    """Return {count, pids} of photos already published to the relay (Tier B)."""
    from urllib.parse import quote
    return _get(base, "/api/index/status?event=" + quote(event_id), key=key)


def event_stats(base, event_id, key):
    """Return registration/match/photo counts for the host dashboard."""
    from urllib.parse import quote
    return _get(base, "/api/event/stats?event=" + quote(event_id), key=key)


def set_lifecycle(base, event_id, key, expires_at=0):
    """Set/clear the event's auto-purge time on the relay (0 = never)."""
    return _post(base, "/api/event/lifecycle",
                 {"event": event_id, "expires_at": int(expires_at or 0)}, key=key)


def delete_event(base, event_id, key):
    """Immediately delete an event and all its guest data from the relay."""
    return _post(base, "/api/event/delete", {"event": event_id}, key=key)


def publish_index(base, event_id, key, photos, chunk=20):
    """Publish face embeddings + deliverable images to the relay in batches.

    *photos* is a list of ``{pid, name, embeds:[[...]], image_bytes}``.
    """
    total = 0
    batch = []
    for ph in photos:
        img = ph.get("image_bytes")
        batch.append({
            "pid": ph.get("pid"), "name": ph.get("name"),
            "embeds": ph.get("embeds") or [],
            "image_b64": base64.b64encode(img).decode() if img else None,
        })
        if len(batch) >= chunk:
            total += _post(base, "/api/index",
                           {"event": event_id, "photos": batch},
                           key=key).get("count", 0)
            batch = []
    if batch:
        total += _post(base, "/api/index",
                       {"event": event_id, "photos": batch},
                       key=key).get("count", 0)
    return total


def post_results(base, event_id, key, rid, matches, status="matched",
                 count=None):
    """Send a guest's results back.  *matches* is a list of
    ``{"name", "score", "image_bytes"}`` — image bytes are the deliverable
    (medium) JPEG the guest downloads."""
    payload = []
    for m in matches:
        img = m.get("image_bytes")
        payload.append({
            "name": m.get("name"),
            "score": m.get("score"),
            "image_b64": base64.b64encode(img).decode() if img else None,
        })
    return _post(base, "/api/results",
                 {"event": event_id, "rid": rid, "status": status,
                  "count": count if count is not None else len(payload),
                  "matches": payload}, key=key)


def sync_once(base, event_id, key, matcher):
    """Pull every pending sign-up and process it with *matcher*.

    ``matcher(selfie_bytes, registration_dict) -> list[{name, score,
    image_bytes}]``.  Returns the number of registrations processed.  Errors on
    a single registration are reported to the relay and do not abort the batch.
    """
    processed = 0
    for reg in pull(base, event_id, key):
        rid = reg.get("rid")
        selfie_b64 = reg.get("selfie_b64") or ""
        try:
            selfie = base64.b64decode(selfie_b64) if selfie_b64 else b""
            matches = matcher(selfie, reg) or []
            post_results(base, event_id, key, rid, matches,
                         status="matched", count=len(matches))
        except Exception as e:  # pragma: no cover - defensive
            try:
                post_results(base, event_id, key, rid, [], status="error",
                             count=0)
            except Exception:
                pass
            _ = e
        processed += 1
    return processed
