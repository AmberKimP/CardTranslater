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
        ("1차/카드/cards p1", "본문", 1, 1, "찾음"), ("1차/카드/cards p2", "본문", 1, 1, "찾음")]

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


def test_locate_ignores_card_frame_and_divider():
    """영역 안에 카드 테두리(가장자리 큰 덩어리)와 가로 구분선·◆ 장식이 있어도 줄을 바르게 센다."""
    import cv2

    img = np.full((300, 600, 3), (245, 225, 200), np.uint8)
    cv2.rectangle(img, (0, 0), (599, 299), (30, 30, 30), 12)  # 카드 테두리
    for y in (40, 100):  # 글자 줄 2개 (네모 글자)
        for k in range(8):
            cv2.rectangle(img, (60 + k * 50, y), (95 + k * 50, y + 35), (40, 40, 40), 4)
    cv2.circle(img, (300, 177), 20, (40, 40, 40), -1)  # 셋째 줄: 아이콘 + 글자
    cv2.rectangle(img, (335, 162), (365, 192), (40, 40, 40), 4)
    cv2.line(img, (60, 225), (280, 225), (40, 40, 40), 4)  # 구분선과 가운데 장식
    cv2.line(img, (320, 225), (540, 225), (40, 40, 40), 4)
    cv2.rectangle(img, (295, 218), (305, 232), (40, 40, 40), -1)
    for k in range(6):  # 넷째 줄 (대사)
        cv2.rectangle(img, (100 + k * 40, 250), (128 + k * 40, 276), (40, 40, 40), 3)
    text = "가나다라마바사아\n가나다라마바사아\n[[?]]＋\n가나다라마바"
    box, why = icons.locate(img, text, 0)
    assert box is not None, why
    assert 275 <= box[0] <= 285 and 315 <= box[2] <= 325 and 150 <= box[1] <= 160
    rgba = icons.cut_out(img, box)
    assert rgba[0, 0, 3] == 0 and rgba[rgba.shape[0] // 2, rgba.shape[1] // 2, 3] == 255  # 바깥은 투명, 안은 그대로


def test_line_cells_skip_spaces_and_small_marks_and_count_icon_as_one():
    """칸 세기: 공백·작은 문장부호는 칸이 아니고 [[아이콘]] 토큰은 한 칸이다 (위치 찾기 기준)."""
    assert icons._line_cells("AB、 C。[[coin]]D…") == ["A", "B", "C", "■", "D"]
    assert icons._token_cell_index("AB、C", "AB、C") == 3
    assert "、" in icons.SMALL_MARKS and "A" not in icons.SMALL_MARKS


def test_text_components_separates_rules_and_drops_card_frame():
    """덩어리 찾기: 위·아래 가장자리에 닿은 조각은 버리고, 얇고 긴 가로선은 구분선으로 따로 돌려준다."""
    ink = np.zeros((100, 300), dtype=bool)
    ink[40:56, 20:30] = True      # 글자 크기 덩어리
    ink[40:56, 60:70] = True
    ink[70:72, 10:250] = True     # 구분선
    ink[0:30, 150:160] = True     # 위 가장자리에 닿음 (그림 조각)
    comps, rules = icons._text_components(ink)
    assert sorted(c[0] for c in comps) == [20, 60]
    assert rules == [(70, 72)]


def test_locate_banner_edge_and_dot_do_not_glue_glyph_to_icon():
    """띠 테두리 선이 글자 밑동에 닿고, 글자와 아이콘 사이에 작은 점이 있어도 앞 글자가 아이콘에 붙지 않는다
    (마족의 맹약 던전 20쪽 ‘…なら[[?]]+2’), 줄 왼쪽 끝의 카드 테두리 조각은 칸으로 세지 않는다(캐릭터 4쪽)."""
    import cv2

    img = np.full((220, 900, 3), (245, 235, 220), np.uint8)
    cv2.rectangle(img, (10, 60), (16, 120), (40, 40, 40), -1)  # 왼쪽 끝 카드 테두리 조각 (가늘고 글자만큼 높음)
    for k in range(5):  # 글자 5개
        cv2.rectangle(img, (120 + k * 70, 60), (170 + k * 70, 120), (40, 40, 40), 5)
    cv2.circle(img, (482, 116), 3, (40, 40, 40), -1)  # 글자와 아이콘 사이 작은 점
    cv2.rectangle(img, (490, 52), (580, 128), (40, 40, 40), 6)  # 아이콘 (카드 두 장 흉내)
    cv2.rectangle(img, (520, 46), (600, 124), (40, 40, 40), 6)
    cv2.line(img, (100, 121), (700, 121), (40, 40, 40), 4)  # 띠 아래 테두리: 글자·아이콘 밑동에 닿음
    cv2.line(img, (620, 90), (650, 90), (40, 40, 40), 5)  # +
    cv2.line(img, (635, 75), (635, 105), (40, 40, 40), 5)
    cv2.rectangle(img, (670, 65), (690, 118), (40, 40, 40), -1)  # 2
    box, why = icons.locate(img, "가나다라마[[?]]+2", 0)
    assert box is not None, why
    assert 455 < box[0] <= 495 and 595 <= box[2] <= 606, box  # 앞 글자(마, 오른쪽 끝 450)가 섞이지 않은 아이콘만


def test_tiles_differing_only_in_digit_are_different_icons():
    import cv2

    def tile(d):
        img = np.full((90, 70, 4), 0, np.uint8)
        cv2.rectangle(img, (2, 2), (67, 87), (150, 150, 140, 255), -1)
        cv2.rectangle(img, (2, 2), (67, 87), (90, 90, 80, 255), 1)
        cv2.putText(img, d, (18, 68), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (40, 40, 40, 255), 6)
        return img

    a, b, a2 = icons.shape_pair(tile("1")), icons.shape_pair(tile("2")), icons.shape_pair(tile("1"))
    assert icons.same_icon(a, a2) > 0.95
    assert icons.same_icon(a, b) == 0.0


def test_same_frame_different_content_or_number_are_different_icons():
    """테두리·바탕이 같고 안의 내용(동전 숫자 3·5, 상자 안 동전·병)만 다르면 다른 아이콘. 크기만 다르면 같은 아이콘."""
    import cv2

    def coin(d, size=120):
        img = np.zeros((size, size, 4), np.uint8)
        c = size // 2
        cv2.circle(img, (c, c), int(size * 0.46), (200, 160, 60, 255), -1)
        cv2.circle(img, (c, c), int(size * 0.46), (120, 90, 30, 255), max(2, size // 30))
        cv2.putText(img, d, (int(size * 0.3), int(size * 0.68)), cv2.FONT_HERSHEY_SIMPLEX, size / 50, (90, 60, 20, 255),
                    max(2, size // 30))
        return img

    def crate(inner, size=120):
        img = np.zeros((size, size, 4), np.uint8)
        cv2.rectangle(img, (0, 0), (size - 1, size - 1), (120, 70, 40, 255), -1)
        cv2.rectangle(img, (0, 0), (size - 1, size - 1), (60, 30, 20, 255), max(2, size // 25))
        c, r = size // 2, size // 4
        if inner == "coin":
            cv2.circle(img, (c, c), r, (200, 170, 60, 255), -1)
        else:  # 병: 좁은 목 + 둥근 몸통
            cv2.rectangle(img, (c - r // 4, c - r), (c + r // 4, c - r // 3), (230, 200, 200, 255), -1)
            cv2.circle(img, (c, c + r // 3), int(r * 0.7), (220, 60, 80, 255), -1)
        return img

    c3, c3s, c5 = icons.shape_pair(coin("1")), icons.shape_pair(coin("1", 60)), icons.shape_pair(coin("8"))
    assert icons.same_icon(c3, c3s) > 0  # 크기만 다름
    assert icons.same_icon(c3, c5) == 0.0  # 숫자가 다름
    gold, red = icons.shape_pair(crate("coin")), icons.shape_pair(crate("potion"))
    assert icons.same_icon(gold, icons.shape_pair(crate("coin", 80))) > 0
    assert icons.same_icon(gold, red) == 0.0


def test_manual_box_for_not_found_icon_regroups_and_shows_guess(tmp_path, monkeypatch):
    """위치를 못 찾은 아이콘: 카드 보기에 예상 위치를 주고, 사람이 잡은 자리로 잘라 같은 모양 묶음에 넣는다."""
    info, fd = _project(tmp_path, "1차")
    real = icons.locate
    calls = {"n": 0}

    def flaky(img, text, index):  # 둘째 카드만 못 찾은 것처럼
        calls["n"] += 1
        return (None, "줄 수가 다름") if calls["n"] == 2 else real(img, text, index)

    monkeypatch.setattr(icons, "locate", flaky)
    service.find_icons(info.dir, suggest=False)
    monkeypatch.setattr(icons, "locate", real)
    review = service.icon_review(info.dir)
    lost = next(g for g in review["groups"] if not g["image"])
    found = next(g for g in review["groups"] if g["image"])
    occ = lost["where"][0]["occ"]
    p = service.icon_place(info.dir, occ)
    assert p["box"] is None and p["guess"] is not None and p["png"][:4] == b"\x89PNG"
    good = service.icon_place(info.dir, found["where"][0]["occ"])["box"]
    gx = (p["guess"][0] + p["guess"][2]) / 2
    assert good[0] - 40 <= gx <= good[2] + 40  # 예상 위치가 실제 아이콘 근처
    with pytest.raises(ValueError):
        service.set_icon_box(info.dir, occ, (10, 10, 12, 12))
    r = service.set_icon_box(info.dir, occ, good)
    assert r["group"] == found["group"] and r["old_gone"] and not r["new_group"]
    review = service.icon_review(info.dir)
    assert len(review["groups"]) == 1 and review["groups"][0]["count"] == 2
    assert service.icon_place(info.dir, occ)["box"] == list(good)
    res = service.confirm_icons(info.dir, [{"group": review["groups"][0]["group"], "name": "coin", "ko": "금화"}])
    assert res.state == "confirmed" and res.replaced == 2
    assert _source(fd, 1) == _source(fd, 2) == "AB[[coin]]CD"


def test_manual_box_new_row_gets_name_suggestion(tmp_path, monkeypatch):
    """직접 잡아 새 줄이 생기면 그 줄만 이름 제안을 받는다 (전에는 이름이 비어 있었다)."""
    info, _fd = _project(tmp_path, "1차")
    real = icons.locate
    calls = {"n": 0}

    def flaky(img, text, index):
        calls["n"] += 1
        return (None, "줄 수가 다름") if calls["n"] == 2 else real(img, text, index)

    monkeypatch.setattr(icons, "locate", flaky)
    service.find_icons(info.dir, suggest=False)
    monkeypatch.setattr(icons, "locate", real)
    review = service.icon_review(info.dir)
    lost = next(g for g in review["groups"] if not g["image"])
    good = service.icon_place(info.dir, next(g for g in review["groups"] if g["image"])["where"][0]["occ"])["box"]
    # 아이콘 왼쪽 글자(AB)를 잘못 감쌈 -> 모양이 달라 새 줄
    wrong = (max(0, good[0] - 3 * (good[2] - good[0])), good[1], good[0] - 2, good[3])
    r = service.set_icon_box(info.dir, lost["where"][0]["occ"], wrong)
    assert r["new_group"]
    run = FakeRunner([_suggest([{"group": r["group"], "name": "Coin", "meaning": "coin", "ko": "동전"}])])
    s = service.suggest_icon_name(info.dir, r["group"], client=ClaudeCodeClient(exe="claude", runner=run))
    assert s["suggest"] == "coin" and s["ko"] == "동전"
    g = next(x for x in service.icon_review(info.dir)["groups"] if x["group"] == r["group"])
    assert g["suggest"] == "coin" and g["ko"] == "동전"
    # 잘못 잡은 자리를 다시 고치면 같은 모양 줄로 합쳐지고 제안 받은 새 줄은 없어진다
    r2 = service.set_icon_box(info.dir, lost["where"][0]["occ"], good)
    assert r2["old_gone"] and not r2["new_group"]
    assert len(service.icon_review(info.dir)["groups"]) == 1


def test_text_line_with_background_drawing_is_not_dropped():
    """글자 줄 밑에 배경 그림(집 그림 선)이 붙어 띠가 커져도 그 줄을 빼지 않는다.
    (빼면 원문 줄과 이미지 줄이 한 줄씩 밀려 아이콘이 윗줄 그림으로 잘린다: 이세길 A-093 본문2)"""
    label = [(100 + 70 * i, 10, 60, 70, 3000) for i in range(5)]
    tile = [(300, 150, 140, 196, 25000)]  # 아이콘만 있는 줄 (속이 꽉 찬 큰 타일)
    line = [(100 + 60 * i, 400, 50, 52, 1500) for i in range(8)] + [(600, 393, 86, 92, 4600)]
    last = [(300 + 60 * i, 508, 50, 90, 1500) for i in range(5)]
    junk = [(10 + 55 * i, 560 + (i % 5) * 20, 22, 79, 100) for i in range(20)]  # 가늘고 속이 빈 그림 선
    junk += [(30 + 57 * i, 600 + (i % 7) * 10, 10, 10, 30) for i in range(20)]
    comps = label + tile + line + last + junk
    bands = [[0, 90], [150, 346], [393, 485], [508, 674]]
    out = icons._drop_sparse(bands, comps)
    assert len(out) == 4 and list(out[3]) == [508, 598]  # 글자 범위로 줄여 살린다
    # 그림 조각만 있는 띠는 여전히 뺀다
    assert len(icons._drop_sparse(bands[:3] + [[700, 800]], comps + [(5 + 60 * i, 700, 20, 90, 200) for i in range(20)])) == 3


def test_edge_scraps_removed_so_side_by_side_icons_pair_in_order():
    """줄 끝에 붙은 배경 그림 조각을 빼면 칸 수가 원문과 맞아, 붙어 있는 두 아이콘도 순서대로 짝짓는다
    (이세길 A-097 ‘すべてのプレイヤーは[검][1 타일]を得る’: 전에는 위치 못 찾음 / 두 아이콘이 한 그림으로)."""
    glyphs = [(111 + 58 * i, 700, 160 + 58 * i, 750) for i in range(10)]
    icons2 = [(710, 681, 802, 773), (817, 679, 887, 775)]
    tail = [(902, 701, 948, 752), (957, 700, 1013, 754), (1021, 704, 1076, 834)]
    cells = [(0, 731, 23, 735)] + glyphs + icons2 + tail + [(1120, 767, 1124, 806), (1163, 718, 1185, 780)]
    out = icons._trim_edge_scraps(cells, 15, 54.0)
    assert len(out) == 15 and out[10:12] == icons2
    # 글자 바로 옆의 작은 칸(ー 등)은 빼지 않는다 / 빼도 수가 안 맞으면 그대로
    near = glyphs + [(glyphs[-1][2] + 8, 722, glyphs[-1][2] + 58, 731)]
    assert icons._trim_edge_scraps(near, 10, 54.0) == near
    assert icons._trim_edge_scraps(cells, 14, 54.0) == cells


def test_band_trimmed_to_glyphs_when_drawing_hangs_below():
    glyph = [(100 + 60 * i, 680, 50, 95, 2500) for i in range(6)]
    junk = [(170, 740, 28, 108, 300), (1100, 770, 4, 39, 100)]  # 가늘고 속이 빈 그림 선
    assert icons._trim_to_core([[679, 848]], glyph + junk) == [[679, 782]]
    assert icons._trim_to_core([[679, 776]], glyph) == [[679, 776]]  # 붙은 그림이 없으면 그대로


def test_flat_icons_group_by_shape_and_color_not_scan_noise():
    """안이 밋밋한 금화는 스캔마다 밝기 무늬가 달라도 같은 아이콘 (엘프 금화). 숫자가 붙은 금화·다른 색은 다르다."""
    import numpy as np

    def coin(color, seed, digit=False, size=60):
        rng = np.random.default_rng(seed)
        a = np.zeros((size, size + (35 if digit else 0), 4), np.uint8)
        yy, xx = np.mgrid[:size, :a.shape[1]]
        disk = (yy - size / 2) ** 2 + (xx - size / 2) ** 2 < (size / 2 - 2) ** 2
        a[disk, :3] = np.clip(np.array(color) + rng.normal(0, 4, (disk.sum(), 3)), 0, 255)  # 스캔 잡음
        a[disk, 3] = 255
        if digit:
            a[15:45, size + 10:size + 25, :3] = 20
            a[15:45, size + 10:size + 25, 3] = 255
        return a

    gold = (245, 214, 80)
    a, b = icons.shape_pair(coin(gold, 1)), icons.shape_pair(coin(gold, 2))
    assert a[4] < icons.FLAT_INNER_STD and icons.same_icon(a, b) >= icons.SAME_SHAPE
    assert icons.same_icon(a, icons.shape_pair(coin(gold, 3, digit=True))) == 0.0  # 금화 2
    assert icons.same_icon(a, icons.shape_pair(coin((200, 200, 205), 4))) == 0.0  # 은화


def test_placeholder_runs():
    assert icons.placeholder_runs("[[?]][[?]][[?]]の山札") == [[0, 1, 2]]
    assert icons.placeholder_runs("A[[?]] [[?]]B[[?]]") == [[0, 1], [2]]
    assert icons.placeholder_runs("[[?]][[coin]][[?]]") == [[0], [1]]  # 이름 붙은 아이콘이 끼면 끊긴다
    assert icons.placeholder_runs("") == []


def _add_run_partner(info, fd):
    """1쪽 원문을 [[?]] 둘이 붙은 것으로 바꾸고, 둘째 자리를 ‘위치 못 찾음’으로 scan.json에 넣는다."""
    service.find_icons(info.dir, suggest=False)
    p = fd / "extract" / "pages" / "page-001.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["regions"]["본문"]["source"] = "AB[[?]][[?]]CD"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    sp = info.game_dir / "work" / "icons" / "scan.json"
    scan = json.loads(sp.read_text(encoding="utf-8"))
    host = next(o for o in scan["occurrences"] if o["page"] == 1)
    extra = dict(host, id="o099", index=1, png="", box=[], manual=False, reason="예상 자리 근처에 아이콘 크기의 덩어리가 없음",
                 group="위치 못 찾음 o099", order=host.get("order", 1) + 1)
    scan["occurrences"].append(extra)
    scan["groups"]["위치 못 찾음 o099"] = {"rep": "", "count": 1}
    sp.write_text(json.dumps(scan, ensure_ascii=False), encoding="utf-8")
    return host["id"], "o099"


def test_absorb_adjacent_unfound_icon_into_boxed_one(tmp_path):
    """문양 여러 개가 붙은 아이콘: 상자 하나가 붙은 [[?]] 자리까지 감싸면, 나머지 자리는 그 아이콘에 포함되어 원문에서 빠진다."""
    info, fd = _project(tmp_path, "run", pages=1)
    host, extra = _add_run_partner(info, fd)
    work = info.game_dir / "work" / "icons"
    assert icons.run_partners(info, work, host) == {"boxed": [], "unfound": [extra]}
    assert icons.run_partners(info, work, extra) == {"boxed": [host], "unfound": []}
    assert service.icon_place(info.dir, extra)["run_host"] == [host]
    assert service.icon_place(info.dir, host)["run_host"] == []  # 자리가 있는 쪽은 아님
    service.absorb_icon_run(info.dir, host, [extra])
    rev = service.icon_review(info.dir)
    by_occ = {w["occ"]: g for g in rev["groups"] for w in g["where"]}
    assert by_occ[extra]["not_icon"] and by_occ[extra]["absorbed_group"] == by_occ[host]["group"]
    assert icons.run_partners(info, work, host)["unfound"] == []  # 이미 포함한 자리는 다시 묻지 않는다
    rows = [{"group": g["group"], "name": "" if g["not_icon"] else "lily_3", "meaning": "", "ko": "백합 셋",
             "not_icon": g["not_icon"], "text": "", "scale": 1.1} for g in rev["groups"]]
    r = service.confirm_icons(info.dir, rows)
    assert r.state == "confirmed", r.blocking
    assert _source(fd, 1) == "AB[[lily_3]]CD"


def _wide(box, n=3):
    """아이콘 상자를 아이콘 n개가 옆으로 늘어선 세트 모양으로 넓힌다."""
    x0, y0, x1, y1 = box
    return (x0, y0, x0 + (y1 - y0) * n, y1)


def test_scan_does_not_set_neighbours_wrongly_given_one_icon_box(tmp_path, monkeypatch):
    """이웃 [[?]]가 아이콘 하나 크기의 같은 자리로 잡힌 것(찾기 실수)은 세트로 합치지 않는다."""
    info, fd = _project(tmp_path, "noset", pages=1)
    p = fd / "extract" / "pages" / "page-001.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["regions"]["본문"]["source"] = "AB[[?]][[?]]CD"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    real = icons.locate
    monkeypatch.setattr(icons, "locate", lambda img, text, index: real(img, "AB[[?]]CD", 0))
    service.find_icons(info.dir, suggest=False)
    assert not any(o.set_of for o in icons.load_scan(info.game_dir / "work" / "icons").occurrences)


def test_scan_treats_touching_icons_found_as_one_blob_as_a_set(tmp_path, monkeypatch):
    """붙어 있는 [[?]] 둘을 그림에서 한 덩어리로 찾으면 세트: 첫 자리만 표에 보이고, 확정하면 [[이름]] 하나가 된다.
    첫 자리를 다시 잡으면 세트가 풀려 나머지 자리가 ‘위치 못 찾음’으로 돌아온다."""
    info, fd = _project(tmp_path, "set", pages=1)
    p = fd / "extract" / "pages" / "page-001.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["regions"]["본문"]["source"] = "AB[[?]][[?]]CD"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    real = icons.locate
    first = {}

    def same_blob(img, text, index):
        box, why = real(img, "AB[[?]]CD", 0)  # 두 자리 모두 그림의 그 아이콘 덩어리 (가로로 긴 세트 모양으로)
        first.setdefault("box", box)
        return _wide(box), why

    monkeypatch.setattr(icons, "locate", same_blob)
    service.find_icons(info.dir, suggest=False)
    monkeypatch.setattr(icons, "locate", real)
    assert first["box"] is not None
    rev = service.icon_review(info.dir)
    assert len(rev["groups"]) == 1 and rev["groups"][0]["set_size"] == 2
    work = info.game_dir / "work" / "icons"
    scan = icons.load_scan(work)
    extra = next(o for o in scan.occurrences if o.set_of)
    assert scan.groups[extra.group]["absorbed"] and scan.groups[extra.group]["auto"]
    host = next(o for o in scan.occurrences if not o.set_of)
    assert service.icon_place(info.dir, host.id)["marks"] == []  # 세트 나머지는 따로 표시하지 않는다
    rows = [{"group": g["group"], "name": "lily_2", "meaning": "", "ko": "백합 둘", "not_icon": False, "text": "",
             "scale": 1.1} for g in rev["groups"]]
    # 확정 전에 다시 잡으면 세트가 풀린다
    service.set_icon_box(info.dir, host.id, host.box)
    scan2 = icons.load_scan(work)
    back = next(o for o in scan2.occurrences if o.id == extra.id)
    assert not back.set_of and back.group.startswith("위치 못 찾음")
    # 다시 세트로 합치고 확정
    r = service.set_icon_box(info.dir, host.id, host.box)
    assert extra.id in r["run_unfound"]
    service.absorb_icon_run(info.dir, host.id, r["run_unfound"])
    rev = service.icon_review(info.dir)
    rows = [{"group": g["group"], "name": "" if g["not_icon"] else "lily_2", "meaning": "", "ko": "백합 둘",
             "not_icon": g["not_icon"], "text": "", "scale": 1.1} for g in rev["groups"]]
    res = service.confirm_icons(info.dir, rows)
    assert res.state == "confirmed", res.blocking
    assert _source(fd, 1) == "AB[[lily_2]]CD"


def test_scan_auto_set_confirms_without_rows_for_hidden_spots(tmp_path, monkeypatch):
    """자동 세트의 나머지 자리는 표에 행이 없어도 확정할 때 원문에서 빠진다."""
    info, fd = _project(tmp_path, "set2", pages=1)
    p = fd / "extract" / "pages" / "page-001.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["regions"]["본문"]["source"] = "AB[[?]][[?]][[?]]CD"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    real = icons.locate
    monkeypatch.setattr(icons, "locate", lambda img, text, index: (_wide(real(img, "AB[[?]]CD", 0)[0]), ""))
    service.find_icons(info.dir, suggest=False)
    monkeypatch.setattr(icons, "locate", real)
    rev = service.icon_review(info.dir)
    assert [g["set_size"] for g in rev["groups"]] == [3]
    rows = [{"group": g["group"], "name": "lily_3", "meaning": "", "ko": "백합 셋", "not_icon": False, "text": "",
             "scale": 1.1} for g in rev["groups"]]
    res = service.confirm_icons(info.dir, rows)
    assert res.state == "confirmed", res.blocking
    assert _source(fd, 1) == "AB[[lily_3]]CD"


def test_scan_set_stops_at_first_spot_that_does_not_fit(tmp_path, monkeypatch):
    """세트 모양(아이콘 2개 폭)에 [[?]]가 셋이면 둘만 세트, 셋째는 같은 그림으로 자르지 않고 ‘위치 못 찾음’."""
    info, fd = _project(tmp_path, "part", pages=1)
    p = fd / "extract" / "pages" / "page-001.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["regions"]["본문"]["source"] = "AB[[?]][[?]][[?]]CD"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    real = icons.locate
    monkeypatch.setattr(icons, "locate", lambda img, text, index: (_wide(real(img, "AB[[?]]CD", 0)[0], 2), ""))
    service.find_icons(info.dir, suggest=False)
    scan = icons.load_scan(info.game_dir / "work" / "icons")
    by_index = {o.index: o for o in scan.occurrences}
    assert by_index[1].set_of == by_index[0].id
    assert not by_index[2].set_of and not by_index[2].png and by_index[2].group.startswith("위치 못 찾음")


def test_rebox_host_releases_manual_absorbed_spot(tmp_path):
    """손으로 합친 자리도 첫 자리를 다시 잡으면 풀린다 (저장할 때 다시 묻는다)."""
    info, fd = _project(tmp_path, "rel", pages=1)
    host, extra = _add_run_partner(info, fd)
    service.absorb_icon_run(info.dir, host, [extra])
    box = service.icon_place(info.dir, host)["box"]
    r = service.set_icon_box(info.dir, host, box)
    assert extra in r["run_unfound"]
    g = next(g for g in service.icon_review(info.dir)["groups"] if g["where"][0]["occ"] == extra)
    assert not g["not_icon"] and not g["absorbed_group"]
