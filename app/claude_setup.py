"""Claude Code 찾기·설치.

찾기(find_claude): PATH → 알려진 설치 위치. 설치 직후에는 PATH가 아직 이 프로그램에 반영되지 않으므로
PATH만 보면 못 찾는다.

설치(install_claude_code): 공식 설치 프로그램(https://claude.ai/install.ps1)과 같은 방법을 먼저 쓴다 —
downloads.claude.ai에서 claude.exe 하나를 받아 manifest의 SHA256을 확인하고 `claude.exe install`로
사용자 폴더(~/.local/bin)에 설치한다. Node.js가 필요 없다. `install`이 실패하면 받은 claude.exe를
tools/claude-native에 두고 그대로 쓴다.
그것도 안 되면(내려받기 실패 등) 예전 방법: Node.js(22 이상)가 없으면 공식 zip(nodejs.org)을 받아
tools/node에 풀고 그 npm으로 @anthropic-ai/claude-code를 tools/claude에 설치한다. 관리자 권한이 필요 없다.
화면·print 없음: 진행은 progress(stage, done, total, message)로 알린다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

NATIVE_DIST = "https://downloads.claude.ai/claude-code-releases"  # 공식 설치 프로그램이 쓰는 곳
NODE_MIN_MAJOR = 22
NODE_DIST = "https://nodejs.org/dist"
CLAUDE_PACKAGE = "@anthropic-ai/claude-code"
_EXE_IN_PKG = Path("node_modules") / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
log = logging.getLogger("cardtranslater.setup")


class SetupError(Exception):
    """설치하지 못했다 (message는 사용자에게 그대로 보여 줄 수 있다)."""


def tools_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "CardTranslater" / "tools"


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def claude_candidates() -> list[Path]:
    """PATH 밖에서 claude를 찾아볼 위치 (앞쪽 우선). .cmd보다 실행 파일을 먼저 쓴다 (긴 인자 따옴표 문제)."""
    appdata = Path(os.environ.get("APPDATA") or _home() / "AppData" / "Roaming")
    local = Path(os.environ.get("LOCALAPPDATA") or _home() / "AppData" / "Local")
    return [
        _home() / ".local" / "bin" / "claude.exe",  # 공식 설치 프로그램 (install.ps1)
        appdata / "npm" / _EXE_IN_PKG,  # npm install -g (기본 위치)
        appdata / "npm" / "claude.cmd",
        local / "Microsoft" / "WinGet" / "Links" / "claude.exe",  # winget
        tools_dir() / "claude-native" / "claude.exe",  # 이 프로그램이 받은 공식 claude.exe (install 실패 때)
        tools_dir() / "claude" / _EXE_IN_PKG,  # 이 프로그램이 npm으로 설치한 것
    ]


def find_claude() -> str | None:
    """claude 실행 파일의 전체 경로. 없으면 None."""
    for name in ("claude", "claude.exe", "claude.cmd"):
        found = shutil.which(name)
        if found:
            return found
    return next((str(p) for p in claude_candidates() if p.is_file()), None)


# ---------------------------------------------------------------- Node.js
def _node_version(node: Path, run=None) -> int | None:
    """node 주 버전 (v24.1.0 → 24). 실행하지 못하면 None."""
    try:
        code, out, _err = (run or _run)([str(node), "--version"], None, 30)
    except OSError:
        return None
    try:
        return int(out.strip().lstrip("v").split(".")[0]) if code == 0 else None
    except ValueError:
        return None


def find_node(run=None) -> Path | None:
    """npm까지 함께 있는 node.exe (22 이상). 없으면 None."""
    cands = [tools_dir() / "node" / "node.exe"]
    found = shutil.which("node")
    if found:
        cands.insert(0, Path(found))
    pf = os.environ.get("ProgramFiles")
    if pf:
        cands.append(Path(pf) / "nodejs" / "node.exe")
    for node in cands:
        if node.is_file() and _npm_cli(node) and (_node_version(node, run) or 0) >= NODE_MIN_MAJOR:
            return node
    return None


def _npm_cli(node: Path) -> Path | None:
    p = node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    return p if p.is_file() else None


def _node_arch() -> str:
    m = os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or ""
    return "arm64" if m.upper() == "ARM64" else "x64"


def _fetch(url: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "CardTranslater-setup"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _download(url: str, dest: Path, progress, cancel, timeout: float = 60, stage: str = "node",
              label: str = "Node.js") -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "CardTranslater-setup"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if cancel is not None:
                cancel.check()
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            progress(stage, done, total or done, f"{label} 받는 중… {done / 1e6:.0f} / {total / 1e6:.0f} MB")


def _long(p: Path) -> Path:
    """Windows 경로 260자 제한을 넘는 파일(npm의 깊은 node_modules)도 다루게 \\\\?\\ 를 붙인다."""
    if sys.platform != "win32":
        return p
    s = str(Path(os.path.abspath(p)))
    return Path(s if s.startswith("\\\\?\\") else "\\\\?\\" + s)


def _extract_stripped(zpath: Path, dest: Path, top: str, cancel=None) -> None:
    """zip을 풀면서 맨 위 폴더(top/)는 빼고 dest 바로 아래에 둔다 (경로를 짧게)."""
    base = _long(dest)
    with zipfile.ZipFile(zpath) as z:
        for i, info in enumerate(z.infolist()):
            if cancel is not None and i % 500 == 0:
                cancel.check()
            parts = Path(info.filename.replace("\\", "/")).parts
            if parts and parts[0] == top:
                parts = parts[1:]
            if not parts or ".." in parts or Path(info.filename).is_absolute():
                continue
            out = base.joinpath(*parts)
            if info.is_dir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(out, "wb") as f:
                shutil.copyfileobj(src, f, 1 << 20)


def latest_node_lts(fetch=_fetch) -> str:
    """가장 최신 LTS 버전 (예: "v24.22.2"), NODE_MIN_MAJOR 이상."""
    for rel in json.loads(fetch(f"{NODE_DIST}/index.json")):
        if rel.get("lts") and int(rel["version"].lstrip("v").split(".")[0]) >= NODE_MIN_MAJOR:
            return rel["version"]
    raise SetupError("Node.js LTS 버전을 찾지 못했습니다")


def install_node(progress, cancel=None, fetch=_fetch, download=_download) -> Path:
    """공식 zip을 받아 SHA256을 확인하고 tools/node에 푼다. node.exe 경로를 돌려준다."""
    ver = latest_node_lts(fetch)
    name = f"node-{ver}-win-{_node_arch()}"
    log.info("Node.js 받기: %s", name)
    sums = fetch(f"{NODE_DIST}/{ver}/SHASUMS256.txt").decode("utf-8", "replace")
    want = next((ln.split()[0] for ln in sums.splitlines() if ln.strip().endswith(f"{name}.zip")), None)
    if not want:
        raise SetupError(f"{name}.zip의 체크섬을 찾지 못했습니다")

    root = tools_dir()
    root.mkdir(parents=True, exist_ok=True)
    zpath = root / f"{name}.zip"
    try:
        download(f"{NODE_DIST}/{ver}/{name}.zip", zpath, progress, cancel)
        h = hashlib.sha256()
        with open(zpath, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest().lower() != want.lower():
            raise SetupError("받은 Node.js 파일이 손상되었습니다 (체크섬 불일치) — 다시 시도하세요")
        progress("node", 0, 0, "Node.js 푸는 중…")
        tmp, dest = root / "node.tmp", root / "node"
        shutil.rmtree(_long(tmp), ignore_errors=True)
        _extract_stripped(zpath, tmp, name, cancel)
        shutil.rmtree(_long(dest), ignore_errors=True)
        _long(tmp).replace(_long(dest))
    finally:
        zpath.unlink(missing_ok=True)
    node = dest / "node.exe"
    if not node.is_file():
        raise SetupError("Node.js를 풀었지만 node.exe가 없습니다")
    return node


# ---------------------------------------------------------------- Claude Code (공식 claude.exe)
def _native_platform() -> str:
    return "win32-arm64" if _node_arch() == "arm64" else "win32-x64"


def install_native(progress, cancel=None, run=None, fetch=_fetch, download=_download) -> str:
    """공식 설치 프로그램과 같은 방법으로 claude.exe를 설치하고 경로를 돌려준다. 못 하면 SetupError."""
    import re

    run = run or _run
    try:
        ver = fetch(f"{NATIVE_DIST}/latest", 30).decode("utf-8", "replace").strip()
    except OSError as e:
        raise SetupError(f"Claude Code 버전을 확인하지 못했습니다 (인터넷 연결 확인): {e}") from e
    if not re.match(r"^\d+\.\d+\.\d+", ver):
        raise SetupError("downloads.claude.ai에 연결하지 못했습니다 (회사·학교 네트워크나 지원하지 않는 지역일 수 있습니다)")
    plat = _native_platform()
    try:
        man = json.loads(fetch(f"{NATIVE_DIST}/{ver}/manifest.json", 30))
        want = man["platforms"][plat]["checksum"]
    except (OSError, ValueError, KeyError) as e:
        raise SetupError(f"Claude Code 설치 정보를 받지 못했습니다: {e}") from e
    root = tools_dir()
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f"claude-{ver}-{plat}.exe.part"
    log.info("Claude Code 받기: %s %s", ver, plat)
    try:
        try:
            download(f"{NATIVE_DIST}/{ver}/{plat}/claude.exe", tmp, progress, cancel, 120, "claude", "Claude Code")
        except OSError as e:
            raise SetupError(f"Claude Code를 내려받지 못했습니다 (인터넷 연결 확인): {e}") from e
        progress("claude", 0, 0, "받은 파일 확인 중…")
        h = hashlib.sha256()
        with open(tmp, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest().lower() != str(want).lower():
            raise SetupError("받은 Claude Code 파일이 손상되었습니다 (체크섬 불일치) — 다시 시도하세요")
        progress("claude", 0, 0, "Claude Code 설치 중…")
        try:
            code, out, err = run([str(tmp), "install", "latest"], root, 600)
        except (OSError, subprocess.TimeoutExpired) as e:
            code, out, err = 1, "", str(e)
        exe = find_claude()
        if code != 0 or not exe:  # install이 안 돼도 받은 claude.exe는 그 자체로 실행된다: 이 프로그램 폴더에 둔다
            log.warning("claude install 실패 (코드 %s): %s", code, (err or out)[-500:])
            dest = root / "claude-native" / "claude.exe"
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp.replace(dest)
            exe = str(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return exe


# ---------------------------------------------------------------- Claude Code (npm, 예비)
def _run(args: list[str], cwd: Path | None, timeout: float, env: dict | None = None) -> tuple[int, str, str]:
    log.info("실행: %s", " ".join(str(a) for a in args))
    p = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=env, creationflags=_NO_WINDOW)
    log.log(logging.INFO if p.returncode == 0 else logging.WARNING, "  코드 %s%s", p.returncode,
            "" if p.returncode == 0 else f"\n  stdout 끝: {p.stdout[-1500:]}\n  stderr 끝: {p.stderr[-1500:]}")
    return p.returncode, p.stdout, p.stderr


def install_claude_code(progress: Callable | None = None, cancel=None, run=None,
                        node_installer=install_node, native_installer=install_native) -> str:
    """Claude Code를 설치하고 claude 실행 파일 경로를 돌려준다. 이미 있으면 그 경로.
    공식 claude.exe를 먼저 (Node.js 없이), 안 되면 Node.js + npm.
    progress(stage, done, total, message). cancel.check()는 취소되면 예외를 던진다."""
    say = progress or (lambda *_a: None)
    run = run or _run
    existing = find_claude()
    if existing:
        log.info("Claude Code가 이미 있음: %s", existing)
        return existing
    if sys.platform != "win32":
        raise SetupError("자동 설치는 Windows에서만 됩니다 — https://code.claude.com/docs 를 보고 설치하세요")

    say("claude", 0, 0, "Claude Code 받을 준비 중…")
    try:
        exe = native_installer(say, cancel, run)
        code, out, err = run([str(exe), "--version"], None, 60)
        if code == 0:
            log.info("Claude Code 설치 완료 (공식 claude.exe): %s (%s)", exe, out.strip())
            say("done", 1, 1, f"Claude Code 설치 완료 ({out.strip()})")
            return str(exe)
        native_err = f"설치한 claude.exe가 실행되지 않습니다: {(err or out).strip()[:200]}"
    except SetupError as e:
        native_err = str(e)
    log.warning("공식 claude.exe 설치 실패 → npm으로: %s", native_err)

    say("node", 0, 0, "Node.js 확인 중…")
    node = find_node(run)
    log.info("Node.js: %s", node or "없음 → 내려받기")
    if node is None:
        try:
            node = node_installer(say, cancel)
        except (SetupError, OSError) as e:
            raise SetupError(f"Claude Code를 설치하지 못했습니다.\n• 공식 설치: {native_err}\n• Node.js 받기: {e}") from e
        log.info("Node.js 설치함: %s", node)
    if cancel is not None:
        cancel.check()
    npm = _npm_cli(node)
    if npm is None:
        raise SetupError(f"npm을 찾지 못했습니다 ({node.parent})")

    prefix = tools_dir() / "claude"
    prefix.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")  # npm의 설치 스크립트가 node를 찾게
    say("claude", 0, 0, "Claude Code 설치 중… (몇 분 걸릴 수 있습니다)")
    try:
        code, out, err = run([str(node), str(npm), "install", "-g", CLAUDE_PACKAGE, "--prefix", str(prefix),
                              "--no-fund", "--no-audit"], prefix, 900, env)
    except subprocess.TimeoutExpired:
        raise SetupError("Claude Code 설치가 너무 오래 걸려 멈췄습니다 — 인터넷 연결을 확인하고 다시 시도하세요")
    if code != 0:
        tail = (err or out).strip().splitlines()[-6:]
        raise SetupError(f"Claude Code를 설치하지 못했습니다.\n• 공식 설치: {native_err}\n• npm 설치:\n"
                         + "\n".join(tail))

    exe = prefix / _EXE_IN_PKG
    if not exe.is_file():
        raise SetupError("설치는 끝났지만 claude.exe가 없습니다 (npm optional 의존성 확인)")
    code, out, err = run([str(exe), "--version"], prefix, 60, env)
    if code != 0:
        raise SetupError(f"Claude Code를 설치했지만 실행되지 않습니다: {(err or out).strip()[:200]}")
    log.info("Claude Code 설치 완료: %s (%s)", exe, out.strip())
    say("done", 1, 1, f"Claude Code 설치 완료 ({out.strip()})")
    return str(exe)
