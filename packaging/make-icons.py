#!/usr/bin/env python3
"""Render the app icon to PNGs for /usr/share/icons/hicolor.

The art lives in one place, blinky_gui.paint_icon, so the icon the window
carries and the icon the desktop shows can never drift apart. That is also
why there is no hand-written SVG here: a second copy of the drawing would be
a second thing to keep in step.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from PyQt6.QtWidgets import QApplication            # noqa: E402
import blinky_gui                                   # noqa: E402

SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "icons")
    app = QApplication([])                          # noqa: F841
    os.makedirs(out, exist_ok=True)
    for size in SIZES:
        d = os.path.join(out, "%dx%d" % (size, size))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "blinky.png")
        if not blinky_gui.icon_pixmap(size).save(path, "PNG"):
            print("failed to write %s" % path, file=sys.stderr)
            return 1
        print("  %-9s %s" % ("%dx%d" % (size, size), path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
