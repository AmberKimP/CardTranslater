"""용어 변경 일괄 반영 (PRD F20): 용어집에서 번역을 바꾸면 그 용어가 든 칸을 찾아 한꺼번에 바꾼다."""

from __future__ import annotations

import hashlib
import json

from app import card_edits, glossary, service, workspace
from app.translate import _stale, load_cache, save_cache
from tests.test_glossary import _pdf_and_regions
from tests.test_translate import _write_extract

GOLD = {"source": "Gold", "ko": "금화", "category": "기타", "variants": ["ゴールド"]}
DISCARD = {"source": "捨てる", "ko": "버리다", "category": "행동"}


def _entry(ko, used):
    return {"ko": ko, "status": [], "terms_used": used, "by": "claude-sonnet-5-5"}


def _add_project(tmp_path, name, texts, cache):
    pdf, regions = _pdf_and_regions(tmp_path / name)
    info, _ = workspace.create_project(name, "G", root=tmp_path)
    workspace.add_files(info, [pdf], "카드", new_group=True, regions_from=regions)
    fd = workspace.file_dirs(info)[0]
    for i, t in enumerate(texts, 1):
        _write_extract(fd, i, t)
    save_cache(info.dir, cache)
    workspace.set_status(info, "translated")
    return info, fd


def _set_excel(xlsx, page, ko):
    """검수 엑셀의 그 쪽 ‘번역’ 칸을 사람이 고친 것처럼 바꾼다."""
    from openpyxl import load_workbook

    wb = load_workbook(xlsx)
    for ws in wb.worksheets:
        head = [c.value for c in ws[1]]
        if "key" not in head or "번역" not in head:
            continue
        for row in ws.iter_rows(min_row=2):
            if str(row[head.index("key")].value or "").endswith(f"#p{page}#이름"):
                row[head.index("번역")].value = ko
    wb.save(xlsx)


def _game(tmp_path):
    """p: 검수 엑셀이 있는 프로젝트 (1쪽은 엑셀에서 고침, 2쪽은 결과 보기에서 고침).
    q: 엑셀을 만든 뒤 넣은 프로젝트 (초벌 번역만, 2쪽은 옛 용어가 활용된 꼴로만 있음)."""
    p, fd_p = _add_project(tmp_path, "p", ["Goldを得る", "ゴールドを払う"], {
        "Goldを得る": _entry("금화를 얻는다", {"Gold": "금화"}),
        "ゴールドを払う": _entry("금화를 낸다", {"Gold": "금화"})})
    glossary.confirm(p, [GOLD, DISCARD])
    service.export_project_excel(p.dir)
    xlsx = p.game_dir / "번역_검수.xlsx"
    _set_excel(xlsx, 1, "금화 1개를 얻는다")
    card_edits.set_text(fd_p, 2, "이름", "금화를 지불한다", "금화를 낸다")
    q, fd_q = _add_project(tmp_path, "q", ["Goldを得る", "カードを捨てる"], {
        "Goldを得る": _entry("금화를 얻는다", {"Gold": "금화"}),
        "カードを捨てる": _entry("카드를 버린다", {"捨てる": "버리다"})})
    c = glossary.confirm(p, [{**GOLD, "ko": "골드"}, {**DISCARD, "ko": "폐기하다"}])
    workspace.set_status(p, "glossary_confirmed")
    return p, fd_p, q, fd_q, xlsx, c["changed_detail"]


def test_confirm_reports_changed_detail(tmp_path):
    p, *_rest, changes = _game(tmp_path)
    assert {c["source"]: (c["old"], c["new"]) for c in changes} == {"Gold": ("금화", "골드"), "捨てる": ("버리다", "폐기하다")}
    assert next(c for c in changes if c["source"] == "Gold")["variants"] == ["ゴールド"]


def test_candidates_in_review_and_draft_cells(tmp_path):
    p, fd_p, q, fd_q, _x, changes = _game(tmp_path)
    rows = service.term_change_candidates(p.dir, changes)
    got = {(r["project"], r["page"]): r for r in rows}
    assert set(got) == {("p", 1), ("p", 2), ("q", 1), ("q", 2)}
    # 검수 엑셀에서 고친 번역을 본다
    assert got[("p", 1)]["where"] == "review" and got[("p", 1)]["current"] == "금화 1개를 얻는다"
    assert got[("p", 1)]["proposed"] == "골드 1개를 얻는다" and got[("p", 1)]["auto"]
    # 이형태(ゴールド)로 든 용어, 결과 보기에서 고친 번역 위에
    assert got[("p", 2)]["current"] == "금화를 지불한다" and got[("p", 2)]["proposed"] == "골드를 지불한다"
    assert got[("q", 1)]["where"] == "draft" and got[("q", 1)]["proposed"] == "골드를 얻는다"
    # 옛 번역이 활용된 꼴로만 있으면 직접 고칠 칸
    assert got[("q", 2)]["auto"] is False and got[("q", 2)]["proposed"] is None and got[("q", 2)]["terms"] == ["捨てる"]


