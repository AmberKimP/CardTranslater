"""쪽을 묶음으로 나눠 여러 프로세스로 동시에 처리 (⑦ 글자 지우기, ⑧ 한글 넣기).

쪽마다 계산이 따로라 나눠도 결과는 같다 (실제 20쪽 지우기: 순서대로와 픽셀까지 같음, 35.9초 → 4개 동시 14.0초).
- 동시 수: 쪽이 적으면(MIN_PAGES 미만) 1. 아니면 min(MAX_WORKERS, CPU 수 - 2, 쪽 수 / 2, 남은 메모리 / 500MB). 환경 변수
  CARDTRANSLATER_WORKERS로 바꿀 수 있다 (테스트는 1).
- 묶음은 이어진 쪽들 (합칠 때 순서를 지키게), 동시 수만큼 (묶음마다 준비 비용이 들어 더 잘게 나누면 느려진다).
- 진행: 작업 프로세스가 한 단계를 끝낼 때마다 시작할 때 받은 큐로 알리고, 이쪽에서 모아 on_tick으로 넘긴다.
  끝날 때까지(취소·실패 때도) 큐를 비우며 기다린다 (큐 파이프가 차면 작업 프로세스가 끝나지 못한다).
- 취소: 공유 Event를 켜면 작업 프로세스도 다음 쪽에서 멈춘다 (worker_cancel()로 받은 토큰).
- 작업 프로세스를 띄우지 못하면(아무 일도 못 하고 죽음) WorkersFailed: 부르는 쪽이 한 프로세스로 다시 한다.
  일하던 중에 죽으면(MuPDF 오류·메모리 부족) WorkerCrashed: 화면 프로세스에서 다시 하지 않는다 (앱이 같이 죽지 않게).
- 합치기(merge_parts): 묶음 PDF를 순서대로 합치고, 합친 쪽이 묶음 쪽과 똑같이 그려지는지 낮은 해상도로 확인한다.
화면·print 없음.
"""

from __future__ import annotations

import os
import queue as _queue
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from typing import Callable

MAX_WORKERS = 4  # 4보다 늘려도 거의 빨라지지 않는다 (프로세스 시작·캐시 데우기 비용)
MIN_PAGES = 6  # 이보다 적으면 나누는 비용이 더 크다
PARTS_PER_WORKER = 1  # 묶음 수 = 동시 수 × 이것 (20쪽: 2배로 나누면 16.5초, 1배 14.0초)
ENV = "CARDTRANSLATER_WORKERS"


