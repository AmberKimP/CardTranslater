"""영역 지정 창의 ‘미리보기’: 지금 영역을 다른 카드 16장(4×4)에 겹쳐 보여 준다.

- 썸네일 그림은 한 번만 만들어 두고(파일·쪽마다), 영역을 고칠 때는 사각형·주의 표시만 다시 그린다.
- 주의: 영역 안의 줄이 밝은 줄과 어두운 줄로 섞여 있으면(적은 쪽이 WARN_SHARE 이상) 그림·테두리가 걸친 것으로 본다.
  글자 상자가 밝든 어둡든 상자 전체가 한 가지면 주의가 아니다.
- 카드를 누르면 신호 open_page(쪽). 오른쪽 메뉴: 이 카드 편집 / 이 카드만 따로 영역 / 공통 영역으로 되돌리기 / 빼기·되살리기.
- [뺌] 체크·메뉴는 신호 set_excluded(쪽, 뺄지).
화면만: 저장·영역 계산은 영역 지정 창(RegionEditor)이 한다.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from app import service
from app.qt.dialogs import grey_note, section_title

PER_PAGE = 16
COLS = 4
WARN_SHARE = 0.2  # 영역 안 줄 가운데 ‘다른 바탕’ 줄이 이만큼 넘으면 주의
DARK = 150  # 줄의 밝기 가운데값이 이보다 어두우면 어두운 줄
THUMB_DPI = 40
REGION_COLORS = [QColor(40, 120, 255), QColor(0, 160, 90), QColor(150, 60, 200), QColor(230, 120, 0),
                 QColor(0, 150, 170), QColor(200, 60, 120)]
PROTECT_COLOR = QColor(220, 40, 40)


def mixed_share(gray: np.ndarray, rect: dict) -> float:
    """영역(비율 rect) 안 줄 가운데 바탕이 다른 줄의 비율 (밝은 줄과 어두운 줄 중 적은 쪽). gray: 0~255 2차원."""
    h, w = gray.shape
    x0, y0 = max(0, int(rect["x"] * w)), max(0, int(rect["y"] * h))
    x1, y1 = min(w, int((rect["x"] + rect["w"]) * w)), min(h, int((rect["y"] + rect["h"]) * h))
    sub = gray[y0:y1, x0:x1]
    if sub.size == 0 or sub.shape[0] < 2:
        return 0.0
    dark = float((np.median(sub, axis=1) < DARK).mean())
    return min(dark, 1.0 - dark)


class CardThumb(QFrame):
    """썸네일 한 장: 그림 + 영역 사각형 + 쪽 번호·[뺌] + 주의 글."""

    clicked = Signal(int)
    menu_action = Signal(int, str)  # (쪽, 동작: open | override | drop_override | exclude | include)
    exclude_toggled = Signal(int, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.page = 0
        self.base: QPixmap | None = None
        self.gray: np.ndarray | None = None
        self.setObjectName("thumb")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 3)
        lay.setSpacing(2)
        self.pic = QLabel()
        self.pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pic.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        lay.addWidget(self.pic, 1)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.num = QLabel()
        row.addWidget(self.num)
        row.addStretch(1)
        self.ex = QCheckBox("뺌")
        self.ex.setToolTip("이 쪽 빼기 (그룹 형식과 다른 카드: 추출·번역·지우기·한글 넣기에서 모두 빠짐)")
        self.ex.toggled.connect(lambda on: self.exclude_toggled.emit(self.page, on))
        row.addWidget(self.ex)
        lay.addLayout(row)
        self.warn = QLabel(" ")
        self.warn.setStyleSheet("color: #C5221F; font-size: 9pt;")
        lay.addWidget(self.warn)
        self.override = False
        self.excluded = False
        self.warn_text = ""

    def set_card(self, page: int, base: QPixmap, gray: np.ndarray):
        self.page, self.base, self.gray = page, base, gray

    def render(self, regions: list[dict], current: bool, excluded: bool, override: bool, colors: dict):
        """영역(비율)을 그림 위에 그리고 주의·테두리를 정한다."""
        self.override, self.excluded = override, excluded
        if self.base is None:
            return
        avail = max(60, self.pic.width() - 2)
        pm = self.base.scaledToWidth(avail, Qt.TransformationMode.SmoothTransformation)
        if pm.height() > max(40, self.pic.height() - 2):
            pm = self.base.scaledToHeight(max(40, self.pic.height() - 2), Qt.TransformationMode.SmoothTransformation)
        w, h = pm.width(), pm.height()
        warns = []
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for r in regions:
            rc = r["rect"]
            c = PROTECT_COLOR if r.get("kind") == "protect" else colors.get(r.get("name", ""), REGION_COLORS[0])
            fill = QColor(c)
            fill.setAlpha(40)
            p.setPen(QPen(c, 2 if r.get("kind") != "protect" else 1, Qt.PenStyle.SolidLine
                          if r.get("kind") != "protect" else Qt.PenStyle.DashLine))
            p.setBrush(fill)
            p.drawRect(QRectF(rc["x"] * w, rc["y"] * h, rc["w"] * w, rc["h"] * h))
            if r.get("kind") != "protect" and self.gray is not None:
                share = mixed_share(self.gray, rc)
                if share >= WARN_SHARE:
                    warns.append(f"{r.get('name') or '영역'} 그림 {share:.0%}")
        if excluded:
            p.fillRect(pm.rect(), QColor(255, 255, 255, 175))
            p.setPen(QColor(110, 110, 110))
            p.setFont(QFont(self.font().family(), 13, QFont.Weight.Bold))
            p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "뺌")
        p.end()
        self.pic.setPixmap(pm)
        self.warn_text = "" if excluded else ("주의: " + ", ".join(warns) if warns else "")
        self.warn.setText(self.warn_text or " ")
        self.warn.setToolTip("영역 안에 글자 상자 말고 그림·테두리가 걸쳐 있습니다. 영역을 상자에 맞추거나 "
                             "이 카드만 따로 영역을 정하세요" if warns else "")
        tag = " · 지금 카드" if current else ""
        tag += " · 따로" if override else ""
        self.num.setText(f"<b>{self.page}쪽</b>{tag}")
        self.num.setStyleSheet("color: #1E6FD9;" if current else "color: #1F2933;")
        self.ex.blockSignals(True)
        self.ex.setChecked(excluded)
        self.ex.blockSignals(False)
        border, width = "#D0D5DD", 1
        if current:
            border, width = "#1E6FD9", 3
        elif self.warn_text:
            border, width = "#D93025", 3
        self.setStyleSheet(f"QFrame#thumb {{ border: {width}px solid {border}; border-radius: 4px; background: white; }}")

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.page:
            self.clicked.emit(self.page)
        super().mouseReleaseEvent(ev)

    def contextMenuEvent(self, ev):
        if not self.page:
            return
        m = QMenu(self)
        acts = [("이 카드 편집", "open")]
        if self.override:
            acts.append(("공통 영역으로 되돌리기", "drop_override"))
        else:
            acts.append(("이 카드만 따로 영역", "override"))
        acts.append(("되살리기 (빼지 않음)", "include") if self.excluded else ("이 쪽 빼기", "exclude"))
        for text, key in acts:
            a = QAction(text, m)
            a.triggered.connect(lambda _c=False, k=key: self.menu_action.emit(self.page, k))
            m.addAction(a)
        m.exec(ev.globalPos())


class PreviewPanel(QWidget):
    """4×4 미리보기. regions_for(쪽) -> [영역 dict(비율 rect, name, kind)], info_for(쪽) -> (지금 카드?, 뺐나?, 따로 영역?)."""

    open_page = Signal(int)
    card_action = Signal(int, str)
    set_excluded = Signal(int, bool)

    def __init__(self, regions_for: Callable[[int], list[dict]], info_for: Callable[[int], tuple[bool, bool, bool]],
                 parent=None):
        super().__init__(parent)
        self.regions_for, self.info_for = regions_for, info_for
        self.file_dir = None
        self.pages: list[int] = []
        self.start = 0
        self._cache: dict = {}  # (파일, 쪽) -> (QPixmap, gray)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(60)  # 끄는 동안 너무 자주 그리지 않게
        self._timer.timeout.connect(self.refresh)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 0, 4, 0)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(section_title("미리보기 — 다른 카드에서도 맞나요?"))
        head.addStretch(1)
        self.only_warn = QCheckBox("주의만 보기")
        self.only_warn.setToolTip("영역 안에 그림·테두리가 걸친 카드만 모아 봅니다")
        self.only_warn.toggled.connect(self._on_only_warn)
        head.addWidget(self.only_warn)
        lay.addLayout(head)
        self.legend = grey_note("")
        self.legend.setWordWrap(True)
        lay.addWidget(self.legend)
        self.grid = QGridLayout()
        self.grid.setSpacing(6)
        self.thumbs: list[CardThumb] = []
        for i in range(PER_PAGE):
            t = CardThumb(self)
            t.clicked.connect(self.open_page)
            t.menu_action.connect(self._on_menu)
            t.exclude_toggled.connect(self.set_excluded)
            self.grid.addWidget(t, i // COLS, i % COLS)
            self.thumbs.append(t)
        for r in range(PER_PAGE // COLS):
            self.grid.setRowStretch(r, 1)
        for c in range(COLS):
            self.grid.setColumnStretch(c, 1)
        lay.addLayout(self.grid, 1)
        nav = QHBoxLayout()
        self.prev = QPushButton("◀ 이전 16장")
        self.prev.clicked.connect(lambda: self.step(-1))
        self.next = QPushButton("다음 16장 ▶")
        self.next.clicked.connect(lambda: self.step(1))
        self.range_label = QLabel()
        nav.addWidget(self.prev)
        nav.addStretch(1)
        nav.addWidget(self.range_label)
        nav.addStretch(1)
        nav.addWidget(self.next)
        lay.addLayout(nav)

    # ---- 자료
    def set_file(self, file_dir, pages: list[int], current: int | None = None):
        """보여 줄 파일과 쪽 (뺀 쪽도 넣는다: 되살릴 수 있게)."""
        self.file_dir, self.pages = file_dir, list(pages)
        if current is not None:
            self.show_page(current, refresh=False)
        self.refresh()

    def _card(self, page: int):
        key = (str(self.file_dir), page)
        if key not in self._cache:
            png, _ = service.page_image(self.file_dir, page, dpi=THUMB_DPI)
            img = QImage.fromData(png)
            g = img.convertToFormat(QImage.Format.Format_Grayscale8)
            arr = np.frombuffer(g.constBits(), np.uint8, g.bytesPerLine() * g.height()).reshape(
                g.height(), g.bytesPerLine())[:, :g.width()].copy()
            self._cache[key] = (QPixmap.fromImage(img), arr)
        return self._cache[key]

    def warn_pages(self) -> list[int]:
        """주의 카드 (뺀 쪽 제외)."""
        out = []
        for p in self.pages:
            _cur, excluded, _ov = self.info_for(p)
            if excluded:
                continue
            _pm, gray = self._card(p)
            if any(r.get("kind") != "protect" and mixed_share(gray, r["rect"]) >= WARN_SHARE
                   for r in self.regions_for(p)):
                out.append(p)
        return out

    def _visible_list(self) -> list[int]:
        return self.warn_pages() if self.only_warn.isChecked() else self.pages

    def show_page(self, page: int, refresh: bool = True):
        """그 쪽이 들어 있는 16장으로 넘긴다 (이미 보이면 그대로)."""
        vis = self._visible_list()
        if page in vis:
            i = vis.index(page)
            if not self.start <= i < self.start + PER_PAGE:
                self.start = (i // PER_PAGE) * PER_PAGE
        if refresh:
            self.refresh()

    def step(self, d: int):
        n = len(self._visible_list())
        self.start = max(0, min(self.start + d * PER_PAGE, max(0, (n - 1) // PER_PAGE * PER_PAGE)))
        self.refresh()

    def schedule(self):
        """영역을 고치는 동안: 60ms마다 한 번만 다시 그린다 (끄는 동안에도 따라오게)."""
        if self.isVisible() and not self._timer.isActive():
            self._timer.start()

    def _on_only_warn(self, _on: bool):
        self.start = 0
        self.refresh()

    def _on_menu(self, page: int, act: str):
        if act == "open":
            self.open_page.emit(page)
        elif act in ("exclude", "include"):
            self.set_excluded.emit(page, act == "exclude")
        else:
            self.card_action.emit(page, act)

    # ---- 그리기
    def refresh(self):
        if self.file_dir is None:
            return
        vis = self._visible_list()
        if self.start >= len(vis):
            self.start = max(0, (len(vis) - 1) // PER_PAGE * PER_PAGE)
        show = vis[self.start:self.start + PER_PAGE]
        names: list[str] = []
        for p in show:
            for r in self.regions_for(p):
                if r.get("kind") != "protect" and r.get("name") and r["name"] not in names:
                    names.append(r["name"])
        colors = {n: REGION_COLORS[i % len(REGION_COLORS)] for i, n in enumerate(names)}
        for i, t in enumerate(self.thumbs):
            if i < len(show):
                p = show[i]
                pm, gray = self._card(p)
                t.set_card(p, pm, gray)
                cur, excluded, override = self.info_for(p)
                t.render(self.regions_for(p), cur, excluded, override, colors)
                t.setVisible(True)
            else:
                t.page = 0
                t.setVisible(False)
        legend = " ".join(f"<span style='color:{colors[n].name()}'>■</span> {n}" for n in names)
        self.legend.setText(legend + " · 왼쪽에서 영역을 고치면 바로 반영 · 카드를 누르면 왼쪽에서 편집 · "
                            "오른쪽 버튼: 이 카드만 따로 영역·빼기 · 빨간 테두리 = 영역에 그림·테두리가 걸침")
        if self.only_warn.isChecked():
            self.range_label.setText(f"<b>주의 카드 {len(vis)}장</b> (전체 {len(self.pages)}장 중)"
                                     + (f" · {self.start + 1}–{self.start + len(show)}" if len(vis) > PER_PAGE else ""))
        else:
            self.range_label.setText(f"<b>{show[0]}–{show[-1]}쪽 / {len(self.pages)}쪽</b>" if show else "카드 없음")
        self.prev.setEnabled(self.start > 0)
        self.next.setEnabled(self.start + PER_PAGE < len(vis))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.schedule()
