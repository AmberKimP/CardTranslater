"""한글 넣기 전 글꼴 정하기 (8단계 직전).

파일 폴더의 fonts.json:
    {"regions": {영역: 글꼴 파일},              # 영역 기본 글꼴 (사용자가 고른 것)
     "cards": {"쪽": {영역: 글꼴 파일}},        # 카드(쪽)×영역 단위로 따로 고른 글꼴
     "sentences": {"쪽": {영역: {"문장 번호": 글꼴 파일}}},  # ‘문장마다 글꼴’ 영역에서 문장마다 고른 글꼴
     "sizes": {영역: 배율},                     # 글자 크기 배율 (1.0 = 원문 크기, 없으면 1.0)
     "aligns": {영역: "left"|"center"|"right"}, # 정렬 (없으면 영역 설정대로)
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
ALIGNS = ("left", "center", "right")
_TAG_RE = re.compile(r"</?[a-z0-9]+>|\[\[[^\]]*\]\]")


# ---------------------------------------------------------------- 저장
def load(file_dir: Path | str) -> dict:
    p = Path(file_dir) / FILE_NAME
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(file_dir: Path | str, regions: dict, cards: dict | None = None, sentences: dict | None = None,
         sizes: dict | None = None, aligns: dict | None = None, slots: dict | None = None) -> Path:
    """sizes: {영역: 크기 배율} (None이면 예전 것을 그대로, 1.0은 넣지 않는다).
    aligns: {영역: left | center | right} (None이면 예전 것을 그대로, 영역 설정대로인 영역은 넣지 않는다).
    고른 글꼴을 저장한다 (추천 기록은 그대로 둔다). cards: {쪽: {영역: 파일}} (영역 기본과 같으면 넣지 않는다).
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
    if sizes is not None:
        data["sizes"] = {k: round(float(v), 3) for k, v in sizes.items() if v and abs(float(v) - 1.0) > 1e-6}
    if aligns is not None:
        data["aligns"] = {k: v for k, v in aligns.items() if v in ALIGNS}
    if slots is not None:  # ‘문장마다 글꼴’ 영역의 문장 자리별 글꼴 (모든 카드)
        clean_slots = {}
        for name, sl in slots.items():
            c = {k: sl.get(k) for k in ("first", "last") if sl.get(k)}
            mid = {str(int(j)): f for j, f in (sl.get("mid") or {}).items() if f}
            if mid:
                c["mid"] = mid
            if c:
                clean_slots[name] = c
        data["sentence_slots"] = clean_slots
    data["updated"] = time.strftime("%Y-%m-%d %H:%M")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    log.info("글꼴 저장: %s 영역 %d개, 카드별 %d쪽", p, len(data["regions"]), len(clean))
    return p


def set_card_font(file_dir: Path | str, page: int, region: str, font_file: str | None) -> None:
    """이 카드의 이 영역만 쓸 글꼴 (None·빈 값이면 영역 글꼴을 따름). 다른 선택은 그대로 둔다."""
    p = Path(file_dir) / FILE_NAME
    data = load(file_dir)
    cards = data.setdefault("cards", {})
    card = cards.setdefault(str(int(page)), {})
    if font_file and font_file != (data.get("regions") or {}).get(region):
        card[region] = font_file
    else:
        card.pop(region, None)
    if not card:
        cards.pop(str(int(page)), None)
    data["updated"] = time.strftime("%Y-%m-%d %H:%M")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def slot_fonts(slots: dict | None, n: int) -> dict[int, str]:
    """문장 자리별 글꼴을 문장이 n개인 카드에 펼친다 {문장 번호: 글꼴}.
    첫 문장·마지막 문장이 먼저 (문장이 하나면 첫 문장 글꼴). 가운데 j번째(1부터 = 둘째 문장)는 그 카드에 그 문장이
    있을 때만 (마지막 문장 자리는 빼고). 정하지 않은 자리는 영역 글꼴."""
    slots = slots or {}
    out: dict[int, str] = {}
    mids = slots.get("mid") or {}
    for j in range(1, n - 1):
        f = mids.get(str(j))
        if f:
            out[j] = f
    if n > 1 and slots.get("last"):
        out[n - 1] = slots["last"]
    if n >= 1 and slots.get("first"):
        out[0] = slots["first"]
    return out


