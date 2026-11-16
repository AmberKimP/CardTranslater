"""벡터 인쇄 PDF(배경 스캔 이미지 없음) 지우기: 텍스트 레이어 글자만 지우고 그림은 그대로, 줄 모양 메타, 한글 넣기."""

import numpy as np
import pymupdf

from app.erase import erase_page
from app.erase_vector import render_page
from app.regions import RatioRect, Region, RegionTemplate

W, H = 180.0, 252.0
INK = (0.2, 0.1, 0.05)
TITLE = (0.7, 0.45, 0.3)


def make_vector_pdf() -> bytes:
    """벡터 도형 배경 + 진짜 글꼴 글자 (제목, 본문 3줄, 영역 밖 숫자) + 영역 밖 윤곽선 도형."""
    doc = pymupdf.open()
    p = doc.new_page(width=W, height=H)
    p.draw_rect(p.rect, color=None, fill=(0.93, 0.88, 0.78))  # 종이색 바탕
    p.draw_rect(pymupdf.Rect(20, 100, 160, 170), color=None, fill=(0.85, 0.75, 0.6))  # 본문 뒤 색 상자
    p.draw_line((20, 120), (160, 160), color=(0.4, 0.2, 0.1), width=1.5)  # 본문 영역을 가로지르는 그림 선
    p.draw_circle((90, 210), 20, color=(0.1, 0.1, 0.1), fill=(0.6, 0.1, 0.1))
    p.insert_text((50, 60), "Bone Altar", fontsize=16, fontname="helv", color=TITLE)
    for i, t in enumerate(["A dark altar with", "skulls on display.", "Time will tell."]):
        p.insert_text((35, 120 + i * 14), t, fontsize=10, fontname="helv", color=INK)
    p.insert_text((85, 215), "5", fontsize=14, fontname="helv", color=(1, 1, 1))
    return doc.tobytes()


def template() -> RegionTemplate:
    return RegionTemplate(regions=[
        Region("이름", RatioRect(30 / W, 40 / H, 120 / W, 26 / H), "text"),
        Region("본문", RatioRect(25 / W, 105 / H, 130 / W, 52 / H), "text"),
    ], protect=[])


def _ink(img: np.ndarray, rect: pymupdf.Rect, color, k=300 / 72, tol=40) -> int:
    x0, y0, x1, y1 = (int(v * k) for v in rect)
    sub = img[y0:y1, x0:x1].astype(int)
    c = np.array([v * 255 for v in color])
    return int((np.abs(sub - c).max(axis=2) < tol).sum())


def test_vector_page_redacts_text_only_and_keeps_art():
    doc = pymupdf.open(stream=make_vector_pdf(), filetype="pdf")
    page = doc[0]
    before = render_page(page)
    n_draw = len(page.get_drawings())
    r = erase_page(page, template(), sources={"이름": "Bone Altar", "본문": "A dark altar with\nskulls on display.\nTime will tell."})

    m = r.meta
    assert m["background_image"]["virtual"] is True
    assert m["patches"] == []  # 텍스트 레이어만 지웠다: 그림 패치 없음
    texts = page.get_text()
    assert "Bone" not in texts and "altar" not in texts and "Time" not in texts
    assert "5" in texts  # 영역 밖 글자는 그대로
    assert len(page.get_drawings()) == n_draw  # 벡터 도형은 그대로
    after = render_page(page)
    body = pymupdf.Rect(25, 105, 155, 157)
    assert _ink(before, body, INK) > 200 and _ink(after, body, INK) < 20
    # 그림 선·색 상자는 남는다 (본문 영역 안 그림 선 색 픽셀)
    assert _ink(after, body, (0.4, 0.2, 0.1), tol=25) > 50
    # 영역 밖은 바뀌지 않는다
    out = np.ones(before.shape[:2], bool)
    for reg in m["regions"]:
        x0, y0, x1, y1 = reg["work_rect_px"]
        out[y0 - 2:y1 + 2, x0 - 2:x1 + 2] = False
    assert int((np.abs(after.astype(int) - before.astype(int)).max(axis=2)[out] > 0).sum()) == 0

    regs = {x["name"]: x for x in m["regions"]}
    assert regs["본문"]["erase"]["method"] == "vector_redact"
    assert regs["본문"]["erase"]["flags"] == []
    assert regs["본문"]["text_layer"]["size_pt"] == 10.0
    lines = regs["본문"]["text_layout"]["lines"]
    assert len(lines) == 3
    assert all(ln["color"] == regs["본문"]["text_layer"]["color"] for ln in lines)  # span 색 (렌더 중앙값 대신)
    assert all(5.0 <= ln["glyph_h_pt"] <= 11.0 for ln in lines)
    assert lines[0]["bbox_pt"][1] < lines[1]["bbox_pt"][1] < lines[2]["bbox_pt"][1]
    title = regs["이름"]["text_layout"]["lines"]
    assert len(title) == 1
    assert regs["이름"]["text_layer"]["color"] == title[0]["color"]
    assert m["vector_page"]["raster_regions"] == []
    assert r.before.shape == before.shape


