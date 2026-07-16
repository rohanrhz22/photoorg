"""
FaceFind — command-line entry point.

FaceFind is a desktop app: running it launches the friendly web interface in
your browser.  Point it at an event folder, add a selfie, and it finds every
photo that person appears in.

Usage:
    python -m phorg [ui] [--port N] [--no-browser]
"""
import sys

from . import server


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # accept an optional leading "ui"/"app" verb for familiarity
    if argv and argv[0] in ("ui", "app"):
        argv = argv[1:]

    no_browser = "--no-browser" in argv
    port = 8765
    if "--port" in argv:
        try:
            port = int(argv[argv.index("--port") + 1])
        except (ValueError, IndexError):
            pass
    server.serve(port=port, open_browser=not no_browser)


if __name__ == "__main__":
    main()
