"""⑦·⑧ 쪽 나눠 동시 처리: 결과가 한 프로세스와 같고, 작업 프로세스를 못 띄우면 한 프로세스로 한다."""

from __future__ import annotations

import json

import pymupdf
import pytest

from app import parallel, service


def test_worker_count_and_split(monkeypatch):
    monkeypatch.delenv(parallel.ENV, raising=False)
    assert parallel.worker_count(3) == 1  # 쪽이 적으면 나누지 않는다
    monkeypatch.setattr("os.cpu_count", lambda: 12)
    monkeypatch.setattr(parallel, "_memory_cap", lambda: 99)
    assert parallel.worker_count(20) == parallel.MAX_WORKERS
    monkeypatch.setattr(parallel, "_memory_cap", lambda: 2)  # 메모리가 적으면 줄인다
    assert parallel.worker_count(20) == 2
    monkeypatch.setattr(parallel, "_memory_cap", lambda: 99)
    assert parallel.worker_count(6) == 3  # 쪽 수의 절반까지
    monkeypatch.setattr("os.cpu_count", lambda: 2)
    assert parallel.worker_count(50) == 1  # 코어가 적으면 화면이 쓸 몫을 남긴다
    assert parallel.worker_count(50, requested=3) == 3 and parallel.worker_count(2, requested=8) == 2
    monkeypatch.setenv(parallel.ENV, "1")
    assert parallel.worker_count(50) == 1
    assert parallel.split([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4, 5]]
    assert parallel.split([1, 2], 4) == [[1], [2]]  # 빈 묶음 없이


def _pixels(pdf):
    with pymupdf.open(pdf) as d:
        return [p.get_pixmap(dpi=72).samples for p in d]


def test_parallel_erase_and_compose_match_single_process(tmp_path):
    """실제로 프로세스 2개를 띄워 지우고 넣은 결과가 한 프로세스 결과와 픽셀까지 같다 (쪽 순서·메타 번호 포함)."""
    from tests.test_qt import _translated_project

    info, fd = _translated_project(tmp_path)
    r1 = service.erase(fd, "1-2", test=True, out_root=tmp_path / "e1", workers=1)
    r2 = service.erase(fd, "1-2", test=True, out_root=tmp_path / "e2", workers=2)
    assert r1.ok and r2.ok
    assert _pixels(r1.output_dir / "erased.pdf") == _pixels(r2.output_dir / "erased.pdf")
    for pno in (1, 2):
        m1 = json.loads((r1.output_dir / "meta" / f"page-{pno:03d}.json").read_text(encoding="utf-8"))
        m2 = json.loads((r2.output_dir / "meta" / f"page-{pno:03d}.json").read_text(encoding="utf-8"))
        assert m2["output_page_index"] == m1["output_page_index"] == pno - 1
        assert m2["output_pdf"] == "erased.pdf" and m2["background_image"]["stream_untouched"]
    assert not list(r2.output_dir.glob(".part-*"))
    assert sorted(p.name for p in (r1.output_dir / "compare").glob("*.png")) == \
        sorted(p.name for p in (r2.output_dir / "compare").glob("*.png"))

    from app.typeset_compose import run_compose

    service.erase_project(info.dir)  # 한글 넣기는 지운 결과(cleared/)가 있어야 한다
    cards = service._translation_cards(fd)
    d1, _ = run_compose(fd, fd / "cleared", None, tmp_path / "c1", cards=cards, workers=1)
    d2, _ = run_compose(fd, fd / "cleared", None, tmp_path / "c2", cards=cards, workers=2)
    assert _pixels(d1 / "composed.pdf") == _pixels(d2 / "composed.pdf")
    rep1 = json.loads((d1 / "report.json").read_text(encoding="utf-8"))
    rep2 = json.loads((d2 / "report.json").read_text(encoding="utf-8"))
    assert [p["regions"] for p in rep1["pages_detail"]] == [p["regions"] for p in rep2["pages_detail"]]
    assert rep1["status_summary"] == rep2["status_summary"] and not list(d2.glob(".part-*"))


