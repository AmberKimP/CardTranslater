"""③ 원문 확인: ② 아이콘 정의에서 아이콘 자리가 바뀐 칸은 보통·정밀 읽기로 모두 읽는다.
두 결과가 같고 지금 원문과도 같으면 확인이 끝나고, 아니면 ‘아이콘 고침 · 확인 필요’로 남는다. ‘(다시 읽기)’ 표시는 없다."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app import glossary, service, workspace
from app.extract import ICON_REVIEW_LABEL, MODELS, Paused
from app.extract_cc import CardError
from tests.test_batch_1180 import _changed_project

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _cell(fd: Path, page: int = 1) -> dict:
    return json.loads((fd / "extract" / "pages" / f"page-{page:03d}.json").read_text(encoding="utf-8"))[
        "regions"]["본문"]


def _fake_reads(monkeypatch, by_model: dict, calls: list):
    """모델마다 돌려줄 결과: {"opus": {"source", "uncertain"} | 예외}."""
    def fake_read(client, proj, page, src_doc, model_id, out_dir, regs):
        calls.append(model_id)
        got = by_model[next(k for k, v in MODELS.items() if v == model_id)]
        if isinstance(got, Exception):
            raise got
        return {regs[0].name: dict(got)}, 0.01, 0.0

    monkeypatch.setattr("app.extract._read_regions_once", fake_read)


def _status(info) -> dict:
    return glossary.extraction_status(info, game=True)[0]


def test_agreeing_readings_settle_cell(tmp_path, monkeypatch):
    info, fd = _changed_project(tmp_path, "agree")
    src = _cell(fd)["source"]
    calls = []
    # 다시 읽은 결과에는 이름 없는 [[?]]가 있다: 지금 원문의 [[이름]]과 아이콘 말고는 같다
    same = {"source": "AB[[?]]CD", "uncertain": False}
    _fake_reads(monkeypatch, {"sonnet": same, "opus": same}, calls)
    r = service.reread_cells(service.icon_reread_pending([fd]), client=object())
    assert calls == [MODELS["sonnet"], MODELS["opus"]]  # 보통 읽기 → 정밀 읽기 (꼼꼼히 모드의 두 모델)
    assert r["done"] == 1 and r["settled"] == 1 and r["state"] == "done"
    assert r["spent_usd"] == pytest.approx(0.02)
    cell = _cell(fd)
    assert cell["source"] == src  # ② 아이콘 정의에서 정한 [[이름]]을 지킨다
    assert not cell.get("icon_changed") and not cell.get("icon_reread") and not cell["uncertain"]
    assert [x["model"] for x in cell["readings"]] == [MODELS["sonnet"], MODELS["opus"]]
    c = next(c for c in service.extracted_cells(fd) if c["page"] == 1)
    # 1.24 B5: 확인이 끝나도 ‘아이콘 고침’ 알림 (보라 ‘확인 필요’ 아님, 확인할 칸 아님)
    assert c["label"] == "아이콘 고침" and len(c["readings"]) == 2 and cell.get("icon_settled")
    from app.qt.source_review import SourceReviewDialog
    assert not SourceReviewDialog.needs_check(c) and not SourceReviewDialog.needs_attention(c)
    assert _status(info)["icon_changed"] == 0  # 확인이 끝난 칸은 용어집 만들기를 막지 않는다


@pytest.mark.parametrize("sonnet,opus,why", [
    ({"source": "AB[[?]]CO", "uncertain": False}, {"source": "AB[[?]]CD", "uncertain": False}, "다르게 읽음"),
    ({"source": "AB[[?]]CD", "uncertain": True}, {"source": "AB[[?]]CD", "uncertain": False}, "확실하지 않다고 함"),
    ({"source": "XB[[?]]CD", "uncertain": False}, {"source": "XB[[?]]CD", "uncertain": False}, "지금 원문과 다름"),
])
def test_problem_readings_get_icon_review_status(tmp_path, monkeypatch, sonnet, opus, why):
    info, fd = _changed_project(tmp_path, "diff")
    src = _cell(fd)["source"]
    calls = []
    _fake_reads(monkeypatch, {"sonnet": sonnet, "opus": opus}, calls)
    r = service.reread_cells(service.icon_reread_pending([fd]), client=object())
    assert len(calls) == 2 and r["done"] == 1 and r["settled"] == 0
    cell = _cell(fd)
    assert cell["source"] == src and cell["uncertain"] and cell["icon_changed"] and not cell.get("icon_reread")
    assert why in cell["note"]
    assert [x["source"] for x in cell["readings"]] == [sonnet["source"], opus["source"]]
    c = next(c for c in service.extracted_cells(fd) if c["page"] == 1)
    assert c["label"] == ICON_REVIEW_LABEL and len(c["readings"]) == 2  # 두 결과를 나란히 보인다
    st = _status(info)
    assert st["icon_changed"] == 1 and st["no_verdict"] == 0  # 보통의 ‘결론 못 냄’과 나눠 센다


def test_second_read_paused_keeps_pending_semantics(tmp_path, monkeypatch):
    info, fd = _changed_project(tmp_path, "pause2")
    calls = []
    _fake_reads(monkeypatch, {"sonnet": {"source": "AB[[?]]CD", "uncertain": False},
                              "opus": Paused("한도", 60)}, calls)
    r = service.reread_cells(service.icon_reread_pending([fd]), client=object())
    assert r["state"] == "paused" and r["done"] == 0 and r["retry_after"]
    assert service.icon_reread_pending([fd]) == []  # 다음에 원문 확인을 열 때 또 자동으로 읽지 않는다
    cell = _cell(fd)
    assert cell["icon_changed"] and cell["uncertain"]
    assert [x["model"] for x in cell["rereads"]] == [MODELS["sonnet"]]  # 먼저 읽은 결과는 버리지 않는다
    c = next(c for c in service.extracted_cells(fd) if c["page"] == 1)
    assert c["label"] == ICON_REVIEW_LABEL


def test_card_error_is_failed_and_not_reread_again(tmp_path, monkeypatch):
    info, fd = _changed_project(tmp_path, "err")
    calls = []
    _fake_reads(monkeypatch, {"sonnet": CardError("응답 없음"), "opus": {"source": "x", "uncertain": False}}, calls)
    r = service.reread_cells(service.icon_reread_pending([fd]), client=object())
    assert calls == [MODELS["sonnet"]]  # 보통 읽기가 실패하면 정밀 읽기에 돈을 쓰지 않는다
    assert r["failed"] and r["state"] == "done" and r["done"] == 0
    assert service.icon_reread_pending([fd]) == []
    assert _cell(fd)["icon_changed"]


def test_single_model_reread_keeps_old_behaviour(tmp_path, monkeypatch):
    """model을 주면 예전처럼 한 번만 읽어 rereads에 덧붙이고 상태는 그대로."""
    info, fd = _changed_project(tmp_path, "single")
    calls = []
    _fake_reads(monkeypatch, {"opus": {"source": "AB[[?]]CD", "uncertain": False}}, calls)
    r = service.reread_cells(service.icon_reread_pending([fd]), model="opus", client=object())
    assert calls == [MODELS["opus"]] and r["done"] == 1 and r["settled"] == 0
    assert _cell(fd)["icon_changed"]


def test_save_source_clears_icon_review_status(tmp_path, monkeypatch):
    info, fd = _changed_project(tmp_path, "save")
    _fake_reads(monkeypatch, {"sonnet": {"source": "AB[[?]]CO", "uncertain": False},
                              "opus": {"source": "AB[[?]]CD", "uncertain": False}}, [])
    service.reread_cells(service.icon_reread_pending([fd]), client=object())
    service.save_source(fd, 1, "본문", _cell(fd)["source"])
    c = next(c for c in service.extracted_cells(fd) if c["page"] == 1)
    assert c["label"] != ICON_REVIEW_LABEL and not c["icon_changed"]


# ---------------------------------------------------------------- 화면
def test_ui_status_filter_and_no_reread_tag():
    from app.qt import source_review, step_common
    from app.qt.source_review import SourceReviewDialog

    assert not hasattr(step_common, "REREAD_TAG")
    text = Path(source_review.__file__).read_text(encoding="utf-8")
    assert "(다시 읽기)" not in text and "REREAD_TAG" not in text
    assert step_common.ICON_REVIEW_LABEL == ICON_REVIEW_LABEL
    assert step_common.LABEL_COLORS[ICON_REVIEW_LABEL] not in (step_common.LABEL_COLORS["결론 못 냄"], None)
    c = {"label": ICON_REVIEW_LABEL, "uncertain": False, "keep_original": False, "keep_mode": "", "source": "A",
         "readings": [{"model": "claude-sonnet-5-5", "source": "A", "reread": True}], "note": ""}
    assert SourceReviewDialog.needs_check(c) and SourceReviewDialog.needs_attention(c)
    assert SourceReviewDialog.reading_texts(c) == [("보통 읽기", "A")]


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_dialog_counts_and_shows_icon_review(qapp, tmp_path, monkeypatch):
    from app.qt.source_review import SourceReviewDialog
    from app.qt.step_common import label_text

    info, fd = _changed_project(tmp_path, "dlg")
    _fake_reads(monkeypatch, {"sonnet": {"source": "AB[[?]]CO", "uncertain": False},
                              "opus": {"source": "AB[[?]]CD", "uncertain": False}}, [])
    service.reread_cells(service.icon_reread_pending([fd]), client=object())
    files = service.project_overview(info.dir)["groups"][0]["files"]
    dlg = SourceReviewDialog(files, engine="api")
    assert "아이콘 고침" in dlg.only.text()
    assert "(아이콘 고침 1)" in dlg.count.text()
    labels = [dlg.table.item(i, 2).text() for i in range(dlg.table.rowCount())]
    assert label_text(ICON_REVIEW_LABEL) in labels
    assert all("(다시 읽기)" not in h.text() for h, _v in dlg.reading_rows)
    dlg.close()


# ---------------------------------------------------------------- 견주기: 아이콘 자리·수, 아이콘에 넣은 숫자만 무시
def _r(source, uncertain=False):
    return {"model": "m", "source": source, "uncertain": uncertain, "note": "", "at": "t"}


def test_settle_needs_same_icon_place_and_count():
    from app.service._cells_icons import _settle_icon_cell

    cell = {"source": "AB[[coin]]CD", "icon_changed": ["아이콘을 추가함"]}
    assert _settle_icon_cell(cell, _r("AB[[?]]CD"), _r("AB[[?]]CD"))  # 이름은 몰라도 자리·수가 같으면 끝
    for read in ("ABCD[[?]]", "AB[[?]][[?]]CD", "ABCD"):  # 자리가 다름 / 수가 다름
        cell = {"source": "AB[[coin]]CD", "icon_changed": ["아이콘을 추가함"]}
        assert not _settle_icon_cell(cell, _r(read), _r(read)), read
        assert cell["icon_changed"]


def test_settle_ignores_only_numbers_put_into_icons():
    from app.service._cells_icons import _settle_icon_cell

    nums = [{"index": 0, "name": "coin", "text": "2", "side": "before", "sep": ""}]
    cell = {"source": "攻撃力3 [[coin]]", "icon_numbers": nums, "icon_changed": ["x"]}
    assert _settle_icon_cell(cell, _r("攻撃力3 2[[?]]"), _r("攻撃力3 2[[?]]"))
    cell = {"source": "攻撃力3 [[coin]]", "icon_numbers": nums, "icon_changed": ["x"]}
    assert not _settle_icon_cell(cell, _r("攻撃力8 2[[?]]"), _r("攻撃力8 2[[?]]"))  # 다른 숫자를 잘못 읽음
