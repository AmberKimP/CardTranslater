"""동작 로그: 테스트하는 사람이 문제를 보내 줄 수 있게 파일로 남긴다.

위치: %LOCALAPPDATA%/CardTranslater/logs/ (환경변수 CARDTRANSLATER_LOG_DIR로 바꿀 수 있다)
- cardtranslater.log      작업 시작·끝·실패, 진행 메시지, Claude 호출, 보여 준 오류 창 (2MB × 5개 돌려 씀)
- crash.log               파이썬이 통째로 죽을 때(faulthandler)의 스택
API 키(sk-ant-…)는 기록하기 전에 가린다. 화면 코드 없음: 화면은 service를 통해 부른다.

문제를 받았을 때 고칠 수 있게:
- context(파일=…, 쪽=…, 영역=…): 그 안에서 남긴 경고·오류 줄 끝에 ‘| 위치: …’를 붙인다 (어느 카드에서 났는지).
- Timings: 작업 하나 안의 단계별 시간 (Claude 요청·원본 자르기·저장 …). 끝에 ‘[시간]’ 줄 하나로 남긴다.
  작업마다 ‘[시간] 작업 …’ 줄도 남아, 한 사이클(①~⑧)의 단계별 시간을 timing_report()로 모아 볼 수 있다.
"""

from __future__ import annotations

import contextlib
import contextvars
import faulthandler
import json
import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
import time
import zipfile
from pathlib import Path

LOG_NAME = "cardtranslater.log"
_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{4,}")
_state: dict = {}

log = logging.getLogger("cardtranslater")
tlog = logging.getLogger("cardtranslater.time")
TIME_TAG = "[시간]"
# 작업 함수 이름 -> 화면의 단계 이름 (사이클 시간표에 쓴다)
JOB_LABELS = {
    "extract_project": "① 글자 추출", "find_icons": "② 아이콘 정의", "finish_extraction": "④ 용어집 만들기",
    "import_glossary": "⑤ 용어집 확정 (엑셀에서)", "confirm_glossary_rows": "⑤ 용어집 확정", "translate_project": "⑥ 전체 번역", "export_project_excel": "검수 엑셀 만들기",
    "erase_project": "⑦ 글자 지우기", "compose_project": "⑧ 한글 넣기", "font_plans": "글꼴 추천",
    "redo_file": "카드 다시 만들기", "render_preview": "미리 보기", "test_run": "시험 실행",
}
_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("cardtranslater_log_ctx", default={})


@contextlib.contextmanager
def context(**kv):
    """이 안에서 남긴 경고·오류 줄에 위치(파일·쪽·영역 등)를 붙인다. 겹쳐 쓰면 합친다 (None 값은 뺀다).
    작업 스레드(ThreadPoolExecutor)로는 넘어가지 않는다: 주 스레드에서 결과를 처리할 때 감싼다."""
    tok = _ctx.set({**_ctx.get(), **{k: v for k, v in kv.items() if v is not None}})
    try:
        yield
    finally:
        _ctx.reset(tok)


def current_context() -> dict:
    return dict(_ctx.get())


def _fmt_ctx(ctx: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in ctx.items())


class Timings:
    """작업 하나 안의 단계별 시간. part()/add()는 여러 스레드에서 불러도 된다.
    단계 시간을 더한 값은 동시에 한 일(동시 요청 등) 때문에 전체 시간보다 클 수 있다."""

    def __init__(self, job: str):
        self.job = job
        self.t0 = time.monotonic()
        self._lock = threading.Lock()
        self.parts: dict[str, list] = {}  # 이름 -> [합, 횟수, 최대]

    def add(self, name: str, secs: float, n: int = 1) -> None:
        with self._lock:
            p = self.parts.setdefault(name, [0.0, 0, 0.0])
            p[0] += secs
            p[1] += n
            p[2] = max(p[2], secs / max(n, 1))

    @contextlib.contextmanager
    def part(self, name: str):
        t = time.monotonic()
        try:
            yield
        finally:
            self.add(name, time.monotonic() - t)

    def timed(self, name: str, fn):
        """fn()을 부르고 걸린 시간을 name에 더하는 함수 (작업 스레드에 넘길 때)."""
        def run():
            with self.part(name):
                return fn()
        return run

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def summary(self, **extra) -> str:
        with self._lock:  # 취소 뒤에도 작업 스레드가 add()할 수 있다
            parts = {k: list(v) for k, v in self.parts.items()}
        bits = []
        for name, (tot, n, mx) in sorted(parts.items(), key=lambda kv: -kv[1][0]):
            bits.append(f"{name} {tot:.1f}초" + (f" ({n}회, 평균 {tot / n:.2f}초, 최대 {mx:.2f}초)" if n > 1 else ""))
        bits += [f"{k} {v}" for k, v in extra.items() if v not in (None, "")]
        return f"{TIME_TAG} {self.job} {self.elapsed():.1f}초" + (" | " + " | ".join(bits) if bits else "")

    def log(self, **extra) -> str:
        try:
            line = self.summary(**extra)
        except Exception:  # noqa: BLE001 - 로그 때문에 끝난 작업을 실패로 만들지 않는다
            log.exception("단계별 시간을 적지 못함: %s", self.job)
            return ""
        tlog.info("%s", line)
        return line


def safe(fn, *args, **kwargs) -> None:
    """로그 요약 함수를 부르되, 그 안의 오류가 이미 끝난 작업을 실패로 만들지 않게 (오류는 로그에만)."""
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        log.exception("로그 요약을 적지 못함: %s", getattr(fn, "__name__", fn))


