"""아이콘 찾기·묶기·사전 (PRD 5.6).

추출 모델은 문장 속 그림(아이콘)을 ``[[?]]``로 적는다. 여기서는

1. ``scan``: 모든 파일의 원문에서 ``[[?]]``를 찾아, 그 줄의 잉크에서 아이콘 위치를 찾고 배경을 투명하게 잘라 낸다.
   게임 아이콘 사전에 같은 모양이 있으면 바로 ``[[이름]]``으로 바꾼다(다음 프로젝트에서 재사용).
   나머지는 같은 모양끼리 묶어 ``아이콘_검증.xlsx``로 보여 준다(모델이 이름·뜻을 제안).
2. ``import_review``: 사용자가 이름을 확정하면 사전(``games/<게임>/icons/``)에 그림·뜻·한국어 이름을 저장하고
   원문의 ``[[?]]``를 ``[[이름]]``으로 바꾼다. ‘아이콘 아님’은 원문에서 뺀다. 같은 이름을 쓴 묶음은 하나로 합친다.

화면 코드를 모른다 (PySide6 전환 대비). 위치는 영역을 바로 세운 이미지 좌표에서만 다루므로
180도 돌린 영역도 같은 방식으로 처리되고, 저장되는 아이콘 그림은 언제나 바로 선 모양이다.
"""

from __future__ import annotations

import json
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

PLACEHOLDER = "[[?]]"
PLACEHOLDER_RE = re.compile(r"\[\[\?\]\]")
TOKEN_RE = re.compile(r"\[\[([^\[\]]*)\]\]")
NAME_RE = re.compile(r"^[a-z0-9_]+$")
CROP_DPI = 600
SAME_SHAPE = 0.80  # 알파 모양 상관계수가 이 이상이면 같은 아이콘 후보
SAME_GRAY = 0.82  # 밝기 무늬 상관이 이 이상이면 같은 아이콘 (같은 타일 2: 0.83~0.91, 타일 1·2: 0.78 이하)
SAME_INNER = 0.82  # 가운데 내용 상관 (같은 아이콘 0.86 이상, 동전 3·5 0.78, 상자 안 동전·병 0.37)
SAME_DETAIL = 0.90  # 그리고 64칸으로 자세히 본 상관계수도 이 이상 (숫자만 다른 타일 1·2를 가른다)
MAX_COLOR_DIST = 80.0  # 평균 색 거리(0~441)가 이보다 크면 다른 아이콘 (빨간 하트·파란 하트)
DEFAULT_SCALE = 1.1
NOT_ICON = "아이콘 아님"


# ---------------------------------------------------------------- 사전
def icons_dir(info) -> Path:
    return info.game_dir / "icons"


def dictionary_path(info) -> Path:
    return icons_dir(info) / "icons.json"


def load_dictionary(info) -> dict:
    """{"icons": {이름: {"file", "meaning", "ko", "scale", "valign", ...}}}"""
    p = dictionary_path(info)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"game": info.game, "icons": {}}


def save_dictionary(info, data: dict) -> None:
    p = dictionary_path(info)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def style_for_compose(file_dir: Path) -> dict:
    """합성 때 영역 서식에 넣을 아이콘 설정 (게임 프로젝트 파일이 아니면 빈 dict)."""
    from app.workspace import project_of

    info = project_of(file_dir)
    if info is None:
        return {}
    d = load_dictionary(info)
    return {"icons_dir": str(icons_dir(info)),
            "icon_scales": {n: float(e.get("scale", DEFAULT_SCALE)) for n, e in d["icons"].items()}}


# ---------------------------------------------------------------- 이미지
def region_image(page, region) -> np.ndarray:
    """영역을 바로 세운 RGB 이미지 (CROP_DPI)."""
    import pymupdf

    from app.regions import ratio_to_pt

    rect = pymupdf.Rect(ratio_to_pt(region.rect, tuple(page.rect))) & page.rect
    rot = int((region.style or {}).get("rotate", 0)) % 360
    pm = page.get_pixmap(matrix=pymupdf.Matrix(CROP_DPI / 72, CROP_DPI / 72).prerotate(rot), clip=rect, alpha=False)
    return np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[:, :, :3].copy()


def _ink(img: np.ndarray, em_px: float) -> np.ndarray:
    """글자·아이콘 잉크: 주변 종이보다 어두운 픽셀, 또는 채도가 높고 조금이라도 어두운 픽셀(색 아이콘)."""
    from app.erase import dark_ink

    bg_px = int(max(31, em_px * 2.2)) | 1  # 속이 찬 아이콘보다 커야 그 안까지 잉크로 잡힌다
    dark = dark_ink(img, 50.0, bg_px)
    f = img.astype(np.float32) / 255.0
    mx, mn = f.max(axis=2), f.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1e-6)
    return dark | ((sat > 0.35) & dark_ink(img, 20.0, bg_px))


