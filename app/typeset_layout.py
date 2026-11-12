"""한글 조판의 순수 계산 부분 (PDF 없이 동작).

- 서식 태그 파싱·검사: ``<b> <i> <cN>`` (PRD 5.7)
- 아이콘 토큰 ``[word]`` 인식 (PRD 5.6)
- 어절 단위 줄바꿈 + 금칙 + 아이콘·조사 묶기 + 긴 어절 글자 단위 끊기 (PRD 5.5)
- 크기 맞추기: 원문 크기 -> 줄바꿈 -> 자간 최대 -5% -> 원문의 80%까지 축소 -> 넘침

폭·높이 측정은 ``Measurer`` 프로토콜로 받는다. 실제 글꼴 메트릭 구현은
``app.typeset.FontMeasurer``에 있고, 테스트는 고정 폭 가짜 측정기를 쓸 수 있다.

하한 규칙 (6pt):
    min_size = max(start * min_ratio, min(floor_pt, start))
    - 시작 크기가 6pt 이상이면 원문의 80%와 6pt 중 큰 값까지만 줄인다.
    - 시작 크기가 이미 6pt 미만(예: 본문 5.5pt)이면 시작 크기 자체가 하한이다.
      즉 더 줄이지 않고, 줄바꿈·자간까지만 조정한 뒤 안 되면 '넘침'.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Protocol

# ---------------------------------------------------------------- 상태 문자열
STATUS_SHRUNK = "글자 축소"
STATUS_OVERFLOW = "넘침"
STATUS_TAG_MISMATCH = "서식 불일치"
STATUS_ICON_ERROR = "아이콘 오류"
STATUS_EMPTY = "빈 영역"
STATUS_ERASE_ONLY = "지우기만"
STATUS_MISSING_GLYPH = "글리프 없음"

# ---------------------------------------------------------------- 금칙 문자
#: 줄 첫머리에 올 수 없는 문자 (닫는 부호, 문장 부호)
NO_LINE_START = set(".,!?;:)]}>%" "、。，．！？；：）］｝〉》」』】〕〙〗’”…‥·～~" "ーゝゞヽヾ")
#: 줄 끝에 올 수 없는 문자 (여는 부호)
NO_LINE_END = set("([{<" "（［｛〈《「『【〔〘〖‘“")

# f1, f2 ...: 문장 단위 글꼴 (한글 넣기 직전에 앱이 붙이는 내부 태그. 엑셀·번역문에는 없다)
TAG_RE = re.compile(r"<(/?)(b|i|c\d+|f\d+)>")
TAG_LIKE_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9]*>")
# 아이콘 자리: [[단어]] (이중 대괄호). 한 겹 [ ]는 카드에 인쇄된 괄호라서 그냥 글자다 (PRD 4장)
ICON_RE = re.compile(r"\[\[([a-z0-9_]+)\]\]")


# ---------------------------------------------------------------- 서식 태그
@dataclass(frozen=True)
class Fmt:
    bold: bool = False
    italic: bool = False
    color_tag: str | None = None  # "c1", "c2" ... 실제 색은 style.tag_colors
    font_tag: str | None = None  # "f1", "f2" ... 실제 글꼴 파일은 style.tag_fonts (문장 단위 글꼴)


@dataclass
class Span:
    text: str
    fmt: Fmt = field(default_factory=Fmt)


def strip_tags(text: str) -> str:
    return TAG_LIKE_RE.sub("", text)


def parse_markup(text: str) -> tuple[list[Span], list[str]]:
    """서식 태그를 해석한다.

    반환: (spans, errors). errors가 있으면 태그를 모두 무시한 기본 서식 span 하나를 돌려준다.
    """
    errors: list[str] = []
    for m in TAG_LIKE_RE.finditer(text):
        if not TAG_RE.fullmatch(m.group(0)):
            errors.append(f"모르는 태그 {m.group(0)}")
    spans: list[Span] = []
    stack: list[str] = []
    font: str | None = None  # 글꼴 태그는 다른 태그와 엇갈려도 된다 (문장 경계가 <b> 안에 있을 수 있음)
    pos = 0
    if not errors:
        for m in TAG_RE.finditer(text):
            if m.start() > pos:
                spans.append(Span(text[pos:m.start()], _fmt_from_stack(stack, font)))
            closing, name = m.group(1) == "/", m.group(2)
            if name.startswith("f"):
                if closing and font != name:
                    errors.append(f"짝이 맞지 않는 닫는 태그 </{name}>")
                    break
                if not closing and font is not None:
                    errors.append(f"글꼴 태그 <{name}> 중첩")
                    break
                font = None if closing else name
                pos = m.end()
                continue
            if closing:
                if not stack or stack[-1] != name:
                    errors.append(f"짝이 맞지 않는 닫는 태그 </{name}>")
                    break
                stack.pop()
            else:
                if name in stack:
                    errors.append(f"같은 태그 <{name}> 중첩")
                    break
                stack.append(name)
            pos = m.end()
        else:
            if pos < len(text):
                spans.append(Span(text[pos:], _fmt_from_stack(stack, font)))
            if stack or font:
                errors.append("닫히지 않은 태그 " + ", ".join(f"<{s}>" for s in stack + ([font] if font else [])))
    if errors:
        return [Span(strip_tags(text), Fmt())], errors
    return [s for s in spans if s.text], errors


def _fmt_from_stack(stack: list[str], font: str | None = None) -> Fmt:
    colors = [s for s in stack if s.startswith("c")]
    return Fmt(bold="b" in stack, italic="i" in stack, color_tag=colors[-1] if colors else None, font_tag=font)


def tag_counts(text: str) -> Counter:
    """원문과 비교할 태그 수 (앱이 붙이는 글꼴 태그 fN은 세지 않는다)."""
    return Counter(m.group(2) for m in TAG_RE.finditer(text) if m.group(1) == "" and not m.group(2).startswith("f"))


# ---------------------------------------------------------------- 문장 나누기 (문장 단위 글꼴)
SENTENCE_END = set(".!?。！？…")
SENTENCE_CLOSE = set("\"'”’」』)）]")


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """번역문(태그·아이콘 포함)을 문장으로 나눈 (시작, 끝) 위치. 끝은 포함하지 않는다.
    문장 끝: 마침표·물음표·느낌표·말줄임표(뒤의 닫는 따옴표·괄호까지) 다음에 빈칸·줄바꿈이 오거나 글이 끝날 때,
    또는 빈 줄. 태그와 [[아이콘]] 안은 나누지 않는다."""
    spans: list[tuple[int, int]] = []
    n = len(text)
    start = None
    i = 0
    last_end = 0  # 마지막으로 본 글자(공백 아닌) 다음 위치

    def close(at):
        nonlocal start
        if start is not None and at > start:
            spans.append((start, at))
        start = None

    while i < n:
        m = TAG_LIKE_RE.match(text, i) or ICON_RE.match(text, i)
        if m:
            if start is None and not m.group(0).startswith("</"):
                start = i
            i = m.end()
            last_end = i
            continue
        ch = text[i]
        if ch == "\n" and text[i + 1:i + 2] == "\n":  # 빈 줄
            close(last_end)
            i += 2
            continue
        if ch.isspace():
            i += 1
            continue
        if start is None:
            start = i
        i += 1
        last_end = i
        if ch in SENTENCE_END:
            while i < n and (text[i] in SENTENCE_END or text[i] in SENTENCE_CLOSE):
                i += 1
            while i < n and (mt := TAG_LIKE_RE.match(text, i)) and mt.group(0).startswith("</"):
                i = mt.end()  # 문장 끝에서 닫히는 태그는 그 문장에
            last_end = i
            if i >= n or text[i].isspace():
                close(i)
    close(last_end)
    return spans


def sentences(text: str) -> list[str]:
    """문장마다 태그를 뺀 글 (화면 표시용)."""
    return [re.sub(r"\s+", " ", strip_tags(text[a:b])).strip() for a, b in sentence_spans(text or "")]


PARA_END = set(".!?。！？…」』”’)）\"'")  # 줄이 이것으로 끝나면 그 줄에서 문단이 끝난다
DASH_START_RE = re.compile(r"(?:(?<=\s)|^)[—―–]{1,2}")  # ‘——탐험가’ 같은 말한 사람 (새 문단)
HEADING_RE = re.compile(r"[:：]")


def _visible(s: str) -> str:
    """태그를 빼고 아이콘은 ■ 한 글자로 본 글 (문단 나누기 판단용)."""
    return strip_tags(ICON_RE.sub("■", s)).strip()


def paragraph_spans(text: str) -> list[tuple[int, int]]:
    """번역문(태그·아이콘 포함)을 문단으로 나눈 (시작, 끝) 위치 (끝은 포함하지 않음).
    카드 글의 줄바꿈은 대개 원문 줄 폭 때문이라, 줄이 문장 끝(. ! ? 。 」 등)으로 끝나거나 짧은 머리 줄(‘공포: 1’)일 때,
    빈 줄이 있을 때, 그리고 ‘——이름’(말한 사람) 앞에서만 문단을 나눈다. 문장 중간에서 바뀐 줄은 같은 문단."""
    text = text or ""
    pieces: list[tuple[int, int]] = []  # 줄(또는 줄 안 ‘——’ 앞뒤 조각)
    pos = 0
    blank_before: set[int] = set()
    for line in text.split("\n"):
        a, b = pos, pos + len(line)
        pos = b + 1
        if not line.strip():
            blank_before.add(len(pieces))
            continue
        cuts = [m.start() for m in DASH_START_RE.finditer(line) if line[:m.start()].strip()]
        starts = [0] + cuts
        for i, s in enumerate(starts):
            e = cuts[i] if i < len(cuts) else len(line)
            seg = line[s:e]
            lead = len(seg) - len(seg.lstrip())
            trail = len(seg.rstrip())
            if seg.strip():
                pieces.append((a + s + lead, a + s + trail))
    if not pieces:
        return []
    paras: list[list[int]] = [list(pieces[0])]
    for k in range(1, len(pieces)):
        prev_vis = _visible(text[pieces[k - 1][0]:pieces[k - 1][1]])
        cur_vis = _visible(text[pieces[k][0]:pieces[k][1]])
        new = (k in blank_before
               or (prev_vis and prev_vis[-1] in PARA_END)
               or (len(prev_vis) <= 12 and bool(HEADING_RE.search(prev_vis)))
               or bool(DASH_START_RE.match(cur_vis)) or bool(DASH_START_RE.match(prev_vis)))
        if new:
            paras.append(list(pieces[k]))
        else:
            paras[-1][1] = pieces[k][1]
    return [(a, b) for a, b in paras]


def paragraphs(text: str) -> list[str]:
    """문단마다 태그를 뺀 글 (화면 표시용)."""
    return [re.sub(r"\s+", " ", strip_tags(text[a:b])).strip() for a, b in paragraph_spans(text or "")]


def wrap_sentence_fonts(text: str, fonts: dict) -> tuple[str, dict]:
    """문단마다 고른 글꼴을 내부 태그 <fN>…</fN>로 감싼다. fonts: {문단 번호(0부터): 글꼴 파일}.
    (이름은 예전 ‘문장마다’ 그대로. 나누는 단위는 문단: paragraph_spans)
    줄바꿈에서는 태그를 닫았다 다시 연다 (줄마다 따로 배치하는 그림 카드에서도 짝이 맞게).
    돌려줌: (태그를 붙인 글, {"f1": 글꼴 파일, ...})."""
    keys: dict[str, str] = {}
    out, pos = [], 0
    for idx, (a, b) in enumerate(paragraph_spans(text or "")):
        f = fonts.get(idx) if idx in fonts else fonts.get(str(idx))
        if not f:
            continue
        key = keys.setdefault(f, f"f{len(keys) + 1}")
        body = text[a:b].replace("\n", f"</{key}>\n<{key}>")
        out.append(text[pos:a] + f"<{key}>" + body + f"</{key}>")
        pos = b
    out.append(text[pos:])
    return "".join(out), {v: k for k, v in keys.items()}


def close_tags_per_line(text: str) -> str:
    """줄바꿈에서 열린 서식 태그를 닫았다가 다음 줄에서 다시 연다.

    그림 카드는 번역을 줄마다 원문 줄 자리에 따로 넣으므로(줄마다 태그 짝을 검사), 사람이 넣은 줄바꿈이
    ``<b>은행에서\\n금화를 받고</b>``처럼 태그 안에 있으면 두 줄 모두 짝이 깨져 ‘서식 불일치’가 되고
    굵게가 사라진다. 이렇게 바꾸면 ``<b>은행에서</b>\\n<b>금화를 받고</b>``가 되어 줄마다 짝이 맞는다.
    빈 줄은 그대로 둔다. 태그 짝이 원래 깨진 글은 바꾸지 않는다(그대로 ‘서식 불일치’로 알린다)."""
    if "\n" not in (text or ""):
        return text
    _, errors = parse_markup(text)
    if errors:
        return text
    out: list[str] = []
    opened: list[str] = []  # 연 순서 (글꼴 태그 fN은 b·i·cN과 엇갈릴 수 있다: 닫을 때는 거꾸로)
    for line in text.split("\n"):
        if not line.strip():
            out.append(line)
            continue
        head = "".join(f"<{t}>" for t in opened)
        for m in TAG_RE.finditer(line):
            name = m.group(2)
            if m.group(1) == "/":
                if name in opened:
                    opened.remove(name)
            else:
                opened.append(name)
        tail = "".join(f"</{t}>" for t in reversed(opened))
        out.append(head + line + tail)
    return "\n".join(out)


_REOPEN_RE = re.compile(r"</(b|i|c\d+|f\d+)>([ \t]*\n\s*)<\1>")


def _span_counts(text: str) -> Counter:
    """줄바꿈만 사이에 둔 같은 태그(``<b>A</b>\\n<b>B</b>``)는 한 덩어리로 센 태그 수.
    스캔 원문은 줄마다 태그를 따로 달고 번역은 여러 줄을 한 번에 감싸는 일이 많아(``<b>A\\nB</b>``),
    그대로 세면 개수가 달라 ‘서식 불일치’가 잘못 난다. 두 쪽 모두 줄마다 닫았다 연 뒤 다시 이어 붙여 센다."""
    s = close_tags_per_line(text or "")
    while True:
        t = _REOPEN_RE.sub(r"\2", s)
        if t == s:
            return tag_counts(s)
        s = t


def check_tags(target: str, source: str | None = None) -> list[str]:
    """번역문의 태그 짝 검사 + (원문이 있으면) 종류·개수 비교. 오류 목록을 돌려준다."""
    _, errors = parse_markup(target)
    if source is not None and not errors:
        a, b = _span_counts(source), _span_counts(target)
        if a != b:
            errors.append(f"태그 종류·개수가 원문과 다름 (원문 {dict(a)}, 번역 {dict(b)})")
    return errors


# ---------------------------------------------------------------- 원자 (글자·공백·아이콘·줄바꿈)
@dataclass(frozen=True)
class Atom:
    kind: str  # "char" | "space" | "icon" | "newline"
    text: str  # 글자 하나, 공백, 아이콘 이름
    fmt: Fmt = Fmt()

    @property
    def display(self) -> str:
        return f"[[{self.text}]]" if self.kind == "icon" else self.text


def to_atoms(spans: list[Span]) -> list[Atom]:
    atoms: list[Atom] = []
    for sp in spans:
        pos = 0
        for m in ICON_RE.finditer(sp.text):
            atoms.extend(_char_atoms(sp.text[pos:m.start()], sp.fmt))
            atoms.append(Atom("icon", m.group(1), sp.fmt))
            pos = m.end()
        atoms.extend(_char_atoms(sp.text[pos:], sp.fmt))
    return atoms


# 예전 규칙(아이콘이 [단어]이던 때)으로 쓴 전각 ［］도 받아 준다. 전각 괄호는 양옆이 넓게 비어
# '［1층］ 에'처럼 벌어지므로 보통 괄호로 그린다. 지금 규칙은 카드 괄호를 보통 [ ]로 쓴다.
DRAW_AS = {"［": "[", "］": "]"}


def _char_atoms(s: str, fmt: Fmt) -> list[Atom]:
    out = []
    s = "".join(DRAW_AS.get(c, c) for c in s)
    for ch in s.replace("\r\n", "\n").replace("\r", "\n"):
        if ch == "\n":
            out.append(Atom("newline", "\n", fmt))
        elif ch in (" ", "\t", "　"):
            out.append(Atom("space", " ", fmt))
        else:
            out.append(Atom("char", ch, fmt))
    return out


def icon_names(text: str) -> list[str]:
    return ICON_RE.findall(text)


# ---------------------------------------------------------------- 측정기
class Measurer(Protocol):
    def advance(self, atom: Atom, size: float) -> float:
        """atom 하나의 진행 폭(pt). 아이콘은 글자 크기에 따른 폭."""

    def ink_extent(self, atoms: list[Atom]) -> tuple[float, float]:
        """글자 크기 1pt 기준 (기준선 위 잉크 높이, 기준선 아래 잉크 깊이)."""


# ---------------------------------------------------------------- 줄
@dataclass
class Line:
    atoms: list[Atom]
    width: float = 0.0
    overflow: bool = False  # 끊을 수 없는 덩어리가 폭보다 넓음

    @property
    def text(self) -> str:
        return "".join(a.display for a in self.atoms)


def _width(atoms: list[Atom], size: float, tracking: float, m: Measurer) -> float:
    if not atoms:
        return 0.0
    return sum(m.advance(a, size) for a in atoms) + tracking * size * (len(atoms) - 1)


def _trim(atoms: list[Atom]) -> list[Atom]:
    i, j = 0, len(atoms)
    while i < j and atoms[i].kind == "space":
        i += 1
    while j > i and atoms[j - 1].kind == "space":
        j -= 1
    return atoms[i:j]


def split_paragraphs(atoms: list[Atom]) -> list[list[Atom]]:
    paras: list[list[Atom]] = [[]]
    for a in atoms:
        if a.kind == "newline":
            paras.append([])
        else:
            paras[-1].append(a)
    return paras


def _words(para: list[Atom]) -> list[list[Atom]]:
    """공백으로 나눈 어절. 금칙(줄 시작 금지·줄 끝 금지)이면 앞뒤 어절과 공백째 묶는다."""
    words: list[list[Atom]] = []
    cur: list[Atom] = []
    for a in para:
        if a.kind == "space":
            if cur:
                words.append(cur)
                cur = []
        else:
            cur.append(a)
    if cur:
        words.append(cur)
    units: list[list[Atom]] = []
    for w in words:
        if units and (
            (w[0].kind == "char" and w[0].text in NO_LINE_START)
            or (units[-1][-1].kind == "char" and units[-1][-1].text in NO_LINE_END)
        ):
            units[-1] = units[-1] + [Atom("space", " ", units[-1][-1].fmt)] + w
        else:
            units.append(w)
    return units


def _can_break_inside(unit: list[Atom], k: int) -> bool:
    """긴 덩어리를 글자 단위로 끊을 때 unit[k-1] | unit[k] 사이를 끊을 수 있는가.

    공백을 건너뛴 앞뒤 글자로 금칙을 본다 (공백 뒤 '」'도 줄 처음에 오지 않게).
    """
    i = k - 1
    while i >= 0 and unit[i].kind == "space":
        i -= 1
    j = k
    while j < len(unit) and unit[j].kind == "space":
        j += 1
    if i < 0 or j >= len(unit):
        return False
    prev, nxt = unit[i], unit[j]
    if nxt.kind == "char" and nxt.text in NO_LINE_START:
        return False
    if prev.kind == "char" and prev.text in NO_LINE_END:
        return False
    if (i == k - 1 and j == k) and (prev.kind == "icon" or nxt.kind == "icon"):
        return False  # 붙어 있는 [coin]을, 금화[coin] 묶기
    return True


def _split_unit(unit: list[Atom], avail: float, size: float, tracking: float, m: Measurer, force: bool):
    """unit 앞쪽에서 avail 안에 드는 가장 긴 조각을 끊는다. 없으면 (force일 때) 가장 짧은 조각."""
    best = None
    first = None
    for k in range(1, len(unit)):
        if not _can_break_inside(unit, k):
            continue
        if first is None:
            first = k
        if _width(_trim(unit[:k]), size, tracking, m) <= avail + 1e-6:
            best = k
        else:
            break
    if best is None:
        if not force:
            return None
        best = first if first is not None else len(unit)
    return unit[:best], _trim(unit[best:])


def wrap_paragraph(para: list[Atom], max_w: float, size: float, tracking: float, m: Measurer) -> list[Line]:
    units = _words(para)
    lines: list[Line] = []
    cur: list[Atom] = []
    queue = list(units)
    while queue:
        u = queue.pop(0)
        cand = cur + ([Atom("space", " ", cur[-1].fmt)] if cur else []) + u
        if _width(cand, size, tracking, m) <= max_w + 1e-6:
            cur = cand
            continue
        if cur:
            lines.append(Line(cur))
            cur = []
            queue.insert(0, u)
            continue
        # 빈 줄에도 안 들어가는 긴 어절 -> 글자 단위로 끊기
        head, tail = _split_unit(u, max_w, size, tracking, m, force=True)
        lines.append(Line(_trim(head)))
        if tail:
            queue.insert(0, tail)
    if cur or not lines:
        lines.append(Line(cur))
    for ln in lines:
        ln.atoms = _trim(ln.atoms)
        ln.width = _width(ln.atoms, size, tracking, m)
        ln.overflow = ln.width > max_w + 1e-6
    return lines


# 문장이 끝난 줄: 마침표류·닫는 괄호/따옴표·콜론, 또는 ‘…다’
_LINE_END = re.compile(r"(?:[.!?。．！？…」』”’)）\]］:：]|다)\s*$")
_WORDY_CH = re.compile(r"[가-힣A-Za-z0-9]")
_TAG_OR_ICON = re.compile(r"</?[a-z0-9]+>|\[\[[^\]]*\]\]")
_TRAILING_TAGS = re.compile(r"(?:\s*</?[a-z0-9]+>)+\s*$")


def soft_break(prev: str, nxt: str) -> bool:
    """두 줄이 한 문장으로 이어지는가: 앞 줄이 문장 중간에서 끝났고 두 줄 모두 글자가 있는 줄
    (아이콘·기호만 있는 줄, 아이콘으로 끝난 줄은 따로 둔다). 태그·[[아이콘]]이 든 글이어도 된다.
    줄 끝의 서식 태그(close_tags_per_line이 붙인 ``</b>`` 등)는 빼고 끝 글자를 본다."""
    p, n = _TAG_OR_ICON.sub("", prev).strip(), _TAG_OR_ICON.sub("", nxt).strip()
    if not (_WORDY_CH.search(p) and _WORDY_CH.search(n)):
        return False
    end = _TRAILING_TAGS.sub("", prev).strip()
    return not _LINE_END.search(end) and not end.endswith("]]")


def two_line_sentences(lines: list[str]) -> list[int]:
    """딱 두 줄로 된 문장의 첫 줄 번호들 (앞 줄은 문장이 끝났고, 둘째 줄에서 문장이 끝남)."""
    n = len(lines)
    return [i for i in range(n - 1)
            if soft_break(lines[i], lines[i + 1])
            and not (i > 0 and soft_break(lines[i - 1], lines[i]))
            and not (i + 2 < n and soft_break(lines[i + 1], lines[i + 2]))]


def join_lines_at(lines: list[str], firsts: list[int]) -> list[str]:
    """firsts의 줄을 다음 줄과 빈칸으로 잇는다."""
    out, skip = [], set(firsts)
    i = 0
    while i < len(lines):
        if i in skip and i + 1 < len(lines):
            out.append(lines[i].rstrip() + " " + lines[i + 1].lstrip())
            i += 2
        else:
            out.append(lines[i])
            i += 1
    return out


def wrap_text(atoms: list[Atom], max_w: float, size: float, tracking: float, m: Measurer,
              respect_manual: bool = True) -> list[Line]:
    """사람이 넣은 줄바꿈이 있으면 그 줄을 그대로 쓰고 자동 줄바꿈하지 않는다 (PRD 4장)."""
    paras = split_paragraphs(atoms)
    lines: list[Line] = []
    if respect_manual and len(paras) > 1:
        for p in paras:
            p = _trim(p)
            w = _width(p, size, tracking, m)
            lines.append(Line(p, w, w > max_w + 1e-6))
        return lines
    for p in paras:
        lines.extend(wrap_paragraph(p, max_w, size, tracking, m))
    return lines


# ---------------------------------------------------------------- 크기 맞추기
@dataclass
class FitParams:
    box_w: float
    box_h: float
    start_size: float  # 원문 크기(또는 F28 잉크 보정 크기)
    fixed_size: float | None = None  # 엑셀 I열(글자 크기)
    min_ratio: float = 0.8
    floor_pt: float | None = 6.0  # None이면 pt 하한 없이 원문 크기(잉크 높이 보정)의 min_ratio만 쓴다
    max_tracking: float = 0.05  # 최대 -5% (base_tracking에서 더 좁히는 양)
    base_tracking: float = 0.0  # 영역 기본 자간 (글자 크기 대비, 음수면 좁힘). 글꼴별 보정용
    tracking_step: float = 0.01
    size_step: float = 0.05
    line_pitch: float | None = None  # start_size에서의 줄 간격(pt). None이면 1.2 x 크기
    respect_manual: bool = True
    # 사람이 넣은 줄바꿈을 지킬 때도, 딱 두 줄로 된 문장이 같은 크기로 한 줄에 들어가면 한 줄로 넣는다
    join_two: bool = True

    def min_size(self) -> float:
        return min_size_rule(self.start_size, self.min_ratio, self.floor_pt)

    def pitch(self, size: float) -> float:
        if self.line_pitch:
            return self.line_pitch * size / self.start_size
        return 1.2 * size


def min_size_rule(start: float, min_ratio: float = 0.8, floor_pt: float | None = 6.0) -> float:
    """하한.

    - floor_pt=None (원문 글자 높이 기준, 기본): 시작 크기의 80%. 시작 크기가 원문 잉크 높이로
      정해지므로, 한글 글자 높이가 원문 글자 높이의 80% 아래로 내려가지 않는다는 뜻이다.
    - floor_pt 숫자 (pt 기준): 원문의 80%와 floor_pt 중 큰 값. 단 시작 크기가 floor_pt 미만이면 시작 크기.
    """
    if floor_pt is None:
        return start * min_ratio
    return max(start * min_ratio, min(floor_pt, start))


@dataclass
class FitResult:
    size: float
    tracking: float  # 0 ~ -0.05 (글자 크기 대비)
    lines: list[Line]
    status: list[str]
    start_size: float
    min_size: float
    ink_top: float  # 1pt 기준
    ink_bottom: float
    overflow_text: str = ""

    @property
    def line_texts(self) -> list[str]:
        return [ln.text for ln in self.lines]

    def block_height(self, pitch: float) -> float:
        n = len(self.lines)
        return (n - 1) * pitch + (self.ink_top + self.ink_bottom) * self.size if n else 0.0


def _fits(lines: list[Line], size: float, p: FitParams, top: float, bottom: float) -> bool:
    if any(ln.overflow for ln in lines):
        return False
    h = (len(lines) - 1) * p.pitch(size) + (top + bottom) * size
    return h <= p.box_h + 1e-6


def _size_ladder(p: FitParams) -> list[float]:
    if p.fixed_size:
        return [p.fixed_size]
    lo = p.min_size()
    sizes = [p.start_size]
    s = p.start_size
    while True:
        s = round(s - p.size_step, 4)
        if s < lo - 1e-9:
            break
        sizes.append(s)
    if sizes[-1] > lo + 1e-9:
        sizes.append(lo)
    return sizes


def fit_text(atoms: list[Atom], p: FitParams, m: Measurer) -> FitResult:
    res = _fit_text(atoms, p, m)
    if not (p.respect_manual and p.join_two):
        return res
    paras = split_paragraphs(atoms)
    if len(paras) < 2:
        return res
    texts = ["".join(a.display for a in q) for q in paras]
    joinable = []
    for i in two_line_sentences(texts):  # 두 줄 문장: 이으면 지금 크기로 한 줄 폭 안에 드는 것만
        one = _trim(paras[i]) + [Atom("space", " ")] + _trim(paras[i + 1])
        if _width(one, res.size, res.tracking, m) <= p.box_w + 1e-6:
            joinable.append(i)
    if not joinable:
        return res
    nls = [a for a in atoms if a.kind == "newline"]
    alt_atoms: list[Atom] = _trim(paras[0])
    for i in range(1, len(paras)):
        alt_atoms.append(Atom("space", " ", nls[i - 1].fmt) if (i - 1) in joinable else nls[i - 1])
        alt_atoms.extend(_trim(paras[i]))
    alt = _fit_text(alt_atoms, p, m)
    if STATUS_OVERFLOW not in alt.status and alt.size >= res.size - 1e-9:
        return alt
    return res


def _fit_text(atoms: list[Atom], p: FitParams, m: Measurer) -> FitResult:
    top, bottom = m.ink_extent([a for a in atoms if a.kind in ("char", "icon")])
    n_track = int(round(p.max_tracking / p.tracking_step))
    trackings = [p.base_tracking - i * p.tracking_step for i in range(n_track + 1)]
    sizes = _size_ladder(p)
    start = p.fixed_size or p.start_size
    lo = sizes[-1]
    for size in sizes:
        for t in trackings:
            lines = wrap_text(atoms, p.box_w, size, t, m, p.respect_manual)
            if _fits(lines, size, p, top, bottom):
                status = [STATUS_SHRUNK] if size < start - 1e-9 else []
                return FitResult(size, t, lines, status, start, lo, top, bottom)
    # 넘침: 가장 작은 크기·가장 좁은 자간으로, 영역 안에 드는 줄·글자만 남긴다
    size, t = lo, trackings[-1]
    lines = wrap_text(atoms, p.box_w, size, t, m, p.respect_manual)
    kept: list[Line] = []
    dropped: list[str] = []
    for ln in lines:
        if _fits(kept + [replace(ln, overflow=False)], size, p, top, bottom):
            if ln.overflow:
                keep: list[Atom] = []
                for i, a in enumerate(ln.atoms):
                    if _width(keep + [a], size, t, m) > p.box_w + 1e-6:
                        dropped.append("".join(x.display for x in ln.atoms[i:]))
                        break
                    keep.append(a)
                ln = Line(keep, _width(keep, size, t, m), False)
            kept.append(ln)
        else:
            dropped.append(ln.text)
    status = ([STATUS_SHRUNK] if size < start - 1e-9 else []) + [STATUS_OVERFLOW]
    return FitResult(size, t, kept, status, start, lo, top, bottom, overflow_text="\n".join(dropped))