def test_vector_page_without_text_layer_in_region_uses_picture_fallback():
    """영역에 span이 없으면(윤곽선 글자) 렌더링한 그림으로 지우고 패치를 덮는다."""
    doc = pymupdf.open()
    p = doc.new_page(width=W, height=H)
    p.draw_rect(p.rect, color=None, fill=(0.95, 0.95, 0.92))
    # 윤곽선 글자 흉내: 영역 안에 글자 같은 굵은 획 (텍스트 레이어 없음)
    for i in range(6):
        p.draw_rect(pymupdf.Rect(40 + i * 14, 115, 46 + i * 14, 127), color=None, fill=(0.1, 0.1, 0.1))
    p.insert_text((50, 60), "Title", fontsize=16, fontname="helv", color=(0, 0, 0))
    page = doc[0]
    before = render_page(page)
    r = erase_page(page, template(), sources={"이름": "Title", "본문": "IIIIII"})
    m = r.meta
    regs = {x["name"]: x for x in m["regions"]}
    assert m["vector_page"]["raster_regions"] == ["본문"]
    assert regs["이름"]["erase"]["method"] == "vector_redact"
    assert regs["본문"]["erase"]["method"].startswith("vector_raster:")
    assert any("그림 방식" in w for w in m["warnings"])
    after = render_page(page)
    body = pymupdf.Rect(25, 105, 155, 157)
    assert _ink(before, body, (0.1, 0.1, 0.1)) > 300
    assert _ink(after, body, (0.1, 0.1, 0.1)) < 0.2 * _ink(before, body, (0.1, 0.1, 0.1))
    if m["patches"]:
        assert all(pt["region"] == "본문" for pt in m["patches"])
    assert page.rect == pymupdf.Rect(0, 0, W, H)


def test_vector_page_compose_draws_korean_on_layout_lines(tmp_path):
    from app.typeset_compose import compose_page

    src = pymupdf.open(stream=make_vector_pdf(), filetype="pdf")
    doc = pymupdf.open(stream=make_vector_pdf(), filetype="pdf")
    page = doc[0]
    r = erase_page(page, template())
    card = {"이름": {"ko": "뼈 제단", "source": "Bone Altar"},
            "본문": {"ko": "해골이 놓인 어두운 제단.\n이상한 힘이 느껴진다.\n시간이 말해 주리라.",
                     "source": "A dark altar with\nskulls on display.\nTime will tell."}}
    rec = compose_page(page, src[0], r.meta, card)
    assert rec["본문"].get("drawn", True) is not False
    assert rec["본문"].get("source_ink", {}).get("layout_lines") == 3
    txt = page.get_text()
    assert "제단" in txt or len(page.get_fonts()) > len(src[0].get_fonts())
    # 결과 쪽 크기·박스는 원본과 같다
    assert page.rect == src[0].rect and page.mediabox == src[0].mediabox


def test_text_shadow_effect_form_is_removed_but_art_form_kept():
    """소프트 마스크를 쓴 폼(글자 그림자)이 지운 글자 상자에 들면 지우고, 큰 그림 폼은 남긴다."""
    from app.erase_vector import remove_text_effects

    doc = pymupdf.open()
    p = doc.new_page(width=W, height=H)
    smask = doc.get_new_xref()
    doc.update_object(smask, "<< /Type /Mask /S /Luminosity /G 0 0 R >>")
    gs = doc.get_new_xref()
    doc.update_object(gs, f"<< /Type /ExtGState /SMask {smask} 0 R >>")
    small, big = doc.get_new_xref(), doc.get_new_xref()
    for x, bb in ((small, "30 130 150 160"), (big, "0 0 180 252")):
        doc.update_object(x, f"<< /Type /XObject /Subtype /Form /BBox [ {bb} ] >>")
        doc.update_stream(x, b"0 0 1 rg 30 130 120 30 re f")
    doc.xref_set_key(p.xref, "Resources", f"<< /ExtGState << /GS1 {gs} 0 R >> /XObject << /Fm1 {small} 0 R /Fm2 {big} 0 R >> >>")
    cx = doc.get_new_xref()
    doc.update_object(cx, "<< >>")
    doc.update_stream(cx, b"q /GS1 gs /Fm1 Do Q q /GS1 gs /Fm2 Do Q")
    doc.xref_set_key(p.xref, "Contents", f"{cx} 0 R")
    # 지운 글자 줄 상자: 쪽 좌표 (위가 0) — 폼 1 (PDF y 130~160) = 쪽 y 92~122
    removed = remove_text_effects(p, [(35.0, 95.0, 145.0, 120.0)])
    assert len(removed) == 1
    data = doc.xref_stream(p.get_contents()[0])
    assert b"/Fm1 Do" not in data and b"/Fm2 Do" in data