def log_job_time(name: str, secs: float, state: str) -> None:
    """작업 하나가 끝날 때 (화면 작업 실행기가 부른다): 사이클 시간표의 한 줄."""
    tlog.info("%s 작업 %s (%s) %.1f초 %s", TIME_TAG, JOB_LABELS.get(name, name), name, secs, state)


def timing_report(text: str | None = None, last: int = 400) -> str:
    """로그에서 ‘[시간]’ 줄만 모아 읽기 쉬운 표로 (최근 last줄). text가 없으면 지금 로그 파일들."""
    if text is None:
        d = Path(_state["file"]).parent if _state.get("file") else log_dir()
        files = sorted(d.glob(LOG_NAME + "*"), key=lambda f: f.stat().st_mtime) if d.exists() else []
        text = "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in files)
    rows = [ln for ln in text.splitlines() if TIME_TAG in ln]
    rows = rows[-last:] if last and last > 0 else rows
    out = ["단계별 시간 (오래된 것부터). ‘작업’ 줄이 화면에서 누른 단계 하나, 그 위 줄들이 그 단계 안의 자세한 시간.", ""]
    for ln in rows:
        stamp = ln[:19]
        body = ln.split(TIME_TAG, 1)[1].strip()
        out.append(f"{stamp}  {'' if body.startswith('작업 ') else '    '}{body}")
    return "\n".join(out) + "\n"


def log_dir() -> Path:
    env = os.environ.get("CARDTRANSLATER_LOG_DIR")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "CardTranslater" / "logs"


def mask(text: str) -> str:
    return _KEY_RE.sub(lambda m: m.group(0)[:7] + "…(가림)", text)


class _MaskFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        ctx = _ctx.get()
        if ctx and record.levelno >= logging.WARNING:  # 어느 파일·카드에서 났는지
            msg += f" | 위치: {_fmt_ctx(ctx)}"
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        record.msg, record.args = mask(msg), None
        if record.exc_text:
            record.exc_text = mask(record.exc_text)
        return True


def build_info() -> dict:
    """빌드할 때 넣은 정보(build_info.json: 버전·커밋·시각). 개발 중에는 {"version": "dev"}."""
    from app.settings import resource_root

    p = resource_root() / "build_info.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": "dev"}


def setup(directory: Path | None = None) -> Path:
    """로그 파일을 연다 (여러 번 불러도 한 번만). 로그 파일 경로를 돌려준다."""
    if _state.get("file"):
        return _state["file"]
    d = Path(directory) if directory else log_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / LOG_NAME
    h = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"))
    h.addFilter(_MaskFilter())
    log.setLevel(logging.DEBUG)
    log.addHandler(h)
    log.propagate = False
    _state.update(file=path, handler=h)

    try:  # 통째로 죽을 때(네이티브 오류)도 남게
        crash = open(d / "crash.log", "a", encoding="utf-8")  # noqa: SIM115 - 끝날 때까지 열어 둔다
        crash.write(f"\n=== 시작 {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        crash.flush()
        faulthandler.enable(crash)
        _state["crash"] = crash
    except (OSError, RuntimeError, ValueError):
        pass

    def excepthook(t, v, tb):
        log.critical("처리되지 않은 예외", exc_info=(t, v, tb))
        sys.__excepthook__(t, v, tb)

    def thread_hook(args):
        log.critical("작업 스레드에서 처리되지 않은 예외 (%s)", getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    return path


def log_startup(extra: dict | None = None) -> None:
    info = {"build": build_info(), "windows": platform.platform(), "python": sys.version.split()[0],
            "frozen": bool(getattr(sys, "frozen", False)), "exe": sys.executable}
    info.update(extra or {})
    log.info("=== CardTranslater 시작 === %s", json.dumps(info, ensure_ascii=False, default=str))


def export(dest: Path, extra: dict | None = None) -> Path:
    """로그 파일들과 환경 정보(system.json)를 zip 하나로 묶는다. 묶은 파일 경로를 돌려준다."""
    d = Path(_state["file"]).parent if _state.get("file") else log_dir()
    h = _state.get("handler")
    if h:
        h.flush()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    info = {"build": build_info(), "windows": platform.platform(), "python": sys.version.split()[0],
            "exported": time.strftime("%Y-%m-%d %H:%M:%S")}
    info.update(extra or {})
    texts = []
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        files = sorted(d.glob("*.log*")) if d.exists() else []
        for f in files:
            t = mask(f.read_text(encoding="utf-8", errors="replace"))
            z.writestr(f.name, t)
            if f.name.startswith(LOG_NAME):
                texts.append((f.stat().st_mtime, t))
        z.writestr("system.json", mask(json.dumps(info, ensure_ascii=False, indent=2, default=str)))
        joined = "\n".join(t for _m, t in sorted(texts))
        if TIME_TAG in joined:  # 단계별 시간만 모은 표 (묶은 로그에서)
            z.writestr("timings.txt", timing_report(joined))
    log.info("로그를 묶었습니다: %s", dest)
    return dest


def shorten(args: list, limit: int = 160) -> str:
    """명령줄을 기록용으로 줄인다 (긴 지시문은 앞부분만)."""
    out = []
    for a in args:
        s = str(a)
        out.append(s if len(s) <= limit else s[:limit] + f"…(+{len(s) - limit}자)")
    return " ".join(out)
