"""② 원문 확인 카드 보기가 쓰는 아이콘 자리 그림(IconPlaceView)과 조작 중 창 잠금(busy)."""

from __future__ import annotations

import functools
import inspect
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget


def busy(fn):
    """카드 보기 조작: 끝날 때까지 창 전체를 잠근다 (다른 버튼·목록·그림을 누르지 못하게).
    조작이 화면을 멈춘 채 도는 동안 누른 클릭은 쌓였다가 끝난 뒤 그대로 실행되므로(예: ‘되돌리기’ 바로 아래
    ‘원본 그대로’가 눌림), 잠근 채로 쌓인 입력을 먼저 처리해 꺼진 버튼에서 버려지게 한 뒤 푼다.
    안에서 다시 부르면(조작 안의 조작) 그대로 실행한다. 신호가 넘기는 여분 인자(clicked의 checked)는 버린다."""
    params = list(inspect.signature(fn).parameters.values())
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        n_args = None
    else:
        n_args = len([p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]) - 1

    @functools.wraps(fn)
    def run(self, *args, **kw):
        if n_args is not None:
            args = args[:n_args]
        if self._busy_depth:
            return fn(self, *args, **kw)
        self._busy_depth += 1
        focus = QApplication.focusWidget()  # 잠그면 포커스가 빠진다: 끝나면 돌려준다 (목록 화살표 키 등)
        self.setEnabled(False)
        self.setCursor(Qt.CursorShape.WaitCursor)  # 이 창에만 (묻는 창은 보통 화살표)
        try:
            return fn(self, *args, **kw)
        finally:
            try:
                QApplication.processEvents()  # 잠근 채로: 그동안 쌓인 클릭은 꺼진 위젯에 가서 버려진다
            finally:
                self._busy_depth -= 1
                self.unsetCursor()
                self.setEnabled(True)
                if focus is not None and self.isVisible() and self.isAncestorOf(focus) and focus.isEnabled() \
                        and focus.isVisible():
                    focus.setFocus()

    return run


# ================================================================ 아이콘 이름
class IconPlaceView(QWidget):
    """카드 영역 그림 위에 아이콘 자리를 보여 주고, 마우스로 끌어 새 자리를 잡는다.

    초록 = 같은 칸에서 이미 찾은 아이콘, 파랑 = 이 아이콘의 지금 자리, 주황 점선 = 예상 위치(못 찾았을 때),
    빨강 = 사람이 새로 잡은 자리. 좌표는 모두 그림 px."""

    drawn = Signal()

    def __init__(self):
        super().__init__()
        self._pm = QPixmap()
        self.marks: list[tuple[list, str]] = []
        self.box = self.guess = self.new = None
        self._drag = None
        self.setMinimumSize(300, 200)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(False)

    def set_data(self, pm: QPixmap, box, guess, marks: list[tuple[list, str]]):
        self._pm, self.box, self.guess, self.marks, self.new, self._drag = pm, box, guess, marks, None, None
        self.update()

    def _geom(self):
        w, h = self._pm.width(), self._pm.height()
        if not w or not h:
            return 1.0, 0.0, 0.0
        s = min((self.width() - 16) / w, (self.height() - 16) / h, 3.0)
        return s, (self.width() - w * s) / 2, (self.height() - h * s) / 2

    def _to_img(self, p) -> tuple[float, float]:
        s, ox, oy = self._geom()
        return (min(max((p.x() - ox) / s, 0), self._pm.width()), min(max((p.y() - oy) / s, 0), self._pm.height()))

    def _rect(self, b) -> QRectF:
        s, ox, oy = self._geom()
        return QRectF(ox + b[0] * s, oy + b[1] * s, (b[2] - b[0]) * s, (b[3] - b[1]) * s)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#FAFAFA"))
        if self._pm.isNull():
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        s, ox, oy = self._geom()
        p.drawPixmap(QRectF(ox, oy, self._pm.width() * s, self._pm.height() * s), self._pm, QRectF(self._pm.rect()))
        f = p.font()
        f.setBold(True)
        p.setFont(f)

        def frame(b, color, label="", dash=False, width=2):
            r = self._rect(b)
            pen = QPen(QColor(color), width)
            if dash:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)
            if label:
                fm = p.fontMetrics()
                tw, th = fm.horizontalAdvance(label) + 8, fm.height() + 2
                ty = r.top() - th if r.top() - th >= 0 else r.bottom()
                p.fillRect(QRectF(r.left(), ty, tw, th), QColor(color))
                p.setPen(QColor("white"))
                p.drawText(QRectF(r.left(), ty, tw, th), Qt.AlignmentFlag.AlignCenter, label)

        for b, label, *rest in self.marks:  # (상자, 이름[, 예상 위치인가[, 아이콘 아님·합침으로 정했나[, 목록에서 고름]]])
            if len(rest) > 2 and rest[2]:  # 목록에서 고른 자리: 보라 굵은 테두리
                frame(b, "#7B2CBF", label, dash=bool(rest[0] or rest[1]), width=3)
            elif len(rest) > 1 and rest[1]:  # 아이콘 아님·옆에 합친 자리: 정해진 것이라 회색 점선 (어디였는지만)
                frame(b, "#8A8A8A", label, dash=True, width=1)
            elif rest and rest[0]:  # 위치를 못 찾은 다른 아이콘: 예상 자리에 점선
                frame(b, "#9A6A00", label, dash=True)
            else:
                frame(b, "#2E8B57", label)
        if self.guess and not self.new:
            frame(self.guess, "#E67E22", "예상 위치", dash=True)
        if self.box:
            frame(self.box, "#1F6FEB", "지금 자리", width=3 if not self.new else 2)
        if self.new:
            frame(self.new, "#D62828", "새 자리", width=3)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and not self._pm.isNull():
            self._drag = self._to_img(e.position())
            self.new = None
            self.update()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            x, y = self._to_img(e.position())
            x0, y0 = self._drag
            self.new = [min(x0, x), min(y0, y), max(x0, x), max(y0, y)]
            self.update()

    def mouseReleaseEvent(self, e):
        if self._drag is None:
            return
        self._drag = None
        if self.new and (self.new[2] - self.new[0] < 4 or self.new[3] - self.new[1] < 4):
            self.new = None  # 그냥 누른 것
        self.update()
        self.drawn.emit()
