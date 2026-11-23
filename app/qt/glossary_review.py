"""③ 용어집 창: 모은 용어 후보의 번역을 프로그램 안에서 확인·고쳐 확정한다 (‘용어 후보 다시 만들기…’로 후보를 새로 모은다).

엑셀(용어집_검증.xlsx)은 따로 둔다: ‘엑셀로 내보내기’로 표 내용을 엑셀에 쓰고, ‘엑셀에서 가져오기’로 엑셀에서 고친
것을 그대로 확정할 수 있다 (예전 흐름). 화면에서 고친 값은 닫을 때 검증 엑셀에 저장해 다음에 이어 볼 수 있다."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app import service
from app.qt import ui_codes
from app.qt.dialogs import PRIMARY_QSS, fit_to_screen, grey_note, hint_bar, page_title, set_hint, show_error
from app.qt.step_common import FitImage, source_font
from app.qt.work_layout import FlashLabel, NavBox, adopt, chip_qss, make_big_primary, panel, shortcut_hint

# 표 열 = 검증 엑셀 열 (같은 이름이라 같은 검사를 쓴다). 고칠 수 있는 열만 편집된다
COLS = ["원문 용어", "번역", "추천 번역", "원문 상태", "번역 확인", "다른 후보", "분류", "등장 횟수", "예문", "변형", "메모"]
EDITABLE = {"원문 용어", "번역", "분류", "변형", "메모"}
C = {h: i for i, h in enumerate(COLS)}
FILL = {"edit": "#FFF8DC", "done": "#D9EAD3", "no_verdict": "#FF7C80", "unsure": "#FFE066", "check": "#D9D2E9"}
LIST_COLS = ("원문 용어", "번역", "등장 횟수")  # 나눠 보기에서 가운데 목록에 보이는 열. ‘표로 보기’(기본)는 모든 열
LIST_W, RIGHT_W = 520, 420  # 나눠 보기의 가운데 목록·오른쪽 고치기 칸 폭 (px)
START_TEXT = "번역 시작"  # 용어집을 확정하고 바로 ④ 전체 번역으로
NEXT_TEXT = "확인 → 다음 볼 것"
NAV_HINT = "Alt+←/→ 용어 · Alt+Shift+←/→ 볼 것"
NAV_SPEC = [("prev", "◀ 이전 용어", "목록의 앞 용어로", "Alt+Left"),
            ("next", "다음 용어 ▶", "목록의 다음 용어로", "Alt+Right"),
            ("prev_bad", "◀◀ 볼 것", "앞쪽의 아직 확인하지 않은 볼 것(번역 확인·불확실한 원문·번역 빈 칸)으로", "Alt+Shift+Left"),
            ("next_bad", "볼 것 ▶▶", "뒤쪽의 아직 확인하지 않은 볼 것으로", "Alt+Shift+Right")]
NO_TARGET = {"prev": "앞에 다른 용어가 없습니다", "next": "뒤에 다른 용어가 없습니다",
             "prev_bad": "앞쪽에 볼 것이 없습니다", "next_bad": "뒤쪽에 볼 것이 없습니다"}


def _esc(t: str) -> str:
    import html

    return html.escape(str(t))


class _NumItem(QTableWidgetItem):
    """숫자로 정렬되는 칸 (등장 횟수)."""

    def __lt__(self, other):
        try:
            return int(self.text() or 0) < int(other.text() or 0)
        except ValueError:
            return super().__lt__(other)


class GlossaryReviewDialog(QDialog):
    """용어 후보 표. 머리글을 누르면 그 열로 정렬, ‘번역’을 고치고 ‘번역 시작’을 누른다 (용어집을 확정하고 번역으로).
    result: 확정하면 service.GlossaryConfirmResult. translate_next: ‘번역 시작’으로 확정했나 (부른 쪽이 이어서 번역)."""

    def __init__(self, project_dir, rows: list[dict], parent=None):
        super().__init__(parent)
        self.project_dir = project_dir
        self.result = None
        self.rebuild = False  # ‘용어 후보 다시 만들기…’를 눌러 닫았나 (부른 쪽이 후보를 다시 만든다)
        self.translate_next = False  # ‘번역 시작’으로 확정했나 (부른 쪽이 ④ 전체 번역을 이어서 한다)
        self._dirty = False  # 표를 고쳤는가 (고친 것이 있을 때만 닫으면서 검증 엑셀에 쓴다)
        self._from_excel = False  # 엑셀에서 가져오기를 해 봤는가 (그 뒤에는 표로 엑셀을 덮어쓰지 않는다)
        self.setWindowTitle("③ 용어집")
        fit_to_screen(self, 1400, 760, maximize_below=0)
        self._cur = None  # 오른쪽 칸에 띄운 용어의 ‘원문 용어’ 칸 (정렬해도 같은 용어를 가리킨다)
        self._syncing = False  # 오른쪽 칸 ↔ 표를 맞추는 중 (서로 다시 부르지 않게)
        self._places: list[dict] = []
        self._place_i = 0
        self._crop_cache: dict[tuple, QPixmap] = {}
        self._build()
        self._fill(rows)
        self.table.itemChanged.connect(self._mark_dirty)
        self.table.itemChanged.connect(self._table_changed)
        self.table.currentCellChanged.connect(lambda *_a: self._row_picked())
        self._set_split(False)  # 처음은 표로 보기
        # 볼 것이 하나라도 있으면 ‘볼 것만’을 켜고(등장 횟수 많은 순), 없으면 끈 채 모든 용어
        self.only_check.setChecked(any(self._is_look(i) for i in range(self.table.rowCount())))
        self._go_first()

    # ================================================================ 화면 만들기
    # 배치 (카드 보기와 같은 틀, work_layout). 나눠 보기(기본):
    #   머리: 제목 · 개수 칩 · 알림 · (엑셀로 내보내기 · 엑셀에서 가져오기)
    #   왼쪽(무엇): 이 용어가 나온 카드 영역 그림 (◀ 1 / 2 ▶) · 그 원문
    #   가운데(어디): 찾기 · 볼 것만 · 표로 보기 · 용어 목록(원문 · 번역 · 등장 횟수) · 이동 단추 · 용어 더하기 · 고른 줄 빼기
    #   오른쪽(어떻게): 원문 용어 · 번역 · 후보 ‘이것 쓰기’ · 분류 · 변형 · 메모 · 큰 ‘확인 → 다음 볼 것’
    #   아래: (후보 다시 만들기 · 닫기 · 번역 시작) — 다른 창처럼 확정 단추는 아래 오른쪽
    # ‘표로 보기’(처음): 모든 열이 있는 넓은 표 하나 (한꺼번에 고치기). 데이터는 늘 표에 있다.
    # 버튼 코드는 예전 순서(찾기 지우기 · 볼 것만 · 표 · 용어 더하기 · 고른 줄 빼기 · 엑셀로 내보내기 · 엑셀에서 가져오기 ·
    # 후보 다시 만들기 · 용어집 확정 · 닫기)대로 붙이고, 새 단추(이동·표로 보기·확인·나온 곳 넘기기)는 맨 뒤에 붙인다.
    def _build(self) -> None:
        self.guide = hint_bar("", "info")
        self.guide.setWordWrap(True)
        self.search = QLineEdit()
        self.search.setPlaceholderText("찾기 (원문·번역)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        self.only_check = QCheckBox("볼 것만 (번역 확인·불확실한 원문·번역 빈 칸)")
        self.only_check.toggled.connect(self._only_toggled)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAutoScroll(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed
                                   | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.table.verticalHeader().setDefaultSectionSize(26)
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
        self.rebuild_btn = QPushButton("용어 후보 다시 만들기…")
        self.rebuild_btn.setToolTip("원문을 고쳤거나 파일을 더 읽었을 때: 지금 원문으로 용어 후보를 다시 모읍니다.\n"
                                    "표에서 고친 번역은 저장하고 닫은 뒤 만들고, 새 후보에도 같은 원문 용어면 그대로 옮깁니다.\n"
                                    "확정된 용어는 그대로 남습니다 (원문에서 사라진 용어의 고친 번역은 옮기지 않습니다)")
        self.rebuild_btn.clicked.connect(self._rebuild)
        self.ok_btn = QPushButton(START_TEXT)
        self.ok_btn.setStyleSheet(PRIMARY_QSS)
        self.ok_btn.setToolTip("표의 모든 용어로 용어집을 확정하고(번역이 빈 용어는 넣지 않음) 이어서 ④ 전체 번역을 합니다")
        self.ok_btn.clicked.connect(self.start_translate)
        close = QPushButton("닫기 (고친 것 저장)")
        close.clicked.connect(self.reject)
        for b in (self.add_btn, self.del_btn, self.export_btn, self.import_btn, self.rebuild_btn, self.ok_btn, close):
            b.setAutoDefault(False)
        self.title = page_title("③ 용어집", 14)
        self.chips = {k: QLabel() for k in ("check", "unsure", "empty", "done")}
        self.flash_label = FlashLabel()
        self._build_detail()
        body = QWidget()
        ll = QVBoxLayout()
        ll.setSpacing(6)
        ll.addWidget(self.search)
        self._only_row = QHBoxLayout()  # 볼 것만 · (표로 보기 단추는 만든 뒤 붙인다)
        self._only_row.addWidget(self.only_check, 1)
        ll.addLayout(self._only_row)
        ll.addWidget(self.table, 1)
        lb = QHBoxLayout()
        lb.setSpacing(4)
        adopt(body, (self.title, *self.chips.values(), self.guide, self.search, self.only_check, self.table,
                     self.add_btn, self.del_btn, self.export_btn, self.import_btn, self.rebuild_btn, self.ok_btn, close,
                     self.flash_label))
        # 새 단추 (버튼 코드 11~): 이동 4 · 표로 보기 · 확인 → 다음 볼 것 · 나온 곳 ◀ ▶
        self.nav = NavBox(self, NAV_SPEC, self._nav_go, self.flash_label, NO_TARGET)
        self.nav_w = QWidget()
        self.nav_w.setLayout(self.nav.grid)
        self.toggle_btn = QPushButton("표로 보기")
        self.toggle_btn.setAutoDefault(False)
        self.toggle_btn.setToolTip("모든 열이 있는 넓은 표로 봅니다 (여러 용어를 한꺼번에 고칠 때). 다시 누르면 나눠 보기")
        self.toggle_btn.clicked.connect(lambda: self._set_split(not self._split))
        adopt(body, (self.nav_w, self.toggle_btn, self.mid, self.right))
        lb.addWidget(self.add_btn)
        lb.addWidget(self.del_btn)
        lb.addStretch(1)
        ll.addWidget(self.nav_w)
        ll.addLayout(lb)
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(self.title)
        for c in self.chips.values():
            header.addWidget(c)
        header.addWidget(self.flash_label, 1)
        for b in (self.export_btn, self.import_btn):
            header.addWidget(b)
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        bottom.addStretch(1)
        for b in (self.rebuild_btn, close, self.ok_btn):
            bottom.addWidget(b)
        self._only_row.addWidget(self.toggle_btn)
        main = QHBoxLayout()
        main.setSpacing(12)
        self.only_check.setText("볼 것만")
        self.only_check.setToolTip("번역 확인 · 불확실한 원문 · 번역 빈 칸인 용어만")
        self._left_ws = (self.search, self.table, self.nav_w)  # 나눠 보기에서 폭을 LIST_W로
        main.addWidget(self.mid, 1)
        main.addLayout(ll, 1)
        main.addWidget(self.right)
        bl = QVBoxLayout(body)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.setSpacing(8)
        bl.addLayout(header)
        bl.addWidget(self.guide)
        bl.addLayout(main, 1)
        bl.addLayout(bottom)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(body)
        for keys in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(keys), self, activated=self.confirm_next)

    def _build_detail(self) -> None:
        """가운데(나온 곳 그림)와 오른쪽(고치기) 칸: self.mid, self.right."""
        self.place_head = QLabel()
        self.place_head.setStyleSheet("font-size: 15px; font-weight: 600; color: #1D1D1F;")
        # 긴 카드 이름이 창 폭을 늘리지 않게 (왼쪽 칸이 좁아도 목록·고치기 칸 폭은 그대로)
        self.place_head.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.place_prev = QPushButton("◀")
        self.place_next = QPushButton("▶")
        self.place_pos = QLabel()
        self.place_pos.setStyleSheet("color: #6E6E73;")
        for b, d in ((self.place_prev, -1), (self.place_next, 1)):
            b.setFixedWidth(36)
            b.setStyleSheet("QPushButton { padding: 2px 0px; }")  # 테마 여백이면 좁은 단추에서 화살표가 잘린다
            b.setAutoDefault(False)
            b.setToolTip("이 용어가 나온 다른 곳 (예문에 있는 곳만)")
            b.clicked.connect(lambda _c=False, k=d: self._show_place(self._place_i + k))
        self.place_img = FitImage(160)
        self.place_text = QLabel()
        self.place_text.setWordWrap(True)
        self.place_text.setTextFormat(Qt.TextFormat.RichText)
        self.place_text.setFont(source_font(self.place_text.font(), 3))
        self.place_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pic = QWidget()
        pl = QVBoxLayout(pic)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)
        pl.addWidget(self.place_img, 1)
        pl.addWidget(self.place_text)
        ph = QHBoxLayout()
        ph.setSpacing(6)
        ph.addWidget(self.place_head, 1)
        ph.addWidget(self.place_prev)
        ph.addWidget(self.place_pos)
        ph.addWidget(self.place_next)
        self.mid = QWidget()
        ml = QVBoxLayout(self.mid)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(6)
        ml.addLayout(ph)
        ml.addWidget(panel("", pic), 1)
        # 오른쪽: 고치기
        self.f_src = QLineEdit()
        self.f_src.setFont(source_font(self.f_src.font(), 2))
        self.f_meta = grey_note("")
        self.f_meta.setWordWrap(True)
        self.f_ko = QLineEdit()
        self.f_ko.setPlaceholderText("번역 (비우면 이 용어는 용어집에 넣지 않습니다)")
        self.f_cands = QVBoxLayout()
        self.f_cands.setSpacing(4)
        self.f_reason = grey_note("")
        self.f_reason.setWordWrap(True)
        self.f_cat, self.f_var, self.f_note = QLineEdit(), QLineEdit(), QLineEdit()
        self.f_var.setPlaceholderText("쉼표로 나눔 (예: 고블린들, 고블린을)")
        self.fields = {"원문 용어": self.f_src, "번역": self.f_ko, "분류": self.f_cat, "변형": self.f_var, "메모": self.f_note}
        for h, w in self.fields.items():
            w.textEdited.connect(lambda _t, k=h: self._field_edited(k))
        form_w = QWidget()
        fl = QVBoxLayout(form_w)
        fl.setContentsMargins(10, 8, 10, 8)
        fl.setSpacing(6)
        for title, w in (("원문 용어", self.f_src), (None, self.f_meta), ("번역", self.f_ko)):
            if title:
                t = QLabel(f"<b>{title}</b>")
                fl.addWidget(t)
            fl.addWidget(w)
        fl.addWidget(QLabel("<b>후보</b>"))
        fl.addLayout(self.f_cands)
        fl.addWidget(self.f_reason)
        extra = QFormLayout()
        extra.addRow("분류", self.f_cat)
        extra.addRow("변형", self.f_var)
        extra.addRow("메모", self.f_note)
        fl.addLayout(extra)
        fl.addStretch(1)
        scroll = QScrollArea()
        scroll.setObjectName("panel")
        scroll.setStyleSheet("QScrollArea#panel { background: white; border: 1px solid #E5E5EA; border-radius: 10px; }"
                             "QScrollArea#panel > QWidget > QWidget { background: white; }")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(form_w)
        self.next_btn = QPushButton(NEXT_TEXT)
        make_big_primary(self.next_btn)
        self.next_btn.setToolTip("이 용어를 확인함으로 하고(보라 ‘번역 확인’을 지움) 다음 볼 것으로 (Ctrl+Enter)")
        self.next_btn.clicked.connect(self.confirm_next)
        self.right = QWidget()
        self.right.setFixedWidth(RIGHT_W)
        rl = QVBoxLayout(self.right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        rl.addWidget(scroll, 1)
        rl.addWidget(self.next_btn)
        rl.addWidget(shortcut_hint("Ctrl+Enter 확인 · " + NAV_HINT))

    def _mark_dirty(self, _item=None):
        self._dirty = True

    def _rebuild(self):
        """‘용어 후보 다시 만들기…’: 고친 것을 저장하며 닫고(닫기와 같음), 부른 쪽이 후보를 다시 만든다."""
        self.rebuild = True
        self.reject()
        if self.isVisible():  # 저장하지 못해 창에 남기로 했으면 다시 만들지 않는다
            self.rebuild = False

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
        n_unsure = sum(1 for r in rows if r["원문 상태"] and r["원문 상태"] != "확실")
        n_done = sum(1 for r in rows if r["confirmed"])
        for k, text, n, bg in (("check", "번역 확인", n_check, FILL["check"]), ("unsure", "불확실한 원문", n_unsure, FILL["unsure"]),
                               ("empty", "번역 빈 칸", n_empty, "#E5E5EA"), ("done", "이미 확정", n_done, FILL["done"])):
            self.chips[k].setText(f"{text} {n}")
            self.chips[k].setStyleSheet(chip_qss(bg))
            self.chips[k].setVisible(bool(n))  # 0개인 칩은 숨긴다 (작은 화면에서 머리 줄이 넘치지 않게)
        set_hint(self.guide,
                 f"용어 {len(rows)}개 (새 후보 {n_new}) · 번역 확인 {n_check}개(보라) · 번역 빈 칸 {n_empty}개\n"
                 "‘번역’ 칸을 두 번 누르거나 글자를 치면 고칩니다. 번역을 비우면 그 용어는 용어집에 넣지 않습니다. "
                 "머리글을 누르면 그 열로 정렬합니다.\n"
                 "색: 빨강 = 결론 못 낸 원문에서만 나온 용어 · 노랑 = 불확실한 원문 · 보라 = 번역이 갈릴 수 있음 · "
                 "연두 = 이미 확정", "todo" if n_check else "info")

    def _only_toggled(self, on: bool) -> None:
        """‘볼 것만’을 켜면 등장 횟수 많은 순으로 (자주 나오는 용어부터 본다)."""
        if on:
            self.table.sortItems(C["등장 횟수"], Qt.SortOrder.DescendingOrder)
        self._filter()

    def _filter(self):
        q = self.search.text().strip().lower()
        only = self.only_check.isChecked()
        for i in range(self.table.rowCount()):
            src = self.table.item(i, C["원문 용어"]).text()
            ko = self.table.item(i, C["번역"]).text()
            show = not q or q in src.lower() or q in ko.lower()
            if only:
                show = show and self._is_look(i)
            self.table.setRowHidden(i, not show)
        cur = self._cur_row()
        if cur >= 0 and self.table.isRowHidden(cur):  # 보던 용어가 거르기로 숨으면 보이는 첫 줄로 (이동·확인이 숨은 줄에 걸리지 않게)
            rows = self._visible()
            i = next((k for k in rows if self._todo(k)), rows[0] if rows else None)
            if i is None:
                self.table.setCurrentCell(-1, -1)
                self._show_term(None)
            else:
                self.table.setCurrentCell(i, C["번역"])

    # ================================================================ 나눠 보기 (목록 · 나온 곳 · 고치기)
    def _is_look(self, i: int) -> bool:
        """볼 것: 번역 확인 · 불확실한 원문 · 번역 빈 칸."""
        st = self.table.item(i, C["원문 상태"]).text()
        return bool(self.table.item(i, C["번역 확인"]).text() or (st and st != "확실")
                    or not self.table.item(i, C["번역"]).text().strip())

    def _meta(self, i: int) -> dict:
        return self.table.item(i, C["원문 용어"]).data(Qt.ItemDataRole.UserRole) or {}

    def _todo(self, i: int) -> bool:
        """아직 확인하지 않은 볼 것 (이 창에서 ‘확인’을 누른 용어는 빼고)."""
        return self._is_look(i) and not self._meta(i).get("reviewed")

    def _visible(self) -> list[int]:
        """보이는 줄 번호 (표의 지금 순서)."""
        return [i for i in range(self.table.rowCount()) if not self.table.isRowHidden(i)]

    def _set_split(self, on: bool) -> None:
        """나눠 보기(on) ↔ 표로 보기. 데이터는 늘 표에 있다."""
        self._split = on
        self.mid.setVisible(on)
        self.right.setVisible(on)
        self.guide.setVisible(not on)  # 표로 보기에서만 긴 안내 (나눠 보기는 칩과 오른쪽 칸이 대신)
        for i, h in enumerate(COLS):
            self.table.setColumnHidden(i, on and h not in LIST_COLS)
        hh = self.table.horizontalHeader()
        if on:
            for h in LIST_COLS:
                hh.setSectionResizeMode(C[h], QHeaderView.ResizeMode.Stretch)
            hh.setSectionResizeMode(C["등장 횟수"], QHeaderView.ResizeMode.ResizeToContents)
            hh.setSectionResizeMode(C["예문"], QHeaderView.ResizeMode.Interactive)
            for w in self._left_ws:
                w.setFixedWidth(LIST_W)
        else:
            for h in LIST_COLS:
                hh.setSectionResizeMode(C[h], QHeaderView.ResizeMode.Interactive)
            hh.setSectionResizeMode(C["예문"], QHeaderView.ResizeMode.Stretch)
            for h, w in {"원문 용어": 160, "번역": 160, "추천 번역": 150, "원문 상태": 80, "번역 확인": 72,
                         "다른 후보": 200, "분류": 90, "등장 횟수": 64, "변형": 110, "메모": 160}.items():
                self.table.setColumnWidth(C[h], w)
            for w in self._left_ws:
                w.setMinimumWidth(0)
                w.setMaximumWidth(16777215)
        self.nav_w.setVisible(on)
        self.toggle_btn.setText("나눠 보기" if not on else "표로 보기")

    def _go_first(self) -> None:
        """처음: 첫 볼 것 (없으면 첫 줄)."""
        rows = self._visible()
        i = next((k for k in rows if self._todo(k)), rows[0] if rows else None)
        if i is not None:
            self.table.setCurrentCell(i, C["번역"])
        else:
            self._show_term(None)

    def _cur_row(self) -> int:
        try:
            return self._cur.row() if self._cur is not None else -1
        except RuntimeError:  # 지운 줄
            return -1

    def _row_picked(self) -> None:
        i = self.table.currentRow()
        if i >= 0 and self._cur is not None and self.table.item(i, C["원문 용어"]) is self._cur:
            return  # 같은 줄의 다른 칸으로만 옮김: 다시 그리지 않는다
        self._show_term(i if i >= 0 else None)

    def _show_term(self, i: int | None) -> None:
        """오른쪽 칸과 가운데 그림을 그 용어로."""
        self._cur = self.table.item(i, C["원문 용어"]) if i is not None else None
        on = self._cur is not None
        for w in (*self.fields.values(), self.next_btn):
            w.setEnabled(on)
        self._syncing = True
        try:
            for h, w in self.fields.items():
                w.setText(self.table.item(i, C[h]).text() if on else "")
        finally:
            self._syncing = False
        self._fill_cands(i)
        if not on:
            self.f_meta.setText("")
            self._places = []
            self._show_place(0)
            return
        cell = {h: self.table.item(i, C[h]).text() for h in COLS}
        meta = self._meta(i)
        bits = [cell["분류"] or "분류 없음", f"등장 {cell['등장 횟수'] or 0}회", f"원문 {cell['원문 상태'] or '확실'}"]
        if meta.get("confirmed"):
            bits.append("이미 확정된 용어")
        if cell["번역 확인"]:
            bits.append("번역이 갈릴 수 있음 (보라)")
        elif meta.get("reviewed"):
            bits.append("이 창에서 확인함")
        self.f_meta.setText(" · ".join(bits))
        try:
            self._places = service.glossary_example_places(self.project_dir, meta.get("예문", ""))
        except Exception:  # noqa: BLE001 - 그림은 보조
            self._places = []
        self._show_place(0)

    def _fill_cands(self, i: int | None) -> None:
        """후보: 추천 번역 · 다른 후보마다 ‘이것 쓰기’ (그때그때 생기는 단추라 버튼 코드는 맨 뒤)."""
        while self.f_cands.count():
            it = self.f_cands.takeAt(0)
            w = it.widget() or (it.layout() and None)
            if it.layout() is not None:
                while it.layout().count():
                    x = it.layout().takeAt(0).widget()
                    if x is not None:
                        x.deleteLater()
            if w is not None:
                w.deleteLater()
        self.f_reason.setText("")
        if i is None:
            return
        rec = self.table.item(i, C["추천 번역"]).text().strip()
        alt = self.table.item(i, C["다른 후보"]).text().strip()
        alts, reason = (alt.split(" — ", 1) + [""])[:2] if alt else ("", "")
        cands = [("추천", rec)] if rec else []
        cands += [("다른 후보", a.strip()) for a in alts.split(",") if a.strip() and a.strip() != rec]
        if not cands:
            self.f_cands.addWidget(grey_note("후보가 없습니다"))
        for kind, text in cands:
            row = QHBoxLayout()
            lab = QLabel(f'<span style="color:#6E6E73">{kind}</span>&nbsp; {text}')
            lab.setWordWrap(True)
            lab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            b = QPushButton("이것 쓰기")
            b.setAutoDefault(False)
            b.setProperty(ui_codes.LATE, True)
            b.setToolTip(f"번역을 ‘{text}’(으)로")
            b.clicked.connect(lambda _c=False, t=text: self._use_cand(t))
            row.addWidget(lab, 1)
            row.addWidget(b)
            self.f_cands.addLayout(row)
        self.f_reason.setText(f"이유: {reason}" if reason else "")

    def _use_cand(self, text: str) -> None:
        self.f_ko.setText(text)
        self._field_edited("번역")
        self.f_ko.setFocus()

    def _field_edited(self, h: str) -> None:
        """오른쪽 칸에서 고친 글을 표에 바로 쓴다 (표가 데이터를 가진다)."""
        i = self._cur_row()
        if i < 0 or self._syncing:
            return
        self._syncing = True
        try:
            self.table.setSortingEnabled(False)  # 고치는 줄이 정렬로 옮겨 가지 않게
            self.table.item(i, C[h]).setText(self.fields[h].text())
            hh = self.table.horizontalHeader()
            hh.setSortIndicator(-1, hh.sortIndicatorOrder())
            self.table.setSortingEnabled(True)
        finally:
            self._syncing = False
        self._update_guide()

    def _table_changed(self, item: QTableWidgetItem) -> None:
        """표에서 직접 고치면 오른쪽 칸도 맞춘다."""
        if self._syncing or self._cur is None or item.row() != self._cur_row():
            return
        h = COLS[item.column()]
        if h in self.fields:
            self._syncing = True
            try:
                self.fields[h].setText(item.text())
            finally:
                self._syncing = False
        if h in ("추천 번역", "다른 후보"):
            self._fill_cands(item.row())

    def _show_place(self, k: int) -> None:
        """가운데: 이 용어가 나온 k번째 곳의 영역 그림과 원문 (예문에 있는 곳, 보통 2곳까지)."""
        n = len(self._places)
        self.place_prev.setEnabled(n > 1)
        self.place_next.setEnabled(n > 1)
        if not n:
            self._place_i = 0
            self.place_head.setText("나온 곳")
            self.place_pos.setText("")
            self.place_img.clear()
            meta = self._meta(self._cur_row()) if self._cur_row() >= 0 else {}
            ex = str(meta.get("예문") or "")
            self.place_text.setText(_esc(ex).replace("\n", "<br>") if ex else
                                    '<span style="color:#8E8E93">이 용어가 나온 곳을 모릅니다 (새로 더한 용어이거나 예문이 없음)</span>')
            return
        k %= n
        self._place_i = k
        pl = self._places[k]
        self.place_head.setText(f"나온 곳 · {pl['page']}쪽 {pl['region']}" + (f" ({pl['card']})" if pl["card"] else ""))
        self.place_head.setToolTip(self.place_head.text())  # 좁으면 잘리므로 전체는 툴팁으로
        self.place_pos.setText(f"{k + 1} / {n}")
        key = (str(pl["file_dir"]), pl["page"], pl["region"])
        if key not in self._crop_cache:
            pm = QPixmap()
            try:
                pm.loadFromData(service.region_crop(pl["file_dir"], pl["page"], pl["region"]), "PNG")
            except Exception:  # noqa: BLE001 - 그림만 못 보일 뿐
                pass
            self._crop_cache[key] = pm
        pm = self._crop_cache[key]
        if pm.isNull():
            self.place_img.clear()
        else:
            self.place_img.set_image(pm)
        src = self.f_src.text().strip()
        text = _esc(pl["text"])
        if src:
            text = text.replace(_esc(src), f'<b style="background:#FFE066">{_esc(src)}</b>')
        self.place_text.setText(text)

    # ---- 이동 · 확인
    def _nav_go(self, kind: str) -> bool:
        rows = self._visible()
        cur = self._cur_row()
        pos = rows.index(cur) if cur in rows else -1
        if kind in ("prev", "next"):
            j = pos + (1 if kind == "next" else -1) if pos >= 0 else (0 if kind == "next" else -1)
            if not rows or not 0 <= j < len(rows):
                return False
            self.table.setCurrentCell(rows[j], C["번역"])
            return True
        order = rows[pos + 1:] if kind == "next_bad" else rows[:max(pos, 0)][::-1]
        j = next((r for r in order if self._todo(r)), None)
        if j is None:
            return False
        self.table.setCurrentCell(j, C["번역"])
        return True

    def confirm_next(self) -> None:
        """‘확인 → 다음 볼 것’: 이 용어를 확인함으로(보라 ‘번역 확인’을 지움) 하고 다음 볼 것으로. 없으면 알린다."""
        i = self._cur_row()
        if i < 0 or not self._split:
            return
        from PySide6.QtWidgets import QAbstractItemDelegate, QApplication

        ed = QApplication.focusWidget()
        if ed is not None and self.table.isAncestorOf(ed) and ed is not self.table:  # 표 칸을 고치는 중: 먼저 반영
            self.table.commitData(ed)
            self.table.closeEditor(ed, QAbstractItemDelegate.EndEditHint.NoHint)
        empty = bool(self.table.item(i, C["원문 용어"]).text().strip() and not self.table.item(i, C["번역"]).text().strip())
        if empty:
            self.flash_label.flash("번역이 비어 있어 이 용어는 용어집에 넣지 않습니다 (넣으려면 번역을 적으세요)", "warn")
        cleared = False
        self._syncing = True
        try:
            self.table.setSortingEnabled(False)
            meta = dict(self._meta(i))
            meta["reviewed"] = True
            self.table.blockSignals(True)  # 이 창 안 표시일 뿐 저장하지 않는다: 고친 것(_dirty)으로 치지 않게
            self.table.item(i, C["원문 용어"]).setData(Qt.ItemDataRole.UserRole, meta)
            self.table.blockSignals(False)
            chk = self.table.item(i, C["번역 확인"])
            if chk.text() and not empty:  # 번역을 정했을 때만 (빈 번역은 확정 때 경고가 남게)
                cleared = True
                chk.setText("")  # 사람이 후보를 보고 정했다
                chk.setBackground(QBrush())
                self.table.item(i, C["번역"]).setBackground(
                    QBrush(QColor(FILL["done"] if meta.get("confirmed") else FILL["edit"])))
            hh = self.table.horizontalHeader()
            hh.setSortIndicator(-1, hh.sortIndicatorOrder())
            self.table.setSortingEnabled(True)
        finally:
            self._syncing = False
        if cleared:  # 이동만 했으면 표가 바뀌지 않았다 (닫을 때 엑셀을 덮어쓰지 않게)
            self._dirty = True
        self._update_guide()
        if not self._nav_go("next_bad") and not self._nav_go("prev_bad"):
            self._show_term(i)
            self.flash_label.flash("볼 것을 모두 확인했습니다. ‘번역 시작’을 누르세요", "ok")

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
    def start_translate(self):
        """‘번역 시작’: 용어집을 확정하고, 확정되면 부른 쪽이 ④ 전체 번역을 이어서 한다."""
        self.translate_next = True
        self.confirm()
        if self.result is None:  # 확정하지 못했거나 취소: 창에 남는다
            self.translate_next = False

    def confirm(self):
        rows = self.rows()
        self.done_with(lambda accept: service.confirm_glossary_rows(self.project_dir, rows, accept_issues=accept))

    def done_with(self, run, fix_where: str = "표"):
        """확정을 해 보고 (문제가 있으면 알리거나 묻고) 확정되면 창을 닫는다. fix_where: 고칠 곳 (표·엑셀)."""
        from app.qt.dialogs import show_problem
        from app.qt.step_common import _ask_list

        r = run(False)
        if r.state == "blocked":
            show_problem(self, "용어집을 확정하지 못했습니다", "", items=r.blocking + r.issues,
                         todo=("엑셀에서 이 내용을 고치고 저장한 뒤 ‘엑셀에서 가져오기’를 다시 누르세요." if fix_where == "엑셀"
                               else "표에서 이 내용을 고친 뒤 ‘번역 시작’을 다시 누르세요."))
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
