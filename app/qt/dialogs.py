"""새 프로젝트·파일 넣기·뺄 쪽·설정 대화상자."""

from __future__ import annotations

from pathlib import Path

import re
import sys

from PySide6.QtCore import QLibraryInfo, QSize, Qt, QTranslator
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout,
    QLabel, QSpinBox, QStackedWidget,
    QInputDialog, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QRadioButton, QVBoxLayout,
)

from app import service

LANGS = [("ja", "일본어"), ("en", "영어")]


def install_korean(app=None) -> bool:
    """Qt 표준 버튼(취소·예·아니요 등)을 한국어로. 번역 파일을 찾아 불렀으면 True."""
    app = app or QApplication.instance()
    if app is None:
        return False
    tr = QTranslator(app)
    dirs = [QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)]
    try:
        import PySide6

        dirs.append(str(Path(PySide6.__file__).parent / "translations"))
    except Exception:  # noqa: BLE001
        pass
    if getattr(sys, "_MEIPASS", None):
        dirs.append(str(Path(sys._MEIPASS) / "PySide6" / "translations"))
    for d in dirs:
        if tr.load("qtbase_ko", d):
            app.installTranslator(tr)
            app._ko_translator = tr  # 지워지지 않게 붙들어 둔다
            return True
    return False


# 화면 색 약속: 주 버튼 파랑, 끝난 단계 연두(#D9EAD3), 확인할 것 노랑(#FFE066)·주황(#F4B183)
PRIMARY_QSS = ("QPushButton { background: #0b5cad; color: white; font-weight: bold; border: 1px solid #0b5cad;"
               " border-radius: 6px; padding: 5px 16px; min-height: 20px; }"
               "QPushButton:hover { background: #0a4f95; border-color: #0a4f95; }"
               "QPushButton:pressed { background: #083f78; }"
               "QPushButton:disabled { background: #A9C2DC; border-color: #A9C2DC; color: #F4F7FB; }")
NEXT_QSS = PRIMARY_QSS.replace("padding: 5px 16px;", "padding: 10px 26px; font-size: 12pt; border-radius: 8px;")
EMPTY_QSS = ("color: #5F6B7A; font-size: 11pt; background: white; border: 1px dashed #C5CCD6;"
             " border-radius: 10px;")
BANNER_QSS = "background: #FFF4C2; border: 1px solid #FFE066; border-radius: 8px;"
DONE_BG = "#D9EAD3"

# 안내 띠 종류: (배경, 테두리). 확인할 것 노랑·주황, 끝남 연두 (색 약속), 바탕은 조금 옅게
HINT_COLORS = {"info": ("#EEF4FB", "#C9D9EE"), "todo": ("#FFF4C2", "#FFE066"),
               "done": ("#E7F2E2", "#D9EAD3"), "warn": ("#FCE6D8", "#F4B183")}


def make_primary(btn: QPushButton) -> None:
    """주 버튼(파랑). 엔터로 눌리는 기본 버튼도 된다."""
    btn.setStyleSheet(PRIMARY_QSS)
    btn.setDefault(True)


def make_danger(btn: QPushButton) -> None:
    """되돌리기 어려운 일(삭제·초기화) 버튼: 빨간 글자 (테마의 danger 속성)."""
    btn.setProperty("danger", True)
    btn.style().unpolish(btn)
    btn.style().polish(btn)


def page_title(text: str, size: int = 15) -> QLabel:
    """창·화면 맨 위 큰 제목."""
    lb = QLabel(text)
    f = lb.font()
    f.setPointSize(size)
    f.setBold(True)
    lb.setFont(f)
    lb.setStyleSheet("color: #102A43;")
    return lb


def subtitle(text: str) -> QLabel:
    """제목 바로 아래 한 줄 설명 (회색, 줄바꿈)."""
    lb = QLabel(text)
    lb.setWordWrap(True)
    lb.setStyleSheet("color: #5F6B7A;")
    return lb


def section_title(text: str) -> QLabel:
    """창 안의 묶음 제목 (굵게, 조금 크게)."""
    lb = QLabel(text)
    f = lb.font()
    f.setPointSize(f.pointSize() + 1 if f.pointSize() > 0 else 11)
    f.setBold(True)
    lb.setFont(f)
    lb.setStyleSheet("color: #243B53;")
    return lb


def card(*items, margins: tuple[int, int, int, int] = (14, 12, 14, 12), spacing: int = 8) -> QFrame:
    """흰 카드(둥근 테두리) 안에 위젯·배치를 위에서 아래로 담는다."""
    fr = QFrame()
    fr.setObjectName("card")
    fr.setStyleSheet("QFrame#card { background: white; border: 1px solid #E6E9EE; border-radius: 10px; }")
    lay = QVBoxLayout(fr)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for it in items:
        if it is None:
            continue
        (lay.addLayout if not hasattr(it, "show") else lay.addWidget)(it)
    return fr


def file_label(key: str) -> str:
    """파일 이름 표시: ‘그룹/파일’에서 그룹과 파일 이름이 같으면 하나만, 다르면 ‘그룹 › 파일’."""
    if "/" not in (key or ""):
        return key
    g, f = key.split("/", 1)
    return f if g == f else f"{g} › {f}"


ERASE_LABELS = {
    "auto": "자동 (알아서 고름)",
    "solid": "한 가지 색으로 덮기",
    "pattern": "주변 무늬로 채우기",
    "rect_avg": "사각형으로 덮기",
    "inpaint": "뒤 그림 이어 채우기",
}


def erase_modes() -> list[tuple[str, str]]:
    """글자 지우기 방식 [(키, 쉬운 문구)]. 키는 service.ERASE_MODES 그대로, 문구만 일반 말로."""
    return [(k, ERASE_LABELS.get(k, t)) for k, t in service.ERASE_MODES]


def set_ok_text(bb: QDialogButtonBox, text: str, primary: bool = True) -> None:
    """확인 버튼을 ‘확인’ 대신 하는 일 이름(만들기·넣기·저장)으로. primary이면 파랗게."""
    b = bb.button(QDialogButtonBox.StandardButton.Ok)
    b.setText(text)
    if primary:
        make_primary(b)


