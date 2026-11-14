"""③ 원문 확인 창: 칸마다 그림과 읽은 글자를 비교해 고치고, 이 칸 다시 읽기."""

from __future__ import annotations

import html as _html
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from app import service
from app.qt import worker
from app.qt.dialogs import (
    PRIMARY_QSS, ask_choice, file_label, fit_to_screen, hint_bar, set_hint, show_error, show_problem,
)
from app.qt.step_common import (
    DIFF_STYLE, KEEP_LABEL, KEEP_UNDO_LABEL, FitImage, ICON_TOKEN_RE, LABEL_COLORS, REREAD_MODELS, REREAD_TAG, UNKNOWN_ICON, _fail_as, as_text,
    diff_html, fill_icon_names, label_text, plain_note, precise_index, reader_name, source_font, state_label,
)


# ================================================================ 원문 확인
class SourceReviewDialog(QDialog):
    """파일의 추출 결과를 칸마다 보고, 잘못 읽은 원문을 이미지와 비교해 고친다.

    고친 글이 있는 채로 다른 칸·파일로 옮기거나 닫으면 저장할지 묻는다."""

    COLS = ["쪽", "영역", "원문 확인", "원문"]
    UNREAD = "읽지 않음"

    def __init__(self, files: list[dict], parent=None, only_check: bool = True, engine: str | None = None,
                 focus_key: str | None = None, focus_label: str | None = None):
        """focus_key: 처음 보여 줄 파일 키, focus_label: 그 파일에서 처음 고를 칸의 원문 확인 상태 (예: ‘결론 못 냄’)."""
        super().__init__(parent)
        self.setWindowTitle("원문 확인")
        self.engine = engine  # ‘이 칸 다시 읽기’ 로그인 방식 (없으면 설정에 저장된 방식)
        fit_to_screen(self, 1300, 940)  # 그림 아래에 사유·읽은 결과 표·편집 칸이 있어 세로로 넉넉히
        self.files = files
        self.file = QComboBox()
        for f in files:
            self.file.addItem(file_label(f["key"]), str(f["dir"]))
            self.file.setItemData(self.file.count() - 1, f["key"], Qt.ItemDataRole.ToolTipRole)
        self.file.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.file.setMinimumContentsLength(18)
        self.only = QCheckBox("확인할 칸만 보기 (다시 봐야 함·확인 필요·아직 읽지 않음)")
        self.only.setChecked(only_check)
        self.count = QLabel()
        self.count.setStyleSheet("color: #3A3A3C; background: white; border: 1px solid #E5E5EA;"
                                 " border-radius: 10px; padding: 3px 10px;")
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(QLabel("파일"))
        top.addWidget(self.file)
        top.addWidget(self.only)
        top.addStretch(1)
        top.addWidget(self.count)
        self.guide = hint_bar("", "info")
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)  # 줄 번호 대신 ‘쪽’ 열을 본다
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.currentCellChanged.connect(self._on_cell_changed)
        self._src_font = source_font(self.table.font(), 1)
        self.image = FitImage(160)  # 세로로 긴 그림도 다 보이게 칸에 맞춘다
        # 그림 아래: 위 = 확인할 사유, 가운데 = 읽은 결과 표 (왼쪽 ‘보통 읽기’·‘정밀 읽기’, 오른쪽 그 결과)
        self.info = hint_bar("", "info")
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.readings_box = QWidget()
        self.readings_grid = QGridLayout(self.readings_box)
        self.readings_grid.setContentsMargins(0, 0, 0, 0)
        self.readings_grid.setHorizontalSpacing(0)
        self.readings_grid.setVerticalSpacing(0)
        self.readings_grid.setColumnStretch(1, 1)
        self.reading_rows: list[tuple[QLabel, QLabel]] = []  # (머리 칸, 결과 칸)
        self._reading_font = source_font(self.font(), 1)
        self.edit = QPlainTextEdit()
        self.edit.setMinimumHeight(100)  # 네 줄쯤
        self.edit.setMaximumHeight(140)
        self.edit.setFont(source_font(self.edit.font(), 3))
        self.edit.setPlaceholderText("원문 (줄바꿈 그대로. 태그 칸은 한 줄에 하나. 아이콘은 [[이름]]). "
                                     "읽은 결과가 맞으면 고치지 않고 저장해도 ‘사람이 확인’이 됩니다")
        # 두 번 읽은 결과가 다르면: 그중 하나를 골라 편집 칸에 넣는다 (그 뒤 고쳐서 저장해도 된다)
        self.pick_row = QHBoxLayout()
        self.pick_label = QLabel("읽은 결과 쓰기:")
        self.pick_row.addWidget(self.pick_label)
        self.pick_buttons: list[QPushButton] = []
        self.pick_row.addStretch(1)
        self.save_btn = QPushButton("고친 원문 저장")
        self.save_btn.setToolTip("Ctrl+S")
        self.save_btn.clicked.connect(self.save_current)
        self.next_btn = QPushButton("저장하고 다음 칸 (Ctrl+Enter)")
        self.next_btn.clicked.connect(self.save_and_next)
        # 이 칸만 다시 읽기: 결과는 읽은 결과 표와 편집 칸에 넣고, 저장해야 원문이 바뀐다
        self.reread_model = QComboBox()
        for k, t in REREAD_MODELS:
            self.reread_model.addItem(t, k)
        self.reread_model.setToolTip("다시 읽을 방식 (정밀 읽기가 더 정확하지만 느리고 사용량이 많음)")
        self.reread_btn = QPushButton("이 칸 다시 읽기")
        self.reread_btn.setToolTip("그림을 Claude로 다시 읽어 편집 칸에 넣습니다 (저장해야 원문이 바뀝니다)")
        self.reread_btn.clicked.connect(self.reread_current)
        # 칸 전체가 아이콘(그림)이면: 번역·지우기·한글 넣기를 하지 않고 원본 그대로 둔다
        self.keep_btn = QPushButton(KEEP_LABEL)
        self.keep_btn.setCheckable(True)
        self.keep_btn.setToolTip("이 칸은 글자 없이 아이콘(그림)만 있습니다.\n"
                                 "번역·용어집에서 빼고, 글자 지우기·한글 넣기도 하지 않고 원본 그대로 둡니다 (다시 누르면 풀림).")
        self.keep_btn.clicked.connect(self.toggle_keep_original)
        hint = QLabel("Ctrl+S 저장 · Ctrl+Enter 저장하고 다음 칸 · Alt+↑/↓ 칸 이동")
        hint.setStyleSheet("color: #8E8E93")
        self.next_btn.setStyleSheet(PRIMARY_QSS)  # 주 동작 (엔터 기본 버튼으로는 만들지 않는다)
        # 버튼 글이 잘리지 않게 두 줄로: 위 = 이 칸 도구 (다시 읽기·원본 그대로), 아래 = 저장 (주 동작은 오른쪽 끝)
        tools = QHBoxLayout()
        tools.setSpacing(8)
        tools.addWidget(QLabel("이 칸:"))
        tools.addWidget(self.reread_btn)
        tools.addWidget(self.reread_model)
        tools.addSpacing(8)
        tools.addWidget(self.keep_btn)
        tools.addStretch(1)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addWidget(hint, 1)
        btns.addWidget(self.save_btn)
        btns.addWidget(self.next_btn)
        for b in (self.reread_btn, self.keep_btn, self.save_btn, self.next_btn):
            b.setMinimumWidth(b.sizeHint().width())
        # 오른쪽: 머리(고른 칸) → 그림(남는 높이를 모두 쓰고, 늘 칸 안에 다 보이게) → 사유 → 읽은 결과 표 → 고르기
        # → ‘원문’ 편집 칸 → 이 칸 도구 → 저장 (글이 잘리지 않게 제 높이대로)
        self.cell_title = QLabel()
        self.cell_title.setStyleSheet("font-size: 15px; font-weight: 600; color: #1D1D1F;")
        edit_title = QLabel("원문")
        edit_title.setStyleSheet("font-weight: 600; color: #3A3A3C;")
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        rl.addWidget(self.cell_title)
        rl.addWidget(self.image, 1)
        rl.addWidget(self.info)
        rl.addWidget(self.readings_box)
        rl.addLayout(self.pick_row)
        rl.addSpacing(4)
        rl.addWidget(edit_title)
        rl.addWidget(self.edit)
        rl.addLayout(tools)
        rl.addSpacing(2)
        rl.addLayout(btns)
        # 왼쪽 표를 끌어 넓힐 수 있게: 오른쪽(그림·읽은 결과·편집 칸)이 제 최소 크기를 크게 잡으면 나눔 막대가 움직이지 않는다
        right.setMinimumWidth(380)
        self.table.setMinimumWidth(240)
        for col in (0, 1, 2):  # 쪽·영역·원문 확인은 내용 폭으로, 원문 열이 나머지를 쓴다
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        split = QSplitter()
        split.setChildrenCollapsible(False)
        split.addWidget(self.table)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([560, 680])  # 표 45% (원문이 덜 잘리게)
        self.split = split
        close = QPushButton("닫기")
        close.setAutoDefault(False)
        close.clicked.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)
        lay.addLayout(top)
        lay.addWidget(self.guide)
        lay.addWidget(split, 1)
        lay.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        for b in (self.save_btn, self.next_btn, self.reread_btn, self.keep_btn):
            b.setAutoDefault(False)
        for keys, fn in (("Ctrl+S", self.save_current), ("Ctrl+Return", self.save_and_next),
                         ("Ctrl+Enter", self.save_and_next), ("Alt+Down", lambda: self.move_row(1)),
                         ("Alt+Up", lambda: self.move_row(-1))):
            QShortcut(QKeySequence(keys), self, activated=fn)
        self.cells: list[dict] = []
        self.saved = 0
        self._busy = False  # 표를 다시 채우는 동안에는 칸 바뀜을 무시
        self._shown: tuple | None = None  # 편집기에 띄운 칸 (쪽, 영역)
        self._shown_cell: dict | None = None
        self._shown_file: str | None = None
        self._orig_text = ""
        if focus_key:  # 고칠 칸이 있는 파일부터 (용어집 만들기에서 ‘남은 항목’을 고치러 왔을 때)
            i = next((k for k, f in enumerate(files) if f["key"] == focus_key), -1)
            if i >= 0:
                self.file.setCurrentIndex(i)
        self._file_idx = self.file.currentIndex()
        self.file.currentIndexChanged.connect(self._on_file_changed)
        self.only.toggled.connect(self._on_only_toggled)
        self.reload()
        if focus_label:
            i = next((k for k, c in enumerate(self.cells) if c["label"] == focus_label), None)
            if i is not None:
                self.table.setCurrentCell(i, 0)

    @staticmethod
    def needs_check(c: dict) -> bool:
        return c["uncertain"] or c["label"] in ("결론 못 냄", SourceReviewDialog.UNREAD)

    @staticmethod
    def needs_attention(c: dict) -> bool:
        """사람이 보고 판단할 칸 (아직 읽지 않은 칸은 뺀다)."""
        return c["label"] != SourceReviewDialog.UNREAD and (c["uncertain"] or c["label"] == "결론 못 냄")

    # ---- 고친 글 지키기
    def _dirty(self) -> bool:
        return self._shown is not None and self.edit.toPlainText() != self._orig_text

    def _value_of(self, c: dict):
        text = self.edit.toPlainText().strip("\n")
        return [t.strip() for t in text.split("\n") if t.strip()] if c["kind"] == "tag_pills" else text

    def _save_shown(self):
        c = self._shown_cell
        service.save_source(self._shown_file, c["page"], c["region"], self._value_of(c))
        self.saved += 1
        self._orig_text = self.edit.toPlainText()  # 더 묻지 않게

    def _ask_save(self) -> str:
        """고친 글이 있으면 묻는다. 'save' | 'discard' | 'cancel' (고친 글이 없으면 'discard')."""
        if not self._dirty():
            return "discard"
        c = self._shown_cell
        r = ask_choice(self, "고친 원문", f"{c['page']}쪽 {c['region']}의 고친 원문을 저장할까요?",
                       [("저장", "accept"), ("저장 안 함", "destructive"), ("취소", "reject")], default=0)
        return {0: "save", 1: "discard"}.get(r, "cancel")

    def _confirm_leave(self) -> bool:
        a = self._ask_save()
        if a == "save":
            self._save_shown()
        return a != "cancel"

    def _on_cell_changed(self, _row, _col, prow, _pcol):
        if self._busy:
            return
        c = self.current()
        if c is not None and self._shown == (c["page"], c["region"]):
            return  # 같은 칸의 다른 열을 눌렀을 뿐: 편집 중인 글을 건드리지 않는다
        a = self._ask_save()
        if a == "cancel":
            self._busy = True
            try:
                self.table.setCurrentCell(prow, 0)  # 원래 줄로
            finally:
                self._busy = False
            return
        if a == "save":
            self._save_shown()
            self.reload(keep=(c["page"], c["region"]) if c else None)
            return
        self._show()

    def _on_file_changed(self, idx):
        if not self._confirm_leave():
            self.file.blockSignals(True)
            self.file.setCurrentIndex(self._file_idx)
            self.file.blockSignals(False)
            return
        self._file_idx = idx
        self._shown = None
        self.reload()

    def _on_only_toggled(self, checked):
        if not self._confirm_leave():
            self.only.blockSignals(True)
            self.only.setChecked(not checked)
            self.only.blockSignals(False)
            return
        self.reload(keep=self._shown)

    def done(self, r):
        if self._confirm_leave():
            super().done(r)

    # ---- 표
    def reload(self, keep: tuple | None = None, row: int | None = None):
        fd = self.file.currentData()
        allc = service.extracted_cells(fd)
        self._known = set(service.known_icon_names(fd))  # 이름을 정한 아이콘 (읽은 결과의 [[?]]를 이름으로 보인다)
        self.cells = [c for c in allc if self.needs_check(c)] if self.only.isChecked() else allc
        n_att = sum(1 for c in allc if self.needs_attention(c))
        n_unread = sum(1 for c in allc if c["label"] == self.UNREAD)
        self.count.setText(f"확인할 칸 {n_att} · 아직 읽지 않은 칸 {n_unread} / 전체 {len(allc)}")
        if n_att:
            set_hint(self.guide, f"확인할 칸 {n_att}개가 남았습니다. 그림과 비교해 고친 뒤 ‘저장하고 다음 칸’을 누르세요.", "todo")
        elif n_unread:
            set_hint(self.guide, f"아직 읽지 않은 칸이 {n_unread}개 있습니다. 프로젝트 화면에서 ‘① 글자 추출’을 다시 누르면 이어서 읽습니다.", "info")
        else:
            set_hint(self.guide, "확인할 칸이 없습니다. 창을 닫고 ‘④ 용어집 만들기’로 넘어가세요.", "done")
        self._busy = True
        try:
            self.table.setRowCount(len(self.cells))
            for i, c in enumerate(self.cells):
                src = c["source"]
                text = "\n".join(src) if isinstance(src, list) else (src or "")
                unread = c["label"] == self.UNREAD
                vals = [str(c["page"]), c["region"],
                        "번역 안 함 (아이콘)" if c.get("keep_original") else label_text(c["label"]),
                        "(아직 추출하지 않음 — ① 글자 추출)" if unread and not text else text.replace("\n", " / ")]
                for j, v in enumerate(vals):
                    it = QTableWidgetItem(v)
                    color = LABEL_COLORS.get(c["label"]) or ("#FFE066" if c["uncertain"] else None)
                    if color and j == 2:
                        it.setBackground(QColor(color))
                    if unread:
                        it.setForeground(QColor("#888"))
                    if j == 3:
                        it.setFont(self._src_font)
                    self.table.setItem(i, j, it)
            self.table.resizeColumnsToContents()
            if row is None:
                row = 0
                if keep:
                    row = next((i for i, c in enumerate(self.cells) if (c["page"], c["region"]) == keep), 0)
            row = min(row, len(self.cells) - 1)
            self._shown = None
            if self.cells:
                self.table.setCurrentCell(row, 0)
        finally:
            self._busy = False
        if self.cells:
            self._show()
        else:
            self.image.clear()
            set_hint(self.info, "확인할 칸이 없습니다", "done")
            self.cell_title.setText("")
            self._fill_readings([], None)
            self._fill_picks([])
            self.edit.clear()
            self._shown = self._shown_cell = None
            self._orig_text = ""
        self.save_btn.setEnabled(bool(self.cells))
        self.next_btn.setEnabled(bool(self.cells))
        self.reread_btn.setEnabled(bool(self.cells))
        if not self.cells:  # 칸이 있으면 _show가 정한다 (아직 읽지 않은 칸은 정할 수 없음)
            self.keep_btn.setEnabled(False)

    def current(self) -> dict | None:
        r = self.table.currentRow()
        return self.cells[r] if 0 <= r < len(self.cells) else None

    def _show(self):
        c = self.current()
        if c is None:
            return
        pm = QPixmap()
        pm.loadFromData(service.region_crop(self.file.currentData(), c["page"], c["region"]), "PNG")
        self.image.set_image(pm)  # 칸에 다 들어가게 비율 그대로 맞춘다 (창 크기가 바뀌면 다시)
        src = as_text(c["source"])
        texts = self.reading_texts(c, self._known)
        pi = precise_index(c["readings"])
        differ = len({t for _n, t in texts}) >= 2
        default = self.default_text(c, texts, pi)
        kind = "todo" if self.needs_attention(c) else "info"
        head = _html.escape(f"원문 확인: {label_text(c['label'])}")  # 쪽·영역은 위 제목에
        reason, memo = self.reason_text(c, differ)
        reason = _html.escape(reason)
        if differ:
            reason = reason.replace("다릅니다.", f'다릅니다 (다른 곳은 <span style="{DIFF_STYLE}">빨간 굵은 글자</span>).', 1)
        if differ and pi is not None and self.needs_attention(c):
            reason += " 편집 칸에는 정밀 읽기 결과를 넣어 두었습니다."
        self.cell_title.setText(f"{c['page']}쪽 · {c['region']}   ")
        self.cell_title.setToolTip(f"원문 확인: {label_text(c['label'])}")
        text = f"<b>{head}</b><br>사유: {reason}"
        if memo:
            text += f'<br><span style="color:#555">읽은 모델의 메모: {_html.escape(memo)}</span>'
        set_hint(self.info, text, kind)
        self._fill_readings(texts, pi)
        self._fill_picks(texts)
        self.edit.setPlainText(default)
        self._shown, self._shown_cell, self._shown_file = (c["page"], c["region"]), c, self.file.currentData()
        self._orig_text = self.edit.toPlainText()
        keep = bool(c.get("keep_original"))
        self.keep_btn.setEnabled(c["source"] is not None)  # 아직 읽지 않은 칸은 정할 수 없다
        self.keep_btn.setChecked(keep)
        self.keep_btn.setText(KEEP_UNDO_LABEL if keep else KEEP_LABEL)

    def toggle_keep_original(self):
        """지금 칸을 ‘전체가 아이콘 — 번역 안 함’으로 정하거나 푼다. 정하면 다음 칸으로 간다."""
        c = self.current()
        if c is None:
            return
        keep = not c.get("keep_original")
        if keep and self._dirty():  # 고친 글은 버린다 (번역하지 않는 칸)
            self.edit.setPlainText(self._orig_text)
        try:
            service.set_keep_original(self.file.currentData(), c["page"], c["region"], keep)
        except Exception as e:  # noqa: BLE001
            show_error(self, "정하지 못했습니다", e)
            self.keep_btn.setChecked(not keep)
            return
        self.saved += 1
        if keep:  # 다음 칸으로 (확인할 칸만 보는 중이면 이 칸이 목록에서 빠질 수 있어 같은 줄 번호)
            self.reload(row=self.table.currentRow() + (0 if self.only.isChecked() else 1))
        else:
            self.reload(keep=(c["page"], c["region"]))

    @staticmethod
    def reading_texts(c: dict, known=None) -> list[tuple[str, str]]:
        """[(‘보통 읽기’ 등, 읽은 결과 글)]. 이름을 정한 아이콘은 [[?]] 대신 [[이름]]으로."""
        src = as_text(c["source"])
        return [(reader_name(rd["model"]) + (REREAD_TAG if rd.get("reread") else ""),
                 fill_icon_names(as_text(rd["source"]), src, known)) for rd in c["readings"]]

    def default_text(self, c: dict, texts: list[tuple[str, str]], pi: int | None) -> str:
        """편집 칸의 처음 글. 다시 봐야 하는 칸은 정밀 읽기 결과를 따른다.
        지금 원문이 정밀 읽기와 (아이콘·빈칸 말고) 같으면 원문 그대로 둔다 (아이콘 이름을 정한 결과를 지키려고)."""
        src = as_text(c["source"])
        if pi is None or not self.needs_attention(c):
            return src
        precise = texts[pi][1]

        def core(t: str) -> str:
            return re.sub(r"\s", "", ICON_TOKEN_RE.sub("", t))
        return src if src and core(src) == core(precise) else precise

    @staticmethod
    def reason_text(c: dict, differ: bool) -> tuple[str, str]:
        """(이 칸을 확인해야 하는 까닭, 읽은 모델이 남긴 메모) — 쉬운 말로."""
        note = c.get("note") or ""
        if c["label"] == SourceReviewDialog.UNREAD:
            return "아직 읽지 않은 칸입니다. 그림을 보고 원문을 직접 적어 저장해도 됩니다.", ""
        if not SourceReviewDialog.needs_attention(c):
            return plain_note(note) or "따로 확인할 점은 없습니다. 틀린 곳이 보이면 고쳐서 저장하세요.", ""
        # 엔진 메모 ‘두 모델이 다르게 읽음 (A: … / B: …). 덧붙인 말’에서 두 결과는 표에 있으니 덧붙인 말만
        memo = note.rsplit(").", 1)[1].strip() if note.startswith("두 모델이") and ")." in note else ""
        if differ:
            base = "두 번 읽은 결과가 다릅니다. 그림과 비교해 맞게 고쳐 저장하세요."
        elif c["readings"]:
            base = "두 번 읽은 결과는 같지만 정밀 읽기도 확실하지 않다고 했습니다. 그림과 비교해 주세요."
        else:
            base, memo = "읽은 결과가 확실하지 않다고 했습니다. 그림과 비교해 주세요.", note
        return base, plain_note(memo)

    def _fill_readings(self, texts: list[tuple[str, str]], pi: int | None):
        """읽은 결과 표: 왼쪽 ‘보통 읽기’·‘정밀 읽기’, 오른쪽 그 결과 (정밀 읽기와 다른 곳은 빨간 굵은 글자)."""
        for h, v in self.reading_rows:
            for w in (h, v):
                self.readings_grid.removeWidget(w)
                w.hide()  # 지워지기 전까지 옛 칸이 겹쳐 그려지지 않게
                w.deleteLater()
        self.reading_rows = []
        self.readings_box.setVisible(bool(texts))
        if not texts:
            return
        htmls = diff_html([t for _n, t in texts], pi if pi is not None else len(texts) - 1)
        last = len(texts) - 1
        for i, ((name, _t), body) in enumerate(zip(texts, htmls)):
            edge = "border: 1px solid #D1D1D6;" + ("" if i == last else " border-bottom: none;")
            h = QLabel(name)
            h.setStyleSheet(f"background: #F2F2F7; color: #3A3A3C; font-weight: bold; padding: 5px 10px; {edge} border-right: none;")
            h.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            v = QLabel(body or '<span style="color:#888">(빈 칸)</span>')
            v.setTextFormat(Qt.TextFormat.RichText)
            v.setWordWrap(True)
            v.setFont(self._reading_font)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            v.setStyleSheet(f"background: white; padding: 5px 10px; {edge}")
            self.readings_grid.addWidget(h, i, 0)
            self.readings_grid.addWidget(v, i, 1)
            self.reading_rows.append((h, v))

    def _fill_picks(self, texts: list[tuple[str, str]]):
        """읽은 결과마다 ‘이것으로’ 버튼. 결과가 하나뿐이거나 같으면 숨긴다."""
        for b in self.pick_buttons:
            self.pick_row.removeWidget(b)
            b.hide()
            b.deleteLater()
        self.pick_buttons = []
        uniq: list[tuple[str, str]] = []
        for name, t in texts:
            if t not in [x for _n, x in uniq]:
                uniq.append((name, t))
        show = len(uniq) >= 2
        self.pick_label.setVisible(show)
        if not show:
            return
        for i, (name, t) in enumerate(uniq):
            b = QPushButton(f"{name}대로")
            b.setAutoDefault(False)
            b.setToolTip(t)
            b.clicked.connect(lambda _c=False, tt=t: (self.edit.setPlainText(tt), self.edit.setFocus()))
            self.pick_row.insertWidget(1 + i, b)
            self.pick_buttons.append(b)

    def save_current(self):
        c = self.current()
        if c is None:
            return
        self._shown, self._shown_cell, self._shown_file = (c["page"], c["region"]), c, self.file.currentData()
        self._save_shown()
        self.reload(keep=(c["page"], c["region"]))

    def save_and_next(self):
        """저장하고 다음 칸으로: 확인할 칸이 남아 있으면 (지금 칸 뒤에서, 없으면 처음부터) 다음 확인할 칸으로 간다.
        확인할 칸이 없으면 바로 다음 칸. ‘확인할 칸만’일 때는 저장한 칸이 목록에서 빠지므로 같은 줄 번호부터 찾는다."""
        c = self.current()
        if c is None:
            return
        r = self.table.currentRow()
        key = (c["page"], c["region"])
        self._shown, self._shown_cell, self._shown_file = key, c, self.file.currentData()
        self._save_shown()
        self.reload(keep=key)  # 새 목록을 만든 뒤 다음 칸을 찾는다
        idx = next((i for i, x in enumerate(self.cells) if (x["page"], x["region"]) == key), None)
        start = r if idx is None else idx + 1
        order = list(range(start, len(self.cells))) + list(range(0, min(start, len(self.cells))))
        nxt = next((i for i in order if self.needs_attention(self.cells[i]) and i != idx), None)
        if nxt is None:  # 확인할 칸이 없다: 바로 다음 칸
            nxt = start
        if 0 <= nxt < len(self.cells) and nxt != self.table.currentRow():
            self.table.setCurrentCell(nxt, 0)

    def move_row(self, d: int):
        r = self.table.currentRow() + d
        if 0 <= r < len(self.cells):
            self.table.setCurrentCell(r, 0)

    # ---- 이 칸 다시 읽기
    def reread_current(self):
        """고른 칸만 Claude로 다시 읽는다. 끝나면 결과를 편집 칸에 넣는다 (저장은 사람이)."""
        c = self.current()
        if c is None or not self._confirm_leave():  # 편집 칸을 바꾸므로 고친 글이 있으면 먼저 묻는다
            return
        eng = self.engine or service.get_settings().engine
        st = service.engine_status()
        if not st.get(eng):
            show_error(self, "Claude에 연결할 수 없습니다", st["message"],
                       "‘파일 > 로그인 방식’에서 로그인하거나 다른 방식을 고른 뒤 다시 하세요.")
            return
        fd, key = self.file.currentData(), (c["page"], c["region"])
        model = self.reread_model.currentData()
        job = worker.Job(service.reread_region, fd, c["page"], c["region"], model=model, engine=eng,
                         with_progress=False)
        worker.run_with_dialog(self, f"{c['page']}쪽 {c['region']} 다시 읽는 중", job,
                               lambda r: self._reread_done(r, fd, key),
                               _fail_as(self, "다시 읽지 못했습니다"), cancellable=False)

    def _reread_done(self, r, fd: str, key: tuple):
        if r.state != "done":
            show_problem(self, f"다시 읽지 못했습니다 ({state_label(r.state)})", r.reason or "",
                         retry_after=r.retry_after)
            return
        if self.file.currentData() != fd:
            return  # 그사이 다른 파일로 옮겼다
        self.reload(keep=key)
        c = self.current()
        if (c is None or (c["page"], c["region"]) != key) and self.only.isChecked():
            # 다시 읽기 전에 저장해 ‘확인할 칸만’ 목록에서 빠진 칸: 거르기를 끄고 그 칸을 보여 준다
            self.only.blockSignals(True)
            self.only.setChecked(False)
            self.only.blockSignals(False)
            self.reload(keep=key)
            c = self.current()
        if c is None or (c["page"], c["region"]) != key:
            QMessageBox.information(self, "다시 읽기", f"{key[0]}쪽 {key[1]} 칸을 찾지 못해 다시 읽은 결과를 보여 주지 못했습니다.\n"
                                    f"다시 읽은 결과: {as_text(r.source)}")
            return
        text = fill_icon_names(as_text(r.source), as_text(c["source"]), self._known)
        self.edit.setPlainText(text)  # _orig_text는 그대로: 저장하지 않고 떠나면 묻는다
        msg = "다시 읽은 결과를 편집 칸에 넣었습니다. 맞으면 저장하세요."
        if UNKNOWN_ICON in text:
            msg += " 이름 없는 아이콘 [[?]]는 저장한 뒤 ‘② 아이콘 정의’에서 정합니다."
        if r.uncertain:
            msg += " (다시 읽은 결과도 확실하지 않다고 했습니다. 그림과 비교해 주세요.)"
        set_hint(self.info, self.info.text() + f'<br><b>{_html.escape(msg)}</b>', "todo")
