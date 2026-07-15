"""Optional public tunnel so a guest link works over the internet (not just the
same Wi-Fi).

Uses Cloudflare's free "quick tunnel" (``*.trycloudflare.com``) — no account and
no config required.  The ``cloudflared`` helper is used if it's already on PATH;
otherwise a copy is downloaded once into ``~/.phorg/bin``.

The public URL is HTTPS, which also lets phone cameras work in the guest portal.
Everything stays opt-in: nothing is exposed until the host turns online sharing
on, and the tunnel is torn down when sharing stops or the app closes.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request

_TUNNEL = {"proc": None, "url": None, "host": None, "error": None,
           "starting": False}
_TLOCK = threading.Lock()

_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")

# Latest cloudflared release assets by platform.
_CF_ASSETS = {
    ("nt", "amd64"):
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        "cloudflared-windows-amd64.exe",
    ("nt", "386"):
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        "cloudflared-windows-386.exe",
}


def _bin_dir():
    d = os.path.join(os.path.expanduser("~"), ".phorg", "bin")
    os.makedirs(d, exist_ok=True)
    return d


def _download_url():
    if os.name != "nt":
        return None
    import platform
    arch = "386" if platform.machine().endswith("86") else "amd64"
    return _CF_ASSETS.get(("nt", arch)) or _CF_ASSETS.get(("nt", "amd64"))


def cloudflared_path():
    """Return a usable cloudflared path, or ``None`` if not present yet."""
    onpath = shutil.which("cloudflared")
    if onpath:
        return onpath
    local = os.path.join(_bin_dir(),
                         "cloudflared.exe" if os.name == "nt" else "cloudflared")
    return local if os.path.exists(local) else None


def ensure_cloudflared():
    """Return a cloudflared path, downloading it once if needed (Windows)."""
    p = cloudflared_path()
    if p:
        return p
    url = _download_url()
    if not url:
        raise RuntimeError(
            "cloudflared isn't installed. Install it from your package "
            "manager (e.g. 'brew install cloudflared') and try again.")
    local = os.path.join(_bin_dir(), "cloudflared.exe")
    tmp = local + ".part"
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(tmp, local)
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError("Couldn't download the tunnel helper: %s" % e)
    return local


def status():
    with _TLOCK:
        proc = _TUNNEL["proc"]
        return {
            "url": _TUNNEL["url"],
            "host": _TUNNEL["host"],
            "error": _TUNNEL["error"],
            "starting": _TUNNEL["starting"],
            "running": bool(proc and proc.poll() is None),
        }


def _reader(proc):
    try:
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            m = _URL_RE.search(line)
            if m:
                with _TLOCK:
                    if not _TUNNEL["url"]:
                        _TUNNEL["url"] = m.group(0)
                        _TUNNEL["host"] = m.group(0).split("://", 1)[1]
    except Exception:
        pass


def start(port, timeout=45):
    """Start a public tunnel to ``localhost:port``; return {url, host}."""
    with _TLOCK:
        if _TUNNEL["proc"] and _TUNNEL["proc"].poll() is None and _TUNNEL["url"]:
            return {"url": _TUNNEL["url"], "host": _TUNNEL["host"]}
        _TUNNEL.update({"url": None, "host": None, "error": None,
                        "starting": True})
    try:
        exe = ensure_cloudflared()
    except Exception as e:
        with _TLOCK:
            _TUNNEL["starting"] = False
            _TUNNEL["error"] = str(e)
        raise
    cmd = [exe, "tunnel", "--no-autoupdate", "--url",
           "http://localhost:%d" % port]
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
              "text": True, "bufsize": 1}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception as e:
        with _TLOCK:
            _TUNNEL["starting"] = False
            _TUNNEL["error"] = str(e)
        raise RuntimeError("Couldn't launch the tunnel: %s" % e)
    with _TLOCK:
        _TUNNEL["proc"] = proc
    threading.Thread(target=_reader, args=(proc,), daemon=True).start()

    deadline = time.time() + timeout
    while time.time() < deadline:
        with _TLOCK:
            url = _TUNNEL["url"]
            dead = proc.poll() is not None
        if url:
            with _TLOCK:
                _TUNNEL["starting"] = False
            return {"url": url, "host": _TUNNEL["host"]}
        if dead:
            break
        time.sleep(0.3)

    stop()
    with _TLOCK:
        _TUNNEL["error"] = "Timed out establishing the online link."
    raise RuntimeError("Couldn't establish the online link (timed out). "
                       "Check your internet connection and try again.")


def stop():
    with _TLOCK:
        proc = _TUNNEL["proc"]
        _TUNNEL.update({"proc": None, "url": None, "host": None,
                        "starting": False})
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        except Exception:
            pass
