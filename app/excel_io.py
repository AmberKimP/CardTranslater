"""번역 검수 엑셀 내보내기·가져오기 (PRD 4장).

    export_translation(project, out_path, pages) -> Path
    import_translation(project, xlsx_path) -> ImportResult

한 행 = 카드 한 장(PDF 한 쪽)의 영역 하나. key는 `파일명#p<쪽>#<영역>`이다
(PDF 한 파일에 카드가 여러 장이라 PRD의 `파일명#영역명`에 쪽 번호를 넣었다).
태그 영역은 라벨 하나에 한 줄(셀 안 줄바꿈)로 적는다.

합성(8단계)에는 H열 ‘번역’과 I열 ‘글자 크기’만 쓴다 (열은 이름으로 찾으므로 순서가 바뀌어도 된다). 1단계에는 기계 번역이 없으므로
초벌 번역(F)과 번역(G)의 처음 값은 samples.json의 한글 초안이다.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.extract import MODEL_LABEL, load_extracted, verification_label
from app.translate import check, lookup, terms_in
from app.project import Project, sha256_file

SHEET = "번역"
INFO = "정보"
HEADERS = ["key", "카드", "영역", "미리보기", "원문", "원문 확인", "초벌 번역", "번역", "글자 크기", "상태", "사용한 용어",
           "메모", "그룹", "글꼴"]
FONT_SHEET = "글꼴"
ICON_SHEET = "아이콘"
DEFAULT_FONT = "fonts/notosanskr/NotoSansKR-Regular.ttf"
# '원문 확인' 값: Sonnet 1회 | 두 번 확인 | 결론 못 냄 | Opus 1회 | 라벨 색 | 손 입력 (+ " (확인 필요)")
COL = {h: i + 1 for i, h in enumerate(HEADERS)}
from app.version import VERSION as APP_VERSION  # noqa: E402


def source_of(project: Project, extracted: dict, page: int, region: str):
    """(원문, 불확실 여부). 자동 추출 결과가 있으면 그것, 없으면 samples.json(손으로 옮긴 것)."""
    r = (extracted.get(page) or {}).get(region)
    if r is not None:
        return r.get("source"), bool(r.get("uncertain"))
    return ((project.samples.get(page) or {}).get(region) or {}).get("source"), False


def _project_translation(file_dir: Path) -> tuple[dict, list[dict]]:
    """(프로젝트 번역 캐시, 확정 용어집). 게임 프로젝트의 그룹 파일이 아니면 ({}, [])."""
    from app.glossary import load_glossary
    from app.translate import load_cache
    from app.workspace import project_of

    info = project_of(file_dir)
    if info is None:
        return {}, []
    return load_cache(info.dir), load_glossary(info).get("terms", [])


def font_label(style: dict | None) -> str:
    """영역 서식의 글꼴 이름 (예: 'Gaegu (Gaegu-Regular.ttf)')."""
    font = (style or {}).get("font") or {}
    f = font.get("file")
    if not f:
        return f"Noto Sans KR ({Path(DEFAULT_FONT).name}, 기본값)"
    return f"{font.get('family') or Path(f).stem} ({Path(f).name})"


def group_of(project: Project) -> str:
    """게임 프로젝트의 그룹 이름 (예전 방식 프로젝트는 '-')."""
    from app.workspace import project_of

    return project.root.parent.parent.name if project_of(project.root) else "-"


def _font_sheet(wb, groups: list) -> None:
    """groups: [(Project, 그룹 이름)] (그룹마다 영역 템플릿 하나)."""
    from openpyxl.styles import Font

    ws = wb.create_sheet(FONT_SHEET)
    head = ["그룹", "영역", "종류", "글꼴", "글꼴 파일", "대체 글꼴(없는 글자)", "굵은 글꼴", "색", "정렬", "회전", "고른 이유"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
    for project, group in groups:
        for reg in project.template.regions:
            st = reg.style or {}
            font = st.get("font") or {}
            ws.append([group, reg.name, reg.kind, font_label(st), font.get("file") or DEFAULT_FONT,
                       font.get("fallback_file", "fonts/notosanskr/NotoSansKR-Bold.ttf") or "-",
                       font.get("bold_file") or "-", st.get("color", "#000000"), st.get("align", "left"),
                       st.get("rotate") or 0, font.get("chosen") or ""])
    for col, w in zip("ABCDEFGHIJK", (16, 10, 10, 34, 44, 40, 30, 9, 8, 6, 30)):
        ws.column_dimensions[col].width = w
    ws.protection.sheet = True


def _icon_sheet(wb, files: list, drafts: dict) -> int:
    """원문에 든 아이콘의 쪽·영역·줄·순서와 상태. files: [(Project, 추출 결과, 쪽)]. 확인할 줄 수를 돌려준다."""
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Font, PatternFill

    from app import icons
    from app.workspace import project_of

    ws = wb.create_sheet(ICON_SHEET)
    head = ["key", "카드", "영역", "줄", "줄에서 순서", "아이콘", "그림", "상태", "번역에 있음", "원문"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
    need = PatternFill("solid", fgColor="F4B183")
    row, n_check = 2, 0
    for project, extracted, pages in files:
        info = project_of(project.root)
        dic = icons.load_dictionary(info)["icons"] if info else {}
        idir = icons.icons_dir(info) if info else None
        pdf = project.source_pdf.name
        for pno in pages:
            for reg in project.template.regions_for(pno):
                ext = (extracted.get(pno) or {}).get(reg.name) or {}
                src = ext.get("source")
                if not isinstance(src, str):
                    continue
                key = make_key(pdf, pno, reg.name)
                for li, k, name in icons.tokens_in(src):
                    st, check_it = icons.token_status(name, dic, ext.get("icon_note", ""))
                    ko = drafts.get(key) or ""
                    ws.append([key, f"{pdf} p{pno}", reg.name, li, k, f"[[{name}]]", None, st,
                               ("예" if f"[[{name}]]" in ko else "아니오") if ko else "번역 없음", src])
                    if check_it:
                        n_check += 1
                        for c in (6, 8):
                            ws.cell(row, c).fill = need
                    p = idir / dic[name].get("file", f"{name}.png") if idir and name in dic else None
                    if p and p.exists():
                        img = XLImage(str(p))
                        sc = 20 / max(img.height, 1)
                        img.width, img.height = int(img.width * sc), int(img.height * sc)
                        ws.add_image(img, ws.cell(row, 7).coordinate)
                        ws.row_dimensions[row].height = 18
                    row += 1
    if row == 2:
        ws.append(["(아이콘 없음)"])
    for col, w in zip("ABCDEFGHIJ", (28, 18, 10, 5, 9, 14, 7, 40, 10, 60)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws.protection.sheet = True
    return n_check


def make_key(pdf_name: str, page: int, region: str) -> str:
    return f"{pdf_name}#p{page}#{region}"


def _to_cell(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, list):
        return "\n".join(v)
    return str(v)


def _from_cell(v, is_list: bool):
    """셀 값 -> 합성용 값. 빈 칸은 None(번역 안 함), `-`는 지우기만."""
    if v is None:
        return None
    s = str(v).replace("\r\n", "\n").replace("_x000D_", "")
    if s.strip() == "":
        return None
    if is_list and s.strip() != "-":
        return [t.strip() for t in s.split("\n") if t.strip()]
    return s


def _region_preview(src_doc, page_no: int, rect_ratio: dict, dpi: int = 150):
    import pymupdf
    from PIL import Image

    page = src_doc[page_no - 1]
    W, H = page.rect.width, page.rect.height
    x, y, w, h = rect_ratio["x"], rect_ratio["y"], rect_ratio["w"], rect_ratio["h"]
    clip = pymupdf.Rect(x * W - 2, y * H - 2, (x + w) * W + 2, (y + h) * H + 2) & page.rect
    pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


KEEP_COLS = ("번역", "메모", "글자 크기")  # 사람이 고치는 열: 다시 내보낼 때 이전 엑셀에서 가져온다


def read_human_edits(xlsx_path: Path) -> dict:
    """이전 검수 엑셀에서 사람이 고친 칸 {key: {"번역", "메모", "글자 크기"}}.
    번역이 초벌 번역과 다르거나, 메모·글자 크기를 적은 행만."""
    from openpyxl import load_workbook

    files = game_files(xlsx_path)
    wb = load_workbook(xlsx_path, data_only=True)
    if files is not None:  # 게임 검수 엑셀: {"시트|key": ...}
        sheets = [(f["시트"], f"{f['시트']}|") for f in files]
        sheets = list(dict.fromkeys(sheets))
    else:
        sheets = [(SHEET, "")]
    out = {}
    for name, prefix in sheets:
        if name not in wb.sheetnames:
            continue
        ws = wb[name]
        head = [c.value for c in ws[1]]
        if "key" not in head:
            continue
        ix = {h: head.index(h) for h in head if h}
        for r in ws.iter_rows(min_row=2, values_only=True):
            key = r[ix["key"]]
            if not key:
                continue
            vals = {h: r[ix[h]] for h in KEEP_COLS if h in ix}
            edited = ("초벌 번역" in ix and vals.get("번역") != r[ix["초벌 번역"]]) or vals.get("메모") or vals.get("글자 크기")
            if edited:
                out[prefix + str(key)] = vals
    return out


GAME_KIND = "번역 검수(게임)"
SERIOUS_COLOR = "FF7C80"  # 빨강: 꼭 고쳐야 하는 칸 (의심만 가는 칸은 노랑·주황 그대로)
SERIOUS_STATUS = ("빈 영역", "원문 글자 남음", "아이콘 불일치")
COLOR_LEGEND = ("색: 빨강 = 꼭 고칠 것 (번역 없음·일본어 남음·아이콘 불일치·두 번 읽어도 결론 못 냄) · "
                "노랑 = 확인해 볼 것 (읽기 불확실·용어 불일치) · 연두 = 두 번 읽어 맞음 · 크림 = 고칠 칸")  # 게임 하나에 엑셀 하나, 그룹마다 시트 (정보 시트의 ‘종류’)
GAME_INFO_HEAD = ["시트", "프로젝트", "그룹", "원본 파일", "원본 sha256", "쪽"]


def _setup_sheet(ws) -> None:
    from openpyxl.styles import Font

    ws.append(HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True)
    widths = {"key": 26, "카드": 14, "영역": 8, "미리보기": 48, "원문": 40, "원문 확인": 12, "초벌 번역": 40, "번역": 40,
              "글자 크기": 9, "상태": 14, "사용한 용어": 16, "메모": 20, "그룹": 16, "글꼴": 30}
    for h, wdt in widths.items():
        ws.column_dimensions[ws.cell(1, COL[h]).column_letter].width = wdt
    ws.freeze_panes = "B2"


def _lock(ws) -> None:
    # key·초벌 번역 등 앱이 채운 열은 잠근다 (암호 없음: 실수 방지용)
    ws.protection.sheet = True
    ws.protection.formatColumns = False
    ws.protection.formatRows = False


def _write_rows(ws, project: Project, pages: list[int], keep: dict | None, row: int) -> tuple[int, dict]:
    """파일 하나의 행을 row부터 쓴다. (다음 행, {key: 번역})."""
    import pymupdf
    from openpyxl.comments import Comment
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, PatternFill, Protection

    edit_fill = PatternFill("solid", fgColor="FFF8DC")
    unsure_fill = PatternFill("solid", fgColor="FFE066")
    no_verdict_fill = PatternFill("solid", fgColor=SERIOUS_COLOR)  # 두 번 읽어도 결론 못 낸 칸 (심각: 빨강)
    serious_fill = PatternFill("solid", fgColor=SERIOUS_COLOR)
    checked_fill = PatternFill("solid", fgColor="D9EAD3")  # 두 번 확인해 맞은 칸 (연두)

    src_doc = pymupdf.open(project.source_pdf)
    pdf = project.source_pdf.name
    extracted = load_extracted(project.root)
    tr_cache, gl_terms = _project_translation(project.root)
    group = group_of(project)
    fonts = {r.name: font_label(r.style) for r in project.template.regions}
    from app import card_edits

    edits = card_edits.load(project.root)  # 결과 보기에서 고친 번역
    drafts: dict = {}
    for pno in pages:
        card = project.samples.get(pno) or {}
        for reg in project.template.regions_for(pno):
            entry = card.get(reg.name) or {}
            src, unsure = source_of(project, extracted, pno, reg.name)
            tr = lookup(tr_cache, src) if (tr_cache and src) else None
            draft = _to_cell(tr["ko"]) if tr else _to_cell(entry.get("ko"))  # 초벌 번역(5단계) 우선, 없으면 초안
            used = terms_in(_to_cell(src) or "", gl_terms) if gl_terms else []
            status = [s for s in ("확인 필요" if unsure else None, None if draft else "빈 영역") if s]
            status += (tr or {}).get("status", [])
            ext = (extracted.get(pno) or {}).get(reg.name)
            verdict = verification_label(ext)
            vals = {
                "key": make_key(pdf, pno, reg.name), "카드": f"{pdf} p{pno}", "영역": reg.name,
                "원문": _to_cell(src), "원문 확인": verdict, "초벌 번역": draft, "번역": draft,
                "상태": ", ".join(status) or None,
                "사용한 용어": ", ".join(f"{t['source']} → {t['ko']}" for t in used) or None,
                "그룹": group, "글꼴": fonts[reg.name],
            }
            kept = (keep or {}).get(vals["key"])
            if kept:
                vals.update({h: v for h, v in kept.items() if h in KEEP_COLS})
            e = card_edits.edit_for(edits, pno, reg.name, vals["번역"])
            if e:  # 엑셀 값이 고칠 때 그대로면 결과 보기에서 고친 번역을 엑셀에도 넣는다
                vals["번역"] = _to_cell(e["ko"])
                vals["메모"] = " / ".join(x for x in (vals.get("메모"), "앱에서 고침") if x)
            drafts[vals["key"]] = vals["번역"] or draft
            for h, v in vals.items():
                ws.cell(row, COL[h], v)
            for h in HEADERS:
                ws.cell(row, COL[h]).alignment = Alignment(wrap_text=True, vertical="top")
            for h in ("원문", "번역", "글자 크기", "메모"):
                ws.cell(row, COL[h]).protection = Protection(locked=False)
            ws.cell(row, COL["번역"]).fill = edit_fill
            # 상태: 심각(빨강) — 번역 없음·원문 글자 남음·아이콘 불일치·결론 못 냄 / 의심(노랑) — 확인 필요·용어 불일치
            serious = [x for x in status if x in SERIOUS_STATUS] + (["결론 못 냄"] if verdict == "결론 못 냄" else [])
            if serious:
                ws.cell(row, COL["상태"]).fill = serious_fill
                if not vals["번역"]:
                    ws.cell(row, COL["번역"]).fill = serious_fill
            elif status:
                ws.cell(row, COL["상태"]).fill = unsure_fill
            if unsure:  # 추출이 불확실한 칸은 노란색 (PRD 2장)
                ws.cell(row, COL["원문"]).fill = unsure_fill
            if verdict == "결론 못 냄":
                ws.cell(row, COL["원문 확인"]).fill = no_verdict_fill
            elif verdict == "두 번 확인":
                ws.cell(row, COL["원문 확인"]).fill = checked_fill
            if ext and ext.get("uncertain") and (ext.get("note") or ext.get("readings")):
                lines = [f"{MODEL_LABEL.get(x['model'], x['model'])}: {_to_cell(x['source'])}"
                         for x in ext.get("readings") or []]
                text = "\n".join(lines + ([ext["note"]] if ext.get("note") else []))
                ws.cell(row, COL["원문"]).comment = Comment(text[:1000], "CardTranslater")
            img = _region_preview(src_doc, pno, reg.rect.to_dict())
            scale = min(1.0, 330 / img.width)
            if scale < 1.0:
                img = img.resize((int(img.width * scale), int(img.height * scale)))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            ws.add_image(XLImage(buf), ws.cell(row, COL["미리보기"]).coordinate)
            ws.row_dimensions[row].height = max(30, img.height * 0.75 + 4)
            row += 1
    src_doc.close()
    return row, drafts


def export_translation(project: Project, out_path: Path, pages: list[int], keep: dict | None = None) -> Path:
    """파일 하나의 검수 엑셀 (예전 방식·명령줄). keep: read_human_edits()의 결과."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    _setup_sheet(ws)
    _row, drafts = _write_rows(ws, project, pages, keep, 2)
    _lock(ws)
    group = group_of(project)
    extracted = load_extracted(project.root)
    n_icon_check = _icon_sheet(wb, [(project, extracted, pages)], drafts)
    _font_sheet(wb, [(project, group)])

    info = wb.create_sheet(INFO)
    for k, v in [
        ("프로젝트", project.root.name),
        ("원본 파일", project.source_pdf.name),
        ("원본 sha256", sha256_file(project.source_pdf)),
        ("원문 언어", project.template.source_language),
        ("내보낸 시각", time.strftime("%Y-%m-%d %H:%M:%S")),
        ("앱 버전", APP_VERSION),
        ("쪽", ",".join(map(str, pages))),
        ("그룹", group),
        ("확인할 아이콘", n_icon_check),
    ]:
        info.append([k, v])
    info.column_dimensions["A"].width = 14
    info.column_dimensions["B"].width = 70
    info.protection.sheet = True

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