def test_apply_writes_card_edits_keeps_excel_updates_cache_and_restores_status(tmp_path):
    p, fd_p, q, fd_q, xlsx, changes = _game(tmp_path)
    before = hashlib.sha256(xlsx.read_bytes()).hexdigest()
    rows = service.term_change_candidates(p.dir, changes)
    picks = [r for r in rows if r["auto"]]
    r = service.apply_term_changes(p.dir, picks, changes, prev_status="translated")
    assert r.applied == 3 and r.skipped == 0 and r.status_restored
    assert workspace.load_project_info(p.dir).status == "translated"
    assert hashlib.sha256(xlsx.read_bytes()).hexdigest() == before  # 검수 엑셀은 그대로
    edits = card_edits.load(fd_p)
    assert edits[1]["이름"] == {"ko": "골드 1개를 얻는다", "base": "금화 1개를 얻는다"}
    assert edits[2]["이름"] == {"ko": "골드를 지불한다", "base": "금화를 낸다"}  # base는 엑셀 값 그대로
    # 초벌 번역 캐시도 새 용어로 (⑥ 전체 번역이 다시 번역하지 않음)
    terms = glossary.load_glossary(p).get("terms", [])
    cache_p = load_cache(p.dir)
    assert cache_p["Goldを得る"]["ko"] == "골드를 얻는다" and cache_p["Goldを得る"]["terms_used"]["Gold"] == "골드"
    assert not any(_stale(e, s, terms) for s, e in cache_p.items())
    cache_q = load_cache(q.dir)
    assert cache_q["Goldを得る"]["ko"] == "골드를 얻는다" and not _stale(cache_q["Goldを得る"], "Goldを得る", terms)
    assert _stale(cache_q["カードを捨てる"], "カードを捨てる", terms)  # 직접 고칠 칸은 다시 번역 대상으로 남는다
    assert card_edits.load(fd_q) == {}  # 초벌 번역 칸은 캐시만 고치면 된다
    # 한글 넣기가 쓰는 번역에 새 용어
    for fd, want in ((fd_p, {1: "골드 1개를 얻는다", 2: "골드를 지불한다"}), (fd_q, {1: "골드를 얻는다"})):
        out, notes = card_edits.apply(fd, service._translation_cards(fd))
        assert {pg: out[pg]["이름"]["ko"] for pg in want} == want and notes == []
    assert service.term_change_candidates(p.dir, changes) == [r for r in rows if not r["auto"]]  # 남은 것은 직접 고칠 칸


def test_unpicked_draft_cell_keeps_its_translation(tmp_path):
    p, fd_p, q, fd_q, _x, changes = _game(tmp_path)
    rows = service.term_change_candidates(p.dir, changes)
    picks = [r for r in rows if r["auto"] and r["project"] == "p"]
    r = service.apply_term_changes(p.dir, picks, changes, prev_status="translated")
    assert r.applied == 2
    assert load_cache(q.dir)["Goldを得る"]["ko"] == "금화를 얻는다"  # 고르지 않은 칸의 번역은 그대로
    out, _n = card_edits.apply(fd_q, service._translation_cards(fd_q))
    assert out[1]["이름"]["ko"] == "금화를 얻는다"


def test_status_not_restored_when_term_left_stale(tmp_path):
    p, fd_p, *_rest, changes = _game(tmp_path)
    cache = load_cache(p.dir)
    cache["Goldの山"] = _entry("금화가 쌓인다", {"Gold": "금화"})  # 활용형이 아니어도 고르지 않으면 남는다
    save_cache(p.dir, cache)
    _write_extract(fd_p, 2, "Goldの山")
    rows = service.term_change_candidates(p.dir, changes)
    picks = [r for r in rows if r["project"] == "p" and r["page"] == 1]
    r = service.apply_term_changes(p.dir, picks, changes, prev_status="translated")
    assert r.applied == 1 and not r.status_restored
    assert workspace.load_project_info(p.dir).status == "glossary_confirmed"


