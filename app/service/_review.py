"""결과 쪽·카드별 지우기 방식·카드 고치기와 ⑥ 번역 검수 표."""

from __future__ import annotations

import json
from pathlib import Path

from app.project import load_project
from app.service import _base
from app.service import _erase_compose
from app.service import _extract
from app.service import _overview
from app.service import _translate_erase


# ---------------------------------------------------------------- 화면용: 결과 보기·테스트 실행 (3차)
def result_pages(run_dir: Path | str) -> list[dict]:
    """한글 넣기 결과 폴더(translated/ 또는 시험 폴더)의 쪽마다: {"page", "card_png", "status": [..],
    "regions": [{"name", "status", "png", "errors", "overflow"(넣지 못한 글자)}]}.
    상태(넘침·글자 축소·아이콘 오류 등)가 있는 칸을 표시하는 데 쓴다."""
    from app.typeset_compose import safe_name

    d = Path(run_dir)
    rep_f = d / "report.json"
    if not rep_f.exists():
        return []
    rep = json.loads(rep_f.read_text(encoding="utf-8"))
    out = []
    for pd in rep.get("pages_detail", []):
        pno = int(pd["page"])
        regions, page_status = [], set()
        for idx, (name, rec) in enumerate(pd.get("regions", {}).items()):
            st = list(rec.get("status") or [])
            for r in rec.get("results") or []:  # 태그 라벨마다
                st += [s for s in r.get("status") or [] if s not in st]
            page_status.update(st)
            png = d / "compare" / f"p{pno:03d}_{safe_name(name, idx)}.png"
            regions.append({"name": name, "status": st, "png": png if png.exists() else None,
                            "errors": rec.get("errors") or [],
                            "overflow": " ".join((rec.get("overflow_text") or "").split())})  # 넣지 못한 글자
        card = d / "compare" / f"p{pno:03d}_card.png"
        out.append({"page": pno, "card_png": card if card.exists() else None, "status": sorted(page_status),
                    "regions": regions})
    return out


def card_erase_info(file_dir: Path | str, page: int) -> dict:
    """카드 한 장의 영역별 지우기: {영역: {"mode": 이 카드에 정한 방식, "default": 영역 설정, "used": 실제로 쓴 방식,
    "needs_choice": 그림 배경이라 확인이 필요한가, "text_mask": 글자 모양만 지우는 영역인가}}."""
    from app.workspace import file_settings

    fd = Path(file_dir)
    meta_f = fd / "cleared" / "meta" / f"page-{int(page):03d}.json"
    meta = json.loads(meta_f.read_text(encoding="utf-8")) if meta_f.exists() else {"regions": []}
    over = file_settings(fd).get("erase_modes", {}).get(str(int(page)), {})
    proj = load_project(fd)
    out = {}
    for reg in proj.template.regions_for(int(page)):
        m = next((r for r in meta["regions"] if r["name"] == reg.name), None)
        tm = ((m or {}).get("erase") or {}).get("text_mask")
        out[reg.name] = {"mode": over.get(reg.name), "default": (reg.erase or {}).get("mode") or "auto",
                         "used": (tm or {}).get("used"), "needs_choice": bool((tm or {}).get("needs_choice")),
                         "text_mask": tm is not None, "kind": reg.kind}
    return out


def set_card_erase_mode(file_dir: Path | str, page: int, region: str, mode: str | None) -> None:
    """이 카드의 이 영역만 다른 방식으로 지운다 (None·빈 값이면 영역 설정을 따름). 반영은 redo_file로."""
    from app import workspace

    fd = Path(file_dir)
    if mode and mode not in dict(_overview.ERASE_MODES):
        raise ValueError(f"모르는 지우기 방식: {mode}")
    data = workspace.file_settings(fd)
    modes = data.setdefault("erase_modes", {})
    card = modes.setdefault(str(int(page)), {})
    if mode:
        card[region] = mode
    else:
        card.pop(region, None)
    if not card:
        modes.pop(str(int(page)), None)
    if not modes:
        data.pop("erase_modes", None)
    workspace._write(fd / "file.json", data)


