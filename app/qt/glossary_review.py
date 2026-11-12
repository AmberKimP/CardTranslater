"""⑤ 용어집 확정 창: ④ 용어집 만들기로 모은 용어 후보의 번역을 프로그램 안에서 확인·고쳐 확정한다.

엑셀(용어집_검증.xlsx)은 따로 둔다: ‘엑셀로 내보내기’로 표 내용을 엑셀에 쓰고, ‘엑셀에서 가져오기’로 엑셀에서 고친
것을 그대로 확정할 수 있다 (예전 흐름). 화면에서 고친 값은 닫을 때 검증 엑셀에 저장해 다음에 이어 볼 수 있다."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from app import service
from app.qt.dialogs import PRIMARY_QSS, fit_to_screen, hint_bar, page_title, set_hint, show_error

# 표 열 = 검증 엑셀 열 (같은 이름이라 같은 검사를 쓴다). 고칠 수 있는 열만 편집된다
COLS = ["원문 용어", "번역", "추천 번역", "원문 상태", "번역 확인", "다른 후보", "분류", "등장 횟수", "예문", "변형", "메모"]
EDITABLE = {"원문 용어", "번역", "분류", "변형", "메모"}
C = {h: i for i, h in enumerate(COLS)}
FILL = {"edit": "#FFF8DC", "done": "#D9EAD3", "no_verdict": "#FF7C80", "unsure": "#FFE066", "check": "#D9D2E9"}


class _NumItem(QTableWidgetItem):
    """숫자로 정렬되는 칸 (등장 횟수)."""

    def __lt__(self, other):
        try:
            return int(self.text() or 0) < int(other.text() or 0)
        except ValueError:
            return super().__lt__(other)


class GlossaryReviewDialog(QDialog):
    """용어 후보 표. 머리글을 누르면 그 열로 정렬, ‘번역’을 고치고 ‘용어집 확정’을 누른다.
    result: 확정하면 service.GlossaryConfirmResult."""

    def __init__(self, project_dir, rows: list[dict], parent=None):
        super().__init__(parent)
        self.project_dir = project_dir
        self.result = None
        self._dirty = False  # 표를 고쳤는가 (고친 것이 있을 때만 닫으면서 검증 엑셀에 쓴다)
        self._from_excel = False  # 엑셀에서 가져오기를 해 봤는가 (그 뒤에는 표로 엑셀을 덮어쓰지 않는다)
        self.setWindowTitle("⑤ 용어집 확정")
        fit_to_screen(self, 1400, 760, maximize_below=0)
        self.guide = hint_bar("", "info")
        self.search = QLineEdit()
        self.search.setPlaceholderText("찾기 (원문·번역)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        self.only_check = QCheckBox("볼 것만 (번역 확인·불확실한 원문·번역 빈 칸)")
        self.only_check.toggled.connect(lambda _c: self._filter())
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAutoScroll(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed
                                   | QAbstractItemView.EditTrigger.AnyKeyPressed)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(C["예문"], QHeaderView.ResizeMode.Stretch)
        for h, w in {"원문 용어": 160, "번역": 160, "추천 번역": 150, "원문 상태": 80, "번역 확인": 72,
                     "다른 후보": 200, "분류": 90, "등장 횟수": 64, "변형": 110, "메모": 160}.items():
            self.table.setColumnWidth(C[h], w)
        self.add_btn = QPushButton("용어 더하기")
        self.add_btn.clicked.connect(self.add_row)
        self.del_btn = QPushButton("고른 줄 빼기")
        self.del_btn.setToolTip("새로 더한 줄은 지우고, 후보 줄은 번역을 비웁니다 (번역이 빈 용어는 용어집에 넣지 않음)")
        self.del_btn.clicked.connect(self.remove_rows)
        self.export_btn = QPushButton("엑셀로 내보내기…")
        self.export_btn.clicked.connect(self.export_excel)
        self.import_btn = QPushButton("엑셀에서 가져오기…")
        self.import_btn.setToolTip("엑셀에서 고친 용어집을 가져와 그대로 확정합니다 (표의 내용 대신)")
        self.import_btn.clicked.connect(self.import_excel)
        self.ok_btn = QPushButton("용어집 확정")
        self.ok_btn.setStyleSheet(PRIMARY_QSS)
        self.ok_btn.clicked.connect(self.confirm)
        close = QPushButton("닫기 (고친 것 저장)")
        close.clicked.connect(self.reject)
        for b in (self.add_btn, self.del_btn, self.export_btn, self.import_btn, self.ok_btn, close):
            b.setAutoDefault(False)
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.only_check)
        bottom = QHBoxLayout()
        bottom.addWidget(self.add_btn)
        bottom.addWidget(self.del_btn)
        bottom.addSpacing(16)
        bottom.addWidget(self.export_btn)
        bottom.addWidget(self.import_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.ok_btn)
        bottom.addWidget(close)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.addWidget(page_title("⑤ 용어집 확정", 14))
        lay.addWidget(self.guide)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)
        lay.addLayout(bottom)
        self._fill(rows)
        self.table.itemChanged.connect(self._mark_dirty)

    def _mark_dirty(self, _item=None):
        self._dirty = True

    # ---- 표
    def _fill(self, rows: list[dict]):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        # 처음 순서: 번역 확인(보라)이 위, 그다음 원문 용어 (엑셀과 같음)
        rows = sorted(rows, key=lambda r: (not r.get("번역 확인"), str(r.get("원문 용어") or "")))
        for r in rows:
            self._append(r)
        self.table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)  # 켤 때 다시 정렬하지 않게
        self.table.setSortingEnabled(True)
        self._update_guide()

    def _append(self, r: dict, new: bool = False) -> int:
        i = self.table.rowCount()
        self.table.insertRow(i)
        for h in COLS:
            v = r.get(h)
            it = _NumItem(str(v or 0)) if h == "등장 횟수" else QTableWidgetItem("" if v is None else str(v))
            if h not in EDITABLE:
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if h == "예문":
                it.setToolTip(str(v or ""))
                it.setText(str(v or "").replace("\n", " / "))
            if h == "다른 후보" and v:
                it.setToolTip(str(v))
            self.table.setItem(i, C[h], it)
        meta = self.table.item(i, C["원문 용어"])
        meta.setData(Qt.ItemDataRole.UserRole, {"confirmed": bool(r.get("confirmed")), "new": new,
                                                "예문": r.get("예문") or ""})
        self._paint(i, r)
        return i

    def _paint(self, i: int, r: dict):
        ko = self.table.item(i, C["번역"])
        ko.setBackground(QBrush(QColor(FILL["done"] if r.get("confirmed") else FILL["edit"])))
        if r.get("번역 확인"):
            ko.setBackground(QBrush(QColor(FILL["check"])))
            self.table.item(i, C["번역 확인"]).setBackground(QBrush(QColor(FILL["check"])))
        verdict = r.get("원문 상태") or ""
        if verdict and verdict != "확실":
            col = FILL["no_verdict"] if verdict == "결론 못 냄" else FILL["unsure"]
            for h in ("원문 용어", "원문 상태"):
                self.table.item(i, C[h]).setBackground(QBrush(QColor(col)))
            self.table.item(i, C["원문 용어"]).setToolTip(
                f"이 용어는 추출이 불확실한 칸({verdict})에서만 나왔습니다. 원문 글자가 틀렸을 수 있습니다.")

    def rows(self) -> list[dict]:
        """지금 표의 행 (엑셀과 같은 열 이름). 보이는 순서대로."""
        out = []
        for i in range(self.table.rowCount()):
            r = {h: (self.table.item(i, C[h]).text() if self.table.item(i, C[h]) else "") for h in COLS}
            meta = self.table.item(i, C["원문 용어"]).data(Qt.ItemDataRole.UserRole) or {}
            r["예문"] = meta.get("예문", "")
            r["등장 횟수"] = int(r["등장 횟수"] or 0) if str(r["등장 횟수"]).isdigit() else 0
            r["confirmed"] = bool(meta.get("confirmed"))
            out.append(r)
        return out

    def _update_guide(self):
        rows = self.rows()
        n_check = sum(1 for r in rows if r["번역 확인"])
        n_empty = sum(1 for r in rows if r["원문 용어"].strip() and not r["번역"].strip())
        n_new = sum(1 for r in rows if not r["confirmed"])
        set_hint(self.guide,
                 f"용어 {len(rows)}개 (새 후보 {n_new}) · 번역 확인 {n_check}개(보라) · 번역 빈 칸 {n_empty}개\n"
                 "‘번역’ 칸을 두 번 누르거나 글자를 치면 고칩니다. 번역을 비우면 그 용어는 용어집에 넣지 않습니다. "
                 "머리글을 누르면 그 열로 정렬합니다.\n"
                 "색: 빨강 = 결론 못 낸 원문에서만 나온 용어 · 노랑 = 불확실한 원문 · 보라 = 번역이 갈릴 수 있음 · "
                 "연두 = 이미 확정", "todo" if n_check else "info")

    def _filter(self):
        q = self.search.text().strip().lower()
        only = self.only_check.isChecked()
        for i in range(self.table.rowCount()):
            src = self.table.item(i, C["원문 용어"]).text()
            ko = self.table.item(i, C["번역"]).text()
            show = not q or q in src.lower() or q in ko.lower()
            if only:
                st = self.table.item(i, C["원문 상태"]).text()
                show = show and bool(self.table.item(i, C["번역 확인"]).text() or (st and st != "확실")
                                     or not ko.strip())
            self.table.setRowHidden(i, not show)

    def add_row(self):
        hh = self.table.horizontalHeader()
        col, order = hh.sortIndicatorSection(), hh.sortIndicatorOrder()
        self.table.setSortingEnabled(False)
        self._append({"분류": "기타", "원문 상태": "확실", "등장 횟수": 0}, new=True)
        hh.setSortIndicator(-1, order)  # 새 줄은 맨 아래에 두고 (켤 때 다시 정렬하지 않게) 정렬 표시만 지운다
        self.table.setSortingEnabled(True)
        del col
        i = next(k for k in range(self.table.rowCount())
                 if (self.table.item(k, C["원문 용어"]).data(Qt.ItemDataRole.UserRole) or {}).get("new")
                 and not self.table.item(k, C["원문 용어"]).text())
        self.table.scrollToItem(self.table.item(i, 0))
        self.table.setCurrentCell(i, C["원문 용어"])
        self.table.editItem(self.table.item(i, C["원문 용어"]))
        self._dirty = True

    def remove_rows(self):
        rows = sorted({ix.row() for ix in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        # 정렬을 켠 채 번역을 비우면 줄이 다시 정렬돼 남은 줄 번호가 다른 용어를 가리킨다: 잠시 끈다
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        for i in rows:
            meta = self.table.item(i, C["원문 용어"]).data(Qt.ItemDataRole.UserRole) or {}
            if meta.get("new"):
                self.table.removeRow(i)
            else:
                self.table.item(i, C["번역"]).setText("")
        if sorting:
            hh = self.table.horizontalHeader()
            hh.setSortIndicator(-1, hh.sortIndicatorOrder())  # 켤 때 다시 정렬하지 않게
            self.table.setSortingEnabled(True)
        self._dirty = True
        self._update_guide()

    # ---- 엑셀
    def export_excel(self):
        try:
            p = service.save_glossary_rows(self.project_dir, self.rows())
        except OSError as e:
            show_error(self, "엑셀에 쓰지 못했습니다", str(e), "엑셀에서 용어집_검증.xlsx를 닫고 다시 하세요.")
            return
        self._dirty = False  # 표 내용이 엑셀에 있다: 엑셀에서 고친 것을 닫을 때 덮어쓰지 않게
        from app.qt.main import open_path

        open_path(p)

    def import_excel(self):
        default = str(service.game_dir(self.project_dir) / "용어집_검증.xlsx")
        path, _ = QFileDialog.getOpenFileName(self, "검증한 용어집 엑셀", default, "Excel (*.xlsx)")
        if path:
            # 가져오기가 막혀도 엑셀이 원본이다: 이 뒤로는 닫을 때 표 내용으로 엑셀을 덮어쓰지 않는다
            self._from_excel = True
            self.done_with(lambda accept: service.import_glossary(self.project_dir, path, accept_issues=accept),
                           fix_where="엑셀")

    # ---- 확정
    def confirm(self):
        rows = self.rows()
        self.done_with(lambda accept: service.confirm_glossary_rows(self.project_dir, rows, accept_issues=accept))

    def done_with(self, run, fix_where: str = "표"):
        """확정을 해 보고 (문제가 있으면 알리거나 묻고) 확정되면 창을 닫는다. fix_where: 고칠 곳 (표·엑셀)."""
        from app.qt.dialogs import show_problem
        from app.qt.steps import _ask_list

        r = run(False)
        if r.state == "blocked":
            show_problem(self, "용어집을 확정하지 못했습니다", "", items=r.blocking + r.issues,
                         todo=("엑셀에서 이 내용을 고치고 저장한 뒤 ‘엑셀에서 가져오기’를 다시 누르세요." if fix_where == "엑셀"
                               else "표에서 이 내용을 고친 뒤 ‘용어집 확정’을 다시 누르세요."))
            return
        if r.state == "needs_confirmation":
            if not _ask_list(self, "용어집 확인", "아래 항목을 확인하세요.", r.issues, "이대로 확정할까요?"):
                return
            r = run(True)
            if r.state != "confirmed":
                show_problem(self, "용어집을 확정하지 못했습니다", "", items=r.blocking + r.issues)
                return
        self.result = r
        self.accept()

    def reject(self):
        """확정하지 않고 닫을 때: 표에서 고친 것이 있으면 검증 엑셀에 남긴다 (다음에 이어 보기).
        엑셀에서 가져오기를 해 봤으면 엑셀이 원본이므로 덮어쓰지 않는다. 쓰지 못하면 알리고 닫을지 묻는다."""
        if self._dirty and not self._from_excel:
            try:
                service.save_glossary_rows(self.project_dir, self.rows())
            except OSError as e:
                from app.qt.dialogs import ask_choice

                c = ask_choice(self, "고친 것을 저장하지 못했습니다",
                               f"용어집_검증.xlsx에 쓰지 못했습니다 (엑셀에서 열려 있으면 쓸 수 없습니다).\n{e}",
                               [("창에 남기 (엑셀을 닫고 다시 닫기)", "reject"), ("저장하지 않고 닫기", "destructive")], default=0)
                if c != 1:
                    return
        super().reject()