def test_swap_terms_lists_and_longest_first():
    ch = [{"old": "금", "new": "골드"}, {"old": "금화", "new": "골드 주화"}]
    assert service._swap_terms("금화와 금", ch) == "골드 주화와 골드"
    assert service._swap_terms(["금화", "불"], ch) == ["골드 주화", "불"]
    assert card_edits.norm(service._swap_terms(["금화", "불"], ch)) == "골드 주화\n불"
    # 바로 뒤 조사는 새 번역의 받침에 맞춘다 (조사 뒤에 한글이 이어지면 그대로)
    g = [{"old": "금화", "new": "황금"}]
    assert service._swap_terms("금화를 얻고 금화가 금화로 금화와", g) == "황금을 얻고 황금이 황금으로 황금과"
    assert service._swap_terms("금화이다", g) == "황금이다"
    assert service._swap_terms("불로", [{"old": "불", "new": "물"}]) == "물로"
    assert service._swap_terms("물을", [{"old": "물", "new": "불"}]) == "불을"


def test_import_glossary_result_has_detail_and_prev_status(tmp_path, monkeypatch):
    p, *_rest = _game(tmp_path)
    workspace.set_status(p, "translated")
    terms = [{**GOLD, "ko": "황금"}, {**DISCARD, "ko": "폐기하다"}]
    monkeypatch.setattr(glossary, "import_review", lambda info, x: glossary.ReviewImport(terms=terms))
    r = service.import_glossary(p.dir, "x.xlsx")
    assert r.state == "confirmed" and r.prev_status == "translated" and r.changed == ["Gold"]
    assert r.prev_statuses == {str(p.dir): "translated"}
    assert r.changed_detail == [{"source": "Gold", "old": "골드", "new": "황금", "variants": ["ゴールド"],
                                 "category": "기타"}]
    assert json.loads((p.dir / "project.json").read_text(encoding="utf-8"))["status"] == "glossary_confirmed"


# ---------------------------------------------------------------- 화면 (offscreen)
def _qapp():
    import os

    import pytest

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _sync_worker(monkeypatch):
    """작업 스레드를 그 자리에서 돌린다 (테스트가 기다리지 않게)."""
    from app.qt import worker

    def run(parent, title, job, done, fail=None, **k):
        job.signals.done.connect(done)
        if fail:
            job.signals.failed.connect(fail)
        job.run()
    monkeypatch.setattr(worker, "run_with_dialog", run)


def test_dialog_checks_auto_rows_and_applies_picked(tmp_path, monkeypatch):
    _qapp()
    _sync_worker(monkeypatch)
    from PySide6.QtCore import Qt

    from app.qt.term_changes import MANUAL_NOTE, TermChangesDialog

    p, fd_p, q, fd_q, _x, changes = _game(tmp_path)
    rows = service.term_change_candidates(p.dir, changes)
    dlg = TermChangesDialog(p.dir, changes, rows, "translated")
    states = [(dlg.table.item(i, 0).checkState(), bool(dlg.table.item(i, 0).flags() & Qt.ItemFlag.ItemIsEnabled))
              for i in range(len(rows))]
    assert states == [(Qt.CheckState.Checked, True) if r["auto"] else (Qt.CheckState.Unchecked, False) for r in rows]
    manual = next(i for i, r in enumerate(rows) if not r["auto"])
    assert MANUAL_NOTE in dlg.table.cellWidget(manual, 4).text()
    assert "<b" in dlg.table.cellWidget(0, 4).text()  # 바뀐 곳 강조
    assert dlg.ok.text() == "선택한 칸 바꾸기" and dlg.count.text() == "바꿀 칸: 3개"
    q_row = next(i for i, r in enumerate(rows) if r["project"] == "q" and r["auto"])
    dlg.table.item(q_row, 0).setCheckState(Qt.CheckState.Unchecked)
    assert dlg.count.text() == "바꿀 칸: 2개"
    dlg.apply()
    assert dlg.result.applied == 2 and dlg.result.status_restored
    assert load_cache(q.dir)["Goldを得る"]["ko"] == "금화를 얻는다"


