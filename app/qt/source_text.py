"""② 원문 확인 카드 보기의 원문 패널 글 도우미: 확인할 까닭(쉬운 말), 편집 칸에 넣을 글 (문단 나눔 [====])."""

from __future__ import annotations

import re

from app import service
from app.qt.step_common import ICON_REVIEW_LABEL, ICON_TOKEN_RE, as_text, plain_note

UNREAD = "읽지 않음"


def _nonblank(text: str | None) -> int:
    return sum(1 for ln in (text or "").split("\n") if ln.strip())


def needs_attention(c: dict) -> bool:
    """사람이 보고 판단할 영역 (아직 읽지 않은 영역은 뺀다)."""
    return c["label"] != UNREAD and (c["uncertain"] or c["label"] in ("결론 못 냄", ICON_REVIEW_LABEL))


def for_edit(c: dict, text: str) -> str:
    """편집 칸에 넣을 글: 문단마다 다른 글꼴 영역이면 문단 나눔을 [====] 줄로 보인다 (저장된 나눔은 지금 원문과
    줄 수가 같은 글에만. 다른 글(줄 수가 다른 읽은 결과)은 기본 규칙)."""
    if not (c.get("paragraph_mode") and isinstance(text, str)):
        return text
    br = c.get("paragraph_breaks")
    if br is not None and _nonblank(text) != _nonblank(as_text(c.get("source"))):
        br = None
    return service.mark_paragraphs(text, br)


def reason_text(c: dict, differ: bool) -> tuple[str, str]:
    """(이 영역을 확인해야 하는 까닭, 읽은 모델이 남긴 메모) — 쉬운 말로."""
    note = c.get("note") or ""
    if c["label"] == UNREAD:
        return "아직 읽지 않은 영역입니다. 그림을 보고 원문을 직접 적어 확정해도 됩니다.", ""
    if not needs_attention(c):
        return plain_note(note) or "따로 확인할 점은 없습니다. 틀린 곳이 보이면 고쳐서 확정하세요.", ""
    # 엔진 메모 ‘두 모델이 다르게 읽음 (A: … / B: …). 덧붙인 말’에서 두 결과는 표에 있으니 덧붙인 말만
    memo = note.rsplit(").", 1)[1].strip() if note.startswith("두 모델이") and ")." in note else ""
    if c["label"] == ICON_REVIEW_LABEL:  # 아이콘 자리를 고친 영역 (고치기 전 원문은 본 적 없음)
        if "아이콘 고친 뒤" not in note:  # 다시 읽기를 취소했거나 한도·실패로 못 읽음 (메모는 서비스가 적는다)
            return ("아이콘 자리가 바뀌었는데 아직 다시 읽지 못했습니다. "
                    "그림과 비교하거나 ‘이 영역 다시 읽기’로 읽어 보세요."), ""
        if differ:
            return "아이콘을 고친 뒤 두 번 읽은 결과가 다릅니다. 그림과 비교해 맞게 고쳐 확정하세요.", ""
        if "지금 원문과 다름" in note:
            return "아이콘을 고친 뒤 두 번 읽은 결과가 같지만 지금 원문과 다릅니다. 그림과 비교해 확정하세요.", ""
        if "확실하지 않다고 함" in note:
            return "아이콘을 고친 뒤 두 번 읽었지만 확실하지 않다고 했습니다. 그림과 비교해 확정하세요.", ""
        return "아이콘을 고친 뒤 다시 읽은 결과를 그림과 비교해 확정하세요.", ""
    if differ:
        base = "두 번 읽은 결과가 다릅니다. 그림과 비교해 맞게 고쳐 확정하세요."
    elif c["readings"]:
        base = "두 번 읽은 결과는 같지만 정밀 읽기도 확실하지 않다고 했습니다. 그림과 비교해 주세요."
    else:
        base, memo = "읽은 결과가 확실하지 않다고 했습니다. 그림과 비교해 주세요.", note
    return base, plain_note(memo)


def _icon_runs(text: str) -> int:
    """아이콘 덩어리 수: 빈칸만 사이에 둔 아이콘들은 하나로 센다 (세트·합친 아이콘을 모델은 따로 읽는다)."""
    return len(re.findall(r"(?:\[\[[^\]]*\]\][ 	　]*)+", text or ""))


def same_but_icon_layout(reading: str, current: str, human: bool = True) -> bool:
    """다시 읽은 글이 지금 원문과 글자는 같고 아이콘의 자리·줄바꿈·합친 아이콘 수만 다른가.
    이럴 때는 카드 보기에서 사람이 정한 아이콘(손으로 잡은 자리·합치기)이 맞다고 본다
    (예: 위아래 두 아이콘을 모델이 한 줄에 붙여 읽음, 합친 아이콘 넷을 [[?]] 넷으로 읽음)."""
    if not human or not reading or not current or reading == current:
        return False  # 사람이 아이콘을 손대지 않은 영역은 모델이 고친 아이콘 자리도 보여 준다
    if not (ICON_TOKEN_RE.search(reading) or ICON_TOKEN_RE.search(current)):
        return False  # 아이콘이 없는 글: 줄바꿈·빈칸 차이도 보여 준다

    def bare(t: str) -> str:
        return re.sub(r"\s+", "", ICON_TOKEN_RE.sub("", t))
    if bare(reading) != bare(current):
        return False

    def inline(t: str) -> dict[str, tuple[int, int]]:
        """글이 있는 줄의 글자 -> (그 줄의 아이콘 덩어리 수, 아이콘 수)."""
        out: dict[str, tuple[int, int]] = {}
        for ln in t.split("\n"):
            if bare(ln):
                runs, toks = out.get(bare(ln), (0, 0))
                out[bare(ln)] = (runs + _icon_runs(ln), toks + len(ICON_TOKEN_RE.findall(ln)))
        return out
    r_in, c_in = inline(reading), inline(current)
    for k in r_in.keys() & c_in.keys():
        # 두 글에 똑같이 있는 글 줄인데 그 줄의 아이콘이 다르다: 글 줄에 붙은 아이콘이 다른 줄로 갔다
        # - 덩어리 수가 다름 (1쪽: 동전 자리를 아래 아이콘 줄 [[?]]에 저장해 원문 칸이 ‘を得る … [[coin]]’이 됐는데, 다시 읽은
        #   ‘[[?]]を得る’를 감췄다)
        # - 원문 칸 쪽 아이콘이 더 많음 (5쪽: 동전을 ‘아이콘 추가’하니 위 줄에 들어가 ‘ジャンク[[gear]][[coin]]タグ’)
        # 손으로 합친 세트(원문 칸 [[set]] 하나 ↔ 읽은 [[?]] 셋)는 원문 칸 쪽이 적어 걸리지 않는다.
        # 모델이 글 줄 둘을 한 줄로 붙여 읽은 것(5쪽 예전)은 같은 글 줄이 없어 여기 걸리지 않는다
        if r_in[k][0] != c_in[k][0] or c_in[k][1] > r_in[k][1]:
            return False
    n_r, n_c = len(ICON_TOKEN_RE.findall(reading)), len(ICON_TOKEN_RE.findall(current))
    return n_r == n_c or _icon_runs(reading) == _icon_runs(current) or (n_r > n_c and _icon_runs(reading) <= n_c)

