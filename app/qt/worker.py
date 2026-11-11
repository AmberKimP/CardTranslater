"""긴 작업을 작업 스레드에서 돌리고 진행률·결과를 Signal로 화면 스레드에 넘긴다.

service 함수의 progress 콜백(ProgressEvent)과 CancelToken을 그대로 쓴다.
"""

from __future__ import annotations

import logging
import time
import traceback
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import QProgressDialog, QWidget

from app import service

log = logging.getLogger("cardtranslater.job")


class JobSignals(QObject):
    progress = Signal(object)  # service.ProgressEvent
    done = Signal(object)  # 결과
    failed = Signal(str)  # 오류 메시지
    cancelled = Signal()


class Job(QRunnable):
    """fn(*args, progress=..., cancel=..., **kwargs)를 작업 스레드에서 부른다.
    with_progress=False면 progress·cancel 인자를 넘기지 않는다 (진행률이 없는 함수)."""

    def __init__(self, fn: Callable, *args, with_progress: bool = True, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.with_progress = with_progress
        self.signals = JobSignals()
        self.token = service.CancelToken()
        self.setAutoDelete(False)

    def run(self) -> None:
        name = getattr(self.fn, "__name__", str(self.fn))
        t0 = time.monotonic()
        log.info("작업 시작: %s %s", name, _describe(self.args, self.kwargs))
        last = [""]

        def progress(ev):
            msg = getattr(ev, "message", "") or getattr(ev, "stage", "")
            if msg and msg != last[0]:  # 같은 문구가 계속 오면 한 번만
                last[0] = msg
                log.debug("  %s: [%s %s/%s] %s", name, ev.stage, ev.done, ev.total, msg)
            self.signals.progress.emit(ev)

        try:
            kw = dict(self.kwargs)
            if self.with_progress:
                kw.update(progress=progress, cancel=self.token)
            result = self.fn(*self.args, **kw)
        except service.Cancelled:
            log.info("작업 취소: %s (%.1f초)", name, time.monotonic() - t0)
            service.log_job_time(name, time.monotonic() - t0, "취소")
            self.signals.cancelled.emit()
            return
        except Exception as e:  # noqa: BLE001 - 화면에 보여 준다
            log.error("작업 실패: %s (%.1f초) %s", name, time.monotonic() - t0, _describe(self.args, self.kwargs),
                      exc_info=True)
            service.log_job_time(name, time.monotonic() - t0, f"실패: {type(e).__name__}")
            self.signals.failed.emit(f"{e}\n\n{traceback.format_exc(limit=3)}")
            return
        log.info("작업 끝: %s (%.1f초) → %s", name, time.monotonic() - t0, _brief(result))
        service.log_job_time(name, time.monotonic() - t0, _state_of(result))
        self.signals.done.emit(result)

    def cancel(self) -> None:
        self.token.cancel()


def _state_of(result) -> str:
    """결과의 상태 (done·paused·blocked 등). 없으면 ‘끝’."""
    st = getattr(result, "state", None)
    if st is None and isinstance(result, dict):
        st = result.get("state")
    if st is None and getattr(result, "ok", None) is False:  # EraseResult 등
        return f"실패: {getattr(result, 'error', '') or 'ok=False'}"
    return str(st) if st else "끝"


def _brief(x, limit: int = 300) -> str:
    s = repr(x)
    return s if len(s) <= limit else s[:limit] + "…"


def _describe(args, kwargs) -> str:
    parts = [_brief(a, 120) for a in args] + [f"{k}={_brief(v, 120)}" for k, v in kwargs.items()]
    return "(" + ", ".join(parts) + ")"


_running: set[Job] = set()  # 끝나기 전에 가비지 컬렉션되지 않게


def start(job: Job) -> Job:
    _running.add(job)
    for sig in (job.signals.done, job.signals.failed, job.signals.cancelled):
        sig.connect(lambda *_a, j=job: _running.discard(j))
    t0 = time.monotonic()
    job.signals.done.connect(lambda *_a, t=t0: _done_sound(time.monotonic() - t))
    QThreadPool.globalInstance().start(job)
    return job


def _done_sound(elapsed: float) -> None:
    """오래(5초 넘게) 걸린 작업이 잘 끝나면 소리 (실패·취소는 소리 없음)."""
    if elapsed >= service.DONE_SOUND_MIN_S:
        from app.qt import sound

        sound.play_done()


def run_with_dialog(parent: QWidget | None, title: str, job: Job,
                    on_done: Callable[[object], None], on_failed: Callable[[str], None] | None = None,
                    cancellable: bool = True) -> Job:
    """진행률 창(취소 버튼 포함)을 띄우고 job을 돌린다. 진행률이 없는 작업은 막대가 계속 움직인다."""
    dlg = QProgressDialog(title, "취소", 0, 0, parent)
    dlg.setMinimumWidth(440)
    if not cancellable:
        dlg.setCancelButton(None)
    dlg.setWindowTitle(title)
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)

    def on_progress(ev):
        dlg.setMaximum(max(ev.total, 1))
        dlg.setValue(min(ev.done, ev.total))
        dlg.setLabelText(ev.message or ev.stage)

    def finish():
        dlg.reset()
        dlg.close()

    job.signals.progress.connect(on_progress)
    job.signals.done.connect(lambda r: (finish(), on_done(r)))
    job.signals.failed.connect(lambda m: (finish(), (on_failed or (lambda _m: None))(m)))
    job.signals.cancelled.connect(finish)
    dlg.canceled.connect(job.cancel)
    dlg.show()
    return start(job)