def worker_count(n_pages: int, requested: int | None = None) -> int:
    """동시에 돌릴 프로세스 수. requested가 있으면 그것 (1이면 나누지 않음)."""
    env = os.environ.get(ENV)
    if requested is None and env and env.isdigit():
        requested = int(env)
    if requested is not None:
        return max(1, min(int(requested), n_pages))
    if n_pages < MIN_PAGES:
        return 1
    cpu = os.cpu_count() or 2
    return max(1, min(MAX_WORKERS, cpu - 2, n_pages // 2, _memory_cap()))


MB_PER_WORKER = 500  # 20쪽 지우기: 1개 560MB, 4개 동시 1,240MB → 하나 늘 때 약 230MB (여유를 두고 500)


def _memory_cap() -> int:
    """남은 메모리로 띄울 수 있는 작업 프로세스 수 (모르면 제한 없음)."""
    try:
        import psutil

        return max(1, int(psutil.virtual_memory().available // (MB_PER_WORKER * 1_000_000)))
    except Exception:  # noqa: BLE001
        return MAX_WORKERS


def split(items: list, parts: int) -> list[list]:
    """이어진 묶음으로 나눈다 (빈 묶음 없이, 순서 그대로)."""
    n = len(items)
    parts = max(1, min(parts, n))
    out = [items[i * n // parts:(i + 1) * n // parts] for i in range(parts)]
    return [p for p in out if p]


_TICKS = None  # 작업 프로세스 안: 시작할 때 받은 진행 알림 큐
_CANCEL = None  # 작업 프로세스 안: 취소 Event


def _init_worker(ticks, cancel_ev) -> None:
    global _TICKS, _CANCEL
    _TICKS, _CANCEL = ticks, cancel_ev
    try:
        ticks.cancel_join_thread()  # 끝날 때 알림을 다 보내지 못해도 프로세스가 끝날 수 있게 (진행 표시만 덜 됨)
    except Exception:  # noqa: BLE001
        pass


def tick_progress():
    """작업 프로세스 안에서 쓰는 progress 콜백: 단계(stage)마다 한 번씩 진행 알림 큐에 넣는다."""
    def progress(ev):
        if _TICKS is not None:
            try:
                _TICKS.put_nowait(getattr(ev, "stage", ""))
            except Exception:  # noqa: BLE001 - 진행 표시는 못 해도 일은 한다
                pass
    return progress


class _WorkerCancel:
    """작업 프로세스 안의 취소 토큰 (service.CancelToken처럼 check()가 Cancelled를 낸다)."""

    @property
    def cancelled(self) -> bool:
        return bool(_CANCEL is not None and _CANCEL.is_set())

    def check(self) -> None:
        if self.cancelled:
            from app.service import Cancelled

            raise Cancelled()


def worker_cancel() -> _WorkerCancel:
    return _WorkerCancel()


class WorkersFailed(RuntimeError):
    """작업 프로세스를 띄우지 못했다 (부르는 쪽은 한 프로세스로 다시 한다)."""


class WorkerCrashed(RuntimeError):
    """작업 프로세스가 일하던 중에 죽었다 (같은 일을 화면 프로세스에서 다시 하면 앱이 죽을 수 있다)."""


def run_parts(fn: Callable, parts: list[list], part_args: list[tuple], workers: int,
              on_tick: Callable[[dict], None] | None = None, cancel=None) -> list:
    """fn(part, k, *part_args[k])를 묶음마다 프로세스에서 돌려 결과를 묶음 순서대로 돌려준다.
    작업 프로세스 안에서 tick_progress()가 단계 이름을 알리면 on_tick({단계: 지금까지 수}),
    worker_cancel()이 이쪽의 취소를 따른다. 한 묶음이 예외를 내면 그 예외를 낸다."""
    import multiprocessing
    from concurrent.futures.process import BrokenProcessPool

    ctx = multiprocessing.get_context("spawn")
    results: dict[int, object] = {}
    counts: dict[str, int] = {}
    ticks = ctx.Queue()
    cancel_ev = ctx.Event()

    def drain():
        changed = False
        while True:
            try:
                st = ticks.get_nowait()
            except (_queue.Empty, OSError, EOFError, ValueError):
                break
            counts[st] = counts.get(st, 0) + 1
            changed = True
        if changed and on_tick:
            on_tick(dict(counts))

    ex = ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_init_worker,
                             initargs=(ticks, cancel_ev))
    futs = {ex.submit(fn, part, k, *part_args[k]): k for k, part in enumerate(parts)}
    pending = set(futs)
    try:
        while pending:
            if cancel is not None:
                cancel.check()
            finished, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            drain()
            for f in finished:
                results[futs[f]] = f.result()
    except BrokenProcessPool as e:
        if not results and not counts:
            raise WorkersFailed(f"작업 프로세스를 띄우지 못했습니다: {e}") from e
        raise WorkerCrashed(f"작업 프로세스가 일하던 중에 끝났습니다 (메모리 부족이나 PDF 오류일 수 있습니다): {e}") from e
    finally:
        if pending:  # 취소·실패: 돌고 있는 묶음도 다음 쪽에서 멈춘다
            cancel_ev.set()
        for f in pending:
            f.cancel()
        ex.shutdown(wait=False, cancel_futures=True)
        while any(not f.done() for f in futs):  # 끝나기를 기다리는 동안에도 알림을 비운다 (파이프가 차면 못 끝난다)
            wait(set(futs), timeout=0.25)
            drain()
        drain()
        ticks.close()
        ticks.join_thread()
    return [results[k] for k in range(len(parts))]


def merge_parts(run_dir, n_parts: int, out_pdf, check_dpi: int = 24) -> None:
    """run_dir/.part-k.pdf (k=0..n-1)를 순서대로 out_pdf 하나로 합치고 묶음 파일은 지운다.
    합친 쪽이 묶음의 쪽과 똑같이 그려지는지 낮은 해상도로 확인한다 (다르면 RuntimeError: 부르는 쪽이 한 프로세스로)."""
    from pathlib import Path

    import pymupdf

    run_dir, out_pdf = Path(run_dir), Path(out_pdf)
    paths = [run_dir / f".part-{k:03d}.pdf" for k in range(n_parts)]
    try:
        out = pymupdf.open(paths[0])  # 첫 묶음(원본에서 고른 쪽)의 문서 정보를 그대로 쓴다
        for pth in paths[1:]:
            with pymupdf.open(pth) as d:
                out.insert_pdf(d)
        out.save(out_pdf, garbage=3, deflate=True)
        out.close()
        with pymupdf.open(out_pdf) as merged:
            i = 0
            for pth in paths:
                with pymupdf.open(pth) as d:
                    for pg in d:
                        a = pg.get_pixmap(dpi=check_dpi, alpha=False).samples
                        b = merged[i].get_pixmap(dpi=check_dpi, alpha=False).samples
                        if a != b:
                            raise MergeMismatch(f"합친 PDF의 {i + 1}번째 쪽이 묶음과 다르게 그려집니다")
                        i += 1
    finally:
        cleanup_parts(run_dir)


def cleanup_parts(run_dir) -> None:
    from pathlib import Path

    for f in Path(run_dir).glob(".part-*.pdf"):
        f.unlink(missing_ok=True)


def run_or_fallback(parallel_fn: Callable, serial_fn: Callable, run_dir):
    """동시 처리를 해 보고, 작업 프로세스를 못 띄웠거나 합친 결과가 이상하면 한 프로세스로 (결과는 같다)."""
    import logging

    try:
        return parallel_fn()
    except (WorkersFailed, MergeMismatch) as e:
        logging.getLogger("cardtranslater.parallel").warning("동시 처리 대신 한 프로세스로: %s", e)
        cleanup_parts(run_dir)
        return serial_fn()


class MergeMismatch(RuntimeError):
    """합친 PDF가 묶음과 다르게 그려진다."""


def _selftest_part(part: list, _k: int) -> int:
    tick_progress()(type("E", (), {"stage": "selftest"})())
    return sum(part)


def self_check() -> bool | None:
    """작업 프로세스를 실제로 띄워 보는 점검 (exe 자가 점검: PyInstaller에서 프로세스가 뜨는지).
    동시 처리를 끈 환경(CARDTRANSLATER_WORKERS=1, 테스트)이면 None."""
    if os.environ.get(ENV) == "1":
        return None
    seen: dict = {}
    try:
        return run_parts(_selftest_part, [[1, 2], [3]], [(), ()], 2, lambda c: seen.update(c)) == [3, 3]             and seen.get("selftest") == 2
    except Exception:  # noqa: BLE001
        return False