def set_hint(label: QLabel, text: str, kind: str = "info") -> None:
    """안내 띠의 글과 종류(info·todo·done·warn)를 바꾼다."""
    bg, border = HINT_COLORS.get(kind, HINT_COLORS["info"])
    label.setText(text)
    label.setProperty("hint_kind", kind)
    label.setStyleSheet(f"background: {bg}; border: 1px solid {border}; border-radius: 8px; padding: 8px 12px;")


def hint_bar(text: str, kind: str = "info") -> QLabel:
    """창 맨 위 같은 곳에 두는 안내 띠 (줄바꿈 허용)."""
    lb = QLabel()
    lb.setWordWrap(True)
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    set_hint(lb, text, kind)
    return lb


def grey_note(text: str) -> QLabel:
    """회색 보조 설명."""
    lb = QLabel(text)
    lb.setWordWrap(True)
    lb.setStyleSheet("color: #5F6B7A")
    return lb


def fit_to_screen(w, width: int, height: int, margin: float = 0.92, maximize_below: int = 800) -> None:
    """원하는 크기로 열되 화면(작업 표시줄 제외)보다 크지 않게 줄인다. 화면 높이가 낮으면 최대화로 연다."""
    scr = None
    try:
        scr = (w.parentWidget() or w).screen()
    except Exception:  # noqa: BLE001
        pass
    scr = scr or QGuiApplication.primaryScreen()
    if scr is None:
        w.resize(width, height)
        return
    g = scr.availableGeometry()
    w.resize(min(width, int(g.width() * margin)), min(height, int(g.height() * margin)))
    if maximize_below and g.height() < maximize_below:
        w.setWindowState(w.windowState() | Qt.WindowState.WindowMaximized)


# ---------------------------------------------------------------- 오류 창: 왜 → 무엇을 하면 되는지
UNKNOWN_TODO = "다시 해 보고, 계속되면 ‘자세히 보기’ 내용을 알려 주세요."

# (찾을 낱말들, 왜, 무엇을 하면 되는지). 위에서부터 먼저 맞는 것을 쓴다.
_CAUSES = [
    (("PermissionError", "Permission denied", "Errno 13", "WinError 32", "WinError 5", "다른 프로세스",
      "being used by another process", "액세스가 거부"),
     "파일이 다른 프로그램(엑셀·PDF 보기 등)에서 열려 있어 쓸 수 없습니다.",
     "이 프로젝트의 엑셀·PDF 파일을 닫고 다시 하세요."),
    (("NotAvailable", "로그인되어 있지 않", "인증 오류", "claude 명령", "API 키", "api key", "authentication",
      "invalid x-api-key", "error code: 401"),
     "Claude에 연결하지 못했습니다 (로그인이 풀렸거나, Claude 프로그램이나 API 키를 찾지 못함).",
     "‘파일 > 로그인 방식’에서 로그인을 확인한 뒤 다시 하세요."),
    (("사용 한도", "한도", "rate limit", "rate_limit", "usage limit", "error code: 429", "overloaded", "error code: 529"),
     "Claude 사용 한도에 걸렸습니다.",
     "잠시 뒤 다시 누르면 이미 끝낸 곳 다음부터 이어서 합니다."),
    (("ConnectionError", "connection error", "timed out", "Timeout", "연결이 끊", "연결할 수 없"),
     "인터넷 연결이 끊겼거나 응답이 너무 늦습니다.",
     "연결을 확인하고 다시 하세요. 이미 끝낸 부분은 다시 하지 않습니다."),
    (("FileNotFoundError", "No such file", "찾을 수 없", "찾지 못했"),
     "필요한 파일을 찾지 못했습니다 (옮겼거나 지웠을 수 있음).",
     "프로젝트 화면에서 ‘새로 고침’을 누른 뒤 다시 하세요. 계속되면 ‘파일 넣기’로 파일을 다시 넣으세요."),
    (("영역 없음", "영역이 없", "영역을 지정"),
     "영역을 지정하지 않은 그룹이나 카드가 있습니다.",
     "‘영역 지정’에서 영역을 그린 뒤 다시 하세요."),
]


def explain_error(text: str) -> tuple[str, str] | None:
    """흔한 오류 원인을 쉬운 말로: (왜, 무엇을 하면 되는지). 모르는 오류면 None."""
    t = text or ""
    low = t.lower()
    for keys, why, todo in _CAUSES:
        if any(k.lower() in low for k in keys):
            return why, todo
    return None


_LIST_BREAKERS = (": ", "(", ")", "=", "←", "→", "—", "\n", "/", "\\")
_FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{2,5}\b")  # 파일 이름 (x.pdf 등): 이름에 쉼표가 있을 수 있다


def _split_keys(target: str) -> list[str]:
    """‘a, b, c’처럼 이름만 쉼표로 이은 것이 분명할 때만 낱개로 쪼갠다. 그 밖의 글이 섞였거나, 파일 이름·경로가
    있거나(‘Cards, Vol 2.pdf 3장’), 줄여서 ‘…’로 끝나는 목록이면 그대로 한 줄 (건수도 1)."""
    parts = [p.strip() for p in target.split(", ")]
    if (len(parts) < 2 or any(not p or len(p) > 40 for p in parts) or any(b in target for b in _LIST_BREAKERS)
            or target.endswith("…") or _FILE_EXT_RE.search(target)):
        return [target]
    return parts


def group_issue_lines(items: list[str]) -> str:
    """‘문제: 대상’ 목록을 문제별로 묶는다: 문제 줄(몇 건) 다음에 대상을 한 줄씩. 처음 나온 순서를 지킨다.
    대상이 하나뿐이어도 똑같이 보이고, ‘: ’가 없는 줄은 ‘기타’ 아래에 둔다."""
    groups: dict[str, list[str]] = {}
    plain: list[str] = []
    for it in items:
        it = (it or "").strip()
        if not it:
            continue
        head, sep, target = it.partition(": ")
        if not sep or not head.strip() or not target.strip():
            plain.append(it)
            continue
        groups.setdefault(head.strip(), []).extend(_split_keys(target.strip()))
    blocks = [f"{h} ({len(t)}건)\n" + "\n".join(f"  - {x}" for x in t) for h, t in groups.items()]
    if plain:
        blocks.append(f"기타 ({len(plain)}건)\n" + "\n".join(f"  - {x}" for x in plain))
    return "\n\n".join(blocks)