def _ko_text(entry) -> str | None:
    ko = entry.get("ko") if isinstance(entry, dict) else entry
    return ko if isinstance(ko, str) else None


def sentences_for_page(data: dict, page: int, card: dict | None = None) -> dict:
    """그 쪽의 {영역: {문장 번호(int): 글꼴 파일}}. card(그 쪽 번역)를 주면 문장 자리별 글꼴(sentence_slots)을
    그 카드의 문장 수에 맞게 펼치고, 카드·문장마다 고른 것을 그 위에 덮는다."""
    from app.typeset_layout import sentences as split_sentences

    regs = (data.get("sentences") or {}).get(str(page)) or {}
    out = {k: {int(i): f for i, f in v.items()} for k, v in regs.items()}
    if card:
        for name, sl in (data.get("sentence_slots") or {}).items():
            text = _ko_text(card.get(name))
            if not text:
                continue
            m = slot_fonts(sl, len(split_sentences(text)))
            m.update(out.get(name) or {})
            if m:
                out[name] = m
    return out


def region_font_modes(file_dir: Path) -> dict:
    """{영역: "cell" | "sentence"} — 영역 설정(style.font_mode). 기본은 한 칸 모두 같은 글꼴(cell)."""
    from app.project import load_project

    try:
        regs = load_project(Path(file_dir)).template.regions
    except Exception:  # noqa: BLE001
        return {}
    return {r.name: ((r.style or {}).get("font_mode") or "cell") for r in regs}


def size_scales(data: dict) -> dict:
    """{영역: 글자 크기 배율} (정하지 않은 영역은 빠짐 = 1.0)."""
    return {k: float(v) for k, v in (data.get("sizes") or {}).items() if v}


def align_choices(data: dict) -> dict:
    """{영역: left | center | right} — 글꼴 창에서 정한 정렬 (정하지 않은 영역은 빠짐 = 영역 설정대로)."""
    return {k: v for k, v in (data.get("aligns") or {}).items() if v in ALIGNS}


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


_raw_cache: dict = {}


def _raw_cards(file_dir: Path) -> dict:
    """{쪽: {영역: 칸 dict(ko·source·fixed_size_pt …)}}: 한글 넣기가 쓰는 그대로. 검수 엑셀이 있으면 그것,
    없으면 초벌 번역. 엑셀·지우기 결과가 바뀌지 않았으면 다시 읽지 않는다."""
    from app import service

    fd = Path(file_dir)
    xlsx = service.review_xlsx_for(fd)
    fj = fd / "file.json"
    stamp = (str(fd), _signature(fd), str(xlsx), xlsx.stat().st_mtime if xlsx and xlsx.exists() else 0,
             fj.stat().st_mtime if fj.exists() else 0)
    if _raw_cache.get("key") == stamp:
        return _raw_cache["cards"]
    cards = {}
    if xlsx:
        try:
            from app.excel_io import import_translation
            from app.project import load_project

            cards = import_translation(load_project(fd), xlsx).cards or {}
        except Exception:  # noqa: BLE001 - 엑셀을 못 읽으면 초벌 번역으로
            log.debug("검수 엑셀 읽기 실패", exc_info=True)
    if not cards:
        try:
            cards = service.cards_from_translation(fd)
        except Exception:  # noqa: BLE001
            cards = {}
    cards = {int(k): v for k, v in cards.items()}
    from app import card_edits

    cards, _notes = card_edits.apply(fd, cards)  # 결과 보기에서 고친 번역
    _raw_cache.update(key=stamp, cards=cards)
    return cards