def card_text_offset(file_dir: Path | str, page: int, region: str) -> tuple[float, float]:
    """결과 보기에서 옮긴 이 카드·이 영역의 글 위치 (dx, dy) pt (오른쪽·아래가 +). 옮기지 않았으면 (0.0, 0.0)."""
    from app.typeset_compose import text_offsets_for_page

    return text_offsets_for_page(Path(file_dir), int(page)).get(region, (0.0, 0.0))


def card_text_offsets(file_dir: Path | str, page: int) -> dict:
    """이 카드에서 옮긴 영역 모두 {영역: (dx, dy)} pt."""
    from app.typeset_compose import text_offsets_for_page

    return text_offsets_for_page(Path(file_dir), int(page))


def set_card_text_offset(file_dir: Path | str, page: int, region: str, dx: float, dy: float) -> None:
    """이 카드의 이 영역 글을 원래 자리에서 (dx, dy) pt만큼 옮겨 넣는다 (둘 다 0이면 옮긴 것을 지움).
    file.json에 적는다. 한글 넣기 재료(compose_sig)에 들어가므로 recompose_file이 이 쪽만 다시 넣는다."""
    from app import workspace
    from app.typeset_compose import TEXT_OFFSETS_KEY

    dx, dy = float(dx), float(dy)
    if not all(abs(v) < 1e4 for v in (dx, dy)):  # NaN·터무니없는 값
        raise ValueError(f"옮길 거리가 이상합니다: {dx}, {dy}")
    fd = Path(file_dir)
    data = workspace.file_settings(fd)
    offs = data.setdefault(TEXT_OFFSETS_KEY, {})
    card = offs.setdefault(str(int(page)), {})
    if abs(dx) > 1e-6 or abs(dy) > 1e-6:
        card[region] = [round(dx, 3), round(dy, 3)]
    else:
        card.pop(region, None)
    if not card:
        offs.pop(str(int(page)), None)
    if not offs:
        data.pop(TEXT_OFFSETS_KEY, None)
    workspace._write(fd / "file.json", data)


def card_edit_info(file_dir: Path | str, page: int, region: str) -> dict:
    """결과 보기에서 고칠 칸의 지금 값: {"ko": 쓰는 번역(글, 태그 칸은 줄마다), "base": 엑셀·초벌 번역 그대로,
    "edited": 결과 보기에서 고쳤나, "font": 이 카드만 고른 글꼴(없으면 ''), "region_font": 영역 글꼴, "kind",
    "size": 이 카드만 정한 글자 크기 배율(없으면 None), "region_size": 영역 배율, "align": 이 카드만 정한 정렬(없으면 ''),
    "region_align": 영역 정렬(글꼴 창, 없으면 ''), "paragraph_mode": 문단마다 글꼴 영역인가,
    "paragraph_fonts": {문단 번호: 이 카드에서 고른 글꼴}}."""
    from app import card_edits, font_plan

    fd = Path(file_dir)
    font_plan._raw_cache.clear()  # 엑셀·고친 것이 바뀌었을 수 있다
    base_cards = _translation_cards(fd)
    entry = (base_cards.get(int(page)) or {}).get(region) or {}
    base = entry.get("ko") if isinstance(entry, dict) else None
    e = card_edits.edit_for(card_edits.load(fd), page, region, base)
    data = font_plan.load(fd)
    pg = str(int(page))
    region_font = (data.get("regions") or {}).get(region) or ""
    card_font = ((data.get("cards") or {}).get(pg) or {}).get(region) or ""
    reg = next((r for r in load_project(fd).template.regions_for(int(page)) if r.name == region), None)
    card_size = ((data.get("card_sizes") or {}).get(pg) or {}).get(region)
    card_align = ((data.get("card_aligns") or {}).get(pg) or {}).get(region) or ""
    paras = ((data.get("sentences") or {}).get(pg) or {}).get(region) or {}
    mode = font_plan.region_font_modes(fd).get(region) == "sentence" and (reg is None or reg.kind == "text")
    from app.extract import load_extracted

    ext = (load_extracted(fd).get(int(page)) or {}).get(region) or {}
    return {"ko": card_edits.norm(e["ko"] if e else base), "base": card_edits.norm(base), "edited": bool(e),
            "keep_original": bool(ext.get("keep_original")),  # 전체가 아이콘 — 원본 그대로 (지우기·한글 넣기 안 함)
            "can_keep": ext.get("source") is not None,
            "font": card_font, "region_font": region_font, "kind": reg.kind if reg else "text",
            "size": float(card_size) if card_size else None,
            "region_size": float((data.get("sizes") or {}).get(region) or 1.0),
            "align": card_align, "region_align": font_plan.align_choices(data).get(region, ""),
            "paragraph_mode": mode, "paragraph_fonts": {int(i): f for i, f in paras.items() if f}}


