"""자를 자리 미리 보기: 이미지 시트를 배열로 자를 자리(빨간 상자)와 예상 카드 크기를 보여 주고,
카드 크기를 바꾸면 그 크기의 카드로 다시 찾는다. 파일 넣기의 ‘자를 자리 미리 보기’와 목록의 ‘다시 자르기…’가 함께 쓴다."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QDoubleSpinBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from app import service
from app.qt.dialogs import PRIMARY_QSS, show_problem


class CutPreviewDialog(QDialog):
    """path(이미지 한 장)를 cols×rows로 자를 자리. card_mm: 처음 쓸 카드 크기 (가로, 세로) mm (None = 자동으로 찾음).
    확인하면 chosen_mm(): 정한 카드 크기 (자동으로 찾은 그대로면 None)."""

    def __init__(self, path: str | Path, cols: int, rows: int, card_mm: tuple[float, float] | None = None,
                 parent=None, ok_text: str = "이대로 자르기"):
        super().__init__(parent)
        self.path, self.cols, self.rows = str(path), cols, rows
        self._mm = tuple(card_mm) if card_mm else None
        self._dpi = 300.0
        self.setWindowTitle(f"자를 자리 — {Path(path).name}")
        self.pic = QLabel()
        self.pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.w_mm, self.h_mm = QDoubleSpinBox(), QDoubleSpinBox()
        for sp in (self.w_mm, self.h_mm):
            sp.setRange(5.0, 1000.0)
            sp.setDecimals(1)
            sp.setSingleStep(0.5)
            sp.setSuffix(" mm")
            sp.valueChanged.connect(lambda _v: self._sync_px())
        self.px = QLabel()
        self.px.setStyleSheet("color: #6E6E73")
        self.b_find = QPushButton("이 크기로 다시 찾기")
        self.b_find.setToolTip("카드 크기를 이 값으로 정하고 그 크기의 카드를 다시 찾습니다 "
                               "(붙어 있는 카드는 그 간격으로, 틈이 있는 카드는 가운데를 그대로 두고 크기만)")
        self.b_find.clicked.connect(self.find_with_size)
        self.b_auto = QPushButton("자동으로 찾기")
        self.b_auto.setToolTip("정한 카드 크기를 버리고 처음처럼 카드 크기와 자리를 자동으로 찾습니다")
        self.b_auto.clicked.connect(self.find_auto)
        self._size_labels = (QLabel("예상 카드 크기  가로"), QLabel("× 세로"))
        size_row = QHBoxLayout()
        size_row.addWidget(self._size_labels[0])
        size_row.addWidget(self.w_mm)
        size_row.addWidget(self._size_labels[1])
        size_row.addWidget(self.h_mm)
        size_row.addWidget(self.px)
        size_row.addStretch(1)
        size_row.addWidget(self.b_find)
        size_row.addWidget(self.b_auto)
        self.ok = QPushButton(ok_text)
        self.ok.setStyleSheet(PRIMARY_QSS)
        self.ok.clicked.connect(self.accept)
        cancel = QPushButton("취소")
        cancel.clicked.connect(self.reject)
        for b in (self.b_find, self.b_auto, self.ok, cancel):
            b.setAutoDefault(False)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(self.ok)
        btns.addWidget(cancel)
        lay = QVBoxLayout(self)
        lay.addWidget(self.pic, 1)
        lay.addWidget(self.note)
        lay.addLayout(size_row)
        lay.addLayout(btns)
        self.ok_cards = 0
        self.cancel = cancel
        self._shown: tuple[float, float] | None = None  # 지금 미리 보기에 쓴 크기 칸 값 (고쳤는지 보려고)
        self._load(self._mm)

    # ---- 자르기
    def _load(self, mm: tuple[float, float] | None) -> bool:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            png, info = service.image_preview(self.path, self.cols, self.rows, card_mm=mm)
        except Exception as e:  # noqa: BLE001 - 그림을 못 열면 알리고 창은 그대로
            QApplication.restoreOverrideCursor()
            show_problem(self, "자를 자리를 미리 볼 수 없습니다", str(e),
                         "그림 파일이 열리는지 확인하고, 가로·세로 장수나 카드 크기를 바꿔 다시 해 보세요.")
            self._set_spins(self._shown)  # 칸은 지금 미리 보기의 크기로 되돌린다
            return False
        QApplication.restoreOverrideCursor()
        self._mm = tuple(mm) if mm else None
        self._dpi = float(info.get("dpi") or 300.0)
        pm = QPixmap()
        pm.loadFromData(png, "PNG")
        self.pic.setPixmap(pm)
        self.ok_cards = info["cards"]
        how = (f"정한 크기 {self._mm[0]:.1f}×{self._mm[1]:.1f}mm로 찾았습니다." if self._mm
               else "카드 크기와 자리를 자동으로 찾았습니다.")
        msg = f"카드 {info['cards']}장으로 자릅니다 (빨간 상자). {how}"
        if info["warnings"]:
            msg += "\n" + "\n".join(info["warnings"])
        if not self._mm:
            msg += "\n카드 끝이 맞지 않으면 아래에 실제 카드 크기를 넣고 ‘이 크기로 다시 찾기’를 누르세요."
        self.note.setText(msg)
        self._set_spins(self._mm or info.get("card_mm"))
        self._sync_px()
        self.b_auto.setEnabled(self._mm is not None)
        return True

    def _set_spins(self, size) -> None:
        if size:
            for sp, v in ((self.w_mm, size[0]), (self.h_mm, size[1])):
                sp.blockSignals(True)
                sp.setValue(float(v))
                sp.blockSignals(False)
        self._shown = (round(self.w_mm.value(), 1), round(self.h_mm.value(), 1))

    def _typed(self) -> bool:
        """크기 칸을 고치고 아직 ‘이 크기로 다시 찾기’를 누르지 않았나."""
        return self._shown is not None and (round(self.w_mm.value(), 1), round(self.h_mm.value(), 1)) != self._shown

    def accept(self):
        """크기 칸을 고친 채 확인을 누르면 그 크기로 먼저 다시 찾는다 (못 찾으면 창에 남는다)."""
        if self.w_mm.isVisible() and self._typed():
            if not self._load((round(self.w_mm.value(), 1), round(self.h_mm.value(), 1))):
                return
            return  # 다시 찾은 결과를 보고 다시 누르게 (보지 않은 자리로 자르지 않게)
        super().accept()

    def _sync_px(self):
        w = self.w_mm.value() * self._dpi / 25.4
        h = self.h_mm.value() * self._dpi / 25.4
        self.px.setText(f"({int(round(w))}×{int(round(h))}px, {int(round(self._dpi))}dpi)")

    def find_with_size(self):
        self._load((round(self.w_mm.value(), 1), round(self.h_mm.value(), 1)))

    def find_auto(self):
        self._load(None)

    def size_row_visible(self, on: bool) -> None:
        """카드 크기 줄을 보이거나 숨긴다 (한 장에 카드 한 장이면 숨김)."""
        for w in (self.w_mm, self.h_mm, self.px, self.b_find, self.b_auto, *self._size_labels):
            w.setVisible(on)
        self.cancel.setVisible(on)  # 크기를 정하지 않는 미리 보기는 ‘닫기’ 하나

    def chosen_mm(self) -> tuple[float, float] | None:
        """정한 카드 크기 (가로, 세로) mm. 자동으로 찾은 그대로면 None."""
        return self._mm
