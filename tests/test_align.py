"""자동 정렬 (PRD 5.8, F21): 합성 카드로 이동·크기 찾기, 영역 적용, align.json, 손으로 맞추기·되돌리기."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pymupdf
import pytest

from app import align, service, workspace
from app.project import load_project
from app.regions import RatioRect, align_rect, unalign_rect

W, H = 180.0, 250.0


def _card(page, ox: float = 0.0, oy: float = 0.0, s: float = 1.0, seed: int = 0) -> None:
    """같은 형식의 카드 (테두리·제목 띠·글 상자). 카드마다 그림(동그라미)과 글은 다르다.
    (ox, oy, s): 기준 카드의 점 (x, y)가 이 카드의 (ox + s*x, oy + s*y)에 오게 그린다."""
    def r(x0, y0, x1, y1):
        return pymupdf.Rect(ox + s * x0, oy + s * y0, ox + s * x1, oy + s * y1)

    page.draw_rect(r(12, 12, 168, 238), color=(0.2, 0.2, 0.2), width=2.2)
    page.draw_rect(r(18, 18, 162, 232), color=(0.4, 0.3, 0.1), width=1.0)
    page.draw_rect(r(30, 24, 150, 48), color=(0.1, 0.1, 0.1), fill=(0.85, 0.75, 0.5), width=1.2)
    page.draw_rect(r(24, 150, 156, 226), color=(0.1, 0.1, 0.1), fill=(0.95, 0.93, 0.88), width=1.2)
    page.draw_line(pymupdf.Point(ox + s * 40, oy + s * 188), pymupdf.Point(ox + s * 140, oy + s * 188),
                   color=(0.3, 0.3, 0.3), width=0.8)
    rnd = random.Random(seed)
    for _ in range(6):  # 카드마다 다른 그림
        c = pymupdf.Point(ox + s * rnd.uniform(40, 140), oy + s * rnd.uniform(60, 135))
        page.draw_circle(c, s * rnd.uniform(5, 18), color=(rnd.random(), rnd.random(), rnd.random()), width=1.5)
    page.insert_text((ox + s * 34, oy + s * 168), f"card text {seed} " * 2, fontsize=7 * s)


def _other(page, seed: int = 9) -> None:
    """전혀 다른 쪽 (테두리·글 상자 없이 글과 동그라미만)."""
    rnd = random.Random(seed)
    for i in range(14):
        page.insert_text((rnd.uniform(5, 120), rnd.uniform(15, 245)), f"rules {i} " * 3, fontsize=8)
    for _ in range(8):
        page.draw_circle(pymupdf.Point(rnd.uniform(20, 160), rnd.uniform(20, 230)), rnd.uniform(4, 30),
                         color=(0, 0, 0), width=1.2)


def _pdf(path: Path, cards: list) -> Path:
    doc = pymupdf.open()
    for c in cards:
        pg = doc.new_page(width=W, height=H)
        if c == "other":
            _other(pg)
        else:
            _card(pg, *c)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def _gray(pdf: Path, page: int):
    with pymupdf.open(pdf) as d:
        return align.render_gray(d[page - 1])


# ---------------------------------------------------------------- 맞추기
def test_estimate_recovers_shift_and_scale(tmp_path):
    pdf = _pdf(tmp_path / "c.pdf", [(0, 0, 1.0, 1), (4.0, -5.0, 1.0, 2), (-3.0, 2.5, 1.03, 3), "other"])
    ref = _gray(pdf, 1)
    t = align.estimate(ref, _gray(pdf, 2))
    assert t["dx"] == pytest.approx(4.0 / W, abs=0.004) and t["dy"] == pytest.approx(-5.0 / H, abs=0.004)
    assert t["sx"] == pytest.approx(1.0, abs=0.006) and t["sy"] == pytest.approx(1.0, abs=0.006)
    assert t["status"] == align.STATUS_OK and t["score"] >= align.MIN_SCORE
    t = align.estimate(ref, _gray(pdf, 3))
    assert t["sx"] == pytest.approx(1.03, abs=0.006) and t["sy"] == pytest.approx(1.03, abs=0.006)
    assert t["dx"] == pytest.approx(-3.0 / W, abs=0.004) and t["dy"] == pytest.approx(2.5 / H, abs=0.004)
    assert t["status"] == align.STATUS_OK
    t = align.estimate(ref, _gray(pdf, 4))  # 다른 형식의 쪽
    assert t["status"] == align.STATUS_CHECK and not align.applied(t)


def test_classify_limits():
    ok = {"dx": 0.01, "dy": -0.01, "sx": 1.0, "sy": 1.0, "rot": 0.0, "score": 0.8}
    assert align.classify(ok) == align.STATUS_OK
    assert align.classify({**ok, "dx": 0.07}) == align.STATUS_CHECK  # 6% 넘게 이동
    assert align.classify({**ok, "sx": 1.09}) == align.STATUS_CHECK  # 8% 넘게 크기 변화
    assert align.classify({**ok, "rot": 2.5}) == align.STATUS_CHECK  # 2° 넘게 회전
    assert align.classify({**ok, "score": 0.3}) == align.STATUS_CHECK
    big = {**ok, "dx": 0.07, "status": align.STATUS_CHECK}
    assert align.applied(big) and not align.applied({**big, "score": 0.3})  # 점수가 낮으면 적용 안 함


def test_align_rect_roundtrip_and_clip():
    t = {"dx": 0.02, "dy": -0.01, "sx": 1.05, "sy": 0.98}
    r = RatioRect(0.1, 0.2, 0.3, 0.4)
    a = align_rect(r, t)
    assert (a.x, a.y, a.w, a.h) == pytest.approx((0.1 * 1.05 + 0.02, 0.2 * 0.98 - 0.01, 0.3 * 1.05, 0.4 * 0.98))
    b = unalign_rect(a, t)
    assert (b.x, b.y, b.w, b.h) == pytest.approx((r.x, r.y, r.w, r.h))
    c = align_rect(RatioRect(0.8, 0.0, 0.2, 0.1), {"dx": 0.05, "dy": -0.02, "sx": 1.0, "sy": 1.0})
    assert c.x + c.w <= 1.0 + 1e-9 and c.y >= 0.0 and c.w > 0 and c.h > 0  # 카드 밖은 잘라 낸다


# ---------------------------------------------------------------- 프로젝트
def _project(tmp_path, cards, regions_extra: dict | None = None):
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    service.add_files(info.dir, [_pdf(tmp_path / "in" / "cards.pdf", cards)], "카드", new_group=True)
    g = service.project_overview(info.dir)["groups"][0]
    fd = Path(g["files"][0]["dir"])
    data = {"source_language": "ja", "reference_page": 1,
            "regions": [{"name": "본문", "rect": {"x": 24 / W, "y": 150 / H, "w": 132 / W, "h": 76 / H}}],
            "protect_rects": [{"name": "보호", "rect": {"x": 0.1, "y": 0.05, "w": 0.1, "h": 0.05}}],
            **(regions_extra or {})}
    assert service.save_region_template(g["dir"], data) == []
    return Path(g["dir"]), fd


def test_align_group_applies_to_shared_regions_only(tmp_path):
    gdir, fd = _project(tmp_path, [(0, 0, 1.0, 1), (5.0, 4.0, 1.0, 2), (5.0, 4.0, 1.0, 3), "other"])
    tpl = json.loads((gdir / "regions.json").read_text(encoding="utf-8"))
    tpl["page_regions"] = {f"{fd.name}/3": {"regions": [{"name": "본문",
                                                        "rect": {"x": 0.2, "y": 0.6, "w": 0.5, "h": 0.2}}]}}
    assert service.save_region_template(gdir, tpl) == []
    before = load_project(fd)
    sig_before = service.erase_sig(before, 2, {}, None)
    events = []
    res = service.align_group([fd], progress=events.append)
    assert res.state == "done" and res.reference == {"file": fd.name, "page": 1}
    f = res.files[0]
    assert f["ok"] == [2] and f["check"] == [4] and res.ok_count == 1 and res.check_count == 1
    assert any(e.stage == "align" for e in events)
    data = json.loads((fd / "align.json").read_text(encoding="utf-8"))
    assert data["reference"] == {"file": fd.name, "page": 1}
    assert set(data["pages"]) == {"1", "2", "4"}  # 따로 영역 카드(3쪽)는 맞추지 않는다
    assert data["pages"]["1"]["status"] == "ref"
    p2 = data["pages"]["2"]
    assert p2["dx"] == pytest.approx(5 / W, abs=0.004) and p2["dy"] == pytest.approx(4 / H, abs=0.004)

    proj = load_project(fd)
    assert set(proj.template.align) == {2}  # 기준 카드·적용 안 하는 정렬(4쪽)은 빠진다
    r1, r2 = proj.template.regions_for(1)[0].rect, proj.template.regions_for(2)[0].rect
    assert r1 == before.template.regions_for(1)[0].rect
    assert r2.x == pytest.approx(r1.x + 5 / W, abs=0.005) and r2.y == pytest.approx(r1.y + 4 / H, abs=0.005)
    assert proj.template.protect_for(2)[0].rect.x == pytest.approx(0.1 + 5 / W, abs=0.005)
    assert proj.template.regions_for(3)[0].rect == RatioRect(0.2, 0.6, 0.5, 0.2)  # 카드별 영역은 그대로
    assert proj.template.regions[0].rect == r1  # 공통 영역 자체는 바뀌지 않는다
    assert proj.template.for_page(2).regions[0].rect == r2  # 지우기에 넘기는 템플릿도
    # 정렬이 바뀌면 그 쪽은 다시 지울 대상 (erase_sig가 영역 좌표를 본다)
    assert service.erase_sig(proj, 2, {}, None) != sig_before
    assert service.erase_sig(proj, 1, {}, None) == service.erase_sig(before, 1, {}, None)


def test_align_json_roundtrip_and_page_transforms(tmp_path):
    data = {"reference": {"file": "a", "page": 1}, "pages": {
        "1": {**align.IDENTITY, "score": 1.0, "status": "ref"},
        "2": {"dx": 0.01, "dy": 0.0, "sx": 1.0, "sy": 1.0, "rot": 0.0, "score": 0.8, "status": "ok"},
        "3": {"dx": 0.2, "dy": 0.1, "sx": 1.2, "sy": 1.0, "rot": 0.0, "score": 0.2, "status": "check"},
        "4": {"dx": 0.0, "dy": 0.003, "sx": 1.0, "sy": 1.0, "rot": 0.0, "score": 0.0, "status": "manual"},
        "x": {"dx": 1}}}
    align.save(tmp_path, data)
    back = align.load(tmp_path)
    assert back == data
    assert align.page_transforms(back) == {2: {"dx": 0.01, "dy": 0.0, "sx": 1.0, "sy": 1.0},
                                           4: {"dx": 0.0, "dy": 0.003, "sx": 1.0, "sy": 1.0}}
    (tmp_path / "align.json").write_text("{broken", encoding="utf-8")
    assert align.load(tmp_path) == {} and align.page_transforms({}) == {}
    align.save(tmp_path, {"reference": {}, "pages": {}})  # 맞춘 쪽이 없으면 파일을 지운다
    assert not (tmp_path / "align.json").exists()


def test_nudge_and_reset_alignment(tmp_path):
    gdir, fd = _project(tmp_path, [(0, 0, 1.0, 1), (3.0, 0.0, 1.0, 2), (0, 0, 1.0, 3)])
    service.align_group([fd])
    auto = service.align_info(fd)["pages"][2]
    assert auto["status"] == "ok" and auto["applied"]
    e = service.nudge_alignment(fd, 2, 0.01, -0.002)
    assert e["status"] == "manual" and e["dx"] == pytest.approx(auto["dx"] + 0.01)
    assert e["dy"] == pytest.approx(auto["dy"] - 0.002) and e["auto"]["status"] == "ok"
    e = service.nudge_alignment(fd, 2, 0.01, 0.0)
    assert e["dx"] == pytest.approx(auto["dx"] + 0.02)  # 자동 값은 처음 것 그대로 기억
    assert service.nudge_alignment(fd, 1, 0.01, 0.0) is None  # 기준 카드는 옮기지 않는다
    assert load_project(fd).template.align[2]["dx"] == pytest.approx(auto["dx"] + 0.02)
    # 다시 자동 정렬해도 손으로 맞춘 카드는 그대로
    service.align_group([fd])
    assert service.align_info(fd)["pages"][2]["status"] == "manual"
    back = service.reset_alignment(fd, 2)  # 손으로 맞춤 -> 자동 값
    assert back["status"] == "ok" and back["dx"] == pytest.approx(auto["dx"])
    assert service.reset_alignment(fd, 2) is None  # 자동 -> 정렬 없음
    assert 2 not in service.align_info(fd)["pages"] and 2 not in load_project(fd).template.align
    e = service.nudge_alignment(fd, 2, 0.0, 0.01)  # 정렬이 없던 카드도 손으로 맞출 수 있다
    assert e["status"] == "manual" and e["dx"] == 0.0 and e["dy"] == pytest.approx(0.01)
    service.reset_alignment(fd)  # 파일 전체
    assert not (fd / "align.json").exists() and load_project(fd).template.align == {}


def test_align_group_blocked_and_cancel(tmp_path):
    gdir, fd = _project(tmp_path, [(0, 0, 1.0, 1), (2.0, 2.0, 1.0, 2)])
    tok = service.CancelToken()
    tok.cancel()
    with pytest.raises(service.Cancelled):
        service.align_group([fd], cancel=tok)
    assert not (fd / "align.json").exists()  # 취소하면 아무것도 쓰지 않는다
    tpl = json.loads((gdir / "regions.json").read_text(encoding="utf-8"))
    tpl.update(layout="per_card", page_regions={f"{fd.name}/1": {"regions": tpl["regions"]}}, regions=[])
    assert service.save_region_template(gdir, tpl) == []
    res = service.align_group([fd])
    assert res.state == "blocked" and "카드마다" in res.reason and not (fd / "align.json").exists()


def test_reference_page_excluded_falls_back_to_first_included(tmp_path):
    gdir, fd = _project(tmp_path, [(0, 0, 1.0, 1), (0, 0, 1.0, 2), (2.0, 1.0, 1.0, 3)])
    service.set_pages(fd, [1])
    res = service.align_group([fd])
    assert res.reference["page"] == 2 and service.align_info(fd)["pages"][2]["status"] == "ref"
    assert 1 not in service.align_info(fd)["pages"]


def test_reference_is_first_file_by_name_whatever_the_caller_order(tmp_path):
    """align_group과 nudge_alignment가 같은 기준 파일을 고른다 (손으로 맞춘 카드가 다시 정렬 때 사라지지 않게)."""
    gdir, fd_a = _project(tmp_path, [(0, 0, 1.0, 1), (3.0, 0.0, 1.0, 2)])
    info = workspace.project_of(fd_a)
    service.add_files(info.dir, [_pdf(tmp_path / "in2" / "more.pdf", [(2.0, 1.0, 1.0, 3), (1.0, 2.0, 1.0, 4)])], "카드")
    fds = sorted(Path(f["dir"]) for f in service.project_overview(info.dir)["groups"][0]["files"])
    assert len(fds) == 2
    first, second = fds
    res = service.align_group([second, first])  # 화면과 다른 순서로 불러도
    assert res.reference == {"file": first.name, "page": 1}
    e = service.nudge_alignment(second, 2, 0.01, 0.0)
    assert e["status"] == "manual"
    service.align_group([second, first])
    assert service.align_info(second)["pages"][2]["status"] == "manual"  # 손으로 맞춘 것이 남는다
    (second / "align.json").unlink()  # 정렬이 없던 파일에서 손으로 맞추면 같은 기준을 적는다
    service.nudge_alignment(second, 2, 0.01, 0.0)
    assert align.load(second)["reference"] == res.reference


@pytest.mark.parametrize("bad", [
    {"reference": {"file": "a", "page": 1}, "pages": ["x"]},
    {"reference": "a", "pages": "oops"},
    {"pages": {"2": "x", "3": 5, "4": {"dx": "far", "status": "ok"}, "5": {"status": "check", "score": "hi"}}},
    ["not", "a", "dict"],
])
def test_malformed_align_json_is_treated_as_no_alignment(tmp_path, bad):
    gdir, fd = _project(tmp_path, [(0, 0, 1.0, 1), (3.0, 0.0, 1.0, 2)])
    (fd / "align.json").write_text(json.dumps(bad), encoding="utf-8")
    assert load_project(fd).template.align == {}  # 프로젝트 열기가 멈추지 않는다
    assert isinstance(service.align_info(fd)["pages"], dict)
    assert align.page_transforms({"pages": ["x"]}) == {} and align.page_transforms(["x"]) == {}
    e = service.nudge_alignment(fd, 2, 0.01, 0.0)  # 손으로 맞추기도 된다
    assert e["status"] == "manual" and align.load(fd)["reference"] == {"file": fd.name, "page": 1}
