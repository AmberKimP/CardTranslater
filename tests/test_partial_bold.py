"""문장 일부만 굵은(기울인·다른 색) 카드: 한글도 태그로 감싼 부분만 서식이 바뀐다 (PRD 5.7, P0 완료 기준).

결과 PDF의 글자(span)에서 직접 확인한다: 태그 안 글자만 굵은 글꼴 파일(또는 가짜 굵게 획)이고
나머지는 보통 글꼴이다. 자동 줄바꿈·사람이 넣은 줄바꿈을 걸친 태그, 깨진 태그(‘서식 불일치’)도 본다.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pymupdf
import pytest

from app.typeset import TextStyle, typeset_region
from app.typeset_compose import compose_layout_region
from app.typeset_layout import (STATUS_TAG_MISMATCH, check_tags, close_tags_per_line, parse_markup, soft_break,
                                two_line_sentences)

ROOT = Path(__file__).resolve().parent.parent
NOTO = ROOT / "fonts/notosanskr/NotoSansKR-Regular.ttf"
DOKDO = ROOT / "fonts/dokdo/Dokdo-Regular.ttf"  # 굵기 파일이 하나뿐: 가짜 굵게(획 덧그리기)
needs_noto = pytest.mark.skipif(not (NOTO.exists() and NOTO.with_name("NotoSansKR-Bold.ttf").exists()),
                                reason="fonts/ 없음 (scripts/fetch_fonts.py)")


def _hangul(s: str) -> str:
    return "".join(c for c in s if not c.isspace())


def bold_and_regular(page: pymupdf.Page) -> tuple[str, str]:
    """페이지 글자를 (굵은 글꼴로 그린 글자, 보통 글꼴로 그린 글자)로 나눈다 (공백 제외, 읽는 순서)."""
    bold, regular = [], []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            for sp in ln["spans"]:
                (bold if "Bold" in sp["font"] or "Black" in sp["font"] else regular).append(_hangul(sp["text"]))
    return "".join(bold), "".join(regular)


# ---------------------------------------------------------------- 순수 함수: 줄마다 태그 짝 맞추기
def test_close_tags_per_line_reopens_tags_on_next_line():
    assert close_tags_per_line("뒤 <b>은행에서\n금화를 받고</b> 쉰다.") == "뒤 <b>은행에서</b>\n<b>금화를 받고</b> 쉰다."
    # 겹친 태그: 거꾸로 닫고 같은 순서로 다시 연다
    assert close_tags_per_line("<b>가 <c1>나\n다</c1> 라</b>") == "<b>가 <c1>나</c1></b>\n<b><c1>다</c1> 라</b>"
    # 세 줄에 걸치면 가운데 줄은 통째로 감싼다. 빈 줄은 그대로
    assert close_tags_per_line("<i>가\n\n나\n다</i>") == "<i>가</i>\n\n<i>나</i>\n<i>다</i>"
    for out in (close_tags_per_line("<b>가 <c1>나\n다</c1> 라</b>"), close_tags_per_line("<i>가\n\n나\n다</i>")):
        assert all(not parse_markup(line)[1] for line in out.split("\n"))


def test_close_tags_per_line_keeps_plain_single_line_and_broken_text():
    assert close_tags_per_line("") == ""
    assert close_tags_per_line("<b>한 줄</b>") == "<b>한 줄</b>"
    assert close_tags_per_line("태그 없음\n둘째 줄") == "태그 없음\n둘째 줄"
    broken = "<b>금화\n2개를 얻는다."
    assert close_tags_per_line(broken) == broken  # 원래 깨진 글: 그대로 두어 ‘서식 불일치’로 알린다
    # 앱이 붙인 글꼴 태그(fN)와 엇갈려도 줄마다 짝이 맞는다
    out = close_tags_per_line("<f1>가 <b>나</f1>\n<f1>다</b> 라</f1>")
    assert all(not parse_markup(line)[1] for line in out.split("\n"))


# ---------------------------------------------------------------- 영역 전체 배치 (텍스트 카드)
@needs_noto
def test_only_tagged_words_are_bold_across_automatic_wrap():
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=80)
    st = TextStyle(font_file=str(NOTO), size_pt=8)
    text = "카드 1장을 뽑은 다음, <b>손에서 카드 2장을 골라 버리고</b> 금화 1개를 얻는다. 그 뒤 차례를 마친다."
    res = typeset_region(page, pymupdf.Rect(10, 10, 150, 70), text, st, source_text="Draw, <b>discard two</b>, gain.")
    assert len(res.lines) >= 2 and not res.overflow_text and STATUS_TAG_MISMATCH not in res.status
    # 굵은 부분이 줄바꿈을 걸친다 (첫 줄 끝 ~ 둘째 줄 처음)
    first = res.lines[0]
    assert "손에서" in first and "버리고" not in first
    bold, regular = bold_and_regular(page)
    assert bold == _hangul("손에서 카드 2장을 골라 버리고")
    assert regular == _hangul("카드 1장을 뽑은 다음, 금화 1개를 얻는다. 그 뒤 차례를 마친다.")


@needs_noto
def test_bold_at_start_italic_and_color_only_on_tagged_part():
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=120)
    st = TextStyle(font_file=str(NOTO), size_pt=8, color="#101010", tag_colors={"c1": "#c02020"})
    typeset_region(page, pymupdf.Rect(10, 10, 190, 30), "<b>금화 2개</b>를 얻는다.", st)
    bold, regular = bold_and_regular(page)
    assert bold == "금화2개" and regular == "를얻는다."

    p2 = doc.new_page(width=200, height=60)
    typeset_region(p2, pymupdf.Rect(10, 10, 190, 30), "지금 <c1>금화 2개</c1>를 얻는다.", st)
    colors = {_hangul(sp["text"]): sp["color"] for b in p2.get_text("dict")["blocks"]
              for ln in b.get("lines", []) for sp in ln["spans"]}
    assert colors == {"지금": 0x101010, "금화2개": 0xC02020, "를얻는다.": 0x101010}
    assert bold_and_regular(p2)[0] == ""

    p3 = doc.new_page(width=200, height=60)
    typeset_region(p3, pymupdf.Rect(10, 10, 190, 30), "카드 1장을 뽑고 <i>휴식한다</i>.", st)
    sheared = [doc.xref_stream(x) for x in p3.get_contents()]
    sheared = [s for s in sheared if b" .21256 1 " in s]  # tan(12도) 기울임 행렬
    assert len(sheared) == 1  # 기울인 조각은 하나 (태그 안 글자만)
    assert bold_and_regular(p3)[0] == ""


@pytest.mark.skipif(not DOKDO.exists(), reason="fonts/ 없음")
def test_fake_bold_strokes_only_tagged_characters_when_font_has_no_bold_file():
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=60)
    typeset_region(page, pymupdf.Rect(10, 10, 190, 50), "카드를 <b>두 장</b> 뽑는다.", TextStyle(font_file=str(DOKDO), size_pt=10))
    stroked = "".join(chr(c[0]) for s in page.get_texttrace() if s["type"] == 1 for c in s["chars"])
    assert _hangul(stroked) == "두장"


@needs_noto
def test_broken_tag_pair_falls_back_to_base_style_and_is_reported():
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=60)
    res = typeset_region(page, pymupdf.Rect(10, 10, 190, 50), "<b>금화 2개를 얻는다.", TextStyle(font_file=str(NOTO), size_pt=8))
    assert STATUS_TAG_MISMATCH in res.status and any("닫히지 않은 태그" in e for e in res.errors)
    assert bold_and_regular(page) == ("", "금화2개를얻는다.")
    assert "<b>" not in page.get_text()


# ---------------------------------------------------------------- 원문 줄 자리 배치 (그림 카드)
def _lines(*ys):
    return {"lines": [{"bbox_pt": [20.0, y, 150.0, y + 8], "glyph_h_pt": 8.0, "color": "#303030", "stroke_color": None,
                       "stroke_pt": 0.0, "weight": 0.2, "bold": False, "align": "left", "accents": []} for y in ys],
            "dividers_pt": []}


@needs_noto
def test_line_slots_keep_bold_across_manual_line_break():
    """줄마다 따로 넣는 그림 카드: <b>가 사람이 넣은 줄바꿈을 걸쳐도 짝이 깨지지 않고 그 부분만 굵다.
    (전에는 두 줄 모두 짝이 깨져 ‘서식 불일치’가 되고 굵게가 사라졌다)"""
    doc = pymupdf.open()
    page = doc.new_page(width=180, height=110)
    text = "카드 1장을 뽑은 뒤 <b>은행에서\n금화를 받고</b> 푹 쉰다.\n지금 바로 차례를 마친다."
    rec = compose_layout_region(page, (10, 10, 170, 100), text, {"font": {"file": str(NOTO)}}, _lines(20, 32, 44),
                                source_text="Draw a card then\nfrom the <b>bank, rest</b> well.\nEnd your turn.")
    assert STATUS_TAG_MISMATCH not in rec["status"] and not rec["errors"], rec
    assert not rec["overflow_text"]
    bold, regular = bold_and_regular(page)
    assert bold == _hangul("은행에서 금화를 받고")
    assert regular == _hangul("카드 1장을 뽑은 뒤 푹 쉰다. 지금 바로 차례를 마친다.")


@needs_noto
def test_line_slots_broken_or_mismatched_tags_are_reported_for_the_whole_cell():
    doc = pymupdf.open()
    style = {"font": {"file": str(NOTO)}}
    page = doc.new_page(width=180, height=110)
    rec = compose_layout_region(page, (10, 10, 170, 100), "<b>금화를\n받는다.", style, _lines(20, 32))
    assert STATUS_TAG_MISMATCH in rec["status"] and any("닫히지 않은 태그" in e for e in rec["errors"])
    assert bold_and_regular(page)[0] == "" and "<b>" not in page.get_text()  # 깨진 칸: 기본 서식, 태그 글자 없음
    # 짝은 맞지만 원문과 태그 수가 다르다: 알리되 태그대로 그린다
    p2 = doc.new_page(width=180, height=110)
    rec2 = compose_layout_region(p2, (10, 10, 170, 100), "<b>금화</b>를\n받는다.", style, _lines(20, 32),
                                 source_text="Gain\ngold.")
    assert STATUS_TAG_MISMATCH in rec2["status"] and any("원문과 다름" in e for e in rec2["errors"])
    assert bold_and_regular(p2)[0] == "금화"


# ---------------------------------------------------------------- 줄 끝에 붙인 닫는 태그와 문장 끝 판단
def test_line_end_tags_added_per_line_do_not_hide_sentence_end():
    lines = close_tags_per_line("<b>첫 문장이다\n둘째 문장</b>\n셋째 문장이다.").split("\n")
    assert lines[0].endswith("</b>")  # 문장 끝(‘다’) 뒤에 닫는 태그가 붙는다
    assert two_line_sentences(lines) == [1]  # 태그 없이 쓴 것과 같다
    assert two_line_sentences(["첫 문장이다", "둘째 문장", "셋째 문장이다."]) == [1]
    assert not soft_break("<b>A다</b>", "<b>B</b>")  # 문장이 끝난 줄: 잇지 않는다
    assert not soft_break("<b>금화 [[gold]]</b>", "<b>받는다</b>")  # 아이콘으로 끝난 줄
    assert not soft_break("<b>끝.</b> <i></i>", "다음")
    assert soft_break("<b>은행에서</b>", "<b>금화를 받고</b>")  # 문장 중간: 잇는다


# ---------------------------------------------------------------- 원문은 줄마다, 번역은 여러 줄을 한 번에 감싼 태그
def test_tag_count_ignores_per_line_split_of_same_span():
    src = "<b>Receive gold</b>\n<b>from the bank</b>, then rest."
    assert check_tags("<b>은행에서\n금화를 받고</b> 쉰다.", src) == []
    assert check_tags("<b>은행에서</b>\n<b>금화를 받고</b> 쉰다.", "<b>Receive gold\nfrom the bank</b>.") == []
    assert check_tags("<b>가 <c1>나\n다</c1> 라</b>", "<b>A <c1>B</c1></b>\n<b><c1>C</c1> D</b>") == []
    # 정말 다른 것은 그대로 알린다: 한 줄 안의 두 덩어리, 빠진 태그, 다른 종류
    assert check_tags("<b>은행</b>에서 <b>금화</b>", "<b>Receive gold</b> from the bank")
    assert check_tags("은행에서\n금화를 받고", src)
    assert check_tags("<i>은행에서\n금화를 받고</i>", src)
    # 줄 사이에 글이 있으면 다른 덩어리
    assert check_tags("<b>공격:</b> 1\n<b>방어:</b> 2", "<b>Attack: 1\nDefend: 2</b>")


@needs_noto
def test_line_slots_no_false_mismatch_when_source_tags_each_line():
    doc = pymupdf.open()
    page = doc.new_page(width=180, height=110)
    rec = compose_layout_region(page, (10, 10, 170, 100), "<b>은행에서\n금화를 받고</b>",
                                {"font": {"file": str(NOTO)}}, _lines(20, 32),
                                source_text="<b>Receive gold</b>\n<b>from the bank</b>")
    assert STATUS_TAG_MISMATCH not in rec["status"] and not rec.get("errors")
    assert bold_and_regular(page)[0] == _hangul("은행에서금화를받고")


# ---------------------------------------------------------------- 실제 흐름: 지우기 -> 한글 넣기 -> 결과 PDF
def _image_card_project(tmp: Path, cases: list[tuple[str, str]]) -> Path:
    """글자가 그림에 박힌 카드(본문 세 줄) 쪽마다 하나. 지우기·한글 넣기를 서비스 흐름 그대로 돌린다."""
    src = pymupdf.open()
    for _ in cases:
        page = src.new_page(width=252, height=178)
        img = np.full((742, 1050, 3), 236, np.uint8)
        img[:, :60] = (135, 106, 124)
        page.insert_image(page.rect, pixmap=pymupdf.Pixmap(pymupdf.csRGB, 1050, 742, img.tobytes(), 0),
                          keep_proportion=False)
        for y, s in ((105, "Draw a card and then gain gold"), (117, "from the bank, then pay a big gift."),
                     (129, "End your turn now quickly ok.")):
            page.insert_text((40, y), s, fontsize=9, color=(0.05, 0.05, 0.05))
    flat = pymupdf.open()  # 텍스트 레이어 없는 그림 카드로
    for p in src:
        q = flat.new_page(width=p.rect.width, height=p.rect.height)
        q.insert_image(q.rect, pixmap=p.get_pixmap(dpi=300))
    proj = tmp / "proj"
    (proj / "source").mkdir(parents=True)
    flat.save(proj / "source" / "cards.pdf")
    style = {"color": "#101010", "font": {"file": str(NOTO)}}
    (proj / "regions.json").write_text(json.dumps({"regions": [
        {"name": "본문", "rect": {"x": 0.14, "y": 0.5, "w": 0.75, "h": 0.28}, "kind": "text", "style": style},
    ], "protect_rects": [{"name": "띠", "rect": {"x": 0, "y": 0, "w": 0.1, "h": 1}}]}), encoding="utf-8")
    (proj / "samples.json").write_text(json.dumps({"cards": [
        {"page": i + 1, "본문": {"source": s, "ko": k}} for i, (s, k) in enumerate(cases)
    ]}, ensure_ascii=False), encoding="utf-8")
    return proj


@needs_noto
def test_end_to_end_erase_and_compose_partial_bold(tmp_path):
    from app import service

    cases = [
        ("Gain <b>2 Gold</b>.", "<b>금화 2개</b>를 얻는다."),
        ("Draw a card, then <b>discard two cards</b> and gain 1 Gold.",
         "카드 1장을 뽑은 다음, <b>손에서 카드 2장을 골라 버리고</b> 금화 1개를 얻는다. 그 뒤 차례를 마친다."),
        ("Draw a card and then gain gold\nfrom the <b>bank, then pay</b> a big gift.\nEnd your turn now quickly ok.",
         "카드 1장을 뽑은 뒤 <b>은행에서\n금화를 받고</b> 푹 쉰다.\n지금 바로 차례를 마친다."),
        ("Gain <b>2 Gold</b>.", "<b>금화 2개를 얻는다."),
    ]
    proj = _image_card_project(tmp_path, cases)
    assert service.erase(proj, f"1-{len(cases)}").ok
    assert service.compose(proj).state == "done"
    doc = pymupdf.open(proj / "translated" / "translated.pdf")
    report = json.loads((proj / "translated" / "report.json").read_text(encoding="utf-8"))
    status = [d["regions"]["본문"]["status"] for d in report["pages_detail"]]
    expect = [("금화2개", "를얻는다."),
              (_hangul("손에서 카드 2장을 골라 버리고"), _hangul("카드 1장을 뽑은 다음, 금화 1개를 얻는다. 그 뒤 차례를 마친다.")),
              (_hangul("은행에서 금화를 받고"), _hangul("카드 1장을 뽑은 뒤 푹 쉰다. 지금 바로 차례를 마친다.")),
              ("", "금화2개를얻는다.")]
    for i, (page, want) in enumerate(zip(doc, expect)):
        assert bold_and_regular(page) == want, (i + 1, status[i])
    assert [STATUS_TAG_MISMATCH in s for s in status] == [False, False, False, True]
