"""실제 카드 회귀 비교 도구(scripts/regress.py): 그림·상자 비교, 대상 찾기, 기준 저장 -> 비교, 입력 데이터 바뀜 구분."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from app import service, workspace
from scripts import regress

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
SANS_B = ROOT / "fonts/notosanskr/NotoSansKR-Bold.ttf"


# ---------------------------------------------------------------- 그림·상자 비교
def test_compare_images_ratio_diff_and_size_mismatch(tmp_path):
    a = Image.new("RGB", (100, 100), "white")
    a.save(tmp_path / "a.png")
    a.save(tmp_path / "same.png")
    b = a.copy()
    ImageDraw.Draw(b).rectangle([0, 0, 9, 9], fill="black")  # 100화소 = 1%
    b.save(tmp_path / "b.png")
    c = a.copy()
    ImageDraw.Draw(c).point((50, 50), fill=(250, 250, 250))  # 아주 작은 색 차이는 바뀐 화소가 아니다
    c.save(tmp_path / "c.png")
    Image.new("RGB", (100, 90), "white").save(tmp_path / "small.png")

    assert regress.compare_images(tmp_path / "a.png", tmp_path / "same.png") == 0.0
    assert regress.compare_images(tmp_path / "a.png", tmp_path / "c.png") == 0.0
    assert regress.compare_images(tmp_path / "a.png", tmp_path / "b.png", tmp_path / "d.png") == pytest.approx(0.01)
    d = Image.open(tmp_path / "d.png").convert("RGB")
    assert d.getpixel((5, 5)) == (230, 0, 60) and d.getpixel((50, 50)) != (230, 0, 60)  # 바뀐 곳만 빨강
    assert regress.compare_images(tmp_path / "a.png", tmp_path / "small.png") == 1.0


def test_compare_boxes():
    assert regress.compare_boxes([[0, 0, 10, 10]], [[0, 0, 10, 10]]) == 0
    assert regress.compare_boxes([[0, 0, 10, 10]], [[2, 0, 10, 15]]) == 5
    assert regress.compare_boxes([[0, 0, 10, 10]], []) >= 10 ** 6


def test_case_folder_has_no_path_separators():
    c = regress.Case("pipeline:게임/그룹/파일", "pipeline", Path("."))
    assert c.folder == "pipeline__게임__그룹__파일"


# ---------------------------------------------------------------- 시트 자르기
def _sheet(path: Path, shift: int = 0) -> Path:
    """카드 두 장(가로 배열, 틈 있음). shift만큼 두 번째 카드를 옮긴다."""
    im = Image.new("RGB", (900, 600), "white")
    d = ImageDraw.Draw(im)
    for x in (30, 480 + shift):
        d.rectangle([x, 30, x + 390, 540], fill=(200, 60, 60))
    im.save(path, dpi=(300, 300))
    return path


def test_sheet_case_save_then_check_same_and_changed(tmp_path):
    img = _sheet(tmp_path / "s.png")
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps({"sheets": [{"label": "시트", "glob": "s.png", "cols": 2, "rows": 1}]}),
                          encoding="utf-8")
    cases = regress.discover(projects_roots=[], cases_file=cases_file, root=tmp_path)
    assert [(c.name, c.kind, c.cols, c.rows) for c in cases] == [("sheet:시트/s.png", "sheet", 2, 1)]
    out = tmp_path / "out"
    logs = []
    assert [o.state for o in regress.save(cases, out, log=logs.append)] == ["same"]
    base = out / "baseline" / cases[0].folder
    assert (base / "boxes.png").exists() and len(json.loads((base / "summary.json").read_text("utf-8"))["boxes"]) == 2

    res, run_dir = regress.check(cases, out, log=logs.append)
    assert [o.state for o in res] == ["same"]
    assert (run_dir / "report.html").exists()

    _sheet(img, shift=40)  # 시트 그림이 바뀜 -> 상자가 움직였고, 입력이 바뀐 것도 알린다
    res, run_dir = regress.check(cases, out, log=logs.append)
    assert [o.state for o in res] == ["input"]
    assert res[0].details[0]["state"] == "input" and "px 움직임" in res[0].details[1]["item"]
    report = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "sheet:시트/s.png" in report and "boxes-diff.png" in report
    assert json.loads((run_dir / "report.json").read_text("utf-8"))[0]["state"] == "input"

    # 같은 그림인데 상자만 다르면(코드가 바뀜) ‘바뀜’
    base = out / "baseline" / cases[0].folder
    regress.save(cases, out, log=logs.append)
    s = json.loads((base / "summary.json").read_text("utf-8"))
    s["boxes"][1][0] += 10
    (base / "summary.json").write_text(json.dumps(s), encoding="utf-8")
    res, _ = regress.check(cases, out, log=logs.append)
    assert [o.state for o in res] == ["changed"]


def test_failed_save_keeps_previous_baseline(tmp_path, monkeypatch):
    img = _sheet(tmp_path / "s.png")
    case = regress.Case("sheet:a/s.png", "sheet", img, 2, 1)
    out = tmp_path / "out"
    regress.save([case], out, log=lambda *_: None)
    good = (out / "baseline" / case.folder / "summary.json").read_text("utf-8")
    monkeypatch.setattr(regress, "run_sheet", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert [o.state for o in regress.save([case], out, log=lambda *_: None)] == ["failed"]
    assert (out / "baseline" / case.folder / "summary.json").read_text("utf-8") == good


def test_check_without_baseline_is_new_and_failure_is_reported(tmp_path):
    img = _sheet(tmp_path / "s.png")
    ok = regress.Case("sheet:a/s.png", "sheet", img, 2, 1)
    bad = regress.Case("sheet:a/none.png", "sheet", tmp_path / "none.png", 2, 1)
    res, run_dir = regress.check([ok, bad], tmp_path / "out", log=lambda *_: None)
    assert [o.state for o in res] == ["new", "failed"]
    assert res[1].error
    assert "실패" in (run_dir / "report.html").read_text(encoding="utf-8")
    # 실패한 저장은 기준을 남기지 않는다
    assert [o.state for o in regress.save([bad], tmp_path / "out", log=lambda *_: None)] == ["failed"]
    assert not (tmp_path / "out" / "baseline" / bad.folder).exists()


# ---------------------------------------------------------------- 지우기 -> 한글 넣기 (프로젝트)
def _pipeline_project(tmp_path):
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    im = Image.new("RGB", (900, 600), "white")
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(str(SANS_B), 40)
    for x in (30, 480):
        d.rectangle([x, 30, x + 390, 540], fill=(240, 235, 220))
        d.text((x + 30, 60), "CARD NAME", font=f, fill=(20, 20, 20))
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    im.save(tmp_path / "in" / "s.png", dpi=(300, 300))
    r = service.add_images(info.dir, [tmp_path / "in" / "s.png"], "A", cols=2, rows=1, new_group=True)
    for g in service.project_overview(info.dir)["groups"]:
        if g["name"] == "A":
            service.save_region_template(g["dir"], {"regions": [
                {"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.15}}]})
    pages = r.file_dir / "extract" / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    for p in (1, 2):
        (pages / f"page-{p:03d}.json").write_text(json.dumps({
            "page": p, "model": "m", "regions": {"이름": {"source": "CARD NAME", "uncertain": False, "note": ""}}}),
            encoding="utf-8")
    return info, r.file_dir


@pytest.mark.skipif(not SANS_B.exists(), reason="fonts/ 없음 (scripts/fetch_fonts.py)")
def test_pipeline_case_compares_pages_and_flags_input_changes(tmp_path):
    info, fd = _pipeline_project(tmp_path)
    cases = regress.discover(projects_roots=[info.dir.parent], cases_file=None, root=tmp_path)
    pipe = [c for c in cases if c.kind == "pipeline"]
    sheet = [c for c in cases if c.kind == "sheet"]
    assert [c.name for c in pipe] == ["pipeline:p/A/s"] and [(c.cols, c.rows) for c in sheet] == [(2, 1)]

    before = regress.snapshot(info.dir)
    out = tmp_path / "out"
    assert [o.state for o in regress.save(pipe, out, log=lambda *_: None)] == ["same"]
    base = out / "baseline" / pipe[0].folder
    names = sorted(p.name for p in base.glob("*.png"))
    assert names == ["composed-p001.png", "composed-p002.png", "erased-p001.png", "erased-p002.png"]
    summary = json.loads((base / "summary.json").read_text("utf-8"))
    assert summary["pages"] == [1, 2] and "file/extract/pages/page-001.json" in summary["inputs"]
    assert not any(k.startswith("file/cleared/") for k in summary["inputs"])

    res, _ = regress.check(pipe, out, log=lambda *_: None)
    assert [o.state for o in res] == ["same"]  # 같은 입력이면 같은 결과
    assert regress.snapshot(info.dir) == before  # 프로젝트 폴더는 읽기만 한다

    # 원문이 바뀌면(사용자가 앱에서 고침) 결과 차이와 함께 ‘입력 데이터도 바뀜’으로 알린다
    p1 = fd / "extract" / "pages" / "page-001.json"
    d = json.loads(p1.read_text("utf-8"))
    d["regions"]["이름"]["source"] = "OTHER"
    p1.write_text(json.dumps(d), encoding="utf-8")
    res, run_dir = regress.check(pipe, out, log=lambda *_: None)
    o = res[0]
    assert o.details[0]["state"] == "input" and "page-001.json" in o.details[0]["item"]
    assert o.state in ("input", "same")
    if o.state == "input":
        assert "입력도 바뀜" in (run_dir / "report.html").read_text(encoding="utf-8")


def test_project_roots_include_installed_data_folder(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Documents" / "CardTranslater" / "projects").mkdir(parents=True)
    (tmp_path / "repo" / "projects").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    roots = regress.project_roots(tmp_path / "repo")
    assert roots[0] == tmp_path / "repo" / "projects"
    assert home / "Documents" / "CardTranslater" / "projects" in roots
    assert len(roots) == len({r.resolve() for r in roots})


def test_main_list_and_no_cases(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(regress, "discover", lambda: [regress.Case("sheet:x/a.png", "sheet", tmp_path / "a.png")])
    assert regress.main(["list"]) == 0
    assert "sheet:x/a.png" in capsys.readouterr().out
    assert regress.main(["check", "--only", "없는이름"]) == 2
