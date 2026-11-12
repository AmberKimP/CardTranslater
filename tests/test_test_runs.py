"""시험 실행 (PRD 5.9): 어려운 카드 고르기 · 시험 기록(최근 5번) · 두 시험 비교 · 쪽 지정."""

import json
import os
from pathlib import Path

import pytest

from app import service, workspace
from tests.test_service import make_project

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CARDTRANSLATER_SETTINGS", str(tmp_path / "settings.json"))


def _group_project(tmp_path, pages=6):
    src = make_project(tmp_path / "src", pages=pages)
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    workspace.add_files(info, [src / "source" / "cards.pdf"], "카드", new_group=True, regions_from=src / "regions.json")
    return info, workspace.file_dirs(info)[0]


def _extract(fd: Path, page: int, **regions):
    d = fd / "extract" / "pages"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"page-{page:03d}.json").write_text(json.dumps({
        "page": page, "model": "m",
        "regions": {k: {"source": v, "uncertain": False, "note": ""} for k, v in regions.items()}},
        ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 어려운 카드 고르기
def test_pick_hard_cards_counts_and_distinct(tmp_path):
    _info, fd = _group_project(tmp_path)
    _extract(fd, 1, 이름="짧다")
    # 태그·아이콘을 빼면 10자: 태그 글자로 길어 보여도 길지 않다
    _extract(fd, 2, 이름="<b>가나다라마</b><i>바사아자차</i>")
    _extract(fd, 3, 이름="가" * 30 + "[[coin]][[coin]][[?]]")  # 가장 길고 아이콘도 가장 많다
    _extract(fd, 4, 이름="가" * 20, 효과="[[sword]]")
    _extract(fd, 5, 이름="[[a]][[b]]x")
    _extract(fd, 6, 이름="가" * 99)  # 뺀 쪽: 고르지 않는다
    workspace.set_excluded_pages(fd, [6], "규칙 카드")

    picks = service.pick_hard_cards([fd], n_long=2, n_icons=2)
    # 3쪽(아이콘 3개)·4쪽(1개)은 이미 긴 카드로 골랐으니 아이콘 목록은 다음 카드부터, 아이콘 없는 카드는 안 고른다
    assert [(p["page"], p["reason"]) for p in picks] == [
        (3, "글자 가장 긴 카드 (30자)"), (4, "글자 가장 긴 카드 (20자)"), (5, "아이콘 가장 많은 카드 (2개)")]
    picks = service.pick_hard_cards([fd], n_long=1, n_icons=2)
    assert [(p["page"], p["reason"]) for p in picks] == [
        (3, "글자 가장 긴 카드 (30자)"), (5, "아이콘 가장 많은 카드 (2개)"), (4, "아이콘 가장 많은 카드 (1개)")]
    assert picks[0]["file_key"] == workspace.file_key(fd) and Path(picks[0]["file_dir"]) == fd
    assert all(p["page"] != 6 for p in picks)
    assert service._source_counts("<b>가나</b> [[coin]]\n다<c1>라</c1>") == (4, 1)


def test_pick_hard_cards_falls_back_to_fewer(tmp_path):
    _info, fd = _group_project(tmp_path)
    _extract(fd, 1, 이름="가나다")
    picks = service.pick_hard_cards([fd])
    assert [(p["page"], p["reason"]) for p in picks] == [(1, "글자 가장 긴 카드 (3자)")]  # 아이콘 카드 없음
    assert service.pick_hard_cards([tmp_path / "없는 폴더"]) == []


def test_parse_test_pages_validates(tmp_path):
    _info, fd = _group_project(tmp_path)
    assert service.parse_test_pages(fd, "1, 3-4") == [1, 3, 4]
    workspace.set_excluded_pages(fd, [2])
    for bad, msg in (("abc", "처럼 적으세요"), ("1 2", "처럼 적으세요"), ("1-2-3", "처럼 적으세요"), ("-3", "처럼 적으세요"),
                     ("3-", "처럼 적으세요"), ("9", "범위"), ("2", "뺀"), ("4-3", "잘못된 범위")):
        with pytest.raises(ValueError, match=msg):
            service.parse_test_pages(fd, bad)


def test_parse_test_pages_respects_card_limit(tmp_path):
    """시험 모드(앞 N장만 추출·번역)면 그 밖의 쪽은 쪽 지정으로 고를 수 없다."""
    info, fd = _group_project(tmp_path)
    service.set_card_limit(info.dir, 2)
    assert service.parse_test_pages(fd, "1-2") == [1, 2]
    with pytest.raises(ValueError, match="아직 추출·번역하지 않은 쪽"):
        service.parse_test_pages(fd, "2, 5")


def test_test_file_dir_handles_both_separators_and_stays_in_files(tmp_path):
    _info, fd = _group_project(tmp_path)
    gdir = fd.parent.parent
    assert service._test_file_dir(gdir, "카드/cards") == fd
    assert service._test_file_dir(gdir, "카드\\cards") == fd
    assert service._test_file_dir(gdir, "..") is None and service._test_file_dir(gdir, "") is None
    assert service._test_file_dir(gdir, "카드/..") is None


def test_failed_or_cancelled_selection_removes_its_folders(tmp_path, monkeypatch):
    """끝내지 못한 시험은 기록에 남지 않으므로, 그동안 만든 시험 폴더를 지운다 (test_runs 안만)."""
    monkeypatch.setattr(service, "data_dir", lambda: tmp_path / "data")
    root = tmp_path / "data" / "test_runs"
    old = root / "old-compose"  # 이전 시험 폴더는 그대로
    old.mkdir(parents=True)
    calls = []

    def fake(fd, pages, *, out_root=None, progress=None, cancel=None):
        n = len(calls)
        calls.append(fd)
        comp, er = root / f"{n}-compose", root / f"{n}-erase"
        er.mkdir(parents=True)
        if n == 1:
            return service.TestRunResult("blocked", erase_dir=er, reason="지우기 실패")
        if n == 2:
            raise service.Cancelled()
        comp.mkdir()
        return service.TestRunResult("done", comp, er, [1])

    monkeypatch.setattr(service, "test_run", fake)
    _info, fd = _group_project(tmp_path)
    r = service.test_run_selection([(fd, [1]), (fd, [2])], "쪽 1, 2")
    assert r.state == "blocked" and r.reason == "지우기 실패" and r.runs == []
    assert sorted(p.name for p in root.iterdir()) == ["old-compose"]
    assert service.test_history(fd.parent.parent) == []
    with pytest.raises(service.Cancelled):
        service.test_run_selection([(fd, [3])], "쪽 3")
    assert sorted(p.name for p in root.iterdir()) == ["old-compose"]


# ---------------------------------------------------------------- 시험 기록
def _entry(tmp, n, base):
    comp, er = base / f"{n}-compose", base / f"{n}-erase"
    comp.mkdir(parents=True)
    er.mkdir(parents=True)
    return {"label": f"앞 {n}장", "cards": [{"file_key": "카드/cards", "page": 1}],
            "runs": [{"file_key": "카드/cards", "compose_dir": str(comp), "erase_dir": str(er)}], "summary": {}}


def test_history_keeps_five_and_deletes_only_under_test_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "data_dir", lambda: tmp_path / "data")
    runs = tmp_path / "data" / "test_runs"
    outside = tmp_path / "elsewhere"
    gdir = tmp_path / "group"
    first = _entry(tmp_path, 0, outside)  # 시험 폴더 밖: 밀려나도 지우지 않는다
    service._record_test(gdir, first)
    entries = [_entry(tmp_path, i, runs) for i in range(1, 7)]
    ids = [service._record_test(gdir, e)["id"] for e in entries]
    hist = service.test_history(gdir)
    assert [e["id"] for e in hist] == ids[::-1][:5] and len(set(ids)) == 6  # 새것부터 5개, id는 서로 다름
    assert hist[0]["label"] == "앞 6장" and hist[0]["at"]
    assert [p.name for p in gdir.iterdir()] == [service.TEST_HISTORY_FILE]  # 임시 파일이 남지 않는다
    assert Path(first["runs"][0]["compose_dir"]).is_dir() and Path(first["runs"][0]["erase_dir"]).is_dir()
    assert not (runs / "1-compose").exists() and not (runs / "1-erase").exists()  # 밀려난 시험 폴더는 지움
    assert (runs / "2-compose").exists()
    # test_runs 폴더 자체나 그 바깥은 지우지 않는다
    assert service._remove_test_dir(runs) is False and service._remove_test_dir(outside / "0-compose") is False
    assert runs.is_dir() and (outside / "0-compose").is_dir()


def test_history_drops_missing_and_broken_file(tmp_path):
    import shutil

    gdir = tmp_path / "g"
    a = service._record_test(gdir, _entry(tmp_path, 1, tmp_path / "r"))
    b = service._record_test(gdir, _entry(tmp_path, 2, tmp_path / "r"))
    shutil.rmtree(a["runs"][0]["compose_dir"])
    assert [e["id"] for e in service.test_history(gdir)] == [b["id"]]
    (gdir / service.TEST_HISTORY_FILE).write_text("{깨짐", encoding="utf-8")
    assert service.test_history(gdir) == []
    assert service.rerun_test(gdir, "없음").state == "blocked"


def _fake_run(base: Path, name: str, pages: dict) -> Path:
    """pages: {쪽: [상태]} -> report.json·p###.png가 있는 시험 한글 넣기 폴더."""
    d = base / name
    d.mkdir(parents=True)
    detail = []
    for p, st in pages.items():
        (d / f"p{p:03d}.png").write_bytes(b"png")
        detail.append({"page": p, "regions": {"이름": {"status": st}}})
    (d / "report.json").write_text(json.dumps({"pages_detail": detail}, ensure_ascii=False), encoding="utf-8")
    return d


def test_compare_tests_pairs_common_cards_with_align_status(tmp_path):
    _info, fd = _group_project(tmp_path)
    gdir = fd.parent.parent
    key = workspace.file_key(fd)
    ra = _fake_run(tmp_path / "runs", "a", {1: ["넘침"], 2: []})
    rb = _fake_run(tmp_path / "runs", "b", {2: ["글자 축소"], 3: []})
    a = service._record_test(gdir, {"label": "앞 2장", "cards": [{"file_key": key, "page": 1}, {"file_key": key, "page": 2}],
                                    "runs": [{"file_key": key, "compose_dir": str(ra), "erase_dir": ""}], "summary": {}})
    b = service._record_test(gdir, {"label": "쪽 2-3", "cards": [{"file_key": key, "page": 2}, {"file_key": key, "page": 3}],
                                    "runs": [{"file_key": key, "compose_dir": str(rb), "erase_dir": ""}], "summary": {}})
    (fd / "align.json").write_text(json.dumps({"pages": {"2": {"status": "check"}}}), encoding="utf-8")
    rows = service.compare_tests(gdir, a["id"], b["id"])
    assert len(rows) == 1
    r = rows[0]
    assert (r["file_key"], r["page"]) == (key, 2)
    assert r["a_png"] == ra / "p002.png" and r["b_png"] == rb / "p002.png"
    assert r["a_status"] == ["정렬 확인 필요"] and r["b_status"] == ["글자 축소", "정렬 확인 필요"]
    assert service.compare_tests(gdir, a["id"], "없음") == []
    (fd / "align.json").write_text("깨짐", encoding="utf-8")  # 정렬 파일이 깨져도 비교는 된다
    assert service.compare_tests(gdir, a["id"], b["id"])[0]["a_status"] == []


# ---------------------------------------------------------------- 실제 시험 실행 (작은 프로젝트)
def test_test_run_selection_records_history_and_rerun(tmp_path, monkeypatch):
    from tests.test_qt import _translated_project

    monkeypatch.setattr(service, "data_dir", lambda: tmp_path / "data")
    info, fd = _translated_project(tmp_path)
    gdir = fd.parent.parent
    (fd / "align.json").write_text(json.dumps({"pages": {"2": {"status": "check"}, "1": {"status": "ok"}}}),
                                   encoding="utf-8")
    r = service.test_run_selection([(fd, [2])], "쪽 2")
    assert r.state == "done" and r.runs[0].pages == [2]
    assert r.summary.get("정렬 확인 필요") == 1
    assert r.entry["label"] == "쪽 2" and r.entry["cards"] == [{"file_key": workspace.file_key(fd), "page": 2}]
    assert Path(r.entry["runs"][0]["compose_dir"]).parent == tmp_path / "data" / "test_runs"
    assert not (fd / "translated").exists()  # 프로젝트 결과는 그대로
    r2 = service.test_run_cards([fd], 1)
    assert r2.state == "done" and r2.entry["label"] == "앞 1장" and r2.runs[0].pages == [1]
    r3 = service.rerun_test(gdir, r.entry["id"])
    assert r3.state == "done" and r3.entry["label"] == "쪽 2" and r3.entry["id"] != r.entry["id"]
    hist = service.test_history(gdir)
    assert [e["id"] for e in hist] == [r3.entry["id"], r2.entry["id"], r.entry["id"]]
    rows = service.compare_tests(gdir, r.entry["id"], r3.entry["id"])
    assert [(x["page"], x["a_png"].exists(), x["b_png"].exists()) for x in rows] == [(2, True, True)]


# ---------------------------------------------------------------- 화면 (offscreen)
def test_test_dialogs_smoke(qapp, tmp_path, monkeypatch):
    from app.qt.results import GroupPickDialog, OutputActions, TestCompareDialog, TestHistoryDialog, summary_text

    _info, fd = _group_project(tmp_path)
    _extract(fd, 1, 이름="가" * 10 + "[[a]]")
    _extract(fd, 2, 이름="가" * 3)
    f = {"included": 6, "erased": True, "translated": True, "composed": False, "dir": str(fd), "key": "카드/cards"}
    g = {"name": "카드", "dir": str(fd.parent.parent), "files": [f]}
    dlg = GroupPickDialog("시험 실행", [g], OutputActions._can_test, cards=True)
    assert dlg.test_mode() == "front" and not dlg.file_combo.isVisible()
    dlg.mode.button(1).setChecked(True)
    dlg.pages_edit.setText("1, x")
    dlg._ok()
    assert dlg.error.isVisibleTo(dlg) and "처럼 적으세요" in dlg.error.text() and dlg.result() == 0
    dlg.pages_edit.setText("1, 3-4")
    sel, label = dlg.selection()
    assert sel == [(str(fd), [1, 3, 4])] and label == "쪽 1, 3-4"
    dlg.mode.button(2).setChecked(True)
    assert dlg.test_mode() == "hard" and "p1" in dlg.picked.text() and "아이콘 가장 많은" not in dlg.picked.text()
    sel, label = dlg.selection()
    assert sel == [(str(fd), [1, 2])] and label == "어려운 카드 2장"
    assert summary_text({"글자 축소": 2, "넘침": 1, "아이콘 오류": 1, "정렬 확인 필요": 1}) == \
        "넘침 1·글자 축소 2·아이콘 불일치 1·정렬 확인 필요 1"

    # 시험 기록 창: 줄 고르기에 따라 버튼
    gdir = fd.parent.parent
    ra = _fake_run(tmp_path / "runs", "a", {1: ["넘침"]})
    rb = _fake_run(tmp_path / "runs", "b", {1: []})
    for d, lab in ((ra, "앞 1장"), (rb, "쪽 1")):
        service._record_test(gdir, {"label": lab, "cards": [{"file_key": "카드/cards", "page": 1}],
                                    "runs": [{"file_key": "카드/cards", "compose_dir": str(d), "erase_dir": ""}],
                                    "summary": {"넘침": 1} if d == ra else {}})
    called = []
    h = TestHistoryDialog(gdir, on_rerun=lambda gd, eid: called.append(eid))
    assert h.table.rowCount() == 2 and h.table.item(1, 2).text() == "넘침 1" and h.table.item(0, 2).text() == "없음"
    assert h.b_view.isEnabled() and h.b_rerun.isEnabled() and not h.b_compare.isEnabled()
    h.table.selectRow(1)
    assert h.b_compare.isEnabled() and not h.b_view.isEnabled()
    shown = {}
    monkeypatch.setattr(TestCompareDialog, "exec", lambda self: shown.setdefault("rows", self.rows))
    h.compare()
    assert len(shown["rows"]) == 1 and shown["rows"][0]["a_status"] == ["넘침"]
    h.table.clearSelection()
    h.table.selectRow(0)
    h.rerun()
    assert called == [h.entries[0]["id"]]

    c = TestCompareDialog([{"file_key": "카드/cards", "page": 1, "a_png": None, "b_png": None,
                            "a_status": ["넘침"], "b_status": []},
                           {"file_key": "카드/cards", "page": 2, "a_png": None, "b_png": None,
                            "a_status": [], "b_status": []}], "A", "B")
    assert "1/2" in c.pos.text() and not c.prev_btn.isEnabled() and "넘침" in c.st_a.text()
    c.go(1)
    assert "2/2" in c.pos.text() and not c.next_btn.isEnabled() and c.st_b.text() == "문제 없음"