def _text_components(ink: np.ndarray) -> tuple[list[tuple[int, int, int, int, int]], list[tuple[int, int]]]:
    """(글자·아이콘 덩어리 [(x, y, w, h, 넓이)], 구분선이 있는 높이 [(y0, y1)]).
    영역 가장자리에 닿은 덩어리는 글자 크기일 때만 남기고(카드 테두리·그림 조각 제외), 얇고 긴 가로선은
    구분선으로 따로 돌려준다 (그 높이의 장식, 예: 가운데 ◆도 줄로 세지 않도록)."""
    import cv2

    H, W = ink.shape
    n, _lab, st, _c = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    raw = [tuple(int(v) for v in st[c]) for c in range(1, n)]
    rules, inner, edge = [], [], []
    for x, y, w, h, a in raw:
        if a < 6:
            continue
        # 배경 그림의 가는 선(집·나무 윤곽 등): 크기에 비해 잉크가 아주 적다 (글자 획은 이보다 차 있다)
        if max(w, h) >= 24 and a < 0.08 * w * h:
            continue
        if w > W * 0.15 and h < w * 0.12:
            rules.append((y, y + h))
            continue
        if y <= 0 or y + h >= H:
            continue  # 위·아래 가장자리 (카드 테두리·그림 조각)
        (edge if (x <= 0 or x + w >= W) else inner).append((x, y, w, h, a))
    hs = sorted(c[3] for c in inner if c[4] >= 30) or [H // 6]
    med = hs[len(hs) // 2]
    keep = inner + [c for c in edge if c[3] <= med * 1.3 and c[2] <= med * 2]
    return keep, rules


def _bands_from(comps: list[tuple[int, int, int, int, int]],
                rules: list[tuple[int, int]] = ()) -> list[tuple[int, int]]:
    """덩어리들을 세로로 겹치는 것끼리 묶어 줄 띠 (y0, y1)로. 구분선 높이에 걸친 띠는 뺀다."""
    if not comps:
        return []
    hs = sorted(c[3] for c in comps)
    med = hs[len(hs) // 2]
    main = sorted((c for c in comps if c[3] >= med * 0.5), key=lambda c: c[1])  # 탁점·점은 줄을 만들지 않는다
    bands: list[list[int]] = []
    for x, y, w, h, _a in main:
        if bands and y < bands[-1][1] - min(h, bands[-1][1] - bands[-1][0]) * 0.3:
            bands[-1][1] = max(bands[-1][1], y + h)
        else:
            bands.append([y, y + h])
    bands = [b for b in bands if not any(r0 < b[1] and r1 > b[0] for r0, r1 in rules)]
    if not bands:
        return []
    # 잉크가 드문드문한 띠(배경 그림 조각만 모인 줄)는 글자 줄이 아니다
    dens = []
    for y0, y1 in bands:
        cs = [c for c in comps if y0 <= c[1] + c[3] / 2 <= y1]
        x0, x1 = min(c[0] for c in cs), max(c[0] + c[2] for c in cs)
        dens.append(sum(c[4] for c in cs) / max(1, (x1 - x0) * (y1 - y0)))
    if len(bands) > 1 and max(dens) > 0:
        bands = [b for b, d in zip(bands, dens) if d >= 0.06 or d >= 0.4 * max(dens)]
    hmax = max(b[1] - b[0] for b in bands)
    bands = [b for b in bands if b[1] - b[0] >= hmax * 0.35]  # 띠 테두리 같은 가는 줄
    hmed = sorted(b[1] - b[0] for b in bands)[len(bands) // 2]
    return [(a, b) for a, b in bands if b - a >= hmed * 0.5]


def _merge_rows(items, overlap: float = 0.3) -> list[list[int]]:
    """덩어리를 세로로 겹치는 것끼리 띠 [y0, y1]로 묶는다."""
    bands: list[list[int]] = []
    for x, y, w, h, _a in sorted(items, key=lambda c: c[1]):
        if bands and y < bands[-1][1] - min(h, bands[-1][1] - bands[-1][0]) * overlap:
            bands[-1][1] = max(bands[-1][1], y + h)
        else:
            bands.append([y, y + h])
    return bands


def _bands_text_first(comps: list[tuple[int, int, int, int, int]],
                rules: list[tuple[int, int]] = ()) -> list[tuple[int, int]]:
    """덩어리들을 세로로 겹치는 것끼리 묶어 줄 띠 (y0, y1)로. 구분선 높이에 걸친 띠는 뺀다.

    줄은 글자 크기 덩어리로만 정한다: 글자보다 큰 아이콘(줄 사이에 걸쳐 두 줄을 붙이거나, 아주 큰 아이콘 줄이
    다른 줄을 ‘가는 줄’로 보이게 함)과 탁점·점 같은 작은 덩어리는 줄을 만들지 않는다.
    아이콘만 있는 줄은 따로 더한다."""
    if not comps:
        return []
    big_area = [c for c in comps if c[4] >= 30] or comps
    hs = sorted(c[3] for c in big_area)
    med = hs[len(hs) // 2]
    text = [c for c in comps if med * 0.5 <= c[3] <= med * 1.6]  # 글자 크기
    tall = [c for c in comps if c[3] > med * 1.6]  # 아이콘 등
    bands = _merge_rows(text)
    # 아이콘만 있는 줄: 어느 글자 줄에도 가운데가 들지 않는 큰 덩어리
    lone = [c for c in tall if not any(b0 <= c[1] + c[3] / 2 <= b1 for b0, b1 in bands)]
    for b0, b1 in _merge_rows(lone):
        bands.append([b0, b1])
    bands.sort()
    # 겹치는 띠 정리 (아이콘 줄이 글자 줄과 겹치면 하나로)
    merged: list[list[int]] = []
    for b0, b1 in bands:
        if merged and b0 < merged[-1][1]:  # 조금이라도 겹치면 한 줄
            merged[-1][1] = max(merged[-1][1], b1)
        else:
            merged.append([b0, b1])
    bands = [b for b in merged if not any(r0 < b[1] and r1 > b[0] for r0, r1 in rules)]
    if not bands:
        return []
    # 잉크가 드문드문한 띠(배경 그림 조각만 모인 줄)는 글자 줄이 아니다
    dens = []
    for y0, y1 in bands:
        cs = [c for c in comps if y0 <= c[1] + c[3] / 2 <= y1]
        if not cs:
            dens.append(0.0)
            continue
        x0, x1 = min(c[0] for c in cs), max(c[0] + c[2] for c in cs)
        dens.append(sum(c[4] for c in cs) / max(1, (x1 - x0) * (y1 - y0)))
    if len(bands) > 1 and max(dens) > 0:
        bands = [b for b, d in zip(bands, dens) if d >= 0.06 or d >= 0.4 * max(dens)]
    # 띠 테두리 같은 가는 줄: 보통 줄 높이(중앙값)의 35% 미만
    hmed = sorted(b[1] - b[0] for b in bands)[len(bands) // 2]
    return [(a_, b_) for a_, b_ in bands if b_ - a_ >= hmed * 0.35]


def _bands(ink: np.ndarray) -> list[tuple[int, int]]:
    """잉크가 있는 가로 줄 띠 (y0, y1)."""
    comps, rules = _text_components(ink)
    return _bands_from(comps, rules)


def _line_units(line: str) -> float:
    return sum(_units(ch) for ch in TOKEN_RE.sub("\u25a0", line)) + 0.1 * len(TOKEN_RE.findall(line))


def _match_line(lines: list[str], line_no: int, bands, comps, max_diff: int = 2) -> int | None:
    """원문 줄과 이미지 줄의 수가 다를 때, 남는 쪽을 빼 보며 줄 길이 비율이 가장 비슷한 짝을 찾는다.
    원문 line_no 줄에 짝지어진 이미지 줄 번호 (못 정하면 None)."""
    from itertools import combinations

    nb, nl = len(bands), len(lines)
    if abs(nb - nl) > max_diff or not nb or not nl:
        return None
    widths = []
    for y0, y1 in bands:
        xs = [(x, x + w) for x, y, w, h, _a in comps if y0 <= y + h / 2 <= y1]
        widths.append(max(b for _a, b in xs) - min(a for a, _b in xs) if xs else 0)
    units = [_line_units(ln) for ln in lines]
    best, best_cost = None, float("inf")
    if nb > nl:
        for drop in combinations(range(nb), nb - nl):
            keep = [i for i in range(nb) if i not in drop]
            ws = [widths[i] for i in keep]
            r = sum(ws) / max(sum(units), 1e-6)
            cost = sum(abs(w - u * r) for w, u in zip(ws, units)) / max(sum(ws), 1)
            if cost < best_cost:
                best, best_cost = keep[line_no], cost
    else:
        for drop in combinations(range(nl), nl - nb):
            if line_no in drop:
                continue
            keep = [i for i in range(nl) if i not in drop]
            us = [units[i] for i in keep]
            r = sum(widths) / max(sum(us), 1e-6)
            cost = sum(abs(w - u * r) for w, u in zip(widths, us)) / max(sum(widths), 1)
            if cost < best_cost:
                best, best_cost = keep.index(line_no), cost
    return best if best_cost < 0.35 else None


def _units(ch: str) -> float:
    return 0.5 if unicodedata.east_asian_width(ch) in ("Na", "H") and not ch.isspace() else (
        0.3 if ch.isspace() else 1.0)


def locate(img: np.ndarray, text: str, index: int) -> tuple[tuple[int, int, int, int] | None, str]:
    """text 안 index번째 아이콘 토큰([[..]])의 위치 (x0, y0, x1, y1, 이미지 px)와 실패 이유."""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    # 토큰이 들어 있는 줄과 그 줄 안에서의 순서
    k = -1
    for li, ln in enumerate(lines):
        toks = list(TOKEN_RE.finditer(ln))
        if k + len(toks) >= index:
            tok = toks[index - k - 1]
            line_no, before, after = li, ln[: tok.start()], ln[tok.end():]
            break
        k += len(toks)
    else:
        return None, "원문에서 아이콘 자리를 못 찾음"
    em_guess = img.shape[0] / max(1, len(lines)) * 0.6
    ink = _ink(img, em_guess)
    lum = img.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    # 색 아이콘을 쓰는 카드인가 (잉크 중 색 있는 픽셀이 5% 이상). 검정 선 아이콘 카드에서는 ‘검정=글자’로 거르지 않는다
    _px = img[ink].astype(np.float32)
    color_design = bool(len(_px)) and float(
        (((_px.max(axis=1) - _px.min(axis=1)) / np.maximum(_px.max(axis=1), 1.0)) >= 0.3).mean()) >= 0.05
    all_comps, rules = _text_components(ink)
    # 줄 띠를 두 가지로 잡아 본다: 보통 방법 -> 안 되면 글자 크기 덩어리로만 (큰 아이콘이 줄을 가리거나 붙인 경우)
    def _try(bands):
        if not bands:
            return None, "영역에서 글자를 찾지 못함"
        if len(bands) == len(lines):
            band_i = line_no
        else:  # 줄 수가 다르면(추출이 줄을 빠뜨림·장식 줄) 줄 길이가 가장 잘 맞는 짝을 고른다
            band_i = _match_line(lines, line_no, bands, all_comps)
            if band_i is None:
                return None, f"줄 수가 다름 (원문 {len(lines)}줄, 이미지 {len(bands)}줄)"
        y0, y1 = bands[band_i]
        band_h = y1 - y0
        em = sorted(b[1] - b[0] for b in bands)[len(bands) // 2]  # 보통 글자 줄 높이
        comps = [(x, y, w, h, a) for x, y, w, h, a in all_comps if y0 - band_h * 0.2 <= y + h / 2 <= y1 + band_h * 0.2]
        comps = [c for c in comps if not _light_drawing(img, lum, ink, c)] or comps
        if not comps:
            return None, "그 줄에 잉크가 없음"
        seq = _line_cells(lines[line_no])  # 원문 줄을 글자·아이콘 칸으로 (공백 제외)
        cells = _glyph_cells(comps, em, (y0, y1))
        if len(cells) != len(seq):
            # 칸 수가 안 맞으면: 띠 테두리 같은 긴 가로선이 글자 밑동에 닿아 이웃 글자·아이콘이 붙었을 수 있다.
            # 긴 선을 지우고 다시 나눠 맞으면 그것을 쓴다 (아이콘 테두리가 조금 깎일 수 있어 위아래로 2px 여유)
            alt = _glyph_cells(_line_components(ink, em, (y0, y1), band_h), em, (y0, y1))
            if len(alt) == len(seq):
                cells = [(c[0], max(0, c[1] - 2), c[2], c[3] + 2) for c in alt]
        if not cells:
            return None, "그 줄에서 글자 칸을 찾지 못함"
        k = sum(1 for t in seq[: _token_cell_index(lines[line_no], before)] if t == "\u25a0")
        tok_pos = [i for i, t in enumerate(seq) if t == "\u25a0"][k]
        n_tok = sum(1 for t in seq if t == "\u25a0")
        text_line = any(_WORDY_CH.match(t) for t in seq)
        # 글이 있는 줄의 숫자는 검정 글자라 색 덩어리가 아니다: 아이콘만 세면 된다
        # 그 줄의 글자 높이 (줄 띠 높이는 큰 아이콘 줄·붙은 줄 때문에 커질 수 있다)
        hs_l = sorted(c[3] for c in comps if c[4] >= 30)
        em_line = min(em, float(hs_l[len(hs_l) // 2]) * 1.15) if hs_l else em
        colored = _colored_icon_boxes(img, lum, ink, comps, em_line,
                                      [t for t in seq if not _DIGIT_CH.match(t)] if text_line else seq,
                                      min_gray_fill=0.45 if text_line else 0.3)
        if colored is not None:  # 글자(검정)와 색이 다른 덩어리 = 아이콘. 수가 맞으면 순서대로 (가장 믿을 만함)
            box = colored[k]
        elif len(cells) == len(seq):  # 칸 수가 같으면 순서대로 짝짓는다 (가장 믿을 만함)
            box = cells[tok_pos]
        elif _ordinal_icon_cell(cells, seq, tok_pos, band_h, em) is not None:
            # 칸 수는 달라도 아이콘 크기 칸의 수가 원문 아이콘 수와 같으면 순서대로 (같은 칸을 두 번 고르지 않는다)
            box = _ordinal_icon_cell(cells, seq, tok_pos, band_h, em)
        else:  # 다르면 위치 비율로 가장 가까운 칸 (아이콘 크기만)
            lx0, lx1 = cells[0][0], cells[-1][2]
            units = [1.1 if t == "\u25a0" else _units(t) for t in seq]
            unit = (lx1 - lx0) / max(sum(units), 1e-6)
            expect = lx0 + (sum(units[:tok_pos]) + units[tok_pos] / 2) * unit
            reach = max(unit, em) * 1.6
            cand = [c for c in cells if (c[3] - c[1]) >= em * 0.45 and abs((c[0] + c[2]) / 2 - expect) <= reach]
            if not cand:
                return None, "예상 자리 근처에 아이콘 크기의 덩어리가 없음"
            # 가깝고 큰 칸(아이콘은 보통 글자보다 크다)을 고른다
            box = max(cand, key=lambda c: (c[2] - c[0]) * (c[3] - c[1]) * float(np.exp(-abs((c[0] + c[2]) / 2 - expect) / reach)))
        if colored is None and text_line and _line_color_design(img, ink, y0, y1) and _looks_like_text(img, lum, ink, box):
            # 색 아이콘을 못 찾아 칸으로 짐작한 자리가 검정 글자처럼 보이면 틀린 자리다 (못 찾음으로 둔다)
            return None, "아이콘 자리로 짐작한 곳이 글자임"
        bx0, by0, bx1, by1 = _grow_box(box, comps)
        # 아래·위 그림 조각이 붙어 길어진 칸은 그 줄 높이 근처로 자른다
        by0, by1 = max(by0, y0 - 0.4 * band_h), min(by1, y1 + 0.4 * band_h)
        box = (bx0, by0, bx1, by1)
        if (not (em * 0.35 <= bx1 - bx0 <= em * 3.0) or by1 - by0 > em * 2.5 or by1 - by0 < em * 0.3
                or bx1 - bx0 < 0.55 * (by1 - by0)  # 세로로 가늘고 긴 것은 배경 그림 조각
                or bx1 - bx0 > 2.4 * (by1 - by0)  # 가로로 긴 것은 라벨 띠
                or float(ink[int(by0):int(by1), int(bx0):int(bx1)].mean()) < 0.15):  # 드문드문한 선 = 그림
            return None, "찾은 덩어리 크기가 아이콘 같지 않음"
        return (int(bx0), int(by0), int(bx1), int(by1)), ""

    first = _try(_bands_from(all_comps, rules))
    if first[0] is not None:
        return first
    second = _try(_bands_text_first(all_comps, rules))
    return second if second[0] is not None else first


# ---------------------------------------------------------------- 아이콘에 붙은 숫자 (예: 2[[coin]] -> 숫자까지 아이콘)
_NUM_BEFORE = re.compile(r"(?<![0-9０-９])([+\-＋－]?[0-9０-９]+)[ 　]?$")
_NUM_AFTER = re.compile(r"^[ 　]?([+\-＋－]?[0-9０-９]+)(?![0-9０-９ぁ-んァ-ヶ一-龯々A-Za-zＡ-Ｚａ-ｚ]|[ 　]?\[\[)")


def number_near(text: str, start: int, end: int) -> tuple[str, int, int] | None:
    """text[start:end]의 아이콘 토큰 바로 앞(또는 뒤)에 붙은 숫자: (앞|뒤, 숫자 시작, 숫자 끝). 같은 줄 안만.
    앞이 먼저다 (‘2[[coin]]’). 뒤 숫자는 단위 글자(枚 등)나 다음 아이콘이 이어지면 빼다."""
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", end)
    le = len(text) if le < 0 else le
    before = text[ls:start]
    if not before.endswith("]]"):
        m = _NUM_BEFORE.search(before)
        if m:
            return "before", ls + m.start(1), ls + m.end(1)
    m = _NUM_AFTER.match(text[end:le])
    if m:
        return "after", end + m.start(1), end + m.end(1)
    return None


def absorb_number(img: np.ndarray, box: tuple[int, int, int, int], side: str, n_chars: int):
    """아이콘 상자 옆(side: before=왼쪽, after=오른쪽)에 인쇄된 숫자를 상자에 넣는다. 숫자가 아이콘 위에 인쇄돼
    상자 안에 있으면 상자 그대로. 그림에서 숫자를 찾지 못하면 None (그때는 원문 숫자도 그대로 둔다)."""
    import cv2

    bx0, by0, bx1, by1 = box
    h = max(1, by1 - by0)
    ink = _ink(img, h).astype(np.uint8)
    n, _lab, st, _c = cv2.connectedComponentsWithStats(ink, connectivity=8)
    comps = []
    for i in range(1, n):
        x, y, w, hh, a = (int(v) for v in st[i])
        if a < 6 or not (0.25 * h <= hh <= 1.2 * h) or w > 1.2 * h:
            continue
        cy = y + hh / 2
        if not (by0 - 0.1 * h <= cy <= by1 + 0.1 * h):
            continue
        comps.append((x, y, x + w, y + hh))
    inside = [c for c in comps if c[0] >= bx0 and c[2] <= bx1 and c[1] >= by0 and c[3] <= by1 and c[3] - c[1] <= 0.8 * h]
    got = []
    edge = bx0 if side == "before" else bx1
    gap = 0.6 * h
    for _ in range(max(1, n_chars)):  # 숫자 글자 수만큼만 (옆 글자까지 붙이지 않는다)
        if side == "before":
            cand = [c for c in comps if c not in got and c[2] <= edge + 2 and edge - c[2] <= gap]
            nxt = max(cand, key=lambda c: c[2], default=None)
        else:
            cand = [c for c in comps if c not in got and c[0] >= edge - 2 and c[0] - edge <= gap]
            nxt = min(cand, key=lambda c: c[0], default=None)
        if nxt is None:
            break
        got.append(nxt)
        edge = nxt[0] if side == "before" else nxt[2]
        gap = 0.35 * h
    if got:
        xs = [c[0] for c in got] + [c[2] for c in got]
        ys = [c[1] for c in got] + [c[3] for c in got]
        return (min(bx0, *xs), min(by0, *ys), max(bx1, *xs), max(by1, *ys))
    if inside:
        return box
    return None


def strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """text에서 (시작, 끝) 구간들을 뺀다 (아이콘에 넣은 숫자). 남는 겹 공백은 하나로."""
    for s, e in sorted(spans, reverse=True):
        text = text[:s] + text[e:]
    text = re.sub(r"(?<=\S)[ 　]{2,}(?=\S)", " ", text)
    return "\n".join(ln.strip(" 　") for ln in text.split("\n"))


def strip_in_extract(file_dir: Path, page: int, region: str, original: str, spans: list[tuple[int, int]]) -> None:
    """추출 결과의 원문에서 아이콘에 넣은 숫자를 뺀다 (원문이 그사이 바뀌지 않았을 때만)."""
    for f in _extract_files(file_dir, page):
        rec = json.loads(f.read_text(encoding="utf-8"))
        r = rec["regions"].get(region)
        if not r or r.get("source") != original:
            continue
        r["source"] = strip_spans(original, spans)
        r["icon_number_note"] = "아이콘 옆·위 숫자를 아이콘에 넣고 원문에서 뺌"
        f.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def icon_boxes(img: np.ndarray, text: str) -> list[tuple[str, tuple[int, int, int, int], bool]]:
    """원문의 아이콘 자리 [(이름, (x0, y0, x1, y1) 이미지 px, 아이콘만 있는 줄인가)]. 찾지 못한 아이콘은 뺀다."""
    from app.erase_text import is_icon_line

    out = []
    idx = 0
    for ln in (t for t in text.split("\n") if t.strip()):
        toks = list(TOKEN_RE.finditer(ln))
        only = is_icon_line(ln)
        for k, t in enumerate(toks):
            box, _why = locate(img, text, idx + k)
            if box is not None:
                out.append((t.group(1), box, only))
        idx += len(toks)
    return out


def inline_icon_boxes(img: np.ndarray, text: str) -> list[tuple[str, tuple[int, int, int, int]]]:
    """글 줄 안의 아이콘 자리 [(이름, 상자)]. 아이콘만 있는 줄의 아이콘은 뺀다 (그 줄은 지우지도 다시 그리지도 않는다)."""
    return [(n, b) for n, b, only in icon_boxes(img, text) if not only]


LIGHT_DRAWING = 135.0  # 덩어리의 가장 진한 쪽(10%) 밝기가 이보다 밝으면 연한 배경 그림 (0~255)


def _light_drawing(img: np.ndarray, lum: np.ndarray, ink: np.ndarray, c) -> bool:
    """연하고 색이 거의 없고 드문드문한 덩어리 = 배경 그림 선.
    (금색 동전처럼 밝아도 색이 있거나, 회색 타일처럼 속이 꽉 찬 것은 아이콘)"""
    x, y, w, h, _a = c
    m = ink[y:y + h, x:x + w]
    if not m.any():
        return True
    if float(m.mean()) >= 0.4:  # 속이 찬 덩어리는 그림 선이 아니다
        return False
    if float(np.percentile(lum[y:y + h, x:x + w][m], 10)) < LIGHT_DRAWING:
        return False
    px = img[y:y + h, x:x + w][m].astype(np.float32)
    sat = (px.max(axis=1) - px.min(axis=1)) / np.maximum(px.max(axis=1), 1.0)
    return float(np.median(sat)) < 0.25


_WORDY_CH = re.compile(r"[ぁ-んァ-ヶ一-龯々A-Za-zＡ-Ｚａ-ｚ가-힣]")
_DIGIT_CH = re.compile(r"[0-9０-９＋+×xX]")


def _line_color_design(img, ink, y0: int, y1: int) -> bool:
    """그 줄에 글자색과 다른 ‘색 있는’ 잉크가 5% 이상: 아이콘이 색으로 그려진 카드 (검정 선 아이콘·빨간 글자 카드는 아님)."""
    m = ink[y0:y1]
    if m.sum() < 50:
        return False
    px = img[y0:y1][m].astype(np.float32)
    text_col = np.median(px, axis=0)
    sat = (px.max(axis=1) - px.min(axis=1)) / np.maximum(px.max(axis=1), 1.0)
    other = (sat >= 0.3) & (np.abs(px - text_col).max(axis=1) > 80)
    return float(other.mean()) >= 0.05


def _looks_like_text(img, lum, ink, box) -> bool:
    """상자 안 잉크가 진하고 색이 없다 = 검정 글자."""
    x0, y0, x1, y1 = (int(v) for v in box)
    m = ink[y0:y1, x0:x1]
    if m.sum() < 20:
        return True
    px = img[y0:y1, x0:x1][m].astype(np.float32)
    sat = (px.max(axis=1) - px.min(axis=1)) / np.maximum(px.max(axis=1), 1.0)
    # 진한 쪽(30%) 밝기로 본다: 옆의 연한 그림 선이 섞여도 글자 획은 진하다
    if not (float(np.median(sat)) < 0.2 and float(np.percentile(lum[y0:y1, x0:x1][m], 30)) < 100):
        return False
    # 테두리가 넓은 속을 감싸면(동전·타일처럼) 아이콘이다. 글자 속 빈칸은 작다
    import cv2

    inv = (~m).astype(np.uint8)
    h, w = inv.shape
    if h < 3 or w < 3:
        return True
    ff = inv.copy()
    mask = np.zeros((h + 2, w + 2), np.uint8)
    for sx, sy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        if ff[sy, sx]:
            cv2.floodFill(ff, mask, (sx, sy), 0)
    holes = float(ff.sum()) / max(1, h * w)
    return holes < 0.2


def _colored_icon_boxes(img, lum, ink, comps, em: float, seq: list[str], min_gray_fill: float = 0.45):
    """줄 안에서 글자색(진한 무채색)이 아닌 덩어리(금색 동전·빨간 병·초록 책·회색 타일 등)를 겹치는 것끼리 묶은
    아이콘 상자들(왼쪽부터, 원문 아이콘 순서). 수가 맞을 때만 돌려준다.
    색 숫자(갈색 ‘4’ 등)가 섞이면 원문 줄의 숫자·아이콘 순서에 맞춰 숫자 자리를 뺀다."""
    n_tok = sum(1 for t in seq if t == "\u25a0")
    if not n_tok:
        return None
    picked = []
    for c in comps:
        x, y, w, h, _a = c
        m = ink[y:y + h, x:x + w]
        if m.sum() < 20 or _light_drawing(img, lum, ink, c):
            continue
        px = img[y:y + h, x:x + w][m].astype(np.float32)
        sat = (px.max(axis=1) - px.min(axis=1)) / np.maximum(px.max(axis=1), 1.0)
        med_l = float(np.median(lum[y:y + h, x:x + w][m]))
        fill = float(m.sum()) / max(1, w * h)
        colored = float(np.median(sat)) >= 0.25 and fill >= 0.25
        gray_solid = med_l >= 95 and fill >= min_gray_fill  # 회색 타일처럼 속이 찬 것 (드문드문한 그림 선은 빼다)
        if colored or gray_solid:
            picked.append([x, y, x + w, y + h])
    boxes: list[list[int]] = []
    for b in sorted(picked):  # 겹치거나 아주 가까운 것끼리 하나로
        g = 0  # 겹치는 것만 (옆에 붙은 숫자는 따로)
        for bb in boxes:
            if b[0] <= bb[2] + g and b[2] >= bb[0] - g and b[1] <= bb[3] + g and b[3] >= bb[1] - g:
                bb[:] = [min(bb[0], b[0]), min(bb[1], b[1]), max(bb[2], b[2]), max(bb[3], b[3])]
                break
        else:
            boxes.append(list(b))
    boxes = [b for b in boxes if (b[3] - b[1]) >= 0.5 * em and (b[2] - b[0]) >= 0.35 * em
             and (b[2] - b[0]) >= 0.55 * (b[3] - b[1])]  # 괄호처럼 좁은 것은 빼다
    boxes.sort()
    sub = [t for t in seq if t == "\u25a0" or _DIGIT_CH.match(t)]
    if len(sub) > n_tok:  # 줄에 숫자가 있다 (색 숫자 ‘4’ 등이 상자로 잡혔을 수 있음)
        # 원문 줄의 숫자·아이콘 순서와 상자 수가 같으면 아이콘 자리만
        if len(boxes) == len(sub):
            return [tuple(b) for b, t in zip(boxes, sub) if t == "\u25a0"]
        wide = [b for b in boxes if (b[2] - b[0]) >= 0.68 * (b[3] - b[1])]  # 숫자는 좁다
        if len(wide) == n_tok:
            return [tuple(b) for b in wide]
        return None
    if len(boxes) == n_tok:
        return [tuple(b) for b in boxes]
    if len(boxes) > n_tok:  # 글 속의 색 숫자(갈색 ‘2’ 등)가 더 잡혔으면 숫자처럼 좁은 것을 뺀다
        wide = [b for b in boxes if (b[2] - b[0]) >= 0.75 * (b[3] - b[1])]
        if len(wide) == n_tok:
            return [tuple(b) for b in wide]
    return None


def _ordinal_icon_cell(cells, seq, tok_pos: int, band_h: float, em: float):
    """아이콘 크기 칸(줄 높이의 70% 이상이거나 보통 글자보다 넓은 것)이 원문의 아이콘 수와 같으면 k번째 아이콘 칸."""
    n_tok = sum(1 for t in seq if t == "\u25a0")
    k = sum(1 for t in seq[:tok_pos] if t == "\u25a0")
    big = [c for c in cells if (c[3] - c[1]) >= 0.7 * band_h and (c[2] - c[0]) >= 0.8 * em]  # 숫자 폭은 빼다
    if len(big) == n_tok and n_tok:
        return big[k]
    return None


def _grow_box(box, comps, gap: int = 3):
    """box에 닿거나 겹치는 덩어리를 붙여 넓힌다 (조각난 아이콘을 한 그림으로). 너무 커지면 그대로."""
    bx0, by0, bx1, by1 = box
    h0, w0 = by1 - by0, bx1 - bx0
    changed = True
    while changed:
        changed = False
        for x, y, w, h, _a in comps:
            ix = min(bx1, x + w) - max(bx0, x)
            iy = min(by1, y + h) - max(by0, y)
            # 많이 겹치는 조각만 (상자 테두리가 안의 동전을 감싸는 것 등). 옆 글자처럼 닿기만 한 것은 붙이지 않는다
            if ix > 0 and iy > 0 and ix * iy >= 0.5 * min(w * h, (bx1 - bx0) * (by1 - by0)):
                nb = (min(bx0, x), min(by0, y), max(bx1, x + w), max(by1, y + h))
                if nb != (bx0, by0, bx1, by1) and (nb[3] - nb[1]) <= 1.6 * h0 + 4 and (nb[2] - nb[0]) <= 1.6 * w0 + 4:
                    bx0, by0, bx1, by1 = nb
                    changed = True
    return bx0, by0, bx1, by1


def _glyph_cells(comps, em: float, band: tuple[int, int] | None = None) -> list[tuple[int, int, int, int]]:
    """한 줄의 덩어리를 글자 칸으로 묶는다.
    - 가로로 겹치면 늘 하나로 (겹친 카드 아이콘, 아이콘 안 무늬)
    - 틈이 좁으면 합친 폭이 글자 하나(줄의 글자 높이 정도) 이하일 때만 하나로 (以·ハ처럼 갈라진 글자)
      -> 글자 사이가 좁은 글꼴에서도 서로 다른 글자는 붙지 않는다."""
    # 글자·아이콘보다 훨씬 큰 덩어리(띠 테두리·그림 가장자리)는 칸이 아니다
    boxes = sorted([x, y, x + w, y + h] for x, y, w, h, a in comps if a >= 4 and w <= em * 3.2 and h <= em * 2.6)
    if band is not None and boxes:  # 줄 위·아래 가장자리에 걸친 가는 조각(띠 테두리)은 뺀다. 가운데의 ‘ー’는 남김
        b0, b1 = band
        hb = max(1, b1 - b0)
        boxes = [b for b in boxes if not ((b[3] - b[1]) < hb * 0.25
                                          and not (b0 + hb * 0.25 <= (b[1] + b[3]) / 2 <= b0 + hb * 0.75))]
    if not boxes:
        return []
    hs = sorted(b[3] - b[1] for b in boxes)
    tall = [h for h in hs if h >= hs[-1] * 0.5]
    gh = tall[len(tall) // 2]  # 이 줄 글자의 높이
    # 아주 작은 점·잡티는 묶기 전에 뺀다 (이웃 글자와 아이콘 사이에서 다리처럼 둘을 이어 붙이지 않게)
    boxes = [b for b in boxes if (b[2] - b[0]) >= gh * 0.2 or (b[3] - b[1]) >= gh * 0.2]
    cells: list[list[int]] = []
    for b in boxes:
        if cells:
            c = cells[-1]
            overlap = b[0] < c[2] - 1
            close = b[0] <= c[2] + gh * 0.15 and max(c[2], b[2]) - min(c[0], b[0]) <= gh * 1.12
            if overlap or close:
                c[0], c[1], c[2], c[3] = min(c[0], b[0]), min(c[1], b[1]), max(c[2], b[2]), max(c[3], b[3])
                continue
        cells.append(list(b))
    # 점·잡티와 작은 문장부호(、。…)는 칸으로 세지 않는다 (원문 쪽도 SMALL_MARKS를 뺀다)
    cells = [c for c in cells if (c[3] - c[1]) >= gh * 0.4 or (c[2] - c[0]) >= gh * 0.4]
    return [tuple(c) for c in _tidy_cells(cells, gh)]


def _tidy_cells(cells: list[list[int]], gh: float) -> list[list[int]]:
    """줄 양 끝에서 멀리 떨어진 작은 조각(카드 테두리 조각)은 빼고, 바로 옆에 붙은 아주 작은 점은 이웃 칸에 합친다."""
    def small(c):
        return (c[2] - c[0]) < gh * 0.3  # 가는 조각 (높이와 관계없이: 카드 테두리 조각은 글자만큼 높기도 하다)

    while len(cells) > 1 and small(cells[0]) and cells[1][0] - cells[0][2] > gh * 1.2:
        cells = cells[1:]
    while len(cells) > 1 and small(cells[-1]) and cells[-1][0] - cells[-2][2] > gh * 1.2:
        cells = cells[:-1]
    out: list[list[int]] = []
    for c in cells:
        tiny = (c[2] - c[0]) < gh * 0.25 and (c[3] - c[1]) < gh * 0.7
        if tiny and out and c[0] - out[-1][2] < gh * 0.25:
            o = out[-1]
            o[0], o[1], o[2], o[3] = min(o[0], c[0]), min(o[1], c[1]), max(o[2], c[2]), max(o[3], c[3])
            continue
        out.append(list(c))
    return out


def _line_components(ink: np.ndarray, em: float, band: tuple[int, int], band_h: int):
    """한 줄 높이(위아래 20% 여유) 안의 덩어리 [(x, y, w, h, 넓이)]. 긴 가로선은 지우고 센다."""
    import cv2

    y0, y1 = band
    top, bot = max(0, int(y0 - band_h * 0.2)), min(ink.shape[0], int(y1 + band_h * 0.2) + 1)
    strip = ink[top:bot] & ~_long_lines(ink[top:bot], em)
    n, _lab, st, _c = cv2.connectedComponentsWithStats(strip.astype(np.uint8), connectivity=8)
    out = []
    for c in range(1, n):
        x, y, w, h, a = (int(v) for v in st[c])
        cy = top + y + h / 2
        if a >= 6 and y0 - band_h * 0.2 <= cy <= y1 + band_h * 0.2:
            out.append((x, top + y, w, h, a))
    return out


def _long_lines(ink: np.ndarray, em: float) -> np.ndarray:
    """글자보다 훨씬 긴 가로·세로 선(띠 테두리·구분선)만 골라낸 마스크 (조금 두껍게)."""
    import cv2

    k = max(15, int(em * 1.6))
    m = ink.astype(np.uint8)
    horiz = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1)))
    lines = cv2.dilate(horiz, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    return lines.astype(bool)


SMALL_MARKS = set("、。，．・,.'`:;…‥")  # 이미지에서 칸으로 세지 않을 만큼 작은 글자


def _line_cells(line: str) -> list[str]:
    """원문 한 줄 -> 글자 칸 목록. 아이콘 토큰은 \u25a0 하나, 공백·작은 문장부호는 칸이 아니다."""
    return [ch for ch in TOKEN_RE.sub("\u25a0", line) if not ch.isspace() and ch not in SMALL_MARKS]


def _token_cell_index(line: str, before: str) -> int:
    """before(토큰 앞 문자열)까지의 칸 수."""
    return len(_line_cells(before))


def cut_out(img: np.ndarray, box: tuple[int, int, int, int], pad: int = 4) -> np.ndarray:
    """box를 잘라 아이콘 바깥 배경만 투명하게 한 RGBA (아이콘 안쪽의 흰 부분·무늬는 살린다)."""
    import cv2

    h, w = img.shape[:2]
    x0, y0, x1, y1 = max(0, box[0] - pad), max(0, box[1] - pad), min(w, box[2] + pad), min(h, box[3] + pad)
    crop = img[y0:y1, x0:x1]
    em = max(box[2] - box[0], box[3] - box[1])
    from app.erase import dark_ink

    # 잘라 낼 때는 배경색과 뚜렷이 다른 것만 잉크로 본다 (분홍·노랑 카드 바탕이 남지 않게)
    border = np.concatenate([crop[0], crop[-1], crop[:, 0], crop[:, -1]]).astype(np.float32)
    bgc = np.median(border, axis=0)
    dist = np.abs(crop.astype(np.float32) - bgc).max(axis=2)
    bg_px = int(max(31, em * 2.2)) | 1
    ink = ((dist > 120) | dark_ink(img, 50.0, bg_px)[y0:y1, x0:x1]).astype(np.uint8)
    inside = np.zeros_like(ink)
    inside[box[1] - y0:box[3] - y0, box[0] - x0:box[2] - x0] = 1
    ink &= inside
    k = max(1, em // 25)
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1)))
    # 바깥에서 채워 들어가 닿지 않는 곳 = 모양 안쪽 (구멍 채우기)
    flood = np.pad((1 - ink).astype(np.uint8) * 255, 1, constant_values=255)  # 바깥을 한 겹 둘러 네 모서리가 이어지게
    mask = np.zeros((flood.shape[0] + 2, flood.shape[1] + 2), np.uint8)
    cv2.floodFill(flood, mask, (0, 0), 0)
    flood = flood[1:-1, 1:-1]
    shape = ((ink > 0) | (flood > 0)).astype(np.float32)
    alpha = cv2.GaussianBlur(shape, (0, 0), max(0.6, k * 0.6))  # 가장자리를 부드럽게
    alpha = np.clip(alpha * 1.6, 0, 1)
    return np.dstack([crop, (alpha * 255).astype(np.uint8)])


def save_png(rgba: np.ndarray, path: Path) -> None:
    import cv2

    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
    if not ok:
        raise OSError(f"PNG 저장 실패: {path}")
    path.write_bytes(buf.tobytes())


def load_png(path: Path) -> np.ndarray:
    import cv2

    a = cv2.imdecode(np.frombuffer(Path(path).read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
    if a.ndim == 2:
        a = np.dstack([a, a, a, np.full(a.shape, 255, np.uint8)])
    elif a.shape[2] == 3:
        a = np.dstack([a, np.full(a.shape[:2], 255, np.uint8)])
    return cv2.cvtColor(a, cv2.COLOR_BGRA2RGBA)


# ---------------------------------------------------------------- 모양 비교
@dataclass
class Shape:
    alpha: np.ndarray  # 32x32 0~1
    aspect: float
    color: np.ndarray  # 불투명 부분 평균 RGB


def shape_of(rgba: np.ndarray, n: int = 32) -> Shape:
    import cv2

    a = rgba[:, :, 3].astype(np.float32) / 255.0
    ys, xs = np.nonzero(a > 0.3)
    if len(xs):
        a = a[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1]
        rgb = rgba[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1, :3]
    else:
        rgb = rgba[:, :, :3]
    h, w = a.shape
    # 비교할 무늬: 모양 안의 ‘진한 선’ (바깥을 투명하게 한 실루엣만 보면 동전·카드·왕관이 다 비슷해진다)
    lum = (0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]).astype(np.float32) / 255.0
    feat = ((a > 0.5) & (lum < 0.5)).astype(np.float32)  # 진한 선(그림의 테두리·무늬)만
    if feat.sum() < a.size * 0.03:  # 진한 선이 거의 없는 밝은 색 아이콘은 실루엣으로
        feat = a
    s = max(h, w)
    m0 = max(1, s // 8)  # 둘레에 빈칸을 둬서 꽉 찬 네모 아이콘도 모양 차이가 생기게
    sq = np.zeros((s + 2 * m0, s + 2 * m0), np.float32)
    sq[m0 + (s - h) // 2:m0 + (s - h) // 2 + h, m0 + (s - w) // 2:m0 + (s - w) // 2 + w] = feat
    small = cv2.GaussianBlur(cv2.resize(sq, (n, n), interpolation=cv2.INTER_AREA), (0, 0), 0.8)
    m = a > 0.5
    color = rgb[m].mean(axis=0) if m.any() else np.zeros(3)
    return Shape(small, w / max(h, 1), color.astype(np.float32))


def gray_feature(rgba: np.ndarray, n: int = 24) -> np.ndarray:
    """밝기 무늬(바탕 회색 위 흰 숫자 같은 것까지): 모양만 남겨 n×n으로 줄이고 평균 0·크기 1로 맞춘 벡터."""
    import cv2

    al = rgba[:, :, 3] > 80
    ys, xs = np.nonzero(al)
    a = rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1] if len(xs) else rgba
    g = (a[:, :, :3].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)) / 255.0
    g = cv2.resize(g, (n, n), interpolation=cv2.INTER_AREA).ravel()
    g = g - g.mean()
    nrm = float(np.linalg.norm(g))
    return g / nrm if nrm > 0 else g


def inner_feature(rgba: np.ndarray, n: int = 32, frac: float = 0.6) -> np.ndarray:
    """가운데(60%)만 자세히 본 밝기 무늬: 같은 테두리·바탕 안의 내용(동전 숫자 3·5, 상자 안 동전·병)을 가른다."""
    import cv2

    al = rgba[:, :, 3] > 80
    ys, xs = np.nonzero(al)
    a = rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1] if len(xs) else rgba
    h, w = a.shape[:2]
    m0, m1 = int(h * (1 - frac) / 2), int(w * (1 - frac) / 2)
    a = a[m0:h - m0 or h, m1:w - m1 or w]
    g = a[:, :, :3].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    g = cv2.GaussianBlur(g, (0, 0), max(1.0, h / 60))  # 크기·인쇄 상태 차이를 줄인다
    g = cv2.resize(g, (n, n), interpolation=cv2.INTER_AREA).ravel()
    g = g - g.mean()
    nrm = float(np.linalg.norm(g))
    return g / nrm if nrm > 0 else g


def shape_pair(rgba: np.ndarray):
    """(대강 모양 32칸, 자세한 모양 64칸, 밝기 무늬, 가운데 자세히)."""
    return shape_of(rgba), shape_of(rgba, 64), gray_feature(rgba), inner_feature(rgba)


def _comparable(a: Shape, b: Shape) -> bool:
    if max(a.aspect, b.aspect) / max(min(a.aspect, b.aspect), 1e-6) > 1.3:
        return False
    return float(np.linalg.norm(a.color - b.color)) <= MAX_COLOR_DIST


def same_icon(a, b) -> float:
    """같은 아이콘이면 0보다 큰 점수, 아니면 0.
    - 밝기 무늬가 아주 비슷하면(크기·인쇄 상태가 달라 진한 선이 달라 보여도) 같은 아이콘
    - 아니면 진한 선 모양이 대강·자세히 모두 같고 밝기 무늬도 어느 정도 같아야 한다
      (테두리·바탕이 같고 안의 숫자만 다른 타일 1·2, 같은 나무 상자의 동전·칼은 여기서 갈린다)"""
    if not _comparable(a[0], b[0]):
        return 0.0
    gray = float(a[2] @ b[2]) if len(a) > 2 and len(b) > 2 else 0.0
    inner = float(a[3] @ b[3]) if len(a) > 3 and len(b) > 3 else 1.0
    if inner < SAME_INNER:  # 테두리·바탕이 같아도 안의 내용이 다르면 다른 아이콘
        return 0.0
    s = similarity(a[0], b[0])
    if gray >= SAME_GRAY:
        return max(s, gray)
    if s >= SAME_SHAPE and similarity(a[1], b[1]) >= SAME_DETAIL and gray >= 0.7:
        return s
    return 0.0


def similarity(a: Shape, b: Shape) -> float:
    """0~1. 모양 상관계수. 가로세로 비나 색이 많이 다르면 0."""
    if max(a.aspect, b.aspect) / max(min(a.aspect, b.aspect), 1e-6) > 1.3:
        return 0.0
    if float(np.linalg.norm(a.color - b.color)) > MAX_COLOR_DIST:
        return 0.0
    x, y = a.alpha.ravel() - a.alpha.mean(), b.alpha.ravel() - b.alpha.mean()
    d = float(np.linalg.norm(x) * np.linalg.norm(y))
    return float(x @ y / d) if d > 0 else 0.0


# ---------------------------------------------------------------- 원문 안 아이콘 위치 (엑셀 목록용)
def tokens_in(text: str) -> list[tuple[int, int, str]]:
    """원문 안 아이콘 토큰 [(줄 번호 1부터, 그 줄에서 몇 번째 1부터, 이름)]. 이름이 없으면 '?'."""
    out = []
    for li, ln in enumerate(text.split("\n"), start=1):
        for k, m in enumerate(TOKEN_RE.finditer(ln), start=1):
            out.append((li, k, m.group(1)))
    return out


def token_status(name: str, dictionary: dict, auto_note: str = "") -> tuple[str, bool]:
    """(상태, 사람이 확인해야 하는가)."""
    if not NAME_RE.match(name):
        return "이름 없음 (아이콘 검증 필요)", True
    if name not in dictionary:
        return "사전에 없음 (합성 안 됨)", True
    if auto_note:
        return "사전과 모양이 같아 자동으로 맞춤 (확인 권장)", True
    return "사전 확인됨", False


# ---------------------------------------------------------------- 원문 안 토큰 바꾸기
def replace_placeholders(text: str, mapping: dict[int, str]) -> str:
    """text 안 i번째 ``[[?]]``를 mapping[i]로 ('' = 빼기, 이름 = [[이름]], '=글자' = 그 글자로). 없는 번호는 그대로."""
    i = -1

    def sub(m):
        nonlocal i
        i += 1
        if i not in mapping:
            return m.group(0)
        name = mapping[i]
        if name.startswith("="):  # 아이콘이 아니라 글자였다 (모델이 글자를 아이콘으로 읽음)
            return name[1:]
        return f"[[{name}]]" if name else ""

    out = PLACEHOLDER_RE.sub(sub, text)
    return re.sub(r"[ 　]{2,}", " ", out) if any(v == "" for v in mapping.values()) else out


def _extract_files(file_dir: Path, page: int) -> list[Path]:
    """그 쪽 추출 결과 파일들 (pages/가 실제로 읽히는 파일, 바깥 사본도 맞춰 둔다)."""
    base = Path(file_dir) / "extract"
    return [p for p in (base / "pages" / f"page-{page:03d}.json", base / f"page-{page:03d}.json") if p.exists()]


def apply_to_extract(file_dir: Path, page: int, region: str, mapping: dict[int, str], note: str = "") -> None:
    for f in _extract_files(file_dir, page):
        rec = json.loads(f.read_text(encoding="utf-8"))
        r = rec["regions"].get(region)
        if not r or not isinstance(r.get("source"), str):
            continue
        r["source"] = replace_placeholders(r["source"], mapping)
        if note:
            r["icon_note"] = note
        f.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def icon_uses(info) -> dict[str, int]:
    """게임 원문에 쓰인 아이콘 이름별 횟수 ([[이름]], 뺀 쪽은 세지 않음)."""
    from app.extract import load_extracted
    from app.workspace import game_file_dirs, included_pages

    out: dict[str, int] = {}
    for fd in game_file_dirs(info):
        inc = set(included_pages(fd, limited=False))
        for pno, regs in load_extracted(fd).items():
            if pno not in inc:
                continue
            for v in regs.values():
                src = v.get("source")
                for t in (src if isinstance(src, list) else [src or ""]):
                    for m in TOKEN_RE.finditer(t or ""):
                        out[m.group(1)] = out.get(m.group(1), 0) + 1
    return out


def reset_icon(info, name: str) -> int:
    """아이콘을 다시 정하게: 게임 원문의 [[name]]을 [[?]]로 되돌리고 사전에서 뺀다. 되돌린 곳 수.
    (다른 아이콘이 잘못 같은 이름으로 묶였을 때. 다시 ‘아이콘 이름’을 누르면 새로 묶어 이름을 정한다.)"""
    from app.workspace import game_file_dirs

    tok = f"[[{name}]]"
    n = 0
    for fd in game_file_dirs(info):
        base = Path(fd) / "extract"
        for f in list(base.glob("pages/page-*.json")) + list(base.glob("page-*.json")):
            rec = json.loads(f.read_text(encoding="utf-8"))
            changed = False
            for r in (rec.get("regions") or {}).values():
                src = r.get("source")
                if isinstance(src, str) and tok in src:
                    k = src.count(tok)
                    r["source"] = src.replace(tok, PLACEHOLDER)
                    r["icon_note"] = f"아이콘 다시 정하기 ([[{name}]] 되돌림)"
                    changed = True
                    if f.parent.name == "pages":
                        n += k
            if changed:
                tmp = f.with_suffix(".tmp")
                tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(f)
    d = load_dictionary(info)
    e = d["icons"].pop(name, None)
    save_dictionary(info, d)
    if e and e.get("file"):
        png = icons_dir(info) / e["file"]
        if png.exists():
            png.rename(png.with_name(f"_reset_{png.name}"))  # 지우지 않고 이름만 바꿔 둔다
    return n


# ---------------------------------------------------------------- 찾기 (scan)
@dataclass
class Occurrence:
    id: str
    file: str  # 프로젝트 폴더 기준 상대 경로
    file_key: str
    page: int
    region: str
    index: int  # 그 칸의 몇 번째 [[?]] (0부터, 자동으로 바꾼 것을 뺀 뒤)
    context: str
    png: str = ""
    reason: str = ""
    group: str = ""
    match: str = ""
    score: float = 0.0
    line: int = 0  # 원문에서 몇째 줄 (1부터)
    order: int = 0  # 그 줄에서 몇 번째 아이콘 (1부터)


@dataclass
class ScanResult:
    occurrences: list[Occurrence] = field(default_factory=list)
    groups: dict = field(default_factory=dict)  # 묶음 -> {"rep", "count", "suggest", "meaning", "ko"}
    auto: dict = field(default_factory=dict)  # 사전으로 바로 바꾼 이름 -> 횟수

    @property
    def pending(self) -> int:
        return len(self.occurrences)


def _occ_dir(info, f: str) -> Path:
    """찾은 자리의 파일 폴더. 데이터 폴더 기준 경로(게임 단위), 예전 결과는 프로젝트 기준."""
    from app.workspace import data_root

    d = data_root(info) / f
    return d if d.exists() else info.dir / f


def pending_count(info) -> int:
    """게임의 모든 프로젝트(뺀 쪽 제외) 원문에 남은 ``[[?]]`` 수 (아이콘 검증은 게임 단위)."""
    from app.extract import load_extracted
    from app.workspace import game_file_dirs, included_pages

    n = 0
    for fd in game_file_dirs(info):
        inc = set(included_pages(fd))
        for pno, regs in load_extracted(fd).items():
            if pno in inc:
                n += sum(len(PLACEHOLDER_RE.findall(r["source"])) for r in regs.values()
                         if isinstance(r.get("source"), str))
    return n


def scan(info, work: Path) -> ScanResult:
    """[[?]]를 모두 찾아 위치·그림을 만들고, 사전과 같은 것은 바로 바꾸고, 나머지는 묶는다."""
    import pymupdf

    from app.extract import load_extracted
    from app.project import load_project
    from app.workspace import data_root, game_file_dirs, game_file_key, included_pages

    root = data_root(info)
    work = Path(work)
    if work.exists():
        shutil.rmtree(work)
    (work / "crops").mkdir(parents=True)
    d = load_dictionary(info)
    known = {}
    for name, e in d["icons"].items():
        p = icons_dir(info) / e.get("file", f"{name}.png")
        if p.exists():
            known[name] = shape_pair(load_png(p))
    res = ScanResult()
    n = 0
    for fd in game_file_dirs(info):
        inc = set(included_pages(fd))
        ext = load_extracted(fd)
        todo = [(p, r, v["source"]) for p, regs in sorted(ext.items()) if p in inc
                for r, v in regs.items() if isinstance(v.get("source"), str) and PLACEHOLDER in v["source"]]
        if not todo:
            continue
        proj = load_project(fd)
        src = next((fd / "source").glob("*.pdf"))
        doc = pymupdf.open(src)
        try:
            for pno, rname, text in todo:
                img = region_image(doc[pno - 1], {r.name: r for r in proj.template.regions_for(pno)}[rname])
                tokens = list(TOKEN_RE.finditer(text))
                ph = [i for i, t in enumerate(tokens) if t.group(1) == "?"]
                auto_map, left, strips = {}, [], []
                for k, ti in enumerate(ph):
                    n += 1
                    oc = Occurrence(f"o{n:03d}", str(fd.relative_to(root)), game_file_key(fd), pno, rname, k,
                                    text.replace("\n", " / "))
                    oc.line, oc.order, _nm = tokens_in(text)[ti]
                    box, why = locate(img, text, ti)
                    if box is None:
                        oc.reason = why
                    else:
                        # 아이콘 바로 앞·뒤(또는 위)에 인쇄된 숫자는 아이콘의 일부로: 그림에 넣고 원문에서 뺀다
                        num = number_near(text, tokens[ti].start(), tokens[ti].end())
                        if num is not None:
                            nb = absorb_number(img, box, num[0], num[2] - num[1])
                            if nb is not None:
                                box = nb
                                strips.append((num[1], num[2]))
                        rgba = cut_out(img, box)
                        png = work / "crops" / f"{oc.id}.png"
                        save_png(rgba, png)
                        oc.png = str(png)
                        sh = shape_pair(rgba)
                        if known:
                            name, s = max(((nm, same_icon(sh, ks)) for nm, ks in known.items()), key=lambda t: t[1])
                            if s >= SAME_SHAPE:
                                oc.match, oc.score = name, s
                    if oc.match:
                        auto_map[k] = oc.match
                        res.auto[oc.match] = res.auto.get(oc.match, 0) + 1
                    else:
                        left.append(oc)
                if strips:  # [[?]] 순서는 그대로라 아래 자동 이름 바꾸기와 겹치지 않는다
                    strip_in_extract(fd, pno, rname, text, strips)
                    stripped = strip_spans(text, strips).replace("\n", " / ")
                    for oc in left:
                        oc.context = stripped
                if auto_map:
                    apply_to_extract(fd, pno, rname, auto_map, note="아이콘 사전과 모양이 같아 자동으로 바꿈")
                for j, oc in enumerate(left):  # 자동으로 바꾼 것을 뺀 순서
                    oc.index = j
                    res.occurrences.append(oc)
        finally:
            doc.close()
    _group(res)
    _save_scan(work, res)
    return res


def _group(res: ScanResult) -> None:
    shapes = {o.id: shape_pair(load_png(Path(o.png))) for o in res.occurrences if o.png}
    reps: list[tuple[str, str]] = []  # (묶음, 대표 occurrence id)
    for o in res.occurrences:
        if not o.png:
            o.group = f"위치 못 찾음 {o.id}"
            res.groups[o.group] = {"rep": "", "count": 1}
            continue
        g = next((g for g, rid in reps if same_icon(shapes[o.id], shapes[rid]) >= SAME_SHAPE), None)
        if g is None:
            g = f"g{len(reps) + 1:02d}"
            reps.append((g, o.id))
            res.groups[g] = {"rep": o.id, "count": 0}
        o.group = g
        res.groups[g]["count"] += 1
        # 대표는 가장 큰(선명한) 조각
        rep = res.groups[g]["rep"]
        if _area(o.png) > _area(next(x.png for x in res.occurrences if x.id == rep)):
            res.groups[g]["rep"] = o.id


def _area(png: str) -> int:
    a = load_png(Path(png))
    return int(a.shape[0] * a.shape[1])


def _save_scan(work: Path, res: ScanResult) -> None:
    data = {"occurrences": [o.__dict__ for o in res.occurrences], "groups": res.groups, "auto": res.auto}
    (work / "scan.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_scan(work: Path) -> ScanResult:
    data = json.loads((Path(work) / "scan.json").read_text(encoding="utf-8"))
    return ScanResult([Occurrence(**o) for o in data["occurrences"]], data["groups"], data.get("auto", {}))


# ---------------------------------------------------------------- 이름 제안 (모델)
SUGGEST_SYSTEM = """You name small icons printed inside board game card text, for a translation tool.
For each icon image, give a short identifier (lowercase English letters, digits or _ only, e.g. coin, heart,
sword), what the icon stands for in the original language of the card text (meaning), and a natural Korean name
(ko) used when the icon replaces a noun in a Korean sentence. Use the sentence where it appears as context."""


def _suggest_schema() -> dict:
    return {"type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"group": {"type": "string"}, "name": {"type": "string"},
                       "meaning": {"type": "string"}, "ko": {"type": "string"}},
        "required": ["group", "name", "meaning", "ko"], "additionalProperties": False}}},
        "required": ["items"], "additionalProperties": False}


def suggest_names(res: ScanResult, client, model: str, work_dir: Path) -> tuple[float, float]:
    """묶음마다 이름·뜻·한국어 이름을 제안 받아 res.groups에 넣는다."""
    from app.llm import complete_json

    by_id = {o.id: o for o in res.occurrences}
    lines, images = [], []
    for g, info in res.groups.items():
        if not info.get("rep"):
            continue
        o = by_id[info["rep"]]
        images.append(Path(o.png))
        lines.append(json.dumps({"group": g, "image": Path(o.png).name, "sentence": o.context}, ensure_ascii=False))
    if not lines:
        return 0.0, 0.0
    prompt = "Icons (JSON lines; open each image file):\n" + "\n".join(lines)
    data, api, eq = complete_json(client, model, SUGGEST_SYSTEM, prompt, _suggest_schema(), work_dir, images=images)
    for it in data.get("items", []):
        g = res.groups.get(it["group"])
        if g is not None:
            nm = re.sub(r"[^a-z0-9_]", "_", it["name"].strip().lower()).strip("_")
            g.update(suggest=nm, meaning=it["meaning"], ko=it["ko"])
    return api, eq


# ---------------------------------------------------------------- 검증 엑셀
REVIEW_HEADERS = ["묶음", "그림", "제안 이름", "이름", "뜻(원문)", "한국어 이름", "처리", "표시 크기", "등장 횟수", "예문"]


def export_review(info, res: ScanResult, out: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "아이콘"
    ws.append(REVIEW_HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True)
    col = {h: i + 1 for i, h in enumerate(REVIEW_HEADERS)}
    by_id = {o.id: o for o in res.occurrences}
    known = sorted(load_dictionary(info)["icons"])
    yellow = PatternFill("solid", fgColor="FFE066")
    r = 1
    for g, gi in res.groups.items():
        r += 1
        members = [o for o in res.occurrences if o.group == g]
        ex = "\n".join(f"{o.file_key} p{o.page} {o.region}: {o.context}" for o in members[:3])
        ws.cell(r, col["묶음"], g)
        ws.cell(r, col["제안 이름"], gi.get("suggest") or None)
        ws.cell(r, col["이름"], gi.get("suggest") or None)
        ws.cell(r, col["뜻(원문)"], gi.get("meaning") or None)
        ws.cell(r, col["한국어 이름"], gi.get("ko") or None)
        ws.cell(r, col["표시 크기"], DEFAULT_SCALE)
        ws.cell(r, col["등장 횟수"], len(members))
        ws.cell(r, col["예문"], ex).alignment = Alignment(wrap_text=True, vertical="top")
        if gi.get("rep"):
            img = XLImage(by_id[gi["rep"]].png)
            scale = 48 / max(img.height, 1)
            img.width, img.height = int(img.width * scale), int(img.height * scale)
            ws.add_image(img, f"B{r}")
            ws.row_dimensions[r].height = 42
        else:
            reason = members[0].reason if members else ""
            ws.cell(r, col["그림"], "없음").fill = yellow
            ws.cell(r, col["그림"]).comment = Comment(f"위치를 못 찾음: {reason}. 사전에 있는 이름을 쓰거나 "
                                                     f"‘{NOT_ICON}’으로 표시하세요", "CardTranslater")
    # 모든 등장 위치 (묶음 시트의 예문은 3개까지만 보이므로)
    wl = wb.create_sheet("위치")
    loc_head = ["묶음", "카드", "영역", "줄", "줄에서 순서", "그림", "위치 찾기", "원문"]
    wl.append(loc_head)
    for c in wl[1]:
        c.font = Font(bold=True)
    for i, o in enumerate(res.occurrences, start=2):
        wl.append([o.group, f"{o.file_key} p{o.page}", o.region, o.line or None, o.order or None, None,
                   "찾음" if o.png else f"못 찾음: {o.reason}", o.context])
        if o.png:
            img = XLImage(o.png)
            sc = 28 / max(img.height, 1)
            img.width, img.height = int(img.width * sc), int(img.height * sc)
            wl.add_image(img, f"F{i}")
            wl.row_dimensions[i].height = 24
        else:
            wl.cell(i, 7).fill = yellow
    for colw, w in zip("ABCDEFGH", (18, 26, 10, 5, 9, 8, 24, 70)):
        wl.column_dimensions[colw].width = w
    wl.freeze_panes = "A2"
    ws2 = wb.create_sheet("안내")
    for line in [
        "이름: 영문 소문자·숫자·_ 만. 같은 이름을 쓴 묶음은 하나의 아이콘으로 합칩니다.",
        f"처리: 아이콘이 아니면 ‘{NOT_ICON}’ (원문에서 뺍니다).",
        "사전에 이미 있는 이름을 쓰면 그 아이콘으로 바꿉니다 (그림은 사전 것을 씀).",
        "표시 크기: 한글 글자 높이 대비 (기본 1.1).",
        "이미 있는 이름: " + (", ".join(known) if known else "(없음)"),
    ]:
        ws2.append([line])
    for h, w in {"묶음": 18, "그림": 10, "제안 이름": 14, "이름": 14, "뜻(원문)": 16, "한국어 이름": 14,
                 "처리": 12, "표시 크기": 9, "등장 횟수": 9, "예문": 70}.items():
        ws.column_dimensions[chr(64 + col[h])].width = w
    out = Path(out)
    wb.save(out)
    return out


@dataclass
class IconImport:
    blocking: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    removed: int = 0
    replaced: int = 0


def import_review(info, xlsx: Path, work: Path) -> IconImport:
    """검증 엑셀을 읽어 사전에 넣고 원문 [[?]]를 바꾼다. 막는 문제가 있으면 아무것도 바꾸지 않는다."""
    from openpyxl import load_workbook

    ws = load_workbook(xlsx)["아이콘"]
    head = [c.value for c in ws[1]]
    rows = [dict(zip(head, r)) for r in ws.iter_rows(min_row=2, values_only=True) if any(r)]
    return import_rows(info, rows, work)


def import_rows(info, rows: list[dict], work: Path) -> IconImport:
    """검증 행(엑셀이나 화면): {"묶음", "이름", "뜻(원문)", "한국어 이름", "처리", "표시 크기"}."""
    res = load_scan(work)
    d = load_dictionary(info)
    out = IconImport()
    decision: dict[str, str] = {}  # 묶음 -> 이름 ('' = 아이콘 아님)
    meta: dict[str, dict] = {}
    for row in rows:
        g = str(row.get("묶음") or "")
        if g not in res.groups:
            out.blocking.append(f"모르는 묶음: {g} (다른 프로젝트·이전 검증 엑셀?)")
            continue
        if str(row.get("처리") or "").strip() == NOT_ICON:
            text = str(row.get("글자") or "").strip()
            decision[g] = f"={text}" if text else ""  # 글자를 적으면 그 글자로, 비우면 뺀다
            continue
        name = str(row.get("이름") or "").strip()
        if not name:
            out.blocking.append(f"{g}: 이름이 비어 있음 (아이콘이 아니면 처리 열에 ‘{NOT_ICON}’)")
            continue
        if not NAME_RE.match(name):
            out.blocking.append(f"{g}: 이름은 영문 소문자·숫자·_ 만 ({name})")
            continue
        if not res.groups[g].get("rep") and name not in d["icons"] and name not in [
                str(r.get("이름") or "").strip() for r in rows if res.groups.get(str(r.get("묶음") or ""), {}).get("rep")]:
            out.blocking.append(f"{g}: 그림이 없어 새 아이콘으로 만들 수 없음 (사전에 있는 이름을 쓰거나 ‘{NOT_ICON}’)")
            continue
        decision[g] = name
        m = meta.setdefault(name, {})
        for k_src, k in (("뜻(원문)", "meaning"), ("한국어 이름", "ko")):
            if row.get(k_src) and not m.get(k):
                m[k] = str(row[k_src]).strip()
        if row.get("표시 크기"):
            try:
                m["scale"] = float(row["표시 크기"])
            except (TypeError, ValueError):
                out.blocking.append(f"{g}: 표시 크기가 숫자가 아님 ({row['표시 크기']})")
    missing = [g for g in res.groups if g not in decision and not any(g in b for b in out.blocking)]
    for g in missing:
        out.blocking.append(f"{g}: 엑셀에 행이 없음")
    if out.blocking:
        return out
    # 사전에 넣기 (같은 이름은 하나로: 가장 큰 대표 그림)
    by_id = {o.id: o for o in res.occurrences}
    dst = icons_dir(info)
    for name, m in meta.items():
        groups = [g for g, n in decision.items() if n == name]
        reps = [by_id[res.groups[g]["rep"]].png for g in groups if res.groups[g].get("rep")]
        if name in d["icons"]:
            e = d["icons"][name]
            for k in ("meaning", "ko", "scale"):
                if m.get(k) is not None:
                    e[k] = m[k]
            out.reused.append(name)
        else:
            # 대표 그림: 가장 많이 나온 묶음의 그림 (합친 작은 묶음은 잘못 잘린 조각일 수 있음)
            with_rep = [g for g in groups if res.groups[g].get("rep")]
            top = max(with_rep, key=lambda g: (res.groups[g].get("count", 0), _area(by_id[res.groups[g]["rep"]].png)))
            best = by_id[res.groups[top]["rep"]].png
            save_png(load_png(Path(best)), dst / f"{name}.png")
            d["icons"][name] = {"file": f"{name}.png", "meaning": m.get("meaning", ""), "ko": m.get("ko", ""),
                                "scale": m.get("scale", DEFAULT_SCALE), "valign": "center",
                                "from_project": info.name}
            out.added.append(name)
        samples = dst / "samples" / name
        for p in reps:  # 카드별 원본 조각도 보관
            samples.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, samples / f"{info.name}_{Path(p).name}")
    save_dictionary(info, d)
    # 원문 바꾸기 (칸마다 한 번에)
    per_region: dict[tuple, dict[int, str]] = {}
    for o in res.occurrences:
        per_region.setdefault((o.file, o.page, o.region), {})[o.index] = decision[o.group]
    for (f, page, region), mapping in per_region.items():
        apply_to_extract(_occ_dir(info, f), page, region, mapping)
        out.removed += sum(1 for v in mapping.values() if v == "")
        out.replaced += sum(1 for v in mapping.values() if v)
    shutil.rmtree(work, ignore_errors=True)
    return out
