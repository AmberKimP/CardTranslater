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
SAME_SHAPE = 0.80  # 알파 모양 상관계수가 이 이상이면 같은 아이콘
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


def _bands(ink: np.ndarray) -> list[tuple[int, int]]:
    """잉크가 있는 가로 줄 띠 (y0, y1)."""
    rows = ink.sum(axis=1) > max(2, ink.shape[1] * 0.004)
    bands, start = [], None
    for y, v in enumerate(list(rows) + [False]):
        if v and start is None:
            start = y
        elif not v and start is not None:
            bands.append([start, y])
            start = None
    if not bands:
        return []
    hmax = max(b[1] - b[0] for b in bands)
    merged = []
    for b in bands:  # 가까운 띠(글자 윗부분 점, 탁점)는 합친다
        if merged and b[0] - merged[-1][1] < hmax * 0.25:
            merged[-1][1] = b[1]
        else:
            merged.append(b)
    hmax = max(b[1] - b[0] for b in merged)
    return [(a, b) for a, b in merged if b - a >= hmax * 0.4]


def _units(ch: str) -> float:
    return 0.5 if unicodedata.east_asian_width(ch) in ("Na", "H") and not ch.isspace() else (
        0.3 if ch.isspace() else 1.0)


def locate(img: np.ndarray, text: str, index: int) -> tuple[tuple[int, int, int, int] | None, str]:
    """text 안 index번째 아이콘 토큰([[..]])의 위치 (x0, y0, x1, y1, 이미지 px)와 실패 이유."""
    import cv2

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
    bands = _bands(ink)
    if len(bands) != len(lines):
        return None, f"줄 수가 다름 (원문 {len(lines)}줄, 이미지 {len(bands)}줄)"
    y0, y1 = bands[line_no]
    band_h = y1 - y0
    n, lab, st, _c = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    comps = []
    for c in range(1, n):
        x, y, w, h, a = st[c]
        cy = y + h / 2
        if y0 - band_h * 0.2 <= cy <= y1 + band_h * 0.2 and a >= 4:
            comps.append((c, x, y, w, h, a))
    big = [c for c in comps if c[4] >= band_h * 0.2]
    if not big:
        return None, "그 줄에 잉크가 없음"
    lx0 = min(c[1] for c in big)
    lx1 = max(c[1] + c[3] for c in big)

    def strip(s):
        return TOKEN_RE.sub("■", s)  # 다른 아이콘도 한 칸

    u_before = sum(_units(ch) for ch in strip(before))
    u_total = u_before + 1.1 + sum(_units(ch) for ch in strip(after))
    unit = (lx1 - lx0) / max(u_total, 1e-6)
    expect = lx0 + (u_before + 0.55) * unit
    best, best_s = None, 0.0
    for c in big:
        _ci, x, y, w, h, a = c
        if h < band_h * 0.45:
            continue
        d = abs(x + w / 2 - expect)
        if d > unit * 1.6:
            continue
        s = a * float(np.exp(-d / unit))  # 가깝고 속이 찬(넓은) 덩어리일수록
        if s > best_s:
            best, best_s = c, s
    if best is None:
        return None, "예상 자리 근처에 아이콘 크기의 덩어리가 없음"
    _ci, x, y, w, h, _a = best
    gx, gy = w * 0.15, h * 0.15
    bx0, by0, bx1, by1 = x, y, x + w, y + h
    for c in comps:  # 아이콘 안쪽 무늬(동전 안 숫자 등)는 함께
        _cj, cx, cy, cw, ch_, _ca = c
        if cx >= x - gx and cy >= y - gy and cx + cw <= x + w + gx and cy + ch_ <= y + h + gy:
            bx0, by0, bx1, by1 = min(bx0, cx), min(by0, cy), max(bx1, cx + cw), max(by1, cy + ch_)
    if not (unit * 0.35 <= bx1 - bx0 <= unit * 2.2):
        return None, "찾은 덩어리 크기가 아이콘 같지 않음"
    return (int(bx0), int(by0), int(bx1), int(by1)), ""