def split_paragraphs(text: str) -> list[str]:
    """글을 문단으로 (결과 보기·글꼴 창의 문단 목록용, 한글 넣기와 같은 규칙)."""
    from app.typeset_layout import paragraphs

    return paragraphs(text or "")


_TRANSLATION_CARDS: dict = {}
_NOT_FOR_CARDS = ("card_edits", "review_checked", "erase_modes", "text_offsets")  # 번역 읽기와 상관없는 file.json 키


def _file_json_sig(fd: Path) -> str:
    """file.json 중 번역 읽기에 쓰는 부분(뺀 쪽 등)의 지문. 검수 표시·고친 번역만 바뀌면 그대로 (엑셀을 다시 읽지 않게)."""
    import hashlib

    try:
        data = json.loads((Path(fd) / "file.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    data = {k: v for k, v in data.items() if k not in _NOT_FOR_CARDS} if isinstance(data, dict) else data
    return hashlib.sha1(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _regions_sig(fd: Path) -> tuple:
    """파일·그룹의 영역 템플릿(regions.json)·자동 정렬(align.json) 시각 (영역이 바뀌면 칸 목록이 바뀐다)."""
    out = []
    for p in (Path(fd) / "regions.json", Path(fd).parent.parent / "regions.json", Path(fd) / "align.json"):
        try:
            out.append(p.stat().st_mtime)
        except OSError:
            out.append(0.0)
    return tuple(out)


def _translation_cards(fd: Path) -> dict:
    """한글 넣기가 쓰는 번역 (고친 것을 덮기 전): 검수 엑셀이 있으면 그것, 없으면 초벌 번역.
    엑셀·번역 캐시·추출·file.json이 바뀌지 않았으면 다시 읽지 않는다 (게임 검수 엑셀은 그림이 있어 느리다)."""
    from app import workspace
    from app.translate import cache_path

    fd = Path(fd)
    xlsx = _translate_erase.review_xlsx_for(fd)

    def mt(p) -> float:
        try:
            return Path(p).stat().st_mtime
        except OSError:
            return 0.0

    info = workspace.project_of(fd)
    stamp = (str(xlsx), mt(xlsx) if xlsx else 0.0, mt(cache_path(info.dir)) if info else 0.0, _file_json_sig(fd),
             _regions_sig(fd), max([mt(p) for p in (fd / "extract").glob("**/page-*.json")] + [0.0]))
    hit = _TRANSLATION_CARDS.get(str(fd))
    if hit and hit[0] == stamp:
        return hit[1]
    value = _translation_cards_read(fd, xlsx)
    _TRANSLATION_CARDS[str(fd)] = (stamp, value)
    return value


def _translation_cards_read(fd: Path, xlsx) -> dict:
    if xlsx:
        from app.excel_io import import_translation

        imp = import_translation(load_project(fd), xlsx)
        if not imp.blocking:
            return {int(k): v for k, v in (imp.cards or {}).items()}
    return {int(k): v for k, v in _translate_erase.cards_from_translation(fd).items()}


def save_card_edit(file_dir: Path | str, page: int, region: str, *, text: str | None = None,
                   font: str | None = None, set_font: bool = False, size: float | None = None,
                   set_size: bool = False, align: str | None = None, set_align: bool = False,
                   paragraph_fonts: dict | None = None) -> None:
    """결과 보기에서 고친 것을 저장한다 (반영은 recompose_file·redo_file로).
    text: 고친 번역 (None이면 그대로, 엑셀·초벌 번역과 같으면 고친 것을 지움). 태그 칸은 줄마다 태그 하나.
    set_font=True면 font(None·''이면 영역 글꼴을 따름)를 이 카드의 이 영역 글꼴로.
    set_size·set_align: 이 카드만의 글자 크기 배율·정렬 (None·''이면 글꼴 창의 영역 설정).
    paragraph_fonts: {문단 번호: 글꼴 | None} — 이 카드의 문단마다 글꼴 (주면 그것으로 바꾼다)."""
    from app import card_edits, font_plan

    fd = Path(file_dir)
    if text is not None:
        entry = (_translation_cards(fd).get(int(page)) or {}).get(region) or {}
        base = entry.get("ko") if isinstance(entry, dict) else None
        ko = [t.strip() for t in text.splitlines() if t.strip()] if isinstance(base, list) else text.strip()
        card_edits.set_text(fd, page, region, ko, base)
        from app import review_marks

        review_marks.mark(fd, page, region, ko)  # 결과 보기에서 고쳐 저장한 칸 = 사람이 본 칸
        _clear_review_caches()
    if set_font:
        font_plan.set_card_font(fd, page, region, font or None)
    if set_size or set_align or paragraph_fonts is not None:
        font_plan.set_card_style(fd, page, region, size=size, align=align, set_size=set_size,
                                 set_align=set_align, paragraphs=paragraph_fonts)
    font_plan._raw_cache.clear()


def card_preview(file_dir: Path | str, page: int, region: str, text: str | None, font: str | None,
                 size: float | None = None, align: str | None = None,
                 paragraph_fonts: dict | None = None) -> bytes | None:
    """결과 보기에서 저장 전에 그 칸을 한글 넣기와 똑같이 그려 본 PNG (지금 지운 카드 위). 못 그리면 None.
    화면에서 고른 그대로 그린다: font·size가 None이면 영역 글꼴·영역 크기, align ''이면 영역 정렬
    (None이면 저장된 정렬), paragraph_fonts: 이 카드의 문단마다 글꼴 (None이면 저장된 것)."""
    from app import font_plan

    fd = Path(file_dir)
    data = font_plan.load(fd)
    f = font or (data.get("regions") or {}).get(region)  # 없으면 영역 글꼴 (화면이 ‘영역 글꼴대로’를 고른 것)
    entry = (_translation_cards(fd).get(int(page)) or {}).get(region) or {}
    ko = text
    if text is not None and isinstance(entry.get("ko"), list):
        ko = [t.strip() for t in text.splitlines() if t.strip()]
    scale = size if size else font_plan.size_scales(data).get(region, 1.0)  # 없으면 영역 크기
    if align:  # 이 카드만 고른 정렬
        al = align
    elif align == "":  # ‘영역 설정대로’: 글꼴 창의 영역 정렬
        al = font_plan.align_choices(data).get(region)
    else:  # 저장된 것 (카드별 위에 영역)
        al = font_plan.aligns_for_page(data, int(page)).get(region)
    slots = (data.get("sentence_slots") or {}).get(region)
    paras = paragraph_fonts if paragraph_fonts is not None else \
        {int(i): v for i, v in ((((data.get("sentences") or {}).get(str(int(page))) or {}).get(region)) or {}).items()}
    return font_plan.preview_render(fd, page, region, f, scale, al or None, None, ko, slots=slots, para_fonts=paras)


# ---------------------------------------------------------------- 화면용: 번역 검수
REVIEW_STATUS = {  # 엔진 상태 -> (화면 문구, 등급)
    "빈 영역": ("번역 없음", "must"), "원문 글자 남음": ("일본어 남음", "must"), "아이콘 불일치": ("아이콘 불일치", "must"),
    "결론 못 냄": ("원문 결론 못 냄", "must"), "용어 불일치": ("용어 불일치", "look"), "확인 필요": ("원문 불확실", "look"),
}


def _review_check(source, ko, terms: list[dict], ext: dict | None) -> dict:
    """번역 글 검사 (모델을 부르지 않음): {"status": [화면 문구], "level", "missing_terms", "terms"}."""
    from app.card_edits import norm
    from app.extract import verification_label
    from app.translate import check, ko_term_used, terms_in

    src, text = norm(source), norm(ko)
    raw: list[str] = []
    missing: list[str] = []
    if not text:
        raw.append("빈 영역")
    else:
        st, missing = check(src, text, terms)
        raw += st
    if ext is not None and verification_label(ext) == "결론 못 냄":
        raw.append("결론 못 냄")
    elif ext is not None and ext.get("uncertain"):
        raw.append("확인 필요")
    labels = [REVIEW_STATUS[s][0] for s in raw if s in REVIEW_STATUS]
    levels = {REVIEW_STATUS[s][1] for s in raw if s in REVIEW_STATUS}
    level = "must" if "must" in levels else "look" if "look" in levels else "ok"
    term_list = [{"source": t["source"], "ko": t["ko"], "ok": bool(text) and ko_term_used(t["ko"], text)}
                 for t in terms_in(src, terms)]
    return {"status": labels, "level": level, "missing_terms": list(missing), "terms": term_list}


def _review_file_rows(fd: Path) -> list[dict]:
    """파일 하나의 검수 줄 (아래 review_rows의 줄 모양, same_source 빼고)."""
    import hashlib

    from app import card_edits, review_marks, workspace
    from app.excel_io import _project_translation, source_of
    from app.extract import load_extracted, verification_label
    from app.translate import lookup

    proj = load_project(fd)
    extracted = load_extracted(fd)
    cache, terms = _project_translation(fd)
    base_cards = _translation_cards(fd)
    edits = card_edits.load(fd)
    marks = review_marks.load(fd)
    key = workspace.file_key(fd)
    group = workspace.group_names(fd.parent.parent)
    gname = json.loads((fd.parent.parent / "group.json").read_text(encoding="utf-8")).get("name") \
        if (fd.parent.parent / "group.json").exists() else fd.parent.parent.name
    erased = (fd / "cleared" / "cleared.pdf").exists()
    composed = (fd / "translated" / "translated.pdf").exists()
    rows = []
    for pno in workspace.included_pages(fd):
        for reg in proj.template.regions_for(pno):
            if reg.kind not in ("text", "tag_pills"):
                continue
            src, _unsure = source_of(proj, extracted, pno, reg.name)
            if src is None or not card_edits.norm(src):
                continue
            ext = (extracted.get(pno) or {}).get(reg.name)
            if ext and ext.get("keep_original"):  # ‘전체가 아이콘 — 번역 안 함’ 칸: 번역하지 않으므로 검수할 것도 없다
                continue
            hit = lookup(cache, src) if cache else None
            draft = card_edits.norm(hit["ko"]) if hit else ""
            entry = (base_cards.get(pno) or {}).get(reg.name)
            base = entry.get("ko") if isinstance(entry, dict) else None
            e = card_edits.edit_for(edits, pno, reg.name, base)
            raw_e = (edits.get(pno) or {}).get(reg.name)
            now = e["ko"] if e else base
            conflict = ({"app": card_edits.norm(raw_e.get("ko")), "now": card_edits.norm(base)}
                        if raw_e and not e and card_edits.norm(raw_e.get("ko")) != card_edits.norm(base) else None)

            chk = _review_check(src, now, terms, ext)
            checked, checked_stale = review_marks.state(marks, pno, reg.name, now)
            rows.append({
                "id": f"{key}#p{pno}#{reg.name}", "file_dir": fd, "file_key": key, "group": gname,
                "group_names": sorted(group), "page": pno, "region": reg.name, "kind": reg.kind,
                "source": card_edits.norm(src), "source_check": verification_label(ext),
                "draft": draft, "base": card_edits.norm(base), "excel_changed": card_edits.norm(base) != draft,
                "ko": card_edits.norm(now), "edited": bool(e), "edit_conflict": conflict,
                **chk, "source_id": hashlib.sha1(card_edits.norm(src).encode("utf-8")).hexdigest()[:12],
                "checked": checked, "checked_stale": checked_stale, "erased": erased, "composed": composed,
            })
    return rows


def _review_counts(rows: list[dict]) -> dict:
    return {"total": len(rows), "checked": sum(1 for r in rows if r["checked"]),
            "edited": sum(1 for r in rows if r["edited"]),
            "must_left": sum(1 for r in rows if r["level"] == "must" and not r["checked"]),
            "look_left": sum(1 for r in rows if r["level"] == "look" and not r["checked"])}


def _excel_info(fds: list[Path], info) -> dict:
    path = next((p for p in (_translate_erase.review_xlsx_for(fd) for fd in fds) if p), None)
    if path is None and info is not None:
        g = info.game_dir / "번역_검수.xlsx"
        path = g if g.exists() else None
    is_open = bool(path) and (Path(path).parent / f"~${Path(path).name}").exists()
    return {"path": str(path) if path else None, "open": is_open}


def review_rows(project_dir: Path | str, files: list | None = None) -> dict:
    """번역 검수 화면의 칸 목록 (범위: files = 파일 폴더 목록, None이면 프로젝트 전체; 처리할 쪽의 글·태그 칸).
    {"rows": [{id, file_dir, file_key, group, page, region, kind, source, source_check, draft, base, excel_changed,
               ko(한글 넣기가 쓸 글), edited, edit_conflict, status[화면 문구], level(must|look|ok), missing_terms,
               terms[{source, ko, ok}], source_id, same_source, checked, checked_stale, erased, composed}],
     "excel": {"path", "open"}, "counts": {total, checked, edited, must_left, look_left}}. 모델은 부르지 않는다."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    fds = _extract.scoped_file_dirs(info, None, files)
    rows: list[dict] = []
    for fd in fds:
        try:
            rows += _review_file_rows(fd)
        except FileNotFoundError:
            continue  # 영역 템플릿이 없는 그룹
    seen: dict[tuple, int] = {}
    for r in rows:
        k = (r["source"], r["ko"])
        seen[k] = seen.get(k, 0) + 1
    for r in rows:
        r["same_source"] = seen[(r["source"], r["ko"])] - 1
    return {"rows": rows, "excel": _excel_info(fds, info), "counts": _review_counts(rows)}


def _review_row(fd: Path, page: int, region: str) -> dict | None:
    return next((r for r in _review_file_rows(Path(fd)) if r["page"] == int(page) and r["region"] == region), None)


def check_review_text(file_dir: Path | str, page: int, region: str, text: str) -> dict:
    """입력 중 검사 (모델을 부르지 않음): {"status", "level", "missing_terms", "terms"}. 태그 칸은 한 줄에 태그 하나."""
    from app.excel_io import _project_translation, source_of
    from app.extract import load_extracted

    fd = Path(file_dir)
    proj = load_project(fd)
    extracted = load_extracted(fd)
    src, _u = source_of(proj, extracted, int(page), region)
    _cache, terms = _project_translation(fd)
    return _review_check(src, text, terms, (extracted.get(int(page)) or {}).get(region))


def save_review(file_dir: Path | str, page: int, region: str, text: str | None, *, checked: bool = True,
                same_source: bool = False, scope_files: list | None = None) -> list[dict]:
    """검수에서 고친 번역 저장(결과 보기와 같은 card_edits)·확인 표시. text가 None이면 확인 표시만.
    same_source=True면 범위(scope_files, None이면 이 파일) 안에서 원문이 같고 지금 번역도 같은 칸에도 같은 글을 넣고
    확인한다 (다른 번역을 쓰던 칸은 그대로). 바뀐 칸들의 새 줄을 돌려준다."""
    from app import card_edits, review_marks, workspace

    fd = Path(file_dir).resolve()
    before: dict[str, list[dict]] = {}

    def rows_of(d: Path) -> list[dict]:  # 파일마다 한 번만 (칸마다 파일 전체 줄을 다시 만들면 느리다)
        if str(d) not in before:
            before[str(d)] = _review_file_rows(d)
        return before[str(d)]
    me = next((r for r in rows_of(fd) if r["page"] == int(page) and r["region"] == region), None)
    if me is None:
        raise KeyError(f"검수할 칸이 없습니다: {page}쪽 {region}")
    targets = [(fd, int(page), region)]
    if same_source and text is not None:
        info = workspace.project_of(fd)
        scope = list(dict.fromkeys(Path(x).resolve() for x in (scope_files or [fd])))  # 같은 파일을 두 번 보지 않게
        for d in scope:
            for r in (rows_of(d) if info is not None else []):
                if (r["source"] == me["source"] and r["ko"] == me["ko"]
                        and not (Path(r["file_dir"]).resolve() == fd and int(r["page"]) == int(page)
                                 and r["region"] == region)):
                    targets.append((Path(r["file_dir"]).resolve(), int(r["page"]), r["region"]))
    for d, p_, reg in targets:
        if text is not None:
            entry = (_translation_cards(d).get(p_) or {}).get(reg) or {}
            base = entry.get("ko") if isinstance(entry, dict) else None
            ko = [t.strip() for t in text.splitlines() if t.strip()] if isinstance(base, list) else text.strip()
            card_edits.set_text(d, p_, reg, ko, base)
            final = ko
        else:
            final = me["ko"] if (d, p_, reg) == targets[0] else None
        if checked and final is not None:
            review_marks.mark(d, p_, reg, final)
    _clear_review_caches()
    after: dict = {}
    for d in dict.fromkeys(str(t[0]) for t in targets):
        for r in _review_file_rows(Path(d)):
            after[(d, int(r["page"]), r["region"])] = r
    return [after[k] for k in ((str(d), p_, reg) for d, p_, reg in targets) if k in after]


def set_review_checked(file_dir: Path | str, page: int, region: str, checked: bool) -> dict:
    """확인 표시/취소. 새 줄을 돌려준다."""
    from app import review_marks

    fd = Path(file_dir)
    r = _review_row(fd, page, region)
    if r is None:
        raise KeyError(f"검수할 칸이 없습니다: {page}쪽 {region}")
    review_marks.mark(fd, page, region, r["ko"] if checked else None)
    _clear_review_caches()
    return _review_row(fd, page, region)


def resolve_review_conflict(file_dir: Path | str, page: int, region: str, keep: str) -> dict:
    """앱에서 고친 뒤 엑셀·재번역으로 번역이 바뀐 칸: keep="app"이면 앱에서 고친 글을 지금 번역 위에 다시 쓰고,
    "now"면 앱에서 고친 것을 지운다 (바뀐 번역을 씀). 새 줄을 돌려준다."""
    from app import card_edits

    fd = Path(file_dir)
    r = _review_row(fd, page, region)
    if r is None or not r["edit_conflict"]:
        return r
    entry = (_translation_cards(fd).get(int(page)) or {}).get(region) or {}
    base = entry.get("ko") if isinstance(entry, dict) else None
    if keep == "app":
        app_ko = r["edit_conflict"]["app"]
        ko = [t for t in app_ko.splitlines() if t.strip()] if isinstance(base, list) else app_ko
        card_edits.set_text(fd, page, region, ko, base)
    else:
        card_edits.set_text(fd, page, region, base, base)  # 같으면 고친 것을 지운다
    _clear_review_caches()
    return _review_row(fd, page, region)


_REVIEW_PROGRESS: dict = {}


def _clear_review_caches() -> None:
    """검수 표시·고친 번역이 바뀜: 진행 수만 다시 센다 (번역 읽기 캐시는 지문으로 알아서 맞춘다)."""
    _REVIEW_PROGRESS.clear()


def review_progress(project_dir: Path | str) -> dict:
    """검수 진행 {"total", "checked", "must_left", "look_left", "by_file": {파일 키: 같은 키들}}.
    프로젝트 화면을 새로 고칠 때마다 부르므로 번역 캐시·용어집·엑셀·file.json·추출 파일 시각으로 캐시한다."""
    from app import glossary, workspace
    from app.translate import cache_path

    info = workspace.load_project_info(project_dir)

    def mt(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    fds = workspace.file_dirs(info)
    xl = _excel_info(fds, info)["path"]
    stamp = (str(info.dir), mt(cache_path(info.dir)), mt(glossary.glossary_path(info)), mt(Path(xl)) if xl else 0.0,
             mt(info.dir / "project.json"),  # 시험 모드(card_limit)가 바뀌면 칸 수가 바뀐다
             tuple((str(fd), mt(fd / "file.json"), _regions_sig(fd),
                    max([mt(p) for p in (fd / "extract").glob("**/page-*.json")] + [0.0])) for fd in fds))
    if _REVIEW_PROGRESS.get("stamp") == stamp:
        return _REVIEW_PROGRESS["value"]
    by_file = {}
    allrows: list[dict] = []
    for fd in fds:
        try:
            rows = _review_file_rows(fd)
        except FileNotFoundError:
            continue
        by_file[workspace.file_key(fd)] = _review_counts(rows)
        allrows += rows
    value = {**_review_counts(allrows), "by_file": by_file}
    _REVIEW_PROGRESS.update(stamp=stamp, value=value)
    return value


def recompose_file(file_dir: Path | str, progress: _base.ProgressFn | None = None,
                   cancel: _base.CancelToken | None = None) -> _erase_compose.ComposeResult:
    """파일 하나에 한글만 다시 넣는다 (지우기는 그대로). 고친 번역·글꼴을 반영할 때.
    검수 엑셀이 있으면 그것(확인 항목은 이미 본 것으로 봄), 없으면 초벌 번역."""
    fd = Path(file_dir)
    xlsx = _translate_erase.review_xlsx_for(fd)
    if xlsx:
        return _erase_compose.compose(fd, excel=xlsx, accept_issues=True, progress=progress, cancel=cancel)
    return _translate_erase._compose_with_cards(fd, _translate_erase.cards_from_translation(fd), progress, cancel)


def redo_file(file_dir: Path | str, progress: _base.ProgressFn | None = None,
              cancel: _base.CancelToken | None = None) -> _erase_compose.ComposeResult:
    """파일 하나를 다시 지우고(지금 설정·카드별 방식으로) 한글을 다시 넣는다. 지운 쪽은 그대로.
    검수 엑셀이 있으면 그것(확인 항목은 이미 본 것으로 봄), 없으면 초벌 번역으로 넣는다."""
    from app import workspace

    fd = Path(file_dir)
    pages = sorted(_translate_erase._cleared_pages(fd)) or workspace.included_pages(fd)
    r = _erase_compose.erase(fd, workspace.pages_spec(pages), progress=progress, cancel=cancel)
    if not r.ok:
        return _erase_compose.ComposeResult("failed", error=r.error or "지우기 실패")
    xlsx = _translate_erase.review_xlsx_for(fd)
    if xlsx:
        return _erase_compose.compose(fd, excel=xlsx, accept_issues=True, progress=progress, cancel=cancel)
    return _translate_erase._compose_with_cards(fd, _translate_erase.cards_from_translation(fd), progress, cancel)
