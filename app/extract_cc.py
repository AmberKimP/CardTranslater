"""글자 추출 엔진: Claude Code 요금제 (`claude -p` 헤드리스 모드). API 키가 필요 없다.

카드 한 장마다 영역 이미지를 PNG 파일로 쓰고(180도 영역은 바로 세워서), `claude -p`를 한 번 띄워
Read 도구로 읽게 한 뒤 결과를 JSON 스키마(--json-schema)로 받는다.

- 로그인된 Claude Code 요금제 사용량을 쓴다 (따로 결제 없음). 사용 한도에 걸리면 Paused로 멈추고,
  재설정 시각을 알면 그 시각을 알려 준다. 다시 실행하면 남은 카드부터 이어 간다.
- --bare 모드는 쓰지 않는다 (그 모드는 API 키만 쓴다).
- 일괄 처리(Batch API)는 API 전용이라 이 엔진에서는 쓸 수 없다.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app.extract import SYSTEM_PROMPT, Paused, region_crop_png

CC_MODEL_ALIAS = {"claude-opus-5-5": "opus", "claude-sonnet-5-5": "sonnet", "claude-haiku-5-5": "haiku"}
CALL_TIMEOUT_S = 600
CC_SYSTEM_PROMPT = SYSTEM_PROMPT + """

The region images are PNG files in the current directory. Use the Read tool to open every listed file
before answering. Do not use any other tool. Answer only with the structured output."""


class CardError(Exception):
    """이 카드만 실패 (다른 카드는 계속)."""


class NotAvailable(Exception):
    """claude 명령이 없거나 로그인되어 있지 않다 (기다려도 풀리지 않는다)."""


@dataclass
class CCReply:
    regions_json: dict  # {"regions": [...]}
    model: str
    cost_equiv_usd: float  # API 정가로 환산한 값 (요금제에서는 따로 청구되지 않음)
    usage: dict


def _run_subprocess(args: list[str], cwd: Path, timeout: float, input_text: str | None = None) -> tuple[int, str, str]:
    """input_text: 긴 지시문은 명령줄 대신 표준 입력으로 (Windows 명령줄 길이 제한 ≈ 32K자)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # 창 프로그램(exe)에서 콘솔 창이 깜빡이지 않게
    p = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, input=input_text, creationflags=flags)
    return p.returncode, p.stdout, p.stderr


class ClaudeCodeClient:
    """`claude -p`를 부르는 엔진. runner를 바꿔 끼우면 테스트에서 실제로 띄우지 않는다."""

    engine = "claude-code"

    def __init__(self, exe: str | None = None, runner=None):
        self.exe = exe or shutil.which("claude") or shutil.which("claude.exe") or shutil.which("claude.cmd")
        self.runner = runner or _run_subprocess
        if not self.exe and runner is None:
            raise NotAvailable("claude 명령을 찾지 못했습니다 (Claude Code 설치·PATH 확인)")

    def read_card(self, project, page_no: int, src_doc, model: str, work_dir: Path, schema: dict,
                  regions=None) -> CCReply:
        from app.extract import region_hint

        regions = project.template.regions_for(page_no) if regions is None else regions
        crop_dir = Path(work_dir) / "crops" / f"page-{page_no:03d}"
        crop_dir.mkdir(parents=True, exist_ok=True)
        lines = []
        lang = {"ja": "Japanese", "en": "English"}.get(project.template.source_language,
                                                       project.template.source_language)
        page = src_doc[page_no - 1]
        for i, reg in enumerate(regions, start=1):
            f = crop_dir / f"region{i:02d}.png"
            f.write_bytes(region_crop_png(page, reg))
            lines.append(f"- {f.name}: region name {reg.name} (kind: {reg.kind}). {region_hint(reg)}")
        prompt = (f"Source language: {lang}. Card page {page_no}. Read these region images in order:\n"
                  + "\n".join(lines) + "\nReturn one entry per region, in the order given.")
        args = [self.exe or "claude", "-p", prompt,
                "--output-format", "json",
                "--json-schema", json.dumps(schema, ensure_ascii=False),
                "--model", CC_MODEL_ALIAS.get(model, model),
                "--system-prompt", CC_SYSTEM_PROMPT,
                "--tools", "Read", "--allowedTools", "Read",
                "--permission-prompts", "none",
                "--no-session-persistence"]
        try:
            code, out, err = self.runner(args, crop_dir, CALL_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise Paused("Claude Code 응답이 너무 오래 걸림 (다시 실행하면 이어 감)", 60)
        except FileNotFoundError:
            raise NotAvailable("claude 명령을 실행하지 못했습니다")
        return parse_cc_output(code, out, err, model)

    def ask_json(self, prompt: str, schema: dict, model: str, system: str, work_dir: Path,
                 images: list[Path] | None = None) -> CCReply:
        """JSON 요청 (예: 용어 후보 뽑기·번역). 지시문은 표준 입력으로 보낸다. 도구는 쓰지 않는다.
        images가 있으면 작업 폴더에 복사해 두고 Read 도구로 열게 한다 (예: 아이콘 이름 제안)."""
        import shutil

        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        tools = ""
        if images:
            for p in images:
                if Path(p).resolve().parent != work_dir.resolve():
                    shutil.copy2(p, work_dir / Path(p).name)
            prompt = ("Image files in the current folder (open each with Read): "
                      + ", ".join(Path(p).name for p in images) + "\n" + prompt)
            tools = "Read"
        args = [self.exe or "claude", "-p",
                "--output-format", "json",
                "--json-schema", json.dumps(schema, ensure_ascii=False),
                "--model", CC_MODEL_ALIAS.get(model, model),
                "--system-prompt", system,
                "--tools", tools, *(["--allowedTools", "Read"] if tools else []),
                "--permission-prompts", "none",
                "--no-session-persistence"]
        try:
            code, out, err = self.runner(args, work_dir, CALL_TIMEOUT_S, input_text=prompt)
        except subprocess.TimeoutExpired:
            raise Paused("Claude Code 응답이 너무 오래 걸림 (다시 실행하면 이어 감)", 60)
        except FileNotFoundError:
            raise NotAvailable("claude 명령을 실행하지 못했습니다")
        return parse_cc_output(code, out, err, model)


_LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|hit your limit|too many requests|overloaded|"
                       r"quota|resets?\b|try again later", re.I)
