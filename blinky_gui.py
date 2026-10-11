#!/usr/bin/env python3
"""blinky-gui - a front end for the SiPix StyleCam Blink II.

The look is a deliberate hybrid of the two desktops of 2001: Windows XP's
Luna theme and Mac OS X's Aqua. Luna supplies the blue gradient title bar,
the Tahoma typography and the Control Panel group boxes; Aqua supplies the
pinstripe background, the traffic lights, the gel buttons and the barber-pole
progress bar. Everything is painted rather than themed, so it looks the same
on any desktop.

All camera work is done by the blinky module on a worker thread; this file
contains no protocol code.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import time

from PyQt6.QtCore import (QPoint, QPointF, QRect, QRectF, QSize, Qt, QThread,
                          QTimer, pyqtSignal)
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontDatabase, QIcon, QLinearGradient,
                         QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
                         QRadialGradient)
from PyQt6.QtWidgets import (QApplication, QDialog, QFileDialog, QFrame,
                             QGridLayout, QHBoxLayout, QLabel,
                             QScrollArea, QSizePolicy,
                             QVBoxLayout, QWidget)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blinky                                                    # noqa: E402

__version__ = blinky.__version__

# ---------------------------------------------------------------------------
# Palette
#
# Luna's title bar is a multi-stop vertical gradient with a bright band near
# the top and a dark lip at the bottom; Aqua's pinstripes are a 1px line every
# 4px over a very faintly blue white. Both are reproduced here by hand.
# ---------------------------------------------------------------------------

LUNA_STOPS = [                      # active title bar, top to bottom
    (0.00, "#3C84E8"), (0.03, "#6FB2FB"), (0.09, "#3E8FF2"),
    (0.40, "#1A66DC"), (0.72, "#0B4FC6"), (0.88, "#0846B4"),
    (0.95, "#1257C4"), (1.00, "#2E74D8"),
]
LUNA_INACTIVE = [
    (0.00, "#8CA9CC"), (0.10, "#A8BFD8"), (0.50, "#8FAACB"), (1.00, "#7E99BA"),
]

AQUA_BASE = QColor("#C3D5EA")       # pinstripe ground
AQUA_STRIPE = QColor("#E6EFF9")     # pinstripe line
PANEL_WHITE = QColor(255, 255, 255, 216)   # Aqua panels are translucent
PANEL_BORDER = QColor("#8FA8C8")
HEADER_TOP = QColor(224, 238, 253, 235)
HEADER_BOT = QColor(172, 206, 240, 235)
HEADER_TEXT = QColor("#12447E")
INK = QColor("#20303F")
INK_SOFT = QColor("#5B6C7D")

GEL_BLUE = ["#8CC8FF", "#54A6F2", "#1462C8", "#0E4E9E"]
GEL_GREY = ["#FFFFFF", "#F6F9FC", "#C3D0DF", "#8CA0B8"]
GEL_CANDY = ["#FFF0C8", "#FFD066", "#E8981A", "#B87A12"]

LIGHT_RED = ("#FF6257", "#D8392C", "#FFB4AC")
LIGHT_AMBER = ("#FFBD2E", "#D79A18", "#FFE0A0")
LIGHT_GREEN = ("#28CA42", "#17A02F", "#A8EDB4")

OK_GREEN = QColor("#2E9E42")
BAD_RED = QColor("#C33327")


def ui_font(size=8, bold=False, family=None):
    """Tahoma is XP's shell font; it is present here, so use the real thing."""
    for name in ([family] if family else []) + ["Tahoma", "Verdana", "DejaVu Sans"]:
        if name and name in QFontDatabase.families():
            f = QFont(name, size)
            f.setBold(bold)
            return f
    f = QFont()
    f.setPointSize(size)
    f.setBold(bold)
    return f


def title_font(size=9):
    return ui_font(size, bold=True, family="Trebuchet MS")


def vgrad(rect, stops):
    g = QLinearGradient(QPointF(rect.topLeft()), QPointF(rect.bottomLeft()))
    for pos, col in stops:
        g.setColorAt(pos, QColor(col))
    return g


def paint_pinstripes(painter, rect):
    """Aqua's background: a faint white line every fourth row."""
    painter.fillRect(rect, AQUA_BASE)
    pen = QPen(AQUA_STRIPE)
    pen.setWidth(1)
    painter.setPen(pen)
    y = rect.top() - (rect.top() % 4)
    while y < rect.bottom() + 4:
        painter.drawLine(rect.left(), y, rect.right(), y)
        y += 4