def show_error(parent, title: str, why: str, todo: str = "", detail: str | None = None,
               warn: bool = True) -> QMessageBox:
    """오류·경고 창. 제목=무엇이 안 됐는지, 본문=왜, 아래=무엇을 하면 되는지, 원문 메시지는 ‘자세히 보기’에."""
    import logging

    logging.getLogger("cardtranslater.ui").warning("오류 창: %s | %s | %s%s", title, why, todo,
                                                   f"\n  자세히: {detail.strip()[:4000]}" if detail else "")
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Warning if warn else QMessageBox.Icon.Critical)
    box.setText(why)
    if todo:
        box.setInformativeText(todo)
    if detail and detail.strip() and detail.strip() != why.strip():
        box.setDetailedText(detail.strip()[:6000])
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()
    return box


def _first_line(text: str, limit: int = 200) -> str:
    line = next((x.strip() for x in (text or "").splitlines() if x.strip()), "")
    return line if len(line) <= limit else line[:limit] + "…"


def show_failure(parent, title: str, message: str) -> QMessageBox:
    """작업 중 예외가 난 경우 (worker 실패 콜백). 원인을 알아보면 쉬운 안내, 아니면 ‘자세히 보기’를 알려 달라고."""
    known = explain_error(message)
    if known:
        return show_error(parent, title, known[0], known[1], message)
    return show_error(parent, title, "예상하지 못한 문제가 생겼습니다.", UNKNOWN_TODO, message, warn=False)


def show_problem(parent, title: str, reason: str = "", todo: str = "", retry_after: str | None = None,
                 detail: str | None = None, items: list[str] | None = None) -> QMessageBox:
    """엔진이 ‘할 수 없음·멈춤·실패’ 상태와 이유를 돌려준 경우. reason은 엔진이 쓴 한국어 이유 (여러 줄이어도
    줄글 그대로). items = ‘문제: 대상’ 목록 (blocking·issues 등): 주면 ‘자세히 보기’에 문제별로 묶어 보이고,
    본문에는 reason(없으면 첫 항목)과 건수를 쓴다."""
    items = [x.strip() for x in items or [] if x and x.strip()]
    reason = (reason or "").strip()
    if items:
        shown = group_issue_lines(items) if len(items) > 1 else items[0]
        if reason:
            why = reason + f"\n({len(items)}건 — ‘자세히 보기’에 모두 있습니다)"
        else:
            why = _first_line(items[0]) + (f"\n(외 {len(items) - 1}건 — ‘자세히 보기’에 모두 있습니다)"
                                           if len(items) > 1 else "")
    else:
        shown = reason
        lines = [x for x in reason.splitlines() if x.strip()]
        why = (reason if len(lines) <= 4 and len(reason) <= 600
               else _first_line(reason) + "\n(나머지는 ‘자세히 보기’에 있습니다)")
    full = "\n\n".join(x for x in (shown, (detail or "").strip()) if x)
    if retry_after:
        return show_error(parent, title, "Claude 사용 한도에 걸려 멈췄습니다.",
                          f"{retry_after} 이후 다시 누르면 멈춘 곳부터 이어서 합니다.", full)
    known = explain_error("\n".join([reason, *items]))
    if known:
        return show_error(parent, title, known[0], todo or known[1], full)
    return show_error(parent, title, why or "작업을 마치지 못했습니다.", todo or UNKNOWN_TODO, full)


def ask_choice(parent, title: str, text: str, buttons: list[tuple[str, str]], default: int | None = None,
               informative: str = "") -> int | None:
    """동작 이름 버튼으로 묻는다. buttons: [(문구, 'accept'|'destructive'|'reject')]. 누른 버튼 번호, 닫으면 None."""
    roles = {"accept": QMessageBox.ButtonRole.AcceptRole, "destructive": QMessageBox.ButtonRole.DestructiveRole,
             "reject": QMessageBox.ButtonRole.RejectRole}
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    made = [box.addButton(t, roles[r]) for t, r in buttons]
    if default is not None:
        box.setDefaultButton(made[default])
    for b, (_t, r) in zip(made, buttons):
        if r == "reject":
            box.setEscapeButton(b)
    box.exec()
    clicked = box.clickedButton()
    return made.index(clicked) if clicked in made else None


class NewProjectDialog(QDialog):
    """새 게임 이름을 넣거나 기존 게임을 골라 프로젝트를 만든다 (PRD 0단계)."""

    def __init__(self, root: Path, parent=None, game: str | None = None):
        super().__init__(parent)
        self.root = Path(root)
        self.created = None
        self.setWindowTitle("새 프로젝트")
        self.setMinimumWidth(460)
        self.name = QLineEdit()
        self.game = QComboBox()
        self.game.setEditable(True)
        self.games = [g["name"] for g in service.list_games(self.root)]
        self.game.addItems(self.games)
        self.game.setCurrentText(game or "")
        self.game.lineEdit().setPlaceholderText("기존 게임을 고르거나 새 게임 이름을 입력")
        self.lang = QComboBox()
        for k, t in LANGS:
            self.lang.addItem(t, k)
        self.hint = QLabel()
        self.hint.setStyleSheet("color: #0b5cad")
        self.game.currentTextChanged.connect(self._hint)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(14)
        form.addRow("게임", self.game)  # 프로젝트는 게임 안에 있다: 게임을 먼저
        form.addRow("", self.hint)
        self._hint_label = form.labelForField(self.hint)
        form.addRow("프로젝트 이름", self.name)
        form.addRow("원문 언어", self.lang)
        self.name.textEdited.connect(lambda _t: setattr(self, "_name_auto", False))
        self._name_auto = True
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "만들기")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(12)
        lay.addWidget(page_title("새 프로젝트 만들기", 14))
        lay.addWidget(subtitle("게임 안에 프로젝트를 만듭니다. 같은 게임의 프로젝트는 용어집과 아이콘 사전을 함께 씁니다."))
        self.form = form
        lay.addWidget(card(form))
        lay.addWidget(bb)
        self._hint(self.game.currentText())

    def _hint(self, text: str):
        t = text.strip()
        if not t:
            self.hint.setText("")
        elif t in self.games:
            self.hint.setText("기존 게임: 확정된 용어집과 아이콘 사전을 그대로 씁니다")
        else:
            self.hint.setText("새 게임을 만듭니다")
        # 프로젝트 이름 기본값: 새 게임이면 게임 이름, 기존 게임에 더하는 프로젝트면 비움 (사람이 고친 이름은 둔다)
        if getattr(self, "_name_auto", True):
            self.name.setText("" if (not t or t in self.games) else t)
        self.hint.setVisible(bool(self.hint.text()))
        lab = getattr(self, "_hint_label", None)
        if lab is not None:
            lab.setVisible(bool(self.hint.text()))

    def accept(self):
        name, game = self.name.text().strip(), self.game.currentText().strip()
        if not name or not game:
            show_error(self, "프로젝트를 만들 수 없습니다", "프로젝트 이름이나 게임 이름이 비어 있습니다.",
                       "두 칸을 모두 채운 뒤 ‘만들기’를 누르세요.")
            return
        try:
            info, _new = service.new_project(name, game, self.lang.currentData(), root=self.root)
        except FileExistsError:
            show_error(self, "프로젝트를 만들 수 없습니다", f"‘{game}’ 게임에 같은 이름의 프로젝트(‘{name}’)가 이미 있습니다.",
                       "다른 이름을 넣으세요. (다른 게임에는 같은 이름을 써도 됩니다)")
            return
        except ValueError as e:
            show_problem(self, "프로젝트를 만들 수 없습니다", str(e), "이름에서 쓸 수 없는 글자를 빼고 다시 넣으세요.")
            return
        self.created = info.dir
        super().accept()


IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")


class AddFilesDialog(QDialog):
    """카드 파일(PDF 또는 이미지)을 기존 그룹에 넣거나 새 그룹을 만든다 (PRD 1단계).

    이미지(JPG·PNG 등)는 카드 한 장이 한 쪽인 PDF 하나로 묶어 넣는다. 이미지 한 장(또는 PDF 한 쪽)에 카드가
    가로 X × 세로 Y로 놓여 있으면 ‘여러 장 배열’을 고르고, 미리 보기로 자를 자리를 확인한다.
    카드가 불규칙하게 놓였으면 ‘수동으로 영역 지정’을 골라 카드 자리를 직접 그린다 (CardBoxesDialog).
    PDF를 배열·수동으로 자르면 쪽마다 그림으로 바꿔(임시 폴더) 이미지와 같은 방법으로 자르고, PDF 하나가 파일 하나가 된다."""

    def __init__(self, project_dir: Path, parent=None, files: list[str] | None = None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.result_info = None
        self._pdf_pages: dict[str, list[str]] = {}  # PDF 경로 -> 쪽 그림 (배열·수동 자르기용, 임시 폴더)
        self._tmp = None
        self.finished.connect(lambda _r: self._cleanup())
        self.setWindowTitle("파일 넣기")
        self.groups = service.list_groups(self.project_dir)
        self.files = QListWidget()
        self.files.setTextElideMode(Qt.TextElideMode.ElideMiddle)  # 긴 경로는 가운데를 줄여 파일 이름이 보이게
        self.files.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        for f in files or []:
            self._add_path(f)
        self.files.setMinimumHeight(100)
        self.files.setSpacing(1)
        self.pick = QPushButton("파일 고르기… (PDF·JPG·PNG)")
        self.pick.clicked.connect(self._pick)
        remove = QPushButton("목록에서 빼기")
        remove.clicked.connect(self._remove)
        prow = QHBoxLayout()
        prow.addWidget(self.pick)
        prow.addWidget(remove)
        prow.addStretch(1)
        self.r_exist = QRadioButton("기존 그룹에 넣기")
        self.r_new = QRadioButton("새 그룹 만들기")
        bg = QButtonGroup(self)
        bg.addButton(self.r_exist)
        bg.addButton(self.r_new)
        self.group_combo = QComboBox()
        for g in self.groups:
            self.group_combo.addItem(f"{g['name']}  (파일 {len(g['files'])}개, 영역 {g['regions']}개)", g["name"])
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("예: 아이템 카드")
        self.copy_from = QComboBox()
        self.copy_from.addItem("(복사하지 않음: 영역을 새로 그림)", "")
        for g in self.groups:
            if g["regions"]:
                self.copy_from.addItem(f"{g['name']}의 영역 복사", str(Path(g["dir"]) / "regions.json"))
        if self.groups:
            self.r_exist.setChecked(True)
        else:
            self.r_new.setChecked(True)
            self.r_exist.setEnabled(False)
        self.r_exist.toggled.connect(self._sync)
        form = QFormLayout()
        form.addRow(self.r_exist, self.group_combo)
        form.addRow(self.r_new, self.new_name)
        form.addRow("새 그룹 영역", self.copy_from)

        # ---- 이미지일 때
        self.img_box = QGroupBox("③ 한 장(이미지 한 장·PDF 한 쪽)에 카드가 몇 장인가요?")
        self.r_single = QRadioButton("한 장에 카드 한 장")
        self.r_grid = QRadioButton("한 장에 카드 여러 장 (배열)")
        self.r_manual = QRadioButton("수동으로 영역 지정")
        self.r_manual.setToolTip("카드가 줄 맞춰 놓이지 않았거나 배열 자르기가 맞지 않을 때, 이미지 위에 카드 자리를 직접 그립니다")
        self.manual_boxes: dict = {}  # {이미지 경로: [(x0, y0, x1, y1) 원본 px]}
        ib = QButtonGroup(self)
        ib.addButton(self.r_single)
        ib.addButton(self.r_grid)
        ib.addButton(self.r_manual)
        self.r_single.setChecked(True)
        self.cols, self.rows = QSpinBox(), QSpinBox()
        for sp, v in ((self.cols, 3), (self.rows, 3)):
            sp.setRange(1, 20)
            sp.setValue(v)
        grow = QHBoxLayout()
        grow.addWidget(QLabel("가로"))
        grow.addWidget(self.cols)
        grow.addWidget(QLabel("장 × 세로"))
        grow.addWidget(self.rows)
        grow.addWidget(QLabel("장"))
        grow.addStretch(1)
        self.img_name = QLineEdit()
        self.img_name.setPlaceholderText("비워 두면 첫 이미지 이름")
        self.b_preview = QPushButton("자를 자리 미리 보기")
        self.b_preview.clicked.connect(self.preview)
        self.b_draw = QPushButton("카드 자리 그리기…")
        self.b_draw.clicked.connect(self.draw_boxes)
        mrow = QHBoxLayout()
        mrow.addWidget(self.b_draw)
        mrow.addStretch(1)
        self.img_info = QLabel()
        self.img_info.setWordWrap(True)
        self.img_info.setStyleSheet("color: #666")
        il = QFormLayout(self.img_box)
        il.addRow(self.r_single)
        il.addRow(self.r_grid, grow)
        il.addRow(self.r_manual, mrow)
        il.addRow("파일 이름", self.img_name)
        il.addRow(self.b_preview)
        il.addRow(self.img_info)
        for w in (self.r_single, self.r_grid, self.r_manual):
            w.toggled.connect(lambda _c: self._sync())
        for sp in (self.cols, self.rows):
            sp.valueChanged.connect(lambda _v: self._sync())
        self.files.model().rowsInserted.connect(lambda *_a: self._sync())
        self.files.model().rowsRemoved.connect(lambda *_a: self._sync())

        self.empty_hint = QLabel("‘파일 고르기…’를 눌러 카드 PDF나 그림(JPG·PNG)을 고르세요.\n여러 개를 한 번에 골라도 됩니다.")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setStyleSheet(EMPTY_QSS)
        self.files_stack = QStackedWidget()
        self.files_stack.addWidget(self.files)
        self.files_stack.addWidget(self.empty_hint)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "넣기")
        self.ok_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(12)
        form.setVerticalSpacing(8)
        lay.addWidget(page_title("카드 파일 넣기", 14))
        lay.addWidget(card(section_title("① 넣을 파일"),
                           grey_note("카드 PDF나 그림(JPG·PNG)을 고르세요. 그림은 카드 한 장이 한 쪽인 PDF 하나로 묶어 넣습니다."),
                           prow, self.files_stack), 1)
        lay.addWidget(card(section_title("② 어느 그룹에 넣을까요?"),
                           grey_note("같은 형식의 카드끼리 한 그룹입니다. 그룹마다 영역을 한 번 지정합니다."), form))
        lay.addWidget(self.img_box)
        lay.addWidget(bb)
        self.resize(640, 620)
        self._sync()

    def all_files(self) -> list[str]:
        """고른 파일의 전체 경로 (목록에는 파일 이름만 보이고 경로는 줄에 담아 둔다)."""
        return [self._path_of(self.files.item(i)) for i in range(self.files.count())]

    @staticmethod
    def _path_of(it) -> str:
        return it.data(Qt.ItemDataRole.UserRole) or it.text()

    def _add_path(self, f: str) -> None:
        if f in self.all_files():
            return
        it = QListWidgetItem(f"{Path(f).name}")
        it.setData(Qt.ItemDataRole.UserRole, f)
        it.setToolTip(f)
        self.files.addItem(it)

    def image_files(self) -> list[str]:
        return [f for f in self.all_files() if Path(f).suffix.lower() in IMAGE_EXTS]

    def pdf_files(self) -> list[str]:
        return [f for f in self.all_files() if Path(f).suffix.lower() == ".pdf"]

    def _cleanup(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None
            self._pdf_pages = {}

    def pdf_pages(self, pdf: str) -> list[str]:
        """PDF 쪽 그림 (처음 부를 때 만든다). 배열·수동으로 자를 때만 쓴다."""
        if pdf not in self._pdf_pages:
            import tempfile

            if self._tmp is None:
                self._tmp = tempfile.TemporaryDirectory(prefix="ct_pdf_sheets_")
            out = Path(self._tmp.name) / str(len(self._pdf_pages))
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self._pdf_pages[pdf] = [str(p) for p in service.pdf_sheet_images(pdf, out)]
            finally:
                QApplication.restoreOverrideCursor()
        return self._pdf_pages[pdf]

    def sheet_files(self) -> list[str]:
        """배열·수동으로 자를 그림: 이미지 + PDF 쪽 그림 (목록 순서대로)."""
        out = []
        for f in self.all_files():
            if Path(f).suffix.lower() in IMAGE_EXTS:
                out.append(f)
            elif Path(f).suffix.lower() == ".pdf":
                out += self.pdf_pages(f)
        return out

    def _known_sheets(self) -> list[str]:
        """이미 만든 그림만 (화면을 갱신할 때 PDF를 새로 그리지 않게): 이미지 + 만들어 둔 PDF 쪽 그림."""
        return self.image_files() + [p for f in self.pdf_files() for p in self._pdf_pages.get(f, [])]

    def _page_count(self, pdf) -> int:
        """PDF 쪽 수 (파일마다 한 번만 연다: 칸 수를 바꿀 때마다 다시 열지 않게)."""
        cache = self.__dict__.setdefault("_page_counts", {})
        if pdf not in cache:
            cache[pdf] = service.pdf_page_count(pdf)
        return cache[pdf]

    def _sync(self):
        # 새 그룹 이름: 비어 있거나 앞서 자동으로 넣은 이름이면 첫 파일 이름으로 채운다 (사람이 고친 이름은 둔다)
        files = self.all_files()
        stem = Path(files[0]).stem if files else ""
        if self.new_name.text().strip() in ("", getattr(self, "_auto_name", "")):
            self.new_name.setText(stem)
        self._auto_name = stem
        ex = self.r_exist.isChecked()
        self.group_combo.setEnabled(ex)
        self.new_name.setEnabled(not ex)
        self.copy_from.setEnabled(not ex)
        imgs = self.image_files()
        pdfs = self.pdf_files()
        empty = not files
        self.files_stack.setCurrentWidget(self.empty_hint if empty else self.files)
        self.pick.setStyleSheet(PRIMARY_QSS if empty else "")
        self.ok_btn.setEnabled(not empty)
        self.img_box.setVisible(bool(imgs or pdfs))
        grid = self.r_grid.isChecked()
        manual = self.r_manual.isChecked()
        self.cols.setEnabled(grid)
        self.rows.setEnabled(grid)
        self.b_draw.setEnabled(manual)
        self.b_preview.setVisible(not manual)  # 수동이면 그린 자리가 곧 미리 보기
        self.b_preview.setEnabled(bool(imgs) or grid)  # PDF는 배열일 때만 자를 자리를 미리 본다
        self.img_name.setEnabled(bool(imgs))  # 이미지를 묶은 파일 이름 (PDF는 PDF 이름 그대로)
        cut = grid or manual
        n_pdf_pages = sum(self._page_count(f) for f in pdfs) if pdfs and cut else 0
        what = " + ".join(x for x in (f"이미지 {len(imgs)}장" if imgs else "",
                                      f"PDF {len(pdfs)}개({n_pdf_pages}쪽)" if pdfs and cut else "") if x)
        if (imgs or pdfs) and manual:
            boxes = self._manual_for(self._known_sheets())
            if any(boxes.values()):
                n = sum(len(v) for v in boxes.values())
                self.img_info.setText(f"{what} → 카드 {n}장 (그린 자리대로, 한 쪽에 한 장씩 PDF로)")
            else:
                self.img_info.setText("‘카드 자리 그리기…’를 눌러 이미지·PDF 쪽 위에 카드 자리를 그리세요.")
        elif (imgs or pdfs) and grid:
            per = self.cols.value() * self.rows.value()
            self.img_info.setText(f"{what} → 카드 약 {(len(imgs) + n_pdf_pages) * per}장 (한 쪽에 한 장씩 PDF로)")
        elif imgs:
            self.img_info.setText(f"이미지 {len(imgs)}장 → 카드 {len(imgs)}장 (한 쪽에 한 장씩 PDF로)"
                                  + (" · PDF는 그대로 넣습니다" if pdfs else ""))
        else:
            self.img_info.setText("PDF는 쪽마다 카드 한 장으로 그대로 넣습니다. 한 쪽에 카드가 여러 장이면 위에서 고르세요.")

    def _pick(self):
        exts = " ".join(f"*{e}" for e in IMAGE_EXTS)
        fs, _ = QFileDialog.getOpenFileNames(self, "카드 파일 고르기", "",
                                             f"카드 파일 (*.pdf {exts});;PDF (*.pdf);;이미지 ({exts})")
        for f in sorted(fs):
            self._add_path(f)

    def _remove(self):
        for it in self.files.selectedItems():
            self.files.takeItem(self.files.row(it))

    def _manual_for(self, imgs: list[str]) -> dict:
        """지금 목록의 이미지에 해당하는 그린 자리만 (목록에서 뺀 이미지 것은 버린다)."""
        return {p: b for p, b in self.manual_boxes.items() if p in imgs}  # [] = 그 이미지에서는 카드를 만들지 않음

    def draw_boxes(self) -> None:
        from app.qt.card_boxes import CardBoxesDialog

        try:
            imgs = self.sheet_files()
        except Exception as e:  # noqa: BLE001
            show_problem(self, "PDF 쪽을 그림으로 바꾸지 못했습니다", str(e), "PDF가 열리는지 확인하세요.")
            return
        if not imgs:
            return
        dlg = CardBoxesDialog(imgs, self._manual_for(imgs), self)
        if dlg.exec():
            self.manual_boxes = dlg.result()
        self._sync()

    def layout_choice(self) -> tuple[int, int]:
        return (self.cols.value(), self.rows.value()) if self.r_grid.isChecked() else (1, 1)

    def preview(self):
        sel = [self._path_of(it) for it in self.files.selectedItems()
               if Path(self._path_of(it)).suffix.lower() in IMAGE_EXTS + (".pdf",)]
        if sel and sel[0].lower().endswith(".pdf") and not self.r_grid.isChecked():
            sel = []
        cands = sel or self.image_files() + (self.pdf_files() if self.r_grid.isChecked() else [])
        if not cands:
            return
        path = cands[0]
        if path.lower().endswith(".pdf"):  # PDF는 첫 쪽으로 미리 본다
            try:
                path = self.pdf_pages(path)[0]
            except Exception as e:  # noqa: BLE001
                show_problem(self, "PDF 쪽을 그림으로 바꾸지 못했습니다", str(e), "PDF가 열리는지 확인하세요.")
                return
        cols, rows = self.layout_choice()
        try:
            png, info = service.image_preview(path, cols, rows)
        except Exception as e:  # noqa: BLE001
            show_problem(self, "자를 자리를 미리 볼 수 없습니다", str(e),
                         "그림 파일이 열리는지 확인하고, 가로·세로 장수를 바꿔 다시 해 보세요.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"자를 자리 — {Path(path).name}")
        pic = QLabel()
        pm = QPixmap()
        pm.loadFromData(png, "PNG")
        pic.setPixmap(pm)
        msg = f"카드 {info['cards']}장으로 자릅니다 (빨간 상자)."
        if info["warnings"]:
            msg += "\n" + "\n".join(info["warnings"])
        note = QLabel(msg)
        note.setWordWrap(True)
        close = QPushButton("닫기")
        close.clicked.connect(dlg.accept)
        dl = QVBoxLayout(dlg)
        dl.addWidget(pic)
        dl.addWidget(note)
        dl.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        dlg.exec()

    def accept(self):
        files = self.all_files()
        if not files:
            show_error(self, "넣을 파일이 없습니다", "아직 고른 파일이 없습니다.",
                       "‘파일 고르기…’로 카드 PDF나 그림(JPG·PNG)을 고르세요.")
            return
        new = self.r_new.isChecked()
        group = self.new_name.text().strip() if new else self.group_combo.currentData()
        if not group:
            show_error(self, "새 그룹 이름이 비어 있습니다", "새 그룹을 만들려면 이름이 필요합니다.",
                       "‘새 그룹 만들기’ 옆 칸에 이름을 넣으세요 (예: 아이템 카드).")
            return
        regions_from = (self.copy_from.currentData() or None) if new else None
        pdfs = [f for f in files if Path(f).suffix.lower() == ".pdf"]
        imgs = self.image_files()
        others = [f for f in files if f not in pdfs and f not in imgs]
        added, skipped, warnings = [], [f"{Path(f).name}: PDF·이미지가 아님" for f in others], []
        has_regions = False
        boxes = None
        cut_pdfs = bool(pdfs) and (self.r_grid.isChecked() or self.r_manual.isChecked())  # PDF도 쪽을 잘라 넣는다
        if (imgs or cut_pdfs) and self.r_manual.isChecked():  # 아무것도 넣기 전에 확인
            boxes = self._manual_for(self._known_sheets())
            if not any(boxes.values()):
                show_error(self, "카드 자리를 그리지 않았습니다", "‘수동으로 영역 지정’은 카드 자리를 직접 그려야 합니다.",
                           "‘카드 자리 그리기…’를 눌러 이미지·PDF 쪽 위에 카드마다 사각형을 그리세요.")
                return
        if cut_pdfs:  # 아무것도 넣기 전에 모든 PDF를 그림으로 (깨진 PDF 때문에 반만 넣지 않게)
            try:
                for pdf in pdfs:
                    self.pdf_pages(pdf)
            except Exception as e:  # noqa: BLE001 - pymupdf는 RuntimeError 계열을 낸다
                show_problem(self, "PDF 쪽을 그림으로 바꾸지 못했습니다", f"{Path(pdf).name}: {e}", "PDF가 열리는지 확인하세요.")
                return
        try:
            if cut_pdfs:
                cols, rows = self.layout_choice()
                for pdf in pdfs:  # PDF 하나 = 파일 하나 (쪽 그림을 이미지처럼 자른다)
                    pages = self.pdf_pages(pdf)
                    own = None if boxes is None else {p: boxes.get(p, []) for p in pages}  # 안 그린 쪽은 넣지 않음
                    if own is not None and not any(own.values()):
                        skipped.append(f"{Path(pdf).name}: 카드 자리를 그리지 않아 넣지 않음")
                        continue
                    if own is not None and not all(own.values()):
                        warnings.append(f"{Path(pdf).name}: 카드 자리를 그리지 않은 "
                                        f"{sum(1 for v in own.values() if not v)}쪽은 넣지 않았습니다")
                    ri = service.add_images(self.project_dir, pages, group, cols=cols, rows=rows,
                                            name=Path(pdf).stem, new_group=new, regions_from=regions_from,
                                            boxes=own, origin_pdf=pdf)
                    new, regions_from = False, None
                    if ri.file_dir:
                        added.append(ri.file_dir)
                        warnings.insert(0, f"{Path(pdf).name}: {len(pages)}쪽 → 카드 {ri.pages}장")
                    skipped += ri.skipped
                    warnings += ri.warnings
                    has_regions = ri.has_regions
                pdfs = []
            if pdfs:
                r = service.add_files(self.project_dir, pdfs, group, new_group=new, regions_from=regions_from)
                added += r["added"]
                skipped += r["skipped"]
                has_regions = r["has_regions"]
                new, regions_from = False, None  # 그룹은 이미 만들어졌다
            if imgs:
                cols, rows = self.layout_choice()
                ri = service.add_images(self.project_dir, imgs, group, cols=cols, rows=rows,
                                        name=self.img_name.text().strip() or None, new_group=new,
                                        regions_from=regions_from, boxes=boxes)
                if ri.file_dir:
                    added.append(ri.file_dir)
                skipped += ri.skipped
                warnings += ri.warnings
                has_regions = ri.has_regions
                if ri.file_dir:
                    warnings.insert(0, f"이미지 {len(imgs)}장 → 카드 {ri.pages}장 ({ri.file_dir.name})")
        except (FileExistsError, KeyError, ValueError, OSError, RuntimeError) as e:  # pymupdf 오류는 RuntimeError 계열
            if not added:
                show_problem(self, "파일을 넣지 못했습니다", str(e).strip("'"))
                return
            # 앞 파일은 이미 들어갔다: 창을 닫아 목록을 새로 고치고, 못 넣은 것을 알린다
            skipped.append(f"나머지 파일을 넣지 못했습니다: {str(e).strip(chr(39))}")
        self.result_info = {"group": group, "added": added, "skipped": skipped, "has_regions": has_regions,
                            "warnings": warnings}
        if skipped or warnings:
            QMessageBox.information(self, "파일 넣기", "\n".join(warnings + skipped))
        super().accept()


class ExcludePagesDialog(QDialog):
    """파일에서 그룹 형식과 다른 쪽(예: 규칙 카드)을 뺀다."""

    def __init__(self, file_dir: Path, excluded: list[int], reason: str = "", parent=None):
        super().__init__(parent)
        self.file_dir = Path(file_dir)
        self.setWindowTitle(f"뺄 쪽 — {self.file_dir.name}")
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setIconSize(QSize(150, 150))
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setSpacing(6)
        self._pressed_state = None
        for p in range(1, service.page_count(self.file_dir) + 1):
            png, _ = service.page_image(self.file_dir, p, dpi=36)
            pm = QPixmap()
            pm.loadFromData(png, "PNG")
            it = QListWidgetItem(QIcon(pm), f"{p}쪽")
            it.setData(Qt.ItemDataRole.UserRole, p)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if p in excluded else Qt.CheckState.Unchecked)
            self.list.addItem(it)
        self.reason = QLineEdit(reason)
        self.reason.setPlaceholderText("뺀 이유 (예: 규칙 카드라 형식이 다름)")
        # 한 PDF에 형식이 다른 카드가 섞여 있으면: 체크한 쪽을 다른 그룹에서 처리
        self.moved = False
        self.target = QComboBox()
        pdir = self.file_dir.parents[3]
        here = self.file_dir.parent.parent.name
        for g in service.list_groups(pdir):
            if g["dir"].name != here:
                self.target.addItem(g["name"], g["name"])
        self.target.addItem("새 그룹 만들기…", None)
        send = QPushButton("체크한 쪽을 이 그룹으로 보내기")
        send.clicked.connect(self.send_to_group)
        mrow = QHBoxLayout()
        mrow.addWidget(self.target, 1)
        mrow.addWidget(send)
        move_box = QGroupBox("형식이 다른 카드라면")
        mv = QVBoxLayout(move_box)
        mv.addWidget(grey_note("다른 형식의 카드라면 다른 그룹으로 보내 그 그룹의 영역으로 처리합니다."))
        mv.addLayout(mrow)
        self.count = QLabel()
        self.count.setStyleSheet("font-weight: bold")
        self.list.itemPressed.connect(lambda it: setattr(self, "_pressed_state", it.checkState()))
        self.list.itemClicked.connect(self._toggle_on_click)
        self.list.itemChanged.connect(lambda it: self._paint(it))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "체크한 쪽 빼기")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(10)
        lay.addWidget(page_title("뺄 쪽 고르기", 14))
        lay.addWidget(hint_bar("그림이나 쪽 번호를 눌러 뺄 쪽을 체크하세요. 체크한 쪽은 추출·용어집·번역·지우기·한글 넣기에서 모두 빠집니다."))
        lay.addWidget(self.list, 1)
        lay.addWidget(self.count)
        lay.addWidget(self.reason)
        lay.addWidget(move_box)
        lay.addWidget(bb)
        fit_to_screen(self, 900, 640, maximize_below=0)
        for i in range(self.list.count()):
            self._paint(self.list.item(i))
        self._update_count()

    def _toggle_on_click(self, it):
        # 체크 칸을 직접 눌렀으면 보기가 이미 바꿨다. 그림·글자를 눌렀을 때만 우리가 바꾼다.
        if self._pressed_state is not None and it.checkState() == self._pressed_state:
            it.setCheckState(Qt.CheckState.Unchecked if it.checkState() == Qt.CheckState.Checked
                             else Qt.CheckState.Checked)
        self._pressed_state = None

    def _paint(self, it):
        it.setBackground(QColor("#F4B183") if it.checkState() == Qt.CheckState.Checked else QBrush())
        self._update_count()

    def _update_count(self):
        c = self.checked()
        self.count.setText(f"체크한 쪽: {len(c)}쪽" + (f" ({', '.join(f'{p}쪽' for p in c)})" if c else ""))

    def checked(self) -> list[int]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def accept(self):
        service.set_pages(self.file_dir, self.checked(), self.reason.text().strip())
        super().accept()

    def send_to_group(self):
        pages = self.checked()
        if not pages:
            show_error(self, "보낼 쪽이 없습니다", "다른 그룹으로 보낼 쪽을 아직 고르지 않았습니다.",
                       "그림 옆 칸을 체크한 뒤 다시 누르세요.")
            return
        group, new = self.target.currentData(), False
        if group is None:
            group, ok = QInputDialog.getText(self, "새 그룹", "새 그룹 이름 (카드 형식, 예: 정예 카드)")
            group = (group or "").strip()
            if not ok or not group:
                return
            new = True
        try:
            service.move_pages_to_group(self.file_dir, pages, group, new_group=new)
        except (ValueError, FileExistsError) as e:
            show_problem(self, "쪽을 다른 그룹으로 보내지 못했습니다", str(e))
            return
        self.moved = True
        QMessageBox.information(self, "보냄", f"{len(pages)}쪽을 ‘{group}’ 그룹으로 보냈습니다. 이 그룹에서는 빠집니다."
                                + ("\n새 그룹이므로 영역을 지정하세요." if new else ""))
        super().accept()


