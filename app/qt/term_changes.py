"""바뀐 용어 반영 창 (PRD F20): 용어집에서 번역이 바뀐 용어가 든 칸을 보여 주고, 고른 칸을 한꺼번에 바꾼다."""

from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QLabel, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from app import service
from app.qt import worker
from app.qt.dialogs import file_label, fit_to_screen, grey_note, hint_bar, page_title, set_ok_text, show_failure

MANUAL_NOTE = "직접 고쳐 주세요"


def _marked(text: str, words: list[str], color: str) -> str:
    """글을 HTML로: words가 나온 자리를 굵게·색으로 (줄바꿈은 <br>)."""
    words = sorted({w for w in words if w}, key=len, reverse=True)
    out, i = [], 0
    while i < len(text):
        w = next((w for w in words if text.startswith(w, i)), None)
        if w:
            out.append(f'<b style="color:{color}">{html.escape(w)}</b>')
            i += len(w)
        else:
            out.append(html.escape(text[i]))
            i += 1
    return "".join(out).replace("\n", "<br>")


class TermChangesDialog(QDialog):
    """바뀜 후보 표. 자동으로 바꿀 수 있는 칸은 체크된 채, 옛 번역이 활용된 꼴로만 있는 칸은 ‘직접 고쳐 주세요’."""

    COLS = ["", "카드", "영역", "지금 번역", "바꿀 번역"]

    def __init__(self, project_dir, changes: list[dict], rows: list[dict], prev_status: str | dict = "",
                 parent=None):
        super().__init__(parent)
        self.pdir = Path(project_dir)
        self.changes, self.rows, self.prev_status = changes, rows, prev_status
        self.result = None  # 바꾼 뒤 service.TermApplyResult
        self.setWindowTitle("바뀐 용어 반영")
        heads = "\n".join(f"{c['source']}: {c['old']} → {c['new']}" for c in changes)
        n_auto = sum(1 for r in rows if r["auto"])
        self.table = QTableWidget(len(rows), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        many = len({r["project"] for r in rows}) > 1
        olds = [c["old"] for c in changes]
        news = [c["new"] for c in changes]
        for i, r in enumerate(rows):
            chk = QTableWidgetItem()
            if r["auto"]:
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                chk.setCheckState(Qt.CheckState.Checked)
            else:
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable)  # 고를 수 없음 (흐리게)
                chk.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(i, 0, chk)
            where = (f"{r['project']} › " if many else "") + f"{file_label(r['file_key'])} {r['page']}쪽"
            self.table.setItem(i, 1, QTableWidgetItem(where))
            self.table.setItem(i, 2, QTableWidgetItem(r["region"]))
            self.table.setCellWidget(i, 3, self._label(_marked(r["current"], olds, "#B45309")))
            if r["auto"]:
                self.table.setCellWidget(i, 4, self._label(_marked(r["proposed"] or "", news, "#0b5cad")))
            else:
                self.table.setCellWidget(i, 4, self._label(f'<span style="color:#9A3412">{MANUAL_NOTE}</span>'))
        hh = self.table.horizontalHeader()
        for c in (0, 1, 2):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.resizeRowsToContents()
        self.table.itemChanged.connect(lambda _it: self._update_count())
        self.count = QLabel()
        self.count.setStyleSheet("font-weight: bold")
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "선택한 칸 바꾸기")
        self.ok = bb.button(QDialogButtonBox.StandardButton.Ok)
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("나중에")
        bb.accepted.connect(self.apply)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(10)
        lay.addWidget(page_title("바뀐 용어 반영", 14))
        lay.addWidget(hint_bar(heads, "todo"))
        lay.addWidget(grey_note(f"이 용어가 든 칸 {len(rows)}개를 찾았습니다 (바로 바꿀 수 있는 칸 {n_auto}개). "
                                "체크한 칸의 번역을 새 용어로 바꿉니다. 검수 엑셀은 그대로 두고, ‘직접 고쳐 주세요’ 칸은 "
                                "번역 검수에서 고치세요."))
        lay.addWidget(self.table, 1)
        lay.addWidget(self.count)
        lay.addWidget(bb)
        fit_to_screen(self, 980, 600, maximize_below=0)
        self._update_count()

    @staticmethod
    def _label(rich: str) -> QLabel:
        lb = QLabel(rich)
        lb.setTextFormat(Qt.TextFormat.RichText)
        lb.setWordWrap(True)
        lb.setContentsMargins(6, 4, 6, 4)
        return lb

    def picks(self) -> list[dict]:
        return [r for i, r in enumerate(self.rows)
                if self.table.item(i, 0).checkState() == Qt.CheckState.Checked]

    def _update_count(self):
        n = len(self.picks())
        self.count.setText(f"바꿀 칸: {n}개")
        self.ok.setEnabled(n > 0)

    def apply(self):
        """고른 칸을 작업 스레드에서 바꾼다 (검수 엑셀·캐시를 읽고 쓰므로 화면이 멈추지 않게)."""
        self.ok.setEnabled(False)
        job = worker.Job(service.apply_term_changes, self.pdir, self.picks(), self.changes,
                         prev_status=self.prev_status, with_progress=False)
        worker.run_with_dialog(self, "바뀐 용어를 반영하는 중", job, self._applied, self._failed, cancellable=False)

    def _applied(self, result):
        self.result = result
        self.accept()

    def _failed(self, msg: str):
        self._update_count()
        show_failure(self, "바뀐 용어를 반영하지 못했습니다", msg)
