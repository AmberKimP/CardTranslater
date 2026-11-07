"""글자 모양만 지우기 (텍스트 레이어가 없는 그림 카드). PRD 5.4.

영역 상자 전체를 덮지 않고 **글자 픽셀만** 찾아, 그 픽셀만 주변에 맞춰 채운다.

글자 찾기 (``text_mask``)
  1. 큰 중앙값 흐림(글자 획보다 넓은 창)을 배경으로 보고, 배경과 크게 다른 픽셀을 밝은 쪽·어두운 쪽으로 나눠
     덩어리로 묶는다. 흰 글자 + 검은 테두리 + 스테인드글라스 납선이 한 덩어리로 붙지 않게 하려는 것이다.
  2. 글자가 아닌 덩어리는 뺀다: 상자 밖으로 이어지는 것(그림·선), 상자만큼 큰 것(테두리), 긴 가로선(구분선),
     글자 크기의 3배를 넘는 것(테두리 곡선).
  3. 상자 배경이 고르면(종이·무늬) 남은 덩어리 중 주변이 바쁜 것(상자에 걸친 그림 조각)을 빼되, 주요 글자색과 같은
     것(획이 빽빽한 한자)은 되살린다. 배경이 그림이면 덩어리를 색으로 묶어 ‘가장 넓고 상자 밖에 드문 색’ 묶음만 글자로 본다.
  4. 글자에 붙은 테두리·그림자(배경과 조금 다른 픽셀)를 몇 px 넓혀 포함하고, 글자 속 닫힌 빈칸(코인 아이콘 속 등)을 채운다.

채우기 (``fill_text``) — 영역 설정 ``erase.mode``(카드별로 바꿀 수 있음)
  auto      배경을 보고 고름: 단색 -> solid, 무늬 -> pattern, 그림 -> inpaint (그림이면 ‘방식 확인’ 표시)
  solid     글자 자리를 주변 색(매끄러운 배경 추정)으로 덮음
  pattern   글자 자리를 주변 무늬로 이어 채움 (복원, 좁은 반경)
  rect_avg  글자 줄마다 사각형으로 지우고 사각형 둘레의 평균색으로 채움
  inpaint   글자 뒤 그림을 주변에서 유추해 채움 (복원, 넓은 반경)
numpy 배열만 다룬다(PDF·화면을 모름).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import re
import cv2
import numpy as np

MODES = ("auto", "solid", "pattern", "rect_avg", "inpaint")
MODE_LABELS = {
    "auto": "자동 (배경 보고 고름)",
    "solid": "단색: 주변 색으로 덮기",
    "pattern": "무늬: 주변 무늬로 채우기",
    "rect_avg": "그림: 사각형 + 평균색",
    "inpaint": "그림: 뒤 그림 유추해 채우기",
}
STRONG = 60.0  # 배경과 이만큼(RGB 최대 채널) 다르면 글자 후보
WEAK = 30.0  # 글자에 붙은 테두리·그림자로 넓힐 때의 기준
COLOR_TOL = 45.0  # 같은 색으로 묶는 거리
BOLD_WEIGHT = 0.13  # 획 두께 / 글자 높이가 이 이상이면 굵은 글자


def _disk(r: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def _lum(a: np.ndarray) -> np.ndarray:
    return a @ np.array([0.299, 0.587, 0.114], np.float32)


def _local_std(lum: np.ndarray, k: int = 15) -> np.ndarray:
    m1 = cv2.blur(lum, (k, k))
    m2 = cv2.blur(lum * lum, (k, k))
    return np.sqrt(np.maximum(m2 - m1 * m1, 0))


@dataclass
class TextMask:
    mask: np.ndarray  # 지울 픽셀 (글자 + 테두리 + 닫힌 빈칸)
    core: np.ndarray  # 글자로 고른 덩어리 (넓히기 전)
    busy: bool  # 상자 배경이 그림(바쁨)인가
    texture: float  # 상자 배경의 국소 표준편차 중앙값
    glyph_px: float = 0.0  # 글자 크기 추정 (px)
    notes: list[str] = field(default_factory=list)
    dividers: list[tuple[int, int, int, int]] = field(default_factory=list)  # 남긴 가로 구분선 (x0,y0,x1,y1)


def _outlined(I: np.ndarray, c) -> bool:
    """덩어리 둘레(2~4px 고리)가 고른 한 색이고 덩어리 색과 크게 다르면 테두리 두른 글자 획."""
    comp = c[0]
    ring = cv2.dilate(comp.astype(np.uint8), _disk(4)).astype(bool) & ~cv2.dilate(comp.astype(np.uint8), _disk(1)).astype(bool)
    if ring.sum() < 20:
        return False
    rp = I[ring]
    med = np.median(rp, axis=0)
    coherent = float((np.abs(rp - med).max(axis=1) < COLOR_TOL).mean())
    if coherent < 0.6 or float(np.abs(med - c[1]).max()) < 100:
        return False
    # 글자 테두리는 흰 글자 + 어두운 테두리(또는 그 반대)다. 납선에 둘러싸인 색유리 조각은 빼다
    col = np.asarray(c[1])
    light_on_dark = col.min() >= 190 and med.max() <= 110
    dark_on_light = col.max() <= 80 and med.min() >= 170
    return bool(light_on_dark or dark_on_light)


def text_mask(img: np.ndarray, box: tuple[int, int, int, int], protect: np.ndarray | None = None) -> TextMask:
    """img(H×W×3 uint8, 상자 둘레에 여유가 있는 자른 그림) 안 box(x0,y0,x1,y1)의 글자 픽셀."""
    x0, y0, x1, y1 = box
    H, W = img.shape[:2]
    bw, bh = max(1, x1 - x0), max(1, y1 - y0)
    I = img.astype(np.float32)
    k = int(max(31, min(81, bh * 0.6))) | 1  # 글자 획보다 넓은 창
    med = np.dstack([cv2.medianBlur(np.ascontiguousarray(img[..., c]), k) for c in range(3)]).astype(np.float32)
    D = np.abs(I - med).max(axis=2)
    # 색 라벨 띠(가로로 길고 속이 찬 덩어리) 안은 띠 색을 배경으로 본다: 넓은 창의 중앙값은 종이색이라
    # 띠 위 흰 글자가 배경과 다르게 보이지 않는다
    for pol in (1, -1):
        sl = ((D > STRONG) & (np.sign(_lum(I) - _lum(med)) == pol)).astype(np.uint8)
        n_, lab_, st_, _c = cv2.connectedComponentsWithStats(sl, connectivity=8)
        for i in range(1, n_):
            x, y, w, h, a_ = (int(v) for v in st_[i])
            if w > 0.25 * bw and w > 3 * h and h < 0.2 * bh and a_ >= 0.45 * w * h and h >= 12:
                comp = lab_ == i
                fill = np.median(I[comp], axis=0)
                inside = np.zeros((H, W), bool)
                inside[y:y + h, x:x + w] = True
                med[inside] = fill
    D = np.abs(I - med).max(axis=2)
    lum, lmed = _lum(I), _lum(med)
    inbox = np.zeros((H, W), bool)
    inbox[max(0, y0):y1, max(0, x0):x1] = True
    strong_all = D > STRONG
    near = cv2.dilate(strong_all.astype(np.uint8), _disk(4)).astype(bool)
    sel = inbox & ~near
    texture = float(np.median(_local_std(lum)[sel])) if sel.sum() > 50 else 99.0
    busy = texture >= 8.0

    cands = []  # (덩어리, 평균색, 넓이, 큰 변)
    edge_glyphs: set[int] = set()  # 위·아래 가장자리에 걸쳐 글자로 받아들인 덩어리 (id)
    lines = []  # 가로 구분선의 (세로 중심, 두께): 선 사이의 장식(◆)도 남긴다
    dividers = []
    for pol in (1, -1):
        strong = (strong_all & (np.sign(lum - lmed) == pol)).astype(np.uint8)
        n, lab, st, _ = cv2.connectedComponentsWithStats(strong, connectivity=8)
        for i in range(1, n):
            x, y, w, h, a = (int(v) for v in st[i])
            if a < 6:
                continue
            if not busy and w > 0.25 * bw and w > 4 * h and h < 0.15 * bh and a >= 0.45 * w * h:  # 종이 위 가로 구분선 (그림 위의 긴 'ー'는 글자)
                lines.append((y + h / 2, h, (x, y, x + w, y + h)))
                continue
            if w > 0.6 * bw or h > 0.8 * bh:  # 테두리·큰 그림
                continue
            comp = lab == i
            out_frac = (comp & ~inbox).sum() / a
            if out_frac > 0.03:  # 상자 밖으로 이어지는 그림·선
                # 영역을 글자에 조금 빠듯하게 그려 위·아래 가장자리에 걸친 글자 조각: 대부분 안에 있고 글자 크기이면
                # 글자로 보고 상자를 그만큼 넓힌다 (좌우로 걸친 것·큰 것은 그림·테두리로 두고 그대로 뺀다)
                edge = (y < y0 <= y + h or y < y1 <= y + h) and x >= x0 and x + w <= x1
                if not (edge and out_frac <= 0.6 and h <= 0.5 * bh and w <= 0.2 * bw):
                    continue
                inbox[max(0, y - 2):min(H, y + h + 2), max(0, x - 2):min(W, x + w + 2)] = True
                edge_glyphs.add(id(comp))
            cands.append((comp, I[comp].mean(axis=0), a, max(w, h)))
    # 가로로 긴 덩어리 중 글자보다 두꺼운 것은 구분선이 아니라 색 라벨(예: 빨간 ‘プレイ時効果’ 띠): 라벨은 남기고
    # 안의 글자는 글자로 본다. 진짜 구분선은 글자 획 굵기쯤으로 가늘다
    sizes0 = [c[3] for c in cands if c[2] >= 30]
    glyph0 = float(np.percentile(sizes0, 75)) if sizes0 else 0.0
    lines = [ln for ln in lines if not glyph0 or ln[1] <= max(4.0, 0.35 * glyph0)]
    for _cy, _h, (lx0, ly0, lx1, ly1) in lines:
        if x0 <= (lx0 + lx1) / 2 <= x1 and y0 <= (ly0 + ly1) / 2 <= y1:
            dividers.append((lx0, ly0, lx1, ly1))

    def on_line(c):
        ys, xs = np.nonzero(c[0])
        cy, ch = (ys.min() + ys.max()) / 2, ys.max() - ys.min() + 1
        return any(abs(cy - ly) <= max(3.0, lh) and ch <= 4 * lh + 6 for ly, lh, _b in lines)

    cands = [c for c in cands if not on_line(c)]  # 구분선 위의 작은 장식
    sizes = [c[3] for c in cands if c[2] >= 30]
    glyph = float(np.percentile(sizes, 75)) if sizes else 0.0
    if glyph:
        cands = [c for c in cands if c[3] <= 3.0 * glyph]  # 테두리 곡선·큰 그림

    keep = np.zeros((H, W), bool)
    notes: list[str] = []
    if not busy:
        lum2 = np.where(near, lmed, lum)  # 글자 근처는 배경값으로 바꿔 놓고 바쁜 곳을 잰다
        busy_map = _local_std(lum2) > 2.5 * texture + 8
        busy_map = cv2.dilate(busy_map.astype(np.uint8), _disk(10)).astype(bool)
        def on_label(c) -> bool:
            """색 라벨 위 글자: 둘레가 한 색으로 고르고(종이색 아님) 글자와 대비가 크다."""
            ring = cv2.dilate(c[0].astype(np.uint8), _disk(5)).astype(bool) & ~cv2.dilate(
                c[0].astype(np.uint8), _disk(1)).astype(bool)
            if ring.sum() < 15:
                return False
            rp = I[ring]
            med = np.median(rp, axis=0)
            if paper0 is not None and float(np.abs(med - paper0).max()) < 40:
                return False  # 둘레가 종이색이면 라벨이 아니다 (종이 위 글자는 원래 규칙대로)
            return (float((np.abs(rp - med).max(axis=1) < 30).mean()) >= 0.75
                    and float(np.abs(med - c[1]).max()) >= 90)

        paper0 = np.median(I[sel], axis=0) if sel.sum() > 50 else None
        kept, dropped = [], []
        for c in cands:
            if on_label(c):
                kept.append((c, False))
                continue
            ys, xs = np.nonzero(c[0])
            # 위·아래 가장자리에 걸친 글자로 이미 받아들인 것은 그쪽 가장자리에 닿아도 그림 조각으로 보지 않는다
            # 위·아래에만 닿은 글자 크기 덩어리는 "tb": 영역을 빠듯하게 그려 글자 밑동이 닿은 것일 수 있다
            touch = "lr" if (xs.min() <= x0 + 2 or xs.max() >= x1 - 3) else (
                "tb" if id(c[0]) not in edge_glyphs and (ys.min() <= y0 + 2 or ys.max() >= y1 - 3) else "")
            ov = (c[0] & busy_map).sum() / c[2]
            (dropped if ov > 0.3 or (touch and ov > 0.05) else kept).append((c, touch))
        # 글자보다 길고 속이 빈 가는 곡선(패널 테두리 조각)은 글자가 아니다
        def thin_edge(c, _t) -> bool:
            ys, xs = np.nonzero(c[0])
            w, h = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
            return bool(glyph) and max(w, h) > 1.5 * glyph and c[2] < 0.25 * w * h

        kept = [c for c, t in kept if not thin_edge(c, t)]
        dropped = [(c, t) for c, t in dropped if not thin_edge(c, t)]
        # 종이 위 글자는 둘레가 종이색이다. 둘레가 그림(유리 조각·테두리)인 조각은 빼다
        paper = np.median(I[sel], axis=0) if sel.sum() > 50 else None
        free = ~cv2.dilate(strong_all.astype(np.uint8), _disk(2)).astype(bool) & inbox

        def on_paper(c) -> bool:
            if paper is None:
                return True
            ring = cv2.dilate(c[0].astype(np.uint8), _disk(6)).astype(bool) & free
            if ring.sum() < 15:
                return True
            if float((np.abs(I[ring] - paper).max(axis=1) < 45).mean()) >= 0.5:
                return True
            return on_label(c)  # 색 라벨 위 글자 (유리 조각은 둘레가 여러 색이라 빠진다)

        kept = [c for c in kept if on_paper(c)]
        dropped = [(c, t) for c, t in dropped if on_paper(c)]
        if kept:  # 획이 빽빽한 글자는 주변이 바빠 보인다: 주요 글자색이고 가장자리에 닿지 않으면 되살린다
            tot = sum(c[2] for c in kept)
            main = []
            for c in sorted(kept, key=lambda c: -c[2]):
                if any(np.abs(m - c[1]).max() < COLOR_TOL for m in main):
                    continue
                if sum(d[2] for d in kept if np.abs(d[1] - c[1]).max() < COLOR_TOL) / tot >= 0.2:
                    main.append(c[1])
            # 위·아래에만 닿은 것도 글자 크기면 되살린다 (빽빽한 글자 ‘殻城’의 밑동이 빠듯한 영역 끝에 닿은 경우)
            kept += [c for c, t in dropped if (not t or (t == "tb" and glyph and c[3] <= 2.0 * glyph))
                     and any(np.abs(m - c[1]).max() < 35 for m in main)]
        for c in kept:
            keep |= c[0]
    else:
        ring_out = cv2.dilate(inbox.astype(np.uint8), _disk(14)).astype(bool) & ~inbox
        out_cols = I[ring_out]
        groups: list[dict] = []
        for c in sorted(cands, key=lambda c: -c[2]):
            for g in groups:
                if np.abs(g["color"] - c[1]).max() < COLOR_TOL:
                    g["items"].append(c)
                    g["area"] += c[2]
                    break
            else:
                groups.append({"color": c[1], "items": [c], "area": c[2]})
        best, best_score = None, 0.0
        for g in groups:
            g["rare"] = float((np.abs(out_cols - g["color"]).max(axis=1) < COLOR_TOL).mean()) if len(out_cols) else 0.0
            g["outlined"] = sum(c[2] for c in g["items"] if _outlined(I, c)) / max(1, g["area"])
            score = g["area"] * max(0.0, 1 - g["rare"] * 8)
            if score > best_score:
                best, best_score = g, score
        # 테두리 두른 글자(흰 글자 + 검은 외곽선 등): 둘레가 고르고 글자와 대비가 큰 색 묶음이 있으면 그것이 글자다.
        # 상자 밖에 같은 색(예: 띠 위아래 흰 패널)이 많아도 놓치지 않게 ‘드묾’보다 먼저 본다.
        lined = [g for g in groups if g["outlined"] >= 0.5 and g["area"] >= 0.3 * max(gg["area"] for gg in groups)]
        if lined:
            best = max(lined, key=lambda g: g["area"] * g["outlined"])
        for g in groups:
            if g is best or g["rare"] < 0.005:
                for c in g["items"]:
                    keep |= c[0]
        if best is None:
            notes.append("그림 배경에서 글자색을 찾지 못함")

    grown = keep.copy()
    weak = D > WEAK
    for _ in range(4):  # 글자에 붙은 테두리·그림자
        grown |= cv2.dilate(grown.astype(np.uint8), _disk(2)).astype(bool) & weak & inbox
    inv = (~grown).astype(np.uint8)  # 닫힌 빈칸 채우기 (글자 속, 코인 속)
    n, lab, st, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
    lim = (0.5 * bh) ** 2 if busy else (0.25 * bh) ** 2
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in st[i])
        if x == 0 or y == 0 or x + w >= W or y + h >= H:
            continue
        if a <= lim and w < 0.3 * bw:
            grown |= lab == i
    # 번짐: 종이 1px, 그림 배경은 흰 채움·테두리 잔상이 남지 않게 3px
    grown = cv2.dilate(grown.astype(np.uint8), _disk(3 if busy else 1)).astype(bool) & inbox
    if protect is not None:
        grown &= ~protect
    return TextMask(grown, keep, busy, texture, glyph, notes, dividers)


def background_kind(img: np.ndarray, tm: TextMask) -> str:
    """글자 주변 배경: solid(단색) | pattern(무늬) | image(그림)."""
    if tm.busy:
        return "image"
    ring = cv2.dilate(tm.mask.astype(np.uint8), _disk(8)).astype(bool) & ~tm.mask
    if ring.sum() < 20:
        return "solid"
    # 글자를 뺀 주변만으로 고주파(무늬) 세기를 잰다 (완만한 색 변화는 단색으로 본다)
    lum = _lum(img.astype(np.float32))
    known = (~tm.mask).astype(np.float32)
    smooth = cv2.GaussianBlur(lum * known, (0, 0), 4) / np.maximum(cv2.GaussianBlur(known, (0, 0), 4), 1e-4)
    s = float(np.std((lum - smooth)[ring]))
    return "solid" if s < 2.0 else "pattern"


def _rects(mask: np.ndarray, glyph: float) -> list[tuple[int, int, int, int]]:
    """글자 줄마다 사각형 (가로로 이어 붙여 묶음)."""
    gx = int(max(5, glyph * 0.8)) | 1
    joined = cv2.dilate(mask.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_RECT, (gx, 3)))
    n, _lab, st, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    return [(int(st[i, 0]), int(st[i, 1]), int(st[i, 0] + st[i, 2]), int(st[i, 1] + st[i, 3])) for i in range(1, n)]


def fill_text(img: np.ndarray, tm: TextMask, mode: str = "auto",
              box: tuple[int, int, int, int] | None = None) -> tuple[np.ndarray, str, np.ndarray]:
    """(채운 그림, 실제로 쓴 방식, 바뀐 픽셀). 바뀌는 픽셀은 글자 자리(rect_avg면 줄 사각형)뿐이다."""
    if mode not in MODES:
        mode = "auto"
    kind = background_kind(img, tm)
    used = mode if mode != "auto" else {"solid": "solid", "pattern": "pattern", "image": "inpaint"}[kind]
    out = img.copy()
    m = tm.mask
    if not m.any():
        return out, used, m
    if used == "rect_avg":
        changed = np.zeros(m.shape, bool)
        H, W = m.shape
        bx0, by0, bx1, by1 = box or (0, 0, W, H)
        for x0, y0, x1, y1 in _rects(m, tm.glyph_px):
            x0, y0, x1, y1 = max(bx0, x0 - 1), max(by0, y0 - 1), min(bx1, x1 + 1), min(by1, y1 + 1)
            r = np.zeros(m.shape, bool)
            r[max(0, y0 - 3):min(H, y1 + 3), max(0, x0 - 3):min(W, x1 + 3)] = True
            r[y0:y1, x0:x1] = False
            r &= ~m
            col = img[r].mean(axis=0) if r.sum() else img[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)
            out[y0:y1, x0:x1] = np.clip(np.rint(col), 0, 255).astype(np.uint8)
            changed[y0:y1, x0:x1] = True
        return out, used, changed
    if used == "solid":
        known = ~cv2.dilate(m.astype(np.uint8), _disk(1)).astype(bool)
        f = img.astype(np.float32)
        w = cv2.GaussianBlur(known.astype(np.float32), (0, 0), 6)
        bgs = cv2.GaussianBlur(f * known[..., None], (0, 0), 6) / np.maximum(w, 1e-4)[..., None]
        for s in (12, 24, 48):  # 큰 글자 속처럼 주변이 먼 곳은 더 넓게
            hole = w < 0.02
            if not hole.any():
                break
            w2 = cv2.GaussianBlur(known.astype(np.float32), (0, 0), s)
            b2 = cv2.GaussianBlur(f * known[..., None], (0, 0), s) / np.maximum(w2, 1e-4)[..., None]
            bgs = np.where(hole[..., None], b2, bgs)
            w = np.where(hole, w2, w)
        out[m] = np.clip(np.rint(bgs[m]), 0, 255).astype(np.uint8)
        return out, used, m
    radius = 3 if used == "pattern" else 5
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    bgr = cv2.inpaint(bgr, m.astype(np.uint8) * 255, radius, cv2.INPAINT_TELEA)
    out = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    out[~m] = img[~m]
    return out, used, m


# ---------------------------------------------------------------- 원문 줄 모양 (한글 넣기용)
def _hex(c) -> str:
    c = np.clip(np.rint(np.asarray(c, np.float32)), 0, 255).astype(int)
    return "#%02x%02x%02x" % tuple(c)


def _bands(rows: np.ndarray, gap: int) -> list[list[int]]:
    out: list[list[int]] = []
    for i in np.nonzero(rows)[0]:
        if out and i - out[-1][1] <= gap + 1:
            out[-1][1] = int(i)
        else:
            out.append([int(i), int(i)])
    return out


def _seg_height(m: np.ndarray) -> float | None:
    """빈 세로줄로 나뉜 조각의 높이 중 큰 쪽(75%) = 대표 글자 높이 (작은 ー・、 를 피함)."""
    rows_all = np.nonzero(m.any(1))[0]
    if not len(rows_all):
        return None
    bh = rows_all[-1] - rows_all[0] + 1
    cols = m.any(0)
    hs, i, n = [], 0, len(cols)
    while i < n:
        if cols[i]:
            j = i
            while j < n and cols[j]:
                j += 1
            r = np.nonzero(m[:, i:j].any(1))[0]
            if j - i >= 0.25 * bh:
                hs.append(r[-1] - r[0] + 1)
            i = j
        else:
            i += 1
    return float(np.percentile(hs, 75)) if hs else float(bh)


def text_layout(img: np.ndarray, tm: TextMask, box: tuple[int, int, int, int]) -> dict:
    """지운 글자의 줄 모양: 줄마다 위치·대표 글자 높이·글자색·테두리색·굵기·정렬, 다른 색 낱말, 구분선.

    좌표는 img 픽셀. 한글 넣기가 원문 줄 자리·서식을 그대로 따르게 지우기 때 함께 기록한다.
    """
    x0, y0, x1, y1 = box
    bw = max(1, x1 - x0)
    I = img.astype(np.float32)
    inbox = np.zeros(tm.core.shape, bool)
    inbox[max(0, y0):y1, max(0, x0):x1] = True
    core = tm.core & inbox
    divs = [list(map(int, d)) for d in tm.dividers]
    if not core.any():
        return {"lines": [], "dividers": divs}
    glyph = tm.glyph_px or 10.0
    rows = core.sum(1)
    bands = _bands(rows >= max(1, 0.04 * rows.max()), max(2, int(glyph * 0.12)))  # 작은 티끌이 줄을 잇지 않게
    hmax = max(b - a + 1 for a, b in bands)
    bands = [b for b in bands if b[1] - b[0] + 1 >= 0.25 * hmax]
    bg_px = I[inbox & ~tm.mask]
    bg = np.median(bg_px, axis=0) if len(bg_px) > 20 else None
    lines = []
    for a, b in bands:
        sub = np.zeros_like(core)
        sub[a:b + 1] = core[a:b + 1]
        ys, xs = np.nonzero(sub)
        # 글자색: 덩어리마다 색을 재서, 가장 넓은 색 = 줄 기본색, 그와 다른 색 = 강조 낱말
        n, lab, st, _ = cv2.connectedComponentsWithStats(sub.astype(np.uint8), connectivity=8)
        comps = []
        for i in range(1, n):
            cm = lab == i
            e = cv2.erode(cm.astype(np.uint8), _disk(1)).astype(bool)
            col = np.median(I[e] if e.sum() >= 4 else I[cm], axis=0)
            comps.append((col, int(st[i, 4]), int(st[i, 0]), int(st[i, 0] + st[i, 2])))
        groups: list[dict] = []
        for col, area, cx0, cx1 in sorted(comps, key=lambda c: -c[1]):
            for g in groups:
                if np.abs(g["color"] - col).max() < COLOR_TOL:
                    g["area"] += area
                    g["x"].append((cx0, cx1))
                    break
            else:
                groups.append({"color": col, "area": area, "x": [(cx0, cx1)]})
        groups.sort(key=lambda g: -g["area"])
        tot = sum(g["area"] for g in groups) or 1
        main = groups[0]["color"]
        accents = [g for g in groups[1:] if g["area"] / tot >= 0.04 and np.abs(g["color"] - main).max() >= 80
                   and (bg is None or np.abs(g["color"] - bg).max() >= 60)]  # 코인 속 흰 곳 같은 배경색은 빼다
        accents.sort(key=lambda g: min(x for x, _ in g["x"]))
        gh = _seg_height(sub[a:b + 1]) or float(b - a + 1)
        # 테두리: 글자 바로 바깥 고리가 고른 색이고 글자색·배경색과 많이 다르면 외곽선이 있는 글자
        stroke, stroke_px = None, 0.0
        r = max(2, int(round(gh * 0.1)))
        ring = cv2.dilate(sub.astype(np.uint8), _disk(r)).astype(bool) & ~sub & inbox
        if ring.sum() >= 30:
            rp = I[ring]
            rmed = np.median(rp, axis=0)
            coherent = float((np.abs(rp - rmed).max(axis=1) < COLOR_TOL).mean())
            far_fill = np.abs(rmed - main).max() >= 90
            far_bg = bg is None or tm.busy or np.abs(rmed - bg).max() >= 70
            if coherent >= 0.55 and far_fill and far_bg:
                stroke = rmed
                near = (np.abs(I - rmed).max(axis=2) < COLOR_TOL) & ~sub
                dist = cv2.distanceTransform((~sub).astype(np.uint8), cv2.DIST_L2, 3)
                d = dist[near & (dist <= 4 * r) & inbox]
                stroke_px = float(np.median(d)) if len(d) else float(r)
        # 굵기: 획 두께(2 x 넓이 / 둘레) / 글자 높이
        cnts, _ = cv2.findContours(sub.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        per = sum(cv2.arcLength(c, True) for c in cnts) or 1.0
        weight = 2.0 * float(sub.sum()) / per / max(gh, 1.0)
        # 줄 폭: 글자에서 멀리 떨어진 작은 티끌(테두리 옆 점)은 빼고 잰다
        big = [(c[2], c[3]) for c in comps if c[1] >= 0.05 * gh * gh]
        lx0, lx1 = (min(u for u, _ in big), max(v for _, v in big)) if big else (int(xs.min()), int(xs.max()) + 1)
        for _col, area, u, v in comps:
            if (u >= lx1 and u - lx1 <= gh) or (v <= lx0 and lx0 - v <= gh):
                lx0, lx1 = min(lx0, u), max(lx1, v)
        lines.append({
            "bbox": [int(lx0), int(a), int(lx1), int(b) + 1],
            "glyph_h": round(gh, 2),
            "color": _hex(main),
            "accents": [{"color": _hex(g["color"]), "x": [[int(u), int(v)] for u, v in sorted(g["x"])]}
                        for g in accents],
            "stroke_color": _hex(stroke) if stroke is not None else None,
            "stroke_px": round(stroke_px, 2),
            "weight": round(weight, 3),
            "bold": weight >= BOLD_WEIGHT,
            # 줄의 세로 가운데: 잉크가 몰린 곳(행 10~90%) 기준. 「 」·ー 같은 튀어나온 획에 끌리지 않게
            "cy": round(float(np.mean(np.percentile(ys, [10, 90]))) + 0.5, 2),
        })
    # 정렬: 구분선으로 나뉜 묶음마다 본다. 줄 가운데가 상자 가운데에 가깝고, 묶음의 다른 줄보다 들어가 있거나
    # 묶음에 줄이 하나뿐이면 가운데 정렬
    cx = (x0 + x1) / 2
    cuts = sorted((d[1] + d[3]) / 2 for d in divs)

    def section(ln):
        return sum((ln["bbox"][1] + ln["bbox"][3]) / 2 > c for c in cuts)

    for ln in lines:
        left, _t, right, _b = ln["bbox"]
        mid = (left + right) / 2
        same = [o for o in lines if section(o) == section(ln)]
        lmin = min(o["bbox"][0] for o in same)
        if abs(mid - cx) <= 0.06 * bw and (left - lmin > 0.04 * bw or len(same) == 1):
            ln["align"] = "center"
        elif right >= x1 - 0.04 * bw and left - lmin > 0.15 * bw:
            ln["align"] = "right"
        else:
            ln["align"] = "left"
    return {"lines": lines, "dividers": divs}


_WORD = __import__("re").compile(r"[ぁ-んァ-ヶ一-龯々A-Za-zＡ-Ｚａ-ｚ가-힣]")
_TOKEN = __import__("re").compile(r"\[\[[^\[\]]*\]\]|</?(?:b|i|c\d+)>")


def is_icon_line(text: str) -> bool:
    """아이콘·숫자·기호만 있는 줄 (예: ‘4[[coin]] [[tile]]’). 번역해도 같으므로 원문 그대로 둔다."""
    return not _WORD.search(_TOKEN.sub("", text))


def _trim_stray_end(tm: "TextMask", ln: dict, text: str) -> bool:
    """줄 맨 앞·맨 뒤에 넓은 틈을 두고 떨어진 작은 조각(글자 하나 크기)이 원문에 없으면 줄에서 뺀다.
    예: 제목 영역 왼쪽 위 배지 안의 숫자 ‘5’가 ‘緊急クエスト’ 줄에 붙어 같이 지워지던 것.
    원문 그 끝이 숫자·기호면 진짜 글자일 수 있어 그대로 둔다. 줄 상자(ln["bbox"])를 고친다."""
    from app.icons import TOKEN_RE

    x0, y0, x1, y1 = ln["bbox"]
    gh = float(ln.get("glyph_h") or 0)
    plain = TOKEN_RE.sub("■", text).strip()
    if gh <= 0 or len(plain) < 2 or x1 - x0 < 4 * gh:
        return False
    cols = tm.mask[max(0, y0):y1, max(0, x0):x1].any(axis=0)
    xs = np.nonzero(cols)[0]
    if len(xs) < 2:
        return False
    # 잉크가 있는 열을 틈으로 나눈 덩이들
    segs, start = [], xs[0]
    for a, b in zip(xs[:-1], xs[1:]):
        if b - a > 1:
            segs.append((start, a + 1))
            start = b
    segs.append((start, xs[-1] + 1))
    if len(segs) < 2:
        return False
    changed = False
    for end in ("left", "right"):
        if len(segs) < 2:
            break
        s, nxt = (segs[0], segs[1]) if end == "left" else (segs[-1], segs[-2])
        gap = (nxt[0] - s[1]) if end == "left" else (s[0] - nxt[1])
        ch = plain[0] if end == "left" else plain[-1]
        if (gap >= 2.5 * gh and s[1] - s[0] <= 1.6 * gh
                and not re.match(r"[0-9０-９+＋\-−×xX■]", ch)):
            a, b = x0 + int(s[0]), x0 + int(s[1])
            tm.mask[max(0, y0 - int(gh)):y1 + int(gh), max(0, a - 2):b + 2] = False
            segs = segs[1:] if end == "left" else segs[:-1]
            changed = True
    if changed:
        ln["bbox"] = [x0 + int(segs[0][0]), y0, x0 + int(segs[-1][1]), y1]
    return changed


def _extra_lines(lines: list[dict], src: list[str]) -> list[dict]:
    """찾은 줄이 원문보다 많을 때 원문에 없는 줄을 고른다.
    줄 폭이 원문 줄 길이(글자 수)와 가장 잘 맞게 남는 줄을 뺀다 (원문이 빠뜨린 숫자 타일·카드 번호 등).
    폭으로 가리지 못하면 글자가 작은 줄부터 (예전 방식)."""
    from itertools import combinations

    from app.icons import TOKEN_RE, _line_units

    n_drop = len(lines) - len(src)
    if n_drop <= 0:
        return []

    def smallest() -> list[dict]:
        out: list[dict] = []
        while len(lines) - len(out) > len(src):
            out.append(min((ln for ln in lines if ln not in out), key=lambda ln: ln["glyph_h"]))
        return out

    if n_drop > 3 or len(lines) > 14:
        return smallest()
    widths = [ln["bbox"][2] - ln["bbox"][0] for ln in lines]
    units = [_line_units(t) for t in src]
    icon_only = [bool(TOKEN_RE.findall(t)) and not TOKEN_RE.sub("", t).strip() for t in src]
    best, best_key = None, None
    for drop in combinations(range(len(lines)), n_drop):
        keep = [i for i in range(len(lines)) if i not in drop]
        txt = [(i, j) for i, j in zip(keep, range(len(src))) if not icon_only[j]] or list(zip(keep, range(len(src))))
        r = sum(widths[i] for i, _j in txt) / max(sum(units[j] for _i, j in txt), 1e-6)
        err = sum(abs(widths[i] - units[j] * r) for i, j in txt) / max(sum(widths[i] for i, _j in txt), 1)
        key = (round(err, 2), sum(lines[i]["glyph_h"] for i in drop))
        if best_key is None or key < best_key:
            best, best_key = drop, key
    if best is None or best_key[0] >= 0.35:
        return smallest()
    return [lines[i] for i in best]


def match_source_lines(tm: TextMask, lay: dict, source: str, icon_rows: list | None = None) -> dict:
    """찾은 줄을 읽은 원문 줄에 맞춘다. tm.mask를 고친다(남길 줄은 지우지 않음).

    - 원문보다 줄이 많으면 가장 작은 글자 줄(카드 번호 ‘A-001’ 같은 것)부터 원문에 없는 줄로 보고 남긴다
    - 아이콘·숫자만 있는 원문 줄은 그 자리를 지우지 않고 keep=True로 표시 (한글 넣기도 그리지 않는다)
    """
    src = [t for t in source.split("\n") if t.strip()]
    lines = list(lay.get("lines") or [])
    if not src or not lines:
        return lay
    src_text = [t for t in src if not is_icon_line(t)]
    if len(src_text) < len(src):
        # 색 그림 아이콘만 있는 줄(예: 큰 칼 두 개)은 글자로 잡히지 않는다: 그 줄은 그대로 두고 글 줄끼리 맞춘다.
        # 아이콘 자리(icon_rows: 아이콘만 있는 줄의 아이콘 세로 범위)를 알면, 그 높이에 잡힌 줄이 없을 때만.
        def on_icon(ln) -> bool:
            y0, y1 = ln["bbox"][1], ln["bbox"][3]
            return any(min(y1, b) - max(y0, a) > 0.5 * max(1, y1 - y0) for a, b in icon_rows or [])

        if icon_rows is not None:
            # 아이콘 자리에 잡힌 줄은 아이콘 조각이다: 지우지 않고 빼 둔 뒤, 남은 줄을 글 줄끼리 맞춘다
            # (그대로 두면 아이콘 조각이 줄 수를 늘려 카드 번호가 제목 자리를 차지하는 식으로 한 칸씩 밀린다)
            on = [ln for ln in lines if on_icon(ln)]
            if on:
                H_, W_ = tm.mask.shape
                for ln in on:
                    x0, y0, x1, y1 = ln["bbox"]
                    pad = int(max(3, 0.35 * ln["glyph_h"]))
                    tm.mask[max(0, y0 - pad):min(H_, y1 + pad), max(0, x0 - pad):min(W_, x1 + pad)] = False
                lines = [ln for ln in lines if not on_icon(ln)]
                if not lines:
                    return {**lay, "lines": []}
            src = src_text
        elif len(lines) == len(src_text):
            src = src_text
    extra = _extra_lines(lines, src)
    kept = [ln for ln in lines if ln not in extra]
    restore = list(extra)
    if len(kept) == len(src):
        for ln, t in zip(kept, src):
            if is_icon_line(t):
                ln["keep"] = True
                restore.append(ln)
    H, W = tm.mask.shape
    if len(kept) == len(src):
        trimmed = 0
        for ln, t in zip(kept, src):
            if not ln.get("keep") and _trim_stray_end(tm, ln, t):
                trimmed += 1
        if trimmed:
            tm.notes.append(f"줄 끝에 떨어진 원문에 없는 조각 {trimmed}개는 지우지 않음")
        # 원문 줄과 맞으면: 지울 줄(아이콘만 있는 줄 빼고) 둘레 밖의 지울 픽셀은 원문에 없는 것(작은 카드 번호 등)이라 남긴다
        allowed = np.zeros_like(tm.mask)
        for ln in kept:
            if ln.get("keep"):
                continue
            x0, y0, x1, y1 = ln["bbox"]
            pad = int(max(4, 0.5 * ln["glyph_h"]))
            allowed[max(0, y0 - pad):min(H, y1 + pad), max(0, x0 - pad):min(W, x1 + pad)] = True
        tm.mask &= allowed
    for ln in restore:
        x0, y0, x1, y1 = ln["bbox"]
        pad = int(max(3, 0.35 * ln["glyph_h"]))
        tm.mask[max(0, y0 - pad):min(H, y1 + pad), max(0, x0 - pad):min(W, x1 + pad)] = False
    out = dict(lay)
    out["lines"] = kept
    if extra:
        tm.notes.append(f"원문에 없는 줄 {len(extra)}개는 지우지 않음")
    return out
