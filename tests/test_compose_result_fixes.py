"""결과 보기에서 찾은 한글 넣기 문제 (카드 프로젝트 2·5·8·11·12쪽)와 결과 보기에서 글 위치 옮기기."""

from __future__ import annotations

import json

import numpy as np
import pymupdf
import pytest

from app import erase_text, typeset_compose
from app.typeset_compose import compose_layout_region, sentence_blocks, swap_line_accents


# ---------------------------------------------------------------- 2쪽: 원문 한 줄 + 아이콘 줄, 주사위 숫자가 글 줄로
def test_extra_lines_keeps_line_near_region_glyph_size():
    """원문 글 줄 하나에 찾은 줄 둘(진짜 글 줄 8px, 주사위 속 큰 숫자 11.5px): 폭 맞춤은 늘 같으므로
    영역 대표 글자 크기와 가장 다른 줄(주사위 숫자)을 뺀다. 대표 크기를 모르면 예전처럼 작은 줄부터."""
    text = {"bbox": [128, 49, 248, 57], "glyph_h": 8.0}
    dice = {"bbox": [96, 88, 280, 100], "glyph_h": 11.5}
    assert erase_text._extra_lines([text, dice], ["いずれかを選択し実行"], None, 8.0) == [dice]
    assert erase_text._extra_lines([text, dice], ["いずれかを選択し実行"]) == [text]


def test_match_source_lines_text_line_wins_over_dice_digits():
    mask = np.ones((160, 360), bool)
    tm = erase_text.TextMask(mask, mask.copy(), True, 1.0, glyph_px=8.0)
    lay = {"lines": [{"bbox": [128, 49, 248, 57], "glyph_h": 8.0, "color": "#6b666d", "align": "center"},
                     {"bbox": [96, 88, 280, 100], "glyph_h": 11.5, "color": "#3c4147", "align": "left"}],
           "dividers": []}
    # 아이콘 찾기가 엉뚱한 자리(주사위 아래 작은 상자)를 돌려준 경우
    out = erase_text.match_source_lines(tm, lay, "いずれかを選択し実行\n / [[wood_crate_5]] [[wood_crate_2_2]]",
                                        icon_rows=[(129, 137), (127, 140)])
    assert [ln["bbox"][1] for ln in out["lines"]] == [49]
    assert not tm.mask[88:100, 96:280].any()  # 주사위 숫자는 지우지 않는다


# ---------------------------------------------------------------- 5·12쪽: 줄의 가장 넓은 색이 <c1> 쪽
def _red_line(accent_x):
    return {"bbox_pt": [14.88, 200.16, 111.84, 204.48], "glyph_h_pt": 3.48, "color": "#e65c70",
            "accents": [{"color": "#545454", "x_pt": accent_x}]}


def test_swap_line_accents_when_tag_text_is_the_wider_color():
    src = "加える.<c1>(以降,手札は1枚増加する)</c1>"
    ln = _red_line([[14.88, 21.12], [22.08, 28.32], [29.28, 34.08], [36.0, 36.96]])  # 줄 앞(태그 밖) 글자가 회색
    out = swap_line_accents([ln], [src])[0]
    assert out["color"] == "#545454" and [a["color"] for a in out["accents"]] == ["#e65c70"]
    # 강조색이 태그 안 글자 자리에 있으면(보통 경우) 그대로
    gray = {"bbox_pt": [58.56, 223.68, 158.4, 228.0], "glyph_h_pt": 4.32, "color": "#5b616c",
            "accents": [{"color": "#ed6a78", "x_pt": [[122.4, 127.2], [138.24, 154.56]]}]}
    assert swap_line_accents([gray], ["の数だけ増加する。<c1>（最大4枚増加）</c1>"])[0] is gray
    # 원문에 태그가 없거나 줄 수가 다르면 건드리지 않는다
    assert swap_line_accents([ln], ["加える"])[0] is ln
    assert swap_line_accents([ln], None)[0] is ln


