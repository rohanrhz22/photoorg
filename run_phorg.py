"""
Standalone entry point for the phorg desktop app.

When packaged with PyInstaller this becomes ``phorg.exe`` — double-click it and
the friendly web UI opens in the default browser. No Python install required.

Running from source works too:
    python run_phorg.py
"""
import sys

from phorg import server


def main():
    # Allow "phorg.exe scan --root ..." style CLI use as well, but default to
    # launching the app when double-clicked with no arguments.
    if len(sys.argv) > 1 and sys.argv[1] not in ("ui", "app"):
        from phorg.cli import main as cli_main
        cli_main()
    else:
        argv = sys.argv[2:] if len(sys.argv) > 1 else []
        no_browser = "--no-browser" in argv
        port = 8765
        if "--port" in argv:
            try:
                port = int(argv[argv.index("--port") + 1])
            except (ValueError, IndexError):
                pass
        try:
            server.serve(port=port, open_browser=not no_browser)
        except Exception as e:  # keep the console open so the user sees the error
            print(f"\n  Could not start phorg: {e}")
            input("\n  Press Enter to close...")


if __name__ == "__main__":
    main()
