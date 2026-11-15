"""그림 속 아이콘 칸 찾기·자르기·PNG 저장 (app.icons에서 나눔)."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from app import icons as _ic


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
    return [ch for ch in _ic.TOKEN_RE.sub("\u25a0", line) if not ch.isspace() and ch not in SMALL_MARKS]


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