def cut_out(img: np.ndarray, box: tuple[int, int, int, int], pad: int = 4) -> np.ndarray:
    """box를 잘라 배경을 투명하게 한 RGBA."""
    import cv2

    h, w = img.shape[:2]
    x0, y0, x1, y1 = max(0, box[0] - pad), max(0, box[1] - pad), min(w, box[2] + pad), min(h, box[3] + pad)
    em = max(box[2] - box[0], box[3] - box[1])
    bg_px = int(max(31, em * 2.2)) | 1
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (bg_px, bg_px))
    bg = np.stack([cv2.GaussianBlur(cv2.dilate(img[:, :, i].astype(np.float32), k), (0, 0), bg_px / 6)
                   for i in range(3)], axis=2)
    dist = np.abs(img.astype(np.float32) - bg).max(axis=2)
    alpha = np.clip((dist - 25.0) / 45.0, 0, 1)
    # 옆 글자가 끼지 않도록 아이콘 덩어리 둘레만 남긴다
    keep = np.zeros(alpha.shape, np.uint8)
    keep[box[1]:box[3], box[0]:box[2]] = 1
    alpha *= keep
    rgba = np.dstack([img, (alpha * 255).astype(np.uint8)])[y0:y1, x0:x1]
    return rgba


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
    s = max(h, w)
    m0 = max(1, s // 8)  # 둘레에 빈칸을 둬서 꽉 찬 네모 아이콘도 모양 차이가 생기게
    sq = np.zeros((s + 2 * m0, s + 2 * m0), np.float32)
    sq[m0 + (s - h) // 2:m0 + (s - h) // 2 + h, m0 + (s - w) // 2:m0 + (s - w) // 2 + w] = a
    small = cv2.resize(sq, (n, n), interpolation=cv2.INTER_AREA)
    m = a > 0.5
    color = rgb[m].mean(axis=0) if m.any() else np.zeros(3)
    return Shape(small, w / max(h, 1), color.astype(np.float32))


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
    """text 안 i번째 ``[[?]]``를 mapping[i]로 ('' = 빼기, 이름 = [[이름]]). 없는 번호는 그대로."""
    i = -1

    def sub(m):
        nonlocal i
        i += 1
        if i not in mapping:
            return m.group(0)
        name = mapping[i]
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


def pending_count(info) -> int:
    """프로젝트(뺀 쪽 제외) 원문에 남은 ``[[?]]`` 수."""
    from app.extract import load_extracted
    from app.workspace import file_dirs, included_pages

    n = 0
    for fd in file_dirs(info):
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
    from app.workspace import file_dirs, file_key, included_pages

    work = Path(work)
    if work.exists():
        shutil.rmtree(work)
    (work / "crops").mkdir(parents=True)
    d = load_dictionary(info)
    known = {}
    for name, e in d["icons"].items():
        p = icons_dir(info) / e.get("file", f"{name}.png")
        if p.exists():
            known[name] = shape_of(load_png(p))
    res = ScanResult()
    n = 0
    for fd in file_dirs(info):
        inc = set(included_pages(fd))
        ext = load_extracted(fd)
        todo = [(p, r, v["source"]) for p, regs in sorted(ext.items()) if p in inc
                for r, v in regs.items() if isinstance(v.get("source"), str) and PLACEHOLDER in v["source"]]
        if not todo:
            continue
        proj = load_project(fd)
        regions = {r.name: r for r in proj.template.regions}
        src = next((fd / "source").glob("*.pdf"))
        doc = pymupdf.open(src)
        try:
            for pno, rname, text in todo:
                img = region_image(doc[pno - 1], regions[rname])
                tokens = list(TOKEN_RE.finditer(text))
                ph = [i for i, t in enumerate(tokens) if t.group(1) == "?"]
                auto_map, left = {}, []
                for k, ti in enumerate(ph):
                    n += 1
                    oc = Occurrence(f"o{n:03d}", str(fd.relative_to(info.dir)), file_key(fd), pno, rname, k,
                                    text.replace("\n", " / "))
                    oc.line, oc.order, _nm = tokens_in(text)[ti]
                    box, why = locate(img, text, ti)
                    if box is None:
                        oc.reason = why
                    else:
                        rgba = cut_out(img, box)
                        png = work / "crops" / f"{oc.id}.png"
                        save_png(rgba, png)
                        oc.png = str(png)
                        sh = shape_of(rgba)
                        if known:
                            name, s = max(((nm, similarity(sh, ks)) for nm, ks in known.items()), key=lambda t: t[1])
                            if s >= SAME_SHAPE:
                                oc.match, oc.score = name, s
                    if oc.match:
                        auto_map[k] = oc.match
                        res.auto[oc.match] = res.auto.get(oc.match, 0) + 1
                    else:
                        left.append(oc)
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
    shapes = {o.id: shape_of(load_png(Path(o.png))) for o in res.occurrences if o.png}
    reps: list[tuple[str, str]] = []  # (묶음, 대표 occurrence id)
    for o in res.occurrences:
        if not o.png:
            o.group = f"위치 못 찾음 {o.id}"
            res.groups[o.group] = {"rep": "", "count": 1}
            continue
        g = next((g for g, rid in reps if similarity(shapes[o.id], shapes[rid]) >= SAME_SHAPE), None)
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

    res = load_scan(work)
    ws = load_workbook(xlsx)["아이콘"]
    head = [c.value for c in ws[1]]
    rows = [dict(zip(head, r)) for r in ws.iter_rows(min_row=2, values_only=True) if any(r)]
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
            decision[g] = ""
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
            best = max(reps, key=_area)
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
        apply_to_extract(info.dir / f, page, region, mapping)
        out.removed += sum(1 for v in mapping.values() if v == "")
        out.replaced += sum(1 for v in mapping.values() if v)
    shutil.rmtree(work, ignore_errors=True)
    return out