_LOGIN_RE = re.compile(r"not logged in|please run /login|/login|authentication|invalid api key|unauthori", re.I)


def _reset_seconds(text: str, now: float | None = None) -> float | None:
    """재설정 시각이 있으면 남은 초. 두 형식을 읽는다.

    - '...|1767312000' (유닉스 시각)
    - 'resets 1:10pm (Asia/Seoul)' / 'resets 3pm' / 'resets 13:10' (이 컴퓨터 시각 기준. 지났으면 다음 날)
    """
    now = time.time() if now is None else now
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        return max(60.0, float(m.group(1)) - now)
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, re.I)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        lt = time.localtime(now)
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
        if target <= now:
            target += 24 * 3600
        return max(60.0, target - now + 60)  # 1분 여유
    return None


def parse_cc_output(code: int, out: str, err: str, model: str) -> CCReply:
    text = (out or "").strip()
    try:
        data = json.loads(text) if text else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        msg = (err or text or f"종료 코드 {code}").strip()[:300]
        if _LOGIN_RE.search(msg):
            raise NotAvailable(f"Claude Code에 로그인되어 있지 않습니다: {msg}")
        if _LIMIT_RE.search(msg):
            raise Paused(f"Claude Code 사용 한도: {msg}", _reset_seconds(msg) or 300)
        raise CardError(f"claude 실행 오류: {msg}")
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        msg = str(data.get("result") or data.get("subtype") or "오류")[:300]
        status = data.get("api_error_status")
        if _LOGIN_RE.search(msg) or status in (401, 403):
            raise NotAvailable(f"Claude Code 인증 오류: {msg}")
        if status in (429, 529) or _LIMIT_RE.search(msg):
            raise Paused(f"Claude Code 사용 한도·과부하: {msg}", _reset_seconds(msg) or 300)
        if isinstance(status, int) and status >= 500:
            raise Paused(f"API 서버 오류({status})", 120)
        raise CardError(f"Claude Code 오류: {msg}")
    so = data.get("structured_output")
    if not isinstance(so, dict):
        try:
            so = json.loads(data.get("result") or "")
        except (json.JSONDecodeError, TypeError):
            raise CardError("구조화 출력이 없음")
    used = list((data.get("modelUsage") or {}).keys())
    return CCReply(so, used[0] if used else model, float(data.get("total_cost_usd") or 0.0), data.get("usage") or {})
