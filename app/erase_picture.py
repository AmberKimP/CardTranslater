"""‘복잡한 그림 배경’ 지우기 방식 (Fable로 따로 만든 방식).

영역 지정·결과 보기에서 지우기 방식을 ‘복잡한 그림 배경’으로 고른 영역에만 쓴다 (app.erase.erase_page가 부름).
반투명 상자 뒤로 그림이 비치는 카드에서 글자를 색이 아니라 줄지어 선 가는 획 모양으로 찾고, 아이콘·그림 획은 지키며,
그림의 결을 따라 위·아래 경계를 이어 채운다. 다른 방식(자동 등)은 이 모듈을 쓰지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from app.erase import _disk, luminance, smooth_background

# =====================================================================
# 순수 함수: 그림(일러스트) 배경 위 글자
# =====================================================================
#
# 반투명 상자 뒤로 그림이 비치는 카드(글자 둘레에 흰 번짐, 아이콘 이름표에 부드러운 어두운 그림자)는
# '상자 안에서 가장 넓은 색 = 글자' 규칙이 통하지 않는다(테두리 장식·아이콘 외곽선이 글자와 같은 색).
# 여기서는 색이 아니라 모양으로 고른다.
#   1. 글자 획 대비: 획보다 넓은 구조 요소로 닫은(연) 밝기와의 차이(top-hat). 넓은 그림·아이콘 몸통은 사라지고
#      가는 획만 남는다.
#   2. 덩어리를 가까운 것끼리 이어 줄로 묶고, 같은 높이의 줄 조각을 합친다. 글자 줄 = 비슷한 높이의 덩어리가
#      셋 이상 가로로 늘어선 것. 가장 긴 줄의 글자 크기를 기준으로 너무 크거나 작은 줄(아이콘 조각, 창문 무늬)은 뺀다.
#   3. 아이콘은 지킨다: 글자 획보다 두꺼운 색 덩어리(몸통)에 붙은 조각, 위아래가 다른 획으로 막힌 조각(속 무늬),
#      두꺼운 덩어리 위에 얹힌 조각(장식).
#   4. 지울 픽셀 = 글자 + 그림자(약한 대비, 글자에서 이어진 것) + 번짐(둘레에서 배경과 다른 픽셀).
#      지킬 덩어리보다 글자에 가까운 곳까지만.
#   5. 채우기: 그림의 결을 따라 위·아래 경계를 이어 채우고(directional_fill), 주변과 같은 세기의 결(스캔 잡음)을 얹는다.

PICTURE_NOTE = "배경이 그림이라 지운 자리가 티 날 수 있음"


@dataclass
class PictureParams:
    levels: tuple[float, ...] = (100.0, 55.0)  # 획 대비가 이 이상이면 글자 후보 (뚜렷한 기준부터, 줄이 없으면 다음 기준)
    edge: float = 55.0  # 아이콘 실루엣을 잴 때의 획 기준 (외곽선 + 진한 그림자)
    weak: float = 20.0  # 글자에 이어진 그림자로 넓힐 때의 기준
    wide_gain: float = 2.0  # 상자 바탕(넓은 창의 중앙값)과의 차이에 곱하는 값 (바탕과 기준의 절반은 달라야 후보)
    se_ratio: float = 0.055  # 구조 요소 반경 = 영역 높이 × 이 값 (3~12px)
    link_gap: float = 1.0  # 덩어리 잇기: 가로 틈 ≤ 큰 쪽 높이 × 이 값
    link_dy: float = 0.6  # 덩어리 잇기: 세로 중심 차이 ≤ 큰 쪽 높이 × 이 값
    min_glyphs: int = 3  # 글자 줄이 되려면 글자가 이만큼 (옆 글자와 붙은 덩어리는 폭으로 나눠 센다)
    min_ref_glyphs: int = 4  # 기준 줄(가장 긴 글자 줄)의 최소 글자 수
    min_row_aspect: float = 2.5  # 줄 폭 ≥ 글자 크기 × 이 값
    max_row_height: float = 2.2  # 줄 높이 ≤ 글자 크기 × 이 값 (한 줄)
    size_range: tuple[float, float] = (0.5, 2.2)  # 기준 글자 크기 대비 줄 글자 크기 범위
    big_ratio: float = 2.2  # 기준 글자 크기의 이 배를 넘는 덩어리는 그림·아이콘 외곽선 (그림자 붙은 이름표 ≈ 1.8배)
    interior_max: float = 0.35  # 아이콘 속으로 볼 닫힌 곳의 최대 넓이 (영역 넓이 대비). 더 넓으면 글자 상자 전체
    interior_rim: float = 0.35  # 두꺼운 덩어리 둘레를 이만큼(글자 크기 배) 넓혀 외곽선 조각까지 덮는다
    clear_reach: float = 0.4  # 조각 위·아래로 이만큼(글자 크기 배, 최소 2px) 안에 다른 획이 있으면 그쪽이 막힌 것
    body_sat: float = 0.35  # 아이콘 몸통: 채도가 이 이상이고
    body_thick: float = 0.3  # 두께(반지름)가 글자 크기 × 이 값(최소 2.2px)을 넘는 색 덩어리 (굵은 빨간 글자 획 ≈ 1.5px)
    shadow_reach: float = 0.5  # 그림자 넓히기 거리 (글자 크기 배)
    halo: float = 0.18  # 글자 둘레를 무조건 넓히는 폭 (글자 크기 배, 최소 1px)
    absorb_reach: float = 0.3  # 번짐·남은 그림자를 찾는 둘레 폭 (글자 크기 배, 최소 2px). 넓으면 줄 사이가 다 메워진다
    absorb_diff: float = 14.0  # 그 둘레에서 배경 어림과 이만큼 다르면 함께 지운다
    fill: str = "directional"  # 채우기: directional (그림의 결을 따라 잇기) | telea | ns (OpenCV 인페인팅)
    max_span: float = 3.5  # 위·아래 재료 사이가 이보다(글자 크기 배) 멀면 결 따라 잇지 않고 일반 인페인팅
    fill_smooth: float = 0.7  # 채운 뒤 구멍 안쪽을 고르는 흐림(px). 0이면 하지 않음
    grain: float = 0.6  # 채운 자리에 얹는 결의 세기 (주변 잡음 대비. 1이면 점이 도드라져 보인다)


@dataclass
class PictureText:
    mask: np.ndarray  # 지울 픽셀 (글자 + 그림자 + 번짐)
    core: np.ndarray  # 글자 덩어리
    guard: np.ndarray  # 지킬 덩어리 (아이콘·그림의 뚜렷한 획): 지울 픽셀이 이쪽으로 넘어가지 않는다
    avoid: np.ndarray  # 지우지는 않지만 채울 값의 재료로 쓰지 않을 픽셀 (아이콘 그림자 등 약한 획)
    glyph_px: float
    polarity: int  # -1: 어두운 글자, +1: 밝은 글자
    rows: list[dict]  # 글자 줄 (위부터): {"box": (x0, y0, x1, y1), "core": 그 줄의 덩어리, "under_icon": 아이콘 아래 이름표인가}


def thin_contrast(lum: np.ndarray, r: int, polarity: int) -> np.ndarray:
    """반경 r보다 가는 구조의 대비 (top-hat). polarity -1: 주변보다 어두운 획, +1: 밝은 획."""
    k = _disk(r)
    lum = np.ascontiguousarray(lum, np.float32)
    if polarity < 0:
        return cv2.morphologyEx(lum, cv2.MORPH_CLOSE, k) - lum
    return lum - cv2.morphologyEx(lum, cv2.MORPH_OPEN, k)


def stroke_contrast(img: np.ndarray, box: tuple[int, int, int, int], polarity: int,
                    p: PictureParams | None = None) -> np.ndarray:
    """글자 획 대비: 가는 획 대비(top-hat)와, 넓은 창의 중앙값(상자 바탕)과의 차이 × wide_gain 중 작은 쪽.
    어두운 글자의 획 사이 밝은 틈(번짐)은 가는 '밝은 획'처럼 보이지만 바탕과는 별로 다르지 않아 걸러진다."""
    p = p or PictureParams()
    bh = max(1, box[3] - box[1])
    r = int(np.clip(round(p.se_ratio * bh), 3, 12))
    # 어두운 글자는 가장 어두운 채널로 본다: 빨간 강조 글자는 밝기로는 흐리지만 초록·파랑 채널에서는 검정만큼 어둡다
    lum = (img.min(axis=2) if polarity < 0 else img.max(axis=2)).astype(np.float32)
    T = thin_contrast(lum, r, polarity)
    k = int(max(31, min(81, bh * 0.6))) | 1
    med = cv2.medianBlur(np.clip(lum, 0, 255).astype(np.uint8), k).astype(np.float32)
    return np.minimum(T, polarity * (lum - med) * p.wide_gain)


def _chains(items: list[dict], gap: float, dy: float, ratio: float = 2.0) -> list[list[dict]]:
    """가까운 덩어리끼리 이은 묶음 (가로 틈·세로 중심 차이가 큰 쪽 높이에 비례한 한도 안)."""
    n = len(items)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        a = items[i]
        for j in range(i + 1, n):
            b = items[j]
            s, t = max(a["s"], b["s"]), min(a["s"], b["s"])
            if s > ratio * t:  # 크기가 많이 다른 것(글자와 아이콘 외곽선, 글자와 티끌)은 잇지 않는다
                continue
            if max(b["x0"] - a["x1"], a["x0"] - b["x1"]) <= gap * s and abs(a["cy"] - b["cy"]) <= dy * s:
                parent[find(i)] = find(j)
    groups: dict[int, list[dict]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(items[i])
    return list(groups.values())


def _row_stats(members: list[dict]) -> dict:
    """줄 묶음의 글자 크기(hg: 덩어리 높이의 75%점), 글자 수 어림, 상자.
    옆 글자와 붙은 덩어리는 폭을 글자 크기로 나눠 여러 글자로 센다. 속이 꽉 찬 가로 막대(선·'ー')는 세지 않는다.
    n_glyph는 글자다운 덩어리만 센다: 속이 꽉 차지 않았다 (창문 무늬 같은 작은 네모·점은 꽉 찼다)."""
    solid = [m for m in members if not m["bar"]] or members
    sizes = [m["s"] for m in solid if m["a"] >= 4] or [m["s"] for m in solid]
    hg = float(np.percentile(sizes, 75))
    big = [m for m in solid if m["s"] >= 0.5 * hg] or solid
    x0, x1 = min(m["x0"] for m in members), max(m["x1"] for m in members)
    y0, y1 = min(m["y0"] for m in big), max(m["y1"] for m in big)

    def count(m: dict) -> int:
        return max(1, int(round(m["w"] / max(hg, 1.0))))

    n_big = sum(count(m) for m in big if not m["bar"])
    n_glyph = sum(count(m) for m in big if not m["bar"] and m["a"] <= 0.8 * m["w"] * m["h"])
    n_comp = sum(1 for m in big if not m["bar"])
    return {"members": members, "hg": hg, "n_big": n_big, "n_glyph": n_glyph, "n_comp": n_comp,
            "box": (x0, y0, x1, y1)}


def _text_rows(items: list[dict], p: PictureParams, hg_ref: float | None = None) -> list[dict]:
    """덩어리를 줄로 묶어 글자 줄만 돌려준다 (hg_ref가 있으면 그 글자 크기와 맞는 줄만)."""
    rows = [_row_stats(c) for c in _chains(items, p.link_gap, p.link_dy)]
    # 같은 높이에 있는 묶음(낱말 사이가 먼 이름표 줄 '工房   商館')을 한 줄로 합친다
    rows.sort(key=lambda r: -r["n_big"])
    merged: list[dict] = []
    for r in rows:
        for m in merged:
            a, b = m["box"], r["box"]
            ov = min(a[3], b[3]) - max(a[1], b[1])
            if ov >= 0.6 * min(a[3] - a[1], b[3] - b[1]) and max(m["hg"], r["hg"]) <= 1.6 * min(m["hg"], r["hg"]):
                m.update(_row_stats(m["members"] + r["members"]))
                break
        else:
            merged.append(r)
    out = []
    for r in merged:
        x0, y0, x1, y1 = r["box"]
        hg = r["hg"]
        if r["n_big"] < p.min_glyphs or x1 - x0 < p.min_row_aspect * hg or y1 - y0 > p.max_row_height * hg:
            continue
        if hg_ref and not (p.size_range[0] * hg_ref <= hg <= p.size_range[1] * hg_ref):
            continue
        out.append(r)
    return out


def _picture_candidates(T: np.ndarray, box: tuple[int, int, int, int],
                        level: float) -> tuple[np.ndarray, list[dict], np.ndarray]:
    """(덩어리 라벨, 덩어리 목록, 뚜렷한 픽셀): 획 대비 T가 level을 넘는 픽셀의 덩어리 (절반 넘게 상자 안인 것)."""
    x0, y0, x1, y1 = box
    strong = T > level
    n, lab, st, _ = cv2.connectedComponentsWithStats(strong.astype(np.uint8), connectivity=8)
    inbox = np.zeros(strong.shape, bool)
    inbox[max(0, y0):y1, max(0, x0):x1] = True
    inside = np.bincount(lab[inbox], minlength=n)
    items = []
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in st[i])
        if a < 2 or inside[i] < 0.5 * a:
            continue
        # 크기 s = 높이: 옆 글자와 붙은 덩어리도 높이는 글자 크기다
        items.append({"id": i, "x0": x, "y0": y, "x1": x + w, "y1": y + h, "a": a, "s": float(h),
                      "cy": y + h / 2.0, "w": w, "h": h, "bar": w > 3 * h and a > 0.9 * w * h})
    return lab, items, strong


def _icon_bodies(img: np.ndarray, hg: float, p: PictureParams) -> np.ndarray:
    """글자 획보다 두꺼운 색 덩어리 (아이콘 몸통: 왕관·보석·색 배지)."""
    f = img.astype(np.float32)
    mx, mn = f.max(axis=2), f.min(axis=2)
    sat = ((mx - mn) / np.maximum(mx, 1.0) > p.body_sat) & (mx > 60)
    dist = cv2.distanceTransform(sat.astype(np.uint8), cv2.DIST_L2, 3)
    seeds = dist > max(2.2, p.body_thick * hg)
    if not seeds.any():
        return np.zeros(sat.shape, bool)
    _n, lab = cv2.connectedComponents(sat.astype(np.uint8), connectivity=8)
    ids = np.unique(lab[seeds])
    # 획이 빽빽해 뭉친 색 글자(빨간 '最')는 글자 하나 크기를 넘지 않는다: 그보다 넓은 덩어리만 몸통
    area = np.bincount(lab.ravel())
    ids = [i for i in ids if i > 0 and area[i] >= (0.8 * hg) ** 2]
    return np.isin(lab, ids)


def _silhouette(lines: np.ndarray, box: tuple[int, int, int, int], p: PictureParams) -> np.ndarray:
    """획(lines)과 획에 둘러싸인 닫힌 곳(아이콘의 속)을 합친 실루엣. 외곽선의 작은 틈은 메워서 본다.
    글자 상자 전체처럼 아주 넓은 닫힌 곳(상자 테두리 안)은 넣지 않는다."""
    H, W = lines.shape
    closed = cv2.morphologyEx(lines.astype(np.uint8), cv2.MORPH_CLOSE, _disk(2)).astype(bool)
    n, lab, st, _ = cv2.connectedComponentsWithStats((~closed).astype(np.uint8), connectivity=4)
    hi = p.interior_max * max(1, box[2] - box[0]) * max(1, box[3] - box[1])
    ok = np.zeros(n, bool)
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in st[i])
        ok[i] = not (x == 0 or y == 0 or x + w >= W or y + h >= H) and a <= hi
    return closed | ok[lab]


def _thick_structures(sil: np.ndarray, hg: float, p: PictureParams) -> np.ndarray:
    """실루엣에서 글자 줄 높이보다 두꺼운 곳(아이콘·그림의 몸통)과 그 둘레.
    글자 줄은 글자 높이라 사라지고(열기) 몸통만 남는다. 둘레를 조금 넓혀 몸통을 두른 외곽선 조각까지 덮는다."""
    r = max(3, int(round(0.5 * p.big_ratio * hg)))
    thick = cv2.erode(sil.astype(np.uint8), _disk(r))
    if not thick.any():
        return np.zeros(sil.shape, bool)
    rim = max(1, int(round(p.interior_rim * hg)))
    return cv2.dilate(thick, _disk(r + rim)).astype(bool)


def _detect_polarity(img, box, polarity: int, p: PictureParams) -> dict | None:
    """한쪽 극성(어두운 글자 / 밝은 글자)의 글자 줄. 뚜렷한 기준(levels[0])부터 보고, 줄이 없으면 낮춰 본다.
    기준이 높으면 아이콘 그림자에 이어진 이름표 글자가 아이콘과 떨어진 덩어리가 된다."""
    T = stroke_contrast(img, box, polarity, p)
    for level in p.levels:
        d = _detect_level(img, T, box, level, p)
        if d is not None:
            return d
    return None


def _detect_level(img, T, box, level: float, p: PictureParams) -> dict | None:
    lab, items_all, strong = _picture_candidates(T, box, level)
    bh = max(1, box[3] - box[1])
    items = [m for m in items_all if m["s"] <= 0.6 * bh]
    # 기준 줄: 글자다운 덩어리가 가장 많은 줄 (아이콘 줄은 덩어리가 서넛뿐이고, 'ー'·'一' 같은 납작한 획만 모인
    # 묶음·창문 무늬는 높이가 몇 px뿐이거나 속이 꽉 찼다). 덩어리 두 개가 나란한 것(배지 두 개)은 기준이 못 된다
    first = [r for r in _text_rows(items, p)
             if r["hg"] >= 5 and r["n_comp"] >= 3 and r["n_glyph"] >= p.min_ref_glyphs]  # 5px보다 작은 글자는 없다
    if not first:
        return None
    hg = max(first, key=lambda r: (r["n_glyph"], r["hg"]))["hg"]
    # 아이콘 지키기 1: 글자 획보다 두꺼운 색 덩어리(아이콘 몸통)에 붙은 조각
    n_all = int(lab.max()) + 1
    body = _icon_bodies(img, hg, p)
    near_body = cv2.dilate(body.astype(np.uint8), _disk(2)).astype(bool)
    hit = np.bincount(lab[near_body], minlength=n_all)
    keep = [m for m in items if m["s"] <= p.big_ratio * hg and hit[m["id"]] < 0.4 * m["a"]]
    rows = _text_rows(keep, p, hg_ref=hg)
    if not rows:
        return None
    # 아이콘 지키기 2: 아이콘·그림의 일부인 조각을 뺀다.
    #  (가) 위아래가 모두 다른 획(글자 줄이 아닌 뚜렷한 획)으로 막힌 조각: 아이콘 속 무늬(건물 창문·바퀴살).
    #       글자 줄은 위나 아래 중 한쪽은 비어 있다 (아이콘 아래 이름표는 위가 아이콘 그림자로 막혀도 아래는 비었다).
    #  (나) 바로 아래에 글자보다 두꺼운 덩어리가 있는 조각: 아이콘 위에 얹힌 장식. 줄의 조각은 실루엣에서 떼어 내고
    #       두께를 잰다 (그림자로 아이콘에 이어진 이름표가 아이콘과 한 덩어리로 두꺼워 보이지 않게).
    sil = _silhouette(T > min(level, p.edge), box, p)
    on_body = {m["id"] for m in items if hit[m["id"]] >= 0.4 * m["a"]}

    def attach_shadows(rows_: list[dict]) -> dict[int, list[dict]]:
        """글자 아래쪽을 받친 납작한 조각 = 그 글자의 그림자 (글자 외곽선과 떨어져 따로 잡힌 것). {줄 번호: 조각들}.
        글자 위끝보다 아래에서 시작해 글자 아래로 글자 높이의 1.5배 안에서 끝나고, 가로로 글자를 반 넘게 받친다."""
        used = {m["id"] for r in rows_ for m in r["members"]}
        out: dict[int, list[dict]] = {}
        for m in items_all:
            if m["id"] in used or m["id"] in on_body or m["w"] < 2 * m["h"] or m["h"] > 2 * p.big_ratio * hg:
                continue
            for k, r in enumerate(rows_):
                rx0, _ry0, rx1, _ry1 = r["box"]
                if m["x0"] < rx0 - hg or m["x1"] > rx1 + hg:
                    continue
                if any(o["s"] >= 0.5 * hg and o["y0"] <= m["y0"] <= o["y1"] + 2 and m["y1"] <= o["y1"] + 1.5 * hg
                       and min(m["x1"], o["x1"]) - max(m["x0"], o["x0"]) >= 0.5 * min(m["w"], o["w"])
                       for o in r["members"]):
                    out.setdefault(k, []).append(m)
                    used.add(m["id"])
                    break
        return out

    shadows = attach_shadows(rows)
    taken = {m["id"] for r in rows for m in r["members"]} | {m["id"] for v in shadows.values() for m in v}
    other = strong & ~np.isin(lab, list(taken))
    d = max(2, int(round(p.clear_reach * hg)))
    H, W = strong.shape
    dropped: set[int] = set()

    def occupied(mask: np.ndarray, ya: int, yb: int, xa: int, xb: int) -> float:
        ya, yb = max(0, ya), min(H, yb)
        if yb <= ya or xb <= xa:
            return 0.0
        return float(mask[ya:yb, xa:xb].any(axis=0).mean())

    for k, r in enumerate(rows):
        own = [m["id"] for m in r["members"]] + [m["id"] for m in shadows.get(k, [])]
        foot = cv2.dilate(np.isin(lab, own).astype(np.uint8), _disk(max(2, int(round(0.3 * hg))))).astype(bool)
        zone = _thick_structures(sil & ~foot, hg, p)
        for m in r["members"]:
            above = occupied(other, m["y0"] - d, m["y0"], m["x0"], m["x1"])
            below = occupied(other, m["y1"], m["y1"] + d, m["x0"], m["x1"])
            on_top = occupied(zone, m["y1"], m["y1"] + 2 * d, m["x0"], m["x1"]) if zone.any() else 0.0
            m["capped"] = above >= 0.5  # 바로 위가 막혔다 (아이콘 아래 이름표)
            if (above >= 0.5 and below >= 0.5) or on_top >= 0.5:
                dropped.add(m["id"])
    if dropped:
        keep = [m for m in keep if m["id"] not in dropped]
        rows = _text_rows(keep, p, hg_ref=hg)
        if not rows:
            return None
        shadows = attach_shadows(rows)
    # 줄에 딸린 작은 조각(탁점·마침표·끊긴 획): 줄 높이 안에 있고 줄의 덩어리에서 글자 하나 거리 안
    taken = {m["id"] for r in rows for m in r["members"]} | {m["id"] for v in shadows.values() for m in v}
    for m in keep:
        if m["id"] in taken or (m["s"] >= 0.5 * hg and not m["bar"]):
            continue
        for r in rows:
            _x0, ry0, _x1, ry1 = r["box"]
            if not (ry0 - 0.3 * hg <= m["cy"] <= ry1 + 0.3 * hg):
                continue
            if any(max(o["x0"] - m["x1"], m["x0"] - o["x1"]) <= hg and o["s"] >= 0.5 * hg for o in r["members"]):
                r["members"].append(m)
                taken.add(m["id"])
                break
    # 색 글자 되살리기: '색 몸통'으로 빠진 글자 크기 조각이 줄 안에 있고 바로 옆 글자들과 같은 색이면 글자다
    # (획이 빽빽해 뭉친 빨간 강조 글자 '個'). 아이콘은 옆 글자와 색이 다르다
    I = img.astype(np.float32)
    colors: dict[int, np.ndarray] = {}

    def color(m: dict) -> np.ndarray:
        if m["id"] not in colors:
            colors[m["id"]] = I[m["y0"]:m["y1"], m["x0"]:m["x1"]][lab[m["y0"]:m["y1"], m["x0"]:m["x1"]] == m["id"]].mean(axis=0)
        return colors[m["id"]]

    for m in items:
        if m["id"] not in on_body or m["id"] in taken or m["s"] > p.big_ratio * hg:
            continue
        for r in rows:
            _x0, ry0, _x1, ry1 = r["box"]
            if not (ry0 - 0.3 * hg <= m["cy"] <= ry1 + 0.3 * hg):
                continue
            near = [o for o in r["members"] if o["s"] >= 0.5 * hg
                    and max(o["x0"] - m["x1"], m["x0"] - o["x1"]) <= 2 * hg]
            same = [o for o in near if float(np.abs(color(o) - color(m)).max()) < 45]
            if len(same) >= 2:
                r["members"].append(m)
                taken.add(m["id"])
                body &= ~cv2.dilate((lab == m["id"]).astype(np.uint8), _disk(2)).astype(bool)
                break
    ids = [m["id"] for r in rows for m in r["members"]] + [m["id"] for v in shadows.values() for m in v]
    core = np.isin(lab, ids)
    guard = (strong & ~core) | body
    guard = cv2.morphologyEx(guard.astype(np.uint8), cv2.MORPH_OPEN, np.ones((2, 2), np.uint8)).astype(bool) | body
    guard &= ~core
    # 줄마다: 글자 덩어리, 아이콘 바로 아래에 붙은 이름표인가 (큰 덩어리 대부분의 바로 위가 다른 획으로 막힘)
    row_info = []
    for k, r in enumerate(rows):
        rid = [m["id"] for m in r["members"]] + [m["id"] for m in shadows.get(k, [])]
        big = [m for m in r["members"] if m["s"] >= 0.5 * hg and "capped" in m]
        capped = bool(big) and sum(1 for m in big if m["capped"]) >= 0.5 * len(big)
        row_info.append({"box": tuple(int(v) for v in r["box"]), "core": np.isin(lab, rid), "under_icon": capped})
    # 점수 = 가장 긴 줄의 글자 수 × 대비 (어두운 글자와 그 번짐이 둘 다 줄로 잡히면 대비가 큰 쪽이 글자다)
    score = max(r["n_glyph"] for r in rows) * float(T[core].mean())
    return {"T": T, "core": core, "guard": guard, "hg": hg, "rows": row_info, "score": score, "level": level}


def picture_text_mask(img: np.ndarray, box: tuple[int, int, int, int], protect: np.ndarray | None = None,
                      params: PictureParams | None = None) -> PictureText | None:
    """그림 배경 위 글자: img(상자 둘레에 여유가 있는 자른 그림, H×W×3 uint8) 안 box의 글자 픽셀.
    글자 줄을 찾지 못하면 None."""
    p = params or PictureParams()
    H, W = img.shape[:2]
    best, pol = None, 0
    for polarity in (-1, 1):
        d = _detect_polarity(img, box, polarity, p)
        if d is not None and (best is None or d["score"] > best["score"]):
            best, pol = d, polarity
    if best is None:
        return None
    core, guard, hg, T = best["core"], best["guard"], best["hg"], best["T"]
    ok = np.ones((H, W), bool) if protect is None else ~protect
    core = core & ok
    if not core.any():
        return None
    # 지킬 덩어리보다 글자에 가까운 곳까지만 넓힌다
    d_core = cv2.distanceTransform((~core).astype(np.uint8), cv2.DIST_L2, 3)
    if guard.any():
        d_guard = cv2.distanceTransform((~guard).astype(np.uint8), cv2.DIST_L2, 3)
    else:
        d_guard = np.full((H, W), 1e6, np.float32)
    mine = (d_core <= d_guard) & ok & ~guard
    grown = core.copy()
    weak = T > p.weak
    k3 = np.ones((3, 3), np.uint8)
    for _ in range(max(2, int(round(p.shadow_reach * hg)))):  # 글자에서 이어진 그림자·획 가장자리
        grown |= cv2.dilate(grown.astype(np.uint8), k3).astype(bool) & weak & mine
    # 닫힌 빈칸 (획에 둘러싸인 곳, 테두리 두른 흰 글자의 속)
    n, lab, st, _ = cv2.connectedComponentsWithStats((~grown).astype(np.uint8), connectivity=4)
    lim = (0.9 * hg) ** 2
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in st[i])
        if x == 0 or y == 0 or x + w >= W or y + h >= H:
            continue
        if a <= lim and not (guard & (lab == i)).any():
            grown |= (lab == i) & ok
    # 번짐·남은 그림자: 글자 둘레(absorb_reach) 안에서, 그 둘레 밖의 픽셀로 어림한 배경과 다른 픽셀.
    # 글자 둘레의 흰 번짐을 남기면 채울 때 그 흰색이 글자 자리로 번져 글자 모양 얼룩이 남는다
    I = img.astype(np.float32)
    reach = max(2, int(round(p.absorb_reach * hg)))
    d_grown = cv2.distanceTransform((~grown).astype(np.uint8), cv2.DIST_L2, 3)
    far = (d_grown > reach) & ~cv2.dilate(guard.astype(np.uint8), k3).astype(bool)
    if far.any():
        bg = smooth_background(I, far, max(2.0, 0.7 * reach))
        halo = (d_grown <= reach) & (np.abs(I - bg).max(axis=2) > p.absorb_diff)
    else:
        halo = np.zeros((H, W), bool)
    base = max(1, int(round(p.halo * hg)))
    mask = (cv2.dilate(grown.astype(np.uint8), _disk(base)).astype(bool) | halo) & mine
    mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, k3).astype(bool) & mine
    mask |= grown
    avoid = weak & ~mask
    rows = sorted(best["rows"], key=lambda r: r["box"][1])
    return PictureText(mask, core, guard, avoid, float(hg), pol, rows)


def drop_label_rows(pic: PictureText, n_drop: int) -> PictureText:
    """원문 줄 수보다 글자 줄이 많을 때: 아이콘 바로 아래에 붙은 이름표 줄(아이콘의 일부로 읽혀 원문에 없는 것)을
    작은 것부터 n_drop개까지 지울 줄에서 뺀다. 그런 줄이 없으면 그대로."""
    cands = sorted((r for r in pic.rows if r["under_icon"]), key=lambda r: int(r["core"].sum()))[:max(0, n_drop)]
    if not cands or len(cands) == len(pic.rows):
        return pic
    mask, core, guard = pic.mask.copy(), pic.core.copy(), pic.guard.copy()
    for r in cands:
        rest = core & ~r["core"]
        d_row = cv2.distanceTransform((~r["core"]).astype(np.uint8), cv2.DIST_L2, 3)
        d_rest = cv2.distanceTransform((~rest).astype(np.uint8), cv2.DIST_L2, 3) if rest.any() else np.full(mask.shape, 1e6, np.float32)
        mask &= ~(d_row < d_rest)
        core = rest
        guard |= r["core"]
    rows = [r for r in pic.rows if all(r is not c for c in cands)]
    return PictureText(mask, core, guard, pic.avoid | (pic.mask & ~mask), pic.glyph_px, pic.polarity, rows)


def _column_fill(I: np.ndarray, hole: np.ndarray, src: np.ndarray,
                 max_span: float = 1e9) -> tuple[np.ndarray, np.ndarray]:
    """구멍 픽셀을 같은 열의 위·아래 첫 바깥 픽셀 사이에서 선형 보간한다. (값, 비용).
    src: 재료로 써도 되는 픽셀. 위·아래 첫 바깥 픽셀이 재료가 아니면(지킬 덩어리 등) 그쪽은 쓰지 않는다.
    비용 = 위·아래 값의 차이(둘이 비슷할수록 그 방향이 그림의 결과 맞다). 한쪽만 있으면 큰 비용, 둘 다 없으면 inf."""
    H, W = hole.shape
    rows = np.arange(H, dtype=np.int32)[:, None]
    cols = np.broadcast_to(np.arange(W, dtype=np.int32)[None, :], (H, W))
    up = np.maximum.accumulate(np.where(~hole, rows, -1), axis=0)
    dn = np.minimum.accumulate(np.where(~hole, rows, H)[::-1], axis=0)[::-1]
    upc, dnc = np.clip(up, 0, H - 1), np.clip(dn, 0, H - 1)
    ok_u = (up >= 0) & src[upc, cols]
    ok_d = (dn < H) & src[dnc, cols]
    vu, vd = I[upc, cols], I[dnc, cols]
    span = np.maximum(dn - up, 1).astype(np.float32)
    t = ((rows - up) / span)[..., None]
    both = ok_u & ok_d
    val = np.where(both[..., None], vu * (1 - t) + vd * t, np.where(ok_u[..., None], vu, vd))
    cost = np.where(both, np.abs(vu - vd).sum(axis=2) + 0.3 * span,
                    np.where(ok_u | ok_d, 90.0 + 2.0 * span, np.inf)).astype(np.float32)
    # 위·아래 재료가 너무 멀면(글자 몇 줄 높이) 그 사이를 이은 값은 믿을 수 없다 (상자 밖 그림 색이 끌려온다)
    cost = np.where(span > max_span, np.inf, cost).astype(np.float32)
    return val.astype(np.float32), cost


FILL_SLOPES = (-3.0, -2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 3.0)  # 이어 볼 기울기 (가로 px / 세로 px)


def directional_fill(img: np.ndarray, hole: np.ndarray, src: np.ndarray,
                     slopes: tuple[float, ...] = FILL_SLOPES,
                     slope_penalty: float = 4.0, max_span: float = 1e9) -> tuple[np.ndarray, np.ndarray]:
    """구멍(hole)을 그림의 결을 따라 이어 채운다. (채운 값 H×W×3 float, 채우지 못한 픽셀).

    글자 줄을 지운 자리는 가로로 긴 띠다. 띠를 가로지르는 그림의 선(비스듬한 띠·옷 주름)이 끊기지 않게,
    여러 기울기로 위·아래 경계를 이어 보고(그림을 그 기울기만큼 밀어 놓고 세로로 보간) 픽셀마다 위·아래 색이
    가장 잘 맞는 기울기의 값을 쓴다. 일반 인페인팅은 이런 선을 계단처럼 끊는다.
    """
    H, W = hole.shape
    I = img.astype(np.float32)
    # 경계 픽셀 하나의 잡음이 줄무늬로 늘어나지 않게, 재료는 재료끼리만 살짝 고른 값
    w = src.astype(np.float32)
    Is = cv2.GaussianBlur(I * w[..., None], (0, 0), 0.9) / np.maximum(cv2.GaussianBlur(w, (0, 0), 0.9), 1e-4)[..., None]
    Is = np.where(src[..., None], Is, I)
    pad = int(max(abs(v) for v in slopes) * H / 2) + 2
    Ip = cv2.copyMakeBorder(Is, 0, 0, pad, pad, cv2.BORDER_REPLICATE)
    hole_p = cv2.copyMakeBorder(hole.astype(np.uint8), 0, 0, pad, pad, cv2.BORDER_CONSTANT, value=0)
    src_p = cv2.copyMakeBorder(src.astype(np.uint8), 0, 0, pad, pad, cv2.BORDER_CONSTANT, value=0)
    size = (W + 2 * pad, H)
    vals, costs = [], []
    for sl in slopes:
        M = np.array([[1.0, sl, -sl * H / 2.0], [0.0, 1.0, 0.0]], np.float32)
        Ish = cv2.warpAffine(Ip, M, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        hsh = cv2.warpAffine(hole_p, M, size, flags=cv2.INTER_NEAREST, borderValue=0).astype(bool)
        ssh = cv2.warpAffine(src_p, M, size, flags=cv2.INTER_NEAREST, borderValue=0).astype(bool) & ~hsh
        v, c = _column_fill(Ish, hsh, ssh, max_span)
        c = np.where(np.isfinite(c), c, 1e4).astype(np.float32)
        v = cv2.warpAffine(v, M, size, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE)
        c = cv2.warpAffine(c, M, size, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderValue=1e4)
        vals.append(v[:, pad:pad + W])
        # 기울기는 이웃 픽셀과 함께 고른다 (픽셀마다 따로 고르면 얼룩덜룩해진다)
        costs.append(cv2.blur(c[:, pad:pad + W], (5, 5)) + slope_penalty * abs(sl))
    C = np.stack(costs)
    best = np.argmin(C, axis=0)
    out = np.take_along_axis(np.stack(vals), best[None, ..., None], axis=0)[0]
    failed = hole & (np.min(C, axis=0) >= 5e3)
    return out, failed


def fill_picture(img: np.ndarray, mask: np.ndarray, guard: np.ndarray | None = None, glyph_px: float = 10.0,
                 protect: np.ndarray | None = None, params: PictureParams | None = None,
                 seed: int = 0, avoid: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """그림 배경의 글자 자리(mask)를 채운다. (채운 그림, 바뀐 픽셀).

    주변 그림을 결을 따라 이어 채우고(directional_fill), 주변과 같은 세기의 결(스캔 잡음)을 얹어 뭉개진 자국이
    덜 보이게 한다. guard(지킬 덩어리)와 protect는 바꾸지 않고, 그 색(아이콘 외곽선의 어두운 색)과
    avoid(아이콘 그림자처럼 바탕이 아닌 픽셀)는 채울 값의 재료로도 쓰지 않는다.
    """
    p = params or PictureParams()
    m = mask.copy()
    H, W = m.shape
    stay = np.zeros((H, W), bool)
    if guard is not None:
        stay |= guard
    if protect is not None:
        stay |= protect
    m &= ~stay
    if not m.any():
        return img.copy(), m
    I = img.astype(np.float32)
    radius = int(np.clip(round(0.5 * glyph_px), 3, 9))
    # 지킬 덩어리와 그 가장자리(안티에일리어싱)는 재료가 아니다
    bad = cv2.dilate(stay.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    if avoid is not None:
        bad |= avoid
    src = ~m & ~bad
    if p.fill == "directional":
        out, failed = directional_fill(img, m, src, max_span=p.max_span * glyph_px)
    else:
        out, failed = I.copy(), m.copy()
    if failed.any():  # 위·아래로 이을 재료가 없는 곳 (아이콘 사이에 낀 곳): 일반 인페인팅
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        hole = m | (cv2.dilate(m.astype(np.uint8), _disk(radius + 1)).astype(bool) & bad)
        flag = cv2.INPAINT_NS if p.fill == "ns" else cv2.INPAINT_TELEA
        tel = cv2.cvtColor(cv2.inpaint(bgr, hole.astype(np.uint8) * 255, radius, flag), cv2.COLOR_BGR2RGB)
        out = np.where(failed[..., None], tel.astype(np.float32), out)
    if p.fill_smooth > 0:  # 채운 값의 줄무늬를 구멍 안에서만 고른다 (가장자리는 이어 붙인 값 그대로)
        sm = cv2.GaussianBlur(np.where(m[..., None], out, I), (0, 0), p.fill_smooth)
        d_in = cv2.distanceTransform(m.astype(np.uint8), cv2.DIST_L2, 3)
        a = np.clip(d_in / max(1.0, 2 * p.fill_smooth), 0.0, 1.0)[..., None]
        out = np.where(m[..., None], a * sm + (1 - a) * out, out)
    # 결: 주변(구멍 밖, 뚜렷한 획 아닌 곳) 고주파의 세기를 재서 같은 세기의 잡음을 얹는다
    if p.grain > 0:
        near = cv2.dilate(m.astype(np.uint8), _disk(max(6, int(glyph_px)))).astype(bool) & src
        lum = luminance(I)
        hp = lum - cv2.GaussianBlur(lum, (0, 0), 1.2)
        if near.sum() >= 30:
            v = hp[near]
            sig = float(np.median(np.abs(v - np.median(v))) * 1.4826)
            sig = float(np.clip(sig, 0.0, 6.0)) * p.grain
            if sig > 0.3:
                rng = np.random.default_rng(seed)
                g = cv2.GaussianBlur(rng.standard_normal((H, W)).astype(np.float32), (0, 0), 0.6)
                g = g - cv2.GaussianBlur(g, (0, 0), 1.2)
                g *= sig / max(float(g.std()), 1e-6)
                out = out + g[..., None]
    res = img.copy()
    res[m] = np.clip(np.rint(out[m]), 0, 255).astype(np.uint8)
    return res, m


PICTURE_MODE = "picture"  # 영역·카드 지우기 방식 키 (service.ERASE_MODES)


def source_has_no_text(source, has_source: bool) -> bool:
    """읽은 원문에 지울 글자가 없음 (비었거나 아이콘·숫자뿐): 이 칸은 일부러 지우지 않는다."""
    from app import erase_text

    return has_source and isinstance(source, str) and all(erase_text.is_icon_line(t) for t in source.split("\n"))


def region_text_mask(sub: np.ndarray, region_box: tuple[int, int, int, int], protect: np.ndarray | None, tm,
                     source, has_source: bool):
    """erase_page가 영역 하나에 부른다: (PictureText 또는 None, 쓸 TextMask).

    읽은 원문에 글자가 없으면(비었거나 아이콘·숫자뿐) 아무것도 지우지 않는다. 글자 줄을 못 찾으면 (None, 원래 tm):
    기존 방식으로 지운다. 원문의 글 줄보다 찾은 줄이 많으면 아이콘 아래 이름표부터 남긴다.
    그림 배경에서는 ‘밝은 글자 상자로 좁히기’를 쓰지 않고 원래 영역에서 찾는다."""
    from app import erase_text

    if source_has_no_text(source, has_source):
        tm.mask[:] = False
        tm.core[:] = False
        tm.notes = ["복잡한 그림 배경: 원문에 글자가 없어 지우지 않음"]
        return None, tm
    pic = picture_text_mask(sub, region_box, protect)
    if pic is not None and isinstance(source, str):
        n_text = sum(1 for t in source.split("\n") if t.strip() and not erase_text.is_icon_line(t))
        if n_text and len(pic.rows) > n_text:
            pic = drop_label_rows(pic, len(pic.rows) - n_text)
    if pic is None:
        return None, tm
    return pic, erase_text.TextMask(pic.mask.copy(), pic.core.copy(), True, tm.texture, pic.glyph_px,
                                    ["복잡한 그림 배경: 줄지어 선 가는 획을 글자로 봄"], tm.dividers)


def fill_region(sub: np.ndarray, tm, pic: PictureText, protect: np.ndarray | None, seed: int = 0):
    """찾은 글자 자리를 채운다: (채운 그림, 바뀐 픽셀). tm.mask를 넓힐 수 있다 (글 속 아이콘 상자에 걸친 덩어리)."""
    prot = protect if protect is not None else np.zeros(tm.mask.shape, bool)
    # 일부러 지울 자리(글 속 아이콘 상자)에 많이 걸친 덩어리는 통째로 지운다 (상자 밖으로 삐져나온 조각이 남지 않게)
    # (글 속 아이콘 크기의 덩어리만: 큰 아이콘·상자 테두리는 일부가 걸쳐도 건드리지 않는다)
    n_g, lab_g, st_g, _c = cv2.connectedComponentsWithStats(pic.guard.astype(np.uint8), connectivity=8)
    if n_g > 1:
        tot = st_g[:, cv2.CC_STAT_AREA]
        cov = np.bincount(lab_g[tm.mask], minlength=n_g)
        small = np.maximum(st_g[:, cv2.CC_STAT_WIDTH], st_g[:, cv2.CC_STAT_HEIGHT]) <= 2.5 * pic.glyph_px
        whole = (cov >= 0.25 * tot) & (cov > 0) & small
        whole[0] = False
        extra = whole[lab_g] & ~prot
        tm.mask |= cv2.dilate(extra.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool) & ~prot
    # 지킬 덩어리: 아이콘·그림의 뚜렷한 획 + 원문에 없어 남긴 줄. 일부러 지울 자리(글 속 아이콘)는 뺀다
    guard = (pic.guard | pic.core) & ~tm.mask
    return fill_picture(sub, tm.mask, guard, pic.glyph_px, prot, seed=seed, avoid=pic.avoid & ~tm.mask)


def picture_meta(pic: PictureText | None) -> dict:
    """페이지 메타에 남길 찾기 정보."""
    return {"detector": "rows" if pic is not None else "color", "fill": "directional" if pic is not None else "auto",
            "glyph_px": round(pic.glyph_px, 2) if pic is not None else None,
            "polarity": (("dark" if pic.polarity < 0 else "light") if pic is not None else None),
            "rows": len(pic.rows) if pic is not None else 0}
