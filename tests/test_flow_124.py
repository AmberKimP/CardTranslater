"""1.24 B: ② 아이콘 정의 → ③ 원문 확인 흐름.
B1 ②는 늘 아이콘 정의 표, B2 확정 뒤 묻지 않음 (test_qt), B3 ③ 뒤 지웠던 파일 다시 지우기, B4 문단 나눔 [====],
B5 두 번 읽어 같으면 ‘아이콘 고침’ 알림만, B6 문장 부호(、/，·。/．) 충돌 자동 선택."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import service
from app.extract import ICON_SETTLED_LABEL, MODELS, _agree_norm, prefer_punct, verification_label
from tests.test_batch_1180 import _changed_project

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _cell(fd: Path, page: int = 1) -> dict:
    return json.loads((fd / "extract" / "pages" / f"page-{page:03d}.json").read_text(encoding="utf-8"))[
        "regions"]["본문"]


def _r(source, uncertain=False):
    return {"model": "m", "source": source, "uncertain": uncertain, "note": "", "at": "t"}


def _acts(qapp, tmp_path, name):
    from app.qt.main import MainWindow

    info, fd = _changed_project(tmp_path, name)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    return info, fd, win.project.steps


# ---------------------------------------------------------------- B1
def test_icons_button_opens_review_table_even_when_all_confirmed(qapp, tmp_path, monkeypatch):
    from app.qt import steps

    _info, _fd, acts = _acts(qapp, tmp_path, "b1")
    opened, dicts, infos = [], [], []
    monkeypatch.setattr(service, "icon_review", lambda _p: {"groups": [{"group": "g"}], "known": []})
    monkeypatch.setattr(acts, "_open_icon_review", lambda review, from_finish=False: opened.append(review))
    monkeypatch.setattr(steps, "IconDictionaryDialog", lambda *a, **k: dicts.append(a))
    monkeypatch.setattr(steps.QMessageBox, "information", lambda *a, **k: infos.append(a))
    acts._icons_found(service.IconScanResult("done"))
    assert len(opened) == 1 and dicts == [] and infos == []  # 사전 창(2C)이 아니라 아이콘 정의 표(2A)

    # 아이콘이 하나도 없을 때만: 사전이 있으면 사전 창, 없으면 안내
    monkeypatch.setattr(service, "icon_review", lambda _p: None)
    monkeypatch.setattr(service, "icon_dictionary", lambda _p: [])
    acts._icons_found(service.IconScanResult("done"))
    assert len(opened) == 1 and dicts == [] and len(infos) == 1

    class FakeDict:
        def __init__(self, *a, **k):
            dicts.append(a)

        def exec(self):
            return False
    monkeypatch.setattr(steps, "IconDictionaryDialog", FakeDict)
    monkeypatch.setattr(service, "icon_review", lambda _p: {"groups": [], "known": []})
    monkeypatch.setattr(service, "icon_dictionary", lambda _p: [{"name": "coin"}])
    acts._icons_found(service.IconScanResult("done"))
    assert len(opened) == 1 and len(dicts) == 1


# ---------------------------------------------------------------- B2·B3: ③을 열 때 모아서 다시 읽고, 끝나면 지웠던 파일 다시 지우기
def test_review_sources_rereads_all_project_files_and_remembers_erase(qapp, tmp_path, monkeypatch):
    from app.qt import steps

    _info, fd, acts = _acts(qapp, tmp_path, "b3a")
    ran, opened = {}, []
    monkeypatch.setattr(acts, "_engine", lambda: "api")
    monkeypatch.setattr(acts.page, "scoped_files", lambda: [{"key": "other", "dir": tmp_path / "nothing"}])

    def fake_run(parent, title, job, on_done, on_failed=None, cancellable=True, **_kw):
        ran["cells"] = job.args[0]
        on_done({"done": 1, "failed": [], "state": "done", "reason": "", "retry_after": None, "spent_usd": 0.0})

    monkeypatch.setattr(steps.worker, "run_with_dialog", fake_run)
    monkeypatch.setattr(acts, "_open_source_review", lambda files, *a: opened.append(list(acts._erase_after)))
    acts.review_sources(only_check=True)
    # 그리드에서 고른 범위 밖의 파일이어도 바뀐 칸은 읽는다
    assert [(Path(c["file_dir"]), c["page"]) for c in ran["cells"]] == [(Path(fd), 1)]
    assert opened == [[str(fd)]]


def test_open_source_review_reerases_stale_erased_files(qapp, tmp_path, monkeypatch):
    from app.qt import bg_erase, steps

    _info, fd, acts = _acts(qapp, tmp_path, "b3b")

    class FakeDlg:
        saved = 0

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return 0

    jobs, shown = [], []
    monkeypatch.setattr(steps, "SourceReviewDialog", FakeDlg)
    monkeypatch.setattr(service, "stale_erase_files", lambda dirs, cells=None: [Path(d) for d in dirs])

    def fake_excl(parent, title, job, on_done, on_failed=None):
        jobs.append((job.fn, job.args, job.kwargs))
        on_done(service.ProjectStepResult("done", files=[{"file": "g/f", "state": "done", "pages": 1, "warnings": 0}]))

    monkeypatch.setattr(bg_erase, "run_exclusive", fake_excl)
    monkeypatch.setattr(steps.QMessageBox, "information", lambda *a, **k: shown.append(a))
    acts._erase_after = [str(fd)]
    acts._open_source_review([], True, None, None)
    assert len(jobs) == 1 and jobs[0][0] is service.erase_project and jobs[0][2]["files"] == [Path(fd)]
    assert acts._erase_after == [] and len(shown) == 1
    # 다시 열 때(바뀐 칸 없음)는 지우지 않는다
    acts._open_source_review([], True, None, None)
    assert len(jobs) == 1


def test_cancelled_reread_does_not_reerase_unverified_files(qapp, tmp_path, monkeypatch):
    """리뷰 1.24: 다시 읽기를 취소·실패해 ‘확인 필요’가 남은 파일은 확인 전 원문으로 다시 지우지 않는다 (알림만)."""
    from app.qt import bg_erase, steps
    from app.service import _translate_erase

    _info, fd, acts = _acts(qapp, tmp_path, "b3c")
    (Path(fd) / "cleared").mkdir(exist_ok=True)
    (Path(fd) / "cleared" / "cleared.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(_translate_erase, "stale_erased_pages", lambda _fd: [1])
    jobs, shown, opened = [], [], []

    class FakeDlg:
        saved = 0

        def __init__(self, *a, **k):
            opened.append(1)

        def exec(self):
            return 0

    monkeypatch.setattr(steps, "SourceReviewDialog", FakeDlg)
    monkeypatch.setattr(bg_erase, "run_exclusive", lambda *a, **k: jobs.append(a))
    monkeypatch.setattr(steps.QMessageBox, "information", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(acts, "_engine", lambda: "api")
    monkeypatch.setattr(steps.worker, "run_with_dialog",
                        lambda parent, title, job, on_done, on_failed=None, cancellable=True, on_cancelled=None:
                        on_cancelled())
    pending = service.icon_reread_pending([fd])
    assert pending
    acts.review_sources(only_check=True)  # 취소: 칸은 ‘확인 필요’(icon_changed) 그대로
    assert opened == [1] and jobs == [] and len(shown) == 1 and "다시 지우지 않았습니다" in shown[0][2]
    # 칸이 확인되면(사람이 저장) 그 파일은 다시 지운다
    assert service.stale_erase_files([fd], pending) == []
    for c in pending:
        service.save_source(c["file_dir"], c["page"], c["region"],
                            next(x for x in service.extracted_cells(c["file_dir"])
                                 if (x["page"], x["region"]) == (c["page"], c["region"]))["source"])
    assert service.stale_erase_files([fd], pending) == [Path(fd)]


def test_stale_erase_files_only_erased_and_stale(tmp_path, monkeypatch):
    from app.service import _translate_erase

    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
    (a / "cleared").mkdir()
    (a / "cleared" / "cleared.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(_translate_erase, "stale_erased_pages", lambda fd: [1])
    assert service.stale_erase_files([a, b]) == [a]  # 아직 안 지운 파일은 넣지 않는다
    monkeypatch.setattr(_translate_erase, "stale_erased_pages", lambda fd: [])
    assert service.stale_erase_files([a, b]) == []


# ---------------------------------------------------------------- B4: 문단 나눔 [====] = 칸의 paragraph_breaks
def test_default_paragraphs_do_not_split_at_sentence_end():
    from app.typeset_layout import paragraphs

    assert paragraphs("攻撃する。\n次に移動する。") == ["攻撃する。 次に移動する。"]  # 문장 끝만으로는 안 나눔
    assert len(paragraphs("A。\n\nB")) == 2  # 빈 줄
    assert len(paragraphs("効果：\n1枚引く")) == 2  # 짧은 머리 줄
    assert len(paragraphs("言葉だ\n——探検家")) == 2  # 말한 사람


def test_stored_breaks_define_paragraphs():
    from app.typeset_layout import paragraph_spans, paragraphs, wrap_sentence_fonts

    text = "一行目\n二行目\n\n三行目\n——四"
    assert paragraphs(text, [2]) == ["一行目 二行目", "三行目 ——四"]  # 정한 줄에서만 (빈 줄·‘——’ 무시)
    assert paragraphs(text, []) == ["一行目 二行目 三行目 ——四"]  # 빈 목록 = 한 문단
    assert paragraphs(text, [1, 9]) == ["一行目", "二行目 三行目 ——四"]  # 맞지 않는 번호는 버림
    assert len(paragraph_spans(text, [1, 3])) == 3
    out, fonts = wrap_sentence_fonts("가\n나\n다", {1: "x.ttf"}, [1])
    assert out == "가\n<f1>나</f1>\n<f1>다</f1>" and fonts == {"f1": "x.ttf"}


@pytest.mark.parametrize("text,breaks", [
    ("攻撃する。\n次に移動する", None), ("A。\n\nB", None), ("あいう\nえお", [1]), ("効果：\n1枚引く。\n[[coin]]2", [2]),
    ("一\n\n二\n三", []),
])
def test_paragraph_marks_round_trip(text, breaks):
    marked = service.mark_paragraphs(text, breaks)
    src, br = service.unmark_paragraphs(marked)
    assert src == text  # 원문 글은 그대로 (빈 줄을 넣거나 빼지 않음)
    assert br == service.paragraph_starts(text, breaks)


def test_paragraph_marks_insert_move_remove_without_touching_source():
    assert service.mark_paragraphs("攻撃する。\n次に移動する") == "攻撃する。\n次に移動する"  # 기본: 문장 끝은 안 나눔
    assert service.unmark_paragraphs("攻撃する。\n[====]\n次に移動する") == ("攻撃する。\n次に移動する", [1])  # 넣기
    assert service.mark_paragraphs("A\nB\nC", [1]) == "A\n[====]\nB\nC"
    assert service.unmark_paragraphs("A\nB\n[====]\nC") == ("A\nB\nC", [2])  # 옮기기
    assert service.unmark_paragraphs("A。\n\nB") == ("A。\n\nB", [])  # 빈 줄 나눔도 뺄 수 있다 (빈 줄은 그대로)
    assert service.unmark_paragraphs("[====]\nA\n\n[====]\nB") == ("A\n\nB", [1])  # 첫 줄 앞은 버림, 빈 줄 뒤 첫 글 줄


def test_breaks_for_text_maps_to_translation_and_partial_keep():
    from app.extract import breaks_for_text, with_paragraph_breaks

    cell = {"source": "一\n\n二\n三\n四", "paragraph_breaks": [1, 3]}
    assert breaks_for_text(cell, "가\n나\n다\n라") == [1, 3]  # 줄 수(빈 줄 뺌)가 같은 번역문
    assert breaks_for_text(cell, "가\n나") is None  # 맞출 수 없으면 기본 규칙
    assert breaks_for_text({"source": "一\n二"}, "가\n나") is None  # 정한 적 없음
    # 아이콘 문단만 원본 그대로 (원문 줄 0·2 남김: 빈 줄 뺀 0·1): 번역 원문은 三·四 → 3에서 시작하던 문단은 1번
    kp = {"source": "一\n\n二\n三\n四", "paragraph_breaks": [1, 3],
          "keep_paragraphs": {"lines": [0, 2], "n": 5}}
    assert breaks_for_text(kp, "三\n四") == [1]
    cards = {1: {"본문": {"ko": "가\n나\n다\n라", "source": "一\n二\n三\n四"}, "이름": {"ko": "x"}}}
    got = with_paragraph_breaks(cards, {1: {"본문": cell, "이름": {"source": "x"}}})
    assert got[1]["본문"]["paragraph_breaks"] == [1, 3] and got[1]["본문"]["source_paragraph_breaks"] == [1, 3]
    assert "paragraph_breaks" not in got[1]["이름"] and "paragraph_breaks" not in cards[1]["본문"]  # 원래 dict 그대로


def test_stored_breaks_drive_keep_lines_font_plan_and_blocks():
    from app import font_plan
    from app.extract import keep_paragraph_lines
    from app.typeset_compose import sentence_blocks

    src = "攻撃する。\n[[coin]]を得る。\n次に移動する。"
    assert keep_paragraph_lines(src) == [0, 1, 2]  # 기본: 한 문단 (문장 끝으로 안 나눔) → 칸 전체
    assert keep_paragraph_lines(src, [1, 2]) == [1]  # [====]로 나눈 문단만 남긴다
    # 글꼴 창·한글 넣기의 문단 수 (문장 자리별 글꼴을 문단 수에 맞춰 펼침)
    data = {"sentence_slots": {"본문": {"ref": ["a.ttf", "b.ttf"]}}}
    card = {"본문": {"ko": "가.\n나.\n다.", "paragraph_breaks": [2]}}
    assert font_plan.sentences_for_page(data, 1, card)["본문"] == {0: "a.ttf", 1: "b.ttf"}
    # 줄 정렬 묶음: 나눔이 있으면 그 줄에서만 (문장 끝으로 나누지 않음)
    lines = [{"bbox_pt": [10, y, 100, y + 8], "glyph_h_pt": 8, "_g": 0} for y in (0, 10, 20)]
    kos = ["가다.", "나다.", "라"]
    assert len({id(b) for b in sentence_blocks(lines, kos).values()}) == 0  # 기본: 두 줄 이상 문장이 없음
    blk = sentence_blocks(lines, kos, None, {id(lines[2])})
    assert id(lines[0]) in blk and blk[id(lines[0])] is blk[id(lines[1])] and id(lines[2]) not in blk


def test_source_review_saves_breaks_without_changing_source(qapp, tmp_path, monkeypatch):
    from app import font_plan
    from app.qt.source_review import SourceReviewDialog

    info, fd = _changed_project(tmp_path, "b4")
    monkeypatch.setattr(font_plan, "region_font_modes", lambda _fd: {"본문": "sentence"})
    src = "攻撃する。\n次に移動する\nそして終わる"
    service.save_source(fd, 1, "본문", src)
    c = next(c for c in service.extracted_cells(fd) if c["page"] == 1)
    assert c["paragraph_mode"] and c["paragraph_breaks"] is None
    files = service.project_overview(info.dir)["groups"][0]["files"]
    dlg = SourceReviewDialog(files, only_check=False, engine="api")

    def show_page1():
        dlg.table.setCurrentCell(next(i for i, x in enumerate(dlg.cells) if x["page"] == 1), 0)
    show_page1()
    assert dlg.edit.toPlainText() == src  # 기본: 문장 끝으로는 나누지 않음
    dlg.save_current()  # 고치지 않고 저장: 나눔을 적지 않는다
    assert _cell(fd)["source"] == src and "paragraph_breaks" not in _cell(fd)
    dlg.edit.setPlainText("攻撃する。\n[====]\n次に移動する\n[====]\nそして終わる")
    dlg.save_current()
    assert _cell(fd)["source"] == src and _cell(fd)["paragraph_breaks"] == [1, 2]  # 원문은 그대로
    show_page1()
    assert dlg.edit.toPlainText() == "攻撃する。\n[====]\n次に移動する\n[====]\nそして終わる"  # 저장된 나눔으로
    dlg.edit.setPlainText(src)  # 모두 빼기: 빈 목록 (한 문단)
    dlg.save_current()
    assert _cell(fd)["paragraph_breaks"] == []
    dlg._orig_text = dlg.edit.toPlainText()
    dlg.close()


def test_result_viewer_paragraphs_use_stored_breaks(tmp_path):
    _info, fd = _changed_project(tmp_path, "b4r")
    service.save_source(fd, 1, "본문", "一。\n二。\n三。", paragraph_breaks=[2])
    assert service.split_paragraphs("가.\n나.\n다.", fd, 1, "본문") == ["가. 나.", "다."]  # 번역문 줄에 맞춤
    assert service.split_paragraphs("가.\n나.", fd, 1, "본문") == ["가. 나."]  # 줄 수가 다르면 기본 규칙


def test_breaks_do_not_break_partial_keep(tmp_path):
    from app.extract import keep_state

    _info, fd = _changed_project(tmp_path, "b4k")
    src = "攻撃する\n[[coin]]を得る。\n次に移動する\nそして終わる"
    service.save_source(fd, 1, "본문", src)
    service.set_keep_paragraphs(fd, 1, "본문", [0, 1])
    assert keep_state(_cell(fd)) == ("partial", [0, 1])
    new, br = service.unmark_paragraphs("攻撃する\n[[coin]]を得る。\n[====]\n次に移動する\n[====]\nそして終わる")
    service.save_source(fd, 1, "본문", new, paragraph_breaks=br)
    assert keep_state(_cell(fd)) == ("partial", [0, 1]) and _cell(fd)["paragraph_breaks"] == [2, 3]
    # 빈 줄을 넣어도 남길 줄 번호를 맞춘다 (나눔 번호는 빈 줄을 뺀 번호라 그대로)
    service.save_source(fd, 1, "본문", "\n" + src)
    assert keep_state(_cell(fd)) == ("partial", [1, 2]) and _cell(fd)["paragraph_breaks"] == [2, 3]


def test_paragraph_mode_uses_per_card_region_style(tmp_path, monkeypatch):
    """리뷰 1.24: 카드별 영역 설정(style.font_mode="sentence")도 문단 나눔 표시 대상."""
    from app.service import _cells_icons

    _info, fd = _changed_project(tmp_path, "b4p")
    real = _cells_icons.load_project

    def fake_load(d):
        p = real(d)
        orig = p.template.regions_for

        def regions_for(pno):
            import copy

            regs = [copy.copy(r) for r in orig(pno)]  # 영역 객체는 쪽끼리 같은 것: 1쪽만 따로
            for r in regs:
                if pno == 1 and r.name == "본문":
                    r.style = {**(r.style or {}), "font_mode": "sentence"}
            return regs
        p.template.regions_for = regions_for
        return p

    monkeypatch.setattr(_cells_icons, "load_project", fake_load)
    modes = {c["page"]: c["paragraph_mode"] for c in service.extracted_cells(fd) if c["region"] == "본문"}
    assert modes[1] and not modes.get(2)


# ---------------------------------------------------------------- B5: 두 번 읽어 같으면 ‘아이콘 고침’ 알림만
def test_settled_cell_gets_plain_icon_mark():
    from app.qt.source_review import SourceReviewDialog
    from app.qt.step_common import ICON_REVIEW_LABEL, LABEL_COLORS
    from app.service._cells_icons import _settle_icon_cell

    cell = {"source": "AB[[coin]]CD", "icon_changed": ["아이콘을 추가함"], "uncertain": True}
    assert _settle_icon_cell(cell, _r("AB[[?]]CD"), _r("AB[[?]]CD"))
    assert cell["icon_settled"] and not cell.get("icon_changed")
    assert verification_label(cell) == ICON_SETTLED_LABEL == "아이콘 고침"
    assert LABEL_COLORS[ICON_SETTLED_LABEL] != LABEL_COLORS[ICON_REVIEW_LABEL]  # 보라 아님
    c = {"label": ICON_SETTLED_LABEL, "uncertain": False, "keep_original": False, "keep_mode": "", "source": "x"}
    assert not SourceReviewDialog.needs_check(c) and not SourceReviewDialog.needs_attention(c)
    # 그 뒤 다시 아이콘을 고쳐 두 번 읽은 결과가 다르면 알림은 풀리고 보라 ‘확인 필요’
    cell["icon_changed"] = ["다시 잡음"]
    assert not _settle_icon_cell(cell, _r("AB[[?]]CO"), _r("AB[[?]]CD"))
    assert not cell.get("icon_settled") and verification_label(cell) == ICON_REVIEW_LABEL


def test_settled_mark_cleared_when_person_saves(tmp_path, monkeypatch):
    info, fd = _changed_project(tmp_path, "b5")

    def fake_read(client, proj, page, src_doc, model_id, out_dir, regs):
        return {regs[0].name: {"source": "AB[[?]]CD", "uncertain": False}}, 0.0, 0.0

    monkeypatch.setattr("app.extract._read_regions_once", fake_read)
    r = service.reread_cells(service.icon_reread_pending([fd]), client=object())
    assert r["settled"] == 1
    c = next(c for c in service.extracted_cells(fd) if c["page"] == 1)
    assert c["label"] == "아이콘 고침" and not c["icon_changed"]
    service.save_source(fd, 1, "본문", _cell(fd)["source"])
    assert not _cell(fd).get("icon_settled") and verification_label(_cell(fd)) == "사람이 확인"


# ---------------------------------------------------------------- B6: 문장 부호 충돌
def test_punctuation_conflicts_count_as_same():
    assert _agree_norm("A、B。") == _agree_norm("A，B．")
    assert _agree_norm("A、B") != _agree_norm("A。B")  # 다른 부호끼리는 다르다
    assert prefer_punct("A、B。C、", "A，B．C、") == "A，B．C、"  # 맞선 곳만 ‘，’·‘．’
    assert prefer_punct("A，B．", "A、B。") == "A，B．"
    assert prefer_punct("あ、\nい。", "あ，い．") == "あ，\nい．"  # 줄바꿈·공백은 base 그대로
    assert prefer_punct(["A、", "B"], ["A，", "B"]) == ["A，", "B"]


def test_settle_with_punctuation_conflict_picks_fullwidth_comma():
    from app.service._cells_icons import _settle_icon_cell

    cell = {"source": "AB、[[coin]]CD。", "icon_changed": ["x"]}
    assert _settle_icon_cell(cell, _r("AB、[[?]]CD．"), _r("AB，[[?]]CD。"))
    assert cell["source"] == "AB，[[coin]]CD．"  # [[이름]]은 그대로, 부호만 고른 쪽


def test_recheck_agrees_when_only_punctuation_differs(tmp_path, monkeypatch):
    from app import extract
    from app.project import load_project
    from tests.test_extract import extract_dir, make_project

    proj = make_project(tmp_path, pages=1)
    base = extract_dir(proj)
    (base / "pages").mkdir(parents=True)
    rec = {"page": 1, "model": MODELS["sonnet"],
           "regions": {"이름": {"source": "攻撃、移動。", "uncertain": True, "note": ""}}}
    (base / "pages" / "page-001.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(extract, "_prepare_read",
                        lambda *a, **k: (lambda: ({"이름": {"source": "攻撃，移動。", "uncertain": False}}, 0.0, 0.0)))
    rc = extract.run_recheck(load_project(proj), [1], SimpleNamespace(engine="api"), base=base,
                             check_model=MODELS["opus"])
    assert rc.agreed == 1 and rc.disagreed == 0
    out = json.loads((base / "pages" / "page-001.json").read_text(encoding="utf-8"))["regions"]["이름"]
    assert out["source"] == "攻撃，移動。" and not out["uncertain"]
