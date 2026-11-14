"""수동으로 영역 지정: 이미지 한 장에 카드가 불규칙하게 놓였을 때, 카드 자리를 사람이 직접 그린다.

이미지 위를 끌어 카드마다 사각형을 그린다 (영역 지정 창과 같은 조작: 끌어서 옮기기·가장자리로 크기 바꾸기·Delete로 지우기).
카드 순서(= PDF 쪽 순서)는 위 줄부터, 줄 안에서는 왼쪽부터 자동으로 정한다.
자리를 그리지 않은 이미지는 앞 이미지(자리를 그린 마지막 이미지)와 같은 자리를 쓴다 (같은 배치로 스캔한 여러 장).
화면은 app.service만 부른다.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFont, QPen, QPixmap
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QGraphicsItem, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QVBoxLayout)

from app import service
from app.qt.region_editor import PageScene, PageView, RegionItem
from app.qt.dialogs import fit_to_screen, grey_note, page_title

DISPLAY_PX = 1600  # 화면에 보여 줄 그림의 긴 변 (좌표는 원본 px로 바꿔 저장한다)


class CardBox(RegionItem):
    """카드 자리 상자: 번호를 상자 안 왼쪽 위에 크게(확대·축소와 상관없는 크기), 앞 이미지에서 빌려 온 자리는 점선."""

    inherited = False

    def refresh(self) -> None:
        super().refresh()
        lab = getattr(self, "label", None)
        if lab is None:
            return
        lab.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        lab.setFont(QFont("Malgun Gothic", 16, QFont.Weight.Bold))
        r = self.rect()
        lab.setPos(r.left() + 3, r.top() + 2)
        if self.inherited:
            pen = QPen(self.pen())
            pen.setStyle(Qt.PenStyle.DashLine)
            self.setPen(pen)


class CardBoxesDialog(QDialog):
    """images: 이미지 경로들. boxes: 예전에 그린 것 {경로: [(x0, y0, x1, y1) 원본 px]}.
    확인하면 result()가 {경로: [상자…]}를 돌려준다 (자리를 그리지 않은 이미지는 앞 이미지 자리를 채워서)."""

    def __init__(self, images: list[str], boxes: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("카드 자리 직접 그리기")
        fit_to_screen(self, 1100, 760)
        self.images = [str(p) for p in images]
        self.boxes: dict[str, list] = {k: [tuple(b) for b in v] for k, v in (boxes or {}).items() if v}
        self.cur: str | None = None
        self.scale = 1.0
        self.size = (0, 0)
        self._inherited: list = []  # 지금 이미지에 보여 주는 앞 이미지의 자리 (점선, 고치면 이 이미지 것이 된다)

        self.list = QListWidget()
        self.list.setMaximumWidth(220)
        for p in self.images:
            it = QListWidgetItem(Path(p).name)
            it.setData(Qt.ItemDataRole.UserRole, p)
            it.setToolTip(p)
            self.list.addItem(it)
        self.list.currentItemChanged.connect(lambda cur, _prev: self._show(cur.data(Qt.ItemDataRole.UserRole))
                                             if cur else None)
        self.list.setVisible(len(self.images) > 1)

        self.scene = PageScene(self)
        self.scene.add_kind = "text"  # 늘 그리기 모드: 빈 곳을 끌면 새 카드 자리
        self.scene.item_class = CardBox
        self.scene.created_item.connect(lambda _it: self._changed())
        self.scene.changed_item.connect(lambda _it: self._changed())
        self.view = PageView(self.scene)
        self.view.delete_pressed.connect(self.delete_selected)
        self.view.key_action.connect(lambda a: self.view.fit_again() if a == "fit" else None)

        self.info = QLabel()
        self.info.setStyleSheet("color: #3A3A3C")
        b_del = QPushButton("고른 자리 지우기")
        b_del.clicked.connect(self.delete_selected)
        b_clear = QPushButton("이 이미지 자리 모두 지우기")
        b_clear.clicked.connect(self.clear_current)
        self.b_copy = QPushButton("모든 이미지에 이 자리 쓰기")
        self.b_copy.setToolTip("지금 이미지의 카드 자리를 다른 모든 이미지에도 똑같이 씁니다 (각 이미지에서 따로 고친 것은 덮어씀)")
        self.b_copy.clicked.connect(self.copy_to_all)
        self.b_copy.setVisible(len(self.images) > 1)
        b_fit = QPushButton("화면에 맞춤 (F)")
        b_fit.clicked.connect(self.view.fit_again)
        tools = QHBoxLayout()
        for b in (b_del, b_clear, self.b_copy, b_fit):
            tools.addWidget(b)
        tools.addStretch(1)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("이 자리로 자르기")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)

        mid = QHBoxLayout()
        mid.addWidget(self.list)
        mid.addWidget(self.view, 1)
        lay = QVBoxLayout(self)
        lay.addWidget(page_title("카드 자리 직접 그리기", 14))
        lay.addWidget(grey_note(
            "이미지 위를 끌어 카드마다 사각형을 그리세요. 상자를 끌면 옮기고, 가장자리를 끌면 크기가 바뀝니다. "
            "Delete로 지웁니다. 카드 순서는 위 줄부터, 줄 안에서는 왼쪽부터 정합니다. "
            "자리를 그리지 않은 이미지는 앞 이미지와 같은 자리(점선)를 씁니다."))
        lay.addLayout(tools)
        lay.addLayout(mid, 1)
        lay.addWidget(self.info)
        lay.addWidget(bb)
        if self.images:
            self.list.setCurrentRow(0)
            if self.cur is None:
                self._show(self.images[0])

    # ---- 이미지 바꾸기
    def _inherit_for(self, path: str) -> list:
        """path에 자리가 없을 때 쓸 자리: 앞쪽 이미지 중 자리를 그린 마지막 것 (없으면 뒤쪽 첫 것)."""
        i = self.images.index(path)
        for p in reversed(self.images[:i]):
            if self.boxes.get(p):
                return self.boxes[p]
        return next((self.boxes[p] for p in self.images[i + 1:] if self.boxes.get(p)), [])

    def _show(self, path: str) -> None:
        if path == self.cur:
            return
        self._store()
        self.cur = path
        self.scene.clear()
        png, self.size, self.scale = service.image_display(path, DISPLAY_PX)
        pm = QPixmap()
        pm.loadFromData(png, "PNG")
        self.scene.addPixmap(pm)
        self.scene.page_rect = QRectF(0, 0, pm.width(), pm.height())
        self.scene.setSceneRect(self.scene.page_rect)
        own = self.boxes.get(path)  # [] = 이 이미지에는 카드 없음 (사람이 지움)
        self._inherited = [] if path in self.boxes else list(self._inherit_for(path))
        for b in own if path in self.boxes else self._inherited:
            self._add_item(b, inherited=path not in self.boxes)
        self._renumber()
        self.view.fit_again()

    def _add_item(self, b, inherited: bool = False) -> CardBox:
        s = self.scale
        it = CardBox(QRectF(b[0] * s, b[1] * s, (b[2] - b[0]) * s, (b[3] - b[1]) * s), {"name": "", "kind": "text"})
        it.inherited = inherited
        self.scene.addItem(it)
        it.refresh()
        return it

    def _items(self) -> list[RegionItem]:
        return [i for i in self.scene.items() if isinstance(i, RegionItem)]

    def _current_boxes(self) -> list:
        s = self.scale or 1.0
        out = []
        for it in self._items():
            r = it.rect()
            out.append((r.left() / s, r.top() / s, r.right() / s, r.bottom() / s))
        return service.card_reading_order(out)

    def _store(self, edited: bool = False) -> None:
        """지금 이미지의 자리를 저장한다. 앞 이미지 자리를 그대로 보여 주기만 했으면 저장하지 않는다.
        edited: 사람이 고쳤다 (모두 지웠으면 ‘이 이미지에는 카드 없음’([])으로 남긴다)."""
        if self.cur is None:
            return
        items = self._items()
        if items and all(getattr(i, "inherited", False) for i in items):
            return
        cur = self._current_boxes()
        if cur or edited or self.cur in self.boxes:
            self.boxes[self.cur] = cur

    # ---- 고치기
    def _changed(self) -> None:
        inherited = [i for i in self._items() if getattr(i, "inherited", False)]
        if inherited and self._current_boxes() == service.card_reading_order(self._inherited):
            return  # 누르기만 하고 옮기지 않았다: 앞 이미지 자리를 계속 따른다
        for it in inherited:  # 하나라도 고치면 이 이미지만의 자리가 된다
            it.inherited = False
            it.refresh()
        self._store(edited=True)
        self._renumber()

    def _renumber(self) -> None:
        """카드 번호 = 자를 순서 (위 줄부터, 왼쪽부터)."""
        s = self.scale or 1.0
        order = self._current_boxes()
        for it in self._items():
            r = it.rect()
            b = tuple(int(round(v)) for v in (r.left() / s, r.top() / s, r.right() / s, r.bottom() / s))
            n = order.index(b) + 1 if b in order else 0
            it.data["name"] = str(n) if n else ""
            it.refresh()
        total = sum(len(self.boxes[p] if p in self.boxes else self._inherit_for(p)) for p in self.images)
        mine = bool(self.cur) and self.cur in self.boxes
        here = len(self.boxes[self.cur]) if mine else len(self._inherited)
        tag = "" if mine or not self._inherited else " (앞 이미지와 같은 자리)"
        if mine and not here:
            tag = " (이 이미지에서는 카드를 만들지 않음)"
        self.info.setText(f"이 이미지: 카드 {here}장{tag} · 모든 이미지: 카드 {total}장 (이미지 {len(self.images)}장)")

    def delete_selected(self) -> None:
        sel = [i for i in self.scene.selectedItems() if isinstance(i, RegionItem)]
        if not sel:
            return
        for it in sel:
            self.scene.removeItem(it)
        for it in self._items():  # 지우는 것도 고친 것: 남은 자리는 이 이미지 것
            it.inherited = False
            it.refresh()
        self._store(edited=True)
        self._renumber()

    def clear_current(self) -> None:
        for it in self._items():
            self.scene.removeItem(it)
        self._inherited = []
        if self.cur:
            self.boxes[self.cur] = []  # 이 이미지에는 카드 없음 (앞 이미지 자리도 쓰지 않는다)
        self._renumber()

    def copy_to_all(self) -> None:
        self._store()
        cur = self.boxes.get(self.cur) if self.cur else None
        if not cur:
            return
        for p in self.images:
            self.boxes[p] = list(cur)
        self._renumber()

    # ---- 결과
    def result(self) -> dict:
        """{이미지 경로: [상자 (원본 px)]} — 자리를 그리지 않은 이미지는 앞 이미지 자리로 채운다.
        빈 목록 = 그 이미지에서는 카드를 만들지 않음."""
        self._store()
        out = {}
        for p in self.images:
            b = self.boxes[p] if p in self.boxes else self._inherit_for(p)
            if b or p in self.boxes:
                out[p] = [tuple(int(round(v)) for v in x) for x in b]
        return out

    def accept(self) -> None:
        self._store()
        if not any(self.result().values()):
            self.info.setText("카드 자리를 하나 이상 그리세요 (이미지 위를 끌어 사각형).")
            self.info.setStyleSheet("color: #B00020")
            return
        super().accept()
