"""화면과 상관없는 작업 흐름(app.service): 진행률, 취소, 확인이 필요한 상태, 화면 라이브러리 분리."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pymupdf
import pytest

from app import service
from app.service import CancelToken, Cancelled
from tests.test_erase_pdf import make_card_pdf

ROOT = Path(__file__).resolve().parent.parent
GAEGU = ROOT / "fonts/gaegu/Gaegu-Bold.ttf"


def make_project(tmp: Path, pages: int = 2) -> Path:
    p = tmp / "proj"
    (p / "source").mkdir(parents=True)
    one = pymupdf.open(stream=make_card_pdf(), filetype="pdf")
    doc = pymupdf.open()
    for _ in range(pages):
        doc.insert_pdf(one)
    doc.save(p / "source" / "cards.pdf")
    style = {"color": "#08070b", "font": {"file": str(GAEGU)}}
    (p / "regions.json").write_text(json.dumps({"regions": [
        {"name": "이름", "rect": {"x": 0.15, "y": 0.58, "w": 0.6, "h": 0.12}, "kind": "text", "style": style},
    ], "protect_rects": [{"name": "띠", "rect": {"x": 0, "y": 0, "w": 0.1, "h": 1}}]}), encoding="utf-8")
    (p / "samples.json").write_text(json.dumps({"cards": [
        {"page": i + 1, "이름": {"source": "HELLO", "ko": "안녕"}} for i in range(pages)
    ]}, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.mark.skipif(not GAEGU.exists(), reason="fonts/ 없음 (scripts/fetch_fonts.py)")
def test_erase_and_compose_report_progress_and_write_project_folders(tmp_path):
    proj = make_project(tmp_path)
    events = []
    r = service.erase(proj, "1-2", progress=events.append)
    assert r.ok and r.output_dir == proj / "cleared" and (proj / "cleared" / "cleared.pdf").exists()
    erase_ev = [e for e in events if e.stage == "erase"]
    assert [(e.done, e.total, e.page) for e in erase_ev] == [(0, 2, 1), (1, 2, 2)]
    assert events[-1].stage == "done"

    events.clear()
    c = service.compose(proj, progress=events.append)
    assert c.state == "done" and (proj / "translated" / "translated.pdf").exists()
    assert [e.page for e in events if e.stage == "compose"] == [1, 2]
    # 작업용 숨은 폴더가 남지 않는다
    assert not [d for d in proj.iterdir() if d.name.startswith(".")]


def test_cancel_keeps_previous_result_and_removes_work_folder(tmp_path):
    proj = make_project(tmp_path)
    assert service.erase(proj, "1").ok
    before = (proj / "cleared" / "run.json").read_text(encoding="utf-8")

    tok = CancelToken()

    def cancel_on_second(ev):
        if ev.stage == "erase" and ev.done == 1:
            tok.cancel()

    with pytest.raises(Cancelled):
        service.erase(proj, "1-2", progress=cancel_on_second, cancel=tok)
    assert (proj / "cleared" / "run.json").read_text(encoding="utf-8") == before  # 예전 결과 그대로
    assert not [d for d in proj.iterdir() if d.name.startswith(".")]


def test_compose_without_cleared_is_blocked_not_an_exception(tmp_path):
    proj = make_project(tmp_path)
    r = service.compose(proj)
    assert r.state == "blocked" and "지운 결과가 없습니다" in r.blocking[0]


def test_compose_asks_before_using_excel_with_issues(tmp_path):
    from openpyxl import load_workbook

    proj = make_project(tmp_path)
    assert service.erase(proj, "1-2").ok
    x = service.export_excel(proj)
    with pytest.raises(FileExistsError):  # 검수한 엑셀을 덮어쓰지 않는다
        service.export_excel(proj)
    wb = load_workbook(x)
    ws = wb["번역"]
    ws.cell(2, [c.value for c in ws[1]].index("번역") + 1).value = None  # 첫 행 번역 비우기
    wb.save(x)
    r = service.compose(proj, excel=x)
    assert r.state == "needs_confirmation" and any("번역이 비어 있음" in i for i in r.issues)
    assert not (proj / "translated").exists()
    if GAEGU.exists():
        r = service.compose(proj, excel=x, accept_issues=True)
        assert r.state == "done"


def test_engine_does_not_depend_on_any_ui():
    """app/의 엔진 모듈은 화면 라이브러리를 import하지 않고 print하지 않는다 (cli.py만 예외).
    그래야 같은 엔진을 웹 화면과 PySide6 화면이 그대로 쓸 수 있다 (PRD 6장)."""
    ui_libs = {"PySide6", "PyQt5", "PyQt6", "fastapi", "uvicorn", "starlette", "tkinter", "flask"}
    for f in sorted((ROOT / "app").glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for n in names:
                    assert n.split(".")[0] not in ui_libs, f"{f.name}: {n}"
            if f.name != "cli.py" and isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print":
                pytest.fail(f"{f.name}:{node.lineno} print 대신 progress 콜백을 쓴다")


def test_set_engine_saves_only_the_engine(tmp_path, monkeypatch):
    """로그인 방식만 바꾸고 다른 설정은 그대로 둔다 (테스트용 설정 파일만 씀)."""
    from app import settings

    monkeypatch.setenv("CARDTRANSLATER_SETTINGS", str(tmp_path / "settings.json"))
    before = settings.load()
    other = before.model if hasattr(before, "model") else None
    service.set_engine("api")
    after = settings.load()
    assert after.engine == "api"
    if other is not None:
        assert after.model == other
    service.set_engine("claude-code")
    assert settings.load().engine == "claude-code"