def test_import_glossary_step_opens_term_dialog(tmp_path, monkeypatch):
    _qapp()
    from PySide6.QtWidgets import QMessageBox

    from app.qt import steps, term_changes

    p, fd_p, q, fd_q, _x, changes = _game(tmp_path)
    glossary.confirm(p, [GOLD, DISCARD])  # 번역이 아직 옛 용어(금화·버리다)를 쓰는 때로
    workspace.set_status(p, "translated")
    terms = [{**GOLD, "ko": "황금"}, DISCARD | {"ko": "폐기하다"}]
    monkeypatch.setattr(glossary, "import_review", lambda info, x: glossary.ReviewImport(terms=terms))

    def run(parent, title, job, done, fail=None, **k):
        job.signals.done.connect(done)
        job.run()
    monkeypatch.setattr(steps.worker, "run_with_dialog", run)
    seen, shown = [], []

    def fake_exec(self):
        seen.append([r["proposed"] for r in self.rows])
        self.apply()
        return 1
    monkeypatch.setattr(term_changes.TermChangesDialog, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[2])))

    from PySide6.QtWidgets import QWidget

    class Page(QWidget):
        project_dir = p.dir

        def refresh(self):
            pass
    page = Page()
    acts = steps.StepActions.__new__(steps.StepActions)
    acts.page = page
    monkeypatch.setattr(steps.StepActions, "pdir", property(lambda self: p.dir))
    acts.import_glossary(str(tmp_path / "x.xlsx"))
    assert {x for x in seen[0] if x} == {"황금 1개를 얻는다", "황금을 지불한다", "황금을 얻는다"}
    assert "3칸을 바꿨습니다" in shown[-1] and "다시 번역할 필요가 없습니다" in shown[-1]
    assert workspace.load_project_info(p.dir).status == "translated"


def test_dialog_failure_shows_problem_and_keeps_dialog_open(tmp_path, monkeypatch):
    _qapp()
    _sync_worker(monkeypatch)
    from app.qt import term_changes

    p, fd_p, q, fd_q, _x, changes = _game(tmp_path)
    rows = service.term_change_candidates(p.dir, changes)
    dlg = term_changes.TermChangesDialog(p.dir, changes, rows, "translated")
    shown = []
    monkeypatch.setattr(term_changes, "show_failure", lambda parent, title, msg: shown.append(title))

    def boom(*a, **k):
        raise ValueError("망가진 캐시")
    monkeypatch.setattr(service, "apply_term_changes", boom)
    dlg.apply()
    assert shown == ["바뀐 용어를 반영하지 못했습니다"] and dlg.result is None and dlg.ok.isEnabled()


# ---------------------------------------------------------------- 잘못 바꾸지 않기 (코드 리뷰)
def _plan(ko, source, old, new, extra=()):
    terms = [{"source": "X", "ko": new}, *extra]
    return service._term_plan(ko, source, [{"source": "X", "old": old, "new": new}], terms)


def test_new_containing_old_is_not_swapped_again():
    assert service._swap_terms("장검과 검을 든다", [{"source": "X", "old": "검", "new": "장검"}]) == "장검과 장검을 든다"
    pl = _plan("대마법과 마법을 쓴다", "X", "마법", "대마법")
    assert pl["auto"] and pl["proposed"] == "대마법과 대마법을 쓴다"
    assert _plan("대마법을 쓴다", "X", "마법", "대마법")["used"] == []  # 이미 새 번역 (후보 아님)


def test_new_part_of_old_is_offered_and_cache_agrees(tmp_path):
    p, fd = _add_project(tmp_path, "p", ["魔法の矢を撃つ"], {"魔法の矢を撃つ": _entry("마법 화살을 쏜다", {"魔法の矢": "마법 화살"})})
    glossary.confirm(p, [{"source": "魔法の矢", "ko": "마법 화살", "category": "카드 이름"}])
    c = glossary.confirm(p, [{"source": "魔法の矢", "ko": "화살", "category": "카드 이름"}])
    rows = service.term_change_candidates(p.dir, c["changed_detail"])
    assert [(r["page"], r["auto"], r["proposed"]) for r in rows] == [(1, True, "화살을 쏜다")]
    service.apply_term_changes(p.dir, rows, c["changed_detail"])
    assert load_cache(p.dir)["魔法の矢を撃つ"]["ko"] == "화살을 쏜다"
    out, _n = card_edits.apply(fd, service._translation_cards(fd))
    assert out[1]["이름"]["ko"] == "화살을 쏜다"


