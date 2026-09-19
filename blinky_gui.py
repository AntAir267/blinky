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
import sys
import time

from PyQt6.QtCore import (QPoint, QPointF, QRect, QRectF, QSize, Qt, QThread,
                          QTimer, pyqtSignal)
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontDatabase, QIcon, QLinearGradient,
                         QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
                         QRadialGradient)
from PyQt6.QtWidgets import (QApplication, QFileDialog, QFrame, QGridLayout,
                             QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
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


class Segmented(QWidget):
    """An Aqua segmented control: one pill divided into pressed-in choices."""

    changed = pyqtSignal(str)

    def __init__(self, options, parent=None, width=None):
        super().__init__(parent)
        self.options = list(options)             # [(value, label), ...]
        self.index = 0
        self._hover = -1
        self.setFixedHeight(22)
        self.setFont(ui_font(8))
        w = width or (sum(self.fontMetrics().horizontalAdvance(t) + 24
                          for _, t in self.options))
        self.setFixedWidth(w)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

    def value(self):
        return self.options[self.index][0]

    def setValue(self, value):
        for i, (v, _) in enumerate(self.options):
            if v == value:
                self.index = i
                self.update()
                return

    def _hit(self, x):
        seg = self.width() / len(self.options)
        return max(0, min(len(self.options) - 1, int(x // seg)))

    def mouseMoveEvent(self, e):
        self._hover = self._hit(e.position().x())
        self.update()

    def leaveEvent(self, e):
        self._hover = -1
        self.update()

    def mouseReleaseEvent(self, e):
        i = self._hit(e.position().x())
        if i != self.index:
            self.index = i
            self.update()
            self.changed.emit(self.value())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        paint_gel(p, r, GEL_GREY, radius=r.height() / 2)
        seg = r.width() / len(self.options)
        p.save()
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setClipPath(path)
        for i, (_, label) in enumerate(self.options):
            cell = QRectF(r.left() + i * seg, r.top(), seg, r.height())
            if i == self.index:
                paint_gel(p, cell.adjusted(1, 1, -1, -1), GEL_BLUE,
                          radius=(cell.height() - 2) / 2, gloss=0.42)
            elif i == self._hover:
                p.fillRect(cell, QColor(255, 255, 255, 90))
            if i:
                p.setPen(QPen(QColor(0, 0, 0, 40), 1))
                p.drawLine(QPointF(cell.left(), r.top() + 3),
                           QPointF(cell.left(), r.bottom() - 3))
            p.setFont(ui_font(8, bold=(i == self.index)))
            p.setPen(QColor("#FFFFFF") if i == self.index else INK)
            p.drawText(cell, Qt.AlignmentFlag.AlignCenter, label)
        p.restore()
        p.setPen(QPen(QColor("#8CA0B8"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


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

    def _thumb(self):
        if not self.saved_path:
            return None
        base = os.path.splitext(os.path.basename(self.saved_path))[0]
        here = os.path.dirname(self.saved_path)
        # The picture sits beside the raw, or one level up when raws are
        # kept in a raw/ subfolder.
        for folder in (here, os.path.dirname(here)):
            for ext in (".png", ".jpg"):
                path = os.path.join(folder, base + ext)
                if os.path.exists(path):
                    pm = QPixmap(path)
                    if not pm.isNull():
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

    def run(self):
        log = GuiLog(self.line.emit)
        try:
            self.ok.emit(self.fn(self, log))
        except blinky.CameraError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:                       # never kill the UI
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


class BlinkyWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Blinky")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(600, 780)
        self.resize(600, 820)

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
        self.scroll.setMinimumHeight(190)
        shots.addWidget(self.scroll)
        lay.addWidget(shots, 1)

        # -- Activity -----------------------------------------------------
        act = LunaGroup("Activity", self)
        self.log = ActivityLog(self)
        self.log.setMinimumHeight(104)
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
        row1 = QHBoxLayout()
        row1.setSpacing(9)
        fmtlabel = QLabel("Save stills as")
        fmtlabel.setFont(ui_font(8, bold=True))
        fmtlabel.setStyleSheet("color:#20303F;")
        row1.addWidget(fmtlabel)
        self.fmt = Segmented([("png", "PNG"), ("jpeg", "JPEG"),
                              ("both", "Both")], self)
        row1.addWidget(self.fmt)
        self.fmtnote = QLabel("lossless from the decoded pixels")
        self.fmtnote.setFont(ui_font(8))
        self.fmtnote.setStyleSheet("color:#5B6C7D;")
        self.fmt.changed.connect(self._format_changed)
        row1.addWidget(self.fmtnote, 1)
        opts.addLayout(row1)

        self.skipdupes = AquaCheck(
            "Skip photos already in this folder, even if renamed", True, self)
        opts.addWidget(self.skipdupes)
        self.rawsub = AquaCheck(
            "Keep raw camera files in a raw/ subfolder", True, self)
        self.rawsub.toggled.connect(lambda _: self._rebuild_rows())
        opts.addWidget(self.rawsub)
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

        self.set_busy(False)
        QTimer.singleShot(250, self.refresh)

    def closeEvent(self, e):
        # A worker still talking to the camera must not be torn down under
        # Qt's feet; give it a moment to finish the transfer it is in.
        job = getattr(self, "job", None)
        if job is not None and job.isRunning():
            job.wait(5000)
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

    def _toggle_zoom(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _format_changed(self, value):
        self.fmtnote.setText({
            "png": "lossless from the decoded pixels",
            "jpeg": "smaller, but a second lossy pass",
            "both": "PNG and JPEG side by side",
        }[value])

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
        from PyQt6.QtWidgets import QDialog
        dlg = QDialog(self)
        dlg.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                           Qt.WindowType.Dialog)
        dlg.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        dlg.setFixedWidth(400)
        box = QVBoxLayout(dlg)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        bar = TitleBar("Erase the camera?", dlg)
        bar.close_clicked.connect(dlg.reject)
        bar.minimise_clicked.connect(dlg.reject)
        box.addWidget(bar)
        panel = QWidget(dlg)
        box.addWidget(panel)
        inner = QVBoxLayout(panel)
        inner.setContentsMargins(16, 14, 16, 14)
        inner.setSpacing(12)
        msg = QLabel("All %d photo%s will be downloaded, verified on disk, and "
                     "then erased from the camera.\n\nThe camera has no undo."
                     % (count, "" if count == 1 else "s"))
        msg.setFont(ui_font(9))
        msg.setWordWrap(True)
        msg.setStyleSheet("color:#20303F;")
        inner.addWidget(msg)
        rowb = QHBoxLayout()
        rowb.addStretch(1)
        cancel = GelButton("Cancel", "grey", dlg)
        go = GelButton("Download and Erase", "candy", dlg, width=160)
        cancel.clicked.connect(dlg.reject)
        go.clicked.connect(dlg.accept)
        rowb.addWidget(cancel)
        rowb.addWidget(go)
        inner.addLayout(rowb)

        def paint(_):
            p = QPainter(dlg)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            r = QRectF(dlg.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            path = QPainterPath()
            path.addRoundedRect(r, 7, 7)
            p.save()
            p.setClipPath(path)
            paint_pinstripes(p, dlg.rect())
            p.restore()
            p.setPen(QPen(QColor("#4C6A8E"), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        dlg.paintEvent = paint
        return dlg.exec() == QDialog.DialogCode.Accepted

    def _pretty(self, path):
        home = os.path.expanduser("~")
        return path.replace(home, "~", 1) if path.startswith(home) else path

    # -- plumbing ---------------------------------------------------------

    def set_busy(self, busy, caption=""):
        for b in (self.btn_check, self.btn_refresh, self.btn_download,
                  self.btn_folder):
            b.setEnabled(not busy)
        if busy:
            self.bar.start()
            self.led.set_state("busy")
            if caption:
                self.status.setText(caption)
        else:
            self.bar.stop()

    def start(self, fn, on_ok, caption):
        if self.job is not None and self.job.isRunning():
            return
        self.set_busy(True, caption)
        job = Job(fn, self)
        job.line.connect(self.log.append)
        job.step.connect(self._on_step)
        job.item.connect(self._on_item)
        job.ok.connect(on_ok)
        job.failed.connect(self._on_failed)
        job.finished.connect(lambda: self.set_busy(False))
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
        fmt, quality = self.fmt.value(), 92
        skip_dupes = self.skipdupes.isChecked()
        rawdir = blinky.raw_dir(outdir, self.rawsub.isChecked())

        def work(job, log):
            os.makedirs(outdir, exist_ok=True)
            os.makedirs(rawdir, exist_ok=True)
            cam = blinky.Blink2(log)
            cam.open()
            saved = failed = partial = 0
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

                    if index and e.data_bytes in sizes:
                        try:
                            key = cam.fingerprint(e)
                        except blinky.CameraError:
                            key = None
                        if key in index:
                            log.out("%s: already saved as %s"
                                    % (e.basename,
                                       os.path.basename(index[key])))
                            job.item.emit(i, "done")
                            states[i] = "done"
                            paths[i] = index[key]
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
                        continue

                    stem = "image%04d" % counter
                    counter += 1
                    if e.is_movie:
                        # A clip needs no decoding; the saved file is the raw
                        # data. Name it after the container we actually find.
                        ext, known = blinky.movie_extension(data)
                        if not known:
                            log.warn("%s is flagged as a clip but is not an "
                                     "AVI; saving as %s" % (e.basename, ext))
                        path = blinky.free_path(
                            os.path.join(outdir, stem + ext))
                        blinky.write_file_atomically(path, data)
                        paths[i] = path
                        job.item.emit(i, "done")
                        states[i] = "done"
                        verified[i] = (path, len(data),
                                       hashlib.sha256(data).hexdigest())
                        saved += 1
                        continue

                    # free_path is a belt-and-braces guard: the counter should
                    # already be past anything on disk, but overwriting a
                    # photo is not a mistake worth risking.
                    raw = blinky.free_path(os.path.join(rawdir, stem + ".raw"))
                    stem = os.path.splitext(os.path.basename(raw))[0]
                    blinky.write_file_atomically(raw, data)
                    paths[i] = raw
                    verified[i] = (raw, len(data),
                                   hashlib.sha256(data).hexdigest())
                    try:
                        w, h, raster, part = blinky.decode_still(data, log)
                        blinky.write_still(outdir, stem, w, h, raster,
                                           fmt, quality)
                        states[i] = "partial" if part else "done"
                        job.item.emit(i, states[i])
                        partial += 1 if part else 0
                        saved += 1
                    except blinky.CameraError as exc:
                        log.error("%s: decode failed: %s" % (e.basename, exc))
                        log.info("the raw data is kept at %s" % raw)
                        job.item.emit(i, "failed")
                        states[i] = "failed"
                        failures.append((e.basename, "decode: %s" % exc))
                        failed += 1
                job.step.emit(1.0, "")

                erased = None
                if erase:
                    # Reuse the command line tool's safety rules rather than
                    # writing a second, subtly different set of them.
                    rc = blinky._delete_after_download(
                        cam, log, entries, list(range(len(entries))),
                        verified, failures, skipped)
                    erased = (rc == 0)
                return saved, failed, partial, states, erased, paths
            finally:
                cam.close()

        self.log.append("info", "Downloading %d photo%s to %s"
                        % (len(entries), "" if len(entries) == 1 else "s",
                           self._pretty(outdir)))
        self.start(work, self._downloaded, "Downloading\u2026")

    def _downloaded(self, result):
        saved, failed, partial, states, erased, paths = result
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
