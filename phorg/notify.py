"""Async delivery for FaceFind guests (Phase 2 of always-on availability).

When a guest registers a selfie and then closes their phone, we still want to
send them their album once the host has matched it.  This module sends that
"your photos are ready" message with the guest's private album link.

It is intentionally pluggable and optional:

* Email over SMTP is supported out of the box (standard-library ``smtplib``).
* If nothing is configured, :func:`send` simply reports ``not_configured`` and
  never raises — the rest of the app keeps working, and the guest still has the
  on-screen album link.

Configuration comes from environment variables or ``~/.phorg/notify.json``::

    {
      "smtp_host": "smtp.example.com",
      "smtp_port": 587,
      "smtp_user": "you@example.com",
      "smtp_pass": "app-password",
      "from_addr": "you@example.com",
      "use_tls": true
    }

Env equivalents: ``PHORG_SMTP_HOST``, ``PHORG_SMTP_PORT``, ``PHORG_SMTP_USER``,
``PHORG_SMTP_PASS``, ``PHORG_SMTP_FROM``, ``PHORG_SMTP_TLS``.
"""
from __future__ import annotations

import os
import json
import ssl
import smtplib
from email.message import EmailMessage


def _cfg():
    data = {}
    path = os.path.join(os.path.expanduser("~"), ".phorg", "notify.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        data = {}

    def g(key, env):
        return os.environ.get(env) or data.get(key)

    try:
        port = int(g("smtp_port", "PHORG_SMTP_PORT") or 587)
    except (TypeError, ValueError):
        port = 587
    user = g("smtp_user", "PHORG_SMTP_USER")
    return {
        "host": g("smtp_host", "PHORG_SMTP_HOST"),
        "port": port,
        "user": user,
        "password": g("smtp_pass", "PHORG_SMTP_PASS"),
        "from": g("from_addr", "PHORG_SMTP_FROM") or user,
        "tls": str(g("use_tls", "PHORG_SMTP_TLS") or "1").lower()
        not in ("0", "false", "no"),
    }


def _is_email(s):
    s = (s or "").strip()
    return "@" in s and "." in s.split("@")[-1]


def configured():
    """True when an email transport is set up (so we can actually deliver)."""
    c = _cfg()
    return bool(c["host"] and c["from"])


def send(contact, event, url, name=None):
    """Deliver the album *url* to *contact*.  Never raises; returns a small
    status dict so the caller can log the outcome."""
    if not contact or not url:
        return {"sent": False, "reason": "missing"}
    if not _is_email(contact):
        # Phone numbers (SMS / WhatsApp) are a future transport; skip for now.
        return {"sent": False, "reason": "not_email"}
    c = _cfg()
    if not (c["host"] and c["from"]):
        return {"sent": False, "reason": "not_configured"}

    ev = event or "the event"
    who = name or "there"
    msg = EmailMessage()
    msg["Subject"] = f"Your photos from {ev} are ready"
    msg["From"] = c["from"]
    msg["To"] = contact.strip()
    msg.set_content(
        f"Hi {who},\n\n"
        f"Good news — your photos from {ev} are ready.\n"
        f"Open your private album here:\n{url}\n\n"
        f"This link is just for you. Enjoy!\n")

    try:
        with smtplib.SMTP(c["host"], c["port"], timeout=20) as s:
            if c["tls"]:
                s.starttls(context=ssl.create_default_context())
            if c["user"]:
                s.login(c["user"], c["password"])
            s.send_message(msg)
        return {"sent": True}
    except Exception as e:  # pragma: no cover - network/SMTP dependent
        return {"sent": False, "reason": str(e)[:200]}
