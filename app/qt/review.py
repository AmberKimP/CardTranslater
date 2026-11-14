"""번역 검수 화면: 번역을 칸마다 원문과 비교해 고치고 ‘확인’ 표시를 남긴다.

번역 글은 결과 보기와 같은 저장소(card_edits)에 저장하고, 검수 엑셀은 필요할 때만 내보낸다.
오래 걸릴 수 있는 일(칸 목록 읽기, 미리 보기 그리기, 엑셀 만들기)은 worker.Job으로 작업 스레드에서 한다.
"""

from __future__ import annotations

import html as _html
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
    QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app import service
from app.qt import worker
from app.qt.dialogs import ask_choice, file_label, fit_to_screen, grey_note, hint_bar, make_primary, set_hint, show_error, show_failure
from app.qt.step_common import KEEP_LABEL, FitImage, source_font

MUST_BG, LOOK_BG, OK_BG, EDIT_BG = "#F4B183", "#FFE066", "#D9EAD3", "#CFE2F3"
MUST_STATUS = {"번역 없음", "일본어 남음", "아이콘 불일치", "원문 결론 못 냄"}  # 나머지(용어 불일치·원문 불확실)는 ‘확인해 볼 칸’
VIEWS = [("todo", "확인할 칸"), ("unchecked", "확인 안 한 칸"), ("all", "모든 칸"),
         ("edited", "사람이 고친 칸"), ("checked", "확인한 칸")]
STATUSES = ["번역 없음", "일본어 남음", "아이콘 불일치", "원문 결론 못 냄", "용어 불일치", "원문 불확실", "엑셀과 앱이 다름"]
CONFLICT = "엑셀과 앱이 다름"
HELP = "Ctrl+S 저장 · Alt+↑/↓ 칸 이동 · Ctrl+F 찾기"
GUIDE = "주황 칸부터 고치세요. 번역이 맞으면 고치지 않고 Ctrl+Enter = 확인하고 다음 칸."
NO_PREVIEW_ERASED = "글자를 지운 뒤에 한글 넣은 모습을 볼 수 있습니다 (⑦ 글자 지우기)"
CHECK_MS, PREVIEW_MS, SEARCH_MS = 150, 600, 250


def render_preview(file_dir, page: int, region: str, text: str):
    """작업 스레드에서: 칸의 글꼴·크기대로 편집 중인 글을 한글 넣기처럼 그린다 (PNG bytes | None)."""
    info = service.card_edit_info(file_dir, page, region)
    return service.card_preview(file_dir, page, region, text, info["font"] or None, info["size"], None, None)


def _chk_of(r: dict) -> dict:
    return {"status": r["status"], "level": r["level"], "missing_terms": r["missing_terms"], "terms": r["terms"]}