def test_compose_layout_region_tag_color_stays_inside_tag():
    """‘추가한다.<c1>(…)</c1>’: 태그 밖 ‘추가한다.’는 회색, 태그 안만 빨강으로 그린다."""
    lay = {"lines": [
        {"bbox_pt": [14.88, 30.0, 160.8, 36.0], "glyph_h_pt": 4.32, "color": "#6f625d", "stroke_color": None,
         "stroke_pt": 0.0, "weight": 0.2, "bold": False, "align": "left", "accents": []},
        {**_red_line([[14.88, 21.12], [22.08, 28.32], [29.28, 34.08], [36.0, 36.96]]),
         "bbox_pt": [14.88, 40.0, 111.84, 44.32], "stroke_color": None, "stroke_pt": 0.0, "weight": 0.2,
         "bold": False, "align": "left"},
    ], "dividers_pt": [], "icons": []}
    src = "山札を見て,任意の1枚を選択し手札に\n加える.<c1>(以降,手札は1枚増加する)</c1>"
    doc = pymupdf.open()
    page = doc.new_page(width=180, height=80)
    compose_layout_region(page, (8, 20, 170, 70), "더미를 보고 1장을 선택해 손패에\n추가한다.<c1>(이후 1장 증가)</c1>",
                          {}, lay, source_text=src)
    colors = {}
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            for sp in line["spans"]:
                for ch in sp["text"]:
                    colors.setdefault(ch, set()).add("#%06x" % sp["color"])
    # 태그 밖은 본문 색 (같은 서식 줄 묶음의 색: 첫 줄과 같은 회색), 태그 안만 빨강
    assert colors["추"] == colors["보"] and colors["한"] == colors["보"] and colors["보"] != {"#e65c70"}
    assert colors["후"] == {"#e65c70"}


# ---------------------------------------------------------------- 8·11쪽: 여러 줄 문장의 왼쪽 시작점
def _ln(x0, x1, src, align="left", gh=4.32, g=0):
    return {"bbox_pt": [x0, 0, x1, 4], "glyph_h_pt": gh, "_g": g, "align": align, "_src": src}


def test_sentence_blocks_justified_lines_are_left_not_right():
    """8쪽: 양쪽 맞춤이라 오른쪽 끝이 고르고, 둘째 줄은 잉크로 못 잡은 ☆ 뒤에서 시작. 지우기가 두 줄 다 왼쪽
    정렬로 봤으면 ‘오른쪽 정렬’을 고르지 않고, 왼쪽 시작점은 글 칸 왼쪽 여백(괄호 줄 뺀 가운데 값)."""
    a, b, c = _ln(49.92, 161.76, "[[reflex]]ゲームで"), _ln(49.44, 156.0, "カード」から"), _ln(50.4, 151.68, "すべて")
    d, e = _ln(48.96, 159.36, "［エピソードカード」の"), _ln(58.56, 158.4, "の数だけ増加する。")
    blocks = sentence_blocks([a, b, c, d, e], ["게임에서", "카드」 중", "된다.", "[에피소드 카드」의 플레이 한도가",
                                                "의 수만큼 증가한다."])
    blk = blocks[id(d)]
    assert blk is blocks[id(e)] and blk["align"] == "left"
    assert blk["x0"] == pytest.approx(np.median([49.92, 49.44, 50.4, 58.56]))


def test_sentence_blocks_left_start_ignores_opening_bracket_line():
    """11쪽: 셋째 줄이 ‘［’로 시작해 잉크가 1pt쯤 왼쪽으로 나와 있다. 가장 왼쪽(16.8)이 아니라 괄호 줄을 뺀
    시작점(17.76, 17.28의 가운데)에 맞춘다."""
    lines = [_ln(17.76, 161.28, "自分の手番の"), _ln(17.28, 153.12, "に置かれた"), _ln(16.8, 155.52, "[アクションカード]1枚")]
    blk = sentence_blocks(lines, ["맨 위에", "손패의", "된다."])[id(lines[0])]
    assert blk["align"] == "left" and blk["x0"] == pytest.approx(17.52)