def test_single_syllable_old_is_always_manual():
    # 바꾸기 자체는 낱말 경계만 (다른 낱말 안의 한 글자는 건드리지 않음)
    assert service._swap_terms("지금 금을", [{"source": "X", "old": "금", "new": "금화"}]) == "지금 금화를"
    assert service._swap_terms("불가능한 불로", [{"source": "X", "old": "불", "new": "화염"}]) == "불가능한 화염으로"
    # 그래도 한 글자 옛 번역은 자동으로 바꾸지 않는다
    for ko, old, new in (("지금 금을 얻는다", "금", "금화"), ("금에는 금을", "금", "황금"), ("적은 피해를 준 적에게", "적", "원수"),
                         ("사용 불가. 불을 끈다", "불", "화염"), ("물이다 물인", "물", "액체"), ("<b>금</b>을", "금", "금화"),
                         ("금", "금", "황금")):
        pl = _plan(ko, "X", old, new)
        assert pl["used"] == ["X"] and not pl["auto"] and pl["proposed"] is None, ko
    assert _plan("지금 얻는다", "X", "금", "금화")["used"] == []  # 다른 낱말 안의 한 글자는 후보가 아님
    # 두 글자 이상 옛 번역이 다른 글자에 붙어 있으면 직접 고칠 칸
    pl = _plan("화염구를 쏜다", "X", "화염", "불")
    assert pl["used"] == ["X"] and not pl["auto"] and pl["proposed"] is None


def test_auto_only_when_swaps_match_source_count():
    # 원문에 한 번인데 번역에서 바꿀 자리가 두 번 보이면 직접 고칠 칸
    pl = _plan("마법은 마법을 부른다", "X", "마법", "주문")
    assert not pl["auto"] and pl["used"] == ["X"]
    # 원문에 두 번이고 두 번 바꿨으면 자동
    terms = [{"source": "Mana", "ko": "체력"}]
    ch = [{"source": "Mana", "old": "마나", "new": "체력"}]
    pl = service._term_plan("마나를 내고 마나를 얻는다", "Mana, Mana", ch, terms)
    assert pl["auto"] and pl["proposed"] == "체력을 내고 체력을 얻는다"
    # 원문에 두 번인데 하나만 바꿀 수 있으면 직접 고칠 칸
    pl = service._term_plan("마나를 낸다", "Mana, Mana", ch, terms)
    assert not pl["auto"] and pl["proposed"] is None
    # 바꾸지 못한 옛 번역이 남으면 직접 고칠 칸 (목록에 없는 조사)
    pl = service._term_plan("마나조차 없다", "Mana", ch, terms)
    assert not pl["auto"] and pl["used"] == ["Mana"]


def test_chained_changes_are_manual():
    def plan(ko, src, terms, changes):
        g = [{"source": s_, "ko": k} for s_, k in terms]
        return service._term_plan(ko, src, [{"source": s_, "old": o, "new": n} for s_, o, n in changes], g)
    for args in (("검과 방패", "Sword and Shield", [("Sword", "방패"), ("Shield", "검")],
                  [("Sword", "검", "방패"), ("Shield", "방패", "검")]),
                 ("검과 장검", "Sword and Longsword", [("Sword", "장검"), ("Longsword", "대검")],
                  [("Sword", "검", "장검"), ("Longsword", "장검", "대검")]),
                 ("검과 방패", "Sword and Shield", [("Sword", "방패"), ("Shield", "방벽")],
                  [("Sword", "검", "방패"), ("Shield", "방패", "방벽")])):
        pl = plan(*args)
        assert not pl["auto"] and pl["proposed"] is None and sorted(pl["used"]) == sorted(s_ for s_, _k in args[2])


def test_josa_pairs_and_closing_tag():
    ch = [{"source": "Mana", "old": "마나", "new": "금"}]
    terms = [{"source": "Mana", "ko": "금"}]
    assert service._term_plan("마나나 체력", "Mana", ch, terms)["proposed"] == "금이나 체력"
    assert service._term_plan("마나라 부른다", "Mana", ch, terms)["proposed"] == "금이라 부른다"
    ch = [{"source": "Mana", "old": "마나", "new": "체력"}]
    pl = service._term_plan("<b>마나</b>를 얻는다", "Mana", ch, [{"source": "Mana", "ko": "체력"}])
    assert pl["auto"] and pl["proposed"] == "<b>체력</b>을 얻는다"


