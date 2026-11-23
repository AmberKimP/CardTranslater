"""② 원문 확인 목록 창: 카드·영역 트리(상태 색), 아이콘 종류(같은 모양 한 줄), 사전 전체.

한 줄 = 카드의 영역 하나, 카드마다 묶는다. 영역을 두 번 누르거나 ‘카드 보기’를 누르면 카드 보기 창(모달 아님)이 열린다.
아이콘 이름은 ‘확정 전’으로 적어 두었다가 ‘모두 확정’(또는 카드 보기의 영역 확정)에서 원문에 넣는다. 확정한 영역 중
아이콘이 모두 끝난 영역은 뒤에서 바로 다시 읽는다 (창을 닫아도 계속된다).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QCompleter, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QTabWidget,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from app import service
from app.qt import ui_codes, worker
from app.qt.dialogs import (
    PRIMARY_QSS, ask_choice, file_label, fit_to_screen, grey_note, hint_bar, page_title, show_problem,
)
from app.qt.icon_review import IconDictionaryPanel, NoWheelSpinBox
from app.qt.step_common import source_font
from app.qt.work_layout import CardMap, load_card_map, panel

log = logging.getLogger("cardtranslater.source_check")

# 상태 색: (칩 바탕, 점, 줄 바탕)
CHIP_BG = {"fix": "#F8D0D0", "check": "#FFF0B3", "reading": "#CFE2F3", "todo": "#EDEDF2", "ok": "#D9EAD3",
           "keep": "#E8E8ED"}
DOT = {"fix": "#E5484D", "check": "#F5A623", "reading": "#4A90D9", "todo": "#C7C7CC", "ok": "#34A853",
       "keep": "#AEAEB2"}
ROW_BG = {"fix": "#FDECEC", "check": "#FFF8DB", "reading": "#EEF5FD", "keep": "#F7F7F9"}
PENDING_BG, PENDING_FG = "#EFE6FA", "#5A2D91"
PROBLEM = ("fix", "check", "reading")
KIND_STATE = {"lost": "위치 못 찾음", "unnamed": "이름 없음", "no_ko": "한국어 이름 없음", "pending": "확정 전",
              "ok": "확정됨"}

ROLE_ITEM = Qt.ItemDataRole.UserRole  # ("card", key) | ("region", key)
ROLE_BAR = Qt.ItemDataRole.UserRole + 1  # 빨강 표시줄을 그릴 줄


def _nk(key: tuple) -> tuple:
    """칸 키 비교용 (경로 표기 차이를 없앰)."""
    fd, *rest = key
    return (os.path.normcase(os.path.abspath(str(fd))), *rest)


class _BarDelegate(QStyledItemDelegate):
    """‘고칠 것’ 줄의 왼쪽에 빨간 세로 표시줄."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if index.data(ROLE_BAR):
            painter.save()
            painter.fillRect(QRect(option.rect.left(), option.rect.top(), 3, option.rect.height()), QColor(DOT["fix"]))
            painter.restore()


def _dot(color: str) -> QPixmap:
    pm = QPixmap(12, 12)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(1, 1, 10, 10)
    p.end()
    return pm


