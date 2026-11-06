"""한글 넣기 전 글꼴 정하기 (8단계 직전).

파일 폴더의 fonts.json:
    {"regions": {영역: 글꼴 파일},              # 영역 기본 글꼴 (사용자가 고른 것)
     "cards": {"쪽": {영역: 글꼴 파일}},        # 카드(쪽)×영역 단위로 따로 고른 글꼴
     "sentences": {"쪽": {영역: {"문장 번호": 글꼴 파일}}},  # ‘문장마다 글꼴’ 영역에서 문장마다 고른 글꼴
     "recommended": {영역: [{"file", "reason", "by"}]},  # 추천 (by: claude | shape), 1번째가 기본값
     "recommended_sig": "...",                 # 추천을 만든 지우기 결과 (바뀌면 다시 추천)
     "updated": "YYYY-MM-DD HH:MM"}
글꼴 파일은 앱 기준 상대 경로(fonts/...) 또는 PC의 절대 경로. 한글 넣기(typeset_compose)가 읽어 쓴다.
화면 코드 없음.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("cardtranslater.fonts")

FILE_NAME = "fonts.json"
MAX_CANDIDATES = 8  # Claude에게 보여 줄 후보 수 (모양 값으로 추린 것)
MAX_RECOMMENDED = 5  # 목록에 보여 줄 추천 수
SAMPLE_PAGES = 3  # 원문 모양을 잴 쪽 수
BASELINE_FONTS = ("fonts/notosanskr/NotoSansKR-Regular.ttf", "fonts/notosanskr/NotoSansKR-Medium.ttf",
                  "fonts/notosanskr/NotoSansKR-Bold.ttf", "fonts/gothica1/GothicA1-Medium.ttf",
                  "fonts/notoserifkr/NotoSerifKR-Regular.ttf", "fonts/notoserifkr/NotoSerifKR-Bold.ttf")
_TAG_RE = re.compile(r"</?[a-z0-9]+>|\[\[[^\]]*\]\]")


# ---------------------------------------------------------------- 저장
def load(file_dir: Path | str) -> dict:
    p = Path(file_dir) / FILE_NAME
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(file_dir: Path | str, regions: dict, cards: dict | None = None, sentences: dict | None = None) -> Path:
    """고른 글꼴을 저장한다 (추천 기록은 그대로 둔다). cards: {쪽: {영역: 파일}} (영역 기본과 같으면 넣지 않는다).
    sentences: {쪽: {영역: {문장 번호: 파일}}} (None이면 예전 것을 그대로 둔다)."""
    p = Path(file_dir) / FILE_NAME
    data = load(file_dir)
    data["regions"] = {k: v for k, v in regions.items() if v}
    clean = {}
    for page, regs in (cards or {}).items():
        regs = {k: v for k, v in regs.items() if v and v != data["regions"].get(k)}
        if regs:
            clean[str(page)] = regs
    data["cards"] = clean
    if sentences is not None:
        sc = {}
        for page, regs in sentences.items():
            regs = {k: {str(i): f for i, f in v.items() if f} for k, v in regs.items()}
            regs = {k: v for k, v in regs.items() if v}
            if regs:
                sc[str(page)] = regs
        data["sentences"] = sc
    data["updated"] = time.strftime("%Y-%m-%d %H:%M")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    log.info("글꼴 저장: %s 영역 %d개, 카드별 %d쪽", p, len(data["regions"]), len(clean))
    return p


def sentences_for_page(data: dict, page: int) -> dict:
    """그 쪽의 {영역: {문장 번호(int): 글꼴 파일}}."""
    regs = (data.get("sentences") or {}).get(str(page)) or {}
    return {k: {int(i): f for i, f in v.items()} for k, v in regs.items()}


def region_font_modes(file_dir: Path) -> dict:
    """{영역: "cell" | "sentence"} — 영역 설정(style.font_mode). 기본은 한 칸 모두 같은 글꼴(cell)."""
    from app.project import load_project

    try:
        regs = load_project(Path(file_dir)).template.regions
    except Exception:  # noqa: BLE001
        return {}
    return {r.name: ((r.style or {}).get("font_mode") or "cell") for r in regs}


def overrides_for_page(data: dict, page: int) -> dict:
    """그 쪽에 쓸 {영역: 글꼴 파일}: 영역 기본 위에 카드별 선택."""
    return {**(data.get("regions") or {}), **((data.get("cards") or {}).get(str(page)) or {})}


# ---------------------------------------------------------------- 이름표
def catalog() -> list[dict]:
    from app.service import font_choices

    return font_choices()


def label_for(file: str) -> str:
    """글꼴 파일의 보이는 이름 (목록 글꼴은 '본고딕' 같은 이름, PC 글꼴은 글꼴 안의 이름)."""
    for c in catalog():
        if c["file"] == file:
            return c["label"]
    from app.font_match import font_info

    info = font_info(file)
    return info["label"] if info else Path(file).stem


def resolve(file: str) -> Path:
    from app.typeset import ROOT

    p = Path(file)
    return p if p.is_absolute() else ROOT / p


def project_used_fonts(file_dir: Path | str) -> list[str]:
    """이 파일이 속한 프로젝트에서 쓴 적이 있는 글꼴 (모든 파일의 fonts.json과 그룹 영역 설정의 글꼴)."""
    from app import workspace

    fd = Path(file_dir)
    try:
        info = workspace.project_of(fd)
        dirs = workspace.file_dirs(info, None)
        root = Path(info.dir)
    except Exception:  # noqa: BLE001 - 프로젝트 밖(시험용)이면 이 파일만
        dirs, root = [fd], None
    seen: list[str] = []

    def add(f):
        if f and f not in seen and resolve(f).exists():
            seen.append(f)

    for d in dirs:
        data = load(d)
        for f in (data.get("regions") or {}).values():
            add(f)
        for regs in (data.get("cards") or {}).values():
            for f in regs.values():
                add(f)
        for regs in (data.get("sentences") or {}).values():
            for v in regs.values():
                for f in v.values():
                    add(f)
    for rj in (root.glob("groups/*/regions.json") if root else []):
        try:
            t = json.loads(rj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sets = [t] + list((t.get("page_regions") or {}).values())
        for s in sets:
            for r in s.get("regions") or []:
                add(((r.get("style") or {}).get("font") or {}).get("file"))
    return seen


# ---------------------------------------------------------------- 영역·번역
def _metas(file_dir: Path) -> list[dict]:
    out = []
    for p in sorted((file_dir / "cleared" / "meta").glob("page-*.json")):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        m["_page"] = int(p.stem.split("-")[-1])
        out.append(m)
    return out


def card_texts(file_dir: Path) -> dict:
    """{쪽: {영역: 한글}}: 검수 엑셀이 있으면 그것, 없으면 초벌 번역."""
    from app import service

    cards = {}
    xlsx = service.review_xlsx_for(file_dir)
    if xlsx:
        try:
            from app.excel_io import import_translation
            from app.project import load_project

            cards = import_translation(load_project(file_dir), xlsx).cards or {}
        except Exception:  # noqa: BLE001 - 엑셀을 못 읽으면 초벌 번역으로
            log.debug("검수 엑셀 읽기 실패", exc_info=True)
    if not cards:
        try:
            cards = service.cards_from_translation(file_dir)
        except Exception:  # noqa: BLE001
            cards = {}
    out = {}
    for page, regs in cards.items():
        row = {}
        for name, e in (regs or {}).items():
            ko = e.get("ko") if isinstance(e, dict) else e
            if isinstance(ko, list):
                ko = " / ".join(str(x) for x in ko)
            if ko:
                row[name] = str(ko)
        out[int(page)] = row
    return out


def plain(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", text or "")).strip()


def _signature(file_dir: Path) -> str:
    p = file_dir / "cleared" / "run.json"
    try:
        return hashlib.sha1(p.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


# ---------------------------------------------------------------- 추천
def recommend(file_dir: Path, client=None, model: str = "claude-sonnet-5-5", work_dir: Path | None = None,
              cancel=None, say=None) -> tuple[dict, str]:
    """영역마다 추천 글꼴 [{"file", "reason", "by"}] (1번째가 원본과 가장 비슷한 것)와 안내 문구.
    client가 있으면 모양 값으로 추린 후보를 Claude에게 보여 고르게 하고, 없거나 실패하면 모양 값 순서."""
    import pymupdf

    from app import font_match
    from app.project import load_project

    say = say or (lambda *_a: None)
    fd = Path(file_dir)
    metas = _metas(fd)
    if not metas:
        return {}, "지운 결과가 없어 추천하지 못했습니다"
    texts = card_texts(fd)
    project = load_project(fd)
    src = pymupdf.open(project.source_pdf)
    cl = pymupdf.open(fd / "cleared" / "cleared.pdf")
    cands = catalog()
    work = Path(work_dir) if work_dir else fd / "work" / "fonts"
    work.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for m in metas:
        for r in m["regions"]:
            if r["name"] not in names:
                names.append(r["name"])
    shape: dict[str, list[dict]] = {}
    ask: list[dict] = []
    try:
        for ni, name in enumerate(names):
            if cancel is not None:
                cancel.check()
            say(ni, len(names), f"원문 글자 모양 재는 중: {name}")
            feats, crop_src, sample = [], None, ""
            for m in metas:
                reg = next((r for r in m["regions"] if r["name"] == name), None)
                if reg is None:
                    continue
                txt = plain(texts.get(m["_page"], {}).get(name, ""))
                if not txt and len(feats) >= 1:
                    continue
                f = font_match.source_features(src[m["_page"] - 1], reg["rect_pt"], cl[m["output_page_index"]])
                if f:
                    feats.append(f)
                    if crop_src is None:
                        crop_src, sample = (m, reg), txt
                if len(feats) >= SAMPLE_PAGES:
                    break
            ranked = font_match.rank_fonts(feats, cands, plain(sample) or font_match.KO_SAMPLE)
            shape[name] = ranked
            if client is not None and crop_src is not None and ranked:
                pool = ranked[:4]  # 모양 값 상위 + 대표 글꼴 (고딕 굵기별·명조): 비교할 기준이 늘 있게
                for must in BASELINE_FONTS:
                    c = next((x for x in ranked if x["file"] == must), None)
                    if c and c not in pool and len(pool) < MAX_CANDIDATES:
                        pool.append(c)
                m, reg = crop_src
                safe = f"r{ni + 1}"
                crop = work / f"{safe}_original.png"
                src[m["_page"] - 1].get_pixmap(dpi=200, clip=pymupdf.Rect(reg["rect_pt"]) & src[m["_page"] - 1].rect,
                                               alpha=False).save(str(crop))
                sheet = font_match.render_sample_sheet(pool, sample or font_match.KO_SAMPLE, work / f"{safe}_samples.png")
                ask.append({"name": name, "crop_png": crop, "sheet_png": sheet, "candidates": pool})
    finally:
        src.close()
        cl.close()

    picked: dict[str, list[dict]] = {}
    note = ""
    if ask:
        say(len(names), len(names) + 1, "Claude가 원문과 견본을 비교하는 중…")
        try:
            picked = font_match.ask_claude_pick(client, model, ask, work)
        except Exception as e:  # noqa: BLE001 - 추천이 안 돼도 글꼴은 고를 수 있다
            log.warning("Claude 글꼴 추천 실패: %s", e)
            note = f"Claude 추천을 받지 못해 글자 모양 값으로만 추천했습니다 ({str(e)[:80]})"
    elif client is None:
        note = "글자 모양 값으로만 추천했습니다 (Claude에 연결되어 있지 않음)"
    styles = {c["file"]: c.get("style", "") for c in cands}
    out = {}
    for name in names:
        rec = order_by_category([{**p, "by": "claude"} for p in picked.get(name, [])], styles)
        for c in shape.get(name, []):
            if len(rec) >= MAX_RECOMMENDED:
                break
            if all(r["file"] != c["file"] for r in rec):
                rec.append({"file": c["file"], "reason": c.get("reason", ""), "by": "shape"})
        if not rec:  # 원문을 잴 수 없었다: 기본 글꼴
            rec = [{"file": "fonts/notosanskr/NotoSansKR-Regular.ttf", "reason": "원문 모양을 재지 못해 기본 글꼴",
                    "by": "default"}]
        out[name] = rec
    return out, note


CATEGORY_WORDS = {"sans": ("고딕", "산세리프", "sans", "gothic", "돋움"), "serif": ("명조", "세리프", "serif", "mincho", "바탕")}


def order_by_category(picks: list[dict], styles: dict) -> list[dict]:
    """Claude가 원문을 고딕이라고 했는데 명조 견본을 고르는 등 계열이 어긋난 추천은 뒤로 보낸다
    (견본 줄 번호를 잘못 본 경우). 원문 판단은 1순위 항목의 original_style에 있다."""
    if not picks:
        return picks
    desc = (picks[0].get("original_style") or "").lower()
    cat = None
    if any(w in desc for w in CATEGORY_WORDS["sans"]) and not any(w in desc for w in ("명조", "mincho")):
        cat = "sans"
    elif any(w in desc for w in CATEGORY_WORDS["serif"]):
        cat = "serif"
    if cat is None:
        return picks
    other = "serif" if cat == "sans" else "sans"
    keep = [p for p in picks if styles.get(p["file"]) != other]
    moved = [p for p in picks if styles.get(p["file"]) == other]
    out = keep + moved
    if out and out[0] is not picks[0]:
        out[0] = {**out[0], "original_style": picks[0].get("original_style")}
    return out


def build(file_dir: Path, client=None, model: str = "claude-sonnet-5-5", refresh: bool = False,
          cancel=None, say=None) -> dict:
    """창에 보여 줄 한 파일의 글꼴 계획.
    {"file_dir", "regions": [{"name", "kind", "font_mode", "recommended": [...], "selected",
                              "cards": [{"page", "text", "selected", "sentences": [{"idx", "text", "selected"}]}]}],
     "used": [파일...], "note"}"""
    fd = Path(file_dir)
    data = load(fd)
    sig = _signature(fd)
    note = ""
    if refresh or not data.get("recommended") or data.get("recommended_sig") != sig:
        rec, note = recommend(fd, client, model, cancel=cancel, say=say)
        data["recommended"], data["recommended_sig"] = rec, sig
        data["recommended_note"] = note
        p = fd / FILE_NAME
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        note = data.get("recommended_note", "")
    texts = card_texts(fd)
    metas = _metas(fd)
    regions = []
    names: list[tuple[str, str]] = []
    for m in metas:
        for r in m["regions"]:
            if r["name"] not in [n for n, _k in names]:
                names.append((r["name"], r.get("kind", "text")))
    from app.typeset_layout import sentences as split_sentences

    modes = region_font_modes(fd)
    raw = texts
    for name, kind in names:
        mode = "sentence" if modes.get(name) == "sentence" and kind == "text" else "cell"
        rec = [{**r, "label": label_for(r["file"])} for r in (data["recommended"].get(name) or [])]
        sel = (data.get("regions") or {}).get(name) or (rec[0]["file"] if rec else None)
        cards = []
        for m in metas:
            if not any(r["name"] == name for r in m["regions"]):
                continue
            pg = m["_page"]
            card = {"page": pg, "text": plain(texts.get(pg, {}).get(name, "")),
                    "selected": ((data.get("cards") or {}).get(str(pg)) or {}).get(name), "sentences": []}
            if mode == "sentence":
                chosen = sentences_for_page(data, pg).get(name, {})
                card["sentences"] = [{"idx": i, "text": t, "selected": chosen.get(i)}
                                     for i, t in enumerate(split_sentences(raw.get(pg, {}).get(name, "")))]
            cards.append(card)
        regions.append({"name": name, "kind": kind, "font_mode": mode, "recommended": rec, "selected": sel,
                        "cards": cards})
    used = [f for f in project_used_fonts(fd)]
    return {"file_dir": str(fd), "regions": regions, "used": [{"file": f, "label": label_for(f)} for f in used],
            "note": note}
