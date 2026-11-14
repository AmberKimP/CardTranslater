"""아이콘 찾기·묶기·사전 (PRD 5.6).

추출 모델은 문장 속 그림(아이콘)을 ``[[?]]``로 적는다. 여기서는

1. ``scan``: 모든 파일의 원문에서 ``[[?]]``를 찾아, 그 줄의 잉크에서 아이콘 위치를 찾고 배경을 투명하게 잘라 낸다.
   게임 아이콘 사전에 같은 모양이 있으면 바로 ``[[이름]]``으로 바꾼다(다음 프로젝트에서 재사용).
   나머지는 같은 모양끼리 묶어 ``아이콘_검증.xlsx``로 보여 준다(모델이 이름·뜻을 제안).
2. ``import_review``: 사용자가 이름을 확정하면 사전(``games/<게임>/icons/``)에 그림·뜻·한국어 이름을 저장하고
   원문의 ``[[?]]``를 ``[[이름]]``으로 바꾼다. ‘아이콘 아님’은 원문에서 뺀다. 같은 이름을 쓴 묶음은 하나로 합친다.

아이콘 옆에 인쇄된 숫자(``2[[?]]``)는 그림에 함께 자르고, 원문에서는 확정할 때(또는 사전과 모양·숫자가 같아
자동으로 바꿀 때)만 뺀다. 뺀 숫자는 칸의 ``icon_numbers``에 적어 두어 ‘다시 정하기’가 되살린다.
숫자가 다르면 모양이 같아도 다른 아이콘이다 (모양 비교는 작은 숫자를 거의 보지 못한다).

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

from app import workspace

PLACEHOLDER = "[[?]]"
PLACEHOLDER_RE = re.compile(r"\[\[\?\]\]")
TOKEN_RE = re.compile(r"\[\[([^\[\]]*)\]\]")
NAME_RE = re.compile(r"^[a-z0-9_]+$")
CROP_DPI = 600
SAME_SHAPE = 0.80  # 알파 모양 상관계수가 이 이상이면 같은 아이콘 후보
SAME_GRAY = 0.82  # 밝기 무늬 상관이 이 이상이면 같은 아이콘 (같은 타일 2: 0.83~0.91, 타일 1·2: 0.78 이하)
SAME_INNER = 0.82  # 가운데 내용 상관 (같은 아이콘 0.86 이상, 동전 3·5 0.78, 상자 안 동전·병 0.37)
SAME_DETAIL = 0.90  # 그리고 64칸으로 자세히 본 상관계수도 이 이상 (숫자만 다른 타일 1·2를 가른다)
FLAT_INNER_STD = 10.0  # 가운데 밝기 표준편차(0~255)가 이보다 작으면 ‘안이 밋밋한’ 아이콘 (민무늬 금화 5~7, 무늬 있는 것 12 이상)
FLAT_DETAIL = 0.88  # 밋밋한 아이콘끼리는 밝기 무늬 대신 자세한 모양(64칸)과
FLAT_COLOR_DIST = 25.0  # 색으로 본다 (같은 금화 스캔 2~11, 다른 동전 70 이상)
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


def _density(comps, y0, y1) -> float:
    cs = [c for c in comps if y0 <= c[1] + c[3] / 2 <= y1]
    if not cs:
        return 0.0
    x0, x1 = min(c[0] for c in cs), max(c[0] + c[2] for c in cs)
    return sum(c[4] for c in cs) / max(1, (x1 - x0) * (y1 - y0))


def _band_core(comps, y0, y1) -> tuple[int, int, list] | None:
    """띠 안의 글자·아이콘 크기 덩어리(속이 찬 것)만의 세로 범위. 배경 그림의 가는 선·점은 뺀다."""
    cs = [c for c in comps if y0 <= c[1] + c[3] / 2 <= y1 and c[4] >= 0.15 * c[2] * c[3]]
    if not cs:
        return None
    hmax = max(c[3] for c in cs)
    core = [c for c in cs if c[3] >= 0.5 * hmax]
    return min(c[1] for c in core), max(c[1] + c[3] for c in core), core


def _trim_to_core(bands: list, comps) -> list:
    """글자 줄에 배경 그림(집 그림 선 등)이 위아래로 붙어 길어진 띠를 글자·아이콘 크기 덩어리 범위로 줄인다
    (붙은 그림 조각이 글자 칸으로 세어져 아이콘 자리를 잘못 짚지 않게)."""
    out = []
    for b in bands:
        c = _band_core(comps, b[0], b[1])
        if c is not None and len(c[2]) >= 2 and c[1] - c[0] < 0.8 * (b[1] - b[0]):
            pad = int(0.08 * (c[1] - c[0]))
            nb = [max(b[0], c[0] - pad), min(b[1], c[1] + pad)]
            out.append(nb if isinstance(b, list) else tuple(nb))
        else:
            out.append(b)
    return out


def _drop_sparse(bands: list, comps) -> list:
    """잉크가 드문드문한 띠(배경 그림 조각만 모인 줄)는 글자 줄이 아니다.
    다만 글자 줄에 배경 그림(집 그림 선 등)이 붙어 띠가 커지고 드문드문해 보일 수 있다: 그때는 글자 크기 덩어리만으로
    다시 재어, 글자가 둘 이상 빽빽하면 그 범위로 줄여 살린다 (빼면 아래 줄들이 한 줄씩 밀려 짝지어진다)."""
    if len(bands) <= 1:
        return bands
    dens = [_density(comps, y0, y1) for y0, y1 in bands]
    if max(dens) <= 0:
        return bands
    ok = [d >= 0.06 or d >= 0.4 * max(dens) for d in dens]
    kept_h = sorted(b[1] - b[0] for b, k in zip(bands, ok) if k)
    hmed = kept_h[len(kept_h) // 2] if kept_h else 0
    out = []
    for b, d, k in zip(bands, dens, ok):
        if k:
            out.append(b)
            continue
        c = _band_core(comps, b[0], b[1])
        if c is None or len(c[2]) < 2 or c[1] - c[0] < 0.5 * hmed:
            continue
        dc = _density(c[2], c[0], c[1])
        if dc >= 0.06 or dc >= 0.4 * max(dens):
            out.append(type(b)([c[0], c[1]]) if isinstance(b, list) else (c[0], c[1]))
    return out


def _bands_from(comps: list[tuple[int, int, int, int, int]],
                rules: list[tuple[int, int]] = (), min_rel_h: float = 0.5) -> list[tuple[int, int]]:
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
    bands = _drop_sparse(_trim_to_core(bands, comps), comps)
    hmax = max(b[1] - b[0] for b in bands)
    bands = [b for b in bands if b[1] - b[0] >= hmax * 0.35]  # 띠 테두리 같은 가는 줄
    hmed = sorted(b[1] - b[0] for b in bands)[len(bands) // 2]
    return [(a, b) for a, b in bands if b - a >= hmed * min_rel_h]


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
    bands = _drop_sparse(_trim_to_core(bands, comps), comps)
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
        cs = [c for c in comps if y0 <= c[1] + c[3] / 2 <= y1]
        # 배경 그림의 가는 선 조각(낮거나 속이 빈 것)은 줄 길이에서 뺀다 (집 그림이 줄을 카드 폭만큼 늘리지 않게)
        solid = [c for c in cs if c[3] >= 0.35 * (y1 - y0) and c[4] >= 0.12 * c[2] * c[3]] or cs
        widths.append(max(x + w for x, _y, w, _h, _a in solid) - min(x for x, *_r in solid) if solid else 0)
    units = [_line_units(ln) for ln in lines]
    n_tok = [len(TOKEN_RE.findall(ln)) for ln in lines]
    icon_only = [bool(n) and not TOKEN_RE.sub("", ln).strip() for ln, n in zip(lines, n_tok)]

    def cost_of(pairs):  # [(이미지 줄, 원문 줄)]
        # 글 줄은 글자 수에 비례한 폭, 아이콘만 있는 줄은 그 줄 높이만 한 아이콘이 늘어선 폭으로 잰다
        # (큰 아이콘을 글자 하나 폭으로 치면 아이콘 줄과 카드 번호 줄을 헷갈린다)
        txt = [(b, j) for b, j in pairs if not icon_only[j]] or pairs
        r = sum(widths[b] for b, _j in txt) / max(sum(units[j] for _b, j in txt), 1e-6)
        err = 0.0
        for b, j in pairs:
            if icon_only[j]:
                h = bands[b][1] - bands[b][0]
                exp = n_tok[j] * h + (n_tok[j] - 1) * 0.25 * h
            else:
                exp = units[j] * r
            err += abs(widths[b] - exp)
        return err / max(sum(widths[b] for b, _j in pairs), 1)

    best, best_cost = None, float("inf")
    if nb > nl:
        for drop in combinations(range(nb), nb - nl):
            keep = [i for i in range(nb) if i not in drop]
            cost = cost_of(list(zip(keep, range(nl))))
            if cost < best_cost:
                best, best_cost = keep[line_no], cost
    else:
        for drop in combinations(range(nl), nl - nb):
            if line_no in drop:
                continue
            keep = [i for i in range(nl) if i not in drop]
            cost = cost_of(list(zip(range(nb), keep)))
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
        if len(cells) > len(seq):  # 줄 양 끝에 붙은 배경 그림 조각(집 그림 선 등)을 빼면 맞는가
            _hs = sorted(c[3] for c in comps if c[4] >= 30)
            cells = _trim_edge_scraps(cells, len(seq), float(_hs[len(_hs) // 2]) * 1.15 if _hs else em)
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
        # 원문이 한 칸으로 읽은 세트 아이콘(예: 큰 숫자 3 + 검, 검 + 1 타일)은 붙은 칸까지 하나로.
        # 칸 수가 원문과 꼭 맞아 순서로 짝지은 경우만 빼고 늘 본다 (배경 그림 조각 때문에 칸 수가 어긋나기도 한다)
        if len(cells) != len(seq) or colored is not None:
            box = _set_partner(box, cells, seq, tok_pos, em_line, em, img, lum, ink)
        bx0, by0, bx1, by1 = _grow_box(box, comps)
        # 아래·위 그림 조각이 붙어 길어진 칸은 그 줄 높이 근처로 자른다
        by0, by1 = max(by0, y0 - 0.4 * band_h), min(by1, y1 + 0.4 * band_h)
        box = (bx0, by0, bx1, by1)
        # 아이콘만 있는 줄은 아이콘이 글자보다 훨씬 클 수 있다 (줄 높이 = 아이콘 높이): 그 줄 높이로 크기를 잰다
        em = em if text_line else max(em, band_h / 2.2)
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
    if second[0] is not None:
        return second
    # 줄이 모자라면: 아이콘만 있는 카드에서는 제목 띠(プレイ時効果)가 아이콘 줄보다 낮아 ‘가는 줄’로 빠진다.
    # 높이로 빼지 않은 띠로 한 번 더 (줄 길이 짝 맞추기가 남는 줄을 고른다)
    loose = _bands_from(all_comps, rules, min_rel_h=0.3)
    if len(loose) > len(_bands_from(all_comps, rules)):
        third = _try(loose)
        if third[0] is not None:
            return third
    return first


def estimate(img: np.ndarray, text: str, index: int) -> tuple[int, int, int, int] | None:
    """locate가 못 찾은 아이콘의 대략적인 자리 (사람이 직접 잡을 때 길잡이). 줄 띠와 줄 안 글자 비율로 짐작한다."""
    try:
        lines = [ln for ln in text.split("\n") if ln.strip()]
        k = -1
        for li, ln in enumerate(lines):
            toks = list(TOKEN_RE.finditer(ln))
            if k + len(toks) >= index:
                tok = toks[index - k - 1]
                line_no, before = li, ln[: tok.start()]
                break
            k += len(toks)
        else:
            return None
        H, W = img.shape[:2]
        ink = _ink(img, H / max(1, len(lines)) * 0.6)
        comps, rules = _text_components(ink)
        bands = _bands_from(comps, rules)
        band = None
        if bands and len(bands) == len(lines):
            band = bands[line_no]
        elif bands:
            bi = _match_line(lines, line_no, bands, comps)
            if bi is None:  # 줄 수 비율로 가장 가까운 띠
                cy = (line_no + 0.5) / len(lines) * H
                bi = min(range(len(bands)), key=lambda i: abs((bands[i][0] + bands[i][1]) / 2 - cy))
            band = bands[bi]
        if band is None:
            band = (int(line_no / len(lines) * H), int((line_no + 1) / len(lines) * H))
        y0, y1 = band
        bh = max(8, y1 - y0)
        cs = [c for c in comps if y0 - bh * 0.2 <= c[1] + c[3] / 2 <= y1 + bh * 0.2]
        lx0, lx1 = (min(c[0] for c in cs), max(c[0] + c[2] for c in cs)) if cs else (0, W)
        seq = _line_cells(lines[line_no])
        pos = min(_token_cell_index(lines[line_no], before), max(0, len(seq) - 1))
        units = [1.1 if t == "■" else _units(t) for t in seq] or [1.0]
        unit = (lx1 - lx0) / max(sum(units), 1e-6)
        cx = lx0 + (sum(units[:pos]) + units[pos] / 2) * unit
        half = max(bh, unit) * 0.9  # 짐작이라 넉넉하게
        return (int(max(0, cx - half)), int(max(0, y0 - bh * 0.2)), int(min(W, cx + half)), int(min(H, y1 + bh * 0.2)))
    except Exception:  # noqa: BLE001 - 짐작은 없어도 된다
        return None


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


def _not_black_text(img, lum, ink, box) -> bool:
    """칸의 잉크가 검정 글자가 아니다: 회색·밝은 아이콘(검·숫자 장식)이거나 색이 있다."""
    if img is None:
        return False
    x0, y0, x1, y1 = (int(v) for v in box)
    m = ink[y0:y1, x0:x1]
    if m.sum() < 20:
        return False
    px = img[y0:y1, x0:x1][m].astype(np.float32)
    sat = (px.max(axis=1) - px.min(axis=1)) / np.maximum(px.max(axis=1), 1.0)
    return float(np.median(lum[y0:y1, x0:x1][m])) >= 85 or float(np.median(sat)) >= 0.25


def _trim_edge_scraps(cells: list, n_want: int, em_t: float) -> list:
    """칸이 원문보다 많을 때, 줄 양 끝에서 글자와 떨어진 작은·가는 칸들(배경 그림 조각)을 뺀다.
    빼서 원문 칸 수와 꼭 맞을 때만 쓴다 (아이콘끼리 붙은 줄도 순서대로 짝지을 수 있게)."""
    def small(c):
        return (c[2] - c[0]) < 0.5 * em_t or (c[3] - c[1]) < 0.3 * em_t

    out = list(cells)
    # 왼쪽 끝: 작은 칸이 이어진 묶음이 첫 글자와 떨어져 있으면 묶음째 뺀다
    i = 0
    while i < len(out) and small(out[i]):
        i += 1
    if 0 < i < len(out) and out[i][0] - out[i - 1][2] > 0.6 * em_t:
        out = out[i:]
    j = len(out)
    while j > 0 and small(out[j - 1]):
        j -= 1
    if 0 < j < len(out) and out[j][0] - out[j - 1][2] > 0.6 * em_t:
        out = out[:j]
    return out if len(out) == n_want else cells


def _set_partner(box, cells, seq, tok_pos: int, em_line: float, em: float, img=None, lum=None, ink=None):
    """이미지 칸이 원문보다 많을 때, 고른 아이콘 칸에 바짝 붙은 아이콘 칸이 있으면 합친다 (예: 큰 숫자 5 + 검).
    이웃 칸이 아이콘인지는 크기(보통 글자보다 확실히 큼)나 잉크(검정 글자가 아닌 회색·색)로 본다.
    원문에서 그쪽 이웃이 다른 아이콘이면 합치지 않는다 (따로 읽은 아이콘)."""
    bx0, _by0, bx1, _by1 = box
    i = next((j for j, c in enumerate(cells) if c[0] < bx1 and c[2] > bx0), None)
    if i is None:
        return box
    out = list(box)
    for side, j in (("right", i + 1), ("left", i - 1)):
        if not 0 <= j < len(cells):
            continue
        nb = seq[tok_pos + 1] if side == "right" and tok_pos + 1 < len(seq) else (
            seq[tok_pos - 1] if side == "left" and tok_pos > 0 else "")
        if nb == "■" or _DIGIT_CH.match(nb or " "):
            continue  # 원문에서 그쪽이 다른 아이콘이거나 숫자·기호(+2 등)면 그 칸은 그 글자다
        c = cells[j]
        # 세로는 고른 칸 근처까지만 (이웃 칸에 윗줄 글자가 붙어 있을 수 있다)
        h = out[3] - out[1]
        c = (c[0], max(c[1], int(out[1] - 0.35 * h)), c[2], min(c[3], int(out[3] + 0.35 * h)))
        gap = (c[0] - out[2]) if side == "right" else (out[0] - c[2])
        if img is None:  # 그림이 없으면(시험) 크기로만
            icon_like = c[3] - c[1] >= 1.2 * em_line
        else:  # 검정 글자는 크게 잡혀도(아래 그림 선이 붙음) 아이콘이 아니다: 회색·색 잉크여야 한다
            icon_like = c[3] - c[1] >= 0.8 * em_line and _not_black_text(img, lum, ink, c)
        if (0 <= gap + 2 and gap <= 0.35 * em_line and icon_like
                and max(out[2], c[2]) - min(out[0], c[0]) <= 3.0 * max(em, em_line)):
            out = [min(out[0], c[0]), min(out[1], c[1]), max(out[2], c[2]), max(out[3], c[3])]
            break
    return tuple(out)


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


def inner_contrast(rgba: np.ndarray, frac: float = 0.6) -> float:
    """가운데(60%) 밝기의 표준편차 (0~255). 작으면 안에 그림·숫자가 없는 밋밋한 아이콘."""
    import cv2

    al = rgba[:, :, 3] > 80
    ys, xs = np.nonzero(al)
    a = rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1] if len(xs) else rgba
    h, w = a.shape[:2]
    m0, m1 = int(h * (1 - frac) / 2), int(w * (1 - frac) / 2)
    a = a[m0:h - m0 or h, m1:w - m1 or w]
    g = a[:, :, :3].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    return float(cv2.GaussianBlur(g, (0, 0), max(1.0, h / 60)).std())


def shape_pair(rgba: np.ndarray):
    """(대강 모양 32칸, 자세한 모양 64칸, 밝기 무늬, 가운데 자세히, 가운데 밝기 차이)."""
    return shape_of(rgba), shape_of(rgba, 64), gray_feature(rgba), inner_feature(rgba), inner_contrast(rgba)


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
    if len(a) > 4 and len(b) > 4 and max(a[4], b[4]) < FLAT_INNER_STD:
        # 둘 다 안이 밋밋함(민무늬 금화 등): 밝기 무늬는 스캔 잡음뿐이라 비교할 수 없다 → 모양과 색으로
        s = similarity(a[0], b[0])
        ok = (s >= SAME_SHAPE and similarity(a[1], b[1]) >= FLAT_DETAIL
              and float(np.linalg.norm(a[0].color - b[0].color)) <= FLAT_COLOR_DIST)
        return s if ok else 0.0
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


def num_key(s: str | None) -> str:
    """숫자 비교용 (전각 ２ = 2)."""
    return unicodedata.normalize("NFKC", s or "").strip()


def entry_number(e: dict) -> str | None:
    """사전 항목이 그림에 넣은 숫자 ("" = 없음). 예전 항목(number 키 없음)은 None."""
    return num_key(e["number"]) if isinstance(e.get("number"), str) else None


def number_allows(entry_num: str | None, occ_num: str) -> bool:
    """사전 항목과 자리의 숫자가 맞아 자동으로 바꿔도 되는가.
    예전 항목(number 키 없음 = None)은 어떤 자리와도 자동으로 맞추지 않는다: 예전 찾기는 옆 숫자를 그림에 함께
    잘라 넣었을 수 있어, 숫자 없는 자리에 수량이 박힌 그림이 들어갈 수 있다. 그런 자리는 ② 검토에 사전 이름을
    제안(묶음의 "dict")으로만 올린다 (legacy_hint)."""
    return entry_num is not None and entry_num == occ_num


def apply_icons(text: str, mapping: dict[int, str], strip: dict[int, dict] | None = None,
                numbers: list | None = None) -> tuple[str, list[dict], int]:
    """i번째 [[?]]를 mapping대로 바꾸고, strip[i] = {"text", "side"}인 자리는 옆 숫자를 원문에서 뺀다.
    숫자는 지금 원문에서 다시 찾는다(사람이 그사이 지웠거나 다른 숫자면 빼지 않음).
    돌려줌 (새 원문, 칸의 icon_numbers, 이번에 뺀 숫자 수).
    icon_numbers = [{"index": 칸에서 몇 번째 [[…]] (0부터), "name", "text", "side", "sep"}] (‘다시 정하기’가 되살림).
    numbers = 칸에 이미 있던 icon_numbers (아이콘 아님으로 빠진 [[?]]만큼 번호를 당긴다)."""
    toks = list(TOKEN_RE.finditer(text))
    ph = [ti for ti, t in enumerate(toks) if t.group(1) == "?"]
    spans, found = [], {}
    for j, want in (strip or {}).items():
        name = mapping.get(j, "")
        if j >= len(ph) or not name or name.startswith("="):
            continue
        t = toks[ph[j]]
        hit = number_near(text, t.start(), t.end())
        if hit is None or hit[0] != want.get("side", hit[0]) or num_key(text[hit[1]:hit[2]]) != num_key(want.get("text")):
            continue
        sep = text[hit[2]:t.start()] if hit[0] == "before" else text[t.end():hit[1]]
        spans.append((hit[1], hit[2]))
        found[ph[j]] = {"name": name, "text": text[hit[1]:hit[2]], "side": hit[0], "sep": sep}
    # [[…]] 번호: ‘아이콘 아님’으로 빠지거나 글자가 된 [[?]] 뒤는 당겨진다
    new_idx, k = {}, 0
    for ti, t in enumerate(toks):
        if t.group(1) == "?":
            v = mapping.get(ph.index(ti))
            if v is not None and (v == "" or v.startswith("=")):
                continue
        new_idx[ti] = k
        k += 1
    out = strip_spans(text, spans) if spans else text
    out = replace_placeholders(out, mapping)
    nums = [{**e, "index": new_idx[e["index"]]} for e in (numbers or [])
            if isinstance(e, dict) and isinstance(e.get("index"), int) and e["index"] in new_idx]
    nums += [{"index": new_idx[ti], **v} for ti, v in sorted(found.items())]
    return out, nums, len(found)


def apply_region(file_dir: Path, page: int, region: str, mapping: dict[int, str],
                 strip: dict[int, dict] | None = None, note: str = "") -> str | None:
    """추출 결과 칸에 apply_icons를 적용해 저장한다. 돌려줌: 바뀐 원문(pages/ 파일 기준, 없으면 None)."""
    got = None
    for f in _extract_files(file_dir, page):
        rec = json.loads(f.read_text(encoding="utf-8"))
        r = rec["regions"].get(region)
        if not r or not isinstance(r.get("source"), str):
            continue
        src, nums, n_strip = apply_icons(r["source"], mapping, strip, r.get("icon_numbers"))
        r["source"] = src
        if nums:
            r["icon_numbers"] = nums
        else:
            r.pop("icon_numbers", None)
        if n_strip:
            r["icon_number_note"] = "아이콘 옆·위 숫자를 아이콘에 넣고 원문에서 뺌"
        if note:
            r["icon_note"] = note
        workspace._write(f, rec)  # 임시 파일로 쓰고 바꿔 넣기 (뒤에서 지우는 스레드가 읽는 중이면 다시 시도)
        if got is None:
            got = src
    return got


def restore_numbers(src: str, name: str, numbers: list | None) -> tuple[str, list[dict], int]:
    """[[name]]을 [[?]]로 되돌리며 icon_numbers에 적어 둔 숫자를 제자리에 되살린다.
    돌려줌 (새 원문, 남은 icon_numbers, 되돌린 수). [[…]] 수는 그대로라 다른 번호는 바뀌지 않는다."""
    toks = list(TOKEN_RE.finditer(src))
    by_idx = {e["index"]: e for e in (numbers or [])
              if isinstance(e, dict) and e.get("name") == name and isinstance(e.get("index"), int)}
    used, n, out = set(), 0, src
    for ti in reversed(range(len(toks))):
        t = toks[ti]
        if t.group(1) != name:
            continue
        n += 1
        rep = PLACEHOLDER
        e = by_idx.get(ti)
        if e and e.get("text"):
            sep = e.get("sep", "")
            rep = f"{e['text']}{sep}{PLACEHOLDER}" if e.get("side") == "before" else f"{PLACEHOLDER}{sep}{e['text']}"
            used.add(ti)
        out = out[:t.start()] + rep + out[t.end():]
    rest = [e for e in (numbers or []) if not (isinstance(e, dict) and e.get("index") in used and e.get("name") == name)]
    return out, rest, n


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
                    # 아이콘에 넣으며 원문에서 뺀 숫자도 제자리에 되살린다 (2[[상자]] -> 2[[?]])
                    r["source"], rest, k = restore_numbers(src, name, r.get("icon_numbers"))
                    if rest:
                        r["icon_numbers"] = rest
                    else:
                        r.pop("icon_numbers", None)
                    r["icon_note"] = f"아이콘 다시 정하기 ([[{name}]] 되돌림)"
                    changed = True
                    if f.parent.name == "pages":
                        n += k
            if changed:
                workspace._write(f, rec)  # 임시 파일로 쓰고 바꿔 넣기 (뒤에서 지우는 스레드가 읽는 중이면 다시 시도)
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
    box: list = field(default_factory=list)  # 영역 이미지(CROP_DPI)에서 잘라 낸 자리 [x0, y0, x1, y1]
    manual: bool = False  # 사람이 카드 보기에서 직접 잡은 자리
    # 그림에 함께 넣은 옆 숫자 {"text", "side"}. 원문에서는 확정할 때 뺀다 (그때 원문에서 다시 찾음)
    number: dict = field(default_factory=dict)
    hint: str = ""  # 모양이 같은 예전 사전 항목 이름 (자동으로 바꾸지 않고 ② 검토에 제안만)
    # 붙어 있는 [[?]]들이 그림에서 한 덩어리(세트)로 잡혔을 때, 이 자리가 들어간 첫 자리 id (확정하면 원문에서 뺀다)
    set_of: str = ""
    set_name: str = ""  # 세트 첫 자리가 사전으로 바로 바뀐 경우 그 이름 (이 자리는 표에 보여 확인받는다)

    @property
    def num(self) -> str:
        """묶기·사전 비교에 쓰는 숫자: 손으로 잡은 자리는 숫자를 원문에 남기므로 ""."""
        return "" if self.manual else num_key((self.number or {}).get("text"))


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
    known = _known_shapes(info)
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
                run_of = {k: ri for ri, run in enumerate(placeholder_runs(text)) for k in run}
                located = [locate(img, text, ti) for ti in ph]
                clash = shared_boxes([b for b, _w in located], [run_of.get(k) for k in range(len(ph))])
                set_host: dict[tuple, Occurrence] = {}  # (덩어리, 찾은 자리) -> 그 자리를 처음 찾은 아이콘
                set_n: dict[tuple, int] = {}  # 그 자리에 들어간 아이콘 수
                auto_map, auto_strip, left = {}, {}, []
                for k, ti in enumerate(ph):
                    n += 1
                    oc = Occurrence(f"o{n:03d}", str(fd.relative_to(root)), game_file_key(fd), pno, rname, k,
                                    text.replace("\n", " / "))
                    oc.line, oc.order, _nm = tokens_in(text)[ti]
                    box, why = located[k]
                    if k in clash:
                        # 글자를 사이에 둔 다른 [[?]]와 같은 자리로 잡힘: 어느 쪽이 맞는지 모른다. 그대로 두면 서로 다른 아이콘이
                        # 한 그림으로 사전에 자동으로 맞춰지거나(사교·큐브·왕관 5개가 모두 crown_social) 한 묶음이 된다
                        box, why = None, SHARED_BOX_REASON
                    key = (run_of.get(k), tuple(box)) if box is not None else None
                    host = set_host.get(key) if key else None
                    if host is not None and set_n[key] > 0 and set_box_fits(box, set_n[key] + 1):
                        set_n[key] += 1
                        # 붙어 있는 [[?]]가 앞 아이콘과 같은 덩어리: 아이콘 여러 개가 붙은 한 세트 (그 그림 하나로 보여 주고
                        # 이 자리는 원문에서 뺀다. 둘 다 남기면 세트 그림이 두 번 들어간다)
                        oc.set_of, oc.box = host.id, list(host.box)
                        oc.set_name = host.match  # 첫 자리가 사전으로 바뀌었으면 이 자리는 표에서 확인받는다
                        left.append(oc)
                        continue
                    if host is not None:
                        # 앞 아이콘과 같은 자리로 잡혔는데 세트 모양이 아님(또는 세트가 끝남): 찾기 실수로 보고 직접 잡게 한다
                        # (같은 그림을 잘라 두면 앞 아이콘과 한 묶음이 되어 같은 이름이 두 번 들어간다)
                        set_n[key] = 0
                        box, why = None, "앞 아이콘과 같은 자리로 잡혀 위치를 못 찾음"
                    if box is None:
                        oc.reason = why
                    else:
                        if key not in set_host:
                            set_host[key], set_n[key] = oc, 1
                        # 아이콘 바로 앞·뒤(또는 위)에 인쇄된 숫자는 아이콘의 일부로 그림에 넣는다. 원문에서 빼는 것은
                        # 사람이 확정할 때 (손으로 다시 잡거나 ‘아이콘 아님’이면 숫자를 남겨야 하므로)
                        num = number_near(text, tokens[ti].start(), tokens[ti].end())
                        if num is not None:
                            nb = absorb_number(img, box, num[0], num[2] - num[1])
                            if nb is not None:
                                box = nb
                                oc.number = {"text": text[num[1]:num[2]], "side": num[0]}
                        oc.box = [int(v) for v in box]
                        rgba = cut_out(img, box)
                        png = work / "crops" / f"{oc.id}.png"
                        save_png(rgba, png)
                        oc.png = str(png)
                        sh = shape_pair(rgba)
                        oc.match, oc.score = best_known(sh, known, oc.num)
                        if not oc.match:
                            oc.hint = legacy_hint(sh, known, oc.num)
                    if oc.match:
                        auto_map[k] = oc.match
                        if oc.number:
                            auto_strip[k] = oc.number
                        res.auto[oc.match] = res.auto.get(oc.match, 0) + 1
                    else:
                        left.append(oc)
                if auto_map:  # 사전과 모양·숫자가 같은 것: 바로 바꾸고 그 숫자는 원문에서 뺀다
                    now = apply_region(fd, pno, rname, auto_map, auto_strip,
                                       note="아이콘 사전과 모양이 같아 자동으로 바꿈")
                    if now is not None:
                        for oc in left:
                            oc.context = now.replace("\n", " / ")
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
    by_id = {o.id: o for o in res.occurrences}
    reps: list[tuple[str, str]] = []  # (묶음, 대표 occurrence id)
    for o in res.occurrences:
        if o.set_of:  # 세트의 나머지 자리: 표에 따로 보이지 않고 확정할 때 원문에서 뺀다
            o.group = f"세트 {o.id}"
            res.groups[o.group] = {"rep": "", "count": 1, "not_icon": True, "absorbed": o.set_of, "auto": True}
            if o.set_name:  # 첫 자리가 사전으로 바로 바뀜: 이 자리는 표에 보여 확인받는다 (되돌릴 수 있게)
                res.groups[o.group].update(auto=False, absorbed_name=o.set_name)
            continue
        if not o.png:
            o.group = f"위치 못 찾음 {o.id}"
            res.groups[o.group] = {"rep": "", "count": 1}
            continue
        # 옆 숫자가 다르면 모양이 같아도 다른 묶음 (상자+2·상자+5)
        g = next((g for g, rid in reps if by_id[rid].num == o.num
                  and same_icon(shapes[o.id], shapes[rid]) >= SAME_SHAPE), None)
        if g is None:
            g = f"g{len(reps) + 1:02d}"
            reps.append((g, o.id))
            res.groups[g] = {"rep": o.id, "count": 0}
        o.group = g
        res.groups[g]["count"] += 1
        if o.hint and not res.groups[g].get("dict"):  # 예전 사전 항목: 화면이 그 이름을 골라 둔다 (사람이 확인)
            res.groups[g]["dict"] = o.hint
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
    keys = set(Occurrence.__dataclass_fields__)  # 예전 scan.json(number 없음)도, 모르는 키가 있어도 읽힌다
    occ = []
    for o in data["occurrences"]:
        oc = Occurrence(**{k: v for k, v in o.items() if k in keys})
        if isinstance(oc.number, dict):  # 1.12.0 scan.json의 start·end는 쓰지 않는다
            oc.number = {k: oc.number[k] for k in ("text", "side") if k in oc.number}
        else:
            oc.number = {}
        occ.append(oc)
    return ScanResult(occ, data["groups"], data.get("auto", {}))


# ---------------------------------------------------------------- 사람이 아이콘 자리 잡기 (카드 보기)
def _occ_region(info, o: Occurrence) -> tuple[Path, np.ndarray, str]:
    """그 자리의 (파일 폴더, 영역 이미지, 지금 원문)."""
    import pymupdf

    from app.extract import load_extracted
    from app.project import load_project

    fd = _occ_dir(info, o.file)
    proj = load_project(fd)
    reg = next(r for r in proj.template.regions_for(o.page) if r.name == o.region)
    with pymupdf.open(next((fd / "source").glob("*.pdf"))) as doc:
        img = region_image(doc[o.page - 1], reg)
    src = (load_extracted(fd).get(o.page, {}).get(o.region) or {}).get("source")
    return fd, img, src if isinstance(src, str) else ""


def place_info(info, work: Path, occ_id: str) -> dict:
    """카드 보기에서 자리를 잡는 데 필요한 것: {"image": RGB, "box": 지금 자리 | None, "guess": 예상 자리 | None,
    "marks": [{"box", "name" | "group", "occ"}]} (같은 칸의 다른 아이콘 = 이미 찾은 것). 좌표는 image px."""
    res = load_scan(work)
    o = next(x for x in res.occurrences if x.id == occ_id)
    _fd, img, text = _occ_region(info, o)
    toks = list(TOKEN_RE.finditer(text))
    ph = [ti for ti, t in enumerate(toks) if t.group(1) == "?"]  # 지금 원문의 j번째 [[?]] = index j인 자리
    by_index = {x.index: x for x in res.occurrences if (x.file, x.page, x.region) == (o.file, o.page, o.region)}
    out = {"image": img, "box": None, "guess": None, "marks": []}
    for ti, t in enumerate(toks):
        if t.group(1) == "?":
            j = ph.index(ti)
            x = by_index.get(j)
            box = (x.box or (list(locate(img, text, ti)[0] or []) if x.png else [])) if x is not None else []
            if x is not None and x.id == o.id:
                out["box"] = box or None
                if not box:
                    out["guess"] = estimate(img, text, ti)
            elif x is not None and x.set_of:  # 세트의 나머지 자리는 첫 자리 상자와 같다
                continue
            elif box:
                out["marks"].append({"box": box, "group": x.group, "occ": x.id,
                                     **({"not_icon": True} if res.groups.get(x.group, {}).get("not_icon") else {})})
            else:  # 위치를 못 찾은 다른 자리도 예상 자리에 보여 준다 (빠뜨리지 않게)
                g = estimate(img, text, ti)
                if g is not None:
                    out["marks"].append({"box": list(g), "group": x.group if x is not None else "",
                                         "occ": x.id if x is not None else "", "guess": True,
                                         **({"not_icon": True} if x is not None
                                            and res.groups.get(x.group, {}).get("not_icon") else {})})
        else:
            box = locate(img, text, ti)[0]
            if box is None:  # 줄 배치로 못 찾으면 사전 그림으로 칸 안을 찾는다
                box = match_known(info, img, t.group(1), text)
            guess = False
            if box is None:
                box, guess = estimate(img, text, ti), True
            if box is not None:
                out["marks"].append({"box": list(box), "name": t.group(1), **({"guess": True} if guess else {})})
    return out


MATCH_KNOWN_MIN = 0.6  # 사전 그림으로 찾을 때 이 이상 닮아야 그 자리로 본다 (정규화 상관)


def match_known(info, img: np.ndarray, name: str, text: str) -> tuple[int, int, int, int] | None:
    """사전 아이콘 그림(name)을 칸 그림 안에서 찾는다 (여러 크기로 맞춰 봄). (x0, y0, x1, y1) 또는 None.
    크기는 글 줄 높이의 0.6~2.4배 사이에서 본다 (글 속 아이콘·아이콘만 있는 줄)."""
    import cv2

    e = load_dictionary(info)["icons"].get(name)
    if not e:
        return None
    pth = icons_dir(info) / e.get("file", f"{name}.png")
    if not pth.exists():
        return None
    tpl = load_png(pth)
    if tpl.ndim == 3 and tpl.shape[2] == 4:  # 투명한 곳은 칸 바탕(중간 회색)으로
        a = tpl[..., 3:4].astype(np.float32) / 255.0
        tpl = (tpl[..., :3].astype(np.float32) * a + 200.0 * (1 - a)).astype(np.uint8)
    g_img = cv2.cvtColor(np.ascontiguousarray(img[..., :3]), cv2.COLOR_RGB2GRAY)
    g_tpl = cv2.cvtColor(np.ascontiguousarray(tpl[..., :3]), cv2.COLOR_RGB2GRAY)
    n_lines = max(1, len([ln for ln in text.split("\n") if ln.strip()]))
    line_h = img.shape[0] / n_lines
    best, best_box = MATCH_KNOWN_MIN, None
    for f in np.linspace(0.6, 2.4, 13):
        h = int(round(line_h * f))
        w = int(round(g_tpl.shape[1] * h / max(1, g_tpl.shape[0])))
        if h < 8 or w < 8 or h > g_img.shape[0] or w > g_img.shape[1]:
            continue
        t = cv2.resize(g_tpl, (w, h), interpolation=cv2.INTER_AREA)
        if float(t.std()) < 4:
            continue
        r = cv2.matchTemplate(g_img, t, cv2.TM_CCOEFF_NORMED)
        _mn, mx, _l, loc = cv2.minMaxLoc(r)
        if mx > best:
            best, best_box = mx, (loc[0], loc[1], loc[0] + w, loc[1] + h)
    return best_box


def _known_shapes(info) -> dict:
    """사전 이름 -> (모양, 그림에 넣은 숫자 | None=예전 항목)."""
    d = load_dictionary(info)
    out = {}
    for name, e in d["icons"].items():
        p = icons_dir(info) / e.get("file", f"{name}.png")
        if p.exists():
            out[name] = (shape_pair(load_png(p)), entry_number(e))
    return out


def best_known(sh, known: dict, occ_num: str) -> tuple[str, float]:
    """사전에서 모양이 같은 아이콘 (이름, 점수). 옆 숫자가 다르면 후보가 아니다 (상자+2를 상자+5로 바꾸지 않게:
    모양 비교는 숫자를 거의 보지 못한다). 없으면 ("", 0)."""
    cand = [(nm, same_icon(sh, ks)) for nm, (ks, en) in known.items() if number_allows(en, occ_num)]
    nm, s = max(cand, key=lambda t: t[1], default=("", 0.0))
    return (nm, s) if s >= SAME_SHAPE else ("", 0.0)


def legacy_hint(sh, known: dict, occ_num: str) -> str:
    """모양이 같은 예전 사전 항목(number 키 없음) 이름: 자동으로 바꾸지 않고 ② 검토에 제안만 한다.
    옆 숫자가 있는 자리에는 제안하지 않는다 (예전 그림에 숫자가 들어 있는지 몰라 확정을 막으므로, _number_conflicts)."""
    if occ_num:
        return ""
    cand = [(nm, same_icon(sh, ks)) for nm, (ks, en) in known.items() if en is None]
    nm, s = max(cand, key=lambda t: t[1], default=("", 0.0))
    return nm if s >= SAME_SHAPE else ""


def set_manual_box(info, work: Path, occ_id: str, box: tuple[int, int, int, int]) -> dict:
    """사람이 잡은 자리로 그림을 다시 잘라 묶음을 다시 정한다.
    {"group": 새 묶음, "old": 예전 묶음, "old_gone": 예전 묶음이 없어짐, "new_group": 새로 만든 묶음, "dict": 사전 이름}"""
    work = Path(work)
    res = load_scan(work)
    o = next(x for x in res.occurrences if x.id == occ_id)
    _fd, img, _text = _occ_region(info, o)
    H, W = img.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x0, x1 = sorted((max(0, min(W, x0)), max(0, min(W, x1))))
    y0, y1 = sorted((max(0, min(H, y0)), max(0, min(H, y1))))
    if x1 - x0 < 4 or y1 - y0 < 4:
        raise ValueError("잡은 자리가 너무 작습니다")
    rgba = cut_out(img, (x0, y0, x1, y1))
    n = 1
    while (work / "crops" / f"{o.id}_m{n}.png").exists():  # 화면이 예전 그림을 들고 있지 않게 새 이름
        n += 1
    png = work / "crops" / f"{o.id}_m{n}.png"
    save_png(rgba, png)
    o.png, o.box, o.manual, o.reason = str(png), [x0, y0, x1, y1], True, ""
    o.set_of, o.set_name = "", ""  # 사람이 따로 잡았으면 이 자리는 더는 다른 아이콘의 세트 조각이 아니다
    for x in res.occurrences:  # 이 자리를 다시 잡으면 합친 세트를 푼다 (합칠지는 저장할 때 다시 묻는다)
        if x.set_of == o.id:
            res.groups.pop(x.group, None)
            x.set_of, x.set_name, x.box, x.group = "", "", [], f"위치 못 찾음 {x.id}"
            x.reason = "세트로 합쳤던 자리 (앞 아이콘을 다시 잡아 풀었음)"
            res.groups[x.group] = {"rep": "", "count": 1}
        elif x.id != o.id and res.groups.get(x.group, {}).get("absorbed") == o.id:  # 손으로 합친 자리
            for k in ("absorbed", "not_icon", "auto"):
                res.groups[x.group].pop(k, None)
    # 예전 묶음에서 빼기
    old = o.group
    gi = res.groups.get(old, {})
    gi["count"] = gi.get("count", 1) - 1
    old_gone = gi["count"] <= 0
    if old_gone:
        res.groups.pop(old, None)
    elif gi.get("rep") == o.id:
        rest = [x for x in res.occurrences if x.group == old and x.id != o.id and x.png]
        gi["rep"] = max(rest, key=lambda x: _area(x.png)).id if rest else ""
    # 모양이 같은 묶음에 넣거나 새 묶음
    by_id = {x.id: x for x in res.occurrences}
    sh = shape_pair(rgba)
    best, best_s = None, SAME_SHAPE
    for g, info_g in res.groups.items():
        rep = by_id.get(info_g.get("rep") or "")
        if rep is None or not rep.png or rep.num != o.num:  # 옆 숫자가 다르면 다른 아이콘
            continue
        s = same_icon(sh, shape_pair(load_png(Path(rep.png))))
        if s >= best_s:
            best, best_s = g, s
    known = _known_shapes(info)
    match = best_known(sh, known, o.num)[0] or legacy_hint(sh, known, o.num)  # 제안만 (자동으로 바꾸지 않음)
    if best is not None:
        g = best
        res.groups[g]["count"] += 1
        if _area(str(png)) > _area(by_id[res.groups[g]["rep"]].png):
            res.groups[g]["rep"] = o.id
        new_group = False
    else:
        nums = [int(k[1:]) for k in res.groups if re.fullmatch(r"g\d+", k)]
        g = f"g{max(nums, default=0) + 1:02d}"
        res.groups[g] = {"rep": o.id, "count": 1}
        if old_gone:  # 혼자 있던 줄을 옮긴 것: 적어 둔 제안·뜻은 그대로
            for k in ("suggest", "meaning", "ko"):
                if gi.get(k):
                    res.groups[g][k] = gi[k]
        if match:
            res.groups[g]["dict"] = match
        new_group = True
    o.group = g
    _save_scan(work, res)
    return {"group": g, "old": old, "old_gone": old_gone, "new_group": new_group, "dict": match, "box": [x0, y0, x1, y1]}


def set_not_icon(info, work: Path, occ_id: str) -> dict:
    """한 자리만 ‘아이콘 아님’: 그 자리를 묶음에서 빼 ‘아이콘 아님’으로 표시한 새 묶음(혼자)으로 옮긴다.
    (같은 묶음의 다른 자리는 아이콘인데 이 자리만 글자·장식을 잘못 잡은 경우. 묶음 줄의 ‘아이콘 아님’은 묶음 전체.)
    이미 혼자인 묶음이면 그 묶음을 ‘아이콘 아님’으로 표시만 한다. {"group", "old", "old_gone", "new_group"}"""
    work = Path(work)
    res = load_scan(work)
    o = next(x for x in res.occurrences if x.id == occ_id)
    old = o.group
    gi = res.groups.get(old, {})
    if gi.get("count", 1) <= 1:  # 혼자인 묶음: 옮길 것 없이 표시만
        gi["not_icon"] = True
        res.groups[old] = gi
        _save_scan(work, res)
        return {"group": old, "old": old, "old_gone": False, "new_group": False}
    gi["count"] -= 1
    if gi.get("rep") == o.id:
        rest = [x for x in res.occurrences if x.group == old and x.id != o.id and x.png]
        gi["rep"] = max(rest, key=lambda x: _area(x.png)).id if rest else ""
    nums = [int(k[1:]) for k in res.groups if re.fullmatch(r"g\d+", k)]
    g = f"g{max(nums, default=0) + 1:02d}"
    res.groups[g] = {"rep": o.id if o.png else "", "count": 1, "not_icon": True}
    o.group = g
    _save_scan(work, res)
    return {"group": g, "old": old, "old_gone": False, "new_group": True}


SET_ASPECT_PER_ICON = 0.8  # 세트로 볼 덩어리: 가로/세로가 아이콘 수 × 이 값 이상 (아이콘이 옆으로 늘어선 모양)


def set_box_fits(box, n: int) -> bool:
    """붙어 있는 [[?]] n개를 한 덩어리로 찾았을 때, 그 덩어리가 아이콘 n개가 옆으로 늘어선 모양인가.
    (아이콘 하나 크기인 덩어리에 이웃 [[?]]가 잘못 붙은 것은 세트가 아니다: 건물·상자 아이콘이 같은 자리로 잡힌 경우)"""
    w, h = box[2] - box[0], box[3] - box[1]
    return h > 0 and w / h >= SET_ASPECT_PER_ICON * n


SHARED_BOX_REASON = "다른 아이콘과 같은 자리로 잡혀 위치를 못 찾음"
SHARED_BOX_IOU = 0.5  # 두 자리가 이만큼 겹치면 같은 자리로 본다


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def shared_boxes(boxes: list, runs: list) -> set[int]:
    """같은 칸의 [[?]]들(순번 k) 중 글자를 사이에 둔 다른 [[?]]와 같은(거의 겹치는) 자리로 잡힌 것들의 순번.
    인쇄된 아이콘은 서로 다른 자리에 있으므로 이런 자리는 적어도 하나가 잘못 찾은 것인데 어느 쪽인지 모른다:
    모두 ‘위치 못 찾음’으로 돌려 사람이 ② 카드 보기에서 잡게 한다 (자동으로 사전 이름을 넣지 않는다).
    붙어 있는 [[?]](같은 덩어리 runs[k])끼리 같은 자리는 세트일 수 있어 여기서 보지 않는다 (scan이 따로 가린다)."""
    out: set[int] = set()
    for i, a in enumerate(boxes):
        if a is None:
            continue
        for j in range(i + 1, len(boxes)):
            b = boxes[j]
            if b is not None and runs[i] != runs[j] and _iou(a, b) >= SHARED_BOX_IOU:
                out.update((i, j))
    return out


def placeholder_runs(text: str) -> list[list[int]]:
    """[[?]]가 글자 없이 바로 붙어 있는 덩어리들. 각 덩어리는 [[?]] 순번(= 자리의 index) 목록.
    예: "[[?]][[?]][[?]]の山札" -> [[0, 1, 2]] (문양 셋이 붙은 아이콘을 모델이 셋으로 읽은 경우가 많다)."""
    runs: list[list[int]] = []
    j, prev_end = -1, None
    for t in TOKEN_RE.finditer(text or ""):
        if t.group(1) != "?":
            prev_end = None
            continue
        j += 1
        if runs and prev_end is not None and not text[prev_end:t.start()].strip():
            runs[-1].append(j)
        else:
            runs.append([j])
        prev_end = t.end()
    return runs


def run_partners(info, work: Path, occ_id: str) -> dict:
    """그 자리와 글자 없이 붙어 있는 다른 [[?]] 자리: {"boxed": [그림이 있는 자리 id], "unfound": [위치 못 찾은 자리 id]}.
    ‘아이콘 아님’으로 이미 뺀 자리는 넣지 않는다."""
    from app.extract import load_extracted

    res = load_scan(Path(work))
    o = next(x for x in res.occurrences if x.id == occ_id)
    src = (load_extracted(_occ_dir(info, o.file)).get(o.page, {}).get(o.region) or {}).get("source")
    run = next((r for r in placeholder_runs(src if isinstance(src, str) else "") if o.index in r), [])
    out = {"boxed": [], "unfound": []}
    for x in res.occurrences:
        if (x.file, x.page, x.region) != (o.file, o.page, o.region) or x.id == o.id or x.index not in run:
            continue
        if res.groups.get(x.group, {}).get("not_icon"):
            continue
        out["boxed" if x.png else "unfound"].append(x.id)
    return out


def absorb_into(info, work: Path, host_id: str, occ_ids: list[str]) -> list[str]:
    """붙어 있는 자리들을 host 아이콘 하나에 포함: 그 자리들은 ‘아이콘 아님’(뜻 비움 = 원문에서 뺌)으로 옮기고
    어느 아이콘에 들어갔는지 적어 둔다. 사람이 상자 하나로 문양 여러 개를 감싼 경우. 옮긴 묶음 목록."""
    work = Path(work)
    groups = []
    for oid in occ_ids:
        r = set_not_icon(info, work, oid)
        groups.append(r["group"])
    res = load_scan(work)
    for g in groups:
        res.groups[g]["absorbed"] = host_id
    _save_scan(work, res)
    return groups


OVERLAP_MIN = 0.4  # 겹친 넓이가 작은 쪽 상자의 이만큼이면 ‘겹친 아이콘’ (흡수 후보)


def _overlap_ratio(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / small if small > 0 else 0.0


def overlapping_occ(marks: list[dict], box, exclude: str = "") -> list[str]:
    """카드 보기 표시(place_info의 marks) 중 box와 겹친 다른 [[?]] 자리 id (이미 이름이 있는 [[이름]]은 빼고).
    한 아이콘을 두 [[?]]로 읽어 두 자리가 같은 그림을 가리키면, 한쪽에 흡수시켜 다른 쪽을 원문에서 뺀다."""
    if not box:
        return []
    return [m["occ"] for m in marks
            if m.get("occ") and m["occ"] != exclude and m.get("box") and not m.get("not_icon") and not m.get("guess")
            and _overlap_ratio(box, m["box"]) >= OVERLAP_MIN]


def split_icon(info, work: Path, occ_id: str, part_box) -> dict:
    """한 자리로 잡힌 상자 안에 아이콘이 둘이면 둘로 나눈다. part_box: 사람이 감싼 그중 하나.
    지금 상자에서 part_box를 뺀 나머지가 다른 하나다 (나란히: 가로로, 위아래: 세로로 가른다).
    원문에는 그 [[?]] 바로 뒤에 [[?]]를 하나 더 넣고 (읽는 순서: 왼쪽·위가 먼저), 새 자리를 만든다.
    돌려줌 {"first": 앞 자리 id, "second": 새 자리 id, "groups": [앞 묶음, 뒤 묶음]}. 나눌 수 없으면 ValueError."""
    from app.extract import load_extracted

    work = Path(work)
    res = load_scan(work)
    o = next((x for x in res.occurrences if x.id == occ_id), None)
    if o is None:
        raise ValueError("그 아이콘 자리를 찾지 못했습니다: ② 아이콘 정의를 다시 눌러 찾은 뒤 하세요")
    if not o.box:
        raise ValueError("이 아이콘은 아직 자리가 없습니다: 먼저 아이콘 둘을 함께 감싸 ‘이 자리로 저장’을 하세요")
    bx0, by0, bx1, by1 = o.box
    px0, py0, px1, py1 = (float(v) for v in part_box)
    # 지금 상자 밖으로 끈 부분은 잘라 낸다 (나중에 그림 끝에서 잘려 한쪽이 없어지지 않게: 원문을 바꾸기 전에 확인)
    px0, px1 = sorted((min(max(px0, bx0), bx1), min(max(px1, bx0), bx1)))
    py0, py1 = sorted((min(max(py0, by0), by1), min(max(py1, by0), by1)))
    pcx, pcy = (px0 + px1) / 2, (py0 + py1) / 2
    bcx, bcy = (bx0 + bx1) / 2, (by0 + by1) / 2
    # 감싼 상자가 지금 상자의 폭을 덜 차지하면 좌우로, 높이를 덜 차지하면 위아래로 가른다
    horizontal = (px1 - px0) / max(1, bx1 - bx0) <= (py1 - py0) / max(1, by1 - by0)
    if horizontal:
        part_first = pcx < bcx
        cut = px1 if part_first else px0
        a, b = ([bx0, by0, cut, by1], [cut, by0, bx1, by1])
    else:
        part_first = pcy < bcy
        cut = py1 if part_first else py0
        a, b = ([bx0, by0, bx1, cut], [bx0, cut, bx1, by1])
    if part_first:
        a = [px0, py0, px1, py1]
    else:
        b = [px0, py0, px1, py1]
    if min(a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1]) < 4:
        raise ValueError("나눈 한쪽이 너무 작습니다: 아이콘 하나만 감싸 주세요 (지금 상자 안에서)")
    # 원문: 그 [[?]] 뒤에 [[?]] 하나 더
    fd = _occ_dir(info, o.file)
    src = (load_extracted(fd).get(o.page, {}).get(o.region) or {}).get("source")
    if not isinstance(src, str):
        raise ValueError("이 칸의 원문을 찾지 못했습니다")
    ph = [t for t in TOKEN_RE.finditer(src) if t.group(1) == "?"]
    if o.index >= len(ph):
        raise ValueError("원문의 [[?]] 자리가 바뀌었습니다: ② 아이콘 정의를 다시 눌러 찾은 뒤 하세요")
    t = ph[o.index]
    tpos = [m.start() for m in TOKEN_RE.finditer(src)].index(t.start())  # 모든 [[…]] 중 몇째 (icon_numbers 순번)
    new_src = src[:t.end()] + "[[?]]" + src[t.end():]
    for f in _extract_files(fd, o.page):
        rec = json.loads(f.read_text(encoding="utf-8"))
        r = (rec.get("regions") or {}).get(o.region)
        if r is not None and r.get("source") == src:
            r["source"] = new_src
            r["icon_note"] = "카드 보기에서 아이콘 하나를 둘로 나눔"
            for e in r.get("icon_numbers") or []:  # 아이콘에 넣은 숫자의 자리: 뒤 아이콘들은 하나씩 밀린다
                if isinstance(e, dict) and isinstance(e.get("index"), int) and e["index"] > tpos:
                    e["index"] += 1
            workspace._write(f, rec)
    # 같은 칸의 뒤 자리들은 순번이 하나씩 밀린다
    same = [x for x in res.occurrences if (x.file, x.page, x.region) == (o.file, o.page, o.region)]
    for x in same:
        if x.index > o.index:
            x.index += 1
        if x.line == o.line and x.order > o.order:
            x.order += 1
        x.context = new_src.replace("\n", " / ")
    nums = [int(x.id[1:]) for x in res.occurrences if re.fullmatch(r"o\d+", x.id)]
    new_id = f"o{max(nums, default=0) + 1:03d}"
    g = f"위치 못 찾음 {new_id}"
    res.occurrences.insert(res.occurrences.index(o) + 1, Occurrence(
        new_id, o.file, o.file_key, o.page, o.region, o.index + 1, new_src.replace("\n", " / "),
        group=g, line=o.line, order=o.order + 1, reason="카드 보기에서 나눈 자리"))
    res.groups[g] = {"rep": "", "count": 1}
    _save_scan(work, res)
    r1 = set_manual_box(info, work, o.id, a)  # 두 자리 모두 그 상자로 다시 잘라 같은 모양끼리 다시 묶는다
    r2 = set_manual_box(info, work, new_id, b)
    return {"first": o.id, "second": new_id, "groups": [r1["group"], r2["group"]]}


def resolve_suggested_name(info, res: "ScanResult", group: str) -> str:
    """제안 받은 이름이 이미 있는 이름과 겹칠 때: 그림이 같으면 같은 아이콘으로(사전 아이콘이면 group["dict"]),
    다르면 뒤에 _2, _3 …을 붙여 다른 이름으로 바꾼다. 결과: "same_dict" | "same_row" | "renamed" | ""."""
    gi = res.groups.get(group) or {}
    name = (gi.get("suggest") or "").strip()
    by_id = {o.id: o for o in res.occurrences}
    rep = by_id.get(gi.get("rep") or "")
    if not name or rep is None or not rep.png:
        return ""
    sh = shape_pair(load_png(Path(rep.png)))
    known = _known_shapes(info)
    taken = set(load_dictionary(info)["icons"])
    if name in known and number_allows(known[name][1], rep.num) and same_icon(sh, known[name][0]) >= SAME_SHAPE:
        gi["dict"] = name  # 사전의 그 아이콘과 같음: 같은 아이콘으로 등록
        return "same_dict"
    for g, o_gi in res.groups.items():
        if g == group or o_gi.get("not_icon"):
            continue
        o_name = o_gi.get("suggest") or ""
        taken.add(o_name)
        if o_name == name and name not in known:
            o_rep = by_id.get(o_gi.get("rep") or "")
            if (o_rep is not None and o_rep.png and o_rep.num == rep.num
                    and same_icon(sh, shape_pair(load_png(Path(o_rep.png)))) >= SAME_SHAPE):
                return "same_row"  # 다른 줄과 같은 그림: 같은 이름 그대로 (확정할 때 한 아이콘이 된다)
    if name not in taken:
        return ""
    base = re.sub(r"_\d+$", "", name) or name
    k = 2
    while f"{base}_{k}" in taken:
        k += 1
    gi["suggest"] = f"{base}_{k}"  # 그림이 다른 아이콘: 다른 이름
    return "renamed"


# ---------------------------------------------------------------- 이름 제안 (모델)
SUGGEST_SYSTEM ="""You name small icons printed inside board game card text, for a translation tool.
For each icon image, give a short identifier (lowercase English letters, digits or _ only, e.g. coin, heart,
sword), what the icon stands for in the original language of the card text (meaning), and a natural Korean name
(ko) used when the icon replaces a noun in a Korean sentence. Use the sentence where it appears as context.
Each group is a visually different icon. A number, sign or small mark printed on or right next to the icon is
part of that icon: put it in the identifier (e.g. gold_2, coin_plus1, card_minus) and in meaning/ko. Give two
groups the same identifier only when their images are truly the same icon."""


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
        if gi.get("absorbed"):  # 세트에 포함된 자리: 따로 정할 것이 없다 (확정하면 원문에서 뺀다)
            continue
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
    # 아이콘 자리가 바뀐 칸 [(파일 폴더, 쪽, 칸)]: 손으로 다시 잡음·세트로 합침·아이콘 아님·위치 못 찾음.
    # 모델이 그 자리를 잘못 읽었을 수 있어 ③ 원문 확인에서 다시 읽고 확인해야 한다 (확인 필요로 표시해 둔다)
    changed_cells: list = field(default_factory=list)


def _group_num(res: ScanResult, by_id: dict, g: str) -> str:
    """묶음 대표 자리의 옆 숫자 (사전 항목 number로 적는 그대로의 글자, 손으로 잡은 자리는 "")."""
    o = by_id.get(res.groups.get(g, {}).get("rep") or "")
    return "" if o is None or o.manual else (o.number or {}).get("text", "")


def _number_conflicts(res: ScanResult, by_id: dict, decision: dict[str, str], d: dict) -> list[str]:
    """그림 속 숫자가 어긋나는 이름 정하기를 막는 문구들.
    - 옆 숫자가 다른 묶음(상자+2·상자+5)을 한 이름으로: 그림은 하나라 다른 묶음 자리에 다른 숫자가 함께 보인다.
    - 숫자가 있는 사전 항목과 숫자가 다른 묶음: 사전 그림의 숫자가 카드의 수량과 다르게 보인다.
    - 예전 사전 항목(number 키 없음)을 숫자 있는 묶음에: 예전 그림에 숫자가 들어 있는지 몰라 수량을 잃거나 겹친다.
    모두 숫자가 없으면 괜찮다. 그림 없는 묶음(위치 못 찾음)은 숫자를 모르므로 보지 않는다."""
    def shown(ns):
        return ", ".join(n or "숫자 없음" for n in ns)

    def named(name, n, old=""):  # crate_5를 숫자 2로 나누라고 할 때는 crate_2 (crate_5_2가 아니라)
        base = name[:-len(old) - 1] if old and name.endswith(f"_{old}") else name
        return f"{base}_{n}" if n else base

    out = []
    for name in dict.fromkeys(n for n in decision.values() if n and not n.startswith("=")):
        seen = [g for g, n in decision.items() if n == name and res.groups[g].get("rep")]
        nums = sorted({num_key(_group_num(res, by_id, g)) for g in seen})
        if len(nums) > 1:
            out.append(f"수량이 다른 아이콘을 같은 이름 {name}로 정할 수 없습니다 ({shown(nums)}): "
                       f"{'·'.join(named(name, n) for n in nums)}처럼 나눠 주세요")
            continue
        e = d["icons"].get(name)
        if e is None or not nums:
            continue
        en, n = entry_number(e), nums[0]
        if en is None:
            if n:
                out.append(f"사전의 예전 아이콘 {name}은 그림에 숫자가 들어 있는지 알 수 없어 숫자 {n}이 붙은 아이콘에 "
                           f"쓸 수 없습니다: {name}_{n}처럼 새 이름을 정해 주세요")
        elif en != n:
            out.append(f"수량이 다른 아이콘을 같은 이름 {name}로 정할 수 없습니다 (사전 그림: {shown([en])}, "
                       f"이 아이콘: {shown([n])}): {named(name, n, en) if n else '다른 이름'}처럼 새 이름을 정해 주세요")
    return out


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
        if res.groups[g].get("absorbed") and not str(row.get("이름") or "").strip()                 and not str(row.get("글자") or "").strip():
            decision[g] = ""  # 세트(옆 아이콘)에 포함된 자리: 원문에서 뺀다 (글자를 적었으면 아래에서 그 글자로)
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
    for g, gi in res.groups.items():  # 세트에 포함된 자리는 표·엑셀에 없어도 원문에서 뺀다
        if gi.get("absorbed") and g not in decision:
            decision[g] = ""
    missing = [g for g in res.groups if g not in decision and not any(g in b for b in out.blocking)]
    for g in missing:
        out.blocking.append(f"{g}: 엑셀에 행이 없음")
    by_id = {o.id: o for o in res.occurrences}
    out.blocking += _number_conflicts(res, by_id, decision, d)
    if out.blocking:
        return out
    # 사전에 넣기 (같은 이름은 하나로: 가장 큰 대표 그림)
    dst = icons_dir(info)
    for name, m in meta.items():
        groups = [g for g, n in decision.items() if n == name]
        reps = [by_id[res.groups[g]["rep"]].png for g in groups if res.groups[g].get("rep")]
        if name in d["icons"]:
            e = d["icons"][name]
            for k in ("meaning", "ko", "scale"):
                if m.get(k) is not None:
                    e[k] = m[k]
            seen = [g for g in groups if res.groups[g].get("rep")]
            # 예전 항목: 숫자 없는 묶음으로 확정하면 사람이 ② 창에서 사전 그림을 보고 고른 것이므로 number ""로 적는다.
            # 숫자 있는 묶음은 _number_conflicts가 막았다 (그림 속 숫자를 몰라 묶음 숫자를 적지도, 원문에서 빼지도 않는다).
            # 그림 있는 묶음이 없으면(위치 못 찾음만) 예전 항목 그대로 둔다
            if entry_number(e) is None and seen and all(num_key(_group_num(res, by_id, g)) == "" for g in seen):
                e["number"] = ""
            out.reused.append(name)
        else:
            # 대표 그림: 가장 많이 나온 묶음의 그림 (합친 작은 묶음은 잘못 잘린 조각일 수 있음)
            with_rep = [g for g in groups if res.groups[g].get("rep")]
            top = max(with_rep, key=lambda g: (res.groups[g].get("count", 0), _area(by_id[res.groups[g]["rep"]].png)))
            best = by_id[res.groups[top]["rep"]].png
            save_png(load_png(Path(best)), dst / f"{name}.png")
            d["icons"][name] = {"file": f"{name}.png", "meaning": m.get("meaning", ""), "ko": m.get("ko", ""),
                                "scale": m.get("scale", DEFAULT_SCALE), "valign": "center",
                                "number": _group_num(res, by_id, top),
                                "from_project": info.name}
            out.added.append(name)
        samples = dst / "samples" / name
        for p in reps:  # 카드별 원본 조각도 보관
            samples.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, samples / f"{info.name}_{Path(p).name}")
    save_dictionary(info, d)
    # 원문 바꾸기 (칸마다 한 번에)
    # 옆 숫자는 아이콘 이름으로 정했고 상자를 손으로 잡지 않았고 사전 그림의 숫자와 같을 때만 원문에서 뺀다
    # (‘아이콘 아님’·손으로 잡은 자리는 숫자를 남긴다. 숫자가 다른 묶음을 같은 이름으로 합치는 것은 _number_conflicts가
    # 막는다. 예전 항목(None)은 어떤 숫자와도 같지 않아 빼지 않는다)
    per_region: dict[tuple, dict[int, str]] = {}
    strips: dict[tuple, dict[int, dict]] = {}
    changed: dict[tuple, list[str]] = {}
    for o in res.occurrences:
        key = (o.file, o.page, o.region)
        name = decision[o.group]
        gi = res.groups.get(o.group, {})
        # 찾기가 스스로 합친 세트(auto)는 사람이 바꾼 것이 아니고 읽은 글자도 그대로라 다시 읽지 않는다
        why = ("손으로 자리를 다시 잡음" if o.manual else "세트로 합침" if (gi.get("absorbed") and not gi.get("auto"))
               else "" if (o.set_of or gi.get("auto")) else "아이콘 아님" if (not name or name.startswith("="))
               else "위치를 못 찾음" if not (o.box or o.png) else "")
        if why:
            changed.setdefault(key, []).append(why)
        per_region.setdefault(key, {})[o.index] = name
        e = d["icons"].get(name) if name and not name.startswith("=") else None
        if e is not None and o.number and not o.manual and entry_number(e) == o.num:
            strips.setdefault(key, {})[o.index] = o.number
    for (f, page, region), mapping in per_region.items():
        apply_region(_occ_dir(info, f), page, region, mapping, strips.get((f, page, region)))
        out.removed += sum(1 for v in mapping.values() if v == "")
        out.replaced += sum(1 for v in mapping.values() if v)
    for (f, page, region), whys in changed.items():
        fd = _occ_dir(info, f)
        mark_icon_changed(fd, page, region, sorted(set(whys)))
        out.changed_cells.append((fd, page, region))
    shutil.rmtree(work, ignore_errors=True)
    return out


ICON_CHANGED_NOTE = "② 아이콘 정의에서 아이콘 자리가 바뀜"


def mark_icon_changed(file_dir: Path, page: int, region: str, whys: list[str]) -> None:
    """아이콘 자리가 바뀐 칸을 ‘확인 필요’로 표시한다 (③ 원문 확인이 그 칸을 보여 주고, 다시 읽어 비교하게).
    사람이 원문 확인에서 저장하면 풀린다."""
    for f in _extract_files(file_dir, page):
        rec = json.loads(f.read_text(encoding="utf-8"))
        r = rec["regions"].get(region)
        if not r or not isinstance(r.get("source"), str):
            continue
        r["uncertain"] = True
        r["icon_changed"] = whys
        r["icon_reread"] = True  # 아직 다시 읽지 않음: ③ 원문 확인을 열 때 먼저 다시 읽는다 (다시 읽으면 풀림)
        note = f"{ICON_CHANGED_NOTE}: {', '.join(whys)} — 다시 읽어 확인하세요"
        r["note"] = note if not r.get("note") or ICON_CHANGED_NOTE in r["note"] else f"{r['note']} / {note}"
        r.pop("checked_by", None)
        workspace._write(f, rec)  # 임시 파일로 쓰고 바꿔 넣기 (뒤에서 지우는 스레드가 읽는 중이면 다시 시도)