def test_compose_layout_region_keeps_indented_line_start():
    """왼쪽 정렬 문장에서 원문이 글자 높이 넘게 들여 쓴 줄(줄 앞에 지우지 않은 ☆)은 그 자리에서, 나머지는 여백에서."""
    def ln(x0, y, src_x1=158.0):
        return {"bbox_pt": [x0, y, src_x1, y + 4.32], "glyph_h_pt": 4.32, "color": "#505050", "stroke_color": None,
                "stroke_pt": 0.0, "weight": 0.2, "bold": False, "align": "left", "accents": []}
    lay = {"lines": [ln(49.92, 20), ln(49.44, 30), ln(48.96, 40), ln(58.56, 50)], "dividers_pt": [], "icons": []}
    doc = pymupdf.open()
    page = doc.new_page(width=180, height=80)
    rec = compose_layout_region(page, (10, 10, 170, 70), "첫 줄 글은\n이어지는 줄\n[셋째 줄]의 한도가\n의 수만큼 증가한다.",
                                {}, lay, source_text="一行目\n二行目\n［三行目」の\nの数だけ増加する。")
    x0s = [b[0] for b in rec["line_boxes_pt"]]
    assert x0s[0] == pytest.approx(x0s[1]) == pytest.approx(x0s[2])
    assert x0s[3] == pytest.approx(58.56)


# ---------------------------------------------------------------- 결과 보기에서 글 위치 옮기기
def _meta():
    return {"regions": [{"name": "본문", "kind": "text", "rect_pt": [10, 10, 290, 90],
                         "text_layer": {"size_pt": 8.0}}]}


def test_compose_page_text_offset_moves_drawn_text_and_record():
    doc, sdoc = pymupdf.open(), pymupdf.open()
    out = []
    for offsets in (None, {"본문": (12.5, -4.0)}):
        pg = doc.new_page(width=300, height=100)
        src = sdoc.new_page(width=300, height=100)
        rec = typeset_compose.compose_page(pg, src, _meta(), {"본문": {"ko": "짧은 글"}}, offsets=offsets)
        words = pg.get_text("words")
        assert words and "짧은" in " ".join(w[4] for w in words)  # 옮겨도 벡터 텍스트 그대로 (추출됨)
        out.append((min(w[0] for w in words), min(w[1] for w in words), rec["본문"]))
    (x_a, y_a, rec_a), (x_b, y_b, rec_b) = out
    assert x_b - x_a == pytest.approx(12.5, abs=0.05) and y_b - y_a == pytest.approx(-4.0, abs=0.05)
    assert rec_b["offset_pt"] == [12.5, -4.0] and "offset_pt" not in rec_a
    for ba, bb in zip(rec_a["line_boxes_pt"], rec_b["line_boxes_pt"]):
        assert bb[0] - ba[0] == pytest.approx(12.5, abs=0.01) and bb[1] - ba[1] == pytest.approx(-4.0, abs=0.01)


def test_compose_page_offset_with_outlined_lines_keeps_background():
    """외곽선 줄(ActualText로 감싼 외곽선 스트림 뒤 PyMuPDF가 페이지를 다시 감싸는 q·Q)이 있어도 글만 옮기고
    배경(예전 내용)은 그대로 둔다. (q·Q 감싸기 스트림까지 옮기면 짝이 깨져 카드 그림이 같이 밀렸다)"""
    ln = {"bbox_pt": [20.0, 30.0, 200.0, 36.0], "glyph_h_pt": 6.0, "color": "#303030", "stroke_color": "#f0f0f0",
          "stroke_pt": 0.6, "weight": 0.2, "bold": False, "align": "left", "accents": []}
    meta = {"regions": [{"name": "본문", "kind": "text", "rect_pt": [10, 20, 290, 90],
                         "text_layout": {"lines": [ln, {**ln, "bbox_pt": [20.0, 45.0, 200.0, 51.0]}],
                                         "dividers_pt": [], "icons": []}}]}
    doc, sdoc = pymupdf.open(), pymupdf.open()
    res = []
    for offsets in (None, {"본문": (6.0, 5.0)}):
        pg = doc.new_page(width=300, height=100)
        pg.draw_rect((2, 2, 30, 12), color=(1, 0, 0), fill=(1, 0, 0))  # 배경 (지운 카드)
        src = sdoc.new_page(width=300, height=100)
        typeset_compose.compose_page(pg, src, meta, {"본문": {"ko": "첫 줄 글\n둘째 줄"}}, offsets=offsets)
        words = {w[4]: w[:2] for w in pg.get_text("words")}
        red = [d["rect"] for d in pg.get_drawings() if d.get("fill") == (1.0, 0.0, 0.0)]
        res.append((words, red))
    (w0, red0), (w1, red1) = res
    assert red0 == red1 and red0 and red0[0] == pymupdf.Rect(2, 2, 30, 12)
    assert w1["첫"][0] - w0["첫"][0] == pytest.approx(6.0, abs=0.05)
    assert w1["둘째"][1] - w0["둘째"][1] == pytest.approx(5.0, abs=0.05)


