"""뒤에서 글자 지우기: 원문이 확정되면(④ 용어집 만들기부터) 지워야 할 파일을 한 파일씩 낮은 우선순위로 지운다.

- 지워야 할 파일 = 아직 안 지운 파일 + 지운 뒤 원문·영역·카드별 지우기 방식이 바뀐 파일 (service.files_needing_erase).
- ⑦ 글자 지우기를 누르면 우선순위를 높인다 (boost).
- 프로젝트를 바꾸거나 앱을 닫으면 멈춘다. 다음에 프로젝트를 열면 남은 파일부터 이어 한다.
- 화면에서 ⑦을 직접 돌리는 동안(page.erasing)에는 시작하지 않는다 (같은 파일을 두 번 지우지 않게).
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from app import service
from app.qt import worker

BG_STATUSES = ("glossary_review", "glossary_confirmed", "translating", "translated")  # ④ 용어집 만들기부터


class _BgJob(worker.Job):
    """작업 스레드를 낮은 우선순위로 돌리는 Job (boost되면 보통 우선순위)."""

    def __init__(self, owner: "BackgroundEraser", *a, **k):
        super().__init__(*a, **k)
        self.owner = owner
        self.thread: QThread | None = None

    def run(self) -> None:
        self.thread = QThread.currentThread()
        self.thread.setPriority(QThread.Priority.NormalPriority if self.owner.boosted
                                else QThread.Priority.LowestPriority)
        try:
            super().run()
        finally:
            try:
                self.thread.setPriority(QThread.Priority.NormalPriority)  # 스레드 풀에 돌려줄 때는 보통으로
            except RuntimeError:
                pass
            self.thread = None


class BackgroundEraser(QObject):
    changed = Signal(str)  # 상태 글 (빈 글 = 하는 일 없음)
    file_done = Signal()

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.project_dir: Path | None = None
        self.queue: list[Path] = []
        self.total = 0
        self.job: _BgJob | None = None
        self.boosted = False
        self.text = ""
        self.failed: set[str] = set()  # 이번에 실패한 파일 (같은 세션에서 다시 하지 않음: 무한 반복 방지)
        self._current: Path | None = None

    # ---- 상태
    @property
    def active(self) -> bool:
        return self.job is not None

    def busy_with(self, file_dirs) -> bool:
        """지금 지우는 중이거나 지울 차례인 파일이 file_dirs(None=모두) 안에 있나."""
        if not self.active:
            return False
        if file_dirs is None:
            return True
        want = {str(Path(d)) for d in file_dirs}
        cur = getattr(self, "_current", None)
        return any(str(Path(d)) in want for d in self.queue + ([cur] if cur else []))

    def _set_text(self, t: str):
        self.text = t
        self.changed.emit(t)

    # ---- 시작·멈춤
    def kick(self):
        """할 일이 있으면 뒤에서 지우기를 시작한다 (프로젝트 화면을 새로 고칠 때마다 부름)."""
        pdir = getattr(self.page, "project_dir", None)
        if pdir is None or os.environ.get("CARDTRANSLATER_NO_BG_ERASE"):  # 테스트는 뒤에서 지우기를 끈다
            return
        if self.project_dir is not None and Path(pdir) != self.project_dir:
            self.stop()
        if self.active or getattr(self.page, "erasing", False):
            return
        ov = getattr(self.page, "ov", None) or {}
        if ov.get("status") not in BG_STATUSES:
            return
        if self.project_dir is None or Path(pdir) != self.project_dir:
            self.failed = set()
        try:
            files = [f for f in service.files_needing_erase(pdir) if str(f) not in self.failed]
        except Exception:  # noqa: BLE001 - 뒤에서 하는 일: 못 해도 화면은 그대로
            files = []
        if not files:
            if self.text:
                self._set_text("")
            return
        self.project_dir = Path(pdir)
        self.queue = list(files)
        self.total = len(files)
        self._next()

    def boost(self):
        """⑦ 글자 지우기를 누름: 지금 파일과 남은 파일을 보통 우선순위로."""
        self.boosted = True
        if self.job is not None and self.job.thread is not None:
            try:
                self.job.thread.setPriority(QThread.Priority.NormalPriority)
            except RuntimeError:
                pass
        if self.active:
            self._set_text(self.text.replace("(천천히)", "(서둘러)"))

    def stop(self):
        """멈춘다 (프로젝트를 바꾸거나 앱을 닫을 때). 지우던 파일은 저장되지 않고 다음에 다시 한다."""
        self.queue = []
        if self.job is not None:
            self.job.cancel()
        self.job = None
        self.boosted = False
        self._current = None
        self._set_text("")

    # ---- 한 파일씩
    def _next(self):
        if not self.queue or self.project_dir is None:
            self.job = None
            self._current = None
            self.boosted = False
            self._set_text("")
            self.file_done.emit()
            return
        fd = self.queue.pop(0)
        self._current = fd
        n = self.total - len(self.queue)
        label = f"뒤에서 글자 지우기 {n}/{self.total} 파일"
        self._set_text(f"{label} {self._pace()}")
        job = _BgJob(self, service.erase_project, self.project_dir, files=[fd], stale=True)
        self.job = job
        job.signals.progress.connect(lambda ev, b=label: self._progress(b, ev))
        job.signals.done.connect(lambda r, j=job, f=fd: self._after(j, f, getattr(r, "state", "done") not in ("done", "skipped")))
        job.signals.failed.connect(lambda _m, j=job, f=fd: self._after(j, f, True))
        job.signals.cancelled.connect(lambda j=job: None)
        worker.start(job)

    def _pace(self) -> str:
        return "(서둘러)" if self.boosted else "(천천히)"

    def _progress(self, base: str, ev):
        if getattr(ev, "total", 0):
            self._set_text(f"{base} {self._pace()} · {ev.done}/{ev.total}쪽")

    def _after(self, job, fd, failed: bool):
        if job is not self.job:  # 멈춘 뒤에 끝난 옛 작업
            return
        if failed:
            self.failed.add(str(fd))
        self.file_done.emit()
        self._next()