class SourceCheckDialog(QDialog):
    """② 원문 확인 목록 창. saved: 바꾼 것이 있음 (부른 쪽이 화면을 새로 고치게)."""

    COLS = ["상태", "영역", "원문", "아이콘", "문제", ""]
    THUMB_H = 24

    def __init__(self, project_dir: Path, files: list[dict], parent=None, engine: str | None = None,
                 focus: tuple | None = None, scope: tuple | None = None):
        """scope: 프로젝트 화면 목록에서 고른 범위 (설명, 파일 폴더 목록) — 처음 파일 거르기로 쓴다 (None·전체면 모두)."""
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.files = list(files)
        self._scope = scope
        self.engine = engine
        self.saved = False
        self.reading: set[tuple] = set()  # 다시 읽기 대기·진행 중인 영역 (정규화한 키)
        self._queue: list[dict] = []
        self._running = False
        self._closed = False
        # 다시 읽기 줄이 모두 끝났을 때 부를 함수 (창을 닫은 뒤에도: 프로젝트 화면이 지웠던 파일을 다시 지운다)
        self.on_idle = None
        self._cards: list[dict] = []
        self._kinds: list[dict] = []
        self._visible: list[dict] = []  # 지금 목록에 보이는 카드 (카드 보기가 이 순서를 따른다)
        self._expanded: dict[tuple, bool] = {}
        self._building = False
        self._group_filter: set | None = None
        self._viewer = None
        self._pm_cache: dict = {}
        self._dots = {k: _dot(v) for k, v in DOT.items()}
        self.setWindowTitle("② 원문 확인")
        fit_to_screen(self, 1400, 880, maximize_below=0)
        self.setMinimumSize(760, 480)
        self._build_ui()
        self._load_dictionary()
        self.refresh()
        if not (focus and self.focus_on(focus)):
            self.select_first_problem()

    # ================================================================ 화면 만들기
    def _build_ui(self):
        self.chips: dict[str, QLabel] = {}
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(page_title("② 원문 확인", 14))
        top.addSpacing(10)
        for st in ("fix", "check", "reading", "todo", "ok", "keep"):
            lb = QLabel()
            lb.setStyleSheet(f"background: {CHIP_BG[st]}; border-radius: 10px; padding: 3px 10px; color: #1D1D1F;")
            self.chips[st] = lb
            top.addWidget(lb)
        top.addStretch(1)
        self.pending_label = QLabel()
        self.pending_label.setStyleSheet(f"background: {PENDING_BG}; color: {PENDING_FG}; border-radius: 10px;"
                                         " padding: 3px 12px; font-weight: bold;")
        top.addWidget(self.pending_label)

        # ---- 카드·영역 탭
        self.mode_problem = QPushButton("문제 있는 카드만")
        self.mode_all = QPushButton("전체")
        seg = QButtonGroup(self)
        seg.setExclusive(True)
        for i, b in enumerate((self.mode_problem, self.mode_all)):
            b.setCheckable(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            radius = "8px 0 0 8px" if i == 0 else "0 8px 8px 0"
            b.setStyleSheet("QPushButton { background: white; border: 1px solid #D1D1D6; padding: 5px 14px;"
                            f" border-radius: {radius}; min-height: 20px; }}"
                            "QPushButton:checked { background: #E5F0FD; color: #0071E3; border-color: #0071E3;"
                            " font-weight: bold; }")
            seg.addButton(b)
        self.mode_problem.setChecked(True)
        seg.buttonClicked.connect(lambda _b: self._rebuild_tree())
        self.file_combo = QComboBox()
        self.file_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.file_combo.setMinimumContentsLength(14)
        self.file_combo.addItem("모두", None)
        for f in self.files:
            self.file_combo.addItem(file_label(f.get("key", "")) or Path(str(f["dir"])).name, str(f["dir"]))
        scope = self._scope
        if scope and scope[1]:
            want = {_nk((d,))[0] for d in scope[1]}
            hit = [i for i in range(1, self.file_combo.count()) if _nk((self.file_combo.itemData(i),))[0] in want]
            if len(hit) == 1:
                self.file_combo.setCurrentIndex(hit[0])
            elif hit:  # 그룹을 골랐다: 그 파일들만
                self.file_combo.insertItem(1, f"고른 범위: {scope[0]} ({len(hit)}개)",
                                           [self.file_combo.itemData(i) for i in hit])
                self.file_combo.setCurrentIndex(1)
        self.file_combo.currentIndexChanged.connect(lambda _i: self._rebuild_tree())
        self.search = QLineEdit()
        self.search.setPlaceholderText("원문·아이콘 이름·영역 이름 찾기")
        self.search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self._rebuild_tree)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        self.group_label = QLabel()
        self.group_label.setStyleSheet(f"background: {PENDING_BG}; color: {PENDING_FG}; border-radius: 10px;"
                                       " padding: 3px 10px;")
        self.group_clear = QPushButton("모두 보기")
        self.group_clear.clicked.connect(lambda: self.show_groups(None))
        self.group_label.setVisible(False)
        self.group_clear.setVisible(False)
        seg_box = QHBoxLayout()
        seg_box.setSpacing(0)
        seg_box.addWidget(self.mode_problem)
        seg_box.addWidget(self.mode_all)
        flt = QHBoxLayout()
        flt.setSpacing(10)
        flt.addLayout(seg_box)
        flt.addWidget(QLabel("파일"))
        flt.addWidget(self.file_combo)
        flt.addWidget(self.search, 1)
        flt.addWidget(self.group_label)
        flt.addWidget(self.group_clear)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self.COLS))
        self.tree.setHeaderLabels(self.COLS)
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(240, self.THUMB_H))
        self.tree.setIndentation(18)
        self.tree.setRootIsDecorated(True)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.setItemDelegateForColumn(0, _BarDelegate(self.tree))
        hh = self.tree.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        for col, w in ((0, 210), (1, 110), (3, 250), (4, 230), (5, 104)):
            self.tree.setColumnWidth(col, w)
        self.tree.header().setMinimumSectionSize(60)
        self.tree.itemDoubleClicked.connect(self._on_double)
        self.tree.itemExpanded.connect(lambda it: self._remember_expand(it, True))
        self.tree.itemCollapsed.connect(lambda it: self._remember_expand(it, False))
        self.empty_label = grey_note("")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setVisible(False)

        cards_tab = QWidget()
        cl = QVBoxLayout(cards_tab)
        cl.setContentsMargins(8, 10, 8, 8)
        cl.setSpacing(8)
        cl.addLayout(flt)
        body = QHBoxLayout()
        body.setSpacing(10)
        tl = QVBoxLayout()
        tl.addWidget(self.tree, 1)
        tl.addWidget(self.empty_label)
        body.addLayout(tl, 1)
        body.addWidget(self._build_preview())
        cl.addLayout(body, 1)

        # ---- 아이콘 종류 탭
        self.kind_cols = ["그림", "이름", "한국어 이름", "표시 크기", "쓰인 곳", "상태", "아이콘 아님"]
        self.kind_table = QTableWidget(0, len(self.kind_cols))
        self.kind_table.setHorizontalHeaderLabels(self.kind_cols)
        self.kind_table.verticalHeader().setVisible(False)
        self.kind_table.setIconSize(QSize(56, 40))
        self.kind_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.kind_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.kind_table.setStyleSheet("QLineEdit, QDoubleSpinBox { margin: 7px 6px; padding-top: 1px;"
                                      " padding-bottom: 1px; } QCheckBox { margin-left: 24px; }")
        kh = self.kind_table.horizontalHeader()
        kh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        kh.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        kh.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        kh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        kh.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        kh.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        kh.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        for col, w in ((0, 90), (1, 200), (2, 200), (3, 110), (4, 170), (6, 100)):
            self.kind_table.setColumnWidth(col, w)
        self.kind_widgets: list[dict] = []
        kinds_tab = QWidget()
        kl = QVBoxLayout(kinds_tab)
        kl.setContentsMargins(8, 10, 8, 8)
        kl.setSpacing(8)
        kl.addWidget(hint_bar("같은 모양의 아이콘 = 한 줄. 이름을 적으면 그 모양이 있는 모든 영역이 ‘확정 전’이 됩니다. "
                              "한 자리만 다른 아이콘이면 카드 보기에서 그 자리만 바꾸세요.", "info"))
        kl.addWidget(self.kind_table, 1)

        # ---- 사전 전체 탭 (사전 쪽이 바뀔 때 다시 만든다)
        self.dict_tab = QWidget()
        self.dict_layout = QVBoxLayout(self.dict_tab)
        self.dict_layout.setContentsMargins(8, 10, 8, 8)
        self.dict_panel: IconDictionaryPanel | None = None
        self.dict_save = QPushButton("사전 저장")
        self.dict_save.setToolTip("이 탭에서 고친 한국어 이름·뜻·표시 크기를 게임 아이콘 사전에 저장합니다")
        self.dict_save.clicked.connect(self.save_dictionary)

        self.tabs = QTabWidget()
        self.tabs.addTab(cards_tab, "카드·영역")
        self.tabs.addTab(kinds_tab, "아이콘 종류")
        self.tabs.addTab(self.dict_tab, "사전 전체")
        self.rescan_btn = QToolButton()
        self.rescan_btn.setText("아이콘 다시 찾기")
        self.rescan_btn.setAutoRaise(True)
        self.rescan_btn.setToolTip("게임 전체에서 아이콘을 다시 찾습니다 (확정 전 이름·자리는 사라짐)")
        self.rescan_btn.clicked.connect(self.rescan)
        self.tabs.setCornerWidget(self.rescan_btn, Qt.Corner.TopRightCorner)

        # ---- 아래
        self.note = grey_note("확정한 영역 중 아이콘이 모두 끝난 영역은 바로 다시 읽습니다 (보통·정밀 두 번)")
        self.reading_label = QLabel()
        self.reading_label.setStyleSheet(f"background: {CHIP_BG['reading']}; border-radius: 10px; padding: 3px 12px;")
        self.reading_label.setVisible(False)
        self.close_btn = QPushButton("닫기")
        self.close_btn.clicked.connect(self.reject)
        self.confirm_btn = QPushButton("모두 확정")
        self.confirm_btn.setStyleSheet(PRIMARY_QSS)
        self.confirm_btn.clicked.connect(self.confirm_all)
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        bottom.addWidget(self.note, 1)
        bottom.addWidget(self.reading_label)
        bottom.addWidget(self.close_btn)
        bottom.addWidget(self.confirm_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(10)
        lay.addLayout(top)
        lay.addWidget(self.tabs, 1)
        lay.addLayout(bottom)

    # ================================================================ 미리 보기 칸 (카드 보기 배치의 일부: 고른 카드·영역을 오른쪽에)
    PREVIEW_W = 320

    def _build_preview(self) -> QWidget:
        """오른쪽 미리 보기: 고른 카드 그림(영역 상자 상태 색) · 영역 이름 · 원문 · ‘카드 보기 열기’ (Enter, 더블클릭과 같음)."""
        self._map_cache: dict[tuple, dict] = {}
        self.preview_title = QLabel()
        self.preview_title.setStyleSheet("font-weight: 600; color: #1D1D1F;")
        self.preview_title.setWordWrap(True)
        self.preview_map = CardMap()
        self.preview_map.setMinimumSize(200, 180)
        self.preview_map.picked.connect(self._preview_pick)
        self.preview_src = QPlainTextEdit()
        self.preview_src.setReadOnly(True)
        self.preview_src.setFont(source_font(self.preview_src.font(), 2))
        self.preview_src.setStyleSheet("QPlainTextEdit { background: #F0F5FD; }")
        self.preview_src.setMaximumHeight(150)
        self.preview_open = QPushButton("카드 보기 열기 (Enter)")
        self.preview_open.setProperty(ui_codes.LATE, True)  # 버튼 코드가 밀리지 않게 맨 뒤에 센다
        self.preview_open.setAutoDefault(False)
        self.preview_open.setToolTip("고른 영역(카드면 첫 문제 영역)으로 카드 보기를 엽니다 (Enter · 더블클릭)")
        self.preview_open.clicked.connect(self._open_current)
        QShortcut(QKeySequence(Qt.Key.Key_Return), self.tree, activated=self._open_current,
                  context=Qt.ShortcutContext.WidgetShortcut)
        QShortcut(QKeySequence(Qt.Key.Key_Enter), self.tree, activated=self._open_current,
                  context=Qt.ShortcutContext.WidgetShortcut)
        self.tree.currentItemChanged.connect(lambda *_a: self._update_preview())
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(6)
        il.addWidget(self.preview_title)
        il.addWidget(self.preview_map, 1)
        il.addWidget(QLabel("<b>원문</b>"))
        il.addWidget(self.preview_src)
        il.addWidget(self.preview_open)
        box = panel("미리 보기", inner)
        box.setFixedWidth(self.PREVIEW_W)
        return box

    def _current_target(self) -> tuple[dict | None, dict | None]:
        """지금 고른 줄의 (카드, 영역|None)."""
        it = self.tree.currentItem()
        data = it.data(0, ROLE_ITEM) if it is not None else None
        if not data:
            return None, None
        kind, key = data
        ck = _nk(key)[:2]
        card = next((c for c in self._cards if _nk(c["key"])[:2] == ck), None)
        if card is None or kind == "card":
            return card, None
        return card, next((r for r in card["regions"] if _nk(r["key"]) == _nk(key)), None)

    def _update_preview(self) -> None:
        card, reg = self._current_target()
        self.preview_open.setEnabled(card is not None)
        if card is None:
            self.preview_title.setText("목록에서 카드나 영역을 고르세요")
            self.preview_map.set_data(QPixmap(), {}, {}, "")
            self.preview_src.setPlainText("")
            return
        k = (str(card["file_dir"]), int(card["page"]))
        if k not in self._map_cache:
            self._map_cache[k] = load_card_map(card["file_dir"], card["page"])
        info = self._map_cache[k]
        colors = {r["region"]: DOT[self._status(r)] for r in card["regions"]}
        self.preview_map.set_data(info["pm"], info["rects"], colors, reg["region"] if reg else "")
        title = f"{file_label(card['file_key'])} · {card['page']}쪽"
        if reg is None:
            self.preview_title.setText(title)
            self.preview_src.setPlainText("영역을 고르면 그 영역의 원문을 봅니다 (카드 그림의 상자를 눌러도 됩니다)")
            return
        self.preview_title.setText(f"{title} · {reg['region']} — {service.STATUS_LABEL[self._status(reg)]}")
        try:
            src = service.preview_source(reg) if reg["pending"] else reg.get("source")
        except Exception:  # noqa: BLE001
            src = reg.get("source")
        self.preview_src.setPlainText("\n".join(src) if isinstance(src, list) else (src or "(읽지 않음)"))

    def _preview_pick(self, name: str) -> None:
        """미리 보기 카드 그림에서 상자를 누름: 목록에서 그 영역 줄을 고른다."""
        card, _reg = self._current_target()
        r = next((x for x in card["regions"] if x["region"] == name), None) if card else None
        if r is not None:
            self.focus_on(tuple(r["key"]))

    def _open_current(self) -> None:
        it = self.tree.currentItem()
        if it is not None:
            self._on_double(it, 0)

    # ================================================================ 사전 탭
    def _load_dictionary(self):
        try:
            rows = service.icon_dictionary(self.project_dir)
        except Exception:  # noqa: BLE001 - 사전을 못 읽어도 목록은 된다
            log.warning("아이콘 사전을 읽지 못함", exc_info=True)
            rows = []
        self._dict_names = [r["name"] for r in rows]
        if self.dict_panel is not None:
            self.dict_layout.removeWidget(self.dict_panel)
            self.dict_panel.deleteLater()
        self.dict_save_row = None
        # ‘다시 정하기’: 저장하면 원문의 그 아이콘이 이름 없는 아이콘([[?]])이 되고, 그 칸만 다시 찾아 목록에 보인다
        self.dict_panel = IconDictionaryPanel(rows, "", self.dict_tab, allow_reset=True)
        self.dict_layout.insertWidget(0, self.dict_panel, 1)
        if self.dict_layout.indexOf(self.dict_save) < 0:
            self.dict_layout.addWidget(self.dict_save, alignment=Qt.AlignmentFlag.AlignRight)
        self.tabs.setTabText(2, f"사전 전체 ({len(rows)})")
        self._completer = QCompleter(self._dict_names, self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def save_dictionary(self) -> bool:
        rows = self.dict_panel.rows()
        resets = [r["name"] for r in rows if r.get("reset")]
        if resets:
            r = ask_choice(self, "아이콘 다시 정하기", "다시 정할 아이콘: " + ", ".join(resets),
                           [("다시 정하기", "destructive"), ("취소", "reject")], default=1,
                           informative="사전에서 빠지고, 원문의 그 아이콘은 이름 없는 아이콘이 됩니다 (목록에 ‘고칠 것’으로 "
                                       "보입니다). 이름을 다시 적고 확정하세요.")
            if r != 0:
                return False
        try:
            res = service.update_icon_dictionary(self.project_dir, rows)
        except Exception as e:  # noqa: BLE001
            show_problem(self, "사전을 저장하지 못했습니다", str(e), "")
            return False
        self.saved = True
        if res.get("reset"):  # 원문이 바뀐 칸만 다시 찾는다 (찾기 결과를 원문에 맞춤). 칸이 많으면 오래 걸려 진행 창으로
            # 끝날 때까지 기다린다: 닫기에서 ‘저장’을 골랐을 때 다시 찾기가 창 밖에서 계속 돌며 다음에 연 창과 겹치지 않게
            from PySide6.QtCore import QEventLoop

            loop = QEventLoop(self)
            finished = []

            def after(_r=None):
                finished.append(True)
                self._guard(self._load_dictionary)
                self._guard(self.refresh)
                loop.quit()

            job = worker.Job(service.ensure_icon_scan, self.project_dir, engine=self.engine, suggest=False,
                             full_if_missing=False, with_progress=False)
            worker.run_with_dialog(self, "다시 정한 아이콘의 칸 다시 찾는 중", job, after,
                                   lambda m: (log.warning("다시 정한 아이콘의 칸을 다시 찾지 못함: %s", m), after()),
                                   cancellable=False)
            if not finished:
                loop.exec()
            return True
        self._load_dictionary()
        self.refresh()
        return True

    def _dict_unsaved_ok(self) -> bool:
        if self.dict_panel is None or not self.dict_panel.dirty:
            return True
        r = ask_choice(self, "아이콘 사전", "‘사전 전체’ 탭에서 고친 것을 저장할까요?",
                       [("저장", "accept"), ("저장 안 함", "destructive"), ("취소", "reject")], default=0)
        if r == 0:
            return self.save_dictionary()
        return r == 1

    # ================================================================ 데이터
    def refresh(self):
        """service에서 다시 읽어 화면을 새로 그린다 (펼침·고른 줄·스크롤 위치는 지킨다)."""
        self._cards = service.source_check_rows(self.project_dir, self.files)
        try:
            kinds = service.icon_kinds(self.project_dir)
        except Exception:  # noqa: BLE001
            log.warning("아이콘 종류를 읽지 못함", exc_info=True)
            kinds = []
        self._update_chips()
        self._rebuild_tree()
        self._set_kinds(kinds)

    def _status(self, r: dict) -> str:
        return "reading" if _nk(r["key"]) in self.reading else r["status"]

    def _is_problem_card(self, card: dict) -> bool:
        # 확정 전만으로는 문제가 아니다 (모두 확정으로 넣는다). 추측한 이름은 region_status가 ‘볼 것’으로 만든다
        return any(self._status(r) in PROBLEM for r in card["regions"])

    def _worst(self, card: dict) -> str:
        order = {s: i for i, s in enumerate(service.STATUS_ORDER)}
        return min((self._status(r) for r in card["regions"]), key=lambda s: order[s])

    def _regions(self):
        for c in self._cards:
            yield from c["regions"]

    def pending_keys(self) -> list[tuple]:
        # 다시 읽는 중인 영역은 빼고 (뒤에서 같은 파일을 쓰는 중이다)
        return [r["key"] for r in self._regions() if r["pending"] and _nk(r["key"]) not in self.reading]

    def reading_keys(self) -> set[tuple]:
        """다시 읽는 중인 영역 키 (카드 보기의 reading_fn)."""
        return {r["key"] for r in self._regions() if _nk(r["key"]) in self.reading}

    def current_rows(self) -> list[dict]:
        """지금 목록의 카드 (보이는 순서·거른 영역만). 카드 보기의 rows_fn."""
        return self._visible

    def _update_chips(self):
        counts = {s: 0 for s in self.chips}
        for r in self._regions():
            counts[self._status(r)] += 1
        for st, lb in self.chips.items():
            lb.setText(f"{service.STATUS_LABEL[st]} {counts[st]}")
            lb.setVisible(counts[st] > 0 or st in ("fix", "check", "ok"))
        n = len(self.pending_keys())
        self.pending_label.setText(f"확정 전 {n}개 영역")
        self.pending_label.setVisible(n > 0)
        self.confirm_btn.setText(f"모두 확정 ({n})")
        self.confirm_btn.setEnabled(n > 0)
        total = sum(1 for _ in self._regions())
        self.tabs.setTabText(0, f"카드·영역 ({total})")
        if self.reading:
            self.reading_label.setText(f"다시 읽는 중 {len(self.reading)}개 영역")
        self.reading_label.setVisible(bool(self.reading))

    # ================================================================ 트리
    def _matches(self, r: dict, q: str) -> bool:
        if self._group_filter is not None and not any(e["group"] in self._group_filter for e in r["icons"]):
            return False
        if not q:
            return True
        src = r.get("source")
        text = "\n".join(src) if isinstance(src, list) else (src or "")
        hay = [text, str(r.get("region", ""))] + [e["name"] for e in r["icons"]] + [e.get("ko", "") for e in r["icons"]]
        return any(q in h.lower() for h in hay)

    def _filtered_cards(self) -> list[dict]:
        q = self.search.text().strip().lower()
        fdir = self.file_combo.currentData()
        narrow = bool(q) or self._group_filter is not None
        only_problem = self.mode_problem.isChecked() and not narrow
        fidx = {str(f["dir"]): i for i, f in enumerate(self.files)}
        out = []
        for c in self._cards:
            if isinstance(fdir, list):
                if c["file_dir"] not in fdir:
                    continue
            elif fdir is not None and c["file_dir"] != fdir:
                continue
            if only_problem and not self._is_problem_card(c):
                continue
            regs = [r for r in c["regions"] if self._matches(r, q)] if narrow else list(c["regions"])
            if narrow and not regs:
                continue
            out.append({**c, "regions": regs, "status": self._worst(c)})
        # 쪽 순서만 (문제 카드를 위로 올리지 않는다: 사용자 요청). 처음 열 때 첫 문제 카드를 고른다 (select_first_problem)
        out.sort(key=lambda c: (fidx.get(c["file_dir"], 0), c["page"]))
        return out

    def select_first_problem(self) -> bool:
        """목록에서 문제 있는 첫 카드를 골라 펼쳐 보인다. 없으면 False (첫 카드를 고른다)."""
        first = None
        for i in range(self.tree.topLevelItemCount()):
            head = self.tree.topLevelItem(i)
            kind, k = head.data(0, ROLE_ITEM)
            if kind != "card":
                continue
            first = first or head
            card = next((c for c in self._visible if _nk(c["key"]) == _nk(k)), None)
            if card is not None and self._is_problem_card(card):
                head.setExpanded(True)
                self.tree.setCurrentItem(head)
                self.tree.scrollToItem(head, QAbstractItemView.ScrollHint.PositionAtTop)
                return True
        if first is not None:
            self.tree.setCurrentItem(first)
        return False

    def _capture_tree_state(self) -> dict:
        cur = self.tree.currentItem()
        sel = cur.data(0, ROLE_ITEM) if cur is not None else None
        top = self.tree.itemAt(0, 0)
        return {"sel": sel, "scroll": self.tree.verticalScrollBar().value(),
                "top": top.data(0, ROLE_ITEM) if top is not None else None}

    def _rebuild_tree(self):
        st = self._capture_tree_state()
        self._visible = self._filtered_cards()
        self._building = True
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.clear()
            reselect = None
            for c in self._visible:
                head = self._card_item(c)
                self.tree.addTopLevelItem(head)
                self.tree.setItemWidget(head, 5, self._view_button(c))
                for r in c["regions"]:
                    it = self._region_item(r)
                    head.addChild(it)
                    if st["sel"] == ("region", r["key"]):
                        reselect = it
                if st["sel"] == ("card", c["key"]):
                    reselect = head
                head.setExpanded(self._expanded.get(c["key"], self._is_problem_card(c)))
            if reselect is not None:
                self.tree.setCurrentItem(reselect)
            self.tree.verticalScrollBar().setValue(st["scroll"])
        finally:
            self.tree.setUpdatesEnabled(True)
            self._building = False
        if not self._visible:
            self.empty_label.setText("조건에 맞는 카드가 없습니다." if self._cards else "아직 읽은 카드가 없습니다.")
        self.empty_label.setVisible(not self._visible)

    def _remember_expand(self, item: QTreeWidgetItem, on: bool):
        if self._building:
            return
        data = item.data(0, ROLE_ITEM)
        if data and data[0] == "card":
            self._expanded[data[1]] = on

    def _card_item(self, c: dict) -> QTreeWidgetItem:
        it = QTreeWidgetItem()
        regs = c["regions"]
        sts = [self._status(r) for r in regs]
        n = {s: sts.count(s) for s in set(sts)}
        n_pend = sum(1 for r in regs if r["pending"])
        parts = []
        if n.get("fix"):
            parts.append(f"고칠 것 {n['fix']}")
        if n.get("check"):
            parts.append(f"볼 것 {n['check']}")
        if n.get("reading"):
            parts.append(f"읽는 중 {n['reading']}")
        if n_pend:
            parts.append(f"확정 전 {n_pend}")
        if not parts:
            if n.get("todo"):
                parts.append(f"확인 안 함 {n['todo']}")
            elif n.get("ok"):
                parts.append("모두 확인됨")
            else:
                parts.append("원본 그대로")
        it.setText(0, f"{file_label(c['file_key'])} · {c['page']}쪽")
        it.setText(2, " · ".join(parts))
        it.setIcon(0, self._dots[c["status"]])
        it.setData(0, ROLE_ITEM, ("card", c["key"]))
        f = it.font(0)
        f.setBold(True)
        for col in range(len(self.COLS)):
            it.setFont(col, f)
            it.setBackground(col, QBrush(QColor("#F2F2F7")))
        it.setSizeHint(0, QSize(0, 36))
        return it

    def _view_button(self, c: dict) -> QWidget:
        btn = QPushButton("카드 보기")
        btn.setToolTip("이 카드를 크게 보며 영역·아이콘을 고칩니다")
        btn.setStyleSheet("QPushButton { padding: 2px 10px; min-height: 18px; }")
        btn.clicked.connect(lambda _c=False, key=c["key"]: self._open_card(key))
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(4, 2, 4, 2)
        hl.addWidget(btn)
        return holder

    def _region_item(self, r: dict) -> QTreeWidgetItem:
        st = self._status(r)
        it = QTreeWidgetItem()
        label = service.STATUS_LABEL[st] + ("  · 확정 전" if r["pending"] else "")
        it.setText(0, label)
        it.setIcon(0, self._dots[st])
        it.setText(1, str(r.get("region", "")))
        try:
            src = service.preview_source(r) if r["pending"] else r.get("source")
        except Exception:  # noqa: BLE001
            src = r.get("source")
        text = "  ↵  ".join(src) if isinstance(src, list) else (src or "")
        text = " ".join(text.split("\n")) if "\n" in text else text
        it.setText(2, text[:240] if text else "(읽지 않음)")
        it.setToolTip(2, text)
        pm = self._icons_pixmap(r["icons"])
        if pm is not None:
            it.setIcon(3, pm)
        why = " · ".join(r["reasons"])
        it.setText(4, why)
        it.setToolTip(4, why)
        it.setData(0, ROLE_ITEM, ("region", r["key"]))
        if st == "fix":
            it.setData(0, ROLE_BAR, True)
        bg = ROW_BG.get(st)
        for col in range(len(self.COLS)):
            if bg:
                it.setBackground(col, QBrush(QColor(bg)))
        if r["pending"]:
            it.setForeground(0, QBrush(QColor(PENDING_FG)))
        it.setSizeHint(0, QSize(0, 36))
        return it

    def _thumb(self, path: str | None) -> QPixmap | None:
        if not path:
            return None
        if path not in self._pm_cache:
            pm = QPixmap(path)
            if pm.isNull():
                self._pm_cache[path] = None
            else:
                pm = pm.scaledToHeight(self.THUMB_H, Qt.TransformationMode.SmoothTransformation)
                if pm.width() > 44:
                    pm = pm.scaledToWidth(44, Qt.TransformationMode.SmoothTransformation)
                self._pm_cache[path] = pm
        return self._pm_cache[path]

    def _icons_pixmap(self, icons_: list[dict], limit: int = 6, h: int | None = None) -> QPixmap | None:
        """영역의 아이콘 그림들을 한 장으로 (이름·위치 못 찾은 것은 빨간 점선 + ?)."""
        if not icons_:
            return None
        h = h or self.THUMB_H
        key = (tuple((e.get("image"), e["state"]) for e in icons_), limit)
        if key in self._pm_cache:
            return self._pm_cache[key]
        shown = icons_[:limit]
        cells = []
        for e in shown:
            pm = self._thumb(e.get("image"))
            cells.append((pm, e["state"]))
        w = sum((pm.width() if pm is not None else h) + 8 for pm, _s in cells) + (28 if len(icons_) > limit else 0)
        out = QPixmap(max(w, 1), h + 4)
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        x = 0
        for pm, state in cells:
            cw = pm.width() if pm is not None else h
            faded = state in ("not_icon", "absorbed")
            p.setOpacity(0.4 if faded else 1.0)
            if pm is not None:
                p.drawPixmap(x + 3, 2 + (h - pm.height()) // 2, pm)
            bad = state in ("lost", "unnamed")
            if bad or pm is None:
                pen = QPen(QColor(DOT["fix"] if bad else "#AEAEB2"), 1.4, Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(QRect(x + 1, 1, cw + 4, h + 1))
                if pm is None:
                    p.drawText(QRect(x + 1, 1, cw + 4, h + 1), Qt.AlignmentFlag.AlignCenter, "?")
            x += cw + 8
        if len(icons_) > limit:
            p.setOpacity(1.0)
            p.setPen(QColor("#6E6E73"))
            p.drawText(QRect(x, 0, 28, h + 4), Qt.AlignmentFlag.AlignCenter, f"+{len(icons_) - limit}")
        p.end()
        self._pm_cache[key] = out
        return out

    # ---- 고르기·열기
    def focus_on(self, key: tuple) -> bool:
        """(파일 폴더, 쪽) 카드 또는 (파일 폴더, 쪽, 영역) 영역을 골라 펼쳐 보인다. 없으면 False.
        (파일 폴더, None): 그 파일의 첫 카드 (문제 카드가 위에 있으므로 첫 문제 카드)."""
        nkey = _nk(key)
        file_only = len(key) > 1 and key[1] is None
        for attempt in (0, 1):
            for i in range(self.tree.topLevelItemCount()):
                head = self.tree.topLevelItem(i)
                kind, k = head.data(0, ROLE_ITEM)
                if kind == "card" and (_nk(k)[0] == nkey[0] if file_only else _nk(k) == _nk(key[:2])):
                    target = head
                    if len(key) > 2 and not file_only:
                        for j in range(head.childCount()):
                            ch = head.child(j)
                            if _nk(ch.data(0, ROLE_ITEM)[1]) == nkey:
                                target = ch
                                break
                    head.setExpanded(True)
                    self.tree.setCurrentItem(target)
                    self.tree.scrollToItem(target, QAbstractItemView.ScrollHint.PositionAtCenter)
                    return True
            if attempt == 0:  # 문제 있는 카드만·고른 범위만 보는 중이라 안 보임: 전체·모든 파일로 바꿔 다시
                self.mode_all.setChecked(True)
                if self.file_combo.currentIndex() != 0:
                    self.file_combo.blockSignals(True)
                    self.file_combo.setCurrentIndex(0)
                    self.file_combo.blockSignals(False)
                self._rebuild_tree()
        return False

    def _on_double(self, item: QTreeWidgetItem, _col: int):
        data = item.data(0, ROLE_ITEM)
        if not data:
            return
        if data[0] == "region":
            self.open_viewer(data[1])
        else:
            self._open_card(data[1])

    def _open_card(self, card_key: tuple):
        """‘카드 보기’: 그 카드의 첫 문제 영역 (없으면 첫 영역)."""
        card = next((c for c in self._cards if _nk(c["key"]) == _nk(card_key)), None)
        if card is None:
            return
        pick = next((r for r in card["regions"] if self._status(r) in PROBLEM),
                    card["regions"][0] if card["regions"] else None)
        self.open_viewer(pick["key"] if pick else tuple(card["key"]))

    def open_viewer(self, start: tuple):
        try:
            from app.qt.card_viewer import CardViewer
        except ImportError:
            show_problem(self, "카드 보기를 열 수 없습니다", "카드 보기 창을 찾지 못했습니다.", "프로그램을 다시 설치해 보세요.")
            return None
        v = self._viewer
        if v is not None:
            try:
                v.close()
                if v.isVisible():  # 닫기를 거절했다 (저장 안 한 변경): 그 창을 보인다
                    v.raise_()
                    v.activateWindow()
                    return v
            except RuntimeError:
                pass
            self._viewer = None
        v = CardViewer(self.project_dir, self.current_rows, start, parent=self, engine=self.engine,
                       reading_fn=self.reading_keys)
        v.changed.connect(self._on_viewer_changed)
        v.confirmed.connect(self._on_viewer_confirmed)
        self._viewer = v
        v.show()
        return v

    def _on_viewer_changed(self, *_a):
        self.saved = True
        if not self._closed:
            self.refresh()

    def _on_viewer_confirmed(self, result):
        self.saved = True
        cells = (result or {}).get("reread") if isinstance(result, dict) else None
        self.enqueue_reread(cells or [])
        if not self._closed:
            self.refresh()

    # ================================================================ 아이콘 종류 탭
    def _set_kinds(self, kinds: list[dict]):
        same = [k["groups"] for k in kinds] == [k["groups"] for k in self._kinds] and \
            len(self.kind_widgets) == len(kinds)
        self._kinds = kinds
        self.tabs.setTabText(1, f"아이콘 종류 ({len(kinds)})")
        if same:
            for i, k in enumerate(kinds):
                self._sync_kind(i)
            return
        self._build_kinds()

    def _build_kinds(self):
        t = self.kind_table
        t.setRowCount(0)
        t.setRowCount(len(self._kinds))
        self.kind_widgets = []
        for i, k in enumerate(self._kinds):
            img = QTableWidgetItem()
            img.setFlags(Qt.ItemFlag.ItemIsEnabled)
            pm = self._kind_pixmap(k)
            if pm is not None:
                img.setData(Qt.ItemDataRole.DecorationRole, pm)
            else:
                img.setText("그림 없음")
            t.setItem(i, 0, img)
            name, ko = QLineEdit(), QLineEdit()
            name.setPlaceholderText("영문·숫자·_")
            name.setCompleter(self._completer)
            ko.setPlaceholderText("한국어 이름")
            scale = NoWheelSpinBox()
            scale.setRange(0.3, 4.0)
            scale.setSingleStep(0.05)
            scale.setToolTip("한글 글자 높이 대비 아이콘 높이")
            not_icon = QCheckBox()
            uses = QWidget()
            ul = QHBoxLayout(uses)
            ul.setContentsMargins(8, 0, 4, 0)
            ul_label = QLabel()
            show = QPushButton("곳 보기")
            show.setStyleSheet("QPushButton { padding: 2px 10px; min-height: 18px; }")
            show.clicked.connect(lambda _c=False, g=tuple(k["groups"]), nm=k: self.show_groups(set(g), nm))
            ul.addWidget(ul_label)
            ul.addStretch(1)
            ul.addWidget(show)
            state = QTableWidgetItem()
            state.setFlags(Qt.ItemFlag.ItemIsEnabled)
            t.setCellWidget(i, 1, name)
            t.setCellWidget(i, 2, ko)
            t.setCellWidget(i, 3, scale)
            t.setCellWidget(i, 4, uses)
            t.setItem(i, 5, state)
            t.setCellWidget(i, 6, not_icon)
            t.setRowHeight(i, 56)
            w = {"name": name, "ko": ko, "scale": scale, "not_icon": not_icon, "uses": ul_label, "state": state,
                 "img": img}
            self.kind_widgets.append(w)
            self._sync_kind(i)
            name.editingFinished.connect(lambda r=i: self._edited(r, "name"))
            ko.editingFinished.connect(lambda r=i: self._edited(r, "ko"))
            scale.editingFinished.connect(lambda r=i: self._edited(r, "scale"))
            not_icon.toggled.connect(lambda _v, r=i: self._edited(r, "not_icon"))

    def _kind_pixmap(self, k: dict) -> QPixmap | None:
        path = k.get("image")
        if not path:
            return None
        key = ("kind", path)
        if key not in self._pm_cache:
            pm = QPixmap(path)
            self._pm_cache[key] = None if pm.isNull() else pm.scaled(
                56, 40, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        return self._pm_cache[key]

    def _sync_kind(self, i: int):
        """한 줄의 칸들을 데이터에 맞춘다 (치고 있는 칸은 건드리지 않는다)."""
        k, w = self._kinds[i], self.kind_widgets[i]
        for key, val in (("name", k["name"]), ("ko", k["ko"])):
            ed = w[key]
            if not ed.hasFocus() and ed.text() != val:
                ed.blockSignals(True)
                ed.setText(val)
                ed.blockSignals(False)
            ed.setProperty("last", ed.text())
        sc = w["scale"]
        if not sc.hasFocus():
            sc.blockSignals(True)
            sc.setValue(float(k["scale"] or 1.1))
            sc.blockSignals(False)
        w["scale"].setProperty("last", sc.value())
        ni = w["not_icon"]
        ni.blockSignals(True)
        ni.setChecked(bool(k["not_icon"]))
        ni.blockSignals(False)
        off = bool(k["not_icon"])
        for key in ("name", "ko", "scale"):
            w[key].setEnabled(not off)
        w["name"].setStyleSheet("" if off or w["name"].text().strip() else f"border: 1px solid {DOT['fix']};")
        w["ko"].setStyleSheet("" if off or w["ko"].text().strip() else "background: #FFE066;")
        names = k.get("names") or []
        multi = len(names) > 1
        if multi:
            w["name"].setPlaceholderText("여러 이름 (적으면 하나로 합침)")
            w["name"].setToolTip("지금 이름: " + ", ".join(names))
        else:
            w["name"].setToolTip("")
        w["uses"].setText(f"{k['uses']}곳")
        text = "여러 이름" if multi and not off else KIND_STATE.get(k["state"], k["state"])
        if off:
            text = "아이콘 아님"
        w["state"].setText(text)
        color = {"lost": CHIP_BG["fix"], "unnamed": CHIP_BG["fix"], "no_ko": "#FFF0B3", "pending": PENDING_BG,
                 "ok": "#FFFFFF"}.get(k["state"], "#FFFFFF")
        w["state"].setBackground(QBrush(QColor("#FFF0B3" if multi and not off else color)))

    def _edited(self, i: int, field: str):
        if i >= len(self._kinds) or i >= len(self.kind_widgets):
            return
        k, w = self._kinds[i], self.kind_widgets[i]
        if field == "name":
            val = w["name"].text().strip()
            if val == (w["name"].property("last") or "").strip():
                return
        elif field == "ko":
            val = w["ko"].text().strip()
            if val == (w["ko"].property("last") or "").strip():
                return
        elif field == "scale":
            val = float(w["scale"].value())
            if abs(val - float(w["scale"].property("last") or 0)) < 1e-9:
                return
        else:
            val = w["not_icon"].isChecked()
            if val == bool(k["not_icon"]):
                return
        note = ""
        if field == "name" and val and k["groups"]:
            # 다른 아이콘의 이름과 같으면 그림을 견준다: 같은 아이콘이면 그 이름, 다르면 숫자를 붙인 이름 (카드 보기와 같음)
            try:
                rn = service.resolve_icon_name(self.project_dir, k["groups"][0], val, exclude_groups=k["groups"])
            except Exception:  # noqa: BLE001
                rn = {"name": val, "renamed": False}
            if rn["renamed"]:
                note = f"‘{val}’은(는) 그림이 다른 아이콘의 이름이라 ‘{rn['name']}’(으)로 적었습니다."
            val = rn["name"]
        try:
            service.stage_icon_names(self.project_dir, [{"group": g, field: val} for g in k["groups"]])
        except Exception as e:  # noqa: BLE001
            show_problem(self, "이름을 적어 두지 못했습니다", str(e), "")
            return
        self.saved = True
        self._reload_after_stage()
        if note:
            QMessageBox.information(self, "다른 이름으로 적었습니다", note)

    def _reload_after_stage(self):
        self._cards = service.source_check_rows(self.project_dir, self.files)
        kinds = service.icon_kinds(self.project_dir)
        self._update_chips()
        self._rebuild_tree()
        if [k["groups"] for k in kinds] == [k["groups"] for k in self._kinds]:
            self._set_kinds(kinds)
        else:  # 줄이 달라졌다: 치던 칸을 지우지 않게 이벤트가 끝난 뒤에 다시 만든다
            QTimer.singleShot(0, lambda ks=kinds: self._guard(lambda: self._set_kinds(ks)))

    @staticmethod
    def _guard(fn):
        try:
            fn()
        except RuntimeError:  # 창이 이미 지워짐
            pass

    def show_groups(self, groups: set | None, kind: dict | None = None):
        """‘곳 보기’: 카드·영역 탭을 그 아이콘이 있는 영역만 거른다 (None이면 해제)."""
        self._group_filter = groups
        on = groups is not None
        self.group_label.setVisible(on)
        self.group_clear.setVisible(on)
        if on:
            nm = (kind or {}).get("name") or "이름 없는 아이콘"
            self.group_label.setText(f"아이콘 ‘{nm}’ 있는 영역만")
            self.tabs.setCurrentIndex(0)
        self._rebuild_tree()

    # ================================================================ 확정
    def confirm_all(self):
        keys = self.pending_keys()
        if not keys:
            return
        _wait_cursor(True)
        try:
            r = service.confirm_regions(self.project_dir, keys, mark_checked=False)
        finally:
            _wait_cursor(False)
        if r["state"] != "confirmed":
            show_problem(self, "아이콘 이름을 확정하지 못했습니다", "", "이름을 고친 뒤 다시 누르세요.",
                         items=[str(m) for m in r["blocking"]])
            return
        self.saved = True
        n_reread = len(r["reread"])
        self.enqueue_reread(r["reread"])
        self.refresh()
        parts = [f"확정한 영역 {max(len(keys) - len(r['still']), 0)}개"]
        if n_reread:
            parts.append(f"다시 읽는 중 {n_reread}개")
        if r["still"]:
            parts.append(f"아이콘이 덜 끝나 남은 영역 {len(r['still'])}개")
        QMessageBox.information(self, "모두 확정", "\n".join(parts))

    # ================================================================ 다시 읽기 (뒤에서)
    def enqueue_reread(self, cells: list[dict]):
        fresh = []
        for c in cells:
            nk = _nk((c["file_dir"], c["page"], c["region"]))
            if nk in self.reading:
                continue
            self.reading.add(nk)
            fresh.append({"file_dir": Path(str(c["file_dir"])), "page": c["page"], "region": c["region"]})
        self._queue.extend(fresh)
        if fresh and not self._closed:
            self._update_chips()
            self._rebuild_tree()
        self._start_next()

    def _start_next(self):
        if self._running or not self._queue:
            return
        batch, self._queue = self._queue, []
        self._running = True
        eng = self.engine or service.get_settings().engine
        job = worker.Job(service.reread_cells, batch, engine=eng)
        job.signals.progress.connect(lambda ev, s=self: s._guard(lambda: s._reread_progress(ev, len(batch))))
        job.signals.done.connect(lambda r, s=self, b=batch: s._guard(lambda: s._reread_done(r, b)))
        job.signals.failed.connect(lambda m, s=self, b=batch: s._guard(lambda: s._reread_failed(m, b)))
        job.signals.cancelled.connect(lambda s=self, b=batch: s._guard(lambda: s._reread_finish(b)))
        worker.start(job)

    def _reread_progress(self, ev, total: int):
        if self._closed:
            return
        self.reading_label.setText(f"다시 읽는 중 {getattr(ev, 'done', 0)}/{getattr(ev, 'total', total)}"
                                   f" (남은 영역 {len(self.reading)}개)")
        self.reading_label.setVisible(True)

    def _reread_finish(self, batch: list[dict]):
        for c in batch:
            self.reading.discard(_nk((c["file_dir"], c["page"], c["region"])))
        self._running = False
        self.saved = True
        if not self._closed:
            self.refresh()
        v = self._viewer  # 열린 카드 보기도: ‘읽는 중’을 풀고 새 읽은 결과(보통·정밀)를 보인다
        if v is not None:
            try:
                if v.isVisible():
                    v.background_done(batch)
            except RuntimeError:  # 지워진 창
                self._viewer = None
        self._start_next()
        if not self._running and not self._queue and self.on_idle is not None:
            self.on_idle()

    def _reread_done(self, r: dict, batch: list[dict]):
        self._reread_finish(batch)
        if r.get("state") != "done" or r.get("failed"):
            self._problem_box("아이콘이 바뀐 영역을 다 읽지 못했습니다", r.get("reason") or "",
                              f"{r.get('done', 0)}영역을 다시 읽었습니다. 남은 영역은 목록에서 ‘볼 것’으로 보입니다 "
                              "(카드 보기에서 ‘이 영역 다시 읽기’).",
                              [f"{k}: {why}" for k, why in r.get("failed") or []], r.get("retry_after"))

    def _reread_failed(self, msg: str, batch: list[dict]):
        self._reread_finish(batch)
        self._problem_box("다시 읽지 못했습니다", msg.splitlines()[0] if msg else "", "", [], None, detail=msg)

    def _problem_box(self, title, reason, todo, items, retry_after, detail=None):
        parent = self if self.isVisible() else self.parent()
        show_problem(parent, title, reason, todo, retry_after=retry_after, items=items, detail=detail)

    # ================================================================ 아이콘 다시 찾기
    def rescan(self):
        r = ask_choice(self, "아이콘 다시 찾기", "게임 전체에서 아이콘을 다시 찾을까요?",
                       [("다시 찾기", "destructive"), ("취소", "reject")], default=1,
                       informative="확정 전 이름·손으로 잡은 확정 전 자리가 사라집니다.")
        if r != 0:
            return
        eng = self.engine or service.get_settings().engine
        job = worker.Job(service.rescan_icons, self.project_dir, engine=eng, with_progress=False)

        def done(_r):
            self.saved = True
            self._guard(self.refresh)

        worker.run_with_dialog(self, "아이콘 다시 찾는 중", job, done,
                               lambda m: show_problem(self, "아이콘을 다시 찾지 못했습니다", m.splitlines()[0], "",
                                                      detail=m), cancellable=False)

    # ================================================================ 닫기
    def reject(self):
        if not self._dict_unsaved_ok():
            return
        if self.reading:
            QMessageBox.information(self, "다시 읽는 중", "다시 읽는 중입니다. 닫아도 뒤에서 계속 읽습니다.")
        self._closed = True
        v = self._viewer
        if v is not None:
            try:
                v.close()
                if v.isVisible():  # 카드 보기가 닫기를 거절함 (고친 원문을 두고 ‘취소’ 등): 목록 창도 닫지 않는다
                    self._closed = False
                    v.raise_()
                    v.activateWindow()
                    return
            except RuntimeError:
                pass
        super().reject()

    def done(self, code):
        self._closed = True
        super().done(code)

    def showEvent(self, e):
        self._closed = False
        super().showEvent(e)


def _wait_cursor(on: bool) -> None:
    from PySide6.QtWidgets import QApplication

    if on:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    else:
        QApplication.restoreOverrideCursor()