def test_compose_page_offset_only_moves_that_region():
    meta = {"regions": [{"name": "이름", "kind": "text", "rect_pt": [10, 5, 290, 40], "text_layer": {"size_pt": 8.0}},
                        {"name": "본문", "kind": "text", "rect_pt": [10, 50, 290, 95], "text_layer": {"size_pt": 8.0}}]}
    doc, sdoc = pymupdf.open(), pymupdf.open()
    res = []
    for offsets in (None, {"본문": (0.0, 3.0)}):
        pg = doc.new_page(width=300, height=100)
        src = sdoc.new_page(width=300, height=100)
        typeset_compose.compose_page(pg, src, meta, {"이름": {"ko": "이름"}, "본문": {"ko": "본문 글"}}, offsets=offsets)
        res.append({w[4]: w[1] for w in pg.get_text("words")})
    assert res[1]["이름"] == pytest.approx(res[0]["이름"], abs=0.01)
    assert res[1]["본문"] - res[0]["본문"] == pytest.approx(3.0, abs=0.05)


def test_card_text_offset_service_round_trip(tmp_path):
    from app import service

    fd = tmp_path / "file"
    fd.mkdir()
    (fd / "file.json").write_text(json.dumps({"erase_modes": {"3": {"본문": "inpaint"}}}), encoding="utf-8")
    assert service.card_text_offset(fd, 5, "본문") == (0.0, 0.0)
    service.set_card_text_offset(fd, 5, "본문", 1.5, -2)
    assert service.card_text_offset(fd, 5, "본문") == (1.5, -2.0)
    assert service.card_text_offsets(fd, 5) == {"본문": (1.5, -2.0)}
    assert typeset_compose.text_offsets_for_page(fd, 5) == {"본문": (1.5, -2.0)}
    data = json.loads((fd / "file.json").read_text(encoding="utf-8"))
    assert data["text_offsets"] == {"5": {"본문": [1.5, -2.0]}} and data["erase_modes"]  # 다른 설정은 그대로
    service.set_card_text_offset(fd, 5, "본문", 0, 0)  # 0이면 지운다
    assert "text_offsets" not in json.loads((fd / "file.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError):
        service.set_card_text_offset(fd, 5, "본문", float("nan"), 0)


def test_text_offset_is_part_of_compose_signature(tmp_path, monkeypatch):
    """옮김을 바꾸면 그 쪽의 한글 넣기 재료(compose_sig)가 바뀐다 -> recompose_file이 그 쪽만 다시 넣는다."""
    calls = {"v": {}}
    monkeypatch.setattr(typeset_compose, "text_offsets_for_page", lambda fd, p: calls["v"].get(p, {}))

    class _T:
        regions: list = []

    class _P:
        template = _T()

    from app import font_plan

    monkeypatch.setattr(font_plan, "load", lambda d: {})
    metas = {1: {"regions": []}, 2: {"regions": []}}
    a = typeset_compose._compose_sigs(tmp_path, _P(), [1, 2], metas, {}, "s", "e")
    calls["v"] = {2: {"본문": (1.0, 0.0)}}
    b = typeset_compose._compose_sigs(tmp_path, _P(), [1, 2], metas, {}, "s", "e")
    assert a[1] == b[1] and a[2] != b[2]
