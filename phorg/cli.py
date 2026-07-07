"""
phorg — command-line interface.

Usage:
    python -m phorg <command> [options]

Commands:
    ui         Launch the friendly web interface (recommended).
    scan       Inventory the tree and print a summary.
    junk       Move junk (temp/zero-byte/cache) into a review folder.
    organize   Sort files into folders by type or size.
    fix-ext    Repair broken/missing file extensions via magic bytes.
    empties    Remove empty folders (skips protected/hidden).
    report     Generate the interactive HTML storage report.
    verify     Count files (use before/after to prove nothing was lost).

Global options:
    --backend {local,adb}   Where to operate (default: local).
    --root PATH             Root folder (local path, or on-device path for adb;
                            adb defaults to /storage/emulated/0).
    --device SERIAL         ADB device serial (default: first connected).
    --adb PATH              Path to adb.exe (default: auto-detect).
    --apply                 Actually perform changes (default is dry-run).
    --yes                   Skip the confirmation prompt when applying.
    --recursive             Recurse into sub-folders (junk/organize/size).
    --include-hidden        Do NOT skip dot-folders (dangerous).
    --protect NAME          Extra top-level folder name to protect (repeatable).
"""
import argparse
import sys

from .backends import get_backend
from .safety import SafetyPolicy
from . import organizer, report


def _fmt_kb(kb):
    if kb >= 1048576:
        return f"{kb/1048576:.2f} GB"
    if kb >= 1024:
        return f"{kb/1024:.1f} MB"
    return f"{kb} KB"


def _make_safety(args):
    return SafetyPolicy(include_hidden=args.include_hidden,
                        extra_protect=args.protect or [])