def card_texts(file_dir: Path) -> dict:
    """{쪽: {영역: 한글}}: 검수 엑셀이 있으면 그것, 없으면 초벌 번역 (결과 보기에서 고친 번역 포함)."""
    cards = _raw_cards(Path(file_dir))
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
    {"file_dir", "regions": [{"name", "kind", "font_mode", "recommended": [...], "selected", "size_scale",
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
        most = max(cards, key=lambda c: len(c["sentences"]), default=None)
        regions.append({"name": name, "kind": kind, "font_mode": mode, "recommended": rec, "selected": sel,
                        "size_scale": float((data.get("sizes") or {}).get(name) or 1.0),
                        "align": align_choices(data).get(name, ""), "cards": cards,
                        "slots": (data.get("sentence_slots") or {}).get(name) or {},
                        "max_sentences": len(most["sentences"]) if most else 0,
                        "slot_page": most["page"] if most and most["sentences"] else None})
    used = [f for f in project_used_fonts(fd)]
    return {"file_dir": str(fd), "regions": regions, "used": [{"file": f, "label": label_for(f)} for f in used],
            "note": note}


# ---------------------------------------------------------------- 글꼴 창 미리 보기
PREVIEW_W_PX = 460  # 미리 보기 한쪽 칸 너비 (원본과 미리 보기가 같은 배율)
PREVIEW_H_PX = 220
_preview_cache: dict = {}


def _region_meta(fd: Path, page: int, region: str) -> dict | None:
    p = fd / "cleared" / "meta" / f"page-{int(page):03d}.json"
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    reg = next((r for r in m.get("regions", []) if r["name"] == region), None)
    return None if reg is None else {**reg, "_out_idx": m.get("output_page_index", int(page) - 1)}


def _main_color(lines: list[dict]) -> str | None:
    """줄 모양에서 가장 많이 쓰인 글자 색 (글자 수 대신 줄 너비로 센다)."""
    w: dict[str, float] = {}
    for ln in lines:
        c = ln.get("color")
        if c:
            b = ln.get("bbox_pt") or [0, 0, 1, 0]
            w[c] = w.get(c, 0.0) + max(b[2] - b[0], 0.1)
    return max(w, key=w.get) if w else None


def preview_source(file_dir: Path | str, page: int, region: str) -> dict | None:
    """한 카드·영역의 미리 보기 재료 (같은 배율): {"original_png", "cleared_png" (bytes), "zoom" (px/pt),
    "w_pt", "h_pt", "text_box_pt" (글 칸, 영역 왼쪽 위 기준 pt), "color", "align", "glyph_h_pt", "size_pt", "kind"}. 한글 넣기와 같은 규칙으로 색을 정한다."""
    import numpy as np
    import pymupdf

    from app.project import load_project
    from app.typeset_ink import measure_source_ink

    fd = Path(file_dir)
    key = (str(fd), int(page), region, _signature(fd))
    if key in _preview_cache:
        return _preview_cache[key]
    reg = _region_meta(fd, page, region)
    if reg is None:
        return None
    project = load_project(fd)
    tmpl = next((r for r in project.template.regions if r.name == region), None)
    style_d = (tmpl.style if tmpl is not None else None) or reg.get("style_defaults") or {}
    lines = [ln for ln in (reg.get("text_layout") or {}).get("lines") or [] if ln.get("glyph_h_pt", 0) >= 2.5]
    tl = reg.get("text_layer") or {}
    rot = int(style_d.get("rotate", 0) or 0) % 360
    rect = pymupdf.Rect(reg["rect_pt"])
    src = pymupdf.open(project.source_pdf)
    cl = pymupdf.open(fd / "cleared" / "cleared.pdf")
    try:
        sp = src[int(page) - 1]
        rect &= sp.rect
        if rect.is_empty:
            return None
        zoom = min(PREVIEW_W_PX / rect.width, PREVIEW_H_PX / rect.height, 8.0)
        mat = pymupdf.Matrix(zoom, zoom)
        if rot == 180:  # 거꾸로 인쇄된 영역은 바로 세워 보여 준다
            mat = mat.prerotate(180)
        orig = sp.get_pixmap(matrix=mat, clip=rect, alpha=False).tobytes("png")
        cpg = cl[min(reg["_out_idx"], cl.page_count - 1)]
        clear = cpg.get_pixmap(matrix=mat, clip=rect, alpha=False).tobytes("png")
        glyph = None
        if lines:
            glyph = float(np.median([ln["glyph_h_pt"] for ln in lines]))
        elif reg.get("kind") == "tag_pills":
            for lab in reg.get("tag_labels") or []:
                lr = pymupdf.Rect(lab["bbox_pt"])
                inset = lab.get("radius_pt") or lr.height / 2
                ink = measure_source_ink(sp, (lr.x0 + inset, lr.y0 + 0.5, lr.x1 - inset, lr.y1 - 0.5),
                                         polarity="light")
                if ink.glyph_height:
                    glyph = ink.glyph_height
                    break
        else:
            work = pymupdf.Rect(reg["work_rect_pt"]) if reg.get("work_rect_pt") else rect
            ink = measure_source_ink(sp, (rect | work) & sp.rect, polarity="dark", rotate=rot)
            glyph = ink.glyph_height
    except Exception:  # noqa: BLE001 - 미리 보기를 못 만들어도 고르기는 된다
        log.debug("미리 보기 재료 실패", exc_info=True)
        return None
    finally:
        src.close()
        cl.close()
    if "color" in style_d or not lines:
        color = style_d.get("color", "#000000")
    else:
        color = _main_color(lines) or "#000000"
    align = style_d.get("align") or ""
    if align in ("", "original"):
        aligns = [ln.get("align", "left") for ln in lines]
        align = max(set(aligns), key=aligns.count) if aligns else "left"
    if reg.get("kind") == "tag_pills":
        align = "left"
    # 글을 넣을 칸 (영역 안 pt, 한글 넣기와 같이): 원문 줄이 있으면 그 줄들의 좌우, 없으면 작업 영역 오른쪽까지
    if lines:
        gh = glyph or 4.0
        bx = [min(ln["bbox_pt"][0] for ln in lines), min(ln["bbox_pt"][1] for ln in lines) - 0.3 * gh,
              min(rect.x1, max(ln["bbox_pt"][2] for ln in lines) + 0.5 * gh), rect.y1]
    else:
        x1 = min(rect.x1, reg["work_rect_pt"][2]) if reg.get("work_rect_pt") else rect.x1
        bx = [rect.x0, rect.y0, x1, rect.y1]
    if rot == 180:
        sx, sy = rect.x0 + rect.x1, rect.y0 + rect.y1
        bx = [sx - bx[2], sy - bx[3], sx - bx[0], sy - bx[1]]
    box = [max(bx[0], rect.x0) - rect.x0, max(bx[1], rect.y0) - rect.y0,
           min(bx[2], rect.x1) - rect.x0, min(bx[3], rect.y1) - rect.y0]
    out = {"original_png": orig, "cleared_png": clear, "zoom": zoom, "w_pt": rect.width, "h_pt": rect.height,
           "text_box_pt": box,
           "color": color, "align": align, "glyph_h_pt": glyph, "size_pt": tl.get("size_pt"),
           "kind": reg.get("kind", "text")}
    _preview_cache[key] = out
    return out


def preview_render(file_dir: Path | str, page: int, region: str, font_file: str | None, scale: float = 1.0,
                   align: str | None = None, sentence: int | None = None, text=None, slots: dict | None = None,
                   base_font: str | None = None) -> bytes | None:
    """글꼴 창 미리 보기를 한글 넣기와 똑같이 그린 PNG (지운 카드 사본에 그 칸만 실제로 넣어 본다).
    원본 미리 보기(preview_source)와 같은 자리·배율. sentence가 있으면 그 문장에만 그 글꼴 (‘문장마다 글꼴’).
    text: 저장하지 않은 고친 번역으로 그려 본다. 번역이 없거나 그리지 못하면 None (화면은 예전처럼 간단히 그린다)."""
    import pymupdf

    from app.project import load_project
    from app.typeset import finalize
    from app.typeset_compose import ROOT as T_ROOT, compose_page, icon_style_for

    fd = Path(file_dir)
    src = preview_source(fd, page, region)
    entry = (_raw_cards(fd).get(int(page)) or {}).get(region)
    if text is not None:  # 결과 보기에서 고치는 중인 번역
        entry = {**(entry or {}), "ko": text}
    if src is None or not entry or not entry.get("ko"):
        return None
    try:
        m = json.loads((fd / "cleared" / "meta" / f"page-{int(page):03d}.json").read_text(encoding="utf-8"))
        reg = next(r for r in m["regions"] if r["name"] == region)
        project = load_project(fd)
        tmpl = next((r for r in project.template.regions if r.name == region), None)
        style = dict((tmpl.style if tmpl is not None else None) or {})
        icon_style = icon_style_for(fd)
        if icon_style:
            style = {**icon_style, **style}
        # 문장 자리별 글꼴(slots)은 이 카드의 문장 수에 맞게 펼치고, 고른 문장(sentence)은 그 위에
        from app.typeset_layout import sentences as split_sentences

        smap = slot_fonts(slots, len(split_sentences(_ko_text(entry) or ""))) if slots else {}
        if sentence is not None:
            if font_file:
                smap[int(sentence)] = font_file
            fonts = {region: base_font} if base_font else None
        else:
            fonts = {region: font_file} if font_file else None
        sent = {region: smap} if smap else None
        al = {region: align} if align in ("left", "center", "right") else None
        with pymupdf.open(project.source_pdf) as sdoc, pymupdf.open(fd / "cleared" / "cleared.pdf") as cdoc:
            doc = pymupdf.open()
            idx = min(m.get("output_page_index", int(page) - 1), cdoc.page_count - 1)
            doc.insert_pdf(cdoc, from_page=idx, to_page=idx)
            compose_page(doc[0], sdoc[int(page) - 1], {**m, "regions": [reg]}, {region: entry}, T_ROOT,
                         {region: style}, {region: tmpl.erase if tmpl is not None else None}, fonts, sent,
                         {region: float(scale or 1.0)}, al)
            finalize(doc)
            data = doc.tobytes()
            doc.close()
        with pymupdf.open(stream=data, filetype="pdf") as out:
            rect = pymupdf.Rect(reg["rect_pt"]) & out[0].rect
            mat = pymupdf.Matrix(src["zoom"], src["zoom"])
            if int(style.get("rotate", 0) or 0) % 360 == 180:
                mat = mat.prerotate(180)
            return out[0].get_pixmap(matrix=mat, clip=rect, alpha=False).tobytes("png")
    except Exception:  # noqa: BLE001 - 미리 보기를 못 그려도 고르기는 된다
        log.debug("미리 보기 그리기 실패", exc_info=True)
        return None


def preview_size(src: dict, font_file: str | None, scale: float = 1.0) -> float:
    """미리 보기 글자 크기(pt): 한글 넣기와 같이 원문 글자 높이를 그 글꼴로 맞춘 크기 × 배율."""
    from app.typeset_ink import start_size_for_ink

    base = None
    if src.get("glyph_h_pt") and font_file:
        try:
            base = start_size_for_ink(src["glyph_h_pt"], str(resolve(font_file)))
        except Exception:  # noqa: BLE001
            base = None
    if base is None:
        base = float(src.get("size_pt") or 8.0)
    return base * float(scale or 1.0)
