"""② 원문 확인 창이 쓰는 아이콘 사전 탭(IconDictionaryPanel)과 휠로 바뀌지 않는 숫자 칸."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QHeaderView, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from app.qt.dialogs import grey_note, subtitle


class NoWheelSpinBox(QDoubleSpinBox):
    """표 안의 ‘표시 크기’ 칸: 마우스 휠로 표를 내리다 지나가며 값이 바뀌지 않게, 포커스가 있을 때(눌러서 고르는 중)만
    휠로 바꾼다. 포커스가 없으면 휠은 표로 넘겨 표가 스크롤된다."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # 휠만으로는 포커스를 가져가지 않는다

    def wheelEvent(self, e):
        if not self.hasFocus():
            e.ignore()  # 부모(표 화면)로 넘어가 표가 스크롤된다
            return
        super().wheelEvent(e)


# ================================================================ 아이콘 사전 (표)
class IconDictionaryPanel(QWidget):
    """게임 아이콘 사전 표: 한국어 이름·뜻·표시 크기를 고치고, ‘다시 정하기’로 원문에서 [[?]]로 되돌린다.
    아이콘 사전 창(새로 정할 아이콘이 없을 때)과 ② 원문 확인 창의 ‘사전 전체’ 탭이 함께 쓴다.
    allow_reset=False: 새 아이콘을 정하는 중에는 ‘다시 정하기’를 막는다 (되돌리면 찾은 결과가 맞지 않게 됨)."""

    COLS = ["그림", "이름", "한국어 이름", "뜻(원문)", "표시 크기", "쓰인 곳", "다시 정하기"]
    UNUSED = "지금 프로젝트에서는 안 쓰임 (다음 프로젝트에서 자동으로 알아봄)"

    def __init__(self, rows: list[dict], intro: str = "", parent=None, allow_reset: bool = True):
        super().__init__(parent)
        self.dirty = False  # 고친 것이 있음 (저장 전)
        # 지금 프로젝트에서 안 쓰인 아이콘은 아래로 (같은 무리 안에서는 받은 순서 그대로). rows()가 이 순서를 따른다
        self._rows = rows = sorted(rows, key=lambda r: not r.get("uses"))
        lab = subtitle(intro)
        lab.setVisible(bool(intro))
        reset_note = grey_note("‘다시 정하기’를 체크하면 그 아이콘을 원문에서 되돌리고, 저장한 뒤 다시 찾아 이름을 정합니다."
                               if allow_reset else
                               "사전은 게임 단위라 고치면 같은 게임의 다른 프로젝트에도 쓰입니다. "
                               "‘다시 정하기’는 새 아이콘 이름을 확정한 뒤 아이콘 사전에서 할 수 있습니다.")
        self.table = QTableWidget(len(rows), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setStyleSheet("QDoubleSpinBox { margin: 7px 6px; padding-top: 1px; padding-bottom: 1px; }"
                                 " QCheckBox { margin-left: 12px; }")
        self.scales: list[QDoubleSpinBox] = []
        self.resets: list[QCheckBox] = []
        for i, r in enumerate(rows):
            unused = not r.get("uses")
            img = QTableWidgetItem()
            if r.get("image"):
                pm = QPixmap(r["image"])
                if not pm.isNull():
                    pm = pm.scaledToHeight(40, Qt.TransformationMode.SmoothTransformation)
                    img.setData(Qt.ItemDataRole.DecorationRole, self._faded(pm) if unused else pm)
            img.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(i, 0, img)
            name = QTableWidgetItem(r["name"])
            name.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(i, 1, name)
            self.table.setItem(i, 2, QTableWidgetItem(r.get("ko", "")))
            self.table.setItem(i, 3, QTableWidgetItem(r.get("meaning", "")))
            sp = NoWheelSpinBox()
            sp.setRange(0.3, 4.0)  # ② 정의 줄(0.3~)과 같게: 작게 정한 크기가 저장할 때 바뀌지 않게
            sp.setSingleStep(0.1)
            sp.setValue(float(r.get("scale") or 1.1))
            sp.setToolTip("한글 글자 높이 대비 아이콘 높이")
            self.table.setCellWidget(i, 4, sp)
            self.scales.append(sp)
            uses = QTableWidgetItem(f"{r.get('uses', 0)}곳" + (f" · {self.UNUSED}" if unused else ""))
            uses.setFlags(Qt.ItemFlag.ItemIsEnabled)
            if unused:
                uses.setToolTip(self.UNUSED)
            self.table.setItem(i, 5, uses)
            if unused:  # 흐리게 (고치기는 그대로 된다)
                for j in (1, 2, 3, 5):
                    self.table.item(i, j).setForeground(QColor("#999"))
            cb = QCheckBox()
            cb.setToolTip("원문의 이 아이콘을 [[?]]로 되돌리고 사전에서 뺍니다. 다시 찾아 새로 묶고 이름을 정합니다")
            cb.setEnabled(allow_reset)
            cb.toggled.connect(self._touch)
            sp.valueChanged.connect(self._touch)
            self.table.setCellWidget(i, 6, cb)
            self.resets.append(cb)
            self.table.setRowHeight(i, 48)
        for i in range(len(rows)):
            self._paint_ko(i)
        self.dirty = False
        self.table.itemChanged.connect(self._item_changed)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(lab)
        lay.addWidget(reset_note)
        lay.addWidget(self.table, 1)

    def _touch(self, *_a):
        self.dirty = True

    def _item_changed(self, it):
        if it.column() == 2:
            self._paint_ko(it.row())
        self.dirty = True

    @staticmethod
    def _faded(pm: QPixmap) -> QPixmap:
        """안 쓰인 아이콘 그림: 흐리게."""
        out = QPixmap(pm.size())
        out.setDevicePixelRatio(pm.devicePixelRatio())
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setOpacity(0.35)
        p.drawPixmap(0, 0, pm)
        p.end()
        return out

    def _paint_ko(self, i: int):
        """한국어 이름이 빈 줄은 노랑 (입력하면 지움)."""
        it = self.table.item(i, 2)
        if it is None:
            return
        self.table.blockSignals(True)
        it.setBackground(QBrush(QColor("#FFE066")) if not it.text().strip() else QBrush())
        self.table.blockSignals(False)

    def rows(self) -> list[dict]:
        return [{"name": r["name"], "ko": self.table.item(i, 2).text().strip(),
                 "meaning": self.table.item(i, 3).text().strip(), "scale": self.scales[i].value(),
                 "reset": self.resets[i].isChecked()} for i, r in enumerate(self._rows)]
