"""아이콘 찾기·묶기·사전 (PRD 5.6).

추출 모델은 문장 속 그림(아이콘)을 ``[[?]]``로 적는다. 여기서는

1. ``scan``: 모든 파일의 원문에서 아이콘 토큰(``[[?]]``와 이미 이름이 있는 ``[[이름]]``)을 찾아, 그 줄의 잉크에서
   아이콘 위치를 찾고 배경을 투명하게 잘라 낸다. 카드의 아이콘 하나가 한 줄이다. 게임 아이콘 사전에 같은 모양이 있으면
   그 이름을 줄에 미리 채워 두기만 한다(원문은 사람이 확정할 때 바꾼다). 같은 모양 줄끼리는 "shape"이 같아
   모델 이름 제안을 모양마다 한 번만 받는다. ``아이콘_검증.xlsx``도 줄마다 한 행.
2. ``import_review``: 사용자가 이름을 확정하면 사전(``games/<게임>/icons/``)에 그림·뜻·한국어 이름을 저장하고
   원문의 ``[[?]]``를 ``[[이름]]``으로 바꾼다. ‘아이콘 아님’은 원문에서 뺀다. 같은 이름을 쓴 줄은 한 아이콘(사전 한 항목)이다.
   이미 이름이 있던 줄은 이름을 바꾸거나 ‘아이콘 아님’일 때만 원문을 바꾼다.

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
from app.regions import text_only

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


def region_box_to_pt(page_rect, region, box) -> list[float] | None:
    """region_image 위 상자 [x0, y0, x1, y1](px)를 원본 쪽 좌표(pt)로. 90·270도 돌린 영역은 None."""
    from app.regions import ratio_to_pt

    if not box or len(box) != 4:
        return None
    pr = tuple(page_rect)
    x0, y0, x1, y1 = ratio_to_pt(region.rect, pr)
    x0, y0, x1, y1 = max(x0, pr[0]), max(y0, pr[1]), min(x1, pr[2]), min(y1, pr[3])
    k = 72.0 / CROP_DPI
    rot = int((region.style or {}).get("rotate", 0)) % 360
    bx0, by0, bx1, by1 = (float(v) * k for v in box)
    if rot == 0:
        return [round(x0 + bx0, 3), round(y0 + by0, 3), round(x0 + bx1, 3), round(y0 + by1, 3)]
    if rot == 180:
        return [round(x1 - bx1, 3), round(y1 - by1, 3), round(x1 - bx0, 3), round(y1 - by0, 3)]
    return None


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
        comps = [c for c in comps if not _cells._light_drawing(img, lum, ink, c)] or comps
        if not comps:
            return None, "그 줄에 잉크가 없음"
        seq = _cells._line_cells(lines[line_no])  # 원문 줄을 글자·아이콘 칸으로 (공백 제외)
        cells = _cells._glyph_cells(comps, em, (y0, y1))
        if len(cells) > len(seq):  # 줄 양 끝에 붙은 배경 그림 조각(집 그림 선 등)을 빼면 맞는가
            _hs = sorted(c[3] for c in comps if c[4] >= 30)
            cells = _cells._trim_edge_scraps(cells, len(seq), float(_hs[len(_hs) // 2]) * 1.15 if _hs else em)
        if len(cells) != len(seq):
            # 칸 수가 안 맞으면: 띠 테두리 같은 긴 가로선이 글자 밑동에 닿아 이웃 글자·아이콘이 붙었을 수 있다.
            # 긴 선을 지우고 다시 나눠 맞으면 그것을 쓴다 (아이콘 테두리가 조금 깎일 수 있어 위아래로 2px 여유)
            alt = _cells._glyph_cells(_cells._line_components(ink, em, (y0, y1), band_h), em, (y0, y1))
            if len(alt) == len(seq):
                cells = [(c[0], max(0, c[1] - 2), c[2], c[3] + 2) for c in alt]
        if not cells:
            return None, "그 줄에서 글자 칸을 찾지 못함"
        k = sum(1 for t in seq[: _cells._token_cell_index(lines[line_no], before)] if t == "\u25a0")
        tok_pos = [i for i, t in enumerate(seq) if t == "\u25a0"][k]
        n_tok = sum(1 for t in seq if t == "\u25a0")
        text_line = any(_cells._WORDY_CH.match(t) for t in seq)
        # 글이 있는 줄의 숫자는 검정 글자라 색 덩어리가 아니다: 아이콘만 세면 된다
        # 그 줄의 글자 높이 (줄 띠 높이는 큰 아이콘 줄·붙은 줄 때문에 커질 수 있다)
        hs_l = sorted(c[3] for c in comps if c[4] >= 30)
        em_line = min(em, float(hs_l[len(hs_l) // 2]) * 1.15) if hs_l else em
        colored = _cells._colored_icon_boxes(img, lum, ink, comps, em_line,
                                      [t for t in seq if not _cells._DIGIT_CH.match(t)] if text_line else seq,
                                      min_gray_fill=0.45 if text_line else 0.3)
        if colored is not None:  # 글자(검정)와 색이 다른 덩어리 = 아이콘. 수가 맞으면 순서대로 (가장 믿을 만함)
            box = colored[k]
        elif len(cells) == len(seq):  # 칸 수가 같으면 순서대로 짝짓는다 (가장 믿을 만함)
            box = cells[tok_pos]
        elif _cells._ordinal_icon_cell(cells, seq, tok_pos, band_h, em) is not None:
            # 칸 수는 달라도 아이콘 크기 칸의 수가 원문 아이콘 수와 같으면 순서대로 (같은 칸을 두 번 고르지 않는다)
            box = _cells._ordinal_icon_cell(cells, seq, tok_pos, band_h, em)
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
        if colored is None and text_line and _cells._line_color_design(img, ink, y0, y1) and _cells._looks_like_text(img, lum, ink, box):
            # 색 아이콘을 못 찾아 칸으로 짐작한 자리가 검정 글자처럼 보이면 틀린 자리다 (못 찾음으로 둔다)
            return None, "아이콘 자리로 짐작한 곳이 글자임"
        # 원문이 한 칸으로 읽은 세트 아이콘(예: 큰 숫자 3 + 검, 검 + 1 타일)은 붙은 칸까지 하나로.
        # 칸 수가 원문과 꼭 맞아 순서로 짝지은 경우만 빼고 늘 본다 (배경 그림 조각 때문에 칸 수가 어긋나기도 한다)
        if len(cells) != len(seq) or colored is not None:
            box = _cells._set_partner(box, cells, seq, tok_pos, em_line, em, img, lum, ink)
        bx0, by0, bx1, by1 = _cells._grow_box(box, comps)
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
        if by1 - by0 >= TALL_ICON * em and tall_box_problem(img, text, (bx0, by0, bx1, by1)):
            return None, "찾은 덩어리가 글자 두 줄 높이 (위아래 아이콘 두 개?)"
        # 줄 띠가 아래·위 그림·줄과 붙어 커지면(em이 큼) 위 검사가 빠진다: 상자 안에 다른 글 줄의 글자가 들어 있으면
        # 위아래 줄의 아이콘·글자까지 한 상자로 잡은 것이다 (공주와기사 3쪽 1줄 문양 셋 상자가 2줄 ‘マーカーを’까지,
        # 5쪽 1줄 백합 문장 상자가 2줄 ‘加える’까지 감쌈)
        if text_line:
            out_x = [c for c in comps if not (bx0 <= c[0] + c[2] / 2 <= bx1) and em_line * 0.5 <= c[3] <= em_line * 1.3]
            mids = sorted(c[1] + c[3] / 2 for c in out_x)
            line_mid = mids[len(mids) // 2] if mids else (y0 + y1) / 2
            if crosses_other_line(all_comps, (bx0, by0, bx1, by1), em_line, line_mid):
                return None, "찾은 덩어리가 위아래 두 줄에 걸침 (위아래 아이콘·글자까지 한 상자로 잡힘)"
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


TALL_ICON = 2.0  # 아이콘 상자 높이가 글자 높이의 이 배 이상이면 ‘큰 아이콘’ (아래 tall_box_problem)


def text_em(img: np.ndarray, text: str) -> float:
    """그 칸 글자 줄 높이 (줄 띠 높이의 중간값, 줄을 못 찾으면 그림 높이 / 원문 줄 수)."""
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    H = img.shape[0]
    ink = _ink(img, H / max(1, len(lines)) * 0.6)
    comps, rules = _text_components(ink)
    bands = _bands_from(comps, rules)
    if not bands:
        return H / max(1, len(lines))
    return float(sorted(b[1] - b[0] for b in bands)[len(bands) // 2])


def crosses_other_line(comps, box, em: float, line_mid: float) -> bool:
    """아이콘 상자 안에 그 줄(가운데 높이 line_mid)이 아닌 다른 글 줄의 글자가 들어 있는가.
    글자 = 높이 0.5~1.3 em이고 잉크가 찬 덩어리. 상자 안 글자의 높이가 그 줄에서 0.8 em 넘게 떨어져 있고, 같은 높이에
    상자 밖으로도 글자가 이어지면(= 진짜 글 줄) 위아래 줄까지 감싼 상자다. 상자 옆에만 글자가 있는 큰 아이콘(글 두 줄
    왼쪽·오른쪽)이나 아이콘 그림 속 작은 글씨(상자 밖으로 이어지지 않음)는 걸리지 않는다."""
    x0, y0, x1, y1 = (float(v) for v in box)
    glyphs = [(cx + cw / 2, cy + ch / 2) for cx, cy, cw, ch, a in comps
              if em * 0.5 <= ch <= em * 1.3 and a >= 0.12 * cw * ch]
    inside = [m for x, m in glyphs if x0 <= x <= x1 and y0 <= m <= y1 and abs(m - line_mid) > 0.8 * em]
    outside = [m for x, m in glyphs if not (x0 <= x <= x1)]
    return any(abs(o - m) <= 0.4 * em for m in inside for o in outside)


def tall_box_problem(img: np.ndarray, text: str, box) -> str:
    """글자 높이의 2배가 넘는 아이콘 상자가 아이콘 하나로 맞지 않으면 그 이유, 맞으면 "".
    큰 아이콘은 (1) 모든 글자의 왼쪽이나 오른쪽에 있거나 (2) 아이콘만 있는 줄을 다 차지할 때만 있다.
    그 밖에 (상자 안이나 양쪽에 글자가 있음) 두 줄 높이로 잡혔으면 위아래 아이콘 두 개(또는 글자까지)를 한 상자로 잡은 것이다."""
    x0, y0, x1, y1 = (float(v) for v in box)
    em = text_em(img, text)
    if y1 - y0 < TALL_ICON * em:
        return ""
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    ink = _ink(img, img.shape[0] / max(1, len(lines)) * 0.6)
    comps, _rules = _text_components(ink)
    left = right = inside = False
    for cx, cy, cw, ch, _a in comps:
        if ch > em * 1.3 or ch < em * 0.25:  # 글자 크기 덩어리만 (그림·점은 뺀다)
            continue
        mid_y = cy + ch / 2
        if not (y0 <= mid_y <= y1):
            continue
        if cx + cw <= x0 + 2:
            left = True
        elif cx >= x1 - 2:
            right = True
        elif x0 < cx + cw / 2 < x1:
            inside = True
    if inside or (left and right):
        return "아이콘 상자가 글자 두 줄 높이입니다 (위아래 아이콘 두 개나 글자까지 한 상자로 잡힌 듯함)"
    return ""


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
        seq = _cells._line_cells(lines[line_no])
        pos = min(_cells._token_cell_index(lines[line_no], before), max(0, len(seq) - 1))
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


BAND_MATCH_MIN = 0.62  # 사전 그림을 원문 줄 띠 안에서 찾을 때 이 이상 닮아야 그 자리 (정규화 상관)
BAND_MATCH_FAR = 0.75  # 줄 배치로 찾은 자리와 거의 겹치지 않는(IoU 0.3 미만) 자리는 이 이상 닮아야 옮긴다
BAND_SCALES = (0.9, 2.6)  # 띠에서 찾을 아이콘 높이: 그 줄 글자 높이의 이 배 사이


def templates_for(file_dir: Path) -> dict:
    """게임 아이콘 사전 그림 {이름: RGBA 배열} (지우기에서 글 속 아이콘 자리를 다시 확인할 때). 없으면 {}."""
    from app.workspace import project_of

    try:
        info = project_of(Path(file_dir))
    except Exception:  # noqa: BLE001 - 사전이 없어도 지우기는 한다
        return {}
    if info is None:
        return {}
    out = {}
    for name, e in (load_dictionary(info).get("icons") or {}).items():
        p = icons_dir(info) / e.get("file", f"{name}.png")
        if p.is_file():
            try:
                out[name] = _cells.load_png(p)
            except Exception:  # noqa: BLE001 - 깨진 그림은 건너뛴다
                continue
    return out


def _box_iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def match_in_band(img: np.ndarray, tpl: np.ndarray, band: tuple[float, float], glyph_h: float,
                  n: int = 1) -> list[tuple[float, tuple[int, int, int, int]]]:
    """사전 그림(tpl, RGBA/RGB)을 img(RGB)의 한 줄 띠(band=(y0, y1) px, 위아래로 글자 높이만큼 넓혀 봄) 안에서
    여러 크기로 찾는다. 닮은 정도가 BAND_MATCH_MIN 이상인 자리를 겹치지 않게 n개까지 [(점수, (x0, y0, x1, y1))],
    왼쪽부터."""
    import cv2

    H, W = img.shape[:2]
    gh = max(2.0, float(glyph_h))
    wy0, wy1 = max(0, int(band[0] - 1.2 * gh)), min(H, int(band[1] + 1.2 * gh) + 1)
    if wy1 - wy0 < 6:
        return []
    win = np.ascontiguousarray(img[wy0:wy1, :, :3])
    bgc = np.median(win.reshape(-1, 3), axis=0).astype(np.float32)  # 투명한 곳은 그 띠의 바탕색으로
    t = tpl.astype(np.float32)
    if t.ndim == 3 and t.shape[2] == 4:
        a = t[..., 3:4] / 255.0
        t = t[..., :3] * a + bgc * (1 - a)
    g_win = cv2.cvtColor(win, cv2.COLOR_RGB2GRAY)
    g_tpl = cv2.cvtColor(np.clip(t[..., :3], 0, 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    peaks: list[tuple[float, tuple[int, int, int, int]]] = []
    for f in np.linspace(BAND_SCALES[0], BAND_SCALES[1], 15):
        h = int(round(gh * f))
        w = int(round(g_tpl.shape[1] * h / max(1, g_tpl.shape[0])))
        if h < 6 or w < 6 or h > g_win.shape[0] or w > g_win.shape[1]:
            continue
        tt = cv2.resize(g_tpl, (w, h), interpolation=cv2.INTER_AREA)
        if float(tt.std()) < 4:
            continue
        r = cv2.matchTemplate(g_win, tt, cv2.TM_CCOEFF_NORMED)
        for _ in range(n + 2):
            _mn, mx, _l, loc = cv2.minMaxLoc(r)
            if mx < BAND_MATCH_MIN:
                break
            x, y = loc
            peaks.append((float(mx), (x, y + wy0, x + w, y + wy0 + h)))
            r[max(0, y - h // 2):y + h // 2 + 1, max(0, x - w // 2):x + w // 2 + 1] = -1.0
    peaks.sort(key=lambda p: -p[0])
    keep: list[tuple[float, tuple[int, int, int, int]]] = []
    for s, b in peaks:
        if all(_box_iou(b, k) < 0.3 for _s, k in keep):
            keep.append((s, b))
        if len(keep) >= n:
            break
    return sorted(keep, key=lambda p: p[1][0])


def refine_icon_boxes(img: np.ndarray, text: str, found: list, line_bands: list | None,
                      templates: dict) -> tuple[list, list[str]]:
    """줄 배치로 찾은 글 속 아이콘 자리(found = icon_boxes 결과)를 사전 그림으로 다시 확인한다.
    line_bands: 원문 글 줄마다 (y0, y1, 글자 높이) px — 지우기가 원문 줄에 맞춘 줄 (못 맞췄으면 None: 그대로 둔다).
    - 그 줄 띠에서 사전 그림이 뚜렷이 찾아지면 그 자리를 쓴다 (줄 배치가 그림 조각을 줄로 봐 엉뚱한 곳을 고른 경우).
      줄 배치로 못 찾은 아이콘은 더하지 않는다 (사전 그림이 큰 장식 그림의 일부를 잘라 낸 것이면 장식 반쪽만 지운다)
    - 사전 그림으로 못 찾았고 줄 배치 자리가 그 줄 띠 밖이면 버린다 (엉뚱한 그림을 지우지 않게)
    반환: (고친 목록, 바꾼 것 설명)."""
    if not line_bands or not templates or "[[" not in (text or ""):
        return found, []
    from app.erase_text import is_icon_line

    lines = [t for t in text.split("\n") if t.strip()]
    if len(lines) != len(line_bands):
        lines = [t for t in lines if not is_icon_line(t)]
        if len(lines) != len(line_bands):
            return found, []
    # icon_boxes는 원문 순서대로 찾은 것만 넣는다: 이름별로 순서대로 그 줄 몫을 나눈다.
    # 아이콘만 있는 줄의 것(only)은 넣지 않는다: 그 줄은 아래에서 건너뛰므로 넣으면 다음 글 줄이 그 몫을 가져간다
    by_name: dict[str, list[int]] = {}
    for i, (nm, _b, only) in enumerate(found):
        if not only:
            by_name.setdefault(nm, []).append(i)
    out = list(found)
    notes = []
    drop: set[int] = set()
    used: dict[str, int] = {}
    for li, (ln, (y0, y1, gh)) in enumerate(zip(lines, line_bands)):
        names = [m.group(1) for m in TOKEN_RE.finditer(ln)]
        if not names or is_icon_line(ln):
            continue
        for nm in dict.fromkeys(names):
            k = names.count(nm)
            idxs = by_name.get(nm, [])
            start = used.get(nm, 0)
            mine = idxs[start:start + k]  # found 안에서 이 줄 몫 (찾은 것만 있으므로 모자랄 수 있다)
            used[nm] = start + len(mine)
            tpl = templates.get(nm)
            hits = match_in_band(img, tpl, (y0, y1), gh, n=k) if tpl is not None else []
            in_band = lambda b: y0 - 1.2 * gh <= (b[1] + b[3]) / 2 <= y1 + 1.2 * gh  # noqa: E731
            for j, i in enumerate(mine):
                hit = hits[j] if j < len(hits) else None
                near = hit is not None and _box_iou(out[i][1], hit[1]) >= 0.3
                # 줄 배치 자리와 멀리 떨어진 자리는 더 닮아야 믿는다 (무늬가 있는 그림이 BAND_MATCH_MIN을 넘을 수 있다)
                if hit is not None and (near or hit[0] >= BAND_MATCH_FAR):
                    if not near:
                        notes.append(f"{nm}: 사전 그림으로 자리를 다시 잡음 ({li + 1}째 줄)")
                    out[i] = (nm, tuple(int(v) for v in hit[1]), False)
                elif not in_band(out[i][1]):
                    drop.add(i)
                    notes.append(f"{nm}: 찾은 자리가 원문 줄 밖이라 지우지 않음 ({li + 1}째 줄)")
    out = [f for i, f in enumerate(out) if i not in drop]
    return out, notes


def inline_icon_boxes(img: np.ndarray, text: str) -> list[tuple[str, tuple[int, int, int, int]]]:
    """글 줄 안의 아이콘 자리 [(이름, 상자)]. 아이콘만 있는 줄의 아이콘은 뺀다 (그 줄은 지우지도 다시 그리지도 않는다)."""
    return [(n, b) for n, b, only in icon_boxes(img, text) if not only]


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
    with workspace.EXTRACT_LOCK:
        return _apply_region_locked(file_dir, page, region, mapping, strip, note)


def _apply_region_locked(file_dir: Path, page: int, region: str, mapping: dict[int, str],
                         strip: dict[int, dict] | None, note: str) -> str | None:
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
        n_removed = sum(1 for v in mapping.values() if not v or v.startswith("="))
        # ‘아이콘 아님’으로 [[?]]를 뺐거나 글자로 바꾼 수 (0도 적는다: 아이콘을 정한 칸이라는 표시).
        # 다시 읽은 결과의 그 자리 [[?]]를 원문 확인이 되살리지 않게 (저장해도 남김)
        r["icon_removed"] = int(r.get("icon_removed") or 0) + n_removed
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
    index: int  # 그 칸의 몇 번째 [[?]] (0부터). 이름이 있는 토큰의 자리(named)는 -1
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
    # 글자 지우기 배경이 그림인 칸의 아이콘: 비슷한 아이콘과 같은 것으로 보지 않고 늘 따로 (묶지 않음·사전 자동 맞춤 없음).
    # 그림 배경 위 아이콘은 잘라 낸 그림에 그 자리 배경이 함께 들어가 자리마다 다른 그림으로 쓴다
    unique: bool = False
    # 원문에 이미 이름이 있는 아이콘([[crown]])의 자리: 그 이름. 이런 자리는 index가 -1이고 ti(그 칸 모든 [[…]] 중 몇째)로
    # 원문 토큰을 가리킨다. 자리를 다시 잡거나 나누면 그 토큰을 [[?]]로 열어(open_named) 보통 자리가 된다
    named: str = ""
    ti: int = -1  # 그 칸의 모든 [[…]] 토큰 중 몇째 (0부터)
    prev: str = ""  # 확정된 이름이 있던 자리를 [[?]]로 연 경우 그 이름 (2A가 이름 칸에 미리 채운다)
    added: bool = False  # 사람이 카드 보기에서 새로 그려 넣은 자리 (원문에 없던 아이콘)

    @property
    def num(self) -> str:
        """묶기·사전 비교에 쓰는 숫자: 손으로 잡은 자리는 숫자를 원문에 남기므로 ""."""
        return "" if self.manual else num_key((self.number or {}).get("text"))


@dataclass
class ScanResult:
    occurrences: list[Occurrence] = field(default_factory=list)
    # 줄(카드의 아이콘 하나) -> {"rep", "count"(늘 1), "suggest", "meaning", "ko", "shape"(모양이 같은 줄끼리 같은 값),
    # "dict"(이름 칸에 미리 채울 사전 이름), "dict_how"(named=이미 확정됨 | same=사전과 모양 같음 | legacy=예전 사전 그림과
    # 비슷 | prev=확정된 이름을 다시 연 자리), "named", "not_icon", "absorbed", …}
    groups: dict = field(default_factory=dict)
    auto: dict = field(default_factory=dict)  # (예전) 사전으로 바로 바꾼 이름 -> 횟수. 이제는 늘 비어 있다

    @property
    def pending(self) -> int:
        return len(self.occurrences)


def _occ_dir(info, f: str) -> Path:
    """찾은 자리의 파일 폴더. 데이터 폴더 기준 경로(게임 단위), 예전 결과는 프로젝트 기준."""
    from app.workspace import data_root

    d = data_root(info) / f
    return d if d.exists() else info.dir / f


def text_only_cells(fd: Path):
    """(쪽, 영역 이름) -> ‘글자만’ 영역인가. ② 원문 확인은 이 칸의 토큰을 찾지도 세지도 않는다
    (세기만 하고 찾지 않으면 남은 [[?]] 때문에 ‘추출 완료’가 아이콘 검증에서 멈춘다). 영역 파일은 처음 물을 때 읽는다."""
    tpl: list = []

    def skip(pno: int, rname: str) -> bool:
        if not tpl:
            try:
                from app.project import load_project
                tpl.append(load_project(Path(fd)).template)
            except Exception:  # noqa: BLE001 — 영역 파일이 없으면 기본 동작
                tpl.append(None)
        if tpl[0] is None:
            return False
        reg = next((r for r in tpl[0].regions_for(int(pno)) if r.name == rname), None)
        return text_only(reg)
    return skip


def pending_count(info) -> int:
    """게임의 모든 프로젝트(뺀 쪽 제외) 원문에 남은 ``[[?]]`` 수 (아이콘 검증은 게임 단위)."""
    from app.extract import load_extracted
    from app.workspace import game_file_dirs, included_pages

    n = 0
    for fd in game_file_dirs(info):
        inc = set(included_pages(fd))
        skip = text_only_cells(fd)
        for pno, regs in load_extracted(fd).items():
            if pno in inc:
                n += sum(len(PLACEHOLDER_RE.findall(r["source"])) for name, r in regs.items()
                         if isinstance(r.get("source"), str) and not skip(pno, name))
    return n


def pending_by_file(info) -> dict[str, int]:
    """게임 원문에 남은 ``[[?]]`` 수를 파일마다 {‘프로젝트/그룹/파일’: 수} (0인 파일은 뺀다)."""
    from app.extract import load_extracted
    from app.workspace import game_file_dirs, game_file_key, included_pages

    out: dict[str, int] = {}
    for fd in game_file_dirs(info):
        inc = set(included_pages(fd))
        skip = text_only_cells(fd)
        n = sum(len(PLACEHOLDER_RE.findall(r["source"])) for pno, regs in load_extracted(fd).items() if pno in inc
                for name, r in regs.items() if isinstance(r.get("source"), str) and not skip(pno, name))
        if n:
            out[game_file_key(fd)] = n
    return out


def token_count(info) -> int:
    """게임의 모든 프로젝트(뺀 쪽 제외) 원문에 있는 아이콘 토큰 수 ([[?]]와 이름이 있는 [[이름]] 모두).
    ② 원문 확인은 확정된 아이콘도 표에 보여 고칠 수 있게 하므로, 하나라도 있으면 찾는다."""
    from app.extract import load_extracted
    from app.workspace import game_file_dirs, included_pages

    n = 0
    for fd in game_file_dirs(info):
        inc = set(included_pages(fd))
        skip = text_only_cells(fd)
        for pno, regs in load_extracted(fd).items():
            if pno in inc:
                n += sum(len(TOKEN_RE.findall(r["source"])) for name, r in regs.items()
                         if isinstance(r.get("source"), str) and not skip(pno, name))
    return n


class PictureCells:
    """한 파일에서 글자 지우기 배경이 그림인 칸 (page, 영역)인가: 카드별로 정한 방식 → 영역 설정 → 자동이면 지운 결과의
    실제 방식(‘복잡한 그림 배경’). 아이콘을 그 칸에서는 따로 만든다 (Occurrence.unique)."""

    def __init__(self, fd: Path):
        from app.project import load_project
        from app.workspace import file_settings

        self.fd = Path(fd)
        self.tpl = load_project(self.fd).template
        self.over = file_settings(self.fd).get("erase_modes", {}) or {}
        self._meta: dict[int, dict] = {}

    def _used(self, page: int, region: str) -> str:
        if page not in self._meta:
            f = self.fd / "cleared" / "meta" / f"page-{int(page):03d}.json"
            try:
                self._meta[page] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
            except (OSError, ValueError):
                self._meta[page] = {}
        m = next((r for r in self._meta[page].get("regions", []) if r.get("name") == region), None) or {}
        return str(((m.get("erase") or {}).get("text_mask") or {}).get("used") or "")

    def __call__(self, page: int, region: str) -> bool:
        mode = (self.over.get(str(int(page))) or {}).get(region)
        if not mode:
            reg = next((r for r in self.tpl.regions_for(int(page)) if r.name == region), None)
            mode = ((reg.erase or {}).get("mode") if reg is not None else None) or "auto"
        if mode == "picture":
            return True
        return mode == "auto" and self._used(page, region) == "picture"


def scan(info, work: Path, only: set | None = None) -> ScanResult:
    """원문의 아이콘 토큰을 모두 찾아 자리마다 위치·그림을 만든다 (카드의 아이콘 하나 = 한 줄).
    [[?]]는 사전과 모양이 같으면 그 이름을 줄에 미리 채워 두기만 하고 원문은 바꾸지 않는다 (사람이 확정할 때 바꾼다).
    이미 이름이 있는 [[이름]]도 줄로 만든다 (named): 이름을 그대로 두면 확정해도 원문은 그대로다.
    only: {(파일 상대 경로, 쪽, 영역)} — 그 칸만 찾는다 (icons_stage.rescan_regions가 임시 폴더에 찾아 합친다)."""
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
        todo = [(p, r, v["source"], v.get("icon_numbers"), v.get("icon_boxes")) for p, regs in sorted(ext.items())
                if p in inc for r, v in regs.items() if isinstance(v.get("source"), str) and TOKEN_RE.search(v["source"])
                and (only is None or (str(fd.relative_to(root)), p, r) in only)]
        if not todo:
            continue
        proj = load_project(fd)
        picture = PictureCells(fd)
        src = next((fd / "source").glob("*.pdf"))
        doc = pymupdf.open(src)
        try:
            for pno, rname, text, icon_nums, icon_saved in todo:
                reg = {r.name: r for r in proj.template.regions_for(pno)}.get(rname)
                if reg is None:  # 영역을 지운 칸은 그림을 자를 수 없다
                    continue
                if text_only(reg):  # ‘글자만’ 영역: 아이콘을 찾지 않는다 (예전에 읽은 [[?]]가 남아 있어도)
                    continue
                img = region_image(doc[pno - 1], reg)
                tokens = list(TOKEN_RE.finditer(text))
                ph = [i for i, t in enumerate(tokens) if t.group(1) == "?"]
                # 글자 없이 붙은 [[?]] 덩어리 (줄이 바뀌어도 하나): 같은 자리로 잡혀도 서로 겹친다고 보지 않는다
                adj_of = {k: ri for ri, run in enumerate(_place.placeholder_runs(text)) for k in run}
                # 같은 줄에 붙은 [[?]]만 세트 후보 (위아래로 이어진 것은 사람이 카드 보기에서 묶을 때만)
                run_of = {k: ri for ri, run in enumerate(_place.placeholder_runs(text, same_line=True)) for k in run}
                located = [locate(img, text, ti) for ti in ph]
                clash = _place.shared_boxes([b for b, _w in located], [adj_of.get(k) for k in range(len(ph))])
                set_host: dict[tuple, Occurrence] = {}  # (덩어리, 찾은 자리) -> 그 자리를 처음 찾은 아이콘
                host_k: dict[tuple, int] = {}  # 그 아이콘의 [[?]] 순번 (같은 줄인지 볼 때)
                set_n: dict[tuple, int] = {}  # 그 자리에 들어간 아이콘 수
                left: list[Occurrence] = []
                occ_of: dict[int, Occurrence] = {}
                contested: set[int] = set()  # 다른 줄의 [[?]]도 같은 자리로 잡힌 첫 자리 (그 자리가 누구 것인지 모른다)
                for k, ti in enumerate(ph):
                    n += 1
                    oc = Occurrence(f"o{n:03d}", str(fd.relative_to(root)), game_file_key(fd), pno, rname, k,
                                    text.replace("\n", " / "), ti=ti)
                    oc.line, oc.order, _nm = tokens_in(text)[ti]
                    oc.unique = picture(pno, rname)
                    occ_of[k] = oc
                    box, why = located[k]
                    if k in clash:
                        # 글자를 사이에 둔 다른 [[?]]와 같은 자리로 잡힘: 어느 쪽이 맞는지 모른다. 그대로 두면 서로 다른 아이콘이
                        # 한 그림으로 사전 이름이 채워지거나(사교·큐브·왕관 5개가 모두 crown_social) 한 모양으로 묶인다
                        box, why = None, _place.SHARED_BOX_REASON
                    key = (adj_of.get(k), tuple(box)) if box is not None else None
                    host = set_host.get(key) if key else None
                    if (host is not None and set_n[key] > 0 and run_of.get(k) == run_of.get(host_k[key])
                            and _place.set_box_fits(box, set_n[key] + 1)):
                        set_n[key] += 1
                        # 붙어 있는 [[?]]가 앞 아이콘과 같은 덩어리: 아이콘 여러 개가 붙은 한 세트 (그 그림 하나로 보여 주고
                        # 이 자리는 원문에서 뺀다. 둘 다 남기면 세트 그림이 두 번 들어간다)
                        oc.set_of, oc.box = host.id, list(host.box)
                        left.append(oc)
                        continue
                    if host is not None:
                        # 앞 아이콘과 같은 자리로 잡혔는데 세트 모양이 아님(또는 세트가 끝남): 찾기 실수로 보고 직접 잡게 한다
                        # (같은 그림을 잘라 두면 앞 아이콘과 같은 모양이 되어 같은 이름이 두 번 들어간다)
                        set_n[key] = 0
                        contested.add(host_k[key])
                        box, why = None, "앞 아이콘과 같은 자리로 잡혀 위치를 못 찾음"
                    if box is None:
                        oc.reason = why
                    else:
                        if key not in set_host:
                            set_host[key], set_n[key], host_k[key] = oc, 1, k
                        # 아이콘 바로 앞·뒤(또는 위)에 인쇄된 숫자는 아이콘의 일부로 그림에 넣는다. 원문에서 빼는 것은
                        # 사람이 확정할 때 (손으로 다시 잡거나 ‘아이콘 아님’이면 숫자를 남겨야 하므로)
                        num = number_near(text, tokens[ti].start(), tokens[ti].end())
                        if num is not None:
                            nb = absorb_number(img, box, num[0], num[2] - num[1])
                            if nb is not None:
                                box = nb
                                oc.number = {"text": text[num[1]:num[2]], "side": num[0]}
                        oc.box = [int(v) for v in box]
                        rgba = _cells.cut_out(img, box)
                        png = work / "crops" / f"{oc.id}.png"
                        _cells.save_png(rgba, png)
                        oc.png = str(png)
                        sh = shape_pair(rgba)
                        if not oc.unique:  # 그림 배경 칸: 사전 아이콘과 같다고 보지 않는다 (늘 새 아이콘)
                            # 사전과 모양·숫자가 같으면 이름 칸에 미리 채운다 (원문은 사람이 확정할 때 바꾼다)
                            oc.match, oc.score = best_known(sh, known, oc.num)
                            if not oc.match:
                                oc.hint = _place.legacy_hint(sh, known, oc.num)
                    left.append(oc)
                for hk in sorted(contested):
                    # 첫 자리는 찾은 그대로 보여 주되 사전 이름을 ‘모양 같음’으로 채우지 않는다 (그 상자가 아랫자리 아이콘일
                    # 수 있다): 비슷한 사전 이름으로만 채운다
                    oc = occ_of[hk]
                    if oc.match:
                        oc.match, oc.hint = "", oc.match
                # 이미 이름이 있는 아이콘 [[이름]]: 그 자리도 한 줄로 보여 고칠 수 있게 (그대로 두면 원문은 바뀌지 않는다)
                for ti, t in enumerate(tokens):
                    if t.group(1) == "?":
                        continue
                    n += 1
                    oc = Occurrence(f"o{n:03d}", str(fd.relative_to(root)), game_file_key(fd), pno, rname, -1,
                                    text.replace("\n", " / "), named=t.group(1), ti=ti)
                    oc.line, oc.order, _nm = tokens_in(text)[ti]
                    oc.unique = picture(pno, rname)
                    # ②에서 확정할 때 남긴 자리(손으로 잡은 상자 포함)가 있으면 그대로 쓴다 (다시 찾지 않음)
                    k = sum(1 for t2 in tokens[:ti] if t2.group(1) == t.group(1))
                    saved = next((e["box"] for e in (icon_saved or []) if isinstance(e, dict)
                                  and e.get("name") == t.group(1) and e.get("k") == k and e.get("box")), None)
                    saved = [int(round(float(v))) for v in saved] if saved else None
                    box = saved or locate(img, text, ti)[0]
                    if box is None:  # 줄 배치로 못 찾으면 사전 그림으로 칸 안을 찾는다
                        box = _place.match_known(info, img, t.group(1), text)
                    if box is None:
                        oc.reason = "확정된 아이콘의 자리를 찾지 못함"
                        left.append(oc)
                        continue
                    # 확정할 때 원문에서 뺀 옆 숫자(icon_numbers)는 그림에 다시 넣는다 (사전 그림과 같게)
                    e = next((e for e in (icon_nums or []) if isinstance(e, dict) and e.get("index") == ti
                              and e.get("name") == t.group(1) and e.get("text")), None)
                    if e is not None:
                        # 남긴 자리는 숫자까지 잡은 상자다 (다시 넓히지 않음)
                        nb = None if saved else absorb_number(img, box, e.get("side", "before"), len(str(e["text"])))
                        if nb is not None:
                            box = nb
                        oc.number = {"text": str(e["text"]), "side": e.get("side", "before")}
                    oc.box = [int(v) for v in box]
                    png = work / "crops" / f"{oc.id}.png"
                    _cells.save_png(_cells.cut_out(img, box), png)
                    oc.png = str(png)
                    left.append(oc)
                _drop_shared_named(left)
                left.sort(key=lambda o: o.ti)  # 원문 순서 (읽는 순서)
                res.occurrences.extend(left)
        finally:
            doc.close()
    _group(res)
    _save_scan(work, res)
    return res


def _drop_shared_named(cell: list[Occurrence]) -> None:
    """한 칸에서 이름이 있는 아이콘([[이름]])의 자리가 다른 토큰의 자리와 같으면(거의 겹치면) 둘 다 ‘위치 못 찾음’으로.
    인쇄된 아이콘은 서로 다른 자리에 있으므로 같은 상자는 적어도 하나가 잘못 찾은 것이다 ([[?]]끼리는 scan이
    shared_boxes·세트로 이미 가린다). 이런 상자는 위아래 줄의 아이콘 둘을 한 상자로 감싼 경우가 많다
    (공주와기사 6쪽: 1줄 사교 아이콘과 2줄 도시 아이콘이 crown_4·city 두 토큰에 같은 상자로 잡힘)."""
    boxed = [o for o in cell if o.box and not o.set_of]
    bad: set[str] = set()
    for i, a in enumerate(boxed):
        for b in boxed[i + 1:]:
            if (a.named or b.named) and _place._iou(a.box, b.box) >= _place.SHARED_BOX_IOU:
                bad.update((a.id, b.id))
    for o in cell:
        if o.id in bad:
            o.box, o.png, o.number = [], "", {}
            o.match, o.score, o.hint = "", 0.0, ""
            o.reason = _place.SHARED_BOX_REASON


def _group(res: ScanResult) -> None:
    """자리마다 한 줄(묶음)을 만든다 (카드의 아이콘 하나 = 한 줄). 모양이 같은 줄끼리는 "shape"이 같다
    (이름 제안을 모양마다 한 번만 받고, 화면이 같은 모양 줄에 이름을 함께 적게 한다). 사전과 모양이 같거나 이미 이름이
    있는 자리는 그 이름을 "dict"로 미리 채운다."""
    shapes = {o.id: shape_pair(_cells.load_png(Path(o.png))) for o in res.occurrences if o.png}
    by_id = {o.id: o for o in res.occurrences}
    reps: list[tuple[str, str]] = []  # (모양, 대표 occurrence id)
    n_g = 0  # 만든 줄 수
    for o in res.occurrences:
        if o.set_of:  # 세트의 나머지 자리: 표에 따로 보이지 않고 확정할 때 원문에서 뺀다
            o.group = f"세트 {o.id}"
            res.groups[o.group] = {"rep": "", "count": 1, "not_icon": True, "absorbed": o.set_of, "auto": True}
            if o.set_name:  # (예전) 첫 자리가 사전으로 바로 바뀜: 이 자리는 표에 보여 확인받는다
                res.groups[o.group].update(auto=False, absorbed_name=o.set_name)
            continue
        if o.png:
            n_g += 1
            o.group = f"g{n_g:02d}"
            gi = {"rep": o.id, "count": 1}
            # 옆 숫자가 다르면 모양이 같아도 다른 모양 (상자+2·상자+5). 그림 배경 칸의 아이콘은 늘 혼자인 모양
            sg = None if o.unique else next((sg for sg, rid in reps if by_id[rid].num == o.num
                                             and same_icon(shapes[o.id], shapes[rid]) >= SAME_SHAPE), None)
            if sg is None:
                sg = f"s{len(reps) + 1:02d}" if not o.unique else f"s-{o.id}"
                if not o.unique:
                    reps.append((sg, o.id))
            gi["shape"] = sg
        else:
            o.group = f"위치 못 찾음 {o.id}"
            gi = {"rep": "", "count": 1}
        res.groups[o.group] = gi
        _prefill(gi, o)


def _prefill(gi: dict, o: Occurrence) -> None:
    """줄의 이름 칸에 미리 채울 사전 이름 (dict)과 그 까닭 (dict_how)."""
    for how, name in (("named", o.named), ("same", o.match), ("legacy", o.hint), ("prev", o.prev)):
        if name:
            gi["dict"], gi["dict_how"] = name, how
            if how == "named":
                gi["named"] = name
            return


def shape_of_row(res: ScanResult, o: Occurrence, sh) -> str:
    """사람이 다시 잡은 자리의 모양 (모양이 같은 다른 줄의 shape, 없으면 새 값)."""
    if o.unique:
        return f"s-{o.id}"
    by_id = {x.id: x for x in res.occurrences}
    seen: set[str] = set()
    for g, gi in res.groups.items():
        sg = gi.get("shape")
        rep = by_id.get(gi.get("rep") or "")
        if not sg or sg in seen or rep is None or rep.id == o.id or not rep.png or rep.unique or rep.num != o.num:
            continue
        seen.add(sg)
        if same_icon(sh, shape_pair(_cells.load_png(Path(rep.png)))) >= SAME_SHAPE:
            return sg
    return f"s-{o.id}"


def prefill_name(gi: dict) -> str:
    """줄의 이름 칸에 처음 보일 이름: 이미 확정된 이름 → 사전 이름(모양 같음·비슷함) → 모델 제안."""
    return gi.get("named") or gi.get("dict") or gi.get("suggest") or ""


def _area(png: str) -> int:
    a = _cells.load_png(Path(png))
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
    res = ScanResult(occ, data["groups"], data.get("auto", {}))
    _split_old_groups(res)
    return res


def _split_old_groups(res: ScanResult) -> None:
    """예전 scan.json(1.22 이전: 같은 모양 자리 여럿 = 한 묶음)을 자리마다 한 줄로 나눈다 (적어 둔 제안·뜻은 줄마다
    복사, 예전 묶음 이름은 모양 "shape"으로 남긴다). 새 scan.json은 이미 한 줄에 자리 하나라 바뀌지 않는다."""
    first: dict[str, Occurrence] = {}
    nums = [int(k[1:]) for k in res.groups if re.fullmatch(r"g\d+", k)]
    nxt = max(nums, default=0)
    for o in res.occurrences:
        gi = res.groups.get(o.group)
        if o.set_of or gi is None:
            continue
        if o.group not in first:
            first[o.group] = o
            continue
        nxt += 1
        ng = f"g{nxt:02d}" if o.png else f"위치 못 찾음 {o.id}"
        new = {k: v for k, v in gi.items() if k not in ("rep", "count")}
        new.update(rep=o.id if o.png else "", count=1)
        new.setdefault("shape", o.group)
        res.groups[ng] = new
        o.group = ng
    for g, o in first.items():
        gi = res.groups[g]
        if gi.get("count", 1) > 1:
            gi.update(count=1, rep=o.id if o.png else "")
            gi.setdefault("shape", g)


MATCH_KNOWN_MIN = 0.6  # 사전 그림으로 찾을 때 이 이상 닮아야 그 자리로 본다 (정규화 상관)
def _known_shapes(info) -> dict:
    """사전 이름 -> (모양, 그림에 넣은 숫자 | None=예전 항목)."""
    d = load_dictionary(info)
    out = {}
    for name, e in d["icons"].items():
        p = icons_dir(info) / e.get("file", f"{name}.png")
        if p.exists():
            out[name] = (shape_pair(_cells.load_png(p)), entry_number(e))
    return out
def best_known(sh, known: dict, occ_num: str) -> tuple[str, float]:
    """사전에서 모양이 같은 아이콘 (이름, 점수). 옆 숫자가 다르면 후보가 아니다 (상자+2를 상자+5로 바꾸지 않게:
    모양 비교는 숫자를 거의 보지 못한다). 없으면 ("", 0)."""
    cand = [(nm, same_icon(sh, ks)) for nm, (ks, en) in known.items() if number_allows(en, occ_num)]
    nm, s = max(cand, key=lambda t: t[1], default=("", 0.0))
    return (nm, s) if s >= SAME_SHAPE else ("", 0.0)

# ---------------------------------------------------------------- 나눈 모듈
# 아래 모듈의 이름도 예전처럼 app.icons.이름으로 쓴다 (from app.icons import X, icons.X 모두).
# 나눈 모듈은 이 모듈을 _ic로 불러 이름을 부를 때마다 찾는다: 테스트가 icons.locate 등을 바꿔 끼우면 그것을 쓴다.
from app import icons_cells as _cells  # noqa: E402
from app import icons_place as _place  # noqa: E402
from app import icons_review as _review  # noqa: E402

_PARTS = (_cells, _place, _review)


def __getattr__(name: str):
    for part in _PARTS:
        if name in part.__dict__:
            return part.__dict__[name]
    raise AttributeError(f"module 'app.icons' has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | {n for p in _PARTS for n in p.__dict__ if not n.startswith("__")})