def _preview_and_apply(backend, ops, args, label):
    if not ops:
        print(f"  Nothing to do for '{label}'.")
        return
    moves = sum(1 for o in ops if o.kind == "move")
    mkdirs = sum(1 for o in ops if o.kind == "mkdir")
    rmdirs = sum(1 for o in ops if o.kind == "rmdir")
    print(f"\n  Planned: {moves} moves, {mkdirs} new folders, {rmdirs} removals.")
    show = ops[:12]
    for o in show:
        print(f"    {o}")
    if len(ops) > len(show):
        print(f"    ... and {len(ops)-len(show)} more")

    if not args.apply:
        print("\n  DRY-RUN - no changes made. Re-run with --apply to execute.")
        return

    if not args.yes:
        ans = input(f"\n  Apply {moves+rmdirs} change(s)? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("  Aborted.")
            return

    before = organizer.count_files(backend, backend.root)
    done = backend.apply_ops(ops, log=print)
    after = organizer.count_files(backend, backend.root)
    print(f"  Applied {done} operation(s).")
    if label != "empties":
        if before == after:
            print(f"  [OK] Verified: file count unchanged ({after}). No data lost.")
        else:
            print(f"  [!] File count changed {before} -> {after} - please review!")


# --- command handlers ------------------------------------------------------
def cmd_scan(args):
    be = get_backend(args)
    root = be.root
    print(f"Scanning {be.name}:{root} (depth {args.depth}) ...")
    tree, meta = be.scan(root, max_depth=args.depth)
    tree = sorted(tree, key=lambda n: -n["kb"])
    print(f"\nUsed by files: {_fmt_kb(meta.get('rootKb',0))}   "
          f"Files: {meta.get('rootFiles',0):,}   "
          f"Loose at root: {meta.get('rootLoose',0)}")
    if meta.get("diskTotalKb"):
        print(f"Disk: {_fmt_kb(meta['diskUsedKb'])} used / "
              f"{_fmt_kb(meta['diskTotalKb'])}  ({_fmt_kb(meta['diskFreeKb'])} free)")
    print(f"\n{'Folder':<34}{'Size':>12}{'Files':>10}{'Subs':>7}")
    print("-" * 63)
    for n in tree:
        print(f"{n['name'][:33]:<34}{_fmt_kb(n['kb']):>12}{n['files']:>10}{n['subs']:>7}")


def cmd_junk(args):
    be = get_backend(args)
    ops, s = organizer.plan_junk(be, be.root, _make_safety(args),
                                 recursive=args.recursive)
    print(f"Junk sweep -> {s['folder']}/ : {s['total']} file(s) {dict(s['by_reason'])}")
    _preview_and_apply(be, ops, args, "junk")


def cmd_organize(args):
    be = get_backend(args)
    saf = _make_safety(args)
    if args.mode == "type":
        ops, s = organizer.plan_by_type(be, be.root, saf, recursive=args.recursive)
        print(f"Organise by type: {s['total']} file(s) {s['by_category']}")
    else:
        ops, s = organizer.plan_by_size(be, be.root, saf, recursive=args.recursive)
        print(f"Organise by size: {s['total']} file(s) {s['by_tier']}")
    _preview_and_apply(be, ops, args, "organize")


def cmd_fix_ext(args):
    be = get_backend(args)
    ops, s = organizer.plan_fix_extensions(be, be.root, _make_safety(args),
                                           recursive=args.recursive, sort=args.sort)
    print(f"Fix extensions: {s['total']} file(s) {s['by_type']}")
    _preview_and_apply(be, ops, args, "fix-ext")


def cmd_empties(args):
    be = get_backend(args)
    ops, s = organizer.plan_empty_folders(be, be.root, _make_safety(args))
    print(f"Empty folders found: {s['total']}")
    for d in s["folders"][:20]:
        print(f"    {d}")
    if s["total"] > 20:
        print(f"    ... and {s['total']-20} more")
    _preview_and_apply(be, ops, args, "empties")


def cmd_report(args):
    be = get_backend(args)
    print(f"Scanning {be.name}:{be.root} for report ...")
    tree, meta = be.scan(be.root, max_depth=args.depth)
    outs = args.out or ["phone_storage_report.html"]
    written, meta = report.build_report(tree, meta, outs, root_label=be.root)
    print(f"Report written ({meta['nL1']} top / {meta['nL2']} sub / {meta['nL3']} sub-sub):")
    for p in written:
        print(f"    {p}")


def cmd_verify(args):
    be = get_backend(args)
    n = organizer.count_files(be, be.root)
    print(f"Total files under {be.root}: {n:,}")


def cmd_ui(args):
    from . import server
    server.serve(host=args.host, port=args.port,
                 open_browser=not args.no_browser)


# --- parser ----------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="phorg",
                                description="Safe, rule-based storage organiser "
                                            "for Android (ADB) or local folders.")
    # connection / scope flags — given before the subcommand
    p.add_argument("--backend", choices=["local", "adb"], default="local")
    p.add_argument("--root", help="Root folder to operate on.")
    p.add_argument("--device", help="ADB device serial.")
    p.add_argument("--adb", help="Path to adb executable.")
    p.add_argument("--depth", type=int, default=3, help="Scan depth for scan/report.")

    # execution flags — shared by every subcommand, accepted after it
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--apply", action="store_true",
                        help="Perform changes (default: dry-run).")
    common.add_argument("--yes", action="store_true",
                        help="Skip confirmation prompt.")
    common.add_argument("--recursive", action="store_true",
                        help="Recurse into sub-folders.")
    common.add_argument("--include-hidden", action="store_true",
                        help="Do not skip dot-folders.")
    common.add_argument("--protect", action="append",
                        help="Extra protected top folder (repeatable).")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", parents=[common]).set_defaults(func=cmd_scan)
    sub.add_parser("junk", parents=[common]).set_defaults(func=cmd_junk)

    po = sub.add_parser("organize", parents=[common])
    po.add_argument("--mode", choices=["type", "size"], default="type")
    po.set_defaults(func=cmd_organize)

    pf = sub.add_parser("fix-ext", parents=[common])
    pf.add_argument("--sort", action="store_true",
                    help="Also move corrected files into type folders.")
    pf.set_defaults(func=cmd_fix_ext)

    sub.add_parser("empties", parents=[common]).set_defaults(func=cmd_empties)

    pr = sub.add_parser("report", parents=[common])
    pr.add_argument("--out", action="append", help="Output HTML path (repeatable).")
    pr.set_defaults(func=cmd_report)

    sub.add_parser("verify", parents=[common]).set_defaults(func=cmd_verify)

    pu = sub.add_parser("ui", help="Launch the friendly web interface.")
    pu.add_argument("--host", default="127.0.0.1", help="Bind address.")
    pu.add_argument("--port", type=int, default=8765, help="Port to serve on.")
    pu.add_argument("--no-browser", action="store_true",
                    help="Do not auto-open the browser.")
    pu.set_defaults(func=cmd_ui)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