class TranslationReviewDialog(QDialog):
    """preset: ("all",) | ("group", 그룹 폴더) | ("file", 파일 폴더) — 처음 고를 범위."""

    COLS = ["", "카드", "영역", "번역"]

    def __init__(self, project_dir: Path | str, parent=None, preset: tuple | None = None):
        super().__init__(parent)
        self.setWindowTitle("번역 검수")
        fit_to_screen(self, 1400, 900)
        self.pdir = Path(project_dir)
        self.rows: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.view_ids: list[str] = []
        self.excel: dict = {"path": None, "open": False}
        self.counts: dict = {"total": 0, "checked": 0, "edited": 0, "must_left": 0, "look_left": 0}
        self.saved_ids: set[str] = set()  # 이 창에서 글을 고쳐 저장한 칸
        self.saved_composed = False  # 그중 이미 한글을 넣은 파일의 칸이 있나
        self.multi_file = False
        self._busy = False  # 표를 다시 채우는 동안 칸 바뀜을 무시
        self._loading_text = False
        self._shown_id: str | None = None
        self._orig_text = ""
        self._pv_gen = 0
        self._pv_pending = 0
        self._closed = False
        self._first = True
        self._text_font = source_font(self.font(), 1)

        # ---- 위: 거르기
        self._scopes = self._build_scopes()
        self.scope = self._combo(10)
        for label, _kind, _dirs in self._scopes:
            self.scope.addItem(label)
        self.scope.setCurrentIndex(self._scope_index(preset))
        self.view = self._combo(8)
        for key, label in VIEWS:
            self.view.addItem(label, key)
        self.status = self._combo(8)
        self.status.addItem("모든 상태")
        for s in STATUSES:
            self.status.addItem(s)
        self.region = self._combo(6)
        self.region.addItem("모두")
        self.search = QLineEdit()
        self.search.setPlaceholderText("원문·번역 찾기 (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(120)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_MS)
        self._search_timer.timeout.connect(self._on_filter_changed)
        top = QHBoxLayout()
        top.setSpacing(6)
        for text, w in (("범위", self.scope), ("보기", self.view), ("상태", self.status), ("영역", self.region)):
            top.addWidget(QLabel(text))
            top.addWidget(w)
            top.addSpacing(4)
        top.addWidget(self.search, 1)
        self.guide = hint_bar(GUIDE, "info")
        self.pill = QLabel()
        self.pill.setStyleSheet("color: #3A3A3C; background: white; border: 1px solid #E5E5EA;"
                                " border-radius: 10px; padding: 3px 10px;")
        guide_row = QHBoxLayout()
        guide_row.addWidget(self.guide, 1)
        guide_row.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignTop)

        # ---- 왼쪽: 표
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 30)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.currentCellChanged.connect(self._on_cell_changed)
        legend = QLabel(
            f'<span style="background:{MUST_BG}">&nbsp;●&nbsp;</span> 꼭 고칠 칸 &nbsp;'
            f'<span style="background:{LOOK_BG}">&nbsp;●&nbsp;</span> 확인해 볼 칸 &nbsp;'
            f'<span style="background:{OK_BG}">&nbsp;✓&nbsp;</span> 확인함 &nbsp;'
            f'<span style="background:{EDIT_BG}">&nbsp;&nbsp;&nbsp;</span> 사람이 고친 번역')
        legend.setWordWrap(True)
        legend.setStyleSheet("color: #6E6E73")
        left = QWidget()
        left.setMinimumWidth(420)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        ll.addWidget(self.table, 1)
        ll.addWidget(legend)

        # ---- 오른쪽: 편집
        self.head = QLabel()
        self.head.setStyleSheet("font-weight: bold; font-size: 11pt")
        self.pos = QLabel()
        self.pos.setStyleSheet("color: #6E6E73")
        head = QHBoxLayout()
        head.addWidget(self.head, 1)
        head.addWidget(self.pos)
        self.crop = FitImage(120)
        self.preview = FitImage(120)
        self.pv_note = grey_note("")
        self.pv_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pics = QHBoxLayout()
        for title, img, note in (("원문", self.crop, None), ("한글 넣은 모습", self.preview, self.pv_note)):
            col = QVBoxLayout()
            col.setSpacing(2)
            cap = QLabel(title)
            cap.setStyleSheet("color: #3A3A3C; font-weight: bold")
            col.addWidget(cap)
            col.addWidget(img, 1)
            if note is not None:
                col.addWidget(note)
            else:
                col.addSpacing(self.pv_note.sizeHint().height())
            pics.addLayout(col, 1)
        self.source = QPlainTextEdit()
        self.source.setReadOnly(True)
        self.source.setFont(self._text_font)
        self.source.setFixedHeight(54)
        self.source_note = grey_note("원문이 틀렸으면 ③ 원문 확인에서 고치세요 (고치면 그 칸은 다시 번역해야 합니다)")
        self.base_name = self._line_title("초벌")
        self.base_text = QLabel()
        self.base_text.setWordWrap(True)
        self.base_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.base_text.setFont(self._text_font)
        self.base_btn = QPushButton("초벌 번역으로")
        self.base_btn.clicked.connect(self.use_base)
        base_row = QHBoxLayout()
        base_row.addWidget(self.base_name, 0, Qt.AlignmentFlag.AlignTop)
        base_row.addWidget(self.base_text, 1)
        base_row.addWidget(self.base_btn, 0, Qt.AlignmentFlag.AlignTop)
        self.conflict = QFrame()
        cl = QVBoxLayout(self.conflict)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(4)
        self.conflict_msg = hint_bar("앱에서 고친 뒤 이 칸의 번역이 바뀌었습니다 (엑셀에서 고쳤거나 다시 번역함). "
                                     "지금은 바뀐 번역을 씁니다.", "todo")
        self.keep_app = QPushButton("앱에서 고친 것 쓰기")
        self.keep_now = QPushButton("바뀐 번역 쓰기")
        self.keep_app.clicked.connect(lambda: self.resolve_conflict("app"))
        self.keep_now.clicked.connect(lambda: self.resolve_conflict("now"))
        cb = QHBoxLayout()
        cb.addWidget(self.keep_app)
        cb.addWidget(self.keep_now)
        cb.addStretch(1)
        cl.addWidget(self.conflict_msg)
        cl.addLayout(cb)
        self.edit = QPlainTextEdit()
        self.edit.setFont(self._text_font)
        self.edit.setMinimumHeight(76)
        self.edit.setMaximumHeight(130)
        self.edit.setTabChangesFocus(True)
        self.edit.textChanged.connect(self._on_text_changed)
        self.check_label = QLabel()
        self.check_label.setTextFormat(Qt.TextFormat.RichText)
        self.check_label.setWordWrap(True)
        self.same_box = QCheckBox()
        self.same_box.setChecked(True)
        self.next_btn = QPushButton("확인하고 다음 칸 (Ctrl+Enter)")
        make_primary(self.next_btn)
        self.save_btn = QPushButton("저장")
        self.save_btn.setToolTip("Ctrl+S — 저장하고 확인 표시를 남기며, 이 칸에 머뭅니다")
        self.preview_btn = QPushButton("미리 보기")
        self.preview_btn.setToolTip("Ctrl+P — 지금 글로 한글 넣은 모습을 다시 그립니다")
        self.next_btn.clicked.connect(self.confirm_next)
        self.save_btn.clicked.connect(self.save_here)
        self.preview_btn.clicked.connect(self.request_preview)
        self.keep_btn = QPushButton(KEEP_LABEL)
        self.keep_btn.setToolTip("이 칸은 글자 없이 아이콘만 있다: 번역하지 않고 글자 지우기·한글 넣기도 하지 않아 원본 그대로 둡니다.\n"
                                 "이 목록에서 빠지며, 되돌리려면 ③ 원문 확인이나 결과 보기의 같은 버튼을 누르세요.")
        self.keep_btn.clicked.connect(self.keep_original)
        btns = QHBoxLayout()
        btns.addWidget(self.next_btn)
        btns.addWidget(self.save_btn)
        btns.addWidget(self.preview_btn)
        btns.addWidget(self.keep_btn)
        self.help = grey_note(HELP)
        self.help.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        btns.addWidget(self.help, 1)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 8, 0)
        rl.setSpacing(6)
        rl.addLayout(head)
        rl.addLayout(pics, 1)
        rl.addLayout(self._labeled("원문", self.source))
        rl.addWidget(self.source_note)
        rl.addLayout(base_row)
        rl.addWidget(self.conflict)
        rl.addLayout(self._labeled("번역", self.edit))
        rl.addWidget(self.check_label)
        rl.addWidget(self.same_box)
        rl.addLayout(btns)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(560)
        scroll.setWidget(right)
        self.split = QSplitter()
        self.split.addWidget(left)
        self.split.addWidget(scroll)
        self.split.setStretchFactor(0, 2)
        self.split.setStretchFactor(1, 3)
        self.split.setSizes([560, 840])

        # ---- 아래: 엑셀 줄
        self.excel_hint = hint_bar("엑셀이 열려 있습니다. 엑셀에서 저장한 칸이 앱에서 고친 것보다 우선합니다. 한쪽에서만 고치세요.", "warn")
        self.excel_label = QLabel()
        self.excel_label.setWordWrap(True)
        self.b_xl_open = QPushButton("엑셀 열기")
        self.b_xl_make = QPushButton("엑셀로 내보내기…")
        self.b_xl_open.clicked.connect(self.open_excel)
        self.b_xl_make.clicked.connect(self.export_excel)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addWidget(self.excel_label, 1)
        bottom.addWidget(self.b_xl_open)
        bottom.addWidget(self.b_xl_make)
        bottom.addSpacing(12)
        bottom.addWidget(close)
        for b in (self.next_btn, self.save_btn, self.preview_btn, self.base_btn, self.keep_app, self.keep_now,
                  self.b_xl_open, self.b_xl_make, close):
            b.setAutoDefault(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(8)
        lay.addLayout(top)
        lay.addLayout(guide_row)
        lay.addWidget(self.split, 1)
        lay.addWidget(self.excel_hint)
        lay.addLayout(bottom)

        for keys, fn in (("Ctrl+Return", self.confirm_next), ("Ctrl+Enter", self.confirm_next), ("Ctrl+S", self.save_here),
                         ("Ctrl+P", self.request_preview), ("Ctrl+F", self.focus_search),
                         ("Alt+Down", lambda: self.move_row(1)), ("Alt+Up", lambda: self.move_row(-1))):
            QShortcut(QKeySequence(keys), self, activated=fn)
        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(CHECK_MS)
        self._check_timer.timeout.connect(self._run_check)
        self._pv_timer = QTimer(self)
        self._pv_timer.setSingleShot(True)
        self._pv_timer.setInterval(PREVIEW_MS)
        self._pv_timer.timeout.connect(self.request_preview)
        self._last = self._snapshot()
        self.scope.currentIndexChanged.connect(self._on_scope_changed)
        for c in (self.view, self.status, self.region):
            c.currentIndexChanged.connect(self._on_filter_changed)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        self.search.returnPressed.connect(self._on_filter_changed)
        self._show_empty()
        self._sync_excel()
        self.load()

    # ================================================================ 만들기 도우미
    @staticmethod
    def _combo(min_chars: int) -> QComboBox:
        c = QComboBox()
        c.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        c.setMinimumContentsLength(min_chars)
        return c

    @staticmethod
    def _line_title(text: str) -> QLabel:
        lb = QLabel(text)
        lb.setStyleSheet("color: #3A3A3C; font-weight: bold")
        lb.setFixedWidth(40)
        lb.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        return lb

    @classmethod
    def _labeled(cls, text: str, w: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(cls._line_title(text), 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(w, 1)
        return row

    def _build_scopes(self) -> list[tuple[str, str, list | None]]:
        """범위 콤보 항목 [(글자, 종류, 파일 폴더 목록)]. 번역한 파일만. 모든 그룹은 번역한 파일 전부."""
        ov = service.project_overview(self.pdir)
        out: list[tuple[str, str, list | None]] = []
        all_dirs: list = []
        parts: list[tuple[str, str, list]] = []
        for g in ov["groups"]:
            fs = [f for f in g["files"] if f.get("translated")]
            if not fs:
                continue
            all_dirs += [f["dir"] for f in fs]
            parts.append((f"그룹 {g['name']}", "group:" + str(g["dir"]), [f["dir"] for f in fs]))
            if len(fs) > 1:
                parts += [(f"파일 {file_label(f['key'])}", "file:" + str(f["dir"]), [f["dir"]]) for f in fs]
            else:
                parts.append((f"파일 {file_label(fs[0]['key'])}", "file:" + str(fs[0]["dir"]), [fs[0]["dir"]]))
        out.append(("모든 그룹", "all", all_dirs or None))
        out += parts
        return out

    def _scope_index(self, preset: tuple | None) -> int:
        if preset and preset[0] in ("group", "file"):
            want = f"{preset[0]}:{preset[1]}"
            for i, (_l, kind, _d) in enumerate(self._scopes):
                if kind == want:
                    return i
        return 0

    def scope_dirs(self) -> list | None:
        return self._scopes[self.scope.currentIndex()][2]

    # ================================================================ 불러오기
    def _alive(self) -> bool:
        if self._closed:
            return False
        try:
            self.isVisible()
        except RuntimeError:  # 창이 이미 지워짐
            return False
        return True

    def load(self, keep_id: str | None = None):
        """칸 목록을 service에서 다시 읽는다 (작업 스레드)."""
        job = worker.Job(service.review_rows, self.pdir, files=self.scope_dirs(), with_progress=False)
        worker.run_with_dialog(self, "번역 검수 칸을 불러오는 중", job, lambda r: self._loaded(r, keep_id),
                               lambda m: self._load_failed(m), cancellable=False)

    def _load_failed(self, msg: str):
        if self._alive():
            show_failure(self, "번역 검수 칸을 불러오지 못했습니다", msg)

    def _loaded(self, r: dict, keep_id: str | None):
        if not self._alive():
            return
        self.rows = r["rows"]
        self.by_id = {x["id"]: x for x in self.rows}
        self.excel = r["excel"]
        self.multi_file = len({x["file_key"] for x in self.rows}) > 1
        self._shown_id = None
        names: list[str] = []
        for x in self.rows:
            if x["region"] not in names:
                names.append(x["region"])
        self._busy = True
        try:
            self.region.clear()
            self.region.addItem("모두")
            for n in names:
                self.region.addItem(n)
            want = self._last.get("region_name")
            if want in names:
                self.region.setCurrentIndex(names.index(want) + 1)
            if self._first:  # 처음에는 할 일이 있는 보기로
                self._first = False
                todo = sum(1 for x in self.rows if self._passes_view(x, "todo"))
                unchecked = sum(1 for x in self.rows if not x["checked"])
                self.view.setCurrentIndex(0 if todo else 1 if unchecked else 2)
        finally:
            self._busy = False
        self._recount()
        self._sync_excel()
        self._apply_filter(keep_id=keep_id, force=True)

    # ================================================================ 거르기
    def _snapshot(self) -> dict:
        return {"scope": self.scope.currentIndex(), "view": self.view.currentIndex(), "status": self.status.currentIndex(),
                "region": self.region.currentIndex(), "region_name": self.region.currentText() if self.region.currentIndex() > 0 else None,
                "search": self.search.text()}

    def _restore_filters(self):
        """고른 걸 취소하고 거르기 칸을 직전 값으로."""
        for w, key in ((self.scope, "scope"), (self.view, "view"), (self.status, "status"), (self.region, "region")):
            w.blockSignals(True)
            w.setCurrentIndex(self._last[key])
            w.blockSignals(False)
        self.search.blockSignals(True)
        self.search.setText(self._last["search"])
        self.search.blockSignals(False)

    @staticmethod
    def _passes_view(r: dict, key: str) -> bool:
        if key == "todo":
            return not r["checked"] and r["level"] in ("must", "look")
        if key == "unchecked":
            return not r["checked"]
        if key == "edited":
            return bool(r["edited"] or r["excel_changed"])
        if key == "checked":
            return bool(r["checked"])
        return True

    def _passes(self, r: dict) -> bool:
        if not self._passes_view(r, self.view.currentData()):
            return False
        st = self.status.currentText() if self.status.currentIndex() > 0 else None
        if st == CONFLICT:
            if not r["edit_conflict"]:
                return False
        elif st and st not in r["status"]:
            return False
        if self.region.currentIndex() > 0 and r["region"] != self.region.currentText():
            return False
        q = self.search.text().strip().lower()
        if q and not any(q in (r.get(k) or "").lower() for k in ("source", "ko", "draft", "base")):
            return False
        return True

    def _filtered(self) -> list[dict]:
        return [r for r in self.rows if self._passes(r)]

    def _on_scope_changed(self, _idx=None):
        if self._busy or not self._alive():
            return
        if not self._confirm_leave():
            self._restore_filters()
            return
        self._last = self._snapshot() | {"region_name": None}
        self._busy = True
        self.region.setCurrentIndex(0)
        self._busy = False
        self.load()

    def _on_filter_changed(self, _idx=None):
        if self._busy or not self._alive():
            return
        self._search_timer.stop()
        snap = self._snapshot()
        if snap == self._last:
            return
        self._apply_filter()

    def _apply_filter(self, keep_id: str | None = None, force: bool = False):
        """지금 거르기 조건대로 표를 다시 만든다. 보던 칸이 목록에서 빠지면(그리고 고친 글이 있으면) 먼저 묻는다."""
        ids = [r["id"] for r in self._filtered()]
        cur = keep_id or self._shown_id
        if not force and self._shown_id and self._shown_id not in ids and not self._confirm_leave():
            self._restore_filters()
            return False
        self._last = self._snapshot()
        self.view_ids = ids
        self._fill_table()
        target = cur if cur in ids else (ids[0] if ids else None)
        self._select_id(target, same_ok=(target == self._shown_id))
        return True

    # ================================================================ 표
    def _card_text(self, r: dict) -> str:
        return (file_label(r["file_key"]) + " " if self.multi_file else "") + f"{r['page']}쪽"

    def _fill_table(self):
        self._busy = True
        try:
            self.table.setRowCount(len(self.view_ids))
            for i, rid in enumerate(self.view_ids):
                self._fill_row(i, self.by_id[rid])
            self.table.resizeColumnToContents(1)
        finally:
            self._busy = False
        self._sync_pill()

    def _fill_row(self, i: int, r: dict):
        mark = "✓" if r["checked"] else "●" if r["level"] in ("must", "look") else ""
        vals = [mark, self._card_text(r), r["region"], r["ko"].replace("\n", " / ")]
        for j, v in enumerate(vals):
            it = QTableWidgetItem(v)
            if j == 0:
                it.setData(Qt.ItemDataRole.UserRole, r["id"])
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                color = OK_BG if r["checked"] else MUST_BG if r["level"] == "must" else LOOK_BG if r["level"] == "look" else None
                if color:
                    it.setBackground(QColor(color))
                if r["checked_stale"]:
                    it.setToolTip("확인한 뒤 바뀜")
                elif not r["checked"] and r["status"]:
                    it.setToolTip(", ".join(r["status"]))
            else:
                it.setToolTip(r["source"])
            if j == 3:
                it.setFont(self._text_font)
                if r["edited"] or r["excel_changed"]:
                    it.setBackground(QColor(EDIT_BG))
                if r["checked_stale"]:
                    it.setToolTip("확인한 뒤 바뀜\n" + r["source"])
            self.table.setItem(i, j, it)

    def _row_index(self, rid: str | None) -> int:
        return self.view_ids.index(rid) if rid in self.view_ids else -1

    def _select_id(self, rid: str | None, same_ok: bool = False):
        """표에서 그 칸을 고르고 편집 칸에 띄운다 (same_ok: 이미 띄운 칸이면 편집 중인 글을 그대로 둔다)."""
        i = self._row_index(rid)
        self._busy = True
        try:
            if i >= 0:
                self.table.setCurrentCell(i, 0)
            else:
                self.table.clearSelection()
                self.table.setCurrentCell(-1, -1)
        finally:
            self._busy = False
        if i < 0:
            self._shown_id = None
            self._show_empty()
        elif not (same_ok and rid == self._shown_id):
            self._show_row(self.by_id[rid])

    def current_row(self) -> dict | None:
        return self.by_id.get(self._shown_id) if self._shown_id else None

    def _on_cell_changed(self, cur, _c, prev, _pc):
        if self._busy or not self._alive():
            return
        rid = self.view_ids[cur] if 0 <= cur < len(self.view_ids) else None
        if rid is None or rid == self._shown_id:
            return
        if not self._confirm_leave():
            self._busy = True
            try:
                self.table.setCurrentCell(prev, 0)
            finally:
                self._busy = False
            return
        self._show_row(self.by_id[rid])

    def move_row(self, d: int):
        i = self._row_index(self._shown_id) + d
        if 0 <= i < len(self.view_ids):
            self.table.setCurrentCell(i, 0)

    def _refresh_rows(self, new_rows: list[dict]):
        """service가 돌려준 새 줄로 바꾸고, 보이는 줄과 숫자만 다시 그린다 (거르기에서 빠져도 줄은 그대로 둔다)."""
        idx = {x["id"]: n for n, x in enumerate(self.rows)}
        for r in new_rows:
            if r["id"] in idx:
                self.rows[idx[r["id"]]] = r
            self.by_id[r["id"]] = r
        self._recount()
        self._busy = True
        try:
            for r in new_rows:
                i = self._row_index(r["id"])
                if i >= 0:
                    self._fill_row(i, r)
        finally:
            self._busy = False
        self._sync_pill()

    def _recount(self):
        """같은 원문 칸 수와 진행 숫자를 지금 줄들로 다시 센다 (service.review_rows와 같은 계산)."""
        seen: dict[tuple, int] = {}
        for r in self.rows:
            k = (r["source"], r["ko"])
            seen[k] = seen.get(k, 0) + 1
        for r in self.rows:
            r["same_source"] = seen[(r["source"], r["ko"])] - 1
        rs = self.rows
        self.counts = {"total": len(rs), "checked": sum(1 for r in rs if r["checked"]),
                       "edited": sum(1 for r in rs if r["edited"]),
                       "must_left": sum(1 for r in rs if r["level"] == "must" and not r["checked"]),
                       "look_left": sum(1 for r in rs if r["level"] == "look" and not r["checked"])}

    def _sync_pill(self):
        c = self.counts
        self.pill.setText(f"확인 {c['checked']}/{c['total']} · 꼭 고칠 칸 {c['must_left']}"
                          + (f" · 확인해 볼 칸 {c['look_left']}" if c["look_left"] else ""))
        self.pos.setText(f"{self._row_index(self._shown_id) + 1} / {len(self.view_ids)}" if self._shown_id else
                         f"0 / {len(self.view_ids)}")

    # ================================================================ 편집 칸
    def _show_empty(self):
        self._loading_text = True
        self.edit.clear()
        self.source.clear()
        self._loading_text = False
        self._orig_text = ""
        self._pv_gen += 1
        self.crop.clear()
        self.preview.clear()
        self.pv_note.setText("")
        self.head.setText("검수할 칸이 없습니다" if not self.rows else "이 거르기 조건에 맞는 칸이 없습니다")
        self.base_name.setText("초벌")
        self.base_text.setText("")
        self.base_btn.setEnabled(False)
        self.conflict.setVisible(False)
        self.check_label.setText("")
        self.same_box.setVisible(False)
        for b in (self.next_btn, self.save_btn, self.preview_btn, self.keep_btn):
            b.setEnabled(False)
        self.edit.setEnabled(False)
        self._sync_pill()

    def _show_row(self, r: dict, keep_text: bool = False):
        """칸 하나를 오른쪽에 띄운다. keep_text면 편집 칸의 글은 그대로 (저장한 뒤 설명만 새로 고칠 때)."""
        same = r["id"] == self._shown_id
        self._shown_id = r["id"]
        self.edit.setEnabled(True)
        for b in (self.next_btn, self.save_btn, self.preview_btn, self.keep_btn):
            b.setEnabled(True)
        self.head.setText(f"{file_label(r['file_key'])} {r['page']}쪽 · {r['region']}")
        self.source.setPlainText(r["source"])
        if not same or not keep_text:
            try:
                pm = QPixmap()
                pm.loadFromData(service.region_crop(r["file_dir"], r["page"], r["region"]), "PNG")
                self.crop.set_image(pm)
            except Exception:  # noqa: BLE001 - 그림만 못 보일 뿐 글은 고칠 수 있다
                self.crop.clear()
        if not keep_text:
            self._loading_text = True
            self.edit.setPlainText(r["ko"])
            self._loading_text = False
            self._orig_text = self.edit.toPlainText()
        tag = r["kind"] == "tag_pills"
        self.edit.setPlaceholderText("한 줄에 태그 하나" if tag else
                                     "이 칸에 넣을 한글 (줄을 바꾸면 그 자리에서 줄을 바꿉니다. 아이콘은 [[이름]] 그대로)")
        xl = r["excel_changed"]
        self.base_name.setText("엑셀" if xl else "초벌")
        self.base_text.setText(r["base"] if xl else r["draft"])
        self.base_btn.setText("엑셀 번역으로" if xl else "초벌 번역으로")
        self.base_btn.setEnabled(True)
        self.conflict.setVisible(bool(r["edit_conflict"]))
        n = r["same_source"]
        self.same_box.setVisible(n > 0)
        if n > 0:
            self.same_box.setText(f"같은 원문 칸 {n}개에도 똑같이 넣기")
            if not same:
                self.same_box.setChecked(True)
        self._show_check(_chk_of(r) if self.edit.toPlainText() == r["ko"] else self._fresh_check(r))
        i = self._row_index(r["id"])
        if i >= 0:
            self.table.scrollToItem(self.table.item(i, 0))
        self._sync_pill()
        if not keep_text:
            self.request_preview()

    # ---- 검사
    def _fresh_check(self, r: dict) -> dict:
        try:
            return service.check_review_text(r["file_dir"], r["page"], r["region"], self.edit.toPlainText())
        except Exception:  # noqa: BLE001
            return _chk_of(r)

    @staticmethod
    def _chk_html(chk: dict) -> str:
        chips = []
        for s in chk["status"]:
            bg = MUST_BG if s in MUST_STATUS else LOOK_BG
            chips.append(f'<span style="background:{bg}">&nbsp;● {_html.escape(s)}&nbsp;</span>')
        out = " ".join(chips) if chips else '<span style="color:#2F5E23">문제 없음</span>'
        if chk["missing_terms"]:
            out += "<br>빠진 용어: " + _html.escape(", ".join(chk["missing_terms"]))
        if chk["terms"]:
            out += "<br>용어 " + " &nbsp; ".join(
                f'{_html.escape(t["source"])}→{_html.escape(t["ko"])} '
                + ('<span style="color:#2F7D32">✓</span>' if t["ok"] else '<span style="color:#C00000">✗</span>')
                for t in chk["terms"])
        return out

    def _show_check(self, chk: dict):
        self.check_label.setText(self._chk_html(chk))

    def _run_check(self):
        r = self.current_row()
        if r is not None and self._alive():
            self._show_check(self._fresh_check(r))

    # ---- 고친 글 지키기
    def _dirty(self) -> bool:
        return self._shown_id is not None and self.edit.toPlainText() != self._orig_text

    def _on_text_changed(self):
        if self._loading_text or self._shown_id is None:
            return
        self._check_timer.start()
        self._pv_timer.start()

    def _ask_save(self) -> str:
        """고친 글이 있으면 묻는다. 'save' | 'discard' | 'cancel' (고친 글이 없으면 'discard')."""
        if not self._dirty():
            return "discard"
        r = ask_choice(self, "고친 번역", "고친 번역을 저장할까요?",
                       [("저장", "accept"), ("버리기", "destructive"), ("취소", "reject")], default=0)
        return {0: "save", 1: "discard"}.get(r, "cancel")

    def _confirm_leave(self) -> bool:
        a = self._ask_save()
        if a == "save":
            return self._commit(advance=False)
        return a != "cancel"

    # ---- 저장·확인
    def _commit(self, advance: bool) -> bool:
        """글이 바뀌었으면 저장하고, 확인 표시를 남긴다. 안 바뀌었으면 확인만. 취소하면 False."""
        r = self.current_row()
        if r is None:
            return True
        changed = self._dirty()
        text = self.edit.toPlainText()
        if changed and not text.strip():
            c = ask_choice(self, "번역이 비어 있습니다", "번역이 비어 있습니다. 이 칸은 비운 채로 둘까요?",
                           [("비운 채로 두기", "accept"), ("돌아가 고치기", "reject")], default=1)
            if c != 0:
                return False
        else:
            chk = self._fresh_check(r) if changed else _chk_of(r)
            # 이 창에서 고칠 수 있는 것만 묻는다 (‘원문 결론 못 냄’은 ③ 원문 확인에서 고칠 일이라 칸마다 묻지 않음)
            musts = [s for s in chk["status"] if s in MUST_STATUS and s != "원문 결론 못 냄"]
            if musts:
                c = ask_choice(self, "확인 전에", f"아직 ‘{', '.join(musts)}’이(가) 있습니다. 이대로 확인할까요?",
                               [("이대로 확인", "accept"), ("돌아가 고치기", "reject")], default=1)
                if c != 0:
                    return False
        same = bool(changed and r["same_source"] > 0 and self.same_box.isChecked())
        try:
            new = service.save_review(r["file_dir"], r["page"], r["region"], text if changed else None, checked=True,
                                      same_source=same, scope_files=self.scope_dirs())
        except Exception as e:  # noqa: BLE001
            show_failure(self, "번역을 저장하지 못했습니다", str(e))
            return False
        if changed:
            for x in new:
                self.saved_ids.add(x["id"])
                self.saved_composed = self.saved_composed or bool(x["composed"])
        self._orig_text = text  # 더 묻지 않게
        self._refresh_rows(new)
        if advance:
            i = self._row_index(r["id"])
            if 0 <= i + 1 < len(self.view_ids):
                self.table.setCurrentCell(i + 1, 0)
                self.edit.setFocus()
                return True
        cur = self.current_row()
        if cur is not None:
            self._show_row(cur, keep_text=True)
        return True

    def confirm_next(self):
        self._commit(advance=True)

    def save_here(self):
        self._commit(advance=False)

    def keep_original(self):
        """‘아이콘만 있음 · 원본 그대로’: 이 칸을 번역·글자 지우기·한글 넣기에서 빼고 원본 그대로 둔다.
        목록에서 빠지고 다음 칸을 보여 준다 (이미 지운·한글 넣은 결과는 다음 지우기·한글 넣기 때 바뀐다)."""
        r = self.current_row()
        if r is None:
            return
        try:
            service.set_keep_original(r["file_dir"], r["page"], r["region"], True)
        except Exception as e:  # noqa: BLE001
            show_error(self, "바꾸지 못했습니다", e)
            return
        ids = self.view_ids
        i = ids.index(r["id"]) if r["id"] in ids else -1
        nxt = next((x for x in ids[i + 1:] + ids[:max(i, 0)] if x != r["id"]), None)
        self._orig_text = self.edit.toPlainText()  # 저장 안 한 글을 묻지 않는다 (번역하지 않는 칸이 됨)
        self.load(keep_id=nxt)

    def use_base(self):
        """편집 칸만 초벌(또는 엑셀) 번역으로 되돌린다. 저장해야 반영된다."""
        r = self.current_row()
        if r is not None:
            self.edit.setPlainText(r["base"])
            self.edit.setFocus()

    def resolve_conflict(self, keep: str):
        r = self.current_row()
        if r is None or not r["edit_conflict"] or not self._confirm_leave():
            return
        try:
            new = service.resolve_review_conflict(r["file_dir"], r["page"], r["region"], keep)
        except Exception as e:  # noqa: BLE001
            show_failure(self, "고르지 못했습니다", str(e))
            return
        if new:
            self._refresh_rows([new])
            self._show_row(new)

    # ---- 표 오른쪽 클릭
    def _context_menu(self, pos):
        it = self.table.itemAt(pos)
        if it is None:
            return
        rid = self.table.item(it.row(), 0).data(Qt.ItemDataRole.UserRole)
        r = self.by_id.get(rid)
        if r is None:
            return
        menu = QMenu(self)
        a1 = menu.addAction("확인 취소", lambda: self.uncheck(rid))
        a1.setEnabled(bool(r["checked"] or r["checked_stale"]))
        a2 = menu.addAction("초벌 번역으로 되돌리기", lambda: self.revert_to_base(rid))
        a2.setEnabled(bool(r["edited"]))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _go(self, rid: str) -> bool:
        """그 칸으로 옮긴다 (고친 글이 있으면 묻고, 취소하면 False)."""
        if rid != self._shown_id:
            self.table.setCurrentCell(self._row_index(rid), 0)
        return self._shown_id == rid

    def uncheck(self, rid: str):
        r = self.by_id.get(rid)
        if r is None or not self._go(rid):
            return
        new = service.set_review_checked(r["file_dir"], r["page"], r["region"], False)
        self._refresh_rows([new])
        self._show_row(new, keep_text=True)

    def revert_to_base(self, rid: str):
        """저장된 고친 번역을 지우고 초벌(엑셀) 번역으로 (card_edits에서 지워짐)."""
        r = self.by_id.get(rid)
        if r is None or not self._go(rid):
            return
        new = service.save_review(r["file_dir"], r["page"], r["region"], r["base"], checked=False)
        self._refresh_rows(new)
        cur = self.current_row()
        if cur is not None:
            self._show_row(cur)

    # ================================================================ 한글 넣은 모습
    def request_preview(self):
        """지금 글로 한글 넣은 모습을 다시 그린다 (작업 스레드. 늦게 온 옛 결과는 버린다)."""
        self._pv_timer.stop()
        r = self.current_row()
        if r is None or not self._alive():
            return
        self._pv_gen += 1
        if not r["erased"]:
            self.preview.clear()
            self.pv_note.setText(NO_PREVIEW_ERASED)
            return
        gen = self._pv_gen
        self.pv_note.setText("그리는 중…")
        job = worker.Job(render_preview, r["file_dir"], r["page"], r["region"], self.edit.toPlainText(), with_progress=False)
        job.signals.done.connect(lambda png, g=gen: self._preview_done(g, png))
        job.signals.failed.connect(lambda _m, g=gen: self._preview_done(g, None))
        worker.start(job)

    def _preview_done(self, gen: int, png):
        if not self._alive() or gen != self._pv_gen:
            return
        pm = QPixmap()
        if png and pm.loadFromData(png, "PNG"):
            self.preview.set_image(pm)
            self.pv_note.setText("")
        else:
            self.preview.clear()
            self.pv_note.setText("그려 볼 번역이 없습니다")

    # ================================================================ 엑셀 줄
    def _sync_excel(self):
        has = bool(self.excel.get("path"))
        self.excel_label.setText("검수 엑셀이 있습니다. 엑셀에서 고친 번역도 쓰고, 같은 칸을 엑셀에서 나중에 고치면 엑셀이 우선합니다"
                                 if has else "검수 엑셀: 없음")
        self.b_xl_open.setVisible(has)
        self.b_xl_make.setText("엑셀 다시 만들기" if has else "엑셀로 내보내기…")
        self.excel_hint.setVisible(bool(self.excel.get("open")))

    def export_excel(self):
        if not self._confirm_leave():
            return
        job = worker.Job(service.export_project_excel, self.pdir, force=True, with_progress=False)
        worker.run_with_dialog(self, "번역 검수 엑셀 만드는 중", job, self._excel_made,
                               lambda m: show_failure(self, "검수 엑셀을 만들지 못했습니다", m) if self._alive() else None,
                               cancellable=False)

    def _excel_made(self, _paths):
        if self._alive():
            self.load(keep_id=self._shown_id)

    def open_excel(self):
        from app.qt.main import open_path

        if self.excel.get("path"):
            open_path(Path(self.excel["path"]).resolve())

    # ================================================================ 그 밖에
    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def closing_message(self) -> str:
        """닫은 뒤 상태줄에 보일 말 (고친 칸이 없으면 빈 글)."""
        n = len(self.saved_ids)
        if not n:
            return ""
        return f"고친 칸 {n}개는 다음 ⑧ 한글 넣기부터 들어갑니다" if not self.saved_composed else \
            f"고친 칸 {n}개는 ⑧ 한글 넣기를 다시 하세요 (이미 한글을 넣은 파일이 있습니다)"

    def done(self, r):
        if not self._confirm_leave():
            return
        self._closed = True
        self._pv_gen += 1
        for t in (self._check_timer, self._pv_timer, self._search_timer):
            t.stop()
        super().done(r)
