"""2차 화면 공용: 상태·라벨 문구, 원문 글꼴, 아이콘 토큰·차이 표시, 그림 칸, 묻기·실패 창."""

from __future__ import annotations

import html as _html
import re
from difflib import SequenceMatcher

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont, QFontDatabase, QPixmap
from PySide6.QtWidgets import QLabel, QMessageBox, QSizePolicy
from app.qt.dialogs import group_issue_lines, show_failure


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
    ia = [i for i, u in enumerate(a) if ICON_TOKEN_RE.fullmatch(u)]
    jb = [j for j, u in enumerate(b) if ICON_TOKEN_RE.fullmatch(u)]
    if ia and len(ia) == len(jb):
        # 아이콘 수가 같으면 순서대로 짝짓는다 (옆 글자·빈칸이 조금 달라도: ‘2 [[?]]’ ↔ ‘2[[crate]]’,
        # ‘[[?]]工房 / ’ ↔ ‘[[workshop]]/’ — 글자 단위로 맞추면 이런 곳은 이름을 못 붙였다)
        for i, j in zip(ia, jb):
            n = b[j]
            if a[i] == UNKNOWN_ICON and n != UNKNOWN_ICON and (known is None or n[2:-2] in known):
                out[i] = n
        return "".join(out)
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
    """정밀 읽기 결과의 자리. ‘이 칸 다시 읽기’로 정밀 읽기를 한 적이 있으면 가장 최근 것
    (정밀 읽기가 없으면 마지막 = 나중에 다시 읽은 결과, 읽은 결과가 없으면 None)."""
    if not readings:
        return None
    precise = [i for i, rd in enumerate(readings) if reader_name(rd["model"]) == PRECISE]
    reread = [i for i in precise if readings[i].get("reread")]
    return reread[-1] if reread else precise[0] if precise else len(readings) - 1


REREAD_MODELS = [("opus", "정밀 읽기 (Opus)"), ("sonnet", "보통 읽기 (Sonnet)")]  # ‘이 칸 다시 읽기’ 모델
REREAD_TAG = " (다시 읽기)"
KEEP_LABEL = "아이콘만 있음 · 원본 그대로"  # 원문 확인·번역 검수·결과 보기의 같은 버튼
KEEP_UNDO_LABEL = "원본 그대로 두는 중 · 되돌리기"


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
    box.setDetailedText(group_issue_lines(items))
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def _fail(parent, msg: str, title: str = "작업을 끝내지 못했습니다"):
    """작업 스레드에서 예외가 난 경우: 왜 → 무엇을 하면 되는지, 원문은 ‘자세히 보기’에."""
    show_failure(parent, title, msg)


def _fail_as(parent, title: str):
    return lambda m: _fail(parent, m, title)
