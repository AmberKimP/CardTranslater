"""원문과 어울리는 한글 글꼴 고르기 (PRD 5.7 F28 2단계: 모양 재기).

원문 영역의 글자 마스크와, 후보 글꼴로 그린 한글 견본에서 같은 값을 재어 가까운 순으로 줄 세운다.
- 획 굵기 / 글자 높이 (무게감)
- 획 굵기의 변화 폭 (명조·붓 느낌은 크고, 고딕은 작다)
- 잉크 밀도 (글자 상자 안에서 잉크가 차지하는 비율)
- 글자 폭 / 높이 (좁은 글꼴, 넓은 글꼴)
값은 모두 크기와 무관한 비율이므로 원문(일본어·영어)과 한글 견본을 그대로 비교한다.
일본어 가나·영문과 한글은 모양이 달라 정확한 일치는 아니다: 추천일 뿐이고 결정은 사람이 한다.

PC에 설치된 글꼴 목록(installed_fonts)도 여기서 만든다: 한글이 있고 PDF에 넣어도 되는 글꼴만.
화면 코드 없음.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

import numpy as np

log = logging.getLogger("cardtranslater.fonts")

KO_SAMPLE = "한글 글꼴 견본 가나다라 마바사 아자차카 타파하"
# 특징마다 (이름, 비교 척도, 설명). 척도는 '이만큼 다르면 꽤 다르다'의 크기.
FEATURES = [
    ("stroke", 0.025, "획 굵기"),
    ("contrast", 0.08, "획 굵기 변화"),
    ("density", 0.07, "잉크 밀도"),
]
SERIF_CONTRAST = 0.5  # 원문 획 굵기 변화가 이보다 작으면 명조를 덜 추천한다
SMALL_TEXT_PT = 6.5  # 이보다 작은 원문에는 손글씨·장식 글꼴을 덜 추천한다 (읽기 쉬운 것이 우선)
TARGET_H_PX = 120
ERASE_DIFF_MIN = 48  # 원본과 지운 카드가 이만큼 다르면 글자 픽셀  # 원문·견본 모두 글자 높이가 이 정도가 되게 그려 잰다 (해상도 차이로 값이 흔들리지 않게)


# ---------------------------------------------------------------- 모양 재기
def _line_height(mask: np.ndarray) -> tuple[list[tuple[int, int]], float | None]:
    from app.typeset_ink import _bands, representative_height_px

    bands = _bands(mask.sum(1))
    hs = [representative_height_px(mask[a:b + 1]) for a, b in bands]
    hs = [x for x in hs if x]
    return bands, (float(np.median(hs)) if hs else None)


def shape_features(mask: np.ndarray) -> dict | None:
    """글자 마스크(True=잉크)에서 크기와 무관한 모양 값. 잉크가 너무 적으면 None.

    - stroke: 획 굵기 중앙값 / 글자 높이
    - contrast: 획 굵기의 퍼짐 (표준편차 / 평균, 위아래 5%는 빼고). 명조·붓글씨는 크고 고딕은 작다
    - density: 줄마다 잉크가 있는 세로줄만 센 잉크 비율 (글자 사이·낱말 사이 빈칸은 빼므로
      일본어·영어·한글처럼 글자 간격이 다른 글자 체계끼리도 비교된다)
    """
    import cv2

    mask = mask.astype(bool)
    if mask.sum() < 200:
        return None
    bands, h = _line_height(mask)
    if not h or h < 8:
        return None
    m = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))  # 작은 구멍 메우기
    dist = cv2.distanceTransform(m, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    ridge = (dist >= cv2.dilate(dist, np.ones((3, 3), np.uint8))) & (m > 0) & (dist >= 1.0)
    widths = dist[ridge] * 2.0
    if widths.size < 20:
        return None
    med = float(np.median(widths))
    lo, hi = np.percentile(widths, [5, 95])
    core = widths[(widths >= lo) & (widths <= hi)]
    ink = cols = 0
    for a, b in bands:
        sub = m[a:b + 1].astype(bool)
        if sub.shape[0] < h * 0.5:
            continue
        rows = np.where(sub.any(1))[0]
        on = sub.any(0)
        ink += int(sub.sum())
        cols += int(on.sum()) * int(rows[-1] - rows[0] + 1)
    if not cols:
        return None
    return {"stroke": med / h, "contrast": float(np.std(core) / max(float(np.mean(core)), 1e-6)), "density": ink / cols,
            "height_px": h}


def erased_text_mask(src_page, cleared_page, rect_pt, dpi: int = 600, diff_min: int = 0) -> tuple[np.ndarray, float]:
    """원본과 지운 카드를 같은 영역으로 그려 비교해, 지우기로 사라진 픽셀(= 원문 글자)만 True.
    아이콘·그림·테두리는 지우지 않았으므로 빠진다. (마스크, 1px당 pt)"""
    import pymupdf

    r = pymupdf.Rect(rect_pt) & src_page.rect

    def gray(pg):
        pix = pg.get_pixmap(dpi=dpi, clip=r, colorspace=pymupdf.csGRAY, alpha=False)
        return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w).astype(np.int16)

    diff_min = diff_min or ERASE_DIFF_MIN
    a, b = gray(src_page), gray(cleared_page)
    h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    return np.abs(a[:h, :w] - b[:h, :w]) >= diff_min, 72.0 / dpi


def source_features(src_page, rect_pt, cleared_page=None) -> dict | None:
    """원본 PDF 쪽의 영역에서 원문 글자 모양을 잰다. cleared_page(지운 카드)가 있으면 지워진 글자만 잰다.
    글자 높이가 TARGET_H_PX쯤 되는 해상도로 다시 그려 잰다."""
    from app.typeset_ink import ink_mask

    def mask_at(dpi):
        if cleared_page is not None:
            return erased_text_mask(src_page, cleared_page, rect_pt, dpi)
        mk, _r, sc = ink_mask(src_page, rect_pt, polarity="dark", dpi=dpi)
        return mk, sc

    try:
        mask, s = mask_at(300)
        _b, h = _line_height(mask)
        if not h:
            return None
        dpi = int(min(600, max(150, 300 * TARGET_H_PX / h)))  # 스캔 해상도보다 키워 봐야 흐릴 뿐
        mask, s = mask_at(dpi)
    except Exception:  # noqa: BLE001 - 잴 수 없는 영역은 건너뛴다
        log.debug("원문 마스크 실패", exc_info=True)
        return None
    f = shape_features(mask)
    if f:
        f["height_pt"] = f["height_px"] * s
    return f


@lru_cache(maxsize=256)
def font_features(font_file: str, text: str = KO_SAMPLE, height_px: int = TARGET_H_PX) -> dict | None:
    """글꼴로 견본 한글을 글자 높이 height_px쯤으로 그려 같은 모양 값을 잰다 (원문을 잰 높이와 같게)."""
    from PIL import Image, ImageDraw, ImageFont

    def draw(size_px, scan=False):
        from PIL import ImageFilter

        font = ImageFont.truetype(font_file, size_px)
        left, top, right, bottom = font.getbbox(text)
        im = Image.new("L", (max(right - left + 20, 10), max(bottom - top + 20, 10)), 255)
        ImageDraw.Draw(im).text((10 - left, 10 - top), text, font=font, fill=0)
        if scan:  # 인쇄·스캔처럼 가장자리를 번지게 하고, 원문 마스크와 같은 문턱(바탕보다 48 어두움)으로 자른다
            im = im.filter(ImageFilter.GaussianBlur(max(0.6, size_px / 60)))
            return np.asarray(im) < 255 - ERASE_DIFF_MIN
        return np.asarray(im) < 128

    try:
        _b, h = _line_height(draw(80))
        return shape_features(draw(max(12, int(80 * height_px / h)) if h else 80, scan=True))
    except OSError:
        return None


def _distance(a: dict, b: dict) -> tuple[float, list[tuple[float, str]]]:
    parts = [((a[k] - b[k]) / scale, label) for k, scale, label in FEATURES]
    return float(np.sqrt(sum(d * d for d, _ in parts))), parts


def _reason(parts: list[tuple[float, str]], src: dict, cand: dict) -> str:
    near = [label for d, label in sorted(parts, key=lambda p: abs(p[0])) if abs(d) < 0.6][:2]
    if not near:
        return "가장 덜 다름"
    txt = "·".join(near) + "가 비슷함"
    if cand["stroke"] > src["stroke"] * 1.25:
        txt += " (조금 더 굵음)"
    elif cand["stroke"] < src["stroke"] * 0.8:
        txt += " (조금 더 가늚)"
    return txt


def rank_fonts(sources: list[dict], candidates: list[dict], sample: str = KO_SAMPLE) -> list[dict]:
    """원문 모양 값(여러 쪽)과 후보 [{"file", "label", "style"}]로 가까운 순 목록.
    돌려주는 항목: 후보 dict + {"score"(작을수록 비슷), "reason"}. 원문을 못 쟀으면 후보 순서 그대로(score None)."""
    from app.typeset import ROOT

    srcs = [s for s in sources if s]
    if not srcs:
        return [{**c, "score": None, "reason": ""} for c in candidates]
    src = {k: float(np.median([s[k] for s in srcs])) for k, _sc, _l in FEATURES}
    hpx = int(round(float(np.median([s["height_px"] for s in srcs])) / 4) * 4)  # 같은 높이로 그려 비교 (4px 단위로 캐시)
    small = float(np.median([s.get("height_pt", 99) for s in srcs])) < SMALL_TEXT_PT
    out = []
    for c in candidates:
        p = Path(c["file"])
        f = font_features(str(p if p.is_absolute() else ROOT / p), sample, max(hpx, 16))
        if not f:
            continue
        score, parts = _distance(src, f)
        if small and c.get("style") in ("handwriting", "display"):
            score += 1.0
        if c.get("style") == "serif" and src["contrast"] < SERIF_CONTRAST:
            score += 1.0  # 스캔 번짐으로 고딕도 획 굵기 변화가 커 보인다: 아주 크지 않으면 명조를 앞에 두지 않는다
        out.append({**c, "score": round(score, 3), "reason": _reason(parts, src, f)})
    out.sort(key=lambda x: x["score"])
    return out


# ---------------------------------------------------------------- PC에 설치된 글꼴
def _font_dirs() -> list[Path]:
    dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    return [d for d in dirs if d.is_dir()]


def font_info(path: Path | str) -> dict | None:
    """글꼴 파일 하나: {"file", "family", "style", "hangul", "embeddable"}. 읽을 수 없으면 None.
    .ttc(글꼴 모음)는 지금 한글 넣기에서 쓸 수 없어 None."""
    from fontTools.ttLib import TTFont

    p = Path(path)
    if p.suffix.lower() not in (".ttf", ".otf"):
        return None
    try:
        f = TTFont(str(p), lazy=True, fontNumber=0)
        name = f["name"]
        fam = name.getBestFamilyName() or p.stem
        sub = name.getBestSubFamilyName() or ""
        cmap = f.getBestCmap() or {}
        fs_type = getattr(f["OS/2"], "fsType", 0) if "OS/2" in f else 0
        f.close()
    except Exception:  # noqa: BLE001 - 깨진 글꼴 파일은 건너뛴다
        return None
    hangul = all(cp in cmap for cp in (0xAC00, 0xD55C, 0xAE00))  # 가 한 글
    restricted = bool(fs_type & 0x0002) and not (fs_type & 0x000C)  # 내장 금지 (미리보기·편집 허용이 아니면)
    label = fam + (f" {sub}" if sub and sub.lower() not in ("regular", "normal") else "")
    return {"file": str(p), "family": fam, "style": sub, "label": label, "hangul": hangul,
            "embeddable": not restricted}


def installed_fonts(hangul_only: bool = True) -> list[dict]:
    """PC에 설치된 글꼴 중 한글이 있고 PDF에 넣어도 되는 것 (이름순)."""
    out, seen = [], set()
    for d in _font_dirs():
        for p in sorted(d.iterdir()):
            info = font_info(p)
            if not info or (hangul_only and not info["hangul"]) or not info["embeddable"]:
                continue
            key = info["label"].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(info)
    out.sort(key=lambda x: x["label"].lower())
    return out


# ---------------------------------------------------------------- Claude가 보고 고르기 (F28 3단계)
PICK_SYSTEM = """You help choose Korean fonts for translating board game cards.
For each region you get: a crop of the ORIGINAL card text (Japanese or English) and a sheet of numbered
Korean samples, one candidate font per row. Rank the candidates by how close their overall impression is to the
original lettering: sans vs serif, stroke weight, roundness, handwritten or decorative feel, genre mood.
Readability matters: do not prefer a thin or highly decorative font for small body text.
Return the best 3 candidate numbers per region, best first, each with a short reason in Korean (under 30 chars)."""


def _pick_schema() -> dict:
    return {"type": "object", "properties": {"regions": {"type": "array", "items": {
        "type": "object",
        "properties": {"region": {"type": "string"}, "ranked": {"type": "array", "items": {
            "type": "object", "properties": {"id": {"type": "integer"}, "reason": {"type": "string"}},
            "required": ["id", "reason"], "additionalProperties": False}}},
        "required": ["region", "ranked"], "additionalProperties": False}}},
        "required": ["regions"], "additionalProperties": False}


def render_sample_sheet(candidates: list[dict], text: str, out_png: Path, height_px: int = 44,
                        width_px: int = 1100) -> Path:
    """후보 글꼴마다 한 줄씩 '번호. 견본 문장'을 그린 PNG (번호는 본고딕)."""
    from PIL import Image, ImageDraw, ImageFont

    from app.typeset import ROOT

    label_font = ImageFont.truetype(str(ROOT / "fonts" / "notosanskr" / "NotoSansKR-Regular.ttf"), 26)
    row_h = int(height_px * 1.9)
    im = Image.new("RGB", (width_px, row_h * len(candidates) + 16), "white")
    d = ImageDraw.Draw(im)
    for i, c in enumerate(candidates, start=1):
        y = 8 + (i - 1) * row_h
        d.text((12, y + row_h * 0.25), f"{i}.", font=label_font, fill=(200, 30, 30))
        p = Path(c["file"])
        try:
            f = ImageFont.truetype(str(p if p.is_absolute() else ROOT / p), height_px)
        except OSError:
            continue
        d.text((70, y + row_h * 0.12), text[:40], font=f, fill="black")
        d.line([(0, y + row_h - 1), (width_px, y + row_h - 1)], fill=(220, 220, 220))
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_png)
    return out_png


def ask_claude_pick(client, model: str, regions: list[dict], work_dir: Path) -> dict[str, list[dict]]:
    """regions: [{"name", "crop_png", "sheet_png", "candidates": [...]}] → {영역: [{"file", "reason"}, ...]}.
    한 파일의 모든 영역을 한 번에 묻는다."""
    from app.llm import complete_json

    images, lines = [], []
    for r in regions:
        images += [Path(r["crop_png"]), Path(r["sheet_png"])]
        lines.append(json.dumps({"region": r["name"], "original": Path(r["crop_png"]).name,
                                 "samples": Path(r["sheet_png"]).name, "candidates": len(r["candidates"])},
                                ensure_ascii=False))
    prompt = "Regions (JSON lines; open both image files of each):\n" + "\n".join(lines)
    data, _api, _eq = complete_json(client, model, PICK_SYSTEM, prompt, _pick_schema(), work_dir, images=images)
    by_name = {r["name"]: r for r in regions}
    out: dict[str, list[dict]] = {}
    for item in data.get("regions", []):
        r = by_name.get(item.get("region"))
        if not r:
            continue
        picks = []
        for k in item.get("ranked", []):
            i = int(k.get("id", 0)) - 1
            if 0 <= i < len(r["candidates"]) and all(p["file"] != r["candidates"][i]["file"] for p in picks):
                picks.append({"file": r["candidates"][i]["file"], "reason": str(k.get("reason", ""))[:60]})
        if picks:
            out[r["name"]] = picks
    log.info("Claude 글꼴 추천: %s", {k: [Path(p["file"]).name for p in v] for k, v in out.items()})
    return out
