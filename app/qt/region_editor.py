"""영역 지정 화면 (PRD 2단계). 카드 위에 사각형을 그리고 이름·종류·서식을 정해 그룹의 regions.json에 저장한다.

- 빈 곳을 끌면 새 영역, 영역 안을 끌면 옮기기, 가장자리를 끌면 크기 조절.
- 화살표 키로 조금씩(Shift: 크게) 옮기기, Alt+화살표로 크기 조절, Delete로 지우기, Ctrl+휠로 확대.
- 좌표는 카드 대비 비율로 저장한다. 손으로 쓴 regions.json의 모르는 항목은 그대로 둔다.
- 미리보기(G): 오른쪽에 다른 카드 16장을 같은 영역으로 겹쳐 보여 준다 (app.qt.region_preview). 영역을 고치면 바로 반영.
- 이 카드만 따로 영역: 공통 영역을 쓰는 그룹에서 상자 크기가 다른 카드만 따로 영역을 둔다 (regions.json page_regions,
  엔진은 그 카드에서 공통 영역 대신 이것을 쓴다).
- 자동 정렬: 카드마다 조금씩 밀린 그룹에서 공통 영역을 카드마다 옮겨 맞춘다 (service.align_group, 파일 폴더의 align.json).
  기준이 아닌 카드를 보면 상자를 그 카드의 정렬만큼 옮겨 그리고, 상자를 고치면 기준 카드 좌표로 되돌려 공통 영역을 고친다.
  Ctrl+화살표(Shift: 크게)는 지금 카드의 정렬만 옮긴다 (바로 align.json에 저장).
"""

from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from app import service
from app.qt.dialogs import PRIMARY_QSS, ask_choice, erase_modes, fit_to_screen, grey_note, hint_bar, section_title, set_hint, show_problem
from app.qt.region_align import RegionAlignPreviewMixin
from app.qt.region_form import RegionFormMixin
from app.qt.region_preview import PreviewPanel
from app.qt.region_scene import (  # noqa: F401 - 예전처럼 region_editor.이름으로도 쓴다
    ALIGN_LABELS, COLORS, EDGE, KIND_LABELS, MIN_SIZE, PageScene, PageView, RegionItem, SELECTED_Z,
)


