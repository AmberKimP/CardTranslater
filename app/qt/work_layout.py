"""카드 보기(2B) 배치를 다른 작업 창에서도 쓰는 공용 조각.

배치 규칙 (DESIGN.md ‘작업 창 배치’):
- 맨 위 줄 = 지금 보는 것 (제목 + 상태 칩 + 진행) · 오른쪽 끝에 보조 동작. 아래 단추 줄은 두지 않는다.
- 세 칸: 왼쪽 = 어디(카드 전체·목록·이동), 가운데 = 무엇(큰 그림), 오른쪽 = 어떻게(글·고치기 + 큰 주 버튼).
- 이동: ◀ 이전 · 다음 ▶ (Alt+←/→), ◀◀ 문제 · 문제 ▶▶ (Alt+Shift+←/→). 갈 곳이 없으면 알림과 소리.
- 주 버튼은 오른쪽 아래 하나 (Ctrl+Enter), 그 밑에 단축키 한 줄.
"""

from __future__ import annotations

import html as _html
from typing import Callable

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QFrame, QGridLayout, QLabel, QListWidget, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from app.qt.dialogs import PRIMARY_QSS

TODO_COLOR = "#AEAEB2"  # 상태를 모르는 영역 상자 (옅은 회색)
BIG_PRIMARY_QSS = PRIMARY_QSS.replace("min-height: 22px;", "min-height: 30px;")
LEFT_W = 240  # 왼쪽(이동) 칸 폭 (px)


def adopt(parent: QWidget, widgets) -> None:
    """위젯들을 이 순서로 parent의 자식으로 붙인다 (배치에 넣기 전에). 버튼 코드(ui_codes)는 위젯 나무의 순서라
    화면 자리를 옮겨도 예전 번호가 그대로이게 한다. 배치에 넣을 때는 부모가 같아 순서가 바뀌지 않는다."""
    for w in widgets:
        w.setParent(parent)


def panel(title: str, body: QWidget) -> QFrame:
    """흰 칸 (둥근 모서리·옅은 테두리) + 굵은 제목."""
    fr = QFrame()
    fr.setObjectName("panel")
    fr.setStyleSheet("QFrame#panel { background: white; border: 1px solid #E5E5EA; border-radius: 10px; }")
    lay = QVBoxLayout(fr)
    lay.setContentsMargins(10, 8, 10, 8)
    lay.setSpacing(6)
    if title:
        t = QLabel(title)
        t.setStyleSheet("font-weight: 600; color: #3A3A3C; border: none;")
        lay.addWidget(t)
    lay.addWidget(body, 1)
    return fr


def make_big_primary(b: QPushButton) -> QPushButton:
    """오른쪽 아래 큰 주 버튼 (카드 보기의 ‘이 영역 확정 → 다음 문제’와 같은 모양)."""
    b.setStyleSheet(BIG_PRIMARY_QSS)
    b.setAutoDefault(False)
    return b


def shortcut_hint(text: str) -> QLabel:
    """주 버튼·이동 단추 밑 단축키 한 줄 (좁으면 잘리고, 마우스를 올리면 다 보인다)."""
    lb = QLabel(text)
    lb.setStyleSheet("color: #8E8E93;")
    lb.setToolTip(text)
    lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lb.setWordWrap(True)
    lb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return lb


def chip_qss(bg: str, fg: str = "#1D1D1F") -> str:
    return f"background: {bg}; color: {fg}; border-radius: 9px; padding: 1px 9px;"


def fit_list_rows(lst: QListWidget, lo: int = 2, hi: int = 8) -> None:
    """목록 높이를 줄 수에 맞춘다 (lo~hi줄). 남는 높이는 같은 칸의 다른 것(그림)이 쓴다."""
    n = max(lo, min(hi, lst.count()))
    row = lst.sizeHintForRow(0) if lst.count() else 22
    lst.setFixedHeight(max(row, 18) * n + 2 * lst.frameWidth() + 4)


class FlashLabel(QLabel):
    """잠깐 보이는 알림 글 (8초 뒤 지워짐). 갈 곳이 없을 때 등."""

    COLORS = {"ok": "#2E8B57", "warn": "#B35C00", "info": "#6E6E73"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self.setText(""))

    def flash(self, text: str, kind: str = "info") -> None:
        color = self.COLORS.get(kind, self.COLORS["info"])
        self.setText(f'<span style="color:{color}; font-weight:600">{_html.escape(text)}</span>')
        self._timer.start(8000)


