"""한글 배치(app.typeset*) 테스트.

순수 함수(줄바꿈·금칙·태그·크기 맞추기)는 고정 폭 가짜 측정기로,
글꼴 메트릭·그리기는 fonts/ 의 실제 글꼴로 확인한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.typeset_layout import (  # noqa: E402
    STATUS_OVERFLOW,
    STATUS_SHRUNK,
    Atom,
    FitParams,
    check_tags,
    icon_names,
    fit_text,
    min_size_rule,
    parse_markup,
    to_atoms,
    wrap_text,
)


class Mono:
    """글자 1개 = 1em, 공백 = 0.5em, 아이콘 = 1.1em 인 가짜 측정기."""

    def advance(self, atom: Atom, size: float) -> float:
        if atom.kind == "space":
            return 0.5 * size
        if atom.kind == "icon":
            return 1.1 * size
        return 1.0 * size

    def ink_extent(self, atoms):
        return 0.8, 0.2


M = Mono()


def lines_of(text: str, max_w: float, size: float = 1.0, tracking: float = 0.0, manual=True) -> list[str]:
    spans, _ = parse_markup(text)
    return [ln.text for ln in wrap_text(to_atoms(spans), max_w, size, tracking, M, manual)]


# ---------------------------------------------------------------- 줄바꿈
def test_empty_string_gives_one_empty_line():
    assert lines_of("", 10) == [""]


def test_word_wrap_by_eojeol():
    # "가나 다라 마바" : 가나(2) + 공백(.5) + 다라(2) = 4.5
    assert lines_of("가나 다라 마바", 5) == ["가나 다라", "마바"]


def test_long_word_breaks_by_character():
    assert lines_of("가나다라마바사", 3) == ["가나다", "라마바", "사"]


def test_long_word_after_short_word():
    assert lines_of("가 나다라마바사아", 4) == ["가", "나다라마", "바사아"]


def test_kinsoku_no_line_start_with_closing_punct_in_long_word():
    # 글자 단위로 끊어도 '.' '」' 로 줄을 시작하지 않는다
    out = lines_of("가나다.라마", 3)
    assert all(not ln.startswith((".", "」")) for ln in out)
    assert out[0] == "가나"  # '다.' 는 함께 다음 줄로


def test_kinsoku_separated_punct_glued_to_previous_word():
    # 사람이 공백 뒤에 쓴 닫는 부호도 줄 처음에 오지 않는다
    out = lines_of("가나다 」라", 3.5)
    assert all(not ln.startswith("」") for ln in out)


def test_kinsoku_no_line_end_with_opening_bracket():
    out = lines_of("가나「다라마", 3)
    assert all(not ln.endswith("「") for ln in out)
    assert out[0] == "가나"


def test_icon_with_particle_not_split():
    # [[coin]]을 은 한 덩어리: 폭 3(=1.1+1)를 넘지 않으면 함께 다음 줄로
    out = lines_of("가나 [[coin]]을 얻는다", 3)
    assert "[[coin]]을" in out


def test_icon_glued_inside_long_word_not_split():
    # 금화[[coin]]을 이 줄 폭보다 길어도 아이콘 앞뒤에서는 끊지 않는다
    out = lines_of("금화[[coin]]을", 2.5)
    joined = "|".join(out)
    assert "화|[[coin]]" not in joined and "[[coin]]|을" not in joined


def test_icon_only_line():
    assert lines_of("[[coin]]", 5) == ["[[coin]]"]


def test_manual_newline_respected_and_not_rewrapped():
    out = lines_of("가나 다라\n마바", 100)
    assert out == ["가나 다라", "마바"]
    # 수동 줄바꿈이 있으면 폭이 좁아도 자동 줄바꿈하지 않는다 (PRD 4장)
    out = lines_of("가나 다라 마바\n사", 3)
    assert out == ["가나 다라 마바", "사"]


def test_tracking_narrows_lines():
    spans, _ = parse_markup("가나다라마")
    atoms = to_atoms(spans)
    w0 = wrap_text(atoms, 100, 10, 0.0, M)[0].width
    w5 = wrap_text(atoms, 100, 10, -0.05, M)[0].width
    assert w0 == pytest.approx(50)
    assert w5 == pytest.approx(50 - 0.5 * 4)


# ---------------------------------------------------------------- 서식 태그
def test_tags_parsed():
    spans, err = parse_markup("<b>금화 2개</b>를 <i>얻는다</i> <c1>빨강</c1>")
    assert err == []
    assert spans[0].text == "금화 2개" and spans[0].fmt.bold
    assert spans[2].fmt.italic
    assert spans[-1].fmt.color_tag == "c1"


@pytest.mark.parametrize("bad", ["<b>굵게", "굵게</b>", "<b><i>x</b></i>", "<u>밑줄</u>", "<b><b>x</b></b>"])
def test_broken_tags_are_ignored(bad):
    spans, err = parse_markup(bad)
    assert err
    assert len(spans) == 1 and spans[0].fmt.bold is False and "<" not in spans[0].text


def test_tag_count_mismatch_with_source():
    assert check_tags("<b>금화</b>를 얻는다", "Gain <b>2 Gold</b>.") == []
    assert check_tags("금화를 얻는다", "Gain <b>2 Gold</b>.")
    assert check_tags("<i>금화</i>", "Gain <b>2 Gold</b>.")


# ---------------------------------------------------------------- 크기 맞추기
def test_min_size_rule():
    assert min_size_rule(18.7) == pytest.approx(18.7 * 0.8)
    assert min_size_rule(7.0) == pytest.approx(6.0)  # 80%=5.6 이지만 6pt 아래로는 안 간다
    assert min_size_rule(5.5) == pytest.approx(5.5)  # 원문이 이미 6pt 미만 -> 원문 크기가 하한
    assert min_size_rule(6.0) == pytest.approx(6.0)


def test_min_size_rule_ink_basis():
    # 원문 글자 높이 기준(floor_pt=None): pt 하한 없이 시작 크기(잉크 높이 보정)의 80%
    assert min_size_rule(5.5, floor_pt=None) == pytest.approx(4.4)
    assert min_size_rule(18.7, floor_pt=None) == pytest.approx(18.7 * 0.8)


def test_fit_ink_basis_shrinks_below_6pt():
    # 5.5pt 원문: pt 기준이면 5.5에서 넘침, 글자 높이 기준이면 4.4까지 줄여 들어간다
    pt = _fit("가나다라마바사아", 40, 10, 5.5, max_tracking=0.0)
    ink = _fit("가나다라마바사아", 40, 10, 5.5, max_tracking=0.0, floor_pt=None)
    assert STATUS_OVERFLOW in pt.status
    assert ink.status == [STATUS_SHRUNK] and 4.4 - 1e-9 <= ink.size < 5.5


def test_fit_base_tracking():
    # 기본 자간이 있으면 거기서 시작하고, 맞추기는 거기서 더 좁힌다
    r = _fit("가나다", 100, 20, 10, base_tracking=-0.08)
    assert r.tracking == pytest.approx(-0.08) and r.status == []
    r2 = _fit("가나다라마", 45.4, 10, 10, base_tracking=-0.08)  # 50 - 4*0.8 = 46.8 > 45.4 -> 더 좁힘
    assert -0.13 - 1e-9 <= r2.tracking < -0.08 and r2.size == 10


def _fit(text, w, h, start, **kw):
    spans, _ = parse_markup(text)
    return fit_text(to_atoms(spans), FitParams(box_w=w, box_h=h, start_size=start, **kw), M)


def test_fit_no_change_when_fits():
    r = _fit("가나다", 100, 20, 10)
    assert r.size == 10 and r.tracking == 0 and r.status == []


def test_fit_rewraps_before_shrinking():
    # 한 줄 폭 50 > 40 이지만 두 줄이면 높이 (12 + 10) = 22 <= 25
    r = _fit("가나 다라마", 40, 25, 10)
    assert r.size == 10 and r.status == [] and len(r.lines) == 2


def test_fit_uses_tracking_before_shrinking():
    # 폭 50 / 4자간 -> -2.5% 면 49 이내
    r = _fit("가나다라마", 49, 10, 10)
    assert r.size == 10 and r.tracking < 0 and r.tracking >= -0.05 and r.status == []


def test_fit_shrinks_to_80_percent_and_flags():
    r = _fit("가나다라마", 44, 10, 10)  # -5% 로도 48 > 44 -> 축소
    assert STATUS_SHRUNK in r.status and STATUS_OVERFLOW not in r.status
    assert 8.0 <= r.size < 10


def test_fit_overflow_flags_and_drops_text():
    r = _fit("가나다라마바사아자차카타파하", 20, 10, 10)
    assert STATUS_OVERFLOW in r.status
    assert r.size == pytest.approx(8.0)
    assert r.overflow_text
    kept = "".join(r.line_texts)
    assert len(kept) < 14


def test_floor_rule_below_6pt_never_shrinks():
    r = _fit("가나다라마바사아자차카타파하", 20, 6, 5.5)
    assert r.size == pytest.approx(5.5)
    assert STATUS_SHRUNK not in r.status and STATUS_OVERFLOW in r.status
    assert r.tracking == pytest.approx(-0.05)


def test_fixed_size_is_not_changed():
    r = _fit("가나다라마바사아", 20, 6, 10, fixed_size=9)
    assert r.size == 9


# ---------------------------------------------------------------- 실제 글꼴·그리기
pymupdf = pytest.importorskip("pymupdf")
from app.typeset import TextStyle, typeset_region, typeset_tag, typeset_tags, finalize  # noqa: E402
from app.typeset_ink import font_ink_ratio  # noqa: E402

GAEGU = str(ROOT / "fonts/gaegu/Gaegu-Bold.ttf")
NOTO_BLACK = str(ROOT / "fonts/notosanskr/NotoSansKR-Black.ttf")
JUA = str(ROOT / "fonts/jua/Jua-Regular.ttf")
needs_fonts = pytest.mark.skipif(not Path(GAEGU).exists(), reason="fonts/ 없음 (scripts/fetch_fonts.py)")


@needs_fonts
def test_real_font_draw_and_extract(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=100)
    st = TextStyle(font_file=GAEGU, size_pt=8, color="#2b292a")
    r = typeset_region(page, (10, 10, 190, 60), "한 줄기 열풍이 털가죽을 그을린다.", st)
    assert r.status == [] and r.drawn
    finalize(doc)
    out = tmp_path / "t.pdf"
    doc.save(out)
    d = pymupdf.open(out)
    assert "한 줄기 열풍이" in d[0].get_text()
    # 서브셋으로 내장되었는가 (이름 앞 6글자+)
    assert any("+" in f[3] for f in d[0].get_fonts())


@needs_fonts
def test_special_inputs():
    doc = pymupdf.open()
    page = doc.new_page()
    st = TextStyle(font_file=GAEGU, size_pt=8)
    assert typeset_region(page, (0, 0, 100, 20), "", st).status == ["빈 영역"]
    assert typeset_region(page, (0, 0, 100, 20), "-", st).status == ["지우기만"]
    r = typeset_region(page, (0, 0, 100, 20), "<b>깨진", st)
    assert "서식 불일치" in r.status and r.lines == ["깨진"]
    r = typeset_region(page, (0, 0, 100, 20), "[[nosuch]]을 얻는다", st)
    assert "아이콘 오류" in r.status and not r.drawn


@needs_fonts
def test_tag_label_grows_instead_of_shrinking():
    doc = pymupdf.open()
    page = doc.new_page()
    st = TextStyle(font_file=NOTO_BLACK, size_pt=8, color="#ffffff", align="center")
    labels = [{"bbox_pt": [10, 10, 25, 18], "color": "#204078", "radius_pt": 4},
              {"bbox_pt": [28, 10, 60, 18], "color": "#9c2c3c", "radius_pt": 4}]
    res = typeset_tags(page, labels, ["몬스터 군단", "용"], st)
    assert res[0].size == 8
    assert res[0].label_rect[2] > 25
    gap = res[1].label_rect[0] - res[0].label_rect[2]
    assert gap == pytest.approx(3)


@needs_fonts
def test_ink_ratio_handwriting_vs_gothic():
    # 같은 pt에서 손글씨(Gaegu)의 대표 글자 잉크 높이는 본고딕보다 작다
    assert font_ink_ratio(GAEGU) < font_ink_ratio(NOTO_BLACK)
    assert 0.5 < font_ink_ratio(JUA) < 1.0


# ---------------------------------------------------------------- 합성: 라벨 늘리기
@needs_fonts
def test_compose_label_widen_removes_old_label_streams():
    from app.typeset_compose import find_label_streams

    doc = pymupdf.open()
    page = doc.new_page(width=200, height=60)
    labels = [{"bbox_pt": [10, 10, 30, 18], "color": "#204078", "radius_pt": 4},
              {"bbox_pt": [33, 10, 60, 18], "color": "#9c2c3c", "radius_pt": 4}]
    for lab in labels:  # 지우기 단계처럼 라벨마다 content stream 하나
        r = pymupdf.Rect(lab["bbox_pt"])
        page.draw_rect(r, color=None, fill=(0.1, 0.2, 0.5), radius=(4 / r.width, 0.5), width=0)
    found = find_label_streams(page, [l["bbox_pt"] for l in labels])
    assert sorted(found) == [0, 1]
    for x in found.values():
        doc.update_stream(x, b" ")
    st = TextStyle(font_file=NOTO_BLACK, size_pt=8, color="#ffffff")
    res = typeset_tags(page, labels, ["아주 긴 태그 이름", "용"], st)
    assert res[0].label_rect[2] > 30
    # 예전 두 번째 라벨 왼쪽 끝(33~)은 새 첫 라벨이나 간격으로 덮여야 하고, 예전 라벨 색이 남지 않아야 한다
    pix = page.get_pixmap(dpi=144, clip=(res[0].label_rect[2] + 0.5, 12, res[1].label_rect[0] - 0.5, 16))
    assert set(pix.samples) == {255}


@needs_fonts
def test_fallback_font_for_missing_glyphs(tmp_path):
    """개구에 없는 ♥·… 는 대체 글꼴로 그리고 '글꼴에 없는 글자'로 막지 않는다."""
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=100)
    st = TextStyle.from_region_style({"font": {"file": GAEGU}}, size_pt=8)
    assert st.fallback_file and st.fallback_file.endswith("NotoSansKR-Bold.ttf")
    r = typeset_region(page, (10, 10, 190, 60), "타락해 버려~♥ 어디에……", st)
    assert r.status == [] and r.drawn, r
    finalize(doc)
    out = tmp_path / "t.pdf"
    doc.save(out)
    d = pymupdf.open(out)
    assert "♥" in d[0].get_text()
    assert len(d[0].get_fonts()) == 2
    # 대체 글꼴이 없으면 예전처럼 막는다
    st2 = TextStyle(font_file=GAEGU, size_pt=8)
    assert "글리프 없음" in typeset_region(doc.new_page(), (0, 0, 190, 50), "♥", st2).status


def test_single_brackets_are_text_double_brackets_are_icons():
    """[4층]·[炎]은 카드에 인쇄된 괄호(글자), [[coin]]만 아이콘. 예전 전각 ［］도 보통 괄호로 그린다."""
    atoms = to_atoms(parse_markup("[4층]에 있을 때 [[coin]] ［炎］")[0])
    texts = [a.text for a in atoms if a.kind == "char"]
    assert texts[:4] == ["[", "4", "층", "]"]
    assert texts[-3:] == ["[", "炎", "]"]
    assert [a.text for a in atoms if a.kind == "icon"] == ["coin"]
    assert icon_names("[4층] [[coin]] [[gem_red]]") == ["coin", "gem_red"]


# ---------------------------------------------------------------- 두 줄 문장을 한 줄로
def test_two_line_sentence_rules():
    from app.typeset_layout import join_lines_at, soft_break, two_line_sentences

    assert soft_break("카드를 한 장", "뽑는다.")
    assert not soft_break("카드를 뽑는다.", "그다음 버린다")  # 문장 끝
    assert not soft_break("피해를 준다", "그다음")  # ‘…다’로 끝난 줄
    assert not soft_break("공격력 +", "[[sword]][[sword]]")  # 아이콘만 있는 줄
    assert not soft_break("공격 시 [[coin]]", "을 얻는다")  # 아이콘으로 끝난 줄
    assert soft_break("<b>강한</b>", "<c1>공격</c1>을 한다.")
    # 딱 두 줄로 된 문장만 (세 줄 문장은 그대로)
    assert two_line_sentences(["카드를", "뽑는다.", "그다음", "한 장을", "버린다."]) == [0]
    assert two_line_sentences(["끝.", "카드를", "뽑는다."]) == [1]
    assert join_lines_at(["카드를", "뽑는다.", "끝."], [0]) == ["카드를 뽑는다.", "끝."]


def test_fit_joins_two_line_sentence_only_when_it_fits_one_line():
    # "카드를 한 장 뽑는다." = 9글자 + 빈칸 3 = 105: 폭 120이면 한 줄로
    r = _fit("카드를 한\n장 뽑는다.", 120, 40, 10)
    assert r.line_texts == ["카드를 한 장 뽑는다."] and r.size == 10 and r.status == []
    # 한 줄 폭을 넘으면 원래 두 줄 그대로 (글자를 줄여 합치지 않는다)
    r = _fit("카드를 한\n장 뽑는다.", 100, 40, 10)
    assert r.line_texts == ["카드를 한", "장 뽑는다."] and r.size == 10
    # 세 줄 문장은 줄이지 않는다
    assert _fit("가나\n다라\n마바.", 200, 60, 10).line_texts == ["가나", "다라", "마바."]
    # 문장이 끝난 줄바꿈은 그대로, 뒤의 두 줄 문장만 한 줄로
    assert _fit("끝났다.\n카드를\n뽑는다.", 200, 60, 10).line_texts == ["끝났다.", "카드를 뽑는다."]
    # 끄면 예전처럼 사람이 넣은 줄 그대로
    assert _fit("카드를 한\n장 뽑는다.", 120, 40, 10, join_two=False).line_texts == ["카드를 한", "장 뽑는다."]
