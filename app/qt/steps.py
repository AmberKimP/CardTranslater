"""2차 화면: 추출 → 원문 확인 → 아이콘 이름 → ‘추출 완료’(용어집 검증) → 용어집 가져오기 → 번역·검수 엑셀.

긴 작업은 worker.Job으로 작업 스레드에서 돌린다. 엔진이 사람에게 물을 일(needs_confirmation 등)은
결과 상태로 돌려주므로, 여기서 확인 창을 띄우고 ‘계속’이면 다시 부른다 (PRD 6장).
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import re
from difflib import SequenceMatcher
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontDatabase, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from app import service
from app.qt import worker
from app.qt.dialogs import (
    PRIMARY_QSS, ask_choice, card, file_label, fit_to_screen, grey_note, hint_bar, page_title, set_hint, set_ok_text,
    show_error, show_failure, show_problem, subtitle,
)

MODE_LABELS = [("double-check", "꼼꼼히 (추천) — 보통 읽기 뒤, 애매한 칸만 정밀 읽기로 한 번 더"),
               ("opus", "처음부터 정밀 읽기 (느리고 사용량이 많음)")]
# 화면에 모델 이름 대신 쓰는 말 (service가 주는 Sonnet·Opus, 엔진 메모의 모델 ID)
READER_NAMES = {"sonnet": "보통 읽기", "opus": "정밀 읽기", "haiku": "빠른 읽기"}
# service의 칸 상태 이름을 화면용으로 (색·걸러 보기는 원래 이름으로 한다)
LABEL_TEXT = {"결론 못 냄": "다시 봐야 함"}
JA_FONTS = ["Yu Gothic UI", "Meiryo"]  # 원문(일본어) 칸 글꼴: 차례로, 없으면 기본 글꼴
ENGINE_LABELS = [("claude-code", "Claude 구독 (Claude Code)"), ("api", "API 키 (사용한 만큼 요금)")]  # 로그인 창과 같은 말
LABEL_COLORS = {"결론 못 냄": "#F4B183", "두 번 확인": "#D9EAD3", "읽지 않음": "#EEEEEE", "사람이 고침": "#CFE2F3"}


STATE_LABEL = {"done": "끝남", "skipped": "건너뜀", "blocked": "할 수 없음", "failed": "실패",
               "paused": "사용 한도로 멈춤", "needs_confirmation": "확인 필요", "cancelled": "취소함",
               "icons_review": "아이콘 정의 필요", "waiting_batch": "처리 기다리는 중"}


def state_label(s: str) -> str:
    """엔진의 상태 낱말(done·blocked…)을 화면용 한국어로."""
    return STATE_LABEL.get(s, s)


def label_text(label: str) -> str:
    """칸 상태 이름을 화면용으로. 모르는 이름은 모델 이름(Sonnet 등)만 쉬운 말로 바꾼다."""
    return LABEL_TEXT.get(label) or plain_note(label)


def reader_name(model: str) -> str:
    """Sonnet·claude-opus-5-5 같은 모델 이름을 ‘보통 읽기’·‘정밀 읽기’로."""
    low = (model or "").lower()
    return next((v for k, v in READER_NAMES.items() if k in low), model)


def plain_note(note: str) -> str:
    """엔진 메모 속 모델 ID·‘두 모델’ 같은 말을 쉬운 말로."""
    import re

    note = re.sub(r"claude-[a-z0-9.\-]+", lambda m: reader_name(m.group(0)), note or "")
    note = re.sub(r"\b(Sonnet|Opus|Haiku)\b", lambda m: reader_name(m.group(0)), note)
    return note.replace("두 모델이 다르게 읽음", "두 번 읽은 결과가 다름")


def source_font(base: QFont, bigger: int = 2) -> QFont:
    """일본어 원문을 알아보기 쉬운 글꼴 (Yu Gothic UI → Meiryo → 기본)."""
    f = QFont(base)
    have = set(QFontDatabase.families())
    f.setFamilies([x for x in JA_FONTS if x in have] + [base.family()])
    if f.pointSize() > 0:
        f.setPointSize(f.pointSize() + bigger)
    return f


# ---- 원문 확인: 읽은 결과 비교·아이콘 이름
ICON_TOKEN_RE = re.compile(r"\[\[([^\[\]]*)\]\]")
UNKNOWN_ICON = "[[?]]"
_UNIT_RE = re.compile(r"\[\[[^\[\]]*\]\]|.", re.S)  # 아이콘 토큰은 한 덩어리, 나머지는 한 글자씩
DIFF_STYLE = "color:#C00000; font-weight:bold; background-color:#FFF2CC"
PRECISE = READER_NAMES["opus"]


def as_text(src) -> str:
    """원문 값(글자 칸은 문자열, 태그 칸은 목록)을 편집 칸 글로 (목록은 한 줄에 하나)."""
    return "\n".join(src) if isinstance(src, list) else (src or "")


def _units(text: str) -> list[str]:
    return _UNIT_RE.findall(text or "")


def fill_icon_names(reading: str, source: str, known=None) -> str:
    """읽은 결과 속 이름 없는 아이콘 [[?]]를, 지금 원문의 같은 자리에 있는 [[이름]]으로 바꾼다.

    모델이 읽은 결과에는 아이콘 이름을 정하기 전의 [[?]]가 남아 있고, 이름을 정하면 원문만 [[이름]]이 된다.
    두 글을 맞춰 보아 같은 자리의 아이콘만 바꾼다. known이 있으면 그 안의 이름(사전에서 정한 것)만."""
    a = _units(reading)
    if UNKNOWN_ICON not in a:
        return reading
    b, out = _units(source), list(a)
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "replace":
            continue
        qs = [i for i in range(i1, i2) if a[i] == UNKNOWN_ICON]
        names = [b[j] for j in range(j1, j2) if ICON_TOKEN_RE.fullmatch(b[j]) and b[j] != UNKNOWN_ICON]
        if qs and len(qs) == len(names):
            for i, n in zip(qs, names):
                if known is None or n[2:-2] in known:
                    out[i] = n
    return "".join(out)


def _marked_html(units: list[str], marks: set[int]) -> str:
    out, on = [], False
    for i, u in enumerate(units):
        hit = i in marks
        if hit != on:
            out.append(f'<span style="{DIFF_STYLE}">' if hit else "</span>")
            on = hit
        if u == "\n":
            out.append("↵<br>" if hit else "<br>")  # 줄바꿈 자리가 다르면 보이게
        else:
            out.append("&nbsp;" if hit and u == " " else _html.escape(u, quote=False))  # 다른 빈칸도 배경색으로 보이게
    if on:
        out.append("</span>")
    return "".join(out)


def diff_html(texts: list[str], ref: int) -> list[str]:
    """읽은 결과마다 HTML. texts[ref](정밀 읽기)와 다른 곳을 빨간 굵은 글자로. 줄바꿈은 <br>."""
    units = [_units(t) for t in texts]
    marks: list[set[int]] = [set() for _ in texts]
    for k, u in enumerate(units):
        if k == ref:
            continue
        for op, i1, i2, j1, j2 in SequenceMatcher(None, u, units[ref], autojunk=False).get_opcodes():
            if op != "equal":
                marks[k].update(range(i1, i2))
                marks[ref].update(range(j1, j2))
    return [_marked_html(u, m) for u, m in zip(units, marks)]


def precise_index(readings: list[dict]) -> int | None:
    """정밀 읽기 결과의 자리 (없으면 마지막 = 나중에 다시 읽은 결과, 읽은 결과가 없으면 None)."""
    if not readings:
        return None
    return next((i for i, rd in enumerate(readings) if reader_name(rd["model"]) == PRECISE), len(readings) - 1)


class FitImage(QLabel):
    """그림을 비율 그대로 주어진 칸 안에 다 보이게 줄이거나(크게는 2배까지) 키운다. 창 크기가 바뀌면 다시 맞춘다."""

    MAX_UP = 2.0

    def __init__(self, min_height: int = 160):
        super().__init__()
        self._pm = QPixmap()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(120, min_height)
        # 그림 크기가 창 배치를 밀어내지 않게 (칸 크기는 배치가 정하고, 그림이 거기에 맞춘다)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setStyleSheet("background: #FAFAFA; border: 1px solid #DDD;")

    def set_image(self, pm: QPixmap) -> None:
        self._pm = QPixmap(pm)
        self._refit()

    def source_size(self) -> QSize:
        return self._pm.size()

    def clear(self):
        self._pm = QPixmap()
        super().clear()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refit()

    def _refit(self):
        if self._pm.isNull():
            return
        room = self.contentsRect().size()
        w, h = self._pm.width(), self._pm.height()
        if room.width() < 2 or room.height() < 2 or not w or not h:
            return
        scale = min(room.width() / w, room.height() / h, self.MAX_UP)
        dpr = self.devicePixelRatioF() or 1.0
        pm = self._pm.scaled(max(1, int(w * scale * dpr)), max(1, int(h * scale * dpr)),
                             Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        pm.setDevicePixelRatio(dpr)
        self.setPixmap(pm)


def _ask_list(parent, title: str, intro: str, items: list[str], question: str) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(intro)
    box.setInformativeText(question)
    box.setDetailedText("\n".join(items))
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def _fail(parent, msg: str, title: str = "작업을 끝내지 못했습니다"):
    """작업 스레드에서 예외가 난 경우: 왜 → 무엇을 하면 되는지, 원문은 ‘자세히 보기’에."""
    show_failure(parent, title, msg)


def _fail_as(parent, title: str):
    return lambda m: _fail(parent, m, title)


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


# ================================================================ 원문 확인
class SourceReviewDialog(QDialog):
    """파일의 추출 결과를 칸마다 보고, 잘못 읽은 원문을 이미지와 비교해 고친다.

    고친 글이 있는 채로 다른 칸·파일로 옮기거나 닫으면 저장할지 묻는다."""

    COLS = ["쪽", "영역", "원문 확인", "원문"]
    UNREAD = "읽지 않음"

    def __init__(self, files: list[dict], parent=None, only_check: bool = True):
        super().__init__(parent)
        self.setWindowTitle("원문 확인")
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
        self.count.setStyleSheet("color: #334E68; background: white; border: 1px solid #E6E9EE;"
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
        hint = QLabel("Ctrl+S 저장 · Ctrl+Enter 저장하고 다음 칸 · Alt+↑/↓ 칸 이동")
        hint.setStyleSheet("color: #7B8794")
        self.next_btn.setStyleSheet(PRIMARY_QSS)  # 주 동작 (엔터 기본 버튼으로는 만들지 않는다)
        btns = QHBoxLayout()
        btns.addWidget(self.next_btn)
        btns.addWidget(self.save_btn)
        btns.addStretch(1)
        btns.addWidget(hint)
        # 오른쪽: 위 = 그림(남는 높이를 모두 쓰고, 그림은 늘 칸 안에 다 보이게 맞춘다),
        # 그 아래 = 사유 → 읽은 결과 표 → 고르기 → 편집 칸 → 저장 (글이 잘리지 않게 제 높이대로)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.image, 1)
        rl.addWidget(self.info)
        rl.addWidget(self.readings_box)
        rl.addLayout(self.pick_row)
        rl.addWidget(self.edit)
        rl.addLayout(btns)
        split = QSplitter()
        split.addWidget(self.table)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([500, 750])
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
        for b in (self.save_btn, self.next_btn):
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
        self._file_idx = self.file.currentIndex()
        self.file.currentIndexChanged.connect(self._on_file_changed)
        self.only.toggled.connect(self._on_only_toggled)
        self.reload()

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
                vals = [str(c["page"]), c["region"], label_text(c["label"]),
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
            self._fill_readings([], None)
            self._fill_picks([])
            self.edit.clear()
            self._shown = self._shown_cell = None
            self._orig_text = ""
        self.save_btn.setEnabled(bool(self.cells))
        self.next_btn.setEnabled(bool(self.cells))

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
        head = _html.escape(f"{c['page']}쪽 · {c['region']} · {label_text(c['label'])}")
        reason, memo = self.reason_text(c, differ)
        reason = _html.escape(reason)
        if differ:
            reason = reason.replace("다릅니다.", f'다릅니다 (다른 곳은 <span style="{DIFF_STYLE}">빨간 굵은 글자</span>).', 1)
        if differ and pi is not None and self.needs_attention(c):
            reason += " 편집 칸에는 정밀 읽기 결과를 넣어 두었습니다."
        text = f"<b>{head}</b><br>사유: {reason}"
        if memo:
            text += f'<br><span style="color:#555">읽은 모델의 메모: {_html.escape(memo)}</span>'
        set_hint(self.info, text, kind)
        self._fill_readings(texts, pi)
        self._fill_picks(texts)
        self.edit.setPlainText(default)
        self._shown, self._shown_cell, self._shown_file = (c["page"], c["region"]), c, self.file.currentData()
        self._orig_text = self.edit.toPlainText()

    @staticmethod
    def reading_texts(c: dict, known=None) -> list[tuple[str, str]]:
        """[(‘보통 읽기’ 등, 읽은 결과 글)]. 이름을 정한 아이콘은 [[?]] 대신 [[이름]]으로."""
        src = as_text(c["source"])
        return [(reader_name(rd["model"]), fill_icon_names(as_text(rd["source"]), src, known)) for rd in c["readings"]]

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
            edge = "border: 1px solid #D9DEE5;" + ("" if i == last else " border-bottom: none;")
            h = QLabel(name)
            h.setStyleSheet(f"background: #F4F6F9; color: #334E68; font-weight: bold; padding: 5px 10px; {edge} border-right: none;")
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
        """저장하고 다음 칸으로. ‘확인할 칸만’일 때는 저장한 칸이 목록에서 빠지므로 같은 줄 번호가 다음 칸이다."""
        c = self.current()
        if c is None:
            return
        r = self.table.currentRow()
        key = (c["page"], c["region"])
        self._shown, self._shown_cell, self._shown_file = key, c, self.file.currentData()
        self._save_shown()
        self.reload(keep=key)  # 새 목록을 만든 뒤 다음 칸을 찾는다
        idx = next((i for i, x in enumerate(self.cells) if (x["page"], x["region"]) == key), None)
        nxt = r if idx is None else idx + 1
        if 0 <= nxt < len(self.cells) and nxt != self.table.currentRow():
            self.table.setCurrentCell(nxt, 0)

    def move_row(self, d: int):
        r = self.table.currentRow() + d
        if 0 <= r < len(self.cells):
            self.table.setCurrentCell(r, 0)


# ================================================================ 아이콘 사전 (새로 정할 것이 없을 때)
class IconDictionaryDialog(QDialog):
    """게임 아이콘 사전을 보여 주고 한국어 이름·뜻·표시 크기를 고친다.
    ‘다시 정하기’를 고르면 그 아이콘을 원문에서 [[?]]로 되돌린다 (다른 아이콘이 같은 이름으로 잘못 묶였을 때)."""

    COLS = ["그림", "이름", "한국어 이름", "뜻(원문)", "표시 크기", "쓰인 곳", "다시 정하기"]

    def __init__(self, rows: list[dict], intro: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("아이콘 사전")
        fit_to_screen(self, 900, 560)
        self._rows = rows
        lab = subtitle(intro)
        reset_note = grey_note("‘다시 정하기’를 체크하면 그 아이콘을 원문에서 되돌리고, 저장한 뒤 다시 찾아 이름을 정합니다.")
        self.table = QTableWidget(len(rows), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setStyleSheet("QDoubleSpinBox { margin: 7px 6px; padding-top: 1px; padding-bottom: 1px; }"
                                 " QCheckBox { margin-left: 12px; }")
        self.scales: list[QDoubleSpinBox] = []
        self.resets: list[QCheckBox] = []
        for i, r in enumerate(rows):
            img = QTableWidgetItem()
            if r.get("image"):
                pm = QPixmap(r["image"])
                if not pm.isNull():
                    img.setData(Qt.ItemDataRole.DecorationRole, pm.scaledToHeight(40, Qt.TransformationMode.SmoothTransformation))
            img.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(i, 0, img)
            name = QTableWidgetItem(r["name"])
            name.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(i, 1, name)
            self.table.setItem(i, 2, QTableWidgetItem(r.get("ko", "")))
            self.table.setItem(i, 3, QTableWidgetItem(r.get("meaning", "")))
            sp = QDoubleSpinBox()
            sp.setRange(0.5, 4.0)
            sp.setSingleStep(0.1)
            sp.setValue(float(r.get("scale") or 1.1))
            sp.setToolTip("한글 글자 높이 대비 아이콘 높이")
            self.table.setCellWidget(i, 4, sp)
            self.scales.append(sp)
            uses = QTableWidgetItem(f"{r.get('uses', 0)}곳")
            uses.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(i, 5, uses)
            cb = QCheckBox()
            cb.setToolTip("원문의 이 아이콘을 [[?]]로 되돌리고 사전에서 뺍니다. 다시 찾아 새로 묶고 이름을 정합니다")
            self.table.setCellWidget(i, 6, cb)
            self.resets.append(cb)
            self.table.setRowHeight(i, 48)
        for i in range(len(rows)):
            self._paint_ko(i)
        self.table.itemChanged.connect(lambda it: self._paint_ko(it.row()) if it.column() == 2 else None)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "저장")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(8)
        lay.addWidget(page_title("아이콘 사전", 14))
        lay.addWidget(lab)
        lay.addWidget(reset_note)
        lay.addWidget(self.table, 1)
        lay.addWidget(bb)

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


# ================================================================ 아이콘 이름
class IconPlaceView(QWidget):
    """카드 영역 그림 위에 아이콘 자리를 보여 주고, 마우스로 끌어 새 자리를 잡는다.

    초록 = 같은 칸에서 이미 찾은 아이콘, 파랑 = 이 아이콘의 지금 자리, 주황 점선 = 예상 위치(못 찾았을 때),
    빨강 = 사람이 새로 잡은 자리. 좌표는 모두 그림 px."""

    drawn = Signal()

    def __init__(self):
        super().__init__()
        self._pm = QPixmap()
        self.marks: list[tuple[list, str]] = []
        self.box = self.guess = self.new = None
        self._drag = None
        self.setMinimumSize(300, 200)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(False)

    def set_data(self, pm: QPixmap, box, guess, marks: list[tuple[list, str]]):
        self._pm, self.box, self.guess, self.marks, self.new, self._drag = pm, box, guess, marks, None, None
        self.update()

    def _geom(self):
        w, h = self._pm.width(), self._pm.height()
        if not w or not h:
            return 1.0, 0.0, 0.0
        s = min((self.width() - 16) / w, (self.height() - 16) / h, 3.0)
        return s, (self.width() - w * s) / 2, (self.height() - h * s) / 2

    def _to_img(self, p) -> tuple[float, float]:
        s, ox, oy = self._geom()
        return (min(max((p.x() - ox) / s, 0), self._pm.width()), min(max((p.y() - oy) / s, 0), self._pm.height()))

    def _rect(self, b) -> QRectF:
        s, ox, oy = self._geom()
        return QRectF(ox + b[0] * s, oy + b[1] * s, (b[2] - b[0]) * s, (b[3] - b[1]) * s)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#FAFAFA"))
        if self._pm.isNull():
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        s, ox, oy = self._geom()
        p.drawPixmap(QRectF(ox, oy, self._pm.width() * s, self._pm.height() * s), self._pm, QRectF(self._pm.rect()))
        f = p.font()
        f.setBold(True)
        p.setFont(f)

        def frame(b, color, label="", dash=False, width=2):
            r = self._rect(b)
            pen = QPen(QColor(color), width)
            if dash:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)
            if label:
                fm = p.fontMetrics()
                tw, th = fm.horizontalAdvance(label) + 8, fm.height() + 2
                ty = r.top() - th if r.top() - th >= 0 else r.bottom()
                p.fillRect(QRectF(r.left(), ty, tw, th), QColor(color))
                p.setPen(QColor("white"))
                p.drawText(QRectF(r.left(), ty, tw, th), Qt.AlignmentFlag.AlignCenter, label)

        for b, label in self.marks:
            frame(b, "#2E8B57", label)
        if self.guess and not self.new:
            frame(self.guess, "#E67E22", "예상 위치", dash=True)
        if self.box:
            frame(self.box, "#1F6FEB", "지금 자리", width=3 if not self.new else 2)
        if self.new:
            frame(self.new, "#D62828", "새 자리", width=3)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and not self._pm.isNull():
            self._drag = self._to_img(e.position())
            self.new = None
            self.update()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            x, y = self._to_img(e.position())
            x0, y0 = self._drag
            self.new = [min(x0, x), min(y0, y), max(x0, x), max(y0, y)]
            self.update()

    def mouseReleaseEvent(self, e):
        if self._drag is None:
            return
        self._drag = None
        if self.new and (self.new[2] - self.new[0] < 4 or self.new[3] - self.new[1] < 4):
            self.new = None  # 그냥 누른 것
        self.update()
        self.drawn.emit()


class IconPlaceDialog(QDialog):
    """아이콘이 나온 카드 자리를 보며, 못 찾았거나 잘못 잘린 아이콘을 마우스로 감싸 직접 잡는다."""

    def __init__(self, project_dir: Path, g: dict, row_label, on_saved, parent=None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.row_label = row_label  # 묶음 -> ‘3번’ 같은 화면 이름
        self.on_saved = on_saved
        self.setWindowTitle("카드 보기 · 아이콘 자리 잡기")
        fit_to_screen(self, 1100, 720, maximize_below=0)
        self.where = [dict(w) for w in g.get("where", [])]
        self.combo = QComboBox()
        for w in self.where:
            self.combo.addItem(self._item_text(w), w["occ"])
        self.view = IconPlaceView()
        self.view.drawn.connect(self._drawn)
        self.crop = QLabel()
        self.crop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.crop.setMinimumWidth(110)
        self.status = QLabel()
        self.status.setWordWrap(True)
        legend = QLabel('<span style="color:#2E8B57">■</span> 이미 찾은 아이콘 &nbsp; '
                        '<span style="color:#1F6FEB">■</span> 이 아이콘의 지금 자리 &nbsp; '
                        '<span style="color:#E67E22">■</span> 예상 위치(못 찾았을 때, 점선) &nbsp; '
                        '<span style="color:#D62828">■</span> 새로 잡은 자리')
        hint = hint_bar("아이콘을 마우스로 끌어 감싸세요 (테두리까지 조금 넉넉하게). 다시 끌면 새로 잡습니다.\n"
                        "‘이 자리로 저장’을 누르면 그 자리로 그림을 다시 잘라 같은 모양끼리 다시 묶습니다.", "info")
        self.save_btn = QPushButton("이 자리로 저장")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        side = QVBoxLayout()
        side.addWidget(QLabel("잘라 낸 그림:"))
        side.addWidget(self.crop)
        side.addStretch(1)
        mid = QHBoxLayout()
        mid.addWidget(self.view, 1)
        mid.addLayout(side)
        btns = QHBoxLayout()
        btns.addWidget(self.status, 1)
        btns.addWidget(self.save_btn)
        btns.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addWidget(self.combo)
        lay.addLayout(mid, 1)
        lay.addWidget(legend)
        lay.addLayout(btns)
        self.combo.currentIndexChanged.connect(lambda _i: self._load())
        # 처음 볼 자리: 못 찾은 것 -> 직접 잡은 것(다시 고치러 왔을 때) -> 첫째
        first = next((i for i, w in enumerate(self.where) if not w.get("png")),
                     next((i for i, w in enumerate(self.where) if w.get("manual")), 0))
        if first:
            self.combo.setCurrentIndex(first)  # 신호가 _load를 부른다
        else:
            self._load()

    @staticmethod
    def _item_text(w: dict) -> str:
        tag = " (직접 잡음)" if w.get("manual") else ("" if w.get("png") else " (위치 못 찾음)")
        return w["label"] + tag

    def _load(self):
        i = self.combo.currentIndex()
        if i < 0:
            return
        w = self.where[i]
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            p = service.icon_place(self.project_dir, w["occ"])
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            show_error(self, "카드를 보여 주지 못했습니다", e)
            return
        QApplication.restoreOverrideCursor()
        pm = QPixmap()
        pm.loadFromData(p["png"], "PNG")
        marks = [(m["box"], f"[[{m['name']}]]" if m.get("name") else self.row_label(m.get("group", "")))
                 for m in p["marks"]]
        self.view.set_data(pm, p["box"], p["guess"], marks)
        if w.get("png"):
            self.crop.setPixmap(QPixmap(w["png"]).scaledToHeight(64, Qt.TransformationMode.SmoothTransformation))
        else:
            self.crop.setText("아직 없음")
        if p["box"]:
            self.status.setText("자리가 맞지 않으면 아이콘을 끌어 다시 잡으세요.")
        elif p["guess"]:
            self.status.setText("자동으로 찾지 못했습니다. 주황 점선(예상 위치) 근처의 아이콘을 끌어 감싸세요.")
        else:
            self.status.setText("자동으로 찾지 못했습니다. 아이콘을 끌어 감싸세요.")
        self.save_btn.setEnabled(False)

    def _drawn(self):
        self.save_btn.setEnabled(self.view.new is not None)

    def _save(self):
        box = self.view.new
        if not box:
            return
        i = self.combo.currentIndex()
        w = self.where[i]
        try:
            r = service.set_icon_box(self.project_dir, w["occ"], box)
        except ValueError as e:
            show_problem(self, "자리를 저장하지 못했습니다", str(e), "아이콘을 조금 더 크게 감싸 주세요.")
            return
        except Exception as e:  # noqa: BLE001
            show_error(self, "자리를 저장하지 못했습니다", e)
            return
        self.on_saved(r)  # 이름 정하기 표를 새로 채운다 (줄 번호가 바뀔 수 있음)
        rev = service.icon_review(self.project_dir) or {"groups": []}
        new = next((ww for gg in rev["groups"] for ww in gg["where"] if ww["occ"] == w["occ"]), None)
        if new:
            self.where[i] = dict(new)
            self.combo.setItemText(i, self._item_text(new))
        self._load()
        if r.get("dict"):
            msg = f"저장함. 사전의 [[{r['dict']}]]와 모양이 같아 ‘같은 아이콘’에 미리 골라 두었습니다."
        elif not r.get("new_group"):
            msg = f"저장함. 모양이 같은 {self.row_label(r['group'])} 줄로 합쳤습니다."
        else:
            msg = f"저장함. 새 아이콘 {self.row_label(r['group'])} 줄입니다. 이름을 정해 주세요."
        self.status.setText(msg)
        gg = next((x for x in rev["groups"] if x["group"] == r["group"]), {})
        if r.get("new_group") and not r.get("dict") and not gg.get("suggest"):
            self._suggest(r["group"])

    def _suggest(self, group: str):
        """직접 잡아 새로 생긴 줄의 이름·뜻·한국어 이름을 Claude에게 제안 받는다 (못 받아도 직접 적으면 된다)."""
        eng = service.get_settings().engine
        if not service.engine_status().get(eng):
            self.status.setText(self.status.text() + " (Claude에 연결할 수 없어 이름 제안은 건너뜀)")
            return
        label = self.row_label(group)

        def done(res):
            self.on_saved({})  # 표를 다시 채워 제안을 넣는다 (적어 둔 값은 그대로)
            if res.get("suggest"):
                self.status.setText(f"저장함. 새 아이콘 {label} 줄에 이름을 제안 받았습니다: "
                                    f"{res['suggest']} ({res.get('ko') or '-'}). 확인해 주세요.")
            else:
                self.status.setText(f"저장함. 새 아이콘 {label} 줄입니다. {res.get('note') or '이름을 정해 주세요.'}")

        job = worker.Job(service.suggest_icon_name, self.project_dir, group, engine=eng, with_progress=False)
        worker.run_with_dialog(self, "아이콘 이름 제안 받는 중", job, done,
                               lambda m: self.status.setText(f"저장함. 이름 제안을 받지 못했습니다: {m}"),
                               cancellable=False)


class IconReviewDialog(QDialog):
    """새로 찾은 아이콘 묶음의 이름·뜻·한국어 이름을 정한다 (엑셀 대신).

    ‘같은 아이콘’ 칸에서 다른 줄(번호)이나 사전에 있는 아이콘을 고르면 그 아이콘과 하나로 합친다
    (잘못 잘렸거나 작게 찍혀 따로 묶인 것). ‘카드 보기’로 그 아이콘이 나온 카드 자리를 확인하고,
    위치를 못 찾았거나 잘못 잘린 아이콘은 마우스로 직접 감싸 다시 잡는다."""

    COLS = ["그림", "같은 아이콘", "이름", "뜻(원문)", "한국어 이름", "아이콘 아님", "표시 크기", "등장", "카드", "예문"]
    NEW = "(새 아이콘)"

    def __init__(self, project_dir: Path, review: dict, parent=None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.result = None
        self.setWindowTitle("② 아이콘 정의")
        fit_to_screen(self, 1400, 640, maximize_below=0)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setIconSize(QSize(40, 40))
        self.table.setStyleSheet("QLineEdit, QComboBox, QDoubleSpinBox, QPushButton { margin: 5px 4px; }"
                                 " QLineEdit, QDoubleSpinBox { padding-top: 1px; padding-bottom: 1px; }"
                                 " QCheckBox { margin-left: 12px; }")
        self.table.horizontalHeader().setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)
        self.steps_text = hint_bar("", "info")
        self.known_note = grey_note("")
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "이름 확정")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(8)
        lay.addWidget(page_title("② 아이콘 정의", 14))
        lay.addWidget(self.steps_text)
        lay.addWidget(self.known_note)
        lay.addWidget(self.table, 1)
        lay.addWidget(bb)
        self._fill(review, {})

    # ---- 표 채우기 (자리를 다시 잡으면 묶음이 바뀌어 다시 채운다; 적어 둔 값은 묶음별로 살린다)
    def _fill(self, review: dict, keep: dict):
        self.review = review
        groups = review["groups"]
        self.table.clearContents()
        self.table.setRowCount(len(groups))
        self.widgets = []
        for i, g in enumerate(groups):
            img = QTableWidgetItem("" if g["image"] else "위치 못 찾음")
            if g["image"]:
                img.setIcon(QIcon(QPixmap(g["image"])))
            else:
                img.setToolTip("못 찾은 이유: " + (g.get("reason") or "-") + "\n‘카드 보기’에서 아이콘을 끌어 감싸면 잡을 수 있습니다")
                img.setBackground(QBrush(QColor("#FFE066")))
            self.table.setItem(i, 0, img)
            same = QComboBox()
            same.addItem(self.NEW, None)
            for j, other in enumerate(groups):
                if j != i and other["image"]:
                    same.addItem(QIcon(QPixmap(other["image"])), f"{j + 1}번과 같음", ("row", j))
            for nm in review["known"]:
                same.addItem(f"사전: {nm}", ("known", nm))
            same.setIconSize(QSize(24, 24))
            name, meaning, ko = QLineEdit(g["suggest"]), QLineEdit(g["meaning"]), QLineEdit(g["ko"])
            name.setPlaceholderText("영문 소문자·숫자·_")
            not_icon = QCheckBox()
            scale = QDoubleSpinBox()
            scale.setRange(0.3, 3.0)
            scale.setSingleStep(0.05)
            scale.setValue(1.1)
            view = QPushButton("카드 보기 · 자리 잡기")  # 찾은 아이콘도 자리를 다시 잡을 수 있다
            view.setToolTip("그 아이콘이 나온 카드를 보고, 위치가 틀리면 마우스로 끌어 다시 잡습니다")
            if not g["image"]:
                view.setStyleSheet("font-weight: bold")
            view.clicked.connect(lambda _c=False, gg=g["group"]: self.show_cards(gg))
            for j, w in ((1, same), (2, name), (3, meaning), (4, ko), (5, not_icon), (6, scale), (8, view)):
                self.table.setCellWidget(i, j, w)
            self.table.setItem(i, 7, QTableWidgetItem(str(g["count"])))
            ex = QTableWidgetItem(g["examples"][0] if g["examples"] else "")
            ex.setToolTip("\n".join(g["examples"]))
            self.table.setItem(i, 9, ex)
            self.table.setRowHeight(i, 46)
            self.widgets.append((g["group"], name, meaning, ko, not_icon, scale, same))
        row_of = {g["group"]: i for i, g in enumerate(groups)}
        for i, g in enumerate(groups):  # 적어 둔 값 되살리기, 사전과 모양이 같으면 미리 고르기
            _g, name, meaning, ko, not_icon, scale, same = self.widgets[i]
            k = keep.get(g["group"])
            target = None
            if k:
                for edit, key in ((name, "name"), (meaning, "meaning"), (ko, "ko")):
                    if k[key].strip():  # 빈 칸은 새로 받은 제안을 살린다
                        edit.setText(k[key])
                not_icon.setChecked(k["not_icon"])
                scale.setValue(k["scale"])
                if k["same"] and k["same"][0] == "group" and k["same"][1] in row_of:
                    target = ("row", row_of[k["same"][1]])
                elif k["same"] and k["same"][0] == "known":
                    target = k["same"]
            elif g.get("dict"):
                target = ("known", g["dict"])
            if target is not None:
                idx = same.findData(target)
                if idx >= 0:
                    same.setCurrentIndex(idx)
            same.currentIndexChanged.connect(lambda _x, r=i: self._sync_row(r))
            not_icon.toggled.connect(lambda _x, r=i: self._sync_row(r))
            self._sync_row(i)
        self.table.resizeColumnToContents(1)
        self.table.resizeColumnToContents(8)
        for col, w in ((2, 150), (3, 170), (4, 150), (6, 100)):  # 입력 칸 글이 잘리지 않게
            self.table.setColumnWidth(col, w)
        n_issue = sum(1 for g in groups if g.get("issue"))
        n_lost = sum(1 for g in groups if not g["image"])
        set_hint(self.steps_text,
                 f"정할 아이콘 {len(groups)}개"
                 + (f" · 한국어 이름이 빈 {n_issue}개(노랑)는 맨 위" if n_issue else "")
                 + (f" · 위치 못 찾음 {n_lost}개: ‘카드 보기 · 자리 잡기’에서 아이콘을 끌어 감싸세요" if n_lost else "")
                 + "\n"
                 "1. 그림을 보고 ‘이름’(영문 소문자)과 ‘한국어 이름’을 채우세요.\n"
                 "2. 같은 아이콘이 따로 묶였으면 ‘같은 아이콘’에서 그 줄 번호를 고르세요.\n"
                 "3. 아이콘이 아니라 글자면 ‘아이콘 아님’을 체크하고 ‘뜻’ 칸에 그 글자를 적으세요 (비우면 원문에서 뺌).\n"
                 "4. 그림이 잘못 잘렸으면 ‘카드 보기’에서 다시 잡으세요.", "info")
        self.known_note.setText("사전에 이미 있는 이름: " + (", ".join(review["known"]) or "(없음)"))
        for w in self.widgets:  # 한국어 이름이 빈 줄은 노랑
            self._paint_ko_edit(w[3])
            w[3].textChanged.connect(lambda _t, e=w[3]: self._paint_ko_edit(e))

    def _capture(self) -> dict:
        """지금 표에 적힌 값 (묶음별). ‘같은 아이콘’의 줄 번호는 묶음 이름으로 바꿔 둔다."""
        out = {}
        for g, name, meaning, ko, not_icon, scale, same in self.widgets:
            d = same.currentData()
            if d and d[0] == "row":
                d = ("group", self.widgets[d[1]][0])
            out[g] = {"name": name.text(), "meaning": meaning.text(), "ko": ko.text(),
                      "not_icon": not_icon.isChecked(), "scale": scale.value(), "same": d}
        return out

    def _row_label(self, group: str) -> str:
        i = next((i for i, w in enumerate(self.widgets) if w[0] == group), None)
        return f"{i + 1}번" if i is not None else "아이콘"

    def _after_place(self, r: dict):
        keep = self._capture()
        if r.get("old_gone") and r.get("new_group") and r["old"] in keep:
            keep[r["group"]] = keep.pop(r["old"])  # 혼자 있던 줄을 옮긴 것: 적어 둔 값 그대로
            if r.get("dict"):
                keep[r["group"]]["same"] = ("known", r["dict"])
        review = service.icon_review(self.project_dir)
        if review:
            self._fill(review, keep)

    @staticmethod
    def _paint_ko_edit(edit: QLineEdit):
        edit.setStyleSheet("" if edit.text().strip() else "background: #FFE066")

    def _sync_row(self, r: int):
        _g, name, meaning, ko, not_icon, scale, same = self.widgets[r]
        merged = same.currentData() is not None
        for w in (name, ko, scale):
            w.setEnabled(not merged and not not_icon.isChecked())
        # 아이콘 아님: ‘뜻’ 칸에 그 자리 글자를 적는다 (적으면 [[?]]를 그 글자로, 비우면 뺀다)
        meaning.setEnabled(not merged)
        if not_icon.isChecked():
            meaning.setPlaceholderText("그 자리 글자 (예: 最). 비우면 원문에서 뺌")
            meaning.setToolTip("아이콘이 아니라 글자를 잘못 읽은 것이면 여기에 그 글자를 적으세요")
        else:
            meaning.setPlaceholderText("")
            meaning.setToolTip("")
        same.setEnabled(not not_icon.isChecked())
        not_icon.setEnabled(not merged)

    def show_cards(self, group: str):
        """그 묶음이 나온 카드 자리를 보여 주고, 자리를 직접 잡게 한다."""
        g = next((x for x in self.review["groups"] if x["group"] == group), None)
        if g is None:
            return
        IconPlaceDialog(self.project_dir, g, self._row_label, self._after_place, self).exec()

    def rows(self) -> list[dict]:
        """합치기를 풀어 각 줄의 최종 이름으로. 합치기가 돌고 돌면 ValueError."""
        final: dict[int, str] = {}

        def resolve(i: int, seen: tuple = ()) -> str:
            if i in final:
                return final[i]
            if i in seen:
                raise ValueError(f"{i + 1}번 줄의 ‘같은 아이콘’이 서로를 가리킵니다")
            _g, name, _m, _k, _ni, _sc, same = self.widgets[i]
            d = same.currentData()
            if d is None:
                v = name.text().strip()
            elif d[0] == "known":
                v = d[1]
            else:
                if self.widgets[d[1]][4].isChecked():
                    raise ValueError(f"{i + 1}번 줄이 ‘아이콘 아님’인 {d[1] + 1}번과 같다고 되어 있습니다")
                v = resolve(d[1], seen + (i,))
            final[i] = v
            return v

        out = []
        for i, (g, n, m, k, ni, sc, same) in enumerate(self.widgets):
            target = same.currentData()
            src = self.widgets[target[1]] if target and target[0] == "row" else None
            out.append({"group": g, "name": "" if ni.isChecked() else resolve(i),
                        "meaning": "" if ni.isChecked() else (src[2] if src else m).text().strip(),
                        "ko": (src[3] if src else k).text().strip(),
                        "not_icon": ni.isChecked(), "text": m.text().strip() if ni.isChecked() else "",
                        "scale": (src[5] if src else sc).value()})
        return out

    def accept(self):
        try:
            rows = self.rows()
        except ValueError as e:
            show_problem(self, "아이콘 이름을 확정하지 못했습니다", str(e), "표시된 줄의 이름을 고친 뒤 다시 누르세요.")
            return
        r = service.confirm_icons(self.project_dir, rows)
        if r.state != "confirmed":
            show_problem(self, "아이콘 이름을 확정하지 못했습니다", "\n".join(r.blocking), "이름을 고친 뒤 다시 누르세요.")
            return
        self.result = r
        super().accept()


# ================================================================ 흐름 (프로젝트 화면의 버튼)
class StepActions:
    """프로젝트 화면 버튼이 부르는 2단계 흐름. page: ProjectPage (project_dir, ov, refresh)."""

    def __init__(self, page):
        self.page = page
        self.resume_timer: QTimer | None = None

    @property
    def pdir(self) -> Path:
        return self.page.project_dir

    def _engine(self) -> str | None:
        eng = service.get_settings().engine
        if not service.engine_status()[eng]:
            show_error(self.page, "Claude에 연결할 수 없습니다", service.engine_status()["message"],
                       "‘파일 > 로그인 방식’에서 로그인하거나 다른 방식을 고른 뒤 다시 하세요.")
            return None
        return eng

    # ---- 추출
    def ask_scope(self, what: str) -> list | None | bool:
        """전체로 할지 고른 항목만 할지 묻는다. 전체면 그리드에서 ‘전체’를 고른다.
        돌려줌: None(전체) | 파일 폴더 목록(고른 항목) | False(취소)."""
        from app.qt.dialogs import ask_choice

        label, dirs = self.page.scope()
        if dirs is None:
            return None
        r = ask_choice(self.page, what, f"{what}을(를) 어디에 할까요?",
                       [("전체", "accept"), (f"고른 항목만 ({label})", "accept"), ("취소", "reject")], default=0)
        if r == 0:
            self.page.select_all()
            return None
        if r == 1:
            return dirs
        return False

    def extract_summary(self) -> str:
        """읽을 범위를 ‘파일 N개 = 카드 M장’으로 보여 준다. 이미지 한 장을 배열로 자르면 파일 수보다 카드가 많다."""
        fs = self.page.scoped_files()
        cards = sum(f.get("included", 0) for f in fs)
        left = sum(max(0, f.get("included", 0) - f.get("extracted", 0)) for f in fs)
        return (f"읽을 곳: 파일 {len(fs)}개 = 카드 {cards}장 (아직 안 읽은 카드 {left}장)\n"
                "카드는 PDF의 쪽, 또는 배열(가로×세로)로 자른 이미지 한 칸입니다.")

    def extract(self):
        files = self.ask_scope("글자 추출")
        if files is False:
            return
        dlg = ExtractDialog([], self.page, self.page.ov.get("card_limit"), self.extract_summary())
        if not dlg.exec():
            return
        service.set_card_limit(self.pdir, dlg.card_limit())
        _group, mode, engine = dlg.choice()
        self._run_extract(files, mode, engine)

    def _run_extract(self, files, mode, engine):
        job = worker.Job(service.extract_project, self.pdir, None, mode=mode, engine=engine, files=files)
        worker.run_with_dialog(self.page, "글자 추출", job,
                               lambda r: self._extract_done(r, (files, mode, engine)),
                               _fail_as(self.page, "글자 추출을 끝내지 못했습니다"))

    def _extract_done(self, r, args):
        self.page.refresh()
        lines = [f"{f['file']}: {state_label(f['state'])} — 읽음 {f['done']}장, 남음 {f['remaining']}장, 실패 {f['failed']}장"
                 + (f", 두 번 확인 같음 {f['recheck'][0]}·다름 {f['recheck'][1]}" if f.get("recheck") else "")
                 for f in r.files]
        if r.state == "done":
            n_check = sum(1 for g in self.page.ov["groups"] for f in g["files"]
                          for c in service.extracted_cells(f["dir"]) if SourceReviewDialog.needs_attention(c))
            QMessageBox.information(self.page, "추출 끝", "\n".join(lines)
                                    + (f"\n\n확인할 칸 {n_check}개 — ‘원문 확인’에서 이미지와 비교해 고치세요."
                                       if n_check else "\n\n확인할 칸이 없습니다."))
            return
        if r.state == "paused" and r.retry_after:
            when = _dt.datetime.strptime(r.retry_after, "%Y-%m-%d %H:%M:%S")
            if _ask_list(self.page, "추출 멈춤", f"{r.reason}", lines,
                         f"{r.retry_after} 이후 이어 할 수 있습니다. 앱을 켜 둔 채 그때 자동으로 이어 할까요?"):
                ms = max(0, int((when - _dt.datetime.now()).total_seconds() * 1000)) + 60_000
                self.resume_timer = QTimer(self.page)
                self.resume_timer.setSingleShot(True)
                self.resume_timer.timeout.connect(lambda: self._run_extract(*args))
                self.resume_timer.start(ms)
                self.page.win.statusBar().showMessage(f"추출을 {r.retry_after} 이후 자동으로 이어 합니다")
            return
        if r.state == "cancelled":
            QMessageBox.information(self.page, "글자 추출을 멈췄습니다",
                                    "읽은 카드는 저장했습니다. ‘① 글자 추출’을 다시 누르면 남은 카드부터 이어서 읽습니다.\n\n"
                                    + "\n".join(lines))
            return
        show_problem(self.page, f"글자 추출을 끝내지 못했습니다 ({state_label(r.state)})", r.reason or "",
                     retry_after=r.retry_after, detail="\n".join(lines))

    # ---- 원문 확인
    def review_sources(self, only_check: bool = True):
        files = self.page.scoped_files()  # 그리드에서 고른 범위
        if not files:
            return
        dlg = SourceReviewDialog(files, self.page, only_check)
        dlg.exec()
        if dlg.saved:
            self.page.refresh()

    # ---- 아이콘
    def icons(self):
        """누를 때마다 다시 찾는다 (원문을 고쳤거나 찾기 방식이 나아졌을 수 있으므로 예전 결과를 쓰지 않음)."""
        eng = self._engine()
        if eng is None:
            return
        job = worker.Job(service.find_icons, self.pdir, engine=eng, with_progress=False)
        worker.run_with_dialog(self.page, "아이콘 찾는 중 (이름 제안 포함)", job, self._icons_found,
                               _fail_as(self.page, "아이콘을 찾지 못했습니다"), cancellable=False)

    def _icons_found(self, r):
        msg = ("사전과 같아 바로 바꿈: " + ", ".join(f"[[{n}]] {c}곳" for n, c in r.auto.items())) if r.auto else ""
        if r.state == "done":
            # 새로 정할 아이콘이 없어도 사전을 보여 준다 (이름·크기 고치기, 잘못 묶인 아이콘 다시 정하기)
            rows = service.icon_dictionary(self.pdir)
            if not rows:
                QMessageBox.information(self.page, "아이콘", (msg + "\n" if msg else "") + "원문에 아이콘이 없습니다.")
                return
            dlg = IconDictionaryDialog(rows, (msg + "\n" if msg else "") + "새로 이름을 정할 아이콘은 없습니다. "
                                       "아래는 이 게임의 아이콘 사전입니다.", self.page)
            if dlg.exec():
                res = service.update_icon_dictionary(self.pdir, dlg.rows())
                if res["reset"]:
                    if QMessageBox.question(
                            self.page, "아이콘 다시 정하기",
                            "되돌린 아이콘: " + ", ".join(f"[[{n}]] {c}곳" for n, c in res["reset"].items())
                            + "\n지금 다시 찾아 이름을 정할까요?") == QMessageBox.StandardButton.Yes:
                        self.icons()
                self.page.refresh()
            return
        self._open_icon_review(service.icon_review(self.pdir))

    def _open_icon_review(self, review) -> bool:
        dlg = IconReviewDialog(self.pdir, review, self.page)
        if dlg.exec() and dlg.result:
            r = dlg.result
            QMessageBox.information(self.page, "아이콘 확정",
                                    f"새로 넣음: {', '.join(r.added) or '-'}\n사전에 있던 것: {', '.join(r.reused) or '-'}\n"
                                    f"원문에서 바꿈 {r.replaced}곳, 뺌 {r.removed}곳")
            self.page.refresh()
            return True
        return False

    # ---- 추출 완료 -> 용어집 검증
    def finish(self, accept: bool = False):
        if not accept and self.page.ov.get("status") in ("glossary_confirmed", "translating", "translated"):
            ver = self.page.ov.get("glossary_version")
            r = ask_choice(self.page, "용어집 다시 만들기",
                           f"용어집이 이미 확정되었습니다{f' ({ver}번째 확정본)' if ver else ''}.",
                           [("다시 만들기", "destructive"), ("취소", "reject")], default=1,
                           informative="다시 만들면 상태가 ‘용어집 검증 중’으로 돌아가고, 용어집 엑셀을 다시 가져와야 "
                                       "번역·한글 넣기를 할 수 있습니다.\n확정된 용어는 그대로 남고 새 용어만 후보로 나옵니다.")
            if r != 0:
                return
        eng = self._engine()
        if eng is None:
            return
        job = worker.Job(service.finish_extraction, self.pdir, accept_issues=accept, engine=eng, with_progress=False)
        worker.run_with_dialog(self.page, "용어 후보를 만드는 중 (몇 분 걸릴 수 있음)", job, self._finish_done,
                               _fail_as(self.page, "용어집 엑셀을 만들지 못했습니다"), cancellable=False)

    def _finish_done(self, r):
        self.page.refresh()
        if r.state == "needs_confirmation":
            if _ask_list(self.page, "용어집 만들기 확인", "아래 항목이 남아 있습니다.", r.issues, "그래도 용어집을 만들까요?"):
                self.finish(accept=True)
            return
        if r.state == "icons_review":
            QMessageBox.information(self.page, "아이콘 먼저", f"먼저 ‘② 아이콘 정의’를 해야 합니다: {r.reason}")
            review = service.icon_review(self.pdir)
            if review and self._open_icon_review(review):
                self.finish(accept=True)
            return
        if r.state in ("blocked", "paused"):
            show_problem(self.page, f"용어집 엑셀을 만들지 못했습니다 ({state_label(r.state)})", r.reason or "",
                         retry_after=r.retry_after)
            return
        box = QMessageBox(self.page)
        box.setWindowTitle("용어집 검증")
        box.setText(f"새 용어 후보 {r.new_terms}개, 이미 확정된 용어 {r.confirmed_terms}개.\n"
                    f"엑셀의 ‘번역’ 열을 확인·수정한 뒤 ‘용어집 가져오기’를 누르세요.\n(보라색: 번역이 갈릴 수 있는 용어)")
        openb = box.addButton("엑셀 열기", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("닫기", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is openb and r.review_xlsx:
            from app.qt.main import open_path

            open_path(r.review_xlsx)

    # ---- 용어집 가져오기
    def import_glossary(self, path: str | None = None, accept: bool = False):
        if path is None:
            default = str(service.game_dir(self.pdir) / "용어집_검증.xlsx")
            path, _ = QFileDialog.getOpenFileName(self.page, "검증한 용어집 엑셀", default, "Excel (*.xlsx)")
            if not path:
                return
        r = service.import_glossary(self.pdir, path, accept_issues=accept)
        if r.state == "blocked":
            show_problem(self.page, "용어집을 가져오지 못했습니다", "\n".join(r.blocking + r.issues),
                         "엑셀에서 이 내용을 고치고 저장한 뒤 ‘⑤ 용어집 가져오기’를 다시 누르세요.")
            return
        if r.state == "needs_confirmation":
            if _ask_list(self.page, "용어집 확인", "아래 항목을 확인하세요.", r.issues, "이대로 확정할까요?"):
                self.import_glossary(path, accept=True)
            return
        self.page.refresh()
        QMessageBox.information(self.page, "용어집 확정",
                                f"게임 용어집 버전 {r.version}, 용어 {r.terms}개."
                                + (f"\n번역이 바뀐 용어: {', '.join(r.changed)}\n‘번역’을 다시 하면 이 용어가 든 칸만 다시 번역합니다."
                                   if r.changed else "\n번역을 시작하거나 이어 할 수 있습니다."))

    # ---- 번역
    def translate(self):
        eng = self._engine()
        if eng is None:
            return
        box = QMessageBox(self.page)
        box.setWindowTitle("번역")
        box.setText("확정 용어집을 기준으로 모든 파일의 원문을 번역합니다. 이미 번역한 칸은 다시 하지 않습니다.")
        box.setInformativeText("보통 번역으로 충분합니다. 정밀 번역은 느리고 사용량이 많습니다.")
        sonnet = box.addButton("보통 번역 (추천)", QMessageBox.ButtonRole.AcceptRole)
        opus = box.addButton("정밀 번역", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        then_erase = self._erase_option()
        if then_erase is not None:
            box.setCheckBox(then_erase)
        box.exec()
        model = {sonnet: "sonnet", opus: "opus"}.get(box.clickedButton())
        if not model:
            return
        self._then_erase = bool(then_erase and then_erase.isChecked())
        job = worker.Job(service.translate_project, self.pdir, model=model, engine=eng, files=self.page.scope()[1])
        worker.run_with_dialog(self.page, "번역", job, self._translate_done, _fail_as(self.page, "번역을 끝내지 못했습니다"))

    def _erase_option(self) -> QCheckBox | None:
        """번역 질문 창에 붙일 ‘끝나면 바로 ⑦ 글자 지우기’ 체크 (지울 수 있는 상태일 때만)."""
        btn = getattr(self.page, "step_buttons", {}).get("erase")
        if btn is None or not btn.isEnabled() or not hasattr(self.page, "out"):
            return None
        files = self.page.scoped_files()
        if not files or all(f.get("erased") for f in files):
            return None
        cb = QCheckBox("끝나면 바로 ‘⑦ 글자 지우기’까지 하기")
        cb.setToolTip("번역과 검수 엑셀이 끝나면 이어서 글자를 지웁니다. 이미 지운 파일은 건너뜁니다.")
        cb.setChecked(True)
        return cb

    def _translate_done(self, r):
        """번역이 끝나면 검수 엑셀까지 이어서 만든다 (따로 누르는 단계 없음)."""
        self.page.refresh()
        if r.state != "done":
            self._then_erase = False
        if r.state == "cancelled":
            QMessageBox.information(self.page, "번역을 멈췄습니다",
                                    "번역한 칸은 저장했습니다. ‘⑥ 전체 번역’을 다시 누르면 남은 칸부터 이어서 합니다.")
            return
        if r.state != "done":
            show_problem(self.page, f"번역을 끝내지 못했습니다 ({state_label(r.state)})", r.reason or "",
                         retry_after=r.retry_after)
            return
        issues = []
        if r.term_mismatch:
            issues.append(f"용어 불일치 {len(r.term_mismatch)}칸")
        if r.icon_mismatch:
            issues.append(f"아이콘 불일치 {len(r.icon_mismatch)}칸")
        if r.leftover:
            issues.append(f"일본어가 남은 번역 {len(r.leftover)}칸")
        self._translate_summary = (f"서로 다른 원문 {r.cells}칸: 이번에 번역 {r.translated}, 용어집으로 바로 {r.from_glossary}, "
                                   f"이미 번역됨 {r.cached}." + ("\n확인: " + ", ".join(issues) if issues else ""))
        self.export_excel()

    def export_excel(self):
        job = worker.Job(service.export_project_excel, self.pdir, force=True, with_progress=False)
        worker.run_with_dialog(self.page, "번역 검수 엑셀 만드는 중", job, self._excel_done,
                               _fail_as(self.page, "검수 엑셀을 만들지 못했습니다"), cancellable=False)

    def _excel_done(self, paths):
        from app.qt.main import open_path

        self.page.refresh()
        head = getattr(self, "_translate_summary", "")
        self._translate_summary = ""
        if getattr(self, "_then_erase", False):  # 엑셀을 먼저 열고, 묻지 않고 바로 ⑦ 글자 지우기 (결과는 지우기 끝에 함께)
            self._then_erase = False
            if paths:
                open_path(Path(paths[0]).resolve())
            self.page.out.erase(auto=True, note=(head + "\n" if head else "")
                                + "검수 엑셀: " + ", ".join(Path(p).name for p in paths))
            return
        box = QMessageBox(self.page)
        box.setWindowTitle("⑥ 전체 번역")
        box.setText((head + "\n\n" if head else "")
                    + "검수 엑셀을 만들었습니다 (게임에 하나, 그룹마다 시트, 사람이 고친 칸은 그대로):\n"
                    + "\n".join(str(p) for p in paths)
                    + "\n\n같은 게임의 다른 프로젝트도 이 엑셀에 함께 들어 있습니다.")
        openx = box.addButton("엑셀 열기", QMessageBox.ButtonRole.AcceptRole)
        openf = box.addButton("폴더 열기", QMessageBox.ButtonRole.ActionRole)
        box.addButton("닫기", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if paths and box.clickedButton() is openx:
            open_path(Path(paths[0]).resolve())
        elif paths and box.clickedButton() is openf:
            open_path(Path(paths[0]).resolve().parent)