def _sheet_title(label: str, used: set) -> str:
    bad = set("[]:*?/\\")
    t = "".join("_" if c in bad else c for c in label).strip() or "그룹"
    t = t[:31]
    base, n = t, 2
    while t in used or t in (INFO, ICON_SHEET, FONT_SHEET):
        suf = f" ({n})"
        t = base[:31 - len(suf)] + suf
        n += 1
    used.add(t)
    return t


def export_game(game: str, entries: list[dict], out_path: Path, keep: dict | None = None) -> Path:
    """게임 하나의 검수 엑셀: 그룹마다 시트 하나 (여러 프로젝트에 같은 이름의 그룹이 있으면 ‘프로젝트·그룹’).
    entries: [{"project_name", "group", "project": Project, "pages"}]. keep: read_human_edits()의 결과
    ({"시트|key": {...}})."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    names: dict = {}
    for e in entries:
        names.setdefault(e["group"], set()).add(e["project_name"])
    used: set = set()
    sheets: dict = {}
    rows: dict = {}
    info_rows, icon_files, font_groups, all_drafts = [], [], [], {}
    for e in entries:
        label = e["group"] if len(names[e["group"]]) == 1 else f"{e['project_name']}·{e['group']}"
        sk = (e["project_name"], e["group"])
        if sk not in sheets:
            ws = wb.create_sheet(_sheet_title(label, used))
            _setup_sheet(ws)
            sheets[sk] = ws
            rows[ws.title] = 2
            font_groups.append((e["project"], label))
        ws = sheets[sk]
        # 이름을 바꾼 그룹: 예전 이름으로 만든 시트의 사람 고침도 이어받는다
        olds = {_sheet_title(o if len(names[e["group"]]) == 1 else f"{e['project_name']}·{o}", set())
                for o in e.get("old_groups") or []}
        k = {}
        for key, v in (keep or {}).items():
            sheet, rest = key.split("|", 1)
            if sheet in olds:
                k.setdefault(rest, v)
        k.update({key.split("|", 1)[1]: v for key, v in (keep or {}).items() if key.split("|", 1)[0] == ws.title})
        rows[ws.title], drafts = _write_rows(ws, e["project"], e["pages"], k, rows[ws.title])
        all_drafts.update(drafts)
        icon_files.append((e["project"], load_extracted(e["project"].root), e["pages"]))
        info_rows.append([ws.title, e["project_name"], e["group"], e["project"].source_pdf.name,
                          sha256_file(e["project"].source_pdf), ",".join(map(str, e["pages"]))])
    for ws in sheets.values():
        _lock(ws)
    n_icon_check = _icon_sheet(wb, icon_files, all_drafts)
    _font_sheet(wb, font_groups)
    info = wb.create_sheet(INFO)
    for k_, v in [("종류", GAME_KIND), ("게임", game), ("내보낸 시각", time.strftime("%Y-%m-%d %H:%M:%S")),
                  ("앱 버전", APP_VERSION), ("확인할 아이콘", n_icon_check), ("색", COLOR_LEGEND)]:
        info.append([k_, v])
    info.append([])
    info.append(GAME_INFO_HEAD)
    for r in info_rows:
        info.append(r)
    for col, w in zip("ABCDEF", (24, 16, 16, 24, 70, 40)):
        info.column_dimensions[col].width = w
    info.protection.sheet = True
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


def game_files(xlsx_path: Path) -> list | None:
    """게임 검수 엑셀이면 파일 목록 [{"시트", "프로젝트", "그룹", "원본 파일", "원본 sha256", "쪽"}], 아니면 None."""
    from openpyxl import load_workbook

    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        if INFO not in wb.sheetnames:
            return None
        rows = list(wb[INFO].iter_rows(values_only=True))
    finally:
        wb.close()
    meta = {str(r[0]): r[1] for r in rows if r and r[0] and len(r) > 1}
    if meta.get("종류") != GAME_KIND:
        return None
    h = next((i for i, r in enumerate(rows) if r and list(r[:len(GAME_INFO_HEAD)]) == GAME_INFO_HEAD), None)
    if h is None:
        return []
    return [dict(zip(GAME_INFO_HEAD, r)) for r in rows[h + 1:] if r and r[0]]


@dataclass
class ImportResult:
    cards: dict  # page -> {영역: {"source": ..., "ko": ..., "fixed_size_pt": ...}}  (samples.json과 같은 모양)
    blocking: list[str] = field(default_factory=list)  # 이것이 있으면 합성하지 않는다
    issues: list[str] = field(default_factory=list)  # 알리고 계속할지 묻는 것
    rows: int = 0


def import_translation(project: Project, xlsx_path: Path) -> ImportResult:
    from openpyxl import load_workbook

    from app.workspace import project_of

    res = ImportResult(cards={})
    files = game_files(xlsx_path)
    wb = load_workbook(xlsx_path, data_only=True)
    if files is not None:  # 게임 검수 엑셀: 이 파일의 그룹 시트에서 이 파일의 행만
        pinfo = project_of(project.root)
        from app.workspace import group_names

        pname, group, pdf_name = (pinfo.name if pinfo else "-"), group_of(project), project.source_pdf.name
        names = group_names(project.root.parent.parent) if pinfo else {group}  # 이름을 바꾼 그룹은 예전 이름도
        info = next((f for f in files if f["프로젝트"] == pname and f["그룹"] in names and f["원본 파일"] == pdf_name),
                    None)
        if info is None:
            res.blocking.append(f"게임 검수 엑셀에 이 파일이 없습니다 ({pname} / {group} / {pdf_name}). 검수 엑셀을 다시 만드세요")
            return res
        if info.get("원본 sha256") and info["원본 sha256"] != sha256_file(project.source_pdf):
            res.blocking.append("엑셀을 내보낸 뒤 원본 PDF가 바뀌었습니다")
        if info["시트"] not in wb.sheetnames:
            res.blocking.append(f"시트가 없습니다: {info['시트']}")
        if res.blocking:
            return res
        ws = wb[info["시트"]]
        only_pdf = True
    else:
        if SHEET not in wb.sheetnames or INFO not in wb.sheetnames:
            res.blocking.append(f"`{SHEET}`·`{INFO}` 시트가 없습니다. 이 앱이 내보낸 엑셀인지 확인하세요")
            return res
        info = {str(r[0].value): r[1].value for r in wb[INFO].iter_rows(min_row=1) if r[0].value}
        if info.get("프로젝트") != project.root.name:
            res.blocking.append(f"다른 프로젝트의 엑셀입니다 (엑셀: {info.get('프로젝트')}, 지금: {project.root.name})")
        if info.get("원본 sha256") and info.get("원본 sha256") != sha256_file(project.source_pdf):
            res.blocking.append("엑셀을 내보낸 뒤 원본 PDF가 바뀌었습니다")
        if res.blocking:
            return res
        ws = wb[SHEET]
        only_pdf = False
    head = [c.value for c in ws[1]]
    missing_cols = [h for h in ("key", "원문", "번역", "글자 크기") if h not in head]
    if missing_cols:
        res.blocking.append(f"열이 없습니다: {', '.join(missing_cols)}")
        return res
    ix = {h: head.index(h) for h in head if h}

    pdf = project.source_pdf.name
    extracted = load_extracted(project.root)
    _cache, gl_terms = _project_translation(project.root)
    kinds = {r.name: r.kind for r in project.template.regions}
    exported_pages = [int(p) for p in str(info.get("쪽") or "").split(",") if p.strip()]
    expected = {make_key(pdf, p, r) for p in exported_pages for r in kinds}
    seen: set[str] = set()
    for r in ws.iter_rows(min_row=2, values_only=True):
        key = r[ix["key"]]
        if not key:
            continue
        key = str(key).strip()
        if only_pdf and key.split("#", 1)[0] != pdf:
            continue  # 같은 그룹 시트의 다른 파일 행
        res.rows += 1
        if key in seen:
            res.blocking.append(f"key가 두 번 나옵니다: {key}")
            continue
        seen.add(key)
        try:
            fname, ptag, region = key.split("#", 2)
            page = int(ptag.lstrip("p"))
        except ValueError:
            res.issues.append(f"알 수 없는 key 형식(건너뜀): {key}")
            continue
        if fname != pdf or region not in kinds:
            res.issues.append(f"프로젝트에 없는 행(건너뜀): {key}")
            continue
        is_list = kinds[region] == "tag_pills"
        ko = _from_cell(r[ix["번역"]], is_list)
        src = _from_cell(r[ix["원문"]], is_list)
        entry: dict = {"source": src, "ko": ko}
        size = r[ix["글자 크기"]]
        if size not in (None, ""):
            try:
                entry["fixed_size_pt"] = float(size)
            except (TypeError, ValueError):
                res.issues.append(f"글자 크기가 숫자가 아님(무시): {key} = {size!r}")
        if ko is None:
            res.issues.append(f"번역이 비어 있음(한글을 넣지 않음): {key}")
        elif gl_terms and src and ko != "-":  # 사람이 고친 번역도 용어집을 지켰는지 (PRD 5.3 다)
            st, missing = check(_to_cell(src), _to_cell(ko), gl_terms)
            if missing:
                res.issues.append(f"용어 불일치(기록만): {key}: {', '.join(missing)}")
            if "아이콘 불일치" in st:
                res.issues.append(f"아이콘 불일치(기록만): {key}")
        orig, _ = source_of(project, extracted, page, region)
        if orig is not None and src is not None and src != orig:
            res.issues.append(f"원문을 고친 행(기록만 함): {key}")
        res.cards.setdefault(page, {"page": page})[region] = entry
    for k in sorted(expected - seen):
        res.issues.append(f"엑셀에 없는 행(한글을 넣지 않음): {k}")
    return res
