"""작은 합성 PDF로 erase_page 전체 흐름을 확인한다 (실제 카드 PDF 불필요)."""

import numpy as np
import pymupdf
import pytest

from app.erase import (
    erase_page,
    find_background_image,
    load_image_rgb,
    patch_rect_pt,
    remove_inline_images,
    render_on_image_grid,
    BackgroundImage,
)
from app.project import ensure_not_source
from app.regions import RatioRect, Region, RegionTemplate


def make_card_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=252, height=178)
    w, h = 1050, 742
    yy = np.mgrid[0:h, 0:w][0].astype(np.float32)
    img = np.stack([180 + yy / h * 55] * 3, axis=2)
    img[:, :60] = (135, 106, 124)  # 왼쪽 띠
    img[600:640, 150:400] = 15  # 이미지에 박힌 '글자'
    img = np.clip(img, 0, 255).astype(np.uint8)
    pix = pymupdf.Pixmap(pymupdf.csRGB, w, h, img.tobytes(), 0)
    page.insert_image(page.rect, pixmap=pix, keep_proportion=False)
    page.insert_text((40, 120), "HELLO", fontsize=18, color=(0.03, 0.03, 0.04))
    page.draw_rect(pymupdf.Rect(40, 100, 80, 108), color=None, fill=(0.13, 0.25, 0.47), radius=0.5)
    return doc.tobytes(deflate=True)  # 실제 입력처럼 이미 압축된 이미지 스트림


def template() -> RegionTemplate:
    return RegionTemplate(
        regions=[
            Region("이름", RatioRect(0.15, 0.58, 0.6, 0.12), "text"),
            Region("본문", RatioRect(0.15, 0.78, 0.6, 0.12), "text"),
        ],
        protect=[Region("띠", RatioRect(0.0, 0.0, 0.1, 1.0), "protect")],
    )


def test_erase_page_removes_text_and_image_text_keeps_size():
    data = make_card_pdf()
    doc = pymupdf.open(stream=data, filetype="pdf")
    page = doc[0]
    bg = find_background_image(page)
    before = load_image_rgb(doc, bg.xref)
    stream_before = doc.xref_stream_raw(bg.xref)
    render_before, _ = render_on_image_grid(page, bg, before)
    r = erase_page(page, template(), seed=0)

    assert not [s for s in page.get_text("words") if s[4] == "HELLO"]
    # 원본 배경 이미지 스트림은 바이트 그대로
    bg2 = find_background_image(page)
    assert bg2.xref == bg.xref
    assert doc.xref_stream_raw(bg.xref) == stream_before
    # 정리 결과는 패치로 들어감
    assert r.meta["patches"]
    cleaned = r.cleaned
    assert cleaned[600:640, 150:400].mean() > 150  # 이미지 속 글자도 지워짐
    assert np.array_equal(cleaned[:, :60], before[:, :60])  # 보호 띠 그대로
    # 렌더링: 패치 밖은 원본과 완전히 같고, 패치 안은 정리 결과와 같다 (격자 정렬)
    render_after, _ = render_on_image_grid(page, bg, before)
    outside = np.ones(before.shape[:2], bool)
    for p in r.meta["patches"]:
        x0, y0, x1, y1 = p["bbox_px"]
        outside[y0:y1, x0:x1] = False
    # 저장(deflate) 뒤 패치는 무손실 Flate, 원본 이미지 스트림은 그대로
    saved = pymupdf.open(stream=doc.tobytes(garbage=3, deflate=True), filetype="pdf")
    sp = saved[0]
    sbg = find_background_image(sp)
    assert saved.xref_stream_raw(sbg.xref) == stream_before
    patch_xrefs = [i["xref"] for i in sp.get_image_info(xrefs=True) if i["xref"] not in (0, sbg.xref)]
    assert len(patch_xrefs) == len(r.meta["patches"])
    assert all(saved.xref_get_key(x, "Filter")[1] == "/FlateDecode" for x in patch_xrefs)
    assert np.array_equal(render_after[outside], render_before[outside])
    x0, y0, x1, y1 = [p for p in r.meta["patches"] if p["region"] == "본문"][0]["bbox_px"]
    assert np.array_equal(render_after[y0:y1, x0:x1], cleaned[y0:y1, x0:x1])
    names = [m["name"] for m in r.meta["regions"]]
    assert names == ["이름", "본문"]
    name, body = r.meta["regions"]
    assert name["text_layer"]["present"] and name["text_layer"]["size_pt"] == pytest.approx(18, abs=0.1)
    assert not body["text_layer"]["present"] and body["missing_from_text_layer"]
    # 벡터 도형(영역 밖 라벨 모양)은 레드랙션에 지워지지 않음
    assert page.get_drawings()


def test_remove_inline_images_only_inside_rect():
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=100)
    stream = (
        b"q 0.72 0 0 0.72 50 50 cm .2 .2 .2 rg BI /W 3 /H 3 /IM true /BPC 1 /F /AHx ID bf1f1f> EI Q\n"
        b"q 0.72 0 0 0.72 150 50 cm BI /W 3 /H 3 /IM true /BPC 1 /F /AHx ID bf1f1f> EI Q\n"
    )
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, stream)
    doc.xref_set_key(page.xref, "Contents", f"{xref} 0 R")
    page = doc[0]
    assert len(page.get_image_info()) == 2
    # 페이지 좌표: PDF y=50 -> 위에서 50 - 0.72
    removed = remove_inline_images(page, [(40, 40, 60, 60)])
    assert len(removed) == 1
    assert len(page.get_image_info()) == 1


def test_ensure_not_source(tmp_path):
    src = tmp_path / "source" / "a.pdf"
    src.parent.mkdir()
    src.write_bytes(b"x")
    with pytest.raises(PermissionError):
        ensure_not_source(src, src)
    with pytest.raises(PermissionError):
        ensure_not_source(src.parent / "b.pdf", src)
    ensure_not_source(tmp_path / "out" / "b.pdf", src)


def test_patch_rect_on_pixel_grid():
    bg = BackgroundImage(xref=1, bbox=(0.0, -0.24, 252.0, 177.84), size=(1050, 742))
    r = patch_rect_pt((94, 458, 422, 515), bg)
    s = 1050 / 252.0
    assert (r[0] - bg.bbox[0]) * s == pytest.approx(94)
    assert (r[1] - bg.bbox[1]) * 742 / 178.08 == pytest.approx(458)
    assert (r[2] - bg.bbox[0]) * s == pytest.approx(422)
    assert (r[3] - bg.bbox[1]) * 742 / 178.08 == pytest.approx(515)
