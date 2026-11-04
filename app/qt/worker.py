"""긴 작업을 작업 스레드에서 돌리고 진행률·결과를 Signal로 화면 스레드에 넘긴다.

service 함수의 progress 콜백(ProgressEvent)과 CancelToken을 그대로 쓴다.
"""

from __future__ import annotations

import traceback
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import QProgressDialog, QWidget

from app import service


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
        try:
            kw = dict(self.kwargs)
            if self.with_progress:
                kw.update(progress=self.signals.progress.emit, cancel=self.token)
            result = self.fn(*self.args, **kw)
        except service.Cancelled:
            self.signals.cancelled.emit()
            return
        except Exception as e:  # noqa: BLE001 - 화면에 보여 준다
            self.signals.failed.emit(f"{e}\n\n{traceback.format_exc(limit=3)}")
            return
        self.signals.done.emit(result)

    def cancel(self) -> None:
        self.token.cancel()


_running: set[Job] = set()  # 끝나기 전에 가비지 컬렉션되지 않게


def start(job: Job) -> Job:
    _running.add(job)
    for sig in (job.signals.done, job.signals.failed, job.signals.cancelled):
        sig.connect(lambda *_a, j=job: _running.discard(j))
    QThreadPool.globalInstance().start(job)
    return job


def run_with_dialog(parent: QWidget | None, title: str, job: Job,
                    on_done: Callable[[object], None], on_failed: Callable[[str], None] | None = None) -> Job:
    """진행률 창(취소 버튼 포함)을 띄우고 job을 돌린다."""
    dlg = QProgressDialog(title, "취소", 0, 0, parent)
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
