"""② 원문 확인 카드 보기가 쓰는 아이콘 자리 그림(IconPlaceView: 확대·크기 바꾸기)과 조작 중 창 잠금(busy)."""

from __future__ import annotations

import functools
import inspect
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
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


# ================================================================ 아이콘 자리 그림
EDGE_PX = 7  # 상자 테두리를 잡는 너비 (화면 px)
MAX_ZOOM = 12.0


class IconPlaceView(QWidget):
    """카드 영역 그림 위에 아이콘 자리를 보여 주고, 마우스로 끌어 새 자리를 잡는다.

    초록 = 같은 칸에서 이미 찾은 아이콘, 파랑 = 이 아이콘의 지금 자리, 주황 점선 = 예상 위치(못 찾았을 때),
    빨강 = 사람이 새로 잡은 자리. 좌표는 모두 그림 px.
    - 빈 곳에서 끌면 새 자리. 끄는 동안은 다른 상자를 숨긴다 (새 상자가 가려지지 않게).
    - 지금 자리(파랑)·새 자리(빨강)의 테두리·모서리를 끌면 크기를 바꾼다 (결과는 새 자리).
    - 다른 아이콘 상자를 누르면(끌지 않고) picked(자리 id).
    - 휠: 마우스 자리를 중심으로 확대·축소. 가운데·오른쪽 단추로 끌면 옮긴다. 처음은 화면 맞춤 (fit)."""

    drawn = Signal()
    picked = Signal(str)
    zoomed = Signal(float)
    box_moved = Signal()  # 보이는 상자(새 자리·지금 자리)가 바뀜: 확대 칸이 따라 그린다 (끄는 동안에도)

    def __init__(self):
        super().__init__()
        self._pm = QPixmap()
        self.marks: list[tuple] = []
        self.box = self.guess = self.new = None
        self._focus_area = None
        self._drag = None  # ("new", 시작점) | ("resize", 잡은 변들, 처음 상자) | ("pan", 시작 화면점, 처음 이동)
        self._drag_btn = None  # 끌기를 시작한 단추 (다른 단추를 떼어도 끝나지 않게)
        self._press = None  # 누른 화면 자리 (끌지 않고 뗐는지 보려고)
        self.zoom = 1.0
        self._pan = [0.0, 0.0]
        self.setMinimumSize(300, 200)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

    def set_data(self, pm: QPixmap, box, guess, marks: list[tuple]):
        resized = pm.size() != self._pm.size()
        if resized:  # 다른 그림: 화면 맞춤으로
            self.zoom, self._pan = 1.0, [0.0, 0.0]
        self._pm, self.box, self.guess, self.marks, self.new, self._drag = pm, box, guess, marks, None, None
        self._drag_btn = None
        self._focus_area = None  # 확대 칸이 보이는 그림 자리 (focus_crop): 아이콘을 바꿀 때만 새로 정한다
        self.update()
        self.box_moved.emit()
        if resized:
            self.zoomed.emit(self.zoom)

    # ------------------------------------------------------------ 확대
    def fit(self) -> None:
        """화면 맞춤 (확대 1배·가운데)."""
        self.zoom, self._pan = 1.0, [0.0, 0.0]
        self.update()
        self.zoomed.emit(self.zoom)

    def zoom_by(self, f: float, at=None) -> None:
        """f배 확대·축소. at(화면 점)의 그림 자리가 그대로 있게 (없으면 가운데)."""
        if self._pm.isNull():
            return
        at = at if at is not None else QPointF(self.width() / 2, self.height() / 2)
        ix, iy = self._to_img_raw(at)
        z = min(MAX_ZOOM, max(1.0, self.zoom * f))
        if z == self.zoom:
            return
        self.zoom = z
        if z == 1.0:
            self._pan = [0.0, 0.0]
        else:
            s = self._base() * z
            w, h = self._pm.width(), self._pm.height()
            self._pan = [at.x() - ix * s - (self.width() - w * s) / 2, at.y() - iy * s - (self.height() - h * s) / 2]
        self.update()
        self.zoomed.emit(self.zoom)

    def center_on(self, box, only_if_hidden: bool = True) -> bool:
        """확대한 채로 box(그림 px)가 화면 가운데에 오게 옮긴다 (배율은 그대로). 확대하지 않았거나 box가 없으면 그대로.
        only_if_hidden: box가 이미 화면 안에 다 보이면 옮기지 않는다 (보이는 상자를 눌러도 그림이 튀지 않게).
        옮겼으면 True."""
        if self._pm.isNull() or not box or self.zoom <= 1.0:
            return False
        x0, y0, x1, y1 = (float(v) for v in box)
        if only_if_hidden:
            r = self._rect((x0, y0, x1, y1))
            if r.left() >= 0 and r.top() >= 0 and r.right() <= self.width() and r.bottom() <= self.height():
                return False
        s = self._base() * self.zoom
        w, h = self._pm.width(), self._pm.height()
        self._pan = [(w / 2 - (x0 + x1) / 2) * s, (h / 2 - (y0 + y1) / 2) * s]
        self.update()
        return True

    def _base(self) -> float:
        w, h = self._pm.width(), self._pm.height()
        if not w or not h:
            return 1.0
        return max(1e-3, min((self.width() - 16) / w, (self.height() - 16) / h, 3.0))

    def _geom(self):
        w, h = self._pm.width(), self._pm.height()
        if not w or not h:
            return 1.0, 0.0, 0.0
        s = self._base() * self.zoom
        return s, (self.width() - w * s) / 2 + self._pan[0], (self.height() - h * s) / 2 + self._pan[1]

    def _to_img_raw(self, p) -> tuple[float, float]:
        s, ox, oy = self._geom()
        return (p.x() - ox) / s, (p.y() - oy) / s

    def _to_img(self, p) -> tuple[float, float]:
        x, y = self._to_img_raw(p)
        return min(max(x, 0), self._pm.width()), min(max(y, 0), self._pm.height())

    def _rect(self, b) -> QRectF:
        s, ox, oy = self._geom()
        return QRectF(ox + b[0] * s, oy + b[1] * s, (b[2] - b[0]) * s, (b[3] - b[1]) * s)

    # ------------------------------------------------------------ 그리기
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

        def frame(b, color, label="", dash=False, width=2, handles=False):
            r = self._rect(b)
            pen = QPen(QColor(color), width)
            if dash:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)
            if handles:  # 모서리 손잡이: 끌어 크기를 바꾼다
                p.setPen(QPen(QColor(color), 1))
                p.setBrush(QColor("white"))
                for cx, cy in ((r.left(), r.top()), (r.right(), r.top()), (r.left(), r.bottom()),
                               (r.right(), r.bottom())):
                    p.drawRect(QRectF(cx - 4, cy - 4, 8, 8))
                p.setBrush(Qt.BrushStyle.NoBrush)
            if label:
                fm = p.fontMetrics()
                tw, th = fm.horizontalAdvance(label) + 8, fm.height() + 2
                ty = r.top() - th if r.top() - th >= 0 else r.bottom()
                p.fillRect(QRectF(r.left(), ty, tw, th), QColor(color))
                p.setPen(QColor("white"))
                p.drawText(QRectF(r.left(), ty, tw, th), Qt.AlignmentFlag.AlignCenter, label)

        drawing = self.drawing()
        if not drawing:  # 새 자리를 그리거나 크기를 바꾸는 동안은 그 상자만 (다른 상자에 가려지지 않게)
            for b, label, *rest in self.marks:  # (상자, 이름[, 예상 위치인가[, 정해진 자리인가[, 목록에서 고름]]])
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
                frame(self.box, "#1F6FEB", "지금 자리", width=3 if not self.new else 2, handles=not self.new)
        if self.new:
            frame(self.new, "#D62828", "새 자리", width=3, handles=True)
        p.end()

    def focus_crop(self, size) -> QPixmap:
        """확대 칸에 보일 그림: 새 자리(없으면 지금 자리, 그것도 없으면 예상 위치)를 둘레 조금과 함께 잘라 size에 맞춘다
        (상자 테두리도 그림).
        보일 상자가 없으면 빈 QPixmap."""
        from PySide6.QtCore import QRect

        # 지금 자리가 없는 아이콘(위치 못 찾음)은 새 자리를 그리기 전에도 예상 위치를 보인다 (빈 칸이었다)
        b = self.new or self.box or self.guess
        if self._pm.isNull() or not b:
            return QPixmap()
        x0, y0, x1, y1 = (float(v) for v in b)
        # 보이는 자리(크기)는 고정: 고른 아이콘의 지금 자리(없으면 예상 위치, 그것도 없으면 처음 그린 새 자리) 둘레로 한 번
        # 정한다. 상자를 그리거나 크기를 바꿀 때마다 자리를 다시 정하면 확대 배율이 계속 바뀌어 테두리가 움직이는 것이
        # 보이지 않았다. 새 상자가 그 자리를 벗어날 때만 넓힌다. 새 상자를 지우면 지금 자리 기준으로 되돌린다
        if self.new is None:
            self._focus_area = None
        if self._focus_area is None:
            guess = self.guess
            if self.box is None and guess and self.new and (self.new[2] < guess[0] or self.new[0] > guess[2]
                                                           or self.new[3] < guess[1] or self.new[1] > guess[3]):
                guess = None  # 예상 위치 밖에 그리기 시작함: 예상이 틀렸다. 둘을 다 담느라 확대가 줄지 않게 새 자리로
            a0, b0, a1, b1 = (float(v) for v in (self.box or guess or b))
            side = max(a1 - a0, b1 - b0, 8.0)
            if self.box is None and guess:  # 예상 위치는 이미 아이콘보다 넉넉하다: 그 둘레 조금만 더
                side *= 0.75
            elif self.box is None:  # 예상 위치도 없음: 끌기 시작한 작은 상자 말고 넉넉한 크기로
                side = max(side, 0.25 * min(self._pm.width(), self._pm.height()))
            cx, cy, m = (a0 + a1) / 2, (b0 + b1) / 2, side
            self._focus_area = (cx - m, cy - m, cx + m, cy + m)
        fa = self._focus_area
        if x0 < fa[0] or y0 < fa[1] or x1 > fa[2] or y1 > fa[3]:
            m = 0.15 * max(x1 - x0, y1 - y0, 4.0)
            fa = self._focus_area = (min(fa[0], x0 - m), min(fa[1], y0 - m), max(fa[2], x1 + m), max(fa[3], y1 + m))
        r = QRect(int(fa[0]), int(fa[1]), int(fa[2] - fa[0]) + 1, int(fa[3] - fa[1]) + 1) & self._pm.rect()
        if r.isEmpty():
            return QPixmap()
        crop = self._pm.copy(r).scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
        k = crop.width() / max(1, r.width())
        p = QPainter(crop)
        pen = QPen(QColor("#D62828" if self.new else "#1F6FEB" if self.box else "#E67E22"), 2)  # 빨강 새 자리·파랑 지금 자리
        if not (self.new or self.box):  # 주황 점선 = 예상 위치 (큰 그림과 같은 표시)
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawRect(QRectF((x0 - r.x()) * k, (y0 - r.y()) * k, (x1 - x0) * k, (y1 - y0) * k))
        p.end()
        return crop

    def drawing(self) -> bool:
        """새 자리를 그리거나 상자 크기를 바꾸는 중 (실제로 끌기 시작한 뒤)."""
        return self._drag is not None and self._drag[0] in ("new", "resize") and self.new is not None

    # ------------------------------------------------------------ 마우스
    def _active_box(self):
        """테두리를 끌어 크기를 바꿀 상자: 새 자리가 있으면 그것, 없으면 지금 자리."""
        return self.new or self.box

    def _edges_at(self, pos) -> str:
        """pos(화면 점)가 크기를 바꿀 상자의 어느 변 위인가: "l"·"r"과 "t"·"b"의 조합 ("" = 아님)."""
        b = self._active_box()
        if not b or self._pm.isNull():
            return ""
        r = self._rect(b)
        x, y = pos.x(), pos.y()
        tx = max(2.0, min(EDGE_PX, r.width() / 3))  # 작은 상자: 안쪽은 새 상자를 그릴 수 있게 테두리 폭을 줄인다
        ty = max(2.0, min(EDGE_PX, r.height() / 3))
        if not (r.left() - tx <= x <= r.right() + tx and r.top() - ty <= y <= r.bottom() + ty):
            return ""
        e = ""
        dl, dr = abs(x - r.left()), abs(x - r.right())
        if min(dl, dr) <= tx:
            e += "l" if dl <= dr else "r"
        dt, db = abs(y - r.top()), abs(y - r.bottom())
        if min(dt, db) <= ty:
            e += "t" if dt <= db else "b"
        return e

    def _mark_at(self, pos) -> str:
        """pos에 있는 다른 아이콘 상자의 자리 id (여럿이면 가장 작은 상자, 없으면 "")."""
        x, y = self._to_img_raw(pos)
        hits = [((m[0][2] - m[0][0]) * (m[0][3] - m[0][1]), m[5]) for m in self.marks
                if len(m) > 5 and m[5] and m[0][0] <= x <= m[0][2] and m[0][1] <= y <= m[0][3]]
        return min(hits)[1] if hits else ""

    @staticmethod
    def _cursor_for(edges: str) -> Qt.CursorShape:
        if edges in ("lt", "rb"):
            return Qt.CursorShape.SizeFDiagCursor
        if edges in ("rt", "lb"):
            return Qt.CursorShape.SizeBDiagCursor
        if edges in ("l", "r"):
            return Qt.CursorShape.SizeHorCursor
        if edges in ("t", "b"):
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.CrossCursor

    def mousePressEvent(self, e):
        if self._pm.isNull() or self._drag is not None:  # 끄는 중 다른 단추는 무시
            return
        pos = e.position()
        if e.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            if self.zoom > 1.0:
                self._drag, self._drag_btn = ("pan", (pos.x(), pos.y()), list(self._pan)), e.button()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_btn = e.button()
        self._press = (pos.x(), pos.y())
        edges = self._edges_at(pos)
        if edges:
            self._drag = ("resize", edges, list(self._active_box()))
        else:
            self._drag = ("new", self._to_img(pos))

    def mouseMoveEvent(self, e):
        pos = e.position()
        d = self._drag
        if d is None:
            self.setCursor(self._cursor_for(self._edges_at(pos)))
            return
        if d[0] == "pan":
            s = self._base() * self.zoom
            w, h = self._pm.width() * s, self._pm.height() * s
            lim_x, lim_y = (w + self.width()) / 2 - 40, (h + self.height()) / 2 - 40  # 그림이 조금은 보이게
            self._pan = [max(-lim_x, min(lim_x, d[2][0] + pos.x() - d[1][0])),
                         max(-lim_y, min(lim_y, d[2][1] + pos.y() - d[1][1]))]
            self.update()
            return
        if self._press is None or abs(pos.x() - self._press[0]) + abs(pos.y() - self._press[1]) < 3:
            return  # 아직 끌지 않음 (누르기만 한 것과 가린다)
        x, y = self._to_img(pos)
        if d[0] == "new":
            x0, y0 = d[1]
            self.new = [min(x0, x), min(y0, y), max(x0, x), max(y0, y)]
        else:
            b = list(d[2])
            if "l" in d[1]:
                b[0] = x
            if "r" in d[1]:
                b[2] = x
            if "t" in d[1]:
                b[1] = y
            if "b" in d[1]:
                b[3] = y
            self.new = [min(b[0], b[2]), min(b[1], b[3]), max(b[0], b[2]), max(b[1], b[3])]
        self.update()
        self.box_moved.emit()

    def mouseReleaseEvent(self, e):
        if self._drag is None or e.button() != self._drag_btn:
            return
        d, self._drag, self._drag_btn = self._drag, None, None
        pos = e.position()
        if d[0] == "pan":
            self.setCursor(self._cursor_for(self._edges_at(pos)))
            return
        clicked = self._press is None or abs(pos.x() - self._press[0]) + abs(pos.y() - self._press[1]) < 3
        self._press = None
        if clicked:
            if d[0] == "resize":  # 테두리를 누르기만 함: 상자는 그대로 (바로 옆 다른 아이콘 상자를 눌렀으면 그것을 고른다)
                self.update()
                occ = self._mark_at(pos)
                if occ:
                    self.picked.emit(occ)
                return
            self.new = None  # 빈 곳·다른 아이콘을 누름: 새 자리를 지운다
            self.update()
            self.box_moved.emit()
            occ = self._mark_at(pos)
            if occ:
                self.picked.emit(occ)
            self.drawn.emit()
            return
        if self.new and (self.new[2] - self.new[0] < 4 or self.new[3] - self.new[1] < 4):
            self.new = None  # 너무 작음
        self.update()
        self.box_moved.emit()
        self.drawn.emit()

    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        if not dy or self._pm.isNull():
            return
        self.zoom_by(1.25 if dy > 0 else 0.8, e.position())
        e.accept()