def test_falls_back_to_single_process_when_workers_fail(tmp_path, monkeypatch):
    """작업 프로세스를 띄우지 못하면(WorkersFailed) 멈추지 않고 한 프로세스로 끝낸다."""
    from tests.test_qt import _translated_project

    info, fd = _translated_project(tmp_path)

    def broken(*_a, **_k):
        raise parallel.WorkersFailed("못 띄움")
    monkeypatch.setattr(parallel, "run_parts", broken)
    r = service.erase(fd, "1-2", test=True, out_root=tmp_path / "e", workers=2)
    assert r.ok and len(_pixels(r.output_dir / "erased.pdf")) == 2


def test_cancel_stops_parallel_erase(tmp_path):
    """취소하면 남은 묶음을 시작하지 않고 Cancelled. 프로젝트의 cleared/는 바뀌지 않는다."""
    from tests.test_qt import _translated_project

    _info, fd = _translated_project(tmp_path)
    tok = service.CancelToken()
    tok.cancel()
    with pytest.raises(service.Cancelled):
        service.erase(fd, "1-2", workers=2, cancel=tok)
    assert not (fd / "cleared").exists() and not list(fd.glob(".cleared-*"))  # 작업 폴더도 치운다


# ---------------------------------------------------------------- 작업 프로세스 안 함수 (spawn이 이 모듈을 읽어 부른다)
def _busy_until_cancel(part, _k):
    """진행 알림을 아주 많이 보내고(파이프를 채움), 취소될 때까지 쪽을 도는 척."""
    import time

    tick = parallel.tick_progress()
    cancel = parallel.worker_cancel()
    for _ in range(5000):
        tick(type("E", (), {"stage": "erase"})())
    for _ in range(600):
        cancel.check()
        time.sleep(0.05)
    return part


def _die_after_work(part, k):
    import os

    parallel.tick_progress()(type("E", (), {"stage": "erase"})())
    if k == 1:
        os._exit(3)  # 일하던 중에 죽음 (MuPDF 오류·메모리 부족 흉내)
    return part


def _die_at_start(part, _k):
    import os

    os._exit(3)


def test_cancel_returns_quickly_even_with_full_progress_pipe():
    """취소하면 작업 프로세스도 다음 쪽에서 멈추고, 알림 파이프가 가득 차도 멈춰 서지 않는다."""
    import threading
    import time

    tok = service.CancelToken()
    threading.Timer(3.0, tok.cancel).start()
    t0 = time.perf_counter()
    with pytest.raises(service.Cancelled):
        parallel.run_parts(_busy_until_cancel, [[1], [2]], [(), ()], 2, None, tok)
    assert time.perf_counter() - t0 < 20  # 묶음을 끝까지(30초) 기다리지 않는다


def test_worker_crash_during_work_is_an_error_not_a_fallback():
    with pytest.raises(parallel.WorkerCrashed):
        parallel.run_parts(_die_after_work, [[1], [2]], [(), ()], 2)


def test_workers_that_cannot_start_raise_workers_failed():
    with pytest.raises(parallel.WorkersFailed):
        parallel.run_parts(_die_at_start, [[1], [2]], [(), ()], 2)


def test_merge_mismatch_falls_back_to_single_process(tmp_path, monkeypatch):
    from tests.test_qt import _translated_project

    _info, fd = _translated_project(tmp_path)

    def bad_merge(*_a, **_k):
        raise parallel.MergeMismatch("다르게 그려짐")
    monkeypatch.setattr(parallel, "merge_parts", bad_merge)
    r = service.erase(fd, "1-2", test=True, out_root=tmp_path / "e", workers=2)
    assert r.ok and len(_pixels(r.output_dir / "erased.pdf")) == 2 and not list(r.output_dir.glob(".part-*"))
