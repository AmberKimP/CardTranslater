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
from app.project import Project, sha256_file

SHEET = "번역"
INFO = "정보"
HEADERS = ["key", "카드", "영역", "미리보기", "원문", "원문 확인", "초벌 번역", "번역", "글자 크기", "상태", "사용한 용어",
           "메모"]
# '원문 확인' 값: Sonnet 1회 | 두 번 확인 | 결론 못 냄 | Opus 1회 | 라벨 색 | 손 입력 (+ " (확인 필요)")
COL = {h: i + 1 for i, h in enumerate(HEADERS)}
APP_VERSION = "phase1"


def source_of(project: Project, extracted: dict, page: int, region: str):
    """(원문, 불확실 여부). 자동 추출 결과가 있으면 그것, 없으면 samples.json(손으로 옮긴 것)."""
    r = (extracted.get(page) or {}).get(region)
    if r is not None:
        return r.get("source"), bool(r.get("uncertain"))
    return ((project.samples.get(page) or {}).get(region) or {}).get("source"), False


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


def export_translation(project: Project, out_path: Path, pages: list[int]) -> Path:
    import pymupdf
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font, PatternFill, Protection

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    ws.append(HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True)
    widths = {"key": 26, "카드": 14, "영역": 8, "미리보기": 48, "원문": 40, "원문 확인": 12, "초벌 번역": 40, "번역": 40,
              "글자 크기": 9, "상태": 14, "사용한 용어": 16, "메모": 20}
    for h, wdt in widths.items():
        ws.column_dimensions[ws.cell(1, COL[h]).column_letter].width = wdt
    ws.freeze_panes = "B2"
    edit_fill = PatternFill("solid", fgColor="FFF8DC")
    unsure_fill = PatternFill("solid", fgColor="FFE066")
    no_verdict_fill = PatternFill("solid", fgColor="F4B183")  # 두 번 읽어도 결론 못 낸 칸 (주황)
    checked_fill = PatternFill("solid", fgColor="D9EAD3")  # 두 번 확인해 맞은 칸 (연두)

    src_doc = pymupdf.open(project.source_pdf)
    pdf = project.source_pdf.name
    extracted = load_extracted(project.root)
    row = 2
    for pno in pages:
        card = project.samples.get(pno) or {}
        for reg in project.template.regions:
            entry = card.get(reg.name) or {}
            draft = _to_cell(entry.get("ko"))
            src, unsure = source_of(project, extracted, pno, reg.name)
            status = [s for s in ("확인 필요" if unsure else None, None if draft else "빈 영역") if s]
            ext = (extracted.get(pno) or {}).get(reg.name)
            verdict = verification_label(ext)
            vals = {
                "key": make_key(pdf, pno, reg.name), "카드": f"{pdf} p{pno}", "영역": reg.name,
                "원문": _to_cell(src), "원문 확인": verdict, "초벌 번역": draft, "번역": draft,
                "상태": ", ".join(status) or None,
            }
            for h, v in vals.items():
                ws.cell(row, COL[h], v)
            for h in HEADERS:
                ws.cell(row, COL[h]).alignment = Alignment(wrap_text=True, vertical="top")
            for h in ("원문", "번역", "글자 크기", "메모"):
                ws.cell(row, COL[h]).protection = Protection(locked=False)
            ws.cell(row, COL["번역"]).fill = edit_fill
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
            xl = XLImage(buf)
            ws.add_image(xl, ws.cell(row, COL["미리보기"]).coordinate)
            ws.row_dimensions[row].height = max(30, img.height * 0.75 + 4)
            row += 1
    src_doc.close()
    # key·초벌 번역 등 앱이 채운 열은 잠근다 (암호 없음: 실수 방지용)
    ws.protection.sheet = True
    ws.protection.formatColumns = False
    ws.protection.formatRows = False

    info = wb.create_sheet(INFO)
    for k, v in [
        ("프로젝트", project.root.name),
        ("원본 파일", pdf),
        ("원본 sha256", sha256_file(project.source_pdf)),
        ("원문 언어", project.template.source_language),
        ("내보낸 시각", time.strftime("%Y-%m-%d %H:%M:%S")),
        ("앱 버전", APP_VERSION),
        ("쪽", ",".join(map(str, pages))),
    ]:
        info.append([k, v])
    info.column_dimensions["A"].width = 14
    info.column_dimensions["B"].width = 70
    info.protection.sheet = True

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


@dataclass
class ImportResult:
    cards: dict  # page -> {영역: {"source": ..., "ko": ..., "fixed_size_pt": ...}}  (samples.json과 같은 모양)
    blocking: list[str] = field(default_factory=list)  # 이것이 있으면 합성하지 않는다
    issues: list[str] = field(default_factory=list)  # 알리고 계속할지 묻는 것
    rows: int = 0


def import_translation(project: Project, xlsx_path: Path) -> ImportResult:
    from openpyxl import load_workbook

    wb = load_workbook(xlsx_path, data_only=True)
    res = ImportResult(cards={})
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
    head = [c.value for c in ws[1]]
    missing_cols = [h for h in ("key", "원문", "번역", "글자 크기") if h not in head]
    if missing_cols:
        res.blocking.append(f"열이 없습니다: {', '.join(missing_cols)}")
        return res
    ix = {h: head.index(h) for h in head if h}

    pdf = project.source_pdf.name
    extracted = load_extracted(project.root)
    kinds = {r.name: r.kind for r in project.template.regions}
    exported_pages = [int(p) for p in str(info.get("쪽") or "").split(",") if p.strip()]
    expected = {make_key(pdf, p, r) for p in exported_pages for r in kinds}
    seen: set[str] = set()
    for r in ws.iter_rows(min_row=2, values_only=True):
        key = r[ix["key"]]
        if not key:
            continue
        key = str(key).strip()
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
        orig, _ = source_of(project, extracted, page, region)
        if orig is not None and src is not None and src != orig:
            res.issues.append(f"원문을 고친 행(기록만 함): {key}")
        res.cards.setdefault(page, {"page": page})[region] = entry
    for k in sorted(expected - seen):
        res.issues.append(f"엑셀에 없는 행(한글을 넣지 않음): {k}")
    return res