def paint_gel(painter, rect, colors, radius=None, pressed=False, enabled=True,
              gloss=1.0):
    """An Aqua gel pill.

    The tell is the gloss: a near-white lobe filling the top half that stops
    at a crisp edge on the midline, with the body below picking up a light
    bounce off the bottom rim. Drawn in four passes: body, gloss, inner ring,
    outer rim.
    """
    if rect.height() <= 0 or rect.width() <= 0:
        return
    r = radius if radius is not None else rect.height() / 2.0
    body = QRectF(rect)
    top, upper, lower, rim = [QColor(c) for c in colors]
    if pressed:
        top, upper, lower = upper.darker(114), lower.darker(110), lower.darker(120)
    if not enabled:
        top = top.lighter(112)
        upper = QColor(upper.lighter(126))
        lower = QColor(lower.lighter(124))
        rim = rim.lighter(126)

    path = QPainterPath()
    path.addRoundedRect(body, r, r)

    g = QLinearGradient(body.topLeft(), body.bottomLeft())
    g.setColorAt(0.00, upper.lighter(112))
    g.setColorAt(0.49, upper)
    g.setColorAt(0.50, lower)
    g.setColorAt(0.88, lower.lighter(108))
    g.setColorAt(1.00, lower.lighter(132))
    painter.fillPath(path, QBrush(g))

    # Gloss lobe: stops hard at the midline.
    gloss_rect = QRectF(body.left() + 1.0, body.top() + 1.0,
                        body.width() - 2.0, body.height() * 0.48)
    if gloss_rect.height() > 1.5:
        painter.save()
        painter.setClipPath(path)
        k = gloss * (1.0 if enabled else 0.78)
        gg = QLinearGradient(gloss_rect.topLeft(), gloss_rect.bottomLeft())
        gg.setColorAt(0.0, QColor(255, 255, 255, int(250 * k)))
        gg.setColorAt(0.72, QColor(255, 255, 255, int(190 * k)))
        gg.setColorAt(1.0, QColor(255, 255, 255, int(105 * k)))
        gp = QPainterPath()
        gp.addRoundedRect(gloss_rect, r * 0.92, gloss_rect.height() / 2.0)
        painter.fillPath(gp, QBrush(gg))
        painter.restore()

    # Inner ring: bright along the top, invisible along the bottom.
    painter.save()
    painter.setClipPath(path)
    inner = QPainterPath()
    inner.addRoundedRect(body.adjusted(1, 1, -1, -1), r, r)
    ring = QLinearGradient(body.topLeft(), body.bottomLeft())
    ring.setColorAt(0.0, QColor(255, 255, 255, 230))
    ring.setColorAt(0.55, QColor(255, 255, 255, 40))
    ring.setColorAt(1.0, QColor(255, 255, 255, 150))
    painter.setPen(QPen(QBrush(ring), 1.2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(inner)
    painter.restore()

    painter.setPen(QPen(rim, 1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)


def paint_sparkle(painter, cx, cy, size, colour=QColor(255, 255, 255, 220)):
    """A four-point star. Pure 2001."""
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(colour)
    s, w = size, size * 0.22
    painter.drawPolygon(QPolygonF([
        QPointF(cx, cy - s), QPointF(cx + w, cy - w), QPointF(cx + s, cy),
        QPointF(cx + w, cy + w), QPointF(cx, cy + s), QPointF(cx - w, cy + w),
        QPointF(cx - s, cy), QPointF(cx - w, cy - w)]))
    painter.restore()


class GelButton(QWidget):
    """Aqua lozenge. The default button glows, the way Aqua's used to pulse."""

    clicked = pyqtSignal()

    def __init__(self, text, kind="grey", parent=None, width=None):
        super().__init__(parent)
        self.text = text
        self.kind = kind
        self._down = False
        self._hover = False
        self._enabled = True
        self._phase = 0.0
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(ui_font(8, bold=(kind == "blue")))
        w = width or (self.fontMetrics().horizontalAdvance(text) + 38)
        self.setFixedWidth(max(84, w))

    def sizeHint(self):
        return QSize(self.width(), 28)
        if kind == "blue":
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._pulse)
            self._timer.start(45)

    def _pulse(self):
        self._phase = (self._phase + 0.045) % 1.0
        self.update()

    def setEnabled(self, on):
        self._enabled = on
        super().setEnabled(on)
        self.setCursor(Qt.CursorShape.PointingHandCursor if on
                       else Qt.CursorShape.ArrowCursor)
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        if self._enabled and e.button() == Qt.MouseButton.LeftButton:
            self._down = True
            self.update()

    def mouseReleaseEvent(self, e):
        if self._down:
            self._down = False
            self.update()
            if self.rect().contains(e.position().toPoint()) and self._enabled:
                self.clicked.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        colors = {"blue": GEL_BLUE, "grey": GEL_GREY, "candy": GEL_CANDY}[self.kind]
        if self.kind == "blue" and self._enabled:
            import math
            k = 0.5 + 0.5 * math.sin(self._phase * 2 * math.pi)
            lift = int(4 + 13 * k)
            colors = [QColor(c).lighter(100 + lift).name() for c in colors]
        elif self._hover and self._enabled:
            colors = [QColor(c).lighter(106).name() for c in colors]
        # A weak sheen on blue keeps the white label readable; grey buttons
        # have dark text, so they can take the full Aqua gloss.
        paint_gel(p, rect, colors, pressed=self._down, enabled=self._enabled,
                  gloss=0.42 if self.kind == "blue" else 1.0)

        p.setFont(self.font())
        if self._enabled:
            if self.kind == "blue":
                p.setPen(QColor(12, 54, 110, 90))
                p.drawText(self.rect().adjusted(0, 1, 0, 1),
                           Qt.AlignmentFlag.AlignCenter, self.text)
                p.setPen(QColor("#FFFFFF"))
            else:
                p.setPen(QColor(255, 255, 255, 190))
                p.drawText(self.rect().adjusted(0, 1, 0, 1),
                           Qt.AlignmentFlag.AlignCenter, self.text)
                p.setPen(INK)
        else:
            p.setPen(QColor("#A7B3C0"))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text)


class AquaCheck(QWidget):
    """A gel checkbox with its label, in the same idiom as the buttons."""

    toggled = pyqtSignal(bool)

    def __init__(self, text, checked=False, parent=None, tint=None):
        super().__init__(parent)
        self.text = text
        self.checked = checked
        self.tint = tint
        self._hover = False
        self.setFont(ui_font(8))
        self.setFixedHeight(18)
        self.setMinimumWidth(self.fontMetrics().horizontalAdvance(text) + 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def isChecked(self):
        return self.checked

    def setChecked(self, on):
        if on != self.checked:
            self.checked = on
            self.update()
            self.toggled.emit(on)

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, e):
        if self.rect().contains(e.position().toPoint()):
            self.setChecked(not self.checked)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(0.5, 2.5, 13, 13)
        colors = GEL_GREY
        if self.checked:
            colors = GEL_CANDY if self.tint == "warn" else GEL_BLUE
        paint_gel(p, box, colors, radius=3.5,
                  gloss=0.45 if self.checked else 1.0)
        if self.checked:
            p.setPen(QPen(QColor("#FFFFFF"), 2.0,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPolyline(QPolygonF([QPointF(3.6, 9.2), QPointF(6.2, 11.8),
                                      QPointF(10.6, 5.6)]))
        p.setFont(self.font())
        p.setPen(QColor("#9A6A00") if self.tint == "warn" and self.checked
                 else INK if self._hover or self.checked else INK_SOFT)
        p.drawText(QRectF(19, 0, self.width() - 19, self.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self.text)


class TrafficLight(QWidget):
    """One early-Aqua pill: rim, radial body, specular dot."""

    clicked = pyqtSignal()

    def __init__(self, colors, parent=None):
        super().__init__(parent)
        self.colors = colors
        self._hover = False
        self._down = False
        self.setFixedSize(14, 14)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        # Qt's default mousePressEvent *ignores* the event, which propagates it
        # to the title bar; the release then goes to whoever accepted the
        # press, so without this the light never sees its own click.
        if e.button() == Qt.MouseButton.LeftButton:
            self._down = True
            self.update()
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        was_down, self._down = self._down, False
        self.update()
        if was_down and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        e.accept()

    def paintEvent(self, _):
        face, rim, hi = [QColor(c) for c in self.colors]
        if self._hover:
            face = face.lighter(112)
        if self._down:
            face = face.darker(118)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(0.5, 0.5, 13, 13)
        g = QRadialGradient(QPointF(6.0, 4.0), 12.0)
        g.setColorAt(0.0, hi)
        g.setColorAt(0.45, face)
        g.setColorAt(1.0, rim)
        p.setBrush(QBrush(g))
        p.setPen(QPen(rim.darker(118), 1))
        p.drawEllipse(body)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 205))
        p.drawEllipse(QRectF(3.2, 2.0, 7.0, 4.2))


class ResizeGrip(QWidget):
    """Aqua's ribbed corner grip.

    A frameless window has no border for the compositor to grab, and on
    Wayland a client cannot resize itself, so this hands the job to
    startSystemResize -- which is also how the real thing worked.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(15, 15)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemResize(Qt.Edge.BottomEdge | Qt.Edge.RightEdge)
            e.accept()
        else:
            super().mousePressEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for i, inset in enumerate((3, 7, 11)):
            p.setPen(QPen(QColor(255, 255, 255, 220), 1.6))
            p.drawLine(QPointF(self.width() - 1.0, inset + 0.0),
                       QPointF(inset + 0.0, self.height() - 1.0))
            p.setPen(QPen(QColor("#7C93AE"), 1.2))
            p.drawLine(QPointF(self.width() - 1.6, inset + 0.6),
                       QPointF(inset + 0.6, self.height() - 1.6))


class TitleBar(QWidget):
    """Luna's gradient and centred bold caption, with Aqua's lights at left."""

    close_clicked = pyqtSignal()
    minimise_clicked = pyqtSignal()
    zoom_clicked = pyqtSignal()

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.text = text
        self.active = True
        self.setFixedHeight(30)
        self._drag = None

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 0, 10, 0)
        row.setSpacing(7)
        self.close_light = TrafficLight(LIGHT_RED, self)
        self.min_light = TrafficLight(LIGHT_AMBER, self)
        self.zoom_light = TrafficLight(LIGHT_GREEN, self)
        self.close_light.clicked.connect(self.close_clicked)
        self.min_light.clicked.connect(self.minimise_clicked)
        self.zoom_light.clicked.connect(self.zoom_clicked)
        for w in (self.close_light, self.min_light, self.zoom_light):
            row.addWidget(w)
        row.addStretch(1)
        # A spacer the width of the lights keeps the caption truly centred.
        spacer = QWidget(self)
        spacer.setFixedWidth(3 * 14 + 2 * 7)
        row.addWidget(spacer)

    def set_active(self, on):
        self.active = on
        self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        # Wayland does not let a client position its own window, so move()
        # silently does nothing there. startSystemMove() asks the compositor
        # to do it instead, and works on X11 too; fall back to moving by hand
        # only if the platform refuses.
        handle = self.window().windowHandle()
        if handle is not None and handle.startSystemMove():
            self._drag = None
            return
        self._drag = e.globalPosition().toPoint() - \
            self.window().frameGeometry().topLeft()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.zoom_clicked.emit()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        path = QPainterPath()
        path.addRoundedRect(QRectF(r).adjusted(0, 0, 0, 8), 7, 7)
        p.setClipRect(r)
        p.fillPath(path, QBrush(vgrad(r, LUNA_STOPS if self.active
                                      else LUNA_INACTIVE)))
        p.setPen(QPen(QColor(255, 255, 255, 120), 1))
        p.drawLine(r.left() + 7, r.top() + 1, r.right() - 7, r.top() + 1)
        p.setPen(QPen(QColor(0, 0, 0, 55), 1))
        p.drawLine(r.left(), r.bottom(), r.right(), r.bottom())

        p.setFont(title_font(10))
        if self.active:
            tw = p.fontMetrics().horizontalAdvance(self.text)
            mid = r.center().x()
            paint_sparkle(p, mid - tw / 2 - 13, r.center().y() - 3, 4.0,
                          QColor(255, 255, 255, 210))
            paint_sparkle(p, mid + tw / 2 + 13, r.center().y() + 2, 3.0,
                          QColor(255, 255, 255, 165))
        shadow = QColor(0, 24, 64, 150 if self.active else 70)
        p.setPen(shadow)
        p.drawText(r.adjusted(0, 2, 0, 2), Qt.AlignmentFlag.AlignCenter, self.text)
        p.setPen(QColor("#FFFFFF") if self.active else QColor("#E4ECF6"))
        p.drawText(r.adjusted(0, 1, 0, 1), Qt.AlignmentFlag.AlignCenter, self.text)


class LunaGroup(QFrame):
    """An XP Control Panel panel: captioned header, hairline border, white body."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.title = title
        self.setObjectName("lunaGroup")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 23, 1, 1)
        outer.setSpacing(0)
        self.body = QWidget(self)
        self.body.setAutoFillBackground(False)
        outer.addWidget(self.body)
        self.inner = QVBoxLayout(self.body)
        self.inner.setContentsMargins(11, 9, 11, 10)
        self.inner.setSpacing(6)

    def addWidget(self, w):
        self.inner.addWidget(w)

    def addLayout(self, lay):
        self.inner.addLayout(lay)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 8, 8)
        p.fillPath(path, QBrush(PANEL_WHITE))

        head = QRectF(r.left(), r.top(), r.width(), 22)
        hp = QPainterPath()
        hp.addRoundedRect(head.adjusted(0, 0, 0, 8), 8, 8)
        p.save()
        p.setClipRect(head)
        g = QLinearGradient(head.topLeft(), head.bottomLeft())
        g.setColorAt(0.0, HEADER_TOP)
        g.setColorAt(1.0, HEADER_BOT)
        p.fillPath(hp, QBrush(g))
        p.restore()

        p.setPen(QPen(QColor("#B9CEE8"), 1))
        p.drawLine(QPointF(r.left() + 1, head.bottom()),
                   QPointF(r.right() - 1, head.bottom()))
        p.save()
        p.setClipPath(path)
        p.setPen(QPen(QColor(255, 255, 255, 215), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        inner = QPainterPath()
        inner.addRoundedRect(r.adjusted(1, 1, -1, -1), 7, 7)
        p.drawPath(inner)
        p.restore()
        p.setPen(QPen(PANEL_BORDER, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        p.setFont(ui_font(8, bold=True))
        p.setPen(QColor(255, 255, 255, 190))
        p.drawText(QRectF(head).adjusted(11, 1, 0, 1),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self.title)
        p.setPen(HEADER_TEXT)
        p.drawText(QRectF(head).adjusted(11, 0, 0, 0),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self.title)


class StatusLed(QWidget):
    """A little gel bead. Grey when unknown, green when the camera answers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = "unknown"
        self.setFixedSize(15, 15)

    def set_state(self, state):
        self.state = state
        self.update()

    def paintEvent(self, _):
        colors = {
            "ok": ("#48E066", "#149C2C", "#D8FFDE"),
            "bad": ("#FF6A5E", "#B92718", "#FFD6D0"),
            "busy": ("#FFD24D", "#C98B00", "#FFF2C8"),
            "unknown": ("#C6CFD9", "#8C98A6", "#F0F4F8"),
        }[self.state]
        face, rim, hi = [QColor(c) for c in colors]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = QRadialGradient(QPointF(6.2, 4.4), 13.0)
        g.setColorAt(0.0, hi)
        g.setColorAt(0.5, face)
        g.setColorAt(1.0, rim)
        p.setBrush(QBrush(g))
        p.setPen(QPen(rim.darker(115), 1))
        p.drawEllipse(QRectF(0.5, 0.5, 14, 14))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 200))
        p.drawEllipse(QRectF(3.6, 2.2, 7.4, 4.4))
        if self.state == "ok":
            paint_sparkle(p, 12.4, 2.6, 3.0, QColor(255, 255, 255, 235))


class BarberPole(QWidget):
    """Aqua's determinate/indeterminate bar: blue gel with marching stripes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(16)
        self._offset = 0
        self._value = 0.0
        self._running = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def start(self):
        self._running = True
        self._timer.start(40)
        self.update()

    def stop(self):
        self._running = False
        self._timer.stop()
        self.update()

    def set_value(self, frac):
        self._value = max(0.0, min(1.0, frac))
        self.update()

    def _tick(self):
        self._offset = (self._offset + 1) % 16
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        trough = QPainterPath()
        trough.addRoundedRect(r, r.height() / 2, r.height() / 2)
        tg = QLinearGradient(r.topLeft(), r.bottomLeft())
        tg.setColorAt(0.0, QColor("#D4DCE6"))
        tg.setColorAt(0.5, QColor("#EFF3F8"))
        tg.setColorAt(1.0, QColor("#FDFEFF"))
        p.fillPath(trough, QBrush(tg))

        width = r.width() * self._value if not self._running else r.width()
        if width > 2:
            fill = QRectF(r.left(), r.top(), width, r.height())
            fp = QPainterPath()
            fp.addRoundedRect(fill, fill.height() / 2, fill.height() / 2)
            p.save()
            p.setClipPath(fp)
            fg = QLinearGradient(fill.topLeft(), fill.bottomLeft())
            fg.setColorAt(0.0, QColor("#9FD0FF"))
            fg.setColorAt(0.48, QColor("#3E8FE0"))
            fg.setColorAt(0.52, QColor("#2A79CF"))
            fg.setColorAt(1.0, QColor("#5AA0E6"))
            p.fillRect(fill, QBrush(fg))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 62))
            step = 16
            x = fill.left() - 32 + self._offset
            while x < fill.right() + 32:
                poly = QPolygonF([QPointF(x, fill.bottom()),
                                  QPointF(x + step / 2, fill.bottom()),
                                  QPointF(x + step / 2 + 9, fill.top()),
                                  QPointF(x + 9, fill.top())])
                p.drawPolygon(poly)
                x += step
            gloss = QRectF(fill.adjusted(0, 1, 0, -fill.height() * 0.55))
            gg = QLinearGradient(gloss.topLeft(), gloss.bottomLeft())
            gg.setColorAt(0.0, QColor(255, 255, 255, 190))
            gg.setColorAt(1.0, QColor(255, 255, 255, 20))
            p.fillRect(gloss, QBrush(gg))
            p.restore()

        p.setPen(QPen(QColor("#8FA8C8"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(trough)


class CameraGlyph(QWidget):
    """A small painted camera. Chunky and glossy, the way 2001 liked things."""

    def __init__(self, size=34, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self.width()
        k = s / 34.0
        body = QRectF(2 * k, 9 * k, 30 * k, 21 * k)
        path = QPainterPath()
        path.addRoundedRect(body, 5 * k, 5 * k)
        hump = QPainterPath()
        hump.addRoundedRect(QRectF(8 * k, 5 * k, 12 * k, 7 * k), 2.5 * k, 2.5 * k)
        path = path.united(hump)
        g = QLinearGradient(body.topLeft(), body.bottomLeft())
        g.setColorAt(0.0, QColor("#7FB4E8"))
        g.setColorAt(0.5, QColor("#3E7FC6"))
        g.setColorAt(0.52, QColor("#2E6AB0"))
        g.setColorAt(1.0, QColor("#5C97D8"))
        p.fillPath(path, QBrush(g))
        p.setPen(QPen(QColor("#1D4C82"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        lens = QRectF(11 * k, 14 * k, 12 * k, 12 * k)
        lg = QRadialGradient(QPointF(lens.center().x() - 2 * k,
                                     lens.center().y() - 3 * k), 13 * k)
        lg.setColorAt(0.0, QColor("#E8F6FF"))
        lg.setColorAt(0.45, QColor("#3A7FC0"))
        lg.setColorAt(1.0, QColor("#12325C"))
        p.setBrush(QBrush(lg))
        p.setPen(QPen(QColor("#12325C"), 1))
        p.drawEllipse(lens)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 210))
        p.drawEllipse(QRectF(13.5 * k, 15.5 * k, 4.5 * k, 3 * k))
        # flash bead
        p.setBrush(QColor("#FFD86A"))
        p.setPen(QPen(QColor("#C79A16"), 1))
        p.drawEllipse(QRectF(25 * k, 12.5 * k, 4.5 * k, 4.5 * k))


def paint_icon(p, size):
    """Draw the Blinky mark: a chunky camera in translucent candy plastic.

    Detail is dropped below 32px on purpose -- at icon sizes the silhouette
    and the lens are all that survive, and keeping the rest turns to mush.
    """
    k = size / 128.0
    detailed = size >= 32
    tiny = size < 24          # below this, rings and outlines cost more
    p.setRenderHint(QPainter.RenderHint.Antialiasing)  # pixels than they earn

    # Small icons are not the big one with bits removed: the body fills more
    # of the canvas and the lens takes a bigger share, or nothing reads.
    if detailed:
        body = QRectF(9 * k, 34 * k, 110 * k, 80 * k)
        radius = 18 * k
    else:
        body = QRectF(5 * k, 26 * k, 118 * k, 92 * k)
        radius = 22 * k
    shell = QPainterPath()
    shell.addRoundedRect(body, radius, radius)
    if detailed:
        hump = QPainterPath()
        hump.addRoundedRect(QRectF(34 * k, 18 * k, 44 * k, 26 * k),
                            9 * k, 9 * k)
        shell = shell.united(hump)

    g = QLinearGradient(QPointF(0, body.top()), QPointF(0, body.bottom()))
    g.setColorAt(0.00, QColor("#7FD0F5"))
    g.setColorAt(0.44, QColor("#3E9BD8"))
    g.setColorAt(0.50, QColor("#2477BE"))
    g.setColorAt(0.86, QColor("#2E86CC"))
    g.setColorAt(1.00, QColor("#6FC2EC"))
    p.fillPath(shell, QBrush(g))

    if detailed:
        # gloss sweep across the upper body
        p.save()
        p.setClipPath(shell)
        gloss = QRectF(body.left(), body.top() - 22 * k,
                       body.width(), 58 * k)
        gg = QLinearGradient(gloss.topLeft(), gloss.bottomLeft())
        gg.setColorAt(0.0, QColor(255, 255, 255, 215))
        gg.setColorAt(1.0, QColor(255, 255, 255, 25))
        gp = QPainterPath()
        gp.addRoundedRect(gloss, 26 * k, 26 * k)
        p.fillPath(gp, QBrush(gg))
        p.restore()

    if not tiny:
        p.setPen(QPen(QColor("#12507F"), max(1.0, 2.0 * k)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(shell)

    # Lens: the one thing that must read at every size.
    if detailed:
        lens = QRectF(38 * k, 50 * k, 52 * k, 52 * k)
    else:
        d = (78 if tiny else 66) * k
        lens = QRectF(body.center().x() - d / 2, body.center().y() - d / 2, d, d)
    if tiny:
        # One circle, no ring: the silhouette plus a lens is all that survives.
        glass = lens
    else:
        p.setPen(QPen(QColor("#0E3E66"), max(1.0, 2.2 * k)))
        ring = QLinearGradient(lens.topLeft(), lens.bottomLeft())
        ring.setColorAt(0.0, QColor("#D8E8F4"))
        ring.setColorAt(1.0, QColor("#6E8CA8"))
        p.setBrush(QBrush(ring))
        p.drawEllipse(lens)
        glass = lens.adjusted(*([6 * k, 6 * k, -6 * k, -6 * k] if detailed
                                else [8 * k, 8 * k, -8 * k, -8 * k]))
    lg = QRadialGradient(QPointF(glass.center().x() - 7 * k,
                                 glass.center().y() - 9 * k), 46 * k)
    lg.setColorAt(0.00, QColor("#EAF8FF"))
    lg.setColorAt(0.32, QColor("#3E92D4"))
    lg.setColorAt(0.75, QColor("#123E6E"))
    lg.setColorAt(1.00, QColor("#08203C"))
    p.setBrush(QBrush(lg))
    p.setPen(QPen(QColor("#07203A"), max(1.0, 1.4 * k))
             if not tiny else Qt.PenStyle.NoPen)
    p.drawEllipse(glass)

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 225))
    p.drawEllipse(QRectF(glass.left() + glass.width() * 0.18,
                         glass.top() + glass.height() * 0.13,
                         glass.width() * 0.40, glass.height() * 0.26))
    if detailed:
        p.setBrush(QColor(255, 255, 255, 120))
        p.drawEllipse(QRectF(glass.right() - 14 * k, glass.bottom() - 15 * k,
                             8 * k, 6 * k))
        # Flash bead, and a sparkle off it, because it is 2001.
        flash = QRectF(96 * k, 44 * k, 16 * k, 16 * k)
        fg = QRadialGradient(QPointF(flash.center().x() - 2 * k,
                                     flash.center().y() - 3 * k), 15 * k)
        fg.setColorAt(0.0, QColor("#FFF6D8"))
        fg.setColorAt(0.55, QColor("#FFC93C"))
        fg.setColorAt(1.0, QColor("#C98908"))
        p.setBrush(QBrush(fg))
        p.setPen(QPen(QColor("#9A6A06"), max(1.0, 1.4 * k)))
        p.drawEllipse(flash)
        paint_sparkle(p, 110 * k, 30 * k, 11 * k, QColor(255, 255, 255, 235))
    return shell


def icon_pixmap(size):
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    paint_icon(p, size)
    p.end()
    return pm


def app_icon():
    """A QIcon carrying every size, so window managers pick a sharp one."""
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(icon_pixmap(size))
    return icon


class PhotoRow(QWidget):
    """One directory entry, with its thumbnail once it has been saved."""

    def __init__(self, entry, outdir, parent=None, saved_path=None):
        super().__init__(parent)
        self.entry = entry
        self.outdir = outdir
        self.state = "pending"
        # The file this photo actually lives in, once we know it. Never
        # guessed from the camera's index, which is reused after an erase.
        self.saved_path = saved_path
        self.setFixedHeight(42)

    def set_state(self, state, saved_path=None):
        self.state = state
        if saved_path:
            self.saved_path = saved_path
        self.update()

    @staticmethod
    def _first_frame(path):
        """Pull frame one out of an MJPEG AVI, for the row's thumbnail."""
        try:
            with open(path, "rb") as fh:
                head = fh.read(1 << 20)
        except OSError:
            return None
        start = head.find(b"\xff\xd8")
        if start < 0:
            return None
        end = head.find(b"\xff\xd9", start + 2)
        pm = QPixmap()
        pm.loadFromData(head[start:end + 2] if end > 0 else head[start:], "JPEG")
        return pm if not pm.isNull() else None

    def _thumb(self):
        if not self.saved_path:
            return None
        base = os.path.splitext(os.path.basename(self.saved_path))[0]
        here = os.path.dirname(self.saved_path)
        # The picture sits beside the raw, or one level up when raws are
        # kept in a raw/ subfolder.
        for folder in (here, os.path.dirname(here)):
            for ext in (".png", ".jpg", ".avi"):
                path = os.path.join(folder, base + ext)
                if not os.path.exists(path):
                    continue
                if ext == ".avi":
                    pm = self._first_frame(path)
                else:
                    pm = QPixmap(path)
                if pm is not None and not pm.isNull():
                        return pm.scaled(
                            44, 33, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        return None

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -1.5)
        path = QPainterPath()
        path.addRoundedRect(r, 5, 5)
        tint = {"pending": QColor("#FFFFFF"), "busy": QColor("#FFF8E2"),
                "done": QColor("#F2FBF3"), "failed": QColor("#FDF1EF"),
                "partial": QColor("#FFFAEC")}[self.state]
        p.fillPath(path, QBrush(tint))
        p.setPen(QPen(QColor("#D5DFEB"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        thumb = self._thumb()
        frame = QRectF(6, 5, 44, 31)
        if thumb is not None:
            p.setPen(QPen(QColor("#9BAEC4"), 1))
            p.setBrush(QColor("#FFFFFF"))
            p.drawRect(frame)
            x = frame.left() + (frame.width() - thumb.width()) / 2
            y = frame.top() + (frame.height() - thumb.height()) / 2
            p.drawPixmap(QPoint(int(x), int(y)), thumb)
        else:
            p.setPen(QPen(QColor("#C3D0DF"), 1, Qt.PenStyle.DashLine))
            p.setBrush(QColor("#F6F9FC"))
            p.drawRect(frame)

        p.setFont(ui_font(8, bold=True))
        p.setPen(INK)
        p.drawText(QRectF(58, 5, 150, 16),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self.entry.basename)
        p.setFont(ui_font(8))
        p.setPen(INK_SOFT)
        kind = "movie clip" if self.entry.is_movie else "still"
        p.drawText(QRectF(58, 20, 200, 15),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   "%s · %s KB" % (kind, "{:,}".format(self.entry.data_bytes // 1024)))

        badge = {"done": ("saved", OK_GREEN), "failed": ("failed", BAD_RED),
                 "busy": ("reading…", QColor("#B4801A")),
                 "partial": ("partial", QColor("#B4801A")),
                 "pending": ("", INK_SOFT)}[self.state]
        if badge[0]:
            p.setFont(ui_font(8, bold=True))
            p.setPen(badge[1])
            p.drawText(QRectF(r.right() - 86, 5, 80, 32),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       badge[0])


class Job(QThread):
    """Runs one blinky operation off the UI thread."""

    line = pyqtSignal(str, str)          # level, text
    step = pyqtSignal(float, str)        # fraction, caption
    item = pyqtSignal(int, str)          # image index, state
    ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.exc = None             # what went wrong, for the caller to judge
        self.cancel = False         # checked between photos by long jobs

    def run(self):
        log = GuiLog(self.line.emit)
        try:
            self.ok.emit(self.fn(self, log))
        except blinky.CameraError as exc:
            self.exc = exc
            self.failed.emit(str(exc))
        except Exception as exc:                       # never kill the UI
            self.exc = exc
            self.failed.emit("%s: %s" % (type(exc).__name__, exc))


class GuiLog(blinky.Log):
    """blinky's logger, rerouted into the Activity panel."""

    def __init__(self, sink):
        super().__init__(verbose=False, quiet=True)
        self.sink = sink

    def _emit(self, msg, stream=None):
        self.sink("info", str(msg))

    def out(self, msg=""):
        self._record("OUT", msg)
        if str(msg).strip():
            self.sink("out", str(msg))

    def warn(self, msg):
        self._record("WARN", msg)
        self.sink("warn", str(msg))

    def error(self, msg):
        self._record("ERROR", msg)
        self.sink("error", str(msg))


class ElidedLabel(QLabel):
    """A label that shortens with an ellipsis instead of being cut off.

    Paths elide in the middle, so both the root and the folder stay legible;
    prose elides at the end. The full text stays available as a tooltip.
    """

    def __init__(self, text="", mode=Qt.TextElideMode.ElideMiddle, parent=None):
        super().__init__(parent)
        self._full = text
        self._mode = mode
        self.setMinimumWidth(40)
        super().setText(text)

    def setText(self, text):
        self._full = text
        self._apply()

    def fullText(self):
        return self._full

    def _apply(self):
        avail = max(10, self.width() - 4)
        shown = self.fontMetrics().elidedText(self._full, self._mode, avail)
        super().setText(shown)
        self.setToolTip(self._full if shown != self._full else "")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply()


class ActivityLog(QScrollArea):
    """The Activity panel. Sunken, mono-ish, with coloured levels."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inner = QLabel()
        self.inner.setWordWrap(True)
        self.inner.setTextFormat(Qt.TextFormat.RichText)
        self.inner.setAlignment(Qt.AlignmentFlag.AlignTop |
                                Qt.AlignmentFlag.AlignLeft)
        self.inner.setFont(ui_font(8))
        self.inner.setContentsMargins(7, 5, 7, 5)
        self.setWidget(self.inner)
        self.setStyleSheet(
            "QScrollArea { background: #FFFFFF; border: 1px solid #A8BACF;"
            " border-radius: 4px; }"
            "QLabel { background: #FFFFFF; }"
            "QScrollBar:vertical { background: #EDF1F7; width: 12px;"
            " border: none; margin: 0; }"
            "QScrollBar::handle:vertical { background: #8FB6DE; min-height: 22px;"
            " border-radius: 6px; border: 1px solid #5E8CBD; }"
            "QScrollBar::add-line, QScrollBar::sub-line { height: 0; }")
        self.lines = []

    def append(self, level, text):
        colour = {"error": "#B4281B", "warn": "#9A6A00",
                  "out": "#20303F", "info": "#5B6C7D"}.get(level, "#5B6C7D")
        weight = "bold" if level in ("error", "warn") else "normal"
        safe = (text.replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;").replace("  ", "&nbsp;&nbsp;"))
        self.lines.append(
            '<div style="color:%s;font-weight:%s;margin:0 0 2px 0;">%s</div>'
            % (colour, weight, safe))
        del self.lines[:-400]
        self.inner.setText("".join(self.lines))
        bar = self.verticalScrollBar()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    def clear(self):
        self.lines = []
        self.inner.setText("")


WATCH_UNIT = "blinky-watch.service"


def _systemctl(*args, timeout=6):
    """Run systemctl --user. Returns (rc, output)."""
    import subprocess
    if shutil.which("systemctl") is None:
        return 1, "systemctl is not available"
    try:
        p = subprocess.run(["systemctl", "--user"] + list(args),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def watch_service_state():
    """('missing'|'inactive'|'active', enabled) for the auto-download unit."""
    rc, out = _systemctl("list-unit-files", WATCH_UNIT, "--no-legend")
    if rc != 0 or WATCH_UNIT not in out:
        return "missing", False
    enabled = _systemctl("is-enabled", WATCH_UNIT)[1] == "enabled"
    active = _systemctl("is-active", WATCH_UNIT)[1] == "active"
    return ("active" if active else "inactive"), enabled


class ConfirmDialog(QDialog):
    """A modal in the window's own idiom.

    Sized by its layout rather than pinned: a frameless window on Wayland is
    told its scale factor only after the first layout, so a fixed width
    cannot grow to fit content that gets bigger when that scale arrives.
    """

    def __init__(self, title, message, confirm_text, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        bar = TitleBar(title, self)
        bar.close_clicked.connect(self.reject)
        bar.minimise_clicked.connect(self.reject)
        box.addWidget(bar)

        panel = QWidget(self)
        box.addWidget(panel, 1)
        inner = QVBoxLayout(panel)
        inner.setContentsMargins(18, 15, 18, 15)
        inner.setSpacing(14)

        self.msg = QLabel(message, panel)
        self.msg.setFont(ui_font(9))
        self.msg.setWordWrap(True)
        self.msg.setStyleSheet("color:#20303F;")
        self.msg.setSizePolicy(QSizePolicy.Policy.Preferred,
                               QSizePolicy.Policy.MinimumExpanding)
        # Enough width to wrap sensibly, without pinning the dialog.
        self.msg.setMinimumWidth(360)
        inner.addWidget(self.msg, 1)

        row = QHBoxLayout()
        row.setSpacing(9)
        row.addStretch(1)
        cancel = GelButton("Cancel", "grey", panel)
        confirm = GelButton(confirm_text, "candy", panel,
                            width=max(150, len(confirm_text) * 8 + 40))
        cancel.clicked.connect(self.reject)
        confirm.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(confirm)
        inner.addLayout(row)

        self.setMinimumWidth(400)
        self.adjustSize()

    def ask(self):
        """Show it, centred on the parent, and report the answer."""
        self.adjustSize()
        parent = self.parentWidget()
        if parent is not None:
            centre = parent.frameGeometry().center()
            self.move(centre.x() - self.width() // 2,
                      centre.y() - self.height() // 2)
        return self.exec() == QDialog.DialogCode.Accepted

    def showEvent(self, e):
        # The scale factor can arrive after construction, so measure again
        # once the window is actually on screen.
        super().showEvent(e)
        self.adjustSize()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 7, 7)
        p.save()
        p.setClipPath(path)
        paint_pinstripes(p, self.rect())
        p.restore()
        p.setPen(QPen(QColor("#4C6A8E"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


# A replug is the real retry during recovery, so do not spend minutes on
# in-place retries against a link that has already died. Small reads and a
# long timeout are what got photos through when the link was at its worst.
RECOVERY_TUNING = dict(timeout=10000, chunk=1024, retries=2)


def is_link_trouble(exc):
    """Is this the link misbehaving, as opposed to something else wrong?

    A camera that is absent, already claimed, or not ours to open needs
    plugging in, the other program closing, or the udev rule -- replugging
    over and over will not help. Anything else from the USB layer is the
    link, which is what recovery is for.
    """
    return (isinstance(exc, blinky.CameraError)
            and not isinstance(exc, (blinky.PermissionDenied,
                                     blinky.DeviceBusy, blinky.NotPresent)))


def already_saved(cam, entry, index, sizes):
    """The local file that holds this photo, or None.

    Decided by content: the camera renumbers from zero after an erase, so
    image0000 on the camera and image0000 in the folder are routinely
    different photos. The prefix read is only paid for when some local file
    is the right size to be this photo in the first place.
    """
    if not index or entry.data_bytes not in sizes:
        return None
    try:
        return index.get(cam.fingerprint(entry))
    except blinky.CameraError:
        return None


def save_photo(data, entry, stem, outdir, dirs, still_formats, video_formats,
               quality, log):
    """Write the raw bytes, then the picture or clip made from them.

    Returns (state, raw_path, problem), where state is "done", "partial" or
    "failed". The raw file lands first, so even a failure here leaves the
    camera's exact bytes on disk.
    """
    # free_path is a belt-and-braces guard: the counter should already be
    # past anything on disk, but overwriting a photo is not worth risking.
    raw = blinky.free_path(os.path.join(dirs["raw"], stem + ".raw"))
    stem = os.path.splitext(os.path.basename(raw))[0]
    blinky.write_file_atomically(raw, data)

    # A clip of one frame is a photograph, so decide from the bytes rather
    # than the directory flag alone.
    is_clip = (entry.is_movie
               and (blinky.movie_extension(data)[1]
                    or len(blinky.split_clip_frames(data)) > 1))
    if is_clip:
        try:
            n, w, h, made = blinky.build_clip(data, dirs, stem, log,
                                              video_formats)
            log.out("%s: %d frame%s at %dx%d -> %s"
                    % (entry.basename, n, "" if n == 1 else "s", w, h,
                       ", ".join(os.path.basename(m) for m in made)))
            return "done", raw, None
        except blinky.CameraError as exc:
            log.error("%s: could not build a video: %s" % (entry.basename, exc))
            log.info("the camera's bytes are kept at %s" % raw)
            return "failed", raw, str(exc)
    try:
        w, h, raster, part = blinky.decode_still(data, log)
        blinky.write_still(outdir, stem, w, h, raster, still_formats, quality)
        return ("partial" if part else "done"), raw, None
    except blinky.CameraError as exc:
        log.error("%s: decode failed: %s" % (entry.basename, exc))
        log.info("the raw data is kept at %s" % raw)
        return "failed", raw, "decode: %s" % exc


def extract_round(job, log, opts):
    """One plug's worth of recovery: save what is missing until the link dies.

    Stops at the first photo that will not come through rather than trying
    the rest, because on this camera the rest fail the same way, slowly.
    Whatever made it is on disk, and the next round skips it by content.
    """
    outdir = opts["outdir"]
    dirs = blinky.output_dirs(outdir)
    os.makedirs(dirs["raw"], exist_ok=True)
    result = {"total": None, "had": 0, "gained": 0, "error": None,
              "stopped_at": None, "damaged": [], "cancelled": False}
    # A transfer that died partway can leave bytes queued on the endpoint.
    blinky._step_drain(log)
    cam = blinky.Blink2(log, **RECOVERY_TUNING)
    try:
        cam.open()
        n = cam.get_numpics()
        result["total"] = n
        entries = cam.get_directory(n)[0] if n else []
        index = blinky.local_fingerprints(outdir)
        sizes = {sz for sz, _ in index}
        missing = []
        for e in entries:
            if already_saved(cam, e, index, sizes):
                result["had"] += 1
            else:
                missing.append(e)
        counter = blinky.next_free_index(outdir)
        for k, e in enumerate(missing):
            if job.cancel:
                result["cancelled"] = True
                break
            job.step.emit((result["had"] + result["gained"]) / float(n),
                          "Reading %s — %d to go" % (e.basename,
                                                           len(missing) - k))
            try:
                data = cam.read_image_with_retries(e)
            except blinky.CameraError as exc:
                result["error"] = str(exc)
                result["stopped_at"] = e.basename
                break
            state, _, _ = save_photo(data, e, "image%04d" % counter, outdir,
                                     dirs, opts["still_formats"],
                                     opts["video_formats"], opts["quality"],
                                     log)
            counter += 1
            # The raw bytes are safe even when the picture is not, and the
            # next round must not fetch this photo again.
            result["gained"] += 1
            if state != "done":
                result["damaged"].append(e.basename)
    except blinky.CameraError as exc:
        result["error"] = str(exc)
    finally:
        cam.close()
    return result


def _where_text(path, depth):
    if path is None:
        return "Not plugged in"
    if depth == 0:
        return "At %s, plugged straight into the computer" % path
    return "At %s, %d hub%s in the way" % (path, depth,
                                           "" if depth == 1 else "s")


def _first_line(text, limit=110):
    text = str(text).strip()
    line = text.splitlines()[0] if text else ""
    return line if len(line) <= limit else line[:limit - 1] + "…"


def ports_html(ports):
    """The ranked free ports as a short list a person can act on."""
    if not ports:
        return ("Nothing is free closer to the computer. Unplug something "
                "else from a port on the computer itself and use that.")
    lines = []
    for n, h in enumerate(ports):
        nums = ", ".join(str(p) for p in h["free"])
        plural = "s" if len(h["free"]) > 1 else ""
        if h["depth"] == 0:
            place = "Bus %s, port%s %s on the computer itself" % (
                h["bus"], plural, nums)
        else:
            place = "Hub %s, port%s %s" % (h["name"], plural, nums)
        lines.append("%s<b>%s</b> &nbsp;<span style='color:#5B6C7D'>%s</span>"
                     % ("★ " if n == 0 else "• ", place,
                        blinky.port_summary(h)))
    return "Free ports, best first:<br>" + "<br>".join(lines)


class RoundRow(QWidget):
    """One line of the recovery history: a bead and what that plug got."""

    def __init__(self, number, text, good, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(8)
        led = StatusLed(self)
        led.set_state("ok" if good else "bad")
        lay.addWidget(led)
        tag = QLabel("Round %d" % number, self)
        tag.setFont(ui_font(8, bold=True))
        tag.setStyleSheet("color:#20303F;")
        tag.setFixedWidth(62)
        lay.addWidget(tag)
        msg = ElidedLabel(text, Qt.TextElideMode.ElideRight, self)
        msg.setFont(ui_font(8))
        msg.setStyleSheet("color:#3A4B5C;")
        lay.addWidget(msg, 1)


def release_highlight(body, limit=150):
    """The first change in a release's notes, as one plain sentence.

    The notes are the package changelog stanza, whose first line is a
    version header nobody needs to read; the first bullet says what changed.
    """
    lines, grabbing = [], False
    for raw in (body or "").splitlines():
        text = raw.strip()
        if text.startswith(("* ", "- ")):
            if grabbing:
                break
            grabbing = True
            text = text[2:]
        elif not text or text.startswith("--"):
            if grabbing:
                break
            continue
        if grabbing:
            lines.append(text)
    out = " ".join(lines)
    return out if len(out) <= limit else out[:limit - 1].rstrip() + "\u2026"


class Balloon(QFrame):
    """An XP notification balloon: a tinted strip, a glyph, one button.

    It stays hidden until there is something worth saying, and says it in
    the window rather than in a popup that has to be dismissed.
    """

    clicked = pyqtSignal()

    TINTS = {
        "warn": ("\u26a0", "#C98B00", "#5B4A1A", ("#FFFFF0", "#FFF4C4"),
                 "#D9B54A"),
        "info": ("\u2605", "#1462C8", "#203A5C", ("#F6FBFF", "#DCEBFB"),
                 "#8FB6DE"),
    }

    def __init__(self, tint, button, body="", kind="candy", parent=None):
        super().__init__(parent)
        glyph_text, glyph_ink, body_ink, self.fill, self.rim = self.TINTS[tint]
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 9, 8)
        lay.setSpacing(10)
        glyph = QLabel(glyph_text, self)
        glyph.setFont(ui_font(13, bold=True))
        glyph.setStyleSheet("color:%s; background:transparent;" % glyph_ink)
        lay.addWidget(glyph)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.head = QLabel("", self)
        self.head.setFont(ui_font(8, bold=True))
        self.head.setStyleSheet("color:#20303F; background:transparent;")
        self.head.setWordWrap(True)
        col.addWidget(self.head)
        self.body = QLabel(body, self)
        self.body.setFont(ui_font(8))
        self.body.setStyleSheet("color:%s; background:transparent;" % body_ink)
        self.body.setWordWrap(True)
        col.addWidget(self.body)
        lay.addLayout(col, 1)
        self.button = GelButton(button, kind, self, width=96)
        self.button.clicked.connect(self.clicked)
        lay.addWidget(self.button)
        self.hide()

    def set_button(self, text, enabled=True):
        self.button.text = text
        self.button.setEnabled(enabled)
        self.button.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 7, 7)
        p.fillPath(path, QBrush(vgrad(r, [(0.0, self.fill[0]),
                                          (1.0, self.fill[1])])))
        p.setPen(QPen(QColor(self.rim), 1))
        p.drawPath(path)


class RecoveryDialog(QDialog):
    """Get the photos off a camera whose link keeps dying.

    This is the routine that worked by hand: download until the link drops,
    unplug, plug back in, carry on from where it stopped, and repeat until
    everything is saved. The dialog watches the bus, so the only thing the
    person does is the unplugging and plugging; each round starts by itself.
    When a round gets nothing, it suggests the next remedy -- the kernel
    settings, a better port, another cable -- but never the batteries, which
    would erase the very photos this is trying to save.
    """

    POLL_MS = 700
    SETTLE_MS = 3000            # the camera is unhappy if spoken to at once
    HISTORY = 4

    def __init__(self, opts, sink=None, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        self.opts = opts
        self.sink = sink
        self.round = 0
        self.barren = 0
        self.total = None
        self.saved = 0
        self.phase = "start"
        self.outcome = None
        self.job = None
        self.tip = None
        self._tips_shown = set()
        self._closing = False

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        bar = TitleBar("Camera Recovery", self)
        bar.close_clicked.connect(self.reject)
        bar.minimise_clicked.connect(self.reject)
        bar.zoom_clicked.connect(lambda: None)
        box.addWidget(bar)

        panel = QWidget(self)
        box.addWidget(panel, 1)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(15, 13, 15, 13)
        lay.setSpacing(10)

        # -- the score -------------------------------------------------------
        head = QHBoxLayout()
        head.setSpacing(11)
        head.addWidget(CameraGlyph(30, panel))
        col = QVBoxLayout()
        col.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(7)
        self.led = StatusLed(panel)
        top.addWidget(self.led)
        self.state = QLabel("Getting ready…", panel)
        self.state.setFont(ui_font(9, bold=True))
        self.state.setStyleSheet("color:#20303F;")
        top.addWidget(self.state, 1)
        col.addLayout(top)
        self.detail = ElidedLabel("", Qt.TextElideMode.ElideRight, panel)
        self.detail.setFont(ui_font(8))
        self.detail.setStyleSheet("color:#5B6C7D;")
        col.addWidget(self.detail)
        self.where = ElidedLabel("", Qt.TextElideMode.ElideRight, panel)
        self.where.setFont(ui_font(8))
        self.where.setStyleSheet("color:#5B6C7D;")
        col.addWidget(self.where)
        head.addLayout(col, 1)
        lay.addLayout(head)
        self.meter = BarberPole(panel)
        lay.addWidget(self.meter)

        # -- what to do now --------------------------------------------------
        self.card = LunaGroup("Round 1", panel)
        self.todo = QLabel("", panel)
        self.todo.setFont(ui_font(9, bold=True))
        self.todo.setStyleSheet("color:#12447E;")
        self.todo.setWordWrap(True)
        self.todo.setMinimumWidth(410)
        self.card.addWidget(self.todo)
        prog = QHBoxLayout()
        prog.setSpacing(7)
        self.pled = StatusLed(panel)
        prog.addWidget(self.pled)
        self.progress = QLabel("", panel)
        self.progress.setFont(ui_font(8))
        self.progress.setStyleSheet("color:#20303F;")
        self.progress.setWordWrap(True)
        prog.addWidget(self.progress, 1)
        self.card.addLayout(prog)
        lay.addWidget(self.card)

        # -- a remedy, when plugging alone is getting nowhere ----------------
        self.tipcard = LunaGroup("Nothing came through — try this", panel)
        self.tiptitle = QLabel("", panel)
        self.tiptitle.setFont(ui_font(9, bold=True))
        self.tiptitle.setStyleSheet("color:#20303F;")
        self.tiptitle.setWordWrap(True)
        self.tipcard.addWidget(self.tiptitle)
        self.tipwhy = QLabel("", panel)
        self.tipwhy.setFont(ui_font(8))
        self.tipwhy.setStyleSheet("color:#3A4B5C;")
        self.tipwhy.setWordWrap(True)
        self.tipcard.addWidget(self.tipwhy)
        self.tipports = QLabel("", panel)
        self.tipports.setFont(ui_font(8))
        self.tipports.setTextFormat(Qt.TextFormat.RichText)
        self.tipports.setWordWrap(True)
        self.tipports.setStyleSheet(
            "color:#20303F; background:#F3F7FC; border:1px solid #C9D6E5;"
            " border-radius:4px; padding:5px 8px;")
        self.tipcard.addWidget(self.tipports)
        tiprow = QHBoxLayout()
        tiprow.addStretch(1)
        self.btn_tip = GelButton("Set Them…", "candy", panel, width=120)
        self.btn_tip.clicked.connect(self._set_quirks)
        tiprow.addWidget(self.btn_tip)
        self.tipcard.addLayout(tiprow)
        self.tipcard.hide()
        lay.addWidget(self.tipcard)

        # -- how each plug went ----------------------------------------------
        self.history = LunaGroup("Rounds", panel)
        self.history_box = QVBoxLayout()
        self.history_box.setSpacing(3)
        self.history.addLayout(self.history_box)
        self.history.hide()
        lay.addWidget(self.history)

        foot = QHBoxLayout()
        foot.addStretch(1)
        self.btn_close = GelButton("Stop", "grey", panel)
        self.btn_close.clicked.connect(self.reject)
        foot.addWidget(self.btn_close)
        self.btn_done = GelButton("Done", "blue", panel)
        self.btn_done.clicked.connect(self.accept)
        self.btn_done.hide()
        foot.addWidget(self.btn_done)
        lay.addLayout(foot)

        self._poll = QTimer(self)
        self._poll.timeout.connect(self._tick)
        self.setMinimumWidth(470)
        self.adjustSize()

    # -- running it --------------------------------------------------------

    def run(self):
        """Show it centred on the parent; returns 'done', 'partial' or None."""
        self.adjustSize()
        parent = self.parentWidget()
        if parent is not None:
            centre = parent.frameGeometry().center()
            self.move(centre.x() - self.width() // 2,
                      centre.y() - self.height() // 2)
        QTimer.singleShot(0, self._begin)
        self.exec()
        return self.outcome

    def showEvent(self, e):
        super().showEvent(e)
        self.adjustSize()

    def reject(self):
        # Never tear down a worker mid-photo: let it finish the one it is
        # reading, so nothing is left half-written, then close.
        self._closing = True
        self._poll.stop()
        if self.job is not None and self.job.isRunning():
            self.job.cancel = True
            self.progress.setText("Finishing the photo in progress, then "
                                  "stopping…")
            return
        super().reject()

    def _begin(self):
        self._show_where()
        if blinky.camera_present():
            # Worth one try straight away: a fresh open is sometimes enough.
            self._start_round()
        else:
            self._await("Plug the camera in.", start="plug")

    def _show_where(self):
        path, depth = blinky.camera_path()
        self.where.setText(_where_text(path, depth))
        return path, depth

    def _score(self):
        if self.total:
            self.state.setText("%d of %d photo%s saved"
                               % (self.saved, self.total,
                                  "" if self.total == 1 else "s"))
            self.meter.set_value(self.saved / float(self.total))

    # -- a round -------------------------------------------------------------

    def _start_round(self):
        if self._closing:
            return
        self.round += 1
        self.phase = "round"
        self.card.title = "Round %d" % self.round
        self.card.update()
        self.todo.setText("Leave it plugged in.")
        self.pled.set_state("busy")
        self.progress.setText("Talking to the camera…")
        self.led.set_state("busy")
        if not self.total:
            self.state.setText("Reading the camera…")
        job = Job(lambda job, log: extract_round(job, log, self.opts), self)
        job.cancel = False
        if self.sink is not None:
            job.line.connect(self.sink)
        job.step.connect(self._on_step)
        job.ok.connect(self._round_done)
        job.failed.connect(self._round_failed)
        job.finished.connect(self._job_finished)
        self.job = job
        job.start()

    def _on_step(self, frac, caption):
        self.meter.set_value(frac)
        if caption:
            self.progress.setText(caption)

    def _job_finished(self):
        if self.sender() is not self.job:
            return
        if self._closing:
            super().reject()

    def _round_failed(self, message):
        # extract_round reports camera trouble in its result; an exception
        # here is something else entirely (a full disk, say). Stop, loudly.
        self.led.set_state("bad")
        self.state.setText("Recovery stopped")
        self.detail.setText(_first_line(message))
        self.pled.set_state("bad")
        self.progress.setText(message)
        if self.sink is not None:
            self.sink("error", message)
        self.btn_close.text = "Close"
        self.btn_close.update()

    def _round_done(self, r):
        self._show_where()
        if r["total"] is not None:
            self.total = r["total"]
            self.saved = r["had"] + r["gained"]
        self._score()

        if r["total"] == 0:
            self._finish("The camera is empty", "There is nothing on it to "
                         "recover.")
            return
        if r["cancelled"]:
            return
        if r["total"] is not None and self.saved >= r["total"]:
            self._log_round("%s saved — that was the last of them"
                            % r["gained"] if r["gained"] else
                            "everything was already saved", True)
            self._finish("All %d photo%s are saved"
                         % (self.total, "" if self.total == 1 else "s"),
                         "Everything on the camera is now in %s." %
                         self._pretty(self.opts["outdir"]))
            return

        if r["gained"]:
            self.barren = 0
            what = "%d saved" % r["gained"]
            if r["stopped_at"]:
                what += ", then the link dropped at %s" % r["stopped_at"]
            self._log_round(what, True)
            self.detail.setText("Round %d: %s" % (self.round, what))
            self._hide_tip()
        else:
            self.barren += 1
            why = _first_line(r["error"] or "nothing new", 80)
            self._log_round("nothing new — %s" % why, False)
            self.detail.setText("Round %d got nothing: %s" % (self.round, why))
            self._show_next_tip()
        self.led.set_state("bad")
        self._await(self.tip["do"] if self.tip else blinky.REPLUG_PLAIN)

    def _log_round(self, text, good):
        self.history_box.insertWidget(0, RoundRow(self.round, text, good,
                                                  self.history))
        while self.history_box.count() > self.HISTORY:
            item = self.history_box.takeAt(self.history_box.count() - 1)
            if item.widget():
                item.widget().deleteLater()
        self.history.show()
        self.adjustSize()

    # -- waiting for a replug -------------------------------------------------

    def _await(self, do, start=None):
        if self._closing:
            return
        self.todo.setText(do)
        here = blinky.camera_present()
        self.phase = start or ("unplug" if here else "plug")
        self._say_phase()
        self._poll.start(self.POLL_MS)
        self.adjustSize()

    def _say_phase(self):
        self.pled.set_state("busy")
        self.progress.setText({
            "unplug": "Waiting for you to unplug it…",
            "plug": "Unplugged. Plug it back in when you’re ready.",
            "settle": "Plugged in. Giving it a moment to settle…",
        }[self.phase])

    def _tick(self):
        here = blinky.camera_present()
        self._show_where()
        if self.phase == "unplug" and not here:
            self.phase = "plug"
            self._say_phase()
            if self.round:
                self.todo.setText("Plug it back in." if not self.tip
                                  else self.tip["do"])
        elif self.phase == "plug" and here:
            self._poll.stop()
            self.phase = "settle"
            self._say_phase()
            QTimer.singleShot(self.SETTLE_MS, self._settled)

    def _settled(self):
        if self.phase == "settle" and not self._closing:
            self._start_round()

    # -- remedies -------------------------------------------------------------

    def _show_next_tip(self):
        path, depth = blinky.camera_path()
        tip = None
        for st in blinky._recovery_ladder(None, path, depth):
            key = st["title"]
            if key in self._tips_shown or st.get("destructive"):
                continue
            # Draining happens every round, and a plain power-cycle is what
            # every round already is.
            if st["kind"] == "drain" or (st["kind"] == "replug"
                                         and "ports" not in st
                                         and "cable" not in key.lower()):
                continue
            tip = st
            break
        if tip is None:
            self.tip = None
            self.tipcard.title = "Still nothing"
            self.tiptitle.setText("What is left is hardware.")
            self.tipwhy.setText(
                "A different computer, or the camera’s USB side has "
                "failed. The photos saved so far are safe. You can keep "
                "replugging; sometimes it still comes good.")
            self.tipports.hide()
            self.btn_tip.hide()
        else:
            self._tips_shown.add(tip["title"])
            self.tip = tip
            self.tipcard.title = "Nothing came through — try this"
            self.tiptitle.setText(tip["title"].replace(" (needs sudo)", ""))
            self.tipwhy.setText(tip.get("reason") or tip["why"])
            if "ports" in tip:
                self.tipports.setText(ports_html(tip["ports"]))
                self.tipports.show()
            else:
                self.tipports.hide()
            self.btn_tip.setVisible(tip["kind"] == "quirks")
            self.btn_tip.setEnabled(True)
        self.tipcard.update()
        self.tipcard.show()
        self.adjustSize()

    def _hide_tip(self):
        self.tip = None
        self.tipcard.hide()
        self.adjustSize()

    def _set_quirks(self):
        if not shutil.which("pkexec"):
            self.tipwhy.setText(
                "Run these in a terminal, then replug:\n"
                "  echo '%s' | sudo tee %s\n  echo 1 | sudo tee %s"
                % (blinky.quirks_value(), blinky.QUIRKS_PARAM,
                   blinky.OLD_SCHEME_PARAM))
            self.btn_tip.hide()
            return
        import subprocess
        self.btn_tip.setEnabled(False)
        try:
            # pkexec puts up the desktop's own password prompt and blocks
            # until it is answered, so keep the dialog painting meanwhile.
            p = subprocess.Popen(["pkexec", "sh", "-c", blinky.quirks_script()],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
        except OSError as exc:
            self.tipwhy.setText("Could not ask for your password: %s" % exc)
            self.btn_tip.setEnabled(True)
            return
        self._pk = p
        self._pk_timer = QTimer(self)
        self._pk_timer.timeout.connect(self._quirks_poll)
        self._pk_timer.start(200)

    def _quirks_poll(self):
        rc = self._pk.poll()
        if rc is None:
            return
        self._pk_timer.stop()
        if rc == 0 and blinky.quirks_applied():
            self.tipwhy.setText("Set. They take effect when it is next "
                                "plugged in, and last until you reboot.")
            self.btn_tip.hide()
            if self.sink is not None:
                self.sink("out", "Kernel settings applied for the camera; "
                                 "they last until the next reboot.")
            return
        self.btn_tip.setEnabled(True)
        if rc in (126, 127):
            self.tipwhy.setText("Not set: the password prompt was cancelled. "
                                "Press Set Them to try again.")
        else:
            out = self._pk.stdout.read().decode("utf-8", "replace")
            self.tipwhy.setText("Not set: %s" % _first_line(out or "rc %d"
                                                            % rc))

    # -- the end ---------------------------------------------------------------

    def _finish(self, headline, body):
        self._poll.stop()
        self.phase = "done"
        self.outcome = "done"
        self.led.set_state("ok")
        self.state.setText(headline)
        self.detail.setText("Done in one go" if self.round <= 1 else
                            "Took %d rounds of replugging" % self.round)
        self.meter.set_value(1.0)
        self.card.title = "Done"
        self.card.update()
        self.todo.setText(body)
        self.pled.set_state("ok")
        self.progress.setText("You can unplug the camera whenever you like.")
        self._hide_tip()
        self.btn_close.hide()
        self.btn_done.show()
        self.adjustSize()

    def done(self, code):
        if self.outcome is None and self.saved:
            self.outcome = "partial"
        self._poll.stop()
        super().done(code)

    def _pretty(self, path):
        home = os.path.expanduser("~")
        return path.replace(home, "~", 1) if path.startswith(home) else path

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 7, 7)
        p.save()
        p.setClipPath(path)
        paint_pinstripes(p, self.rect())
        p.restore()
        p.setPen(QPen(QColor("#4C6A8E"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


class BlinkyWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Blinky")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Fit the screen we actually have: this window is tall, and a
        # laptop panel is often shorter than its natural height.
        self.setMinimumSize(560, 520)
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        height = min(880, avail.height() - 60) if avail else 880
        width = min(600, avail.width() - 40) if avail else 600
        self.resize(max(560, width), max(520, height))

        self.outdir = blinky.DEFAULT_OUTDIR
        self.entries = []
        self.rows = []
        self.job = None

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        self.titlebar = TitleBar("Blinky", self)
        self.titlebar.close_clicked.connect(self.close)
        self.titlebar.minimise_clicked.connect(self.showMinimized)
        self.titlebar.zoom_clicked.connect(self._toggle_zoom)
        shell.addWidget(self.titlebar)

        body = QWidget(self)
        shell.addWidget(body, 1)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(15, 14, 15, 14)
        lay.setSpacing(13)

        # Appears only when GitHub has a newer release than this one.
        self.update_bar = Balloon("info", "Update", kind="blue", parent=self)
        self.update_bar.clicked.connect(self._update_clicked)
        lay.addWidget(self.update_bar)
        self._release = None
        self._update_job = None

        # -- Camera -------------------------------------------------------
        cam = LunaGroup("Camera", self)
        head = QHBoxLayout()
        head.setSpacing(11)
        head.addWidget(CameraGlyph(34, self))
        col = QVBoxLayout()
        col.setSpacing(2)
        idline = QHBoxLayout()
        idline.setSpacing(7)
        self.led = StatusLed(self)
        idline.addWidget(self.led)
        self.status = QLabel("Looking for the camera…")
        self.status.setFont(ui_font(9, bold=True))
        self.status.setStyleSheet("color:#20303F;")
        idline.addWidget(self.status)
        idline.addStretch(1)
        col.addLayout(idline)
        self.detail = ElidedLabel("—", Qt.TextElideMode.ElideRight, self)
        self.detail.setFont(ui_font(8))
        self.detail.setStyleSheet("color:#5B6C7D;")
        col.addWidget(self.detail)
        head.addLayout(col, 1)
        btns = QHBoxLayout()
        btns.setSpacing(7)
        self.btn_check = GelButton("Check", "candy", self)
        self.btn_refresh = GelButton("Refresh", "grey", self)
        self.btn_check.clicked.connect(self.run_doctor)
        self.btn_refresh.clicked.connect(self.refresh)
        btns.addWidget(self.btn_check)
        btns.addWidget(self.btn_refresh)
        head.addLayout(btns)
        cam.addLayout(head)
        lay.addWidget(cam)

        # Appears only when the camera has the failure that recovery is for.
        self.rescue = Balloon(
            "warn", "Recover\u2026",
            "Recovery walks you through unplugging and replugging, saving "
            "more each time, until everything is off the camera.",
            parent=self)
        self.rescue.clicked.connect(self.run_recovery)
        lay.addWidget(self.rescue)

        # -- Photos -------------------------------------------------------
        shots = LunaGroup("Photos on the camera", self)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: #EDF1F7; width: 12px; border: none; }"
            "QScrollBar::handle:vertical { background: #8FB6DE; min-height: 22px;"
            " border-radius: 6px; border: 1px solid #5E8CBD; }"
            "QScrollBar::add-line, QScrollBar::sub-line { height: 0; }")
        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        self.rowbox = QVBoxLayout(holder)
        self.rowbox.setContentsMargins(0, 0, 4, 0)
        self.rowbox.setSpacing(4)
        self.empty = QLabel("Plug the camera in and press Refresh.")
        self.empty.setFont(ui_font(8))
        self.empty.setStyleSheet("color:#7C8B9B;")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rowbox.addWidget(self.empty)
        self.rowbox.addStretch(1)
        self.scroll.setWidget(holder)
        self.scroll.setMinimumHeight(110)
        shots.addWidget(self.scroll)
        lay.addWidget(shots, 1)

        # -- Activity -----------------------------------------------------
        act = LunaGroup("Activity", self)
        self.log = ActivityLog(self)
        self.log.setMinimumHeight(62)
        act.addWidget(self.log)
        lay.addWidget(act)

        # -- Save-to + action row ------------------------------------------
        dest = QHBoxLayout()
        dest.setSpacing(8)
        tag = QLabel("Save to")
        tag.setFont(ui_font(8, bold=True))
        tag.setStyleSheet("color:#20303F;")
        dest.addWidget(tag)
        self.destlabel = ElidedLabel(self._pretty(self.outdir),
                                     Qt.TextElideMode.ElideMiddle, self)
        self.destlabel.setFont(ui_font(8))
        self.destlabel.setStyleSheet(
            "color:#12447E; background:#FFFFFF; border:1px solid #A8BACF;"
            " border-radius:4px; padding:3px 8px;")
        dest.addWidget(self.destlabel, 1)
        self.btn_folder = GelButton("Change…", "grey", self, width=84)
        self.btn_folder.clicked.connect(self.pick_folder)
        dest.addWidget(self.btn_folder)
        lay.addLayout(dest)

        opts = LunaGroup("Options", self)

        self.still_boxes = {}
        row1 = QHBoxLayout()
        row1.setSpacing(12)
        lab = QLabel("Photos")
        lab.setFont(ui_font(8, bold=True))
        lab.setStyleSheet("color:#20303F;")
        lab.setFixedWidth(46)
        row1.addWidget(lab)
        for key, text, on in (("png", "PNG", True), ("jpeg", "JPEG", False),
                              ("bmp", "BMP", False)):
            box = AquaCheck(text, on, self)
            box.toggled.connect(
                lambda state, k=key: self._format_toggled("still", k, state))
            self.still_boxes[key] = box
            row1.addWidget(box)
        row1.addStretch(1)
        opts.addLayout(row1)

        self.video_boxes = {}
        self.video_row = QWidget(self)
        vrow = QHBoxLayout(self.video_row)
        vrow.setContentsMargins(0, 0, 0, 0)
        vrow.setSpacing(12)
        lab = QLabel("Videos")
        lab.setFont(ui_font(8, bold=True))
        lab.setStyleSheet("color:#20303F;")
        lab.setFixedWidth(46)
        vrow.addWidget(lab)
        for key, text in (("avi", "AVI"), ("gif", "GIF")):
            box = AquaCheck(text, True, self.video_row)
            box.toggled.connect(
                lambda state, k=key: self._format_toggled("video", k, state))
            self.video_boxes[key] = box
            vrow.addWidget(box)
        vrow.addStretch(1)
        self.video_row.hide()        # shown when the camera holds a clip
        opts.addWidget(self.video_row)

        self.fmtnote = QLabel("Raw camera data is always kept in raw/")
        self.fmtnote.setFont(ui_font(8))
        self.fmtnote.setStyleSheet("color:#5B6C7D;")
        self.fmtnote.setWordWrap(True)
        opts.addWidget(self.fmtnote)

        self.skipdupes = AquaCheck(
            "Skip photos already in this folder, even if renamed", True, self)
        opts.addWidget(self.skipdupes)

        self.autodl = AquaCheck(
            "Download automatically whenever the camera is plugged in",
            False, self)
        self.autodl.toggled.connect(self._auto_toggled)
        opts.addWidget(self.autodl)
        self.autonote = QLabel("")
        self.autonote.setFont(ui_font(8))
        self.autonote.setStyleSheet("color:#5B6C7D;")
        self.autonote.setWordWrap(True)
        opts.addWidget(self.autonote)
        self.eraseafter = AquaCheck(
            "Erase the camera after downloading", False, self, tint="warn")
        self.eraseafter.toggled.connect(self._erase_toggled)
        opts.addWidget(self.eraseafter)
        self.erasenote = QLabel("")
        self.erasenote.setFont(ui_font(8))
        self.erasenote.setStyleSheet("color:#9A6A00;")
        self.erasenote.setWordWrap(True)
        self.erasenote.hide()
        opts.addWidget(self.erasenote)
        lay.addWidget(opts)

        foot = QHBoxLayout()
        foot.setSpacing(10)
        self.bar = BarberPole(self)
        foot.addWidget(self.bar, 1)
        self.btn_download = GelButton("Download All", "blue", self, width=124)
        self.btn_download.clicked.connect(self.download)
        foot.addWidget(self.btn_download)
        lay.addLayout(foot)

        self.grip = ResizeGrip(self)
        self.grip.raise_()

        self._sync_service()
        # A background attempt from the last half hour still counts: the
        # notification may be why the window was opened.
        self._watch_seen = time.time() - 1800
        # Notice the camera coming and going without being asked to look.
        self._present = None
        self._watchdog = QTimer(self)
        self._watchdog.timeout.connect(self._poll_camera)
        self._watchdog.start(2000)

        self.set_busy(False)
        QTimer.singleShot(250, self.refresh)
        # Look for a newer release shortly after opening, then twice a day
        # for a window that is left open.
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self.check_for_update)
        self._update_timer.start(12 * 3600 * 1000)
        QTimer.singleShot(3000, self.check_for_update)

    def closeEvent(self, e):
        # A worker still talking to the camera must not be torn down under
        # Qt's feet; give it a moment to finish the transfer it is in.
        job = getattr(self, "job", None)
        if job is not None and job.isRunning():
            job.wait(5000)
        # Never leave the background downloader stopped because the window
        # was closed mid-operation.
        self._resume_service()
        super().closeEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "grip"):
            self.grip.move(self.width() - self.grip.width() - 3,
                           self.height() - self.grip.height() - 3)
            self.grip.setVisible(not self.isMaximized())

    # -- chrome -----------------------------------------------------------

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 7, 7)
        p.save()
        p.setClipPath(path)
        paint_pinstripes(p, self.rect())
        sheen = QRectF(r.left(), r.top(), r.width(), 96)
        sg = QLinearGradient(sheen.topLeft(), sheen.bottomLeft())
        sg.setColorAt(0.0, QColor(255, 255, 255, 120))
        sg.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(sheen, QBrush(sg))
        p.restore()
        p.setPen(QPen(QColor("#4C6A8E"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

    def _sync_service(self):
        """Reflect the background service's real state in the checkbox."""
        state, enabled = watch_service_state()
        self._service_state = state
        blocked = self.autodl.blockSignals(True)
        self.autodl.setChecked(state == "active")
        self.autodl.blockSignals(blocked)
        if state == "missing":
            self.autodl.setEnabled(False)
            self.autonote.setText(
                "The background service is not installed, so this can only "
                "download while the window is open.")
        elif state == "active":
            self.autonote.setText(
                "Running in the background%s: plug the camera in and it "
                "downloads by itself, window or no window."
                % (" and at login" if enabled else ""))
        else:
            self.autonote.setText(
                "Off. Downloads happen only when you press the button.")

    def _auto_toggled(self, on):
        if getattr(self, "_service_state", "missing") == "missing":
            return
        verb = ("enable", "--now") if on else ("disable", "--now")
        rc, out = _systemctl(verb[0], verb[1], WATCH_UNIT)
        if rc != 0:
            self.log.append("error", "could not %s the background service: %s"
                            % (verb[0], out))
        self._sync_service()
        self.log.append("info", "Automatic downloading is %s."
                        % ("on" if self.autodl.isChecked() else "off"))

    def _poll_camera(self):
        """Keep the window honest about whether the camera is there."""
        if self.job is not None and self.job.isRunning():
            return
        self._check_watch_status()
        here = blinky.camera_present()
        if here == self._present:
            return
        self._present = here
        if here:
            # Only look for ourselves if the service is not already on it.
            if self._service_state == "active":
                self.status.setText("Camera plugged in \u2014 the background "
                                    "service is downloading")
                self.led.set_state("busy")
            else:
                self.log.append("info", "Camera plugged in.")
                self.refresh()
        else:
            self.led.set_state("unknown")
            self.status.setText("No camera")
            self.detail.setText("Plug the camera in.")
            self._sync_service()

    def _toggle_zoom(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _chosen(self, kind):
        boxes = self.still_boxes if kind == "still" else self.video_boxes
        order = blinky.STILL_FORMATS if kind == "still" else blinky.VIDEO_FORMATS
        return tuple(k for k in order if boxes[k].isChecked())

    def _format_toggled(self, kind, key, state):
        """At least one format in each group has to stay ticked."""
        boxes = self.still_boxes if kind == "still" else self.video_boxes
        if not state and not self._chosen(kind):
            boxes[key].setChecked(True)      # put it back
            self.fmtnote.setText(
                "At least one %s format is needed. Raw camera data is always "
                "kept in raw/." % ("picture" if kind == "still" else "video"))
            return
        self.fmtnote.setText("Raw camera data is always kept in raw/")

    def _erase_toggled(self, on):
        self.erasenote.setVisible(on)
        self.erasenote.setText(
            "The camera has no per-photo delete, so this erases everything. "
            "It only runs if every photo downloads and verifies on disk, and "
            "you will be asked once more before anything is erased.")
        self.btn_download.text = "Download and Erase" if on else "Download All"
        self.btn_download.update()

    def _confirm_erase(self, count):
        """A plain modal in the app's own idiom, not a system dialog."""
        dlg = ConfirmDialog(
            "Erase the camera?",
            "All %d photo%s will be downloaded, verified on disk, and then "
            "erased from the camera.\n\nThe camera has no undo."
            % (count, "" if count == 1 else "s"),
            "Download and Erase", self)
        return dlg.ask()

    def _pretty(self, path):
        home = os.path.expanduser("~")
        return path.replace(home, "~", 1) if path.startswith(home) else path

    # -- plumbing ---------------------------------------------------------

    def set_busy(self, busy, caption=""):
        for b in (self.btn_check, self.btn_refresh, self.btn_download,
                  self.btn_folder, self.rescue.button):
            b.setEnabled(not busy)
        if busy:
            self.bar.start()
            self.led.set_state("busy")
            if caption:
                self.status.setText(caption)
        else:
            self.bar.stop()

    def _pause_service(self):
        """Stop the background downloader while we use the camera ourselves.

        Both would claim interface 0 and one would lose with EBUSY, so they
        must take turns. Returns True if it was running and we stopped it.
        """
        if getattr(self, "_service_state", "missing") != "active":
            return False
        rc, out = _systemctl("stop", WATCH_UNIT)
        if rc != 0:
            self.log.append("warn", "could not pause the background service "
                                    "(%s); it may contend for the camera" % out)
            return False
        self.log.append("info", "Paused the background service while this "
                                "window uses the camera.")
        return True

    def _resume_service(self):
        if not getattr(self, "_paused_service", False):
            return
        self._paused_service = False
        rc, out = _systemctl("start", WATCH_UNIT)
        if rc != 0:
            self.log.append("error", "could not restart the background "
                                     "service: %s" % out)
        else:
            self.log.append("info", "Background service resumed.")
        self._sync_service()

    def start(self, fn, on_ok, caption):
        if self.job is not None and self.job.isRunning():
            return
        self._paused_service = self._pause_service()
        self.set_busy(True, caption)
        job = Job(fn, self)
        job.line.connect(self.log.append)
        job.step.connect(self._on_step)
        job.item.connect(self._on_item)
        job.ok.connect(on_ok)
        job.failed.connect(self._on_failed)
        job.finished.connect(lambda: self.set_busy(False))
        job.finished.connect(self._resume_service)
        self.job = job
        job.start()

    def _on_step(self, frac, caption):
        self.bar.stop()
        self.bar.set_value(frac)
        if caption:
            self.status.setText(caption)

    def _on_item(self, index, state):
        if 0 <= index < len(self.rows):
            self.rows[index].set_state(state)

    def _on_failed(self, message):
        self.led.set_state("bad")
        self.status.setText("Something went wrong")
        self.detail.setText(message.splitlines()[0][:96])
        self.log.append("error", message)
        if is_link_trouble(getattr(self.sender(), "exc", None)):
            self._offer_recovery("The camera is plugged in but is not "
                                 "answering properly.")

    # -- updating ---------------------------------------------------------

    def check_for_update(self):
        """Ask GitHub, quietly, whether a newer release exists."""
        if self._update_job is not None and self._update_job.isRunning():
            return
        if self.update_bar.isVisible():
            return                   # already offered, or mid-install
        if os.environ.get("BLINKY_NO_UPDATE_CHECK"):
            return

        def work(job, log):
            return blinky.latest_release(log)

        job = Job(work, self)
        # Being offline is not worth a line in the Activity panel.
        job.ok.connect(self._update_checked)
        self._update_job = job
        job.start()

    def _update_checked(self, result):
        latest, info = result
        if not latest or (blinky.version_tuple(latest)
                          <= blinky.version_tuple(blinky.__version__)):
            return
        self._release = (latest, info)
        self.update_bar.head.setText(
            "Blinky %s is available \u2014 you have %s."
            % (latest, blinky.__version__))
        self.update_bar.body.setText(
            release_highlight(info.get("body"))
            or "Press Update to download and install it.")
        self.update_bar.set_button("Update")
        self.update_bar.show()
        self.log.append("info", "Blinky %s is available. Press Update to "
                                "install it." % latest)

    def _update_clicked(self):
        if self.update_bar.button.text == "Restart":
            self._restart_app()
            return
        if self._release is None:
            return
        if self._update_job is not None and self._update_job.isRunning():
            return
        latest, info = self._release
        self.update_bar.set_button("Updating\u2026", enabled=False)
        self.update_bar.body.setText("Downloading and checking the package"
                                     "\u2026")

        def work(job, log):
            import subprocess
            # Refuse a release without checksums: this ends in a root
            # install, and the window cannot ask "are you sure?" usefully.
            deb, data = blinky.fetch_release_deb(info, log, require_sums=True)
            folder = tempfile.mkdtemp(prefix="blinky-update-")
            # apt fetches local packages as its own unprivileged user, and
            # complains if it cannot read the folder.
            os.chmod(folder, 0o755)
            path = os.path.join(folder, deb)
            blinky.write_file_atomically(path, data)
            os.chmod(path, 0o644)
            if not shutil.which("pkexec"):
                return None, path, ""
            job.step.emit(0.0, "Waiting for your password\u2026")
            p = subprocess.run(["pkexec", "apt-get", "install", "-y", path],
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
            return p.returncode, path, p.stdout.decode("utf-8", "replace")

        job = Job(work, self)
        job.line.connect(self.log.append)
        job.step.connect(lambda _f, caption:
                         self.update_bar.body.setText(caption))
        job.ok.connect(lambda r, latest=latest: self._update_done(latest, r))
        job.failed.connect(self._update_failed)
        self._update_job = job
        job.start()

    def _update_done(self, latest, result):
        rc, path, out = result
        if rc is None:
            self.update_bar.body.setText(
                "Downloaded and verified, but this system has no pkexec to "
                "ask for your password. Install it with: sudo apt install %s"
                % path)
            self.update_bar.set_button("Update")
            return
        if rc != 0:
            for line in out.strip().splitlines()[-6:]:
                self.log.append("info", "   " + line)
            self.update_bar.body.setText(
                "Not installed: the password prompt was cancelled."
                if rc in (126, 127) else
                "Not installed: apt-get exited %d. Details are in the "
                "Activity panel." % rc)
            self.update_bar.set_button("Update")
            return
        # The background service is still running the old code; let it pick
        # up the new one (a no-op when it is not running).
        _systemctl("try-restart", WATCH_UNIT)
        self.update_bar.head.setText("Blinky %s is installed." % latest)
        self.update_bar.body.setText("Restart the window to start using it.")
        self.update_bar.set_button("Restart")
        self.log.append("out", "Installed Blinky %s." % latest)

    def _update_failed(self, message):
        self.update_bar.body.setText("Not installed: %s"
                                     % message.splitlines()[0][:120])
        self.update_bar.set_button("Update")
        self.log.append("error", message)

    def _restart_app(self):
        """Close this window and open the newly installed one."""
        from PyQt6.QtCore import QProcess
        argv = list(sys.argv)
        if not self.close():
            return
        QProcess.startDetached(sys.executable, argv)
        QApplication.quit()

    # -- recovery ---------------------------------------------------------

    def _check_watch_status(self):
        """Offer recovery when the background download drops out partway."""
        st = blinky.read_watch_status()
        if not st or st.get("time", 0) <= self._watch_seen:
            return
        self._watch_seen = st["time"]
        result = st.get("result")
        if result == "complete":
            self.rescue.hide()
        elif result in ("partial", "nothing"):
            if st.get("total"):
                head = ("The background download dropped out partway: %d of "
                        "%d saved." % (st.get("saved") or 0, st["total"]))
            else:
                head = ("The background download could not get through to "
                        "the camera.")
            self._offer_recovery(head)

    def _offer_recovery(self, headline):
        self.rescue.head.setText(headline)
        self.rescue.show()
        self.log.append("warn", headline + " Press Recover\u2026 to get the "
                                           "photos off a replug at a time.")

    def run_recovery(self):
        if self.job is not None and self.job.isRunning():
            return
        # Every replug would set off the background service and the window's
        # own plug-in refresh, and both would fight the dialog for the camera.
        paused = self._pause_service()
        self._watchdog.stop()
        self.set_busy(True, "Recovering\u2026")
        self.log.append("info", "Recovery started.")
        opts = dict(outdir=self.outdir, still_formats=self._chosen("still"),
                    video_formats=self._chosen("video") or ("avi",),
                    quality=92)
        try:
            outcome = RecoveryDialog(opts, self.log.append, self).run()
        finally:
            self.set_busy(False)
            self._present = blinky.camera_present()
            self._watchdog.start(2000)
            self._paused_service = paused
            self._resume_service()
        if outcome == "done":
            self.rescue.hide()
            self.log.append("out", "Recovery finished: everything on the "
                                   "camera is saved.")
        else:
            self.log.append("info", "Recovery stopped. Press Recover\u2026 "
                                    "to pick up where it left off.")
        if self._present:
            self.refresh()

    # -- operations -------------------------------------------------------

    def refresh(self):
        outdir = self.outdir

        def work(job, log):
            cam = blinky.Blink2(log)
            cam.open()
            try:
                fw = cam.firmware_id()
                n = cam.get_numpics()
                entries = []
                already = {}
                if n:
                    entries, _, _ = cam.get_directory(n)
                    # Whether a photo is already saved is a question about
                    # content, not about filenames: the camera renumbers from
                    # zero after an erase, so image0000 on the camera and
                    # image0000 in the folder are routinely different photos.
                    index = blinky.local_fingerprints(outdir)
                    sizes = {sz for sz, _ in index}
                    for i, e in enumerate(entries):
                        if e.data_bytes in sizes:
                            try:
                                key = cam.fingerprint(e)
                                if key in index:
                                    already[i] = index[key]
                            except blinky.CameraError:
                                pass
                return fw, n, entries, already
            finally:
                cam.close()

        self.log.append("info", "Looking for the camera…")
        self.start(work, self._loaded, "Reading the camera…")

    def _loaded(self, result):
        fw, n, entries, already = result
        self.entries = entries
        self.already_saved = dict(already)
        self.led.set_state("ok")
        self.status.setText("SiPix StyleCam Blink II")
        total = sum(e.data_bytes for e in entries)
        self.detail.setText(
            "%d photo%s · %s KB · firmware %s"
            % (n, "" if n == 1 else "s", "{:,}".format(total // 1024),
               fw.hex(" ")))
        self.video_row.setVisible(any(e.is_movie for e in entries))
        self._rebuild_rows()
        self.log.append("out", "Found %d photo%s on the camera."
                        % (n, "" if n == 1 else "s"))

    def _rebuild_rows(self):
        while self.rowbox.count():
            item = self.rowbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.rows = []
        if not self.entries:
            msg = QLabel("The camera is empty.")
            msg.setFont(ui_font(8))
            msg.setStyleSheet("color:#7C8B9B;")
            msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.rowbox.addWidget(msg)
        for i, e in enumerate(self.entries):
            row = PhotoRow(e, self.outdir, self)
            # Only content proves a photo is already saved. A file of the same
            # name may well be a different picture from before an erase.
            known = getattr(self, "already_saved", {})
            if i in known:
                row.set_state("done", known[i])
            self.rows.append(row)
            self.rowbox.addWidget(row)
        self.rowbox.addStretch(1)

    def run_doctor(self):
        def work(job, log):
            results, cam = blinky.run_checks(log)
            if cam is not None:
                cam.close()
            for i, r in enumerate(results):
                job.step.emit((i + 1) / 5.0, "")
            return results

        self.log.clear()
        self.log.append("info", "Running the five checks…")
        self.start(work, self._doctored, "Checking…")

    def _doctored(self, results):
        for r in results:
            self.log.append("out" if r.ok else "error",
                            "%s  %d. %s" % ("[PASS]" if r.ok else "[FAIL]",
                                            r.number, r.title))
            for d in r.detail[:4]:
                self.log.append("info", "      " + d)
        bad = [r for r in results if not r.ok]
        if bad:
            self.led.set_state("bad")
            self.status.setText("Check %d failed" % bad[0].number)
            self.detail.setText(bad[0].title)
            if bad[0].fix:
                self.log.append("warn", "Likely fix:")
                for line in blinky._wrap(bad[0].fix)[:14]:
                    self.log.append("info", "   " + line)
            kernel_trouble = any("USB trouble" in d for d in bad[0].detail)
            if bad[0].number in (4, 5) or (bad[0].number == 1
                                           and kernel_trouble):
                self._offer_recovery("Check %d failed: %s."
                                     % (bad[0].number, bad[0].title.lower()))
        else:
            self.led.set_state("ok")
            self.status.setText("All five checks passed")
            self.detail.setText("The camera is ready to talk to.")

    def pick_folder(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "Save photos to", self.outdir)
        if chosen:
            self.outdir = chosen
            self.destlabel.setText(self._pretty(chosen))
            self._rebuild_rows()

    def download(self):
        if not self.entries:
            self.log.append("warn", "Nothing to download. Press Refresh first.")
            return
        erase = self.eraseafter.isChecked()
        if erase and not self._confirm_erase(len(self.entries)):
            self.log.append("info", "Erase cancelled; nothing was downloaded.")
            return

        entries, outdir = list(self.entries), self.outdir
        quality = 92
        still_formats = self._chosen("still")
        video_formats = self._chosen("video") or ("avi",)
        skip_dupes = self.skipdupes.isChecked()
        dirs = blinky.output_dirs(outdir)
        rawdir = dirs["raw"]

        def work(job, log):
            os.makedirs(outdir, exist_ok=True)
            os.makedirs(rawdir, exist_ok=True)
            cam = blinky.Blink2(log)
            cam.open()
            saved = failed = partial = dropped = 0
            states, verified, failures, skipped = {}, {}, [], []
            paths = {}
            index = blinky.local_fingerprints(outdir) if skip_dupes else {}
            sizes = {sz for sz, _ in index}
            # The camera renumbers from zero after an erase, so carry on from
            # what is in the folder rather than reusing its index.
            counter = blinky.next_free_index(outdir)
            if counter:
                log.out("Continuing numbering from image%04d" % counter)
            try:
                for i, e in enumerate(entries):
                    job.step.emit(i / len(entries), "Reading %s\u2026" % e.basename)

                    known = already_saved(cam, e, index, sizes)
                    if known:
                        log.out("%s: already saved as %s"
                                % (e.basename, os.path.basename(known)))
                        job.item.emit(i, "done")
                        states[i] = "done"
                        paths[i] = known
                        skipped.append(e.basename)
                        continue

                    job.item.emit(i, "busy")
                    try:
                        data = cam.read_image_with_retries(e)
                    except blinky.CameraError as exc:
                        log.error("%s: %s" % (e.basename, exc))
                        job.item.emit(i, "failed")
                        states[i] = "failed"
                        failures.append((e.basename, str(exc)))
                        failed += 1
                        dropped += 1
                        if isinstance(exc, (blinky.NotPresent,
                                            blinky.DeviceStalled,
                                            blinky.PermissionDenied)):
                            # Gone or wedged: every remaining read would fail
                            # the same way, slowly. Recovery takes it from
                            # here.
                            dropped += len(entries) - i - 1
                            log.error("stopping: the camera is no longer "
                                      "usable")
                            break
                        continue

                    state, raw, problem = save_photo(
                        data, e, "image%04d" % counter, outdir, dirs,
                        still_formats, video_formats, quality, log)
                    counter += 1
                    paths[i] = raw
                    verified[i] = (raw, len(data),
                                   hashlib.sha256(data).hexdigest())
                    states[i] = state
                    job.item.emit(i, state)
                    if state == "failed":
                        failures.append((e.basename, problem))
                        failed += 1
                    else:
                        saved += 1
                        partial += 1 if state == "partial" else 0
                job.step.emit(1.0, "")

                erased = None
                if erase:
                    # Reuse the command line tool's safety rules rather than
                    # writing a second, subtly different set of them.
                    rc = blinky._delete_after_download(
                        cam, log, entries, list(range(len(entries))),
                        verified, failures, skipped)
                    erased = (rc == 0)
                return saved, failed, partial, states, erased, paths, dropped
            finally:
                cam.close()

        self.log.append("info", "Downloading %d photo%s to %s"
                        % (len(entries), "" if len(entries) == 1 else "s",
                           self._pretty(outdir)))
        self.start(work, self._downloaded, "Downloading\u2026")

    def _downloaded(self, result):
        saved, failed, partial, states, erased, paths, dropped = result
        if dropped:
            have = sum(1 for st in states.values() if st in ("done", "partial"))
            self._offer_recovery("The camera dropped out partway: %d of %d "
                                 "saved." % (have, len(self.entries)))
        elif not failed:
            self.rescue.hide()
        self.led.set_state("ok" if not failed else "bad")
        self.status.setText("Saved %d photo%s" % (saved, "" if saved == 1 else "s"))
        bits = ["%d saved" % saved]
        if partial:
            bits.append("%d partial" % partial)
        if failed:
            bits.append("%d failed" % failed)
        if erased is True:
            bits.append("camera erased")
            self.entries = []
        elif erased is False:
            bits.append("camera NOT erased")
            self.log.append("warn", "The camera was left untouched because "
                                    "not every photo was verified on disk.")
        self.detail.setText(" · ".join(bits) + " · " +
                            self._pretty(self.outdir))
        self.log.append("out", "Done: " + ", ".join(bits) + ".")
        if erased is True:
            self._rebuild_rows()
            self.status.setText("Saved %d and erased the camera" % saved)
            QTimer.singleShot(400, self.refresh)
            return
        # Remember where each photo landed so the rows show the right picture.
        self.already_saved = dict(getattr(self, "already_saved", {}))
        self.already_saved.update(paths)
        self._rebuild_rows()
        for i, row in enumerate(self.rows):
            if i in states:
                # What actually happened beats what is on disk: a partial
                # decode still leaves a PNG behind, and saying "saved" would
                # hide the one photo the user needs to know about.
                row.set_state(states[i], paths.get(i))


def main(argv=None):
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Blinky")
    app.setApplicationDisplayName("Blinky")
    # Lets the compositor match the window to blinky.desktop on Wayland.
    app.setDesktopFileName("blinky")
    app.setFont(ui_font(8))
    themed = QIcon.fromTheme("blinky")
    app.setWindowIcon(themed if not themed.isNull() else app_icon())
    win = BlinkyWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