def test_manual_cache_entries_stay_stale_and_block_restore(tmp_path):
    """직접 고칠 칸·일부만 바꿀 수 있는 항목은 캐시를 건드리지 않아 ⑥이 다시 번역하고, 상태도 되돌리지 않는다."""
    gold = {"source": "Gold", "ko": "금", "category": "기타"}
    p, fd = _add_project(tmp_path, "p", ["Goldを得る", "SwordとShield"], {
        "Goldを得る": _entry("금에는 금을 얻는다", {"Gold": "금"}),
        "SwordとShield": _entry("검과 방패", {"Sword": "검", "Shield": "방패"})})
    glossary.confirm(p, [gold, {"source": "Sword", "ko": "검", "category": "기타"},
                         {"source": "Shield", "ko": "방패", "category": "기타"}])
    c = glossary.confirm(p, [{**gold, "ko": "황금"}, {"source": "Sword", "ko": "방패", "category": "기타"},
                             {"source": "Shield", "ko": "검", "category": "기타"}])
    workspace.set_status(p, "glossary_confirmed")
    rows = service.term_change_candidates(p.dir, c["changed_detail"])
    assert [r["auto"] for r in rows] == [False, False]
    r = service.apply_term_changes(p.dir, rows, c["changed_detail"], prev_status="translated")
    assert r.applied == 0 and r.skipped == 2 and not r.status_restored
    assert workspace.load_project_info(p.dir).status == "glossary_confirmed"
    terms = glossary.load_glossary(p).get("terms", [])
    cache = load_cache(p.dir)
    assert cache["SwordとShield"]["terms_used"] == {"Sword": "검", "Shield": "방패"}
    assert all(_stale(cache[s_], s_, terms) for s_ in ("Goldを得る", "SwordとShield"))


def test_changed_term_inside_longer_terms_translation_is_kept():
    door = {"source": "ガストラップドア", "ko": "가스 트랩도어"}
    trap = [{"source": "トラップ", "old": "트랩", "new": "함정"}]
    terms = [door, {"source": "トラップ", "ko": "함정"}]
    # 원문에 긴 용어 안에만 있으면 바뀐 용어가 아니다
    assert service._term_plan("가스 트랩도어를 연다", "ガストラップドアを開く", trap, terms)["hits"] == []
    # 둘 다 있으면 긴 용어 번역은 지키고 따로 쓰인 것만 바꾼다
    pl = service._term_plan("가스 트랩도어의 트랩", "ガストラップドアのトラップ", trap, terms)
    assert pl["auto"] and pl["proposed"] == "가스 트랩도어의 함정"


def test_unseen_cache_entries_are_not_corrupted(tmp_path):
    p, fd_p, q, fd_q, _x, changes = _game(tmp_path)
    cache = load_cache(p.dir)
    cache["Goldの山"] = _entry("금화더미가 쌓인다", {"Gold": "금화"})  # 카드에 없는 원문, 안전하게 못 바꿈
    cache["Gold袋"] = _entry("금화 주머니", {"Gold": "금화"})  # 카드에 없는 원문, 안전하게 바꿈
    save_cache(p.dir, cache)
    rows = service.term_change_candidates(p.dir, changes)
    service.apply_term_changes(p.dir, [r for r in rows if r["auto"]], changes, prev_status="translated")
    cache = load_cache(p.dir)
    terms = glossary.load_glossary(p).get("terms", [])
    assert cache["Goldの山"]["ko"] == "금화더미가 쌓인다" and _stale(cache["Goldの山"], "Goldの山", terms)
    assert cache["Gold袋"]["ko"] == "골드 주머니" and not _stale(cache["Gold袋"], "Gold袋", terms)


def test_status_restored_per_project(tmp_path):
    p, fd_p, q, fd_q, _x, changes = _game(tmp_path)
    cache = load_cache(q.dir)
    cache["カードを捨てる"] = _entry("카드를 버리다.", {"捨てる": "버리다"})  # 글자 그대로라 바꿀 수 있음
    save_cache(q.dir, cache)
    workspace.set_status(q, "glossary_confirmed")
    rows = service.term_change_candidates(p.dir, changes)
    prev = {str(p.dir): "translated", str(q.dir): "translating"}
    r = service.apply_term_changes(p.dir, [r for r in rows if r["auto"]], changes, prev_status=prev)
    assert sorted(r.restored) == ["p", "q"] and r.status_restored
    assert workspace.load_project_info(q.dir).status == "translating"
    assert load_cache(q.dir)["カードを捨てる"]["ko"] == "카드를 폐기하다."
