"""① 글자 추출: 읽기 방식·모델·범위를 고르는 창."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QRadioButton, QSpinBox,
    QVBoxLayout,
)
from app import service
from app.qt.dialogs import card, grey_note, hint_bar, page_title, set_ok_text, show_error, subtitle
from app.qt.step_common import ENGINE_LABELS, MODE_LABELS


# ================================================================ 추출
class ExtractDialog(QDialog):
    """추출할 그룹·모드·엔진 고르기."""

    def __init__(self, groups: list[str], parent=None, card_limit: int | None = None, summary: str = ""):
        super().__init__(parent)
        self.setWindowTitle("글자 추출")
        self.summary = hint_bar(summary, "info")
        self.summary.setVisible(bool(summary))
        self.group = QComboBox()
        self.group.addItem("모든 그룹", None)
        for g in groups:
            self.group.addItem(g, g)
        self.modes = QButtonGroup(self)
        mode_box = QVBoxLayout()
        for i, (k, t) in enumerate(MODE_LABELS):
            rb = QRadioButton(t)
            rb.setProperty("key", k)
            rb.setChecked(i == 0)
            self.modes.addButton(rb)
            mode_box.addWidget(rb)
        self.engine = QComboBox()
        st = service.engine_status()
        for k, t in ENGINE_LABELS:
            self.engine.addItem(t + ("" if st[k] else " — 사용할 수 없음"), k)
        self.engine.setCurrentIndex(max(0, self.engine.findData(service.get_settings().engine)))
        # 시험 모드: 파일마다 앞 X장만 (번역·검수·지우기·한글 넣기도 그 카드만). 프로젝트에 저장된다
        self.limit_on = QCheckBox("켜기: 파일마다 앞에서부터")
        self.limit_n = QSpinBox()
        self.limit_n.setRange(1, 9999)
        self.limit_n.setSuffix("장만")
        self.limit_n.setValue(card_limit or 3)
        self.limit_on.setChecked(bool(card_limit))
        self.limit_n.setEnabled(bool(card_limit))
        self.limit_on.toggled.connect(self.limit_n.setEnabled)
        limit_row = QHBoxLayout()
        limit_row.addWidget(self.limit_on)
        limit_row.addWidget(self.limit_n)
        limit_row.addStretch(1)
        limit_note = grey_note("번역·검수 엑셀·지우기·한글 넣기도 이 카드만 합니다. 끄고 다시 추출하면 남은 카드부터 이어 읽습니다.")
        limit_box = QVBoxLayout()
        limit_box.addLayout(limit_row)
        limit_box.addWidget(limit_note)
        note = grey_note("이미 읽은 카드는 다시 읽지 않습니다. 사용 한도로 멈추면 다시 시작한 곳부터 이어 갑니다.")
        form = QFormLayout()
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(16)
        if groups:
            form.addRow("그룹", self.group)
        else:
            self.group.hide()
        form.addRow("읽는 방식", mode_box)
        form.addRow("로그인 방식", self.engine)
        form.addRow("시험으로 몇 장만", limit_box)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "추출 시작")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)
        lay.addWidget(page_title("① 글자 추출", 14))
        lay.addWidget(subtitle("카드 그림에서 원문 글자를 읽습니다. 끝나면 ‘③ 원문 확인’에서 읽은 글을 확인합니다."))
        lay.addWidget(self.summary)
        lay.addWidget(card(form))
        lay.addWidget(note)
        lay.addWidget(bb)
        self.setMinimumWidth(560)

    def choice(self) -> tuple[str | None, str, str]:
        return self.group.currentData(), self.modes.checkedButton().property("key"), self.engine.currentData()

    def card_limit(self) -> int | None:
        """시험 모드 장수 (끄면 None)."""
        return self.limit_n.value() if self.limit_on.isChecked() else None

    def accept(self):
        if not service.engine_status()[self.engine.currentData()]:
            show_error(self, "이 로그인 방식은 지금 쓸 수 없습니다", service.engine_status()["message"],
                       "다른 로그인 방식을 고르거나, ‘파일 > 로그인 방식’에서 로그인한 뒤 다시 하세요.")
            return
        super().accept()
