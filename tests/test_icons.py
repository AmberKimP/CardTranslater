"""아이콘 찾기·사전 (PRD 5.6). 가짜 카드 PDF와 가짜 모델 응답만 쓴다 (실제 호출 없음)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from openpyxl import load_workbook

from app import icons, service, workspace
from app.extract_cc import ClaudeCodeClient
from tests.test_extract import FakeRunner

GAEGU = Path(__file__).resolve().parent.parent / "fonts" / "gaegu" / "Gaegu-Regular.ttf"


def _card_pdf(path: Path, icon_color=(0.85, 0.1, 0.1), pages: int = 2) -> None:
    """한 줄 'AB ● CD' 카드. 동그라미가 아이콘."""
    doc = pymupdf.open()
    for _ in range(pages):
        pg = doc.new_page(width=250, height=180)
        pg.insert_text((40, 120), "AB", fontsize=12)
        pg.draw_circle((66, 116), 4.5, color=icon_color, fill=icon_color)
        pg.insert_text((76, 120), "CD", fontsize=12)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)


def _regions(path: Path) -> Path:
    path.write_text(json.dumps({"regions": [
        {"name": "본문", "kind": "text", "rect": {"x": 0.1, "y": 0.58, "w": 0.6, "h": 0.12}}]}), encoding="utf-8")
    return path


def _project(root: Path, name: str, pages: int = 2, icon_color=(0.85, 0.1, 0.1)):
    pdf = root / "src" / name / "cards.pdf"
    _card_pdf(pdf, icon_color, pages)
    info, _ = workspace.create_project(name, "G", root=root)
    workspace.add_files(info, [pdf], "카드", new_group=True, regions_from=_regions(root / f"{name}_regions.json"))
    fd = workspace.file_dirs(info)[0]
    d = fd / "extract" / "pages"
    d.mkdir(parents=True)
    for p in range(1, pages + 1):
        (d / f"page-{p:03d}.json").write_text(json.dumps({"page": p, "model": "m", "regions": {
            "본문": {"source": "AB[[?]]CD", "uncertain": False, "note": ""}}}), encoding="utf-8")
    return info, fd


def _source(fd: Path, page: int) -> str:
    return json.loads((fd / "extract" / "pages" / f"page-{page:03d}.json").read_text(encoding="utf-8"))[
        "regions"]["본문"]["source"]


def _save(ws, path):
    for s in ws.parent.worksheets:  # openpyxl은 불러온 그림을 다시 저장하지 못한다 (엑셀에서 고치면 문제없음)
        s._images = []
    ws.parent.save(path)


def _suggest(items):
    return (0, json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01,
                           "modelUsage": {}, "structured_output": {"items": items}}), "")


def test_replace_placeholders_by_index():
    t = "A[[?]]B[[coin]]C[[?]]"
    assert icons.replace_placeholders(t, {1: "heart"}) == "A[[?]]B[[coin]]C[[heart]]"
    assert icons.replace_placeholders(t, {0: "", 1: "heart"}) == "AB[[coin]]C[[heart]]"


def test_similarity_shape_and_color():
    a = np.zeros((40, 40, 4), np.uint8)
    a[10:30, 10:30] = (200, 20, 20, 255)
    b = a.copy()
    c = a.copy()
    c[10:30, 10:30, :3] = (20, 20, 200)  # 같은 모양, 다른 색
    d = np.zeros((40, 40, 4), np.uint8)
    d[18:22, 5:35] = (200, 20, 20, 255)  # 다른 모양
    sa = icons.shape_of(a)
    assert icons.similarity(sa, icons.shape_of(b)) > 0.95
    assert icons.similarity(sa, icons.shape_of(c)) == 0.0
    assert icons.similarity(sa, icons.shape_of(d)) < icons.SAME_SHAPE


def test_find_review_import_and_reuse_in_next_project(tmp_path):
    info, fd = _project(tmp_path, "1차")
    assert icons.pending_count(info) == 2
    run = FakeRunner([_suggest([{"group": "g01", "name": "Gold Coin", "meaning": "Gold", "ko": "금화"}])])
    r = service.find_icons(info.dir, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "icons_review" and r.groups == 1 and r.occurrences == 2 and r.not_found == 0
    args, cwd = run.calls[0]
    assert args[args.index("--tools") + 1] == "Read" and list(cwd.glob("o00*.png"))  # 그림을 열어 보게 함
    ws = load_workbook(r.review_xlsx)["아이콘"]
    head = [c.value for c in ws[1]]
    row = dict(zip(head, [c.value for c in ws[2]]))
    assert row["제안 이름"] == "gold_coin" and row["한국어 이름"] == "금화" and row["등장 횟수"] == 2
    assert len(ws._images) == 1

    # 이름 규칙을 어기면 아무것도 바꾸지 않는다
    ws.cell(2, head.index("이름") + 1).value = "Coin!"
    _save(ws, r.review_xlsx)
    bad = service.import_icons(info.dir, r.review_xlsx)
    assert bad.state == "blocked" and "영문 소문자" in bad.blocking[0] and _source(fd, 1) == "AB[[?]]CD"

    ws.cell(2, head.index("이름") + 1).value = "coin"
    _save(ws, r.review_xlsx)
    ok = service.import_icons(info.dir, r.review_xlsx)
    assert ok.state == "confirmed" and ok.added == ["coin"] and ok.replaced == 2
    assert _source(fd, 1) == _source(fd, 2) == "AB[[coin]]CD"
    d = icons.load_dictionary(info)["icons"]["coin"]
    assert d["ko"] == "금화" and d["meaning"] == "Gold" and d["scale"] == icons.DEFAULT_SCALE
    png = icons.load_png(icons.icons_dir(info) / "coin.png")
    assert png.shape[2] == 4 and png[0, 0, 3] == 0 and png[:, :, 3].max() == 255  # 배경 투명
    assert icons.pending_count(info) == 0

    # 같은 게임의 다음 프로젝트: 사전과 모양이 같으면 모델 없이 바로 바꾼다
    info2, fd2 = _project(tmp_path, "2차", pages=1)
    run2 = FakeRunner([])
    r2 = service.find_icons(info2.dir, client=ClaudeCodeClient(exe="claude", runner=run2))
    assert r2.state == "done" and r2.auto == {"coin": 1} and run2.calls == []
    assert _source(fd2, 1) == "AB[[coin]]CD"
    # 합성은 게임 사전 폴더와 아이콘별 크기를 쓴다
    st = icons.style_for_compose(fd2)
    assert Path(st["icons_dir"]) == icons.icons_dir(info2) and st["icon_scales"] == {"coin": icons.DEFAULT_SCALE}


def test_different_color_is_a_new_icon_and_not_icon_is_removed(tmp_path):
    info, fd = _project(tmp_path, "1차", pages=1)
    r = service.find_icons(info.dir, suggest=False)
    ws = load_workbook(r.review_xlsx)["아이콘"]
    head = [c.value for c in ws[1]]
    ws.cell(2, head.index("이름") + 1).value = "red"
    _save(ws, r.review_xlsx)
    assert service.import_icons(info.dir, r.review_xlsx).state == "confirmed"

    info2, fd2 = _project(tmp_path, "2차", pages=1, icon_color=(0.1, 0.1, 0.85))
    r2 = service.find_icons(info2.dir, suggest=False)
    assert r2.state == "icons_review" and not r2.auto  # 파란 동그라미는 빨간 것과 다른 아이콘
    ws = load_workbook(r2.review_xlsx)["아이콘"]
    ws.cell(2, head.index("처리") + 1).value = icons.NOT_ICON
    _save(ws, r2.review_xlsx)
    res = service.import_icons(info2.dir, r2.review_xlsx)
    assert res.state == "confirmed" and res.removed == 1 and _source(fd2, 1) == "ABCD"


def test_finish_extraction_stops_for_unnamed_icons(tmp_path):
    info, _fd = _project(tmp_path, "1차", pages=2)
    r = service.finish_extraction(info.dir, client=ClaudeCodeClient(exe="claude", runner=FakeRunner([_suggest([])])))
    assert r.state == "icons_review" and r.review_xlsx.name == "아이콘_검증.xlsx" and "1개" in r.reason


@pytest.mark.skipif(not GAEGU.exists(), reason="fonts/ 없음")
def test_typeset_refuses_unnamed_icon():
    from app.typeset import TextStyle, typeset_region

    page = pymupdf.open().new_page(width=200, height=100)
    r = typeset_region(page, (10, 10, 190, 60), "너도 [[?]]", TextStyle(font_file=str(GAEGU), size_pt=8))
    assert "아이콘 오류" in r.status and "이름 없는 아이콘" in r.errors[0] and not r.drawn


def test_translate_prompt_gives_icon_meaning():
    from app.translate import _icon_hint

    hint = _icon_hint([("Pay 2 [[coin]]", "본문", "text")], {"coin": {"meaning": "Gold", "ko": "금화"}})
    assert '"coin"' in hint and "금화" in hint
    assert _icon_hint([("no icon", "본문", "text")], {"coin": {}}) == ""


def _rows(ws):
    head = [c.value for c in ws[1]]
    return [dict(zip(head, r)) for r in ws.iter_rows(min_row=2, values_only=True)]


def test_exports_list_icon_positions_groups_and_fonts(tmp_path):
    info, fd = _project(tmp_path, "1차", pages=2)
    # 아이콘 검증 엑셀: 모든 등장 위치 (쪽·영역·줄·순서)
    r = service.find_icons(info.dir, suggest=False)
    loc = _rows(load_workbook(r.review_xlsx)["위치"])
    assert [(x["카드"], x["영역"], x["줄"], x["줄에서 순서"], x["위치 찾기"]) for x in loc] == [
        ("카드/cards p1", "본문", 1, 1, "찾음"), ("카드/cards p2", "본문", 1, 1, "찾음")]

    # 번역 검수 엑셀: 이름 없는 아이콘은 확인 목록에, 행마다 그룹·글꼴
    x = service.export_excel(fd)
    wb = load_workbook(x)
    ic = _rows(wb["아이콘"])
    assert [(i["카드"], i["영역"], i["줄"], i["줄에서 순서"], i["아이콘"]) for i in ic] == [
        ("cards.pdf p1", "본문", 1, 1, "[[?]]"), ("cards.pdf p2", "본문", 1, 1, "[[?]]")]
    assert all("이름 없음" in i["상태"] for i in ic)
    tr = _rows(wb["번역"])
    assert {t["그룹"] for t in tr} == {"카드"} and tr[0]["글꼴"].startswith("Noto Sans KR")
    fonts = _rows(wb["글꼴"])
    assert fonts[0]["그룹"] == "카드" and fonts[0]["영역"] == "본문" and fonts[0]["글꼴 파일"].endswith(".ttf")
    info_sheet = {r[0]: r[1] for r in wb["정보"].iter_rows(values_only=True)}
    assert info_sheet["그룹"] == "카드" and info_sheet["확인할 아이콘"] == 2

    # 확정 뒤: 사전 그림이 붙고, 다음 프로젝트에서 자동으로 맞춘 것은 ‘확인 권장’
    ws = load_workbook(r.review_xlsx)["아이콘"]
    head = [c.value for c in ws[1]]
    ws.cell(2, head.index("이름") + 1).value = "coin"
    _save(ws, r.review_xlsx)
    service.import_icons(info.dir, r.review_xlsx)
    wb = load_workbook(service.export_excel(fd, force=True))
    assert {i["상태"] for i in _rows(wb["아이콘"])} == {"사전 확인됨"} and len(wb["아이콘"]._images) == 2
    info2, fd2 = _project(tmp_path, "2차", pages=1)
    service.find_icons(info2.dir, suggest=False)
    ic2 = _rows(load_workbook(service.export_excel(fd2))["아이콘"])
    assert ic2[0]["아이콘"] == "[[coin]]" and "자동으로 맞춤" in ic2[0]["상태"]