def open_log_folder(parent=None) -> Path:
    """로그 폴더를 탐색기로 연다 (없으면 만든다). 연 폴더 경로."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    folder = service.log_folder()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        show_error(parent, "로그 폴더를 열지 못했습니다", str(e))
        return folder
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
    return folder


class SettingsDialog(QDialog):
    """데이터 폴더와 기본 엔진."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("설정")
        s = service.get_settings()
        self.dir = QLineEdit(str(service.data_dir()))
        browse = QPushButton("바꾸기…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.dir)
        row.addWidget(browse)
        self.engine = s.engine
        st = service.engine_status()["message"]
        info = QLabel(f"지금: {st}\n바꾸려면 메뉴의 ‘파일 > 로그인 방식…’을 누르세요.")
        info.setWordWrap(True)
        info.setStyleSheet("color: #334E68")
        self.log_path = QLineEdit(str(service.log_folder()))  # 경로는 보이지 않게 두고 버튼 말풍선에 보인다
        self.log_path.setReadOnly(True)
        self.log_path.setVisible(False)
        self.b_log = QPushButton("폴더 열기")
        self.b_log.setToolTip(f"{service.log_folder()}\n문제가 생기면 이 폴더의 파일(또는 도움말 > 로그 묶어서 저장)을 보내 주세요")
        self.b_log.clicked.connect(lambda: open_log_folder(self))
        lrow = QHBoxLayout()
        lrow.addWidget(grey_note("문제가 생기면 ‘도움말 > 로그 묶어서 저장…’으로 만든 파일을 보내 주세요."), 1)
        lrow.addWidget(self.b_log)
        # 작업 끝 소리: 5초 넘게 걸린 작업(추출·지우기·한글 넣기 등)이 끝나면
        self.sound_on = QCheckBox(f"{int(service.DONE_SOUND_MIN_S)}초 넘게 걸린 작업이 끝나면 소리 내기")
        self.sound_on.setChecked(s.done_sound_on)
        self.sound_path = QLineEdit(s.done_sound)
        self.sound_path.setPlaceholderText("비우면 Windows 기본 알림 소리")
        self.b_sound = QPushButton("고르기…")
        self.b_sound.clicked.connect(self._pick_sound)
        self.b_listen = QPushButton("들어 보기")
        self.b_listen.clicked.connect(self._listen)
        srow = QHBoxLayout()
        srow.addWidget(self.sound_path)
        srow.addWidget(self.b_sound)
        srow.addWidget(self.b_listen)
        self.sound_on.toggled.connect(lambda on: [w.setEnabled(on) for w in (self.sound_path, self.b_sound, self.b_listen)])
        self.sound_on.toggled.emit(self.sound_on.isChecked())
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "저장")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)
        lay.addWidget(page_title("설정", 14))
        lay.addWidget(card(section_title("저장 위치"), row,
                           grey_note("게임·프로젝트가 저장되는 폴더입니다. 바꾸면 새 폴더의 프로젝트 목록이 보입니다 "
                                     "(예전 폴더의 자료는 옮기지 않습니다).")))
        lay.addWidget(card(section_title("Claude 연결"), info))
        lay.addWidget(card(section_title("작업이 끝나면"), self.sound_on, srow))
        lay.addWidget(card(section_title("문제가 생겼을 때"), lrow))
        lay.addWidget(self.log_path)
        lay.addWidget(bb)
        self.resize(640, 0)

    def _pick_sound(self):
        f, _ = QFileDialog.getOpenFileName(self, "소리 파일", self.sound_path.text() or "C:/Windows/Media",
                                           "소리 (*.mp3 *.wav)")
        if f:
            self.sound_path.setText(f)

    def _listen(self):
        from app.qt import sound

        path = self.sound_path.text().strip()
        if path and not Path(path).exists():
            show_problem(self, "소리 파일이 없습니다", path, "다른 파일을 고르세요.")
            return
        sound.play(path or service.default_done_sound())

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "데이터 폴더", self.dir.text())
        if d:
            self.dir.setText(d)

    def accept(self):
        d = Path(self.dir.text().strip())
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            show_problem(self, "데이터 폴더를 쓸 수 없습니다", str(e), "다른 폴더를 고르거나, 쓸 수 있는 폴더인지 확인하세요.")
            return
        service.save_settings(d, self.engine)
        service.save_done_sound(self.sound_on.isChecked(), self.sound_path.text().strip())
        super().accept()