class RegionEditor(RegionFormMixin, RegionAlignPreviewMixin, QDialog):
    """그룹의 영역 템플릿 편집기. 저장하면 saved 신호."""

    saved = Signal()

    def __init__(self, group_dir: Path, file_dirs: list[Path], parent: QWidget | None = None,
                 groups: list[dict] | None = None):
        """groups: 프로젝트의 모든 그룹 [{"name", "dir", "files": [파일 폴더]}]. 주면 파일 목록에서 다른 그룹의
        파일을 골라 그 그룹의 영역으로 옮겨 갈 수 있다."""
        super().__init__(parent)
        self.group_dir = Path(group_dir)
        self.file_dirs = [Path(f) for f in file_dirs]
        self.groups = [{"name": g["name"], "dir": Path(g["dir"]), "files": [Path(f) for f in g["files"]]}
                       for g in (groups or []) if g["files"]] or [
            {"name": self.group_dir.name, "dir": self.group_dir, "files": self.file_dirs}]
        self.template = service.load_region_template(self.group_dir)
        self.fonts = service.font_choices()
        self.dirty = False
        self._loading = False
        self._ov_key: str | None = None  # 공통 영역 그룹에서 지금 보는 카드가 ‘따로 영역’이면 그 카드 키
        self._common: tuple[list, list] = ([], [])  # 따로 영역 카드를 보는 동안 넣어 둔 공통 영역
        self._view_t: dict | None = None  # 지금 카드에 그린 상자의 정렬 변환 (공통 영역 + 자동 정렬 카드일 때만)
        self._align_cache: dict = {}  # 파일 폴더 -> {쪽: 정렬 값} (service.align_info)
        self.setWindowTitle(f"영역 지정 — {self.group_dir.name}")
        fit_to_screen(self, 1400, 900, maximize_below=0)

        self.scene = PageScene(self)
        self.view = PageView(self.scene)
        self.pix_item = None
        self._build_ui()
        for b in self.findChildren(QPushButton):  # 이름 칸 Enter가 ‘화면에 맞춤’ 같은 버튼을 누르지 않게
            b.setAutoDefault(False)
            b.setDefault(False)
        self._load_page()
        self._load_regions()
        self.scene.selectionChanged.connect(self._on_selection)
        self.scene.changed_item.connect(self._on_geometry)
        self.scene.moving_item.connect(lambda _it: self.preview.schedule())
        self.scene.created_item.connect(self._on_created)
        self.view.nudged.connect(self._on_geometry)
        self.view.delete_pressed.connect(self.delete_selected)
        self.view.page_step.connect(self.step_page)
        self.view.page_jump.connect(self.jump_page)
        self.view.key_action.connect(self._key_action)
        self.view.align_nudge.connect(self.nudge_alignment)
        QShortcut(QKeySequence.StandardKey.Save, self, activated=self.save)
        for seq, fn in (("Ctrl+C", self.copy_regions), ("Ctrl+V", self.paste_regions),
                        ("Ctrl+Shift+D", self.duplicate_selected), ("Ctrl+Shift+V", self.copy_previous_card),
                        ("F1", lambda: self.help_btn.toggle())):
            sc = QShortcut(QKeySequence(seq), self, activated=fn)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._clip: list[dict] = []

    def _widen_file_popup(self):
        """긴 ‘그룹 / 파일’ 이름이 목록에서 잘리지 않게 펼친 목록을 가장 긴 이름 폭에 맞춘다 (화면 폭 안에서).
        칸에 다 안 들어가는 이름은 가운데를 줄이고, 마우스를 올리면 전체 이름이 나온다."""
        cb = self.file_combo
        view = cb.view()
        view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        fm = cb.fontMetrics()
        widest = max((fm.horizontalAdvance(cb.itemText(i)) for i in range(cb.count())), default=0)
        scr = self.screen() or QGuiApplication.primaryScreen()
        cap = int(scr.availableGeometry().width() * 0.8) if scr else 900
        view.setMinimumWidth(min(widest + 60, cap))  # 글자 + 여백·스크롤 막대
        cb.setMinimumWidth(min(widest + 76, 480))  # 칸도 이름이 다 보이게 (너무 길면 480에서 멈춤, 전체는 말풍선)
        cb.setToolTip(cb.currentText())
        cb.currentIndexChanged.connect(lambda _i: cb.setToolTip(cb.currentText()))

    # ------------------------------------------------------------ 화면 구성
    def _build_ui(self):
        top = self._build_file_row()
        tools = self._build_tools_row()
        self._build_top_actions(top)
        side = self._build_side_panel()
        center = self._build_page_nav()
        split = self._build_splitter(center, side)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.addLayout(top)
        lay.addLayout(tools)
        lay.addWidget(self.cards_bar)
        lay.addWidget(split, 1)
        self._build_status_row(lay)
        self._finish_build()

    def _build_file_row(self) -> QHBoxLayout:
        """맨 윗줄: 그룹/파일·쪽 (오른쪽 단추는 _build_top_actions)."""
        top = QHBoxLayout()
        top.addWidget(QLabel("그룹 / 파일"))
        self.file_combo = QComboBox()
        self.file_combo.setMinimumWidth(200)
        self.file_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.file_combo.setMinimumContentsLength(18)
        for g in self.groups:  # 모든 그룹의 파일. 다른 그룹 파일을 고르면 그 그룹 영역으로 옮겨 간다
            for fd in g["files"]:
                full = f"{g['name']} / {fd.name}"
                self.file_combo.addItem(full, (str(g["dir"]), str(fd)))
                self.file_combo.setItemData(self.file_combo.count() - 1, full, Qt.ItemDataRole.ToolTipRole)
        cur = next((i for i in range(self.file_combo.count())
                    if Path(self.file_combo.itemData(i)[0]) == self.group_dir), 0)
        self.file_combo.setCurrentIndex(cur)
        self._combo_index = cur
        self._widen_file_popup()
        top.addWidget(self.file_combo)
        top.addWidget(QLabel("쪽"))
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        top.addWidget(self.page_spin)
        self.page_total = QLabel()
        top.addWidget(self.page_total)
        return top

    def _build_tools_row(self) -> QHBoxLayout:
        """둘째 줄(영역 추가·영역 방식)과 셋째 줄(카드마다 영역 도구) 단추. 둘째 줄 레이아웃을 돌려준다."""
        tools = QHBoxLayout()  # 둘째 줄: 영역 추가·영역 방식
        tools.setSpacing(3)
        cap = QLabel("영역 그리기")
        cap.setStyleSheet("color: #3A3A3C; font-weight: bold;")
        tools.addWidget(cap)
        self.add_buttons = {}
        for kind, text, key in (("text", "+ 글자 영역", "T"), ("tag_pills", "+ 태그 라벨 영역", "L"),
                                ("protect", "+ 보호 영역", "P"), ("ocr_exclude", "+ 읽기 제외 영역", "O")):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(f"카드 위를 끌어서 새 영역을 그립니다 (단축키 {key})")
            b.setCheckable(True)
            b.clicked.connect(lambda checked, k=kind: self._set_add_kind(k if checked else None))
            self.add_buttons[kind] = b
            tools.addWidget(b)
        tools.addSpacing(6)
        self.per_card = QCheckBox("카드마다 영역 지정")
        self.per_card.setToolTip("카드마다 형식이 달라 공통 영역을 쓸 수 없는 그룹: 카드 한 장씩 영역을 정합니다")
        self.per_card.setChecked(self.template.get("layout") == "per_card")
        self.per_card.toggled.connect(self._toggle_per_card)
        tools.addWidget(self.per_card)
        self.override_btn = QPushButton("이 카드만 따로 영역")
        self.override_btn.setCheckable(True)
        self.override_btn.setToolTip("이 카드만 영역의 위치·크기를 다르게 씁니다 (글자 상자 크기가 다른 카드). 서식·글꼴은 공통 영역을 따르고,\n"
                                     "다른 카드는 공통 영역 그대로. 영역을 모두 지우면 이 카드는 처리하지 않습니다. 다시 누르면 공통 영역으로")
        self.override_btn.setStyleSheet("QPushButton:checked { background: #FFF4CC; border: 2px solid #E0A800; font-weight: bold; }")
        self.override_btn.toggled.connect(self._toggle_override)  # 아래 카드 정보 줄에 둔다 (창 최소 폭)
        self.align_btn = QPushButton("자동 정렬")
        self.align_btn.setToolTip("카드마다 조금씩 밀리거나 크기가 다른 그룹에서, 공통 영역을 카드마다 테두리·글 상자에 맞춰 옮깁니다.\n"
                                  "기준은 그룹 첫 파일의 기준 카드입니다. 맞춘 뒤 Ctrl+화살표로 카드마다 미세 조정할 수 있습니다.\n"
                                  "이미 읽은(추출한) 카드는 다시 읽지 않습니다")
        self.align_btn.clicked.connect(self.run_alignment)  # 아래 카드 정보 줄에 둔다 (창 최소 폭)
        self.align_reset_btn = QPushButton("이 카드 정렬 되돌리기")
        self.align_reset_btn.setToolTip("손으로 맞춘 정렬은 자동 정렬 값으로, 자동 정렬은 정렬 없음(공통 영역 그대로)으로 되돌립니다.\n"
                                        "바로 저장됩니다")
        self.align_reset_btn.clicked.connect(self.reset_alignment)
        self.copy_prev = QPushButton("이전 카드 영역 복사")
        self.copy_prev.setToolTip("앞 카드의 영역을 이 카드로 가져옵니다 (Ctrl+Shift+V)")
        self.copy_prev.clicked.connect(self.copy_previous_card)
        cards = QHBoxLayout()  # 셋째 줄: 카드마다 영역 도구 (카드마다일 때만 보인다)
        cards.setContentsMargins(0, 0, 0, 0)
        cards.addWidget(self.copy_prev)
        self.copy_rest = QPushButton("나머지 카드에 복사")
        self.copy_rest.setToolTip("지금 카드의 영역을 같은 그룹의 다른 카드에 복사합니다 (영역 없는 카드에만, 또는 모두 덮어쓰기)")
        self.copy_rest.clicked.connect(self.copy_to_rest)
        cards.addWidget(self.copy_rest)
        self.reset_btn = QPushButton("초기화…")
        self.reset_btn.setToolTip("이 카드(또는 모든 카드)의 영역을 비웁니다. 저장하기 전까지는 파일에 쓰지 않습니다")
        self.reset_btn.clicked.connect(self.reset_regions)
        self.sync_names = QCheckBox("설정은 같은 이름 영역에 함께")
        self.sync_names.setToolTip("카드마다 영역일 때: 영역의 이름·종류·회전·정렬·색·글꼴·지우기 방식을 바꾸면\n"
                                   "다른 카드의 같은 이름 영역에도 똑같이 적용합니다 (위치·크기는 카드마다 따로)")
        self.sync_names.setChecked(True)
        cards.addWidget(self.sync_names)
        cards.addStretch(1)
        self.cards_row = cards
        self.cards_bar = QWidget()
        self.cards_bar.setLayout(cards)
        self.preview_btn = QPushButton("미리보기 (G)")
        self.preview_btn.setCheckable(True)
        self.preview_btn.setStyleSheet("QPushButton:checked { background: #1E6FD9; color: white; font-weight: bold; }")
        self.preview_btn.setToolTip("오른쪽에 다른 카드 16장을 같은 영역으로 보여 줍니다 — 영역이 그림·테두리에 걸치는 카드를 찾고,\n"
                                    "카드를 빼거나 그 카드만 따로 영역을 정할 수 있습니다 (끄면 영역 설정 칸이 다시 보입니다)")
        self.preview_btn.toggled.connect(self._toggle_preview)
        tools.addStretch(1)
        tools.addWidget(self.reset_btn)
        return tools

    def _build_top_actions(self, top: QHBoxLayout) -> None:
        """맨 윗줄 오른쪽: 확대·축소·화면에 맞춤·저장·닫기."""
        top.addStretch(1)
        for text, f, tip in (("－", 1 / 1.25, "축소 (Ctrl+휠 아래)"), ("＋", 1.25, "확대 (Ctrl+휠 위)")):
            zb = QPushButton(text)
            zb.setFixedWidth(36)
            zb.setStyleSheet("font-size: 13pt; font-weight: bold; padding: 0 4px;")
            zb.setToolTip(tip + " · 확대한 뒤 가운데 버튼(휠)을 누른 채 끌거나 Space+끌기로 옮기기")
            zb.clicked.connect(lambda _c=False, ff=f: self.view.zoom(ff))
            top.addWidget(zb)
        fit = QPushButton("화면에 맞춤 (F)")
        fit.clicked.connect(self.view.fit_again)
        top.addWidget(fit)
        self.save_btn = QPushButton("저장 (Ctrl+S)")  # 저장 안 한 변경이 있을 때만 파랗게 (_update_title)
        self.save_btn.clicked.connect(self.save)
        top.addWidget(self.save_btn)
        close = QPushButton("닫기")
        close.clicked.connect(self.close)
        top.addWidget(close)

    def _build_side_panel(self) -> QWidget:
        """오른쪽 칸: 영역 목록 + 입력란 + 도움말. 안쪽 위젯을 돌려준다."""
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(4, 0, 4, 0)
        sl.setSpacing(8)
        lrow = QHBoxLayout()
        lrow.addWidget(section_title("이 카드의 영역"))
        lrow.addWidget(grey_note("위에서부터 처리"), 1)
        sl.addLayout(lrow)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_list_row)
        sl.addWidget(self.list, 1)
        order = QHBoxLayout()
        for text, d in (("위로", -1), ("아래로", 1)):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, dd=d: self.move_selected(dd))
            order.addWidget(b)
        dele = QPushButton("지우기")
        dele.clicked.connect(self.delete_selected)
        order.addWidget(dele)
        sl.addLayout(order)
        self._build_region_form(sl)
        self._build_help(sl)
        self._connect_form_signals()
        return side

    def _build_region_form(self, sl: QVBoxLayout) -> None:
        """고른 영역의 입력란(기본·한글 넣기·글자 지우기·메모)."""
        form = QFormLayout()
        self.f_name = QLineEdit()
        self.f_kind = QComboBox()
        for k, t in KIND_LABELS:
            self.f_kind.addItem(t, k)
        self.f_x, self.f_y, self.f_w, self.f_h = (self._ratio_spin() for _ in range(4))
        from PySide6.QtWidgets import QGridLayout

        xy = QGridLayout()  # 두 줄: x·y / 너비·높이 (좁은 칸에서도 숫자가 보이게)
        for i, (lab, sp) in enumerate((("왼쪽", self.f_x), ("위쪽", self.f_y), ("너비", self.f_w), ("높이", self.f_h))):
            # "0.0863" + 위/아래 버튼이 늘 들어갈 폭 (폼 라벨 열과 무관하게 한 줄 전체를 씀)
            sp.setMinimumWidth(sp.fontMetrics().horizontalAdvance("0.0000") + 44)
            xy.addWidget(QLabel(lab), i // 2, (i % 2) * 2)
            xy.addWidget(sp, i // 2, (i % 2) * 2 + 1)
        xy.setColumnStretch(1, 1)
        xy.setColumnStretch(3, 1)
        xy.setContentsMargins(0, 0, 0, 0)
        self.f_rotate = QCheckBox("180도 돌려 인쇄된 영역")
        self.f_rotate.setToolTip("카드 위쪽 반처럼 거꾸로 인쇄된 글자")
        self.f_align = QComboBox()
        self.f_align.setToolTip("한글을 놓는 방법\n"
                                "자동: 원문 줄마다 찾은 정렬을 따름 · 왼쪽/오른쪽: 글 칸의 왼쪽/오른쪽 끝에 맞춤\n"
                                "가운데: 영역 가운데 · 정렬 안 함: 줄마다 원문이 시작한 자리에서 시작")
        for k, t in ALIGN_LABELS:
            self.f_align.addItem(t, k)
        self.f_color = QLineEdit()
        self.f_color.setPlaceholderText("#000000")
        color_btn = QPushButton("고르기")
        color_btn.clicked.connect(self._pick_color)
        crow = QHBoxLayout()
        crow.addWidget(self.f_color)
        crow.addWidget(color_btn)
        self.f_font = QComboBox()
        self._fill_fonts()
        for cb in (self.f_font, self.f_kind, self.f_align):  # 긴 항목이 오른쪽 칸 폭을 늘리지 않게
            cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(12)
        self.f_font_mode = QComboBox()
        self.f_font_mode.addItem("한 칸 모두 같은 글꼴", "cell")
        self.f_font_mode.addItem("문단마다 글꼴 지정", "sentence")
        self.f_font_mode.setToolTip("한글 넣기 전 글꼴 창에서 글꼴을 고르는 단위\n"
                                    "한 칸 모두 같은 글꼴: 카드의 이 칸 전체를 한 글꼴로 (기본)\n"
                                    "문단마다 글꼴 지정: 칸 안의 문단(효과·설명·말한 사람 등)마다 다른 글꼴을 고를 수 있음")
        self.f_keep_tall = QDoubleSpinBox()
        self.f_keep_tall.setRange(0, 200)
        self.f_keep_tall.setDecimals(1)
        self.f_keep_tall.setSuffix(" 포인트")
        self.f_keep_tall.setSpecialValueText("안 함")
        self.f_keep_tall.setToolTip("원래 카드의 글자·그림 중 이 크기보다 큰 것은 지우지 않고 남깁니다.\n"
                                    "예: 큰 장식 글자나 로고. 크기는 글자 크기 단위(포인트, 보통 본문 글자가 8~12 안팎)입니다.\n"
                                    "‘안 함’이면 크기와 상관없이 영역 안을 모두 지웁니다.")
        self.f_keep_tall.setMinimumWidth(self.f_keep_tall.fontMetrics().horizontalAdvance("200.0 포인트") + 44)
        self.f_erase_mode = QComboBox()
        for k, t in erase_modes():
            self.f_erase_mode.addItem(t, k)
        self.f_erase_mode.setToolTip("글자 뒤가 그림인 영역에서 원래 글자를 어떻게 지울지 정합니다. 결과 보기에서 카드마다 바꿀 수도 있습니다.\n"
                                     "글자가 따로 들어 있는 PDF는 ‘자동’이면 예전 방식으로 지웁니다.")
        self.f_icons = QComboBox()  # 단추가 아닌 선택 상자 (단추 코드 순서를 바꾸지 않게)
        self.f_icons.addItem("섞여 있음", "")
        self.f_icons.addItem("글자만 (아이콘 없음)", "none")
        self.f_icons.setToolTip("이 영역에 문장 속 아이콘(그림 기호)이 있는지 정합니다.\n"
                                "섞여 있음: 글자 사이에 아이콘이 있을 수 있음 (기본)\n"
                                "글자만 (아이콘 없음): 글자를 읽을 때 아이콘을 찾지 않고, ② 원문 확인의 아이콘·글자 지우기·한글 넣기에서도\n"
                                "이 영역은 아이콘을 찾지 않습니다. 카드 이름처럼 글자만 있는 칸에 쓰면 잘못 찾은 아이콘이 줄어듭니다.\n"
                                "글자 영역에서만 고를 수 있습니다.")
        self.f_ocr_exclude = QCheckBox("글자를 읽을 때도 건너뛰기")
        self.f_ocr_exclude.setToolTip("카드 번호·장식 글자처럼 번역하지 않을 글자: 글자를 읽을 때(추출) 가리고, 지우지도 않습니다")
        self.f_keep_above = QCheckBox("글자 위쪽 그림은 남기기")
        self.f_keep_above.setToolTip("글자 줄 바로 위에 붙은 그림은 지우지 않습니다. 예: 이름 위의 왕관 아이콘")
        self.f_notes = QPlainTextEdit()
        self.f_notes.setFixedHeight(70)
        def group(title: str) -> tuple[QGroupBox, QFormLayout]:
            box = QGroupBox(title)
            fl = QFormLayout(box)
            return box, fl

        self.g_basic, fb = group("기본")
        fb.addRow("이름", self.f_name)
        fb.addRow("종류", self.f_kind)
        fb.addRow("아이콘", self.f_icons)
        fb.addRow(grey_note("위치·크기 (카드 전체를 1로 본 비율)"))  # 라벨 열을 빌리지 않고 폼 전체 폭을 쓴다
        fb.addRow(xy)
        fb.addRow("보호 영역", self.f_ocr_exclude)  # 보호 영역일 때만 보인다
        self.g_style, fs = group("한글 넣기")
        fs.addRow("회전", self.f_rotate)
        fs.addRow("정렬", self.f_align)
        fs.addRow("글자 색", crow)
        fs.addRow("한글 글꼴", self.f_font)
        fs.addRow("글꼴 단위", self.f_font_mode)
        self.g_erase, fe = group("글자 지우기")
        fe.addRow("지우는 방법", self.f_erase_mode)
        fe.addRow("큰 글자·그림은 남기기", self.f_keep_tall)
        fe.addRow("", self.f_keep_above)
        self.g_notes, fn = group("메모")
        fn.addRow(self.f_notes)
        self._basic_form = fb
        self.form_box = QWidget()
        fv = QVBoxLayout(self.form_box)
        fv.setContentsMargins(0, 0, 0, 0)
        for g in (self.g_basic, self.g_style, self.g_erase, self.g_notes):
            fv.addWidget(g)
        self.form_hint = grey_note("영역을 고르면 여기에서 이름·글꼴·지우기 방식을 바꿀 수 있습니다.\n"
                                   "카드 위의 영역을 누르거나 위 목록에서 고르세요.")
        sl.addWidget(self.form_box)
        sl.addWidget(self.form_hint)
        sl.addStretch(1)

    def _build_help(self, sl: QVBoxLayout) -> None:
        """단축키 도움말 단추·글."""
        help_text = ("새 영역: T 글자 · L 태그 라벨 · P 보호 · O 읽기 제외 (누른 뒤 카드 위를 끌기) · 안쪽 끌기: 옮기기 · 가장자리 끌기: 크기\n"
                     "고른 영역: 화살표 조금씩 옮기기(Shift: 크게) · Alt+화살표 크기 · R 180도 회전 · Delete 지우기 · Esc 해제\n"
                     "Tab / Shift+Tab: 다음·이전 영역 · Ctrl+C / Ctrl+V: 영역 복사·붙여넣기(다른 카드로) · Ctrl+Shift+D: 복제 (Ctrl+D는 앱 전체 디버깅 모드)\n"
                     "Ctrl+화살표: 이 카드의 정렬만 옮기기 (모든 상자 함께, Shift: 크게) — 자동 정렬한 공통 영역 그룹\n"
                     "쪽: ←/→(영역을 안 골랐을 때) · PageUp/PageDown · Home/End 처음·끝 · 마우스 휠\n"
                     "F: 화면에 맞춤 · ＋/－ 또는 Ctrl+휠: 확대·축소 · 휠 누른 채 끌기 또는 Space+끌기: 옮기기 · Ctrl+Shift+V: 이전 카드 영역 · "
                     "Ctrl+S 저장 · F1 이 도움말")
        help_ = QLabel(help_text)
        help_.setStyleSheet("color: #6E6E73; background: white; border: 1px solid #E5E5EA; border-radius: 8px; padding: 8px;")
        self.help_label = help_
        self.help_btn = QToolButton()
        self.help_btn.setText("단축키 도움말 (F1)")
        self.help_btn.setCheckable(True)
        self.help_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.help_btn.setToolTip(help_text)
        self.help_btn.setAutoRaise(True)
        self.help_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.help_btn.toggled.connect(self._toggle_help)
        scr = self.screen() or QGuiApplication.primaryScreen()
        low = scr is not None and scr.availableGeometry().height() < 900  # 낮은 화면에서는 접어 둔다
        self.help_btn.setChecked(not low)
        self._toggle_help(not low)
        sl.addWidget(self.help_btn)
        sl.addWidget(help_)

    def _connect_form_signals(self) -> None:
        for w in (self.f_name,):
            w.textEdited.connect(self._from_form)
        self.f_name.returnPressed.connect(self.view.setFocus)  # Enter: 카드 화면으로 (화살표로 바로 옮기기)
        for w in (self.f_kind, self.f_align, self.f_font, self.f_font_mode, self.f_erase_mode, self.f_icons):
            w.currentIndexChanged.connect(self._from_form)
        for w in (self.f_x, self.f_y, self.f_w, self.f_h, self.f_keep_tall):
            w.valueChanged.connect(self._from_form)
        for w in (self.f_rotate, self.f_keep_above, self.f_ocr_exclude):
            w.toggled.connect(self._from_form)
        self.f_color.editingFinished.connect(self._from_form)
        self.f_notes.textChanged.connect(self._from_form)

    def _build_page_nav(self) -> QWidget:
        """카드 화면 좌우의 이전·다음 쪽 단추."""
        # 이미지 좌우의 이전·다음 쪽 버튼
        self.prev_btn, self.next_btn = QToolButton(), QToolButton()
        for b, text, tip, d in ((self.prev_btn, "◀", "이전 쪽 (←, PageUp)", -1),
                                (self.next_btn, "▶", "다음 쪽 (→, PageDown)", 1)):
            b.setText(text)
            b.setToolTip(tip)
            b.setFixedWidth(34)
            b.setSizePolicy(b.sizePolicy().horizontalPolicy(), b.sizePolicy().Policy.Expanding)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _c=False, dd=d: self.step_page(dd))
        center = QWidget()
        cl = QHBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(2)
        cl.addWidget(self.prev_btn)
        cl.addWidget(self.view, 1)
        cl.addWidget(self.next_btn)
        return center

    def _build_splitter(self, center: QWidget, side: QWidget) -> QSplitter:
        """카드 칸 | 오른쪽 칸(스크롤) | 미리보기."""
        side.setMinimumWidth(340)
        # 낮은 화면(768)에서 폼이 찌그러지지 않게, 모자라면 오른쪽 칸 안에서 스크롤
        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setFrameShape(QFrame.Shape.NoFrame)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side_scroll.setWidget(side)
        side_scroll.setMinimumWidth(360)
        self.list.setMinimumHeight(90)
        self.list.setMaximumHeight(max(90, (self.list.fontMetrics().height() + 8) * 6 + 8))  # 빈 공간은 폼에게
        self.help_label.setWordWrap(True)
        split = QSplitter()
        split.addWidget(center)
        split.addWidget(side_scroll)
        self.side_scroll = side_scroll
        self.preview = PreviewPanel(self._preview_regions, self._preview_info)
        self.preview.open_page.connect(self._open_from_preview)
        self.preview.card_action.connect(self._preview_card_action)
        self.preview.set_excluded.connect(self.set_page_excluded)
        self.preview.setVisible(False)
        split.addWidget(self.preview)
        split.setSizes([1000, 420])  # 처음에는 카드 칸을 넓게
        self.split = split
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        return split

    def _build_status_row(self, lay: QVBoxLayout) -> None:
        """아래 줄: 미리보기·따로 영역·정렬 단추와 안내 띠."""
        self.status = QLabel()
        self.card_info = QLabel()
        self.card_info.setStyleSheet("color: #0071E3; font-weight: bold")
        self.card_info.setMinimumWidth(60)  # 정렬 상태까지 붙어 길어져도 창 최소 폭을 늘리지 않게 (좁으면 잘림)
        self.guide = hint_bar("", "info")  # 다음 할 일 안내 띠
        srow = QHBoxLayout()  # 아래 줄: 미리보기·따로 영역 (위 두 줄은 파일 이름·도구로 이미 넓다)
        srow.addWidget(self.preview_btn)
        srow.addWidget(self.override_btn)
        srow.addWidget(self.align_btn)
        srow.addWidget(self.align_reset_btn)
        srow.addWidget(self.card_info)
        srow.addWidget(self.guide, 1)
        srow.addWidget(self.status)
        lay.addLayout(srow)

    def _finish_build(self) -> None:
        """처음 쪽 값을 넣고 쪽·파일 바뀜 신호를 잇는다."""
        self.page_spin.setValue(int(self.template.get("reference_page", 1) or 1))
        self.page_spin.valueChanged.connect(lambda _v: self._change_page())
        self.file_combo.currentIndexChanged.connect(self._on_file_combo)
        self._update_form_enabled()
        self._sync_guide()

    # ------------------------------------------------------------ 쪽·영역 불러오기
    def current_file(self) -> Path:
        return Path(self.file_combo.currentData()[1])

    def go_to(self, file_dir: Path | str, page: int) -> None:
        """그 파일의 그 쪽(카드)을 연다 (카드 보기 ‘이 카드 영역 고치기’). 같은 그룹의 파일만."""
        import os

        want = os.path.normcase(os.path.abspath(str(file_dir)))
        idx = next((i for i in range(self.file_combo.count())
                    if os.path.normcase(os.path.abspath(self.file_combo.itemData(i)[1])) == want
                    and Path(self.file_combo.itemData(i)[0]) == self.group_dir), None)
        if idx is not None and idx != self.file_combo.currentIndex():
            self.file_combo.setCurrentIndex(idx)  # currentIndexChanged -> _on_file_combo -> _change_page
        if self.page_spin.value() != int(page):
            self.page_spin.setValue(int(page))  # valueChanged -> _change_page

    def _on_file_combo(self, idx: int):
        gdir = Path(self.file_combo.itemData(idx)[0])
        if gdir == self.group_dir:
            self._combo_index = idx
            self._change_page()
            return
        if not self._confirm_leave():  # 저장 안 한 변경이 있으면 묻는다. 취소면 원래 파일로
            self.file_combo.blockSignals(True)
            self.file_combo.setCurrentIndex(self._combo_index)
            self.file_combo.blockSignals(False)
            return
        self._combo_index = idx
        self.switch_group(gdir)

    def switch_group(self, gdir: Path):
        """다른 그룹의 영역 템플릿으로 바꾼다 (같은 창에서)."""
        for it in self.items():
            self.scene.removeItem(it)
        self._order = []
        self.group_dir = Path(gdir)
        g = next(g for g in self.groups if g["dir"] == self.group_dir)
        self.file_dirs = g["files"]
        self.template = service.load_region_template(self.group_dir)
        if hasattr(self, "_card_store"):
            del self._card_store
        self._ov_key = None
        self.__dict__.pop("_shown_cache", None)
        self.per_card.blockSignals(True)
        self.per_card.setChecked(self.template.get("layout") == "per_card")
        self.per_card.blockSignals(False)
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(9999)
        self.page_spin.setValue(int(self.template.get("reference_page", 1) or 1))
        self.page_spin.blockSignals(False)
        self._load_page()
        self._load_regions()
        self.dirty = False
        self._update_title()
        self._update_form_enabled()
        self.view.fit()
        self._sync_preview(force=True)
        self.status.setText(f"그룹: {g['name']}")

    def _toggle_help(self, shown: bool):
        self.help_label.setVisible(shown)
        self.help_btn.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)

    def _confirm_leave(self) -> bool:
        """바뀐 영역이 있으면 저장할지 묻는다. 계속해도 되면 True."""
        if not self.dirty:
            return True
        r = ask_choice(self, "저장하지 않은 변경", f"‘{self.group_dir.name}’의 바뀐 영역을 저장할까요?",
                       [("저장", "accept"), ("저장 안 함", "destructive"), ("취소", "reject")], default=0)
        if r is None or r == 2:
            return False
        if r == 0:
            return self.save()
        self.dirty = False
        return True

    def _shown(self, fd: Path | None = None) -> list[int]:
        """이 파일에서 보일 쪽 (뺄 쪽은 건너뛴다)."""
        fd = fd or self.current_file()
        cache = self.__dict__.setdefault("_shown_cache", {})
        if fd not in cache:
            cache[fd] = service.shown_pages(fd)
        return cache[fd]

    def _load_page(self):
        fd = self.current_file()
        n = service.page_count(fd)
        shown = self._shown(fd)
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(max(1, n))
        if self.page_spin.value() not in shown:  # 뺀 쪽이면 가까운 보일 쪽으로
            v = self.page_spin.value()
            self.page_spin.setValue(next((p for p in shown if p >= v), shown[-1]))
        self.page_spin.blockSignals(False)
        hidden = n - len(shown)
        self.page_total.setText(f"/ {n}" + (f" (뺀 쪽 {hidden}개 건너뜀)" if hidden else ""))
        self._sync_nav()
        png, _size = service.page_image(fd, self.page_spin.value(), dpi=150)
        pm = QPixmap()
        pm.loadFromData(png, "PNG")
        if self.pix_item is None:
            self.pix_item = self.scene.addPixmap(pm)
            self.pix_item.setZValue(-1)
        else:
            self.pix_item.setPixmap(pm)
        self.scene.page_rect = QRectF(0, 0, pm.width(), pm.height())
        self.scene.setSceneRect(self.scene.page_rect.adjusted(-20, -20, 20, 20))

    def step_page(self, d: int):
        """이전·다음 쪽 (뺄 쪽은 건너뛰고, 파일 끝에서는 멈춘다). 영역은 같은 비율로 다음 카드에 겹쳐 보인다."""
        v, shown = self.page_spin.value(), self._shown()
        p = next((q for q in (shown if d > 0 else reversed(shown)) if (q > v if d > 0 else q < v)), None)
        if p is not None:
            self.page_spin.setValue(p)  # valueChanged -> _change_page
        self._sync_nav()

    def _sync_nav(self):
        if hasattr(self, "prev_btn"):
            v, shown = self.page_spin.value(), self._shown()
            self.prev_btn.setEnabled(any(p < v for p in shown))
            self.next_btn.setEnabled(any(p > v for p in shown))

    def _change_page(self):
        shown = self._shown()
        v = self.page_spin.value()
        if v not in shown:  # 쪽 번호를 직접 넣어 뺀 쪽에 오면 움직인 쪽으로 가까운 보일 쪽에
            prev = getattr(self, "_last_page", v)
            nxt = (next((p for p in shown if p > v), None) if v >= prev
                   else next((p for p in reversed(shown) if p < v), None))
            nxt = nxt or min(shown, key=lambda p: abs(p - v))
            self.page_spin.blockSignals(True)
            self.page_spin.setValue(nxt)
            self.page_spin.blockSignals(False)
        self._last_page = self.page_spin.value()
        self._change_page_impl()

    def _change_page_impl(self):
        if self.is_per_card():  # 카드마다 영역: 보던 카드의 영역을 넣어 두고 새 카드의 영역을 꺼낸다
            self._stash_card()
            self._clear_items()
            self._load_page()
            self._current_key = self.card_key()
            self._load_card(self._current_key)
            self.view.fit()
            self._sync_preview()
            return
        leaving = self._ov_key
        cur = self._items_to_dicts()
        if leaving:  # 따로 영역을 보던 카드: 고친 것을 넣어 둔다
            self._cards()[leaving] = {"regions": cur[0], "protect_rects": cur[1]}
        else:
            self._common = cur
        ratios = [(it, self.ratio_of(it)) for it in self.items()]
        self._load_page()
        ov = self._cards().get(self.card_key())
        self._view_t = None if ov is not None else self._align_t()  # 새 카드의 자동 정렬 (따로 영역이면 없음)
        if ov is not None:  # 이 카드만 따로 영역 (영역을 다 지운 빈 것도: 그 카드는 처리하지 않는다)
            self._clear_items()
            self._load_dicts(ov.get("regions", []), ov.get("protect_rects", []))
            self._ov_key = self.card_key()
        elif leaving:  # 따로 영역 카드에서 공통 영역 카드로
            self._clear_items()
            self._load_dicts(*self._common)
            self._ov_key = None
        else:
            for it, r in ratios:  # 다른 카드에 같은 비율로 겹쳐 본다 (그 카드의 정렬만큼 옮겨)
                self._place(it, r)
        self._snap_shared()
        self._update_card_info()
        self.view.fit()
        self._sync_preview()

    # ------------------------------------------------------------ 카드마다 영역
    def is_per_card(self) -> bool:
        return hasattr(self, "per_card") and self.per_card.isChecked()

    def card_key(self) -> str:
        return f"{self.current_file().name}/{self.page_spin.value()}"

    def _cards(self) -> dict:
        """카드 키 -> {"regions": [...], "protect_rects": [...]} (편집 중인 값)."""
        if not hasattr(self, "_card_store"):
            self._card_store = {k: copy.deepcopy(v) for k, v in (self.template.get("page_regions") or {}).items()}
        return self._card_store

    def _items_to_dicts(self) -> tuple[list[dict], list[dict]]:
        regions, protect = [], []
        for it in self.items():
            d = copy.deepcopy(it.data)
            d["rect"] = self.ratio_of(it)
            if it.kind == "protect":
                d.pop("kind", None)
                for k in ("style", "erase", "extract"):
                    d.pop(k, None)
                protect.append(d)
            else:
                d["kind"] = it.kind
                regions.append(d)
        return regions, protect

    def _stash_card(self):
        key = getattr(self, "_current_key", None)
        if key is None:
            return
        regions, protect = self._items_to_dicts()
        if regions or protect:
            self._cards()[key] = {"regions": regions, "protect_rects": protect}
        else:
            self._cards().pop(key, None)

    def _clear_items(self):
        for it in self.items():
            self.scene.removeItem(it)
        self._order = []

    def _load_card(self, key: str):
        ps = self._cards().get(key) or {}
        self._order = []
        for r in ps.get("regions", []):
            self._add_item(copy.deepcopy(r), protect=False)
        for r in ps.get("protect_rects", []):
            self._add_item(copy.deepcopy(r), protect=True)
        self._rebuild_list()
        self._update_card_info()

    def _sync_guide(self):
        """아래 안내 띠: 지금 무엇을 하면 되는지 (위에서 먼저 맞는 것)."""
        if not hasattr(self, "guide"):
            return
        has = any(i.kind != "protect" for i in self.items())
        left = 0
        if self.is_per_card():
            done = sum(1 for v in self._cards().values() if v.get("regions"))
            keys = set(self.all_card_keys())
            left = max(0, len(keys) - sum(1 for k, v in self._cards().items() if k in keys and v.get("regions")))
        if not has:
            set_hint(self.guide, "위의 ‘+ 글자 영역’(T)을 누르고, 번역할 글자 위를 끌어 영역을 그리세요.", "todo")
        elif self.dirty:
            set_hint(self.guide, "저장하지 않은 변경이 있습니다. Ctrl+S로 저장하세요.", "warn")
        elif left:
            set_hint(self.guide, f"영역이 없는 카드가 {left}장 남았습니다. ▶로 넘기며 지정하거나 ‘나머지 카드에 복사’를 쓰세요.", "todo")
        else:
            set_hint(self.guide, "저장했습니다. 다 지정했으면 창을 닫고 프로젝트 화면에서 ‘① 글자 추출’을 누르세요.", "done")

    def _update_card_info(self):
        try:
            self._update_card_info_impl()
        finally:
            self._sync_guide()

    def _update_card_info_impl(self):
        if hasattr(self, "cards_bar"):
            self.cards_bar.setVisible(self.is_per_card())
        if hasattr(self, "override_btn"):
            self.override_btn.setVisible(not self.is_per_card())
            self._syncing_override = True
            self.override_btn.setChecked(bool(getattr(self, "_ov_key", None)) and not self.is_per_card())
            self._syncing_override = False
        if hasattr(self, "align_btn"):
            self.align_btn.setVisible(not self.is_per_card())
            self.align_reset_btn.setVisible(not self.is_per_card())
        if not self.is_per_card():
            n_ov = len(self.overrides())
            al = self._align_label()
            self.card_info.setText(" · ".join(x for x in (
                "이 카드: 따로 영역" if getattr(self, "_ov_key", None) else "",
                f"따로 영역 카드 {n_ov}장" if n_ov else "", al) if x))
            e = None if getattr(self, "_ov_key", None) else self._align_pages().get(self.page_spin.value())
            self.align_reset_btn.setEnabled(bool(e) and e.get("status") != "ref")
            for w in (self.copy_prev, self.copy_rest, self.sync_names):
                w.setEnabled(False)
            return
        for w in (self.copy_rest, self.sync_names):
            w.setEnabled(True)
        n = len([i for i in self.items() if i.kind != "protect"])
        keys = set(self.all_card_keys())  # 뺄 쪽은 세지 않는다
        done = sum(1 for k, v in self._cards().items() if k in keys and v.get("regions"))
        total = len(keys)
        self.card_info.setText(f"이 카드: {'영역 ' + str(n) + '개' if n else '영역 없음'} · 지정한 카드 {done}/{total}장")
        self.copy_prev.setEnabled(True)

    def copy_previous_card(self):
        """바로 앞(없으면 가장 가까운 앞) 카드의 영역을 이 카드로 복사한다 (비슷한 카드를 빨리 정하기)."""
        if not self.is_per_card():
            return
        self._stash_card()
        name, p = self.current_file().name, self.page_spin.value()
        src = next((self._cards()[f"{name}/{q}"] for q in range(p - 1, 0, -1)
                    if self._cards().get(f"{name}/{q}", {}).get("regions")), None)
        if src is None:
            QMessageBox.information(self, "이전 카드", "앞쪽에 영역을 정한 카드가 없습니다")
            return
        self._clear_items()
        self._cards()[self.card_key()] = copy.deepcopy(src)
        self._load_card(self.card_key())
        self._mark_dirty()

    def all_card_keys(self) -> list[str]:
        """이 그룹의 모든 카드 키 (파일 순서, 쪽 순서)."""
        return [f"{f.name}/{q}" for f in self.file_dirs for q in self._shown(f)]  # 뺄 쪽은 빼고

    def copy_to_rest(self):
        """지금 카드의 영역을 나머지 카드에 복사한다 (영역 없는 카드에만 / 모두 덮어쓰기)."""
        if not self.is_per_card():
            return
        self._stash_card()
        src = self._cards().get(self.card_key())
        if not src or not src.get("regions"):
            QMessageBox.information(self, "나머지 카드에 복사", "이 카드에 영역이 없습니다. 먼저 영역을 그리세요.")
            return
        others = [k for k in self.all_card_keys() if k != self.card_key()]
        empty = [k for k in others if not (self._cards().get(k) or {}).get("regions")]
        r = ask_choice(self, "나머지 카드에 복사",
                       f"이 카드의 영역 {len(src['regions'])}개를 다른 카드에 복사합니다.",
                       [(f"영역 없는 카드에만 ({len(empty)}장)", "accept"),
                        (f"모든 카드 덮어쓰기 ({len(others)}장)", "destructive"), ("취소", "reject")], default=0,
                       informative="저장하기 전까지는 파일에 쓰지 않습니다. 카드마다 위치가 다르면 복사한 뒤 쪽을 넘기며 고치세요.")
        if r not in (0, 1):
            return
        targets = empty if r == 0 else others
        for k in targets:
            self._cards()[k] = copy.deepcopy(src)
        self._mark_dirty()
        self.status.setText(f"영역을 {len(targets)}장에 복사했습니다")

    def reset_regions(self):
        """영역 비우기: 이 카드만 / 모든 카드 (카드마다 영역이 아니면 지금 영역 전부)."""
        if not self.is_per_card():
            if not self.items():
                return
            if QMessageBox.question(self, "초기화", "이 그룹의 영역을 모두 지울까요? (저장하기 전까지는 파일에 쓰지 않습니다)") \
                    != QMessageBox.StandardButton.Yes:
                return
            self._clear_items()
            self._rebuild_list()
            self._to_form(None)
            self._update_form_enabled()
            self._mark_dirty()
            self.status.setText("영역을 모두 지웠습니다 (저장해야 반영됩니다)")
            return
        self._stash_card()
        n_all = sum(1 for v in self._cards().values() if v.get("regions") or v.get("protect_rects"))
        r = ask_choice(self, "초기화", "어느 카드의 영역을 비울까요?",
                       [("이 카드만", "accept"), (f"모든 카드 ({n_all}장)", "destructive"), ("취소", "reject")],
                       default=0, informative="저장하기 전까지는 파일에 쓰지 않습니다 (닫을 때 ‘저장 안 함’이면 그대로).")
        if r not in (0, 1):
            return
        if r == 1:
            self._card_store = {}
        else:
            self._cards().pop(self.card_key(), None)
        self._clear_items()
        self._load_card(self.card_key())
        self._to_form(None)
        self._update_form_enabled()
        self._mark_dirty()
        self.status.setText("이 카드의 영역을 비웠습니다" if r == 0 else "모든 카드의 영역을 비웠습니다")

    def jump_page(self, where: int):
        shown = self._shown()
        self.page_spin.setValue(shown[0] if where == 0 else shown[-1])
        self._sync_nav()

    def _key_action(self, act: str):
        if act in ("add_text", "add_tag", "add_protect", "add_ocr_exclude"):
            kind = {"add_text": "text", "add_tag": "tag_pills", "add_protect": "protect",
                    "add_ocr_exclude": "ocr_exclude"}[act]
            self._set_add_kind(None if self.scene.add_kind == kind else kind)
        elif act == "fit":
            self.view.fit_again()
        elif act == "toggle_preview":
            self.preview_btn.toggle()
        elif act == "rotate":
            it = self.selected()
            if it is not None and it.kind != "protect":
                self.f_rotate.setChecked(not self.f_rotate.isChecked())  # 폼을 거쳐 저장·같은 이름 적용
        elif act in ("next_region", "prev_region") and self._order:
            cur = self.selected()
            i = self._order.index(cur) if cur in self._order else -1
            j = (i + (1 if act == "next_region" else -1)) % len(self._order)
            self.scene.clearSelection()
            self._order[j].setSelected(True)
            self.view.setFocus()

    def _unique_name(self, base: str) -> str:
        names = {i.name for i in self.items()}
        if base not in names:
            return base
        stem = base.rstrip("0123456789") or base
        n = 2
        while f"{stem}{n}" in names:
            n += 1
        return f"{stem}{n}"

    def copy_regions(self):
        """고른 영역(없으면 이 카드의 모든 영역)을 복사해 둔다. 다른 카드에서 Ctrl+V."""
        if self.focusWidget() in (self.f_name, self.f_color, self.f_notes):
            return
        sel = [i for i in self.scene.selectedItems() if isinstance(i, RegionItem)] or self.items()
        self._clip = []
        for it in sel:
            d = copy.deepcopy(it.data)
            d["rect"] = self.ratio_of(it)
            self._clip.append({"data": d, "protect": it.kind == "protect"})
        self.status.setText(f"영역 {len(self._clip)}개를 복사했습니다 (Ctrl+V로 붙여넣기)")

    def paste_regions(self):
        if self.focusWidget() in (self.f_name, self.f_color, self.f_notes) or not self._clip:
            return
        self.scene.clearSelection()
        last = None
        for c in self._clip:
            d = copy.deepcopy(c["data"])
            d["name"] = self._unique_name(d.get("name") or "본문")
            last = self._add_item(d, protect=c["protect"])
            last.setSelected(True)
        self._rebuild_list(last)
        self._mark_dirty()
        self.status.setText(f"영역 {len(self._clip)}개를 붙여 넣었습니다")

    def duplicate_selected(self):
        it = self.selected()
        if it is None:
            return
        d = copy.deepcopy(it.data)
        r = self.ratio_of(it)
        r["x"], r["y"] = min(1 - r["w"], r["x"] + 0.02), min(1 - r["h"], r["y"] + 0.02)
        d["rect"] = r
        d["name"] = self._unique_name(it.name or "본문")
        new = self._add_item(d, protect=it.kind == "protect")
        self.scene.clearSelection()
        new.setSelected(True)
        self._rebuild_list(new)
        self._mark_dirty()

    def _sync_same_name(self, old_name: str, it: RegionItem) -> int:
        """카드마다 영역: 이 영역의 설정(위치 빼고)을 다른 카드의 같은 이름 영역에 적용한다. 바꾼 카드 수."""
        if not (self.is_per_card() and self.sync_names.isChecked()) or not old_name:
            return 0
        n = 0
        cur = self.card_key()
        new_name = it.data.get("name") or old_name
        for key, card in self._cards().items():
            if key == cur:
                continue
            names = {r.get("name") for r in card.get("regions", [])} | {r.get("name") for r in card.get("protect_rects", [])}
            for lst in (card.get("regions", []), card.get("protect_rects", [])):
                for r in lst:
                    if r.get("name") != old_name:
                        continue
                    if new_name != old_name and new_name in names:
                        continue  # 그 카드에 이미 같은 새 이름이 있으면 이름은 그대로
                    for k in ("style", "erase", "notes", "extract"):
                        if k in it.data:
                            r[k] = copy.deepcopy(it.data[k])
                        else:
                            r.pop(k, None)
                    if it.kind != "protect" and lst is card.get("regions"):
                        r["kind"] = it.kind
                    r["name"] = new_name
                    n += 1
        return n

    def _toggle_per_card(self, on: bool):
        if on:
            msg = ("카드마다 영역을 정합니다. 지금 영역은 모든 카드에 그대로 남고,\n"
                   "카드마다 다른 곳만 쪽을 넘기며 고치면 됩니다.")
        else:
            msg = ("모든 카드가 같은 영역을 쓰도록 바꿉니다. 지금 카드의 영역이 공통 영역이 되고,\n"
                   "다른 카드에 정한 영역은 저장할 때 지워집니다.")
        if QMessageBox.question(self, "영역 방식 바꾸기", msg) != QMessageBox.StandardButton.Yes:
            self.per_card.blockSignals(True)
            self.per_card.setChecked(not on)
            self.per_card.blockSignals(False)
            return
        if on:
            items = self._items_to_dicts()  # 공통 영역이면 기준 카드 좌표
            common = self._common if self._ov_key else items
            self._view_t = None  # 카드마다 영역은 자동 정렬 없이 보이는 자리 그대로
            for it in self.items():
                it._placed = None
            shown = self._items_to_dicts()
            overrides = {k: v for k, v in self._cards().items() if v.get("regions") or v.get("protect_rects")}
            if self._ov_key:
                overrides[self._ov_key] = {"regions": items[0], "protect_rects": items[1]}
            self._card_store = {}
            self._current_key = self.card_key()
            self._ov_key = None
            # 공통 영역을 쓰던 모든 카드에 그대로 남기고(자동 정렬한 카드는 맞춘 자리로), 따로 영역을 둔 카드는 그 영역 그대로
            for k in self.all_card_keys():
                f, _, pg = k.rpartition("/")
                t = self._align_t(next((fd for fd in self.file_dirs if fd.name == f), None), int(pg))
                self._card_store[k] = copy.deepcopy(overrides.get(k) or {
                    "regions": [self._aligned_dict(r, t) for r in common[0]],
                    "protect_rects": [self._aligned_dict(r, t) for r in common[1]]})
            self._card_store[self._current_key] = {"regions": shown[0], "protect_rects": shown[1]}
        else:
            self._card_store = {}
            self._current_key = None
            self._ov_key = None
            self._view_t = self._align_t()  # 지금 카드의 영역이 공통 영역: 이 카드의 정렬을 거꾸로 해 기준 좌표로
            self._common = self._items_to_dicts()
        self._update_card_info()
        self._mark_dirty()

    def _load_regions(self):
        self._order = []
        self._align_cache = {}
        if self.template.get("layout") == "per_card":
            self._view_t = None
            self._card_store = {k: copy.deepcopy(v) for k, v in (self.template.get("page_regions") or {}).items()}
            self._current_key = self.card_key()
            self._load_card(self._current_key)
            return
        self._current_key = None
        self._ov_key = None
        self._common = (copy.deepcopy(self.template.get("regions", [])), copy.deepcopy(self.template.get("protect_rects", [])))
        ov = self._cards().get(self.card_key())
        if ov is not None:  # 처음 보이는 카드에 따로 영역이 있으면 그것
            self._view_t = None
            self._load_dicts(ov.get("regions", []), ov.get("protect_rects", []))
            self._ov_key = self.card_key()
        else:
            self._view_t = self._align_t()
            self._load_dicts(*self._common)
        self._snap_shared()
        self._update_card_info()

    def _add_item(self, data: dict, protect: bool) -> RegionItem:
        ratio = data.get("rect") or {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1}
        it = RegionItem(self.rect_from_ratio(ratio), data, protect)
        self.scene.addItem(it)
        it._placed = (QRectF(it.rect()), dict(ratio), self._view_t)
        it.refresh()
        self._order.append(it)
        return it

    def _load_dicts(self, regions: list[dict], protect: list[dict]):
        for r in regions:
            self._add_item(copy.deepcopy(r), protect=False)
        for r in protect:
            self._add_item(copy.deepcopy(r), protect=True)
        self._rebuild_list()

    # ------------------------------------------------------------ 이 카드만 따로 영역 (공통 영역 그룹)
    def _snap_shared(self):
        """공통 영역 카드를 보여 줄 때(불러오기·쪽 넘기기·저장) 공통 영역의 모양(기준 카드 좌표)을 적어 둔다.
        ‘이 카드만 따로 영역’을 켜면 그 뒤에 이 카드에서 고친 것은 공통 영역이 아니라 이 카드 것이 되어야 한다."""
        if self.is_per_card() or self._ov_key:
            self._shared_snap = None
            return
        self._shared_snap = self._items_to_dicts()

    def showEvent(self, ev):
        super().showEvent(ev)
        self.view.fit()
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, self.view.fit)  # 창 배치가 끝난 뒤 크기로 다시 맞춘다

    # ------------------------------------------------------------ 좌표
    def ratio_of(self, it: RegionItem) -> dict:
        """상자의 비율 좌표. 자동 정렬한 카드를 보는 중이면 기준 카드 좌표로 되돌린 값 (공통 영역은 기준 좌표로 저장)."""
        placed = getattr(it, "_placed", None)
        if placed is not None and placed[0] == it.rect() and placed[2] == self._view_t:
            return dict(placed[1])  # 놓은 뒤 움직이지 않았으면 놓을 때 값 그대로 (쪽 밖 자르기·반올림이 쌓이지 않게)
        pr = self.scene.page_rect
        r = it.rect()
        d = {"x": r.left() / pr.width(), "y": r.top() / pr.height(),
             "w": r.width() / pr.width(), "h": r.height() / pr.height()}
        if self._view_t:
            d = service.align_rect(d, self._view_t, inverse=True)
        return {k: round(v, 4) for k, v in d.items()}

    def rect_from_ratio(self, d: dict) -> QRectF:
        """비율 좌표 -> 장면 사각형 (자동 정렬한 카드면 그 카드의 정렬만큼 옮긴 자리)."""
        if self._view_t:
            d = service.align_rect(d, self._view_t)
        pr = self.scene.page_rect
        return QRectF(float(d["x"]) * pr.width(), float(d["y"]) * pr.height(),
                      float(d["w"]) * pr.width(), float(d["h"]) * pr.height())

    def _place(self, it: RegionItem, ratio: dict) -> None:
        """상자를 비율 좌표 자리에 놓고 그 값을 기억한다 (ratio_of가 그대로 돌려준다)."""
        it.set_scene_rect(self.rect_from_ratio(ratio))
        it._placed = (QRectF(it.rect()), dict(ratio), self._view_t)

    # ------------------------------------------------------------ 목록·선택
    def items(self) -> list[RegionItem]:
        return list(getattr(self, "_order", []))

    def _rebuild_list(self, keep: RegionItem | None = None):
        """목록은 템플릿 순서(_order)를 따른다. 새로 그린 영역은 끝에 붙는다."""
        order = self._order
        self.list.blockSignals(True)
        self.list.clear()
        for it in order:
            kind = dict(KIND_LABELS)[it.kind]
            if it.kind == "text" and (it.data.get("style") or {}).get("icons") == "none":
                kind += " (글자만)"
            self.list.addItem(f"{it.name or '(이름 없음)'}  ·  {kind}")
        if keep in order:
            self.list.setCurrentRow(order.index(keep))
        self.list.blockSignals(False)
        self._sync_guide()

    def selected(self) -> RegionItem | None:
        sel = [i for i in self.scene.selectedItems() if isinstance(i, RegionItem)]
        return sel[0] if sel else None

    def _on_list_row(self, row: int):
        if 0 <= row < len(self._order):
            self.scene.clearSelection()
            self._order[row].setSelected(True)
            if not self.view.auto_fit:  # 화면에 맞춰 보는 중이면 카드를 움직이지 않는다
                self.view.ensureVisible(self._order[row], 20, 20)

    def _on_selection(self):
        try:
            it = self.selected()
        except RuntimeError:  # 창을 닫으며 장면이 지워질 때 선택 해제 신호가 온다
            return
        if it in self._order:
            self.list.blockSignals(True)
            self.list.setCurrentRow(self._order.index(it))
            self.list.blockSignals(False)
        self._to_form(it)
        self._update_form_enabled()

    def _update_form_enabled(self):
        sel = self.selected() is not None
        self.form_box.setEnabled(sel)
        self.form_box.setVisible(sel)
        self.form_hint.setVisible(not sel)

    def _on_created(self, it: RegionItem):
        if it not in self._order:
            self._order.append(it)
        if it.kind == "ocr_exclude":  # 읽기 제외 = 보호 영역 + 글자 읽기에서도 빼기
            it.kind = "protect"
            it.data["ocr_exclude"] = True
            it.data["kind"] = "protect"
            it.refresh()
        base = {"text": "본문", "tag_pills": "태그", "protect": "읽기 제외" if it.data.get("ocr_exclude") else "보호"}[it.kind]
        names = {i.name for i in self.items()}
        n, name = 1, base
        while name in names:
            n += 1
            name = f"{base}{n}"
        it.data["name"] = name
        for b in self.add_buttons.values():
            b.setChecked(False)
        self.scene.add_kind = None
        self._rebuild_list(it)
        self._to_form(it)
        self._mark_dirty()
        self._update_card_info()
        self.f_name.setFocus()
        self.f_name.selectAll()

    def _on_geometry(self, it: RegionItem):
        if it is self.selected():
            self._to_form(it)
        self._mark_dirty()

    def _set_add_kind(self, kind: str | None):
        for k, b in self.add_buttons.items():
            b.setChecked(k == kind)
        self.scene.add_kind = kind
        self.view.viewport().setCursor(Qt.CursorShape.CrossCursor if kind else Qt.CursorShape.ArrowCursor)

    def delete_selected(self):
        it = self.selected()
        if it is None:
            return
        self.scene.removeItem(it)
        self._order.remove(it)
        self._rebuild_list()
        self._mark_dirty()

    def move_selected(self, d: int):
        it = self.selected()
        if it is None:
            return
        i = self._order.index(it)
        j = max(0, min(len(self._order) - 1, i + d))
        self._order[i], self._order[j] = self._order[j], self._order[i]
        self._rebuild_list(it)
        self._mark_dirty()

    # ------------------------------------------------------------ 저장
    def to_template(self) -> dict:
        data = copy.deepcopy(self.template)
        if self.is_per_card():
            self._stash_card()
            data["layout"] = "per_card"
            data["regions"], data["protect_rects"] = [], []
            data["page_regions"] = {k: v for k, v in self._cards().items() if v.get("regions") or v.get("protect_rects")}
            data.setdefault("reference_page", self.page_spin.value())
            data.setdefault("source_language", "ja")
            return data
        data.pop("layout", None)
        if self._ov_key:  # 따로 영역 카드를 보는 중: 화면의 영역은 그 카드 것, 공통 영역은 넣어 둔 것
            r, p = self._items_to_dicts()
            self._cards()[self._ov_key] = {"regions": r, "protect_rects": p}
            regions, protect = self._common
        else:
            regions, protect = self._items_to_dicts()
        data["regions"] = regions
        data["protect_rects"] = protect
        ov = {k: self._override_with_common_style(v, regions) for k, v in self._cards().items()}
        if ov:  # 이 카드만 따로 영역 (공통 영역 그룹). 빈 것도 남긴다: 그 카드는 처리하지 않는다
            data["page_regions"] = ov
        else:
            data.pop("page_regions", None)
        data.setdefault("reference_page", self.page_spin.value())
        data.setdefault("source_language", "ja")
        return data

    def save(self) -> bool:
        problems = service.save_region_template(self.group_dir, self.to_template())
        if problems:
            show_problem(self, "영역을 저장하지 못했습니다", "", "표시된 영역을 고친 뒤 다시 저장하세요.", items=problems)
            return False
        self.template = service.load_region_template(self.group_dir)
        if self.is_per_card():
            self._card_store = {k: copy.deepcopy(v) for k, v in (self.template.get("page_regions") or {}).items()}
        self._snap_shared()  # 저장한 모양이 새 공통 영역의 기준
        self.dirty = False
        self._update_title()
        self._update_card_info()
        self.status.setText("영역을 저장했습니다")
        self.saved.emit()
        return True

    def _mark_dirty(self):
        self.dirty = True
        self._update_title()
        if hasattr(self, "card_info"):
            self._update_card_info()
        if hasattr(self, "preview"):
            self.preview.schedule()

    def _update_title(self):
        self.setWindowTitle(f"영역 지정 — {self.group_dir.name}{' *' if self.dirty else ''}")
        if hasattr(self, "save_btn"):
            self.save_btn.setStyleSheet(PRIMARY_QSS if self.dirty else "")

    def closeEvent(self, ev):
        if not self._confirm_leave():
            ev.ignore()
            return
        super().closeEvent(ev)