class NavBox:
    """왼쪽 칸 아래 이동 단추 묶음: 2열 격자. spec = [(kind, 글, 풍선 도움말, 단축키 | None)].
    go(kind)가 False를 돌려주면(갈 곳 없음) no_target[kind]를 알리고 소리를 낸다."""

    def __init__(self, owner: QWidget, spec: list[tuple], go: Callable[[str], bool], flash: FlashLabel,
                 no_target: dict[str, str]):
        self.owner, self._go_fn, self._flash, self._no_target = owner, go, flash, no_target
        self.buttons: dict[str, QPushButton] = {}
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(4)
        self.grid.setVerticalSpacing(4)
        for i, (kind, text, tip, keys) in enumerate(spec):
            b = QPushButton(text)
            b.setToolTip(tip + (f" ({keys})" if keys else ""))
            b.setAutoDefault(False)
            b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)  # 왼쪽 칸을 넓히지 않게
            b.setMinimumWidth(0)
            b.clicked.connect(lambda _c=False, k=kind: self.go(k))
            self.buttons[kind] = b
            self.grid.addWidget(b, i // 2, i % 2)
            if keys:
                QShortcut(QKeySequence(keys), owner, activated=lambda k=kind: self.go(k))

    def go(self, kind: str) -> None:
        if not self._go_fn(kind):
            self._flash.flash(self._no_target.get(kind, "갈 곳이 없습니다"), "warn")
            QApplication.beep()


class CardMap(QWidget):
    """카드 전체 그림 위에 영역 상자를 상태 색 테두리로 그린다. 고른 영역은 굵게 + 옅은 칠. 상자를 누르면 picked(영역 이름)."""

    picked = Signal(str)

    def __init__(self):
        super().__init__()
        self._pm = QPixmap()
        self._rects: dict[str, tuple] = {}
        self._colors: dict[str, str] = {}
        self._sel = ""
        self.setMinimumSize(220, 220)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("영역 상자를 누르면 그 영역으로 갑니다")

    def set_data(self, pm: QPixmap, rects: dict, colors: dict, selected: str) -> None:
        self._pm, self._rects, self._colors, self._sel = pm, dict(rects), dict(colors), selected
        self.update()

    def select(self, name: str, colors: dict | None = None) -> None:
        self._sel = name
        if colors is not None:
            self._colors = dict(colors)
        self.update()

    def _geom(self) -> tuple[float, float, float]:
        w, h = self._pm.width(), self._pm.height()
        if not w or not h:
            return 1.0, 0.0, 0.0
        s = min((self.width() - 8) / w, (self.height() - 8) / h)
        return s, (self.width() - w * s) / 2, (self.height() - h * s) / 2

    def _rect(self, r) -> QRectF:
        s, ox, oy = self._geom()
        return QRectF(ox + r[0] * s, oy + r[1] * s, (r[2] - r[0]) * s, (r[3] - r[1]) * s)

    def hit(self, pos) -> str:
        """누른 자리의 영역 이름 (겹치면 가장 작은 상자)."""
        best, area = "", 0.0
        for name, r in self._rects.items():
            qr = self._rect(r)
            if qr.contains(pos) and (not best or qr.width() * qr.height() < area):
                best, area = name, qr.width() * qr.height()
        return best

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#FAFAFA"))
        if self._pm.isNull():
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        s, ox, oy = self._geom()
        p.drawPixmap(QRectF(ox, oy, self._pm.width() * s, self._pm.height() * s), self._pm, QRectF(self._pm.rect()))
        for name, r in self._rects.items():
            color = QColor(self._colors.get(name, TODO_COLOR))
            mine = name == self._sel
            qr = self._rect(r)
            if mine:
                fill = QColor(color)
                fill.setAlpha(45)
                p.fillRect(qr, fill)
            p.setPen(QPen(color, 4 if mine else 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(qr)
        p.end()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            name = self.hit(e.position())
            if name:
                self.picked.emit(name)


def load_card_map(file_dir, page: int) -> dict:
    """카드 전체 그림과 영역 상자 {"pm": QPixmap, "rects": {이름: (x0, y0, x1, y1)}} (못 읽으면 빈 그림)."""
    from app import service

    info = {"pm": QPixmap(), "rects": {}}
    try:
        d = service.card_regions(file_dir, page)
        info["pm"].loadFromData(d["png"], "PNG")
        info["rects"] = {n: tuple(r) for n, r in d["regions"].items()}
    except Exception:  # noqa: BLE001 - 카드 그림은 보조
        pass
    return info
