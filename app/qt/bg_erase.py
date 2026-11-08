"""뒤에서 글자 지우기: 원문이 확정되면(④ 용어집 만들기부터) 지워야 할 파일을 한 파일씩 낮은 우선순위로 지운다.

- 지워야 할 파일 = 아직 안 지운 파일 + 지운 뒤 원문·영역·카드별 지우기 방식이 바뀐 파일 (service.files_needing_erase).
- ⑦ 글자 지우기를 누르면 우선순위를 높인다 (boost).
- 프로젝트를 바꾸거나 앱을 닫으면 멈춘다. 다음에 프로젝트를 열면 남은 파일부터 이어 한다.
- cleared/를 쓰거나 읽는 화면 작업(⑦ 글자 지우기, ⑧ 한글 넣기, 시험 실행, 결과 보기에서 다시 넣기)은
  run_exclusive로 돌린다: 뒤에서 지우기를 멈추고, 지우던 스레드가 정말 끝난 뒤에 시작하며, 끝나면 다시 이어 한다
  (같은 파일을 두 스레드가 함께 쓰지 않게).
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QThreadPool, Signal
from PySide6.QtWidgets import QProgressDialog

from app import service
from app.qt import worker

BG_STATUSES = ("glossary_review", "glossary_confirmed", "translating", "translated")  # ④ 용어집 만들기부터

_CURRENT: "BackgroundEraser | None" = None  # 지금 프로젝트 화면의 뒤에서 지우기 (결과 보기 창 등에서 찾는다)


def current() -> "BackgroundEraser | None":
    return _CURRENT


def run_exclusive(parent, title: str, job: worker.Job, on_done, on_failed=None) -> None:
    """cleared/를 다루는 화면 작업: 뒤에서 지우기를 멈추고 그 스레드가 끝나기를 기다린 뒤 돌리고, 끝나면(취소 포함) 놓아 준다."""
    bg = _CURRENT
    if bg is None:
        worker.run_with_dialog(parent, title, job, on_done, on_failed)
        return
    bg.hold()
    released = [False]

    def release(*_a):
        if not released[0]:
            released[0] = True
            bg.release()
    for sig in (job.signals.done, job.signals.failed, job.signals.cancelled):
        sig.connect(release)  # on_done보다 먼저 (on_done이 새로 고치며 뒤에서 지우기를 다시 시작할 수 있게)

    def go():
        worker.run_with_dialog(parent, title, job, on_done, on_failed)
    if bg.idle:
        go()
        return
    wait = QProgressDialog("뒤에서 지우던 쪽을 마무리하는 중…", None, 0, 0, parent)  # 몇 초 (한 쪽)
    wait.setWindowTitle(title)
    wait.setWindowModality(Qt.WindowModality.WindowModal)
    wait.setMinimumDuration(0)
    wait.show()
    bg.when_idle(lambda: (wait.close(), go()))


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
        global _CURRENT
        super().__init__(page)
        _CURRENT = self
        self.page = page
        self.holds = 0  # 화면 작업이 cleared/를 쓰는 중 (run_exclusive): 그동안 시작하지 않는다
        self._draining: set = set()  # 멈추라고 했지만 스레드가 아직 끝나지 않은 작업
        self._idle_cbs: list = []
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

    @property
    def idle(self) -> bool:
        """돌고 있는 지우기 스레드가 하나도 없다 (멈춘 것까지 끝남)."""
        return self.job is None and not self._draining

    def when_idle(self, cb) -> None:
        """멈춘 지우기 스레드가 모두 끝나면 cb()를 부른다 (이미 그렇다면 바로)."""
        if self.idle:
            cb()
        else:
            self._idle_cbs.append(cb)

    def hold(self) -> None:
        self.holds += 1
        self.stop()

    def release(self) -> None:
        self.holds = max(0, self.holds - 1)
        if not self.holds:
            self.kick()

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
        if self.active or self.holds or self._draining:
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
            if getattr(self.job, "signals", None) is not None:
                self._draining.add(self.job)  # 다음 쪽에서 멈춘다: 끝날 때까지 새로 시작하지 않는다
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
        for sig in (job.signals.done, job.signals.failed, job.signals.cancelled):
            sig.connect(lambda *_a, j=job: self._ended(j))
        job.signals.done.connect(lambda r, j=job, f=fd: self._after(j, f, getattr(r, "state", "done") not in ("done", "skipped")))
        job.signals.failed.connect(lambda _m, j=job, f=fd: self._after(j, f, True))
        worker.start(job)

    def _ended(self, job):
        """작업 스레드가 끝남 (끝·실패·취소). 멈춘 작업이 다 끝났으면 기다리던 화면 작업을 시작한다."""
        self._draining.discard(job)
        if self._draining or self.job is not None:
            return
        cbs, self._idle_cbs = self._idle_cbs, []
        for cb in cbs:
            cb()
        if not cbs and not self.holds:
            self.kick()

    def shutdown(self, wait_ms: int = 15000) -> None:
        """앱을 닫음: 멈추고 지우던 스레드가 끝나기를 (잠깐) 기다린다."""
        self.stop()
        QThreadPool.globalInstance().waitForDone(wait_ms)

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
