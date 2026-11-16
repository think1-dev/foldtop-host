#!/usr/bin/env python3
"""A second, independent clipboard client for the tests: GTK's own implementation of
X selections, not Foldtop's. What it reads is what a real application would paste.

    clip_tool.py get [--primary]             print the selection's text (exit 3 if none)
    clip_tool.py set [--hold S] [--file F]   own the clipboard with stdin's (or F's) text for S seconds
    clip_tool.py set --image                 … with a small picture instead (a copy that has no text)
    clip_tool.py targets                     print the formats the clipboard's owner offers

The display comes from $DISPLAY.
"""

from __future__ import annotations

import argparse
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    get = sub.add_parser("get")
    get.add_argument("--primary", action="store_true")
    put = sub.add_parser("set")
    put.add_argument("--hold", type=float, default=3.0)
    put.add_argument("--file")
    put.add_argument("--image", action="store_true", help="copy a small picture instead of text")
    sub.add_parser("targets")
    args = parser.parse_args()

    if not Gtk.init_check()[0]:
        print("cannot open the display", file=sys.stderr)
        return 2

    if args.cmd == "get":
        selection = Gdk.SELECTION_PRIMARY if args.primary else Gdk.SELECTION_CLIPBOARD
        text = Gtk.Clipboard.get(selection).wait_for_text()
        if text is None:
            return 3
        sys.stdout.write(text)
        return 0

    if args.cmd == "targets":
        ok, atoms = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_targets()
        if not ok:
            return 3
        print("\n".join(sorted(atom.name() for atom in atoms)))
        return 0

    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = sys.stdin.read()
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    if args.image:
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        clipboard.set_image(GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 8, 8))
    else:
        clipboard.set_text(text, -1)
    print("owned", flush=True)
    GLib.timeout_add(int(args.hold * 1000), Gtk.main_quit)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
