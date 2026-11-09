"""Claude Code 찾기·설치 (실제로 내려받거나 npm을 부르지 않는다)."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

from app import claude_setup as cs


@pytest.fixture
def home(tmp_path, monkeypatch):
    """PATH·사용자 폴더를 비운 상태."""
    monkeypatch.setattr("shutil.which", lambda *_a, **_k: None)
    for k, sub in (("USERPROFILE", "home"), ("APPDATA", "roaming"), ("LOCALAPPDATA", "local")):
        monkeypatch.setenv(k, str(tmp_path / sub))
    monkeypatch.delenv("ProgramFiles", raising=False)
    return tmp_path


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def test_find_claude_outside_path(home):
    assert cs.find_claude() is None
    exe = _touch(home / "home" / ".local" / "bin" / "claude.exe")  # 공식 설치 프로그램, PATH 반영 전
    assert cs.find_claude() == str(exe)


def test_find_claude_prefers_path(home, monkeypatch):
    _touch(home / "home" / ".local" / "bin" / "claude.exe")
    monkeypatch.setattr("shutil.which", lambda name, *_a, **_k: r"C:\bin\claude.exe" if name == "claude" else None)
    assert cs.find_claude() == r"C:\bin\claude.exe"


def test_find_claude_own_install(home):
    exe = _touch(cs.tools_dir() / "claude" / cs._EXE_IN_PKG)
    assert cs.find_claude() == str(exe)


class FakeRun:
    def __init__(self, node_major=24, npm_ok=True, make_exe=True):
        self.calls = []
        self.node_major, self.npm_ok, self.make_exe = node_major, npm_ok, make_exe

    def __call__(self, args, cwd, timeout, env=None):
        self.calls.append(args)
        if args[-1] == "--version" and args[0].endswith("node.exe"):
            return 0, f"v{self.node_major}.1.0\n", ""
        if "install" in args:
            if not self.npm_ok:
                return 1, "", "npm ERR! network\nnpm ERR! fail"
            assert str(Path(args[0]).parent) in env["PATH"]  # 설치 스크립트가 node를 찾게
            if self.make_exe:
                _touch(Path(args[args.index("--prefix") + 1]) / cs._EXE_IN_PKG)
            return 0, "added 2 packages", ""
        if args[-1] == "--version":
            return 0, "2.1.318 (Claude Code)\n", ""
        raise AssertionError(args)


def _no_native(*_a):
    raise cs.SetupError("downloads.claude.ai에 연결하지 못했습니다")


def _fake_node(dirpath: Path) -> Path:
    node = _touch(dirpath / "node.exe")
    _touch(dirpath / "node_modules" / "npm" / "bin" / "npm-cli.js")
    return node


@pytest.mark.skipif(sys.platform != "win32", reason="자동 설치는 Windows만")
def test_install_downloads_node_when_missing(home):
    run, events, installed = FakeRun(), [], []

    def node_installer(say, cancel):
        installed.append(1)
        return _fake_node(cs.tools_dir() / "node")

    exe = cs.install_claude_code(lambda *a: events.append(a), run=run, node_installer=node_installer, native_installer=_no_native)
    assert installed and exe == str(cs.tools_dir() / "claude" / cs._EXE_IN_PKG)
    assert any("install" in c for c in run.calls) and events[-1][0] == "done"
    assert cs.find_claude() == exe  # 다음부터는 PATH 없이도 찾는다


@pytest.mark.skipif(sys.platform != "win32", reason="자동 설치는 Windows만")
def test_install_uses_existing_node_and_rejects_old(home, monkeypatch):
    pf = home / "pf"
    monkeypatch.setenv("ProgramFiles", str(pf))
    _fake_node(pf / "nodejs")
    installed = []
    cs.install_claude_code(run=FakeRun(node_major=24), node_installer=lambda *_a: installed.append(1),
                           native_installer=_no_native)
    assert not installed  # 시스템 Node 22+ → 그대로 사용

    (cs.tools_dir() / "claude").rename(home / "gone")  # 다시 없는 상태로
    called = []
    cs.install_claude_code(run=FakeRun(node_major=18),
                           node_installer=lambda *_a: called.append(1) or _fake_node(cs.tools_dir() / "node"),
                           native_installer=_no_native)
    assert called  # Node 18은 너무 낮아 새로 받는다


@pytest.mark.skipif(sys.platform != "win32", reason="자동 설치는 Windows만")
def test_install_reports_npm_failure(home):
    with pytest.raises(cs.SetupError, match="npm"):
        cs.install_claude_code(run=FakeRun(npm_ok=False),
                               node_installer=lambda *_a: _fake_node(cs.tools_dir() / "node"), native_installer=_no_native)


def test_install_returns_existing(home):
    exe = _touch(home / "home" / ".local" / "bin" / "claude.exe")
    assert cs.install_claude_code(run=lambda *a, **k: pytest.fail("부르면 안 됨")) == str(exe)


def _node_zip(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{name}/node.exe", b"node")
        z.writestr(f"{name}/node_modules/npm/bin/npm-cli.js", b"//")
    return buf.getvalue()


def _fetcher(name, data, sha=None):
    index = json.dumps([{"version": "v25.0.0", "lts": False}, {"version": "v24.2.0", "lts": "Krypton"}])

    def fetch(url, timeout=60):
        if url.endswith("index.json"):
            return index.encode()
        if url.endswith("SHASUMS256.txt"):
            return f"{sha or hashlib.sha256(data).hexdigest()}  {name}.zip\n".encode()
        raise AssertionError(url)

    def download(url, dest, progress, cancel):
        assert url.endswith(f"v24.2.0/{name}.zip")
        dest.write_bytes(data)

    return fetch, download


def test_install_node_verifies_and_extracts(home, monkeypatch):
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    name = "node-v24.2.0-win-x64"
    data = _node_zip(name)
    fetch, download = _fetcher(name, data)
    node = cs.install_node(lambda *a: None, fetch=fetch, download=download)
    assert node == cs.tools_dir() / "node" / "node.exe" and node.is_file()
    assert cs._npm_cli(node) and not list(cs.tools_dir().glob("*.zip"))

    fetch, download = _fetcher(name, data, sha="0" * 64)
    with pytest.raises(cs.SetupError, match="체크섬"):
        cs.install_node(lambda *a: None, fetch=fetch, download=download)


# ---------------------------------------------------------------- 공식 claude.exe (Node.js 없이)
def _native_fakes(body=b"CLAUDE-EXE", checksum=None):
    want = checksum or hashlib.sha256(body).hexdigest()
    urls = []

    def fetch(url, timeout=60):
        urls.append(url)
        if url.endswith("/latest"):
            return b"2.1.319\n"
        if url.endswith("/manifest.json"):
            return json.dumps({"platforms": {"win32-x64": {"checksum": want}, "win32-arm64": {"checksum": want}}}).encode()
        raise AssertionError(url)

    def download(url, dest, progress, cancel, timeout=60, stage="", label=""):
        urls.append(url)
        progress(stage, len(body), len(body), f"{label} 받는 중")
        Path(dest).write_bytes(body)
    return fetch, download, urls


@pytest.mark.skipif(sys.platform != "win32", reason="자동 설치는 Windows만")
def test_native_install_downloads_verifies_and_runs_install(home):
    fetch, download, urls = _native_fakes()
    calls = []

    def run(args, cwd, timeout, env=None):
        calls.append(args)
        if args[1:] == ["install", "latest"]:
            assert Path(args[0]).read_bytes() == b"CLAUDE-EXE"  # 받은 파일로 설치
            _touch(home / "home" / ".local" / "bin" / "claude.exe")  # 공식 install이 두는 곳
            return 0, "Claude Code successfully installed", ""
        return 0, "2.1.319 (Claude Code)", ""
    exe = cs.install_claude_code(run=run, native_installer=lambda say, cancel, r: cs.install_native(
        say, cancel, r, fetch=fetch, download=download), node_installer=lambda *_a: pytest.fail("Node.js 필요 없음"))
    assert exe == str(home / "home" / ".local" / "bin" / "claude.exe")
    assert any(u.endswith("/2.1.319/win32-x64/claude.exe") or u.endswith("/2.1.319/win32-arm64/claude.exe") for u in urls)
    assert not list(cs.tools_dir().glob("*.part"))  # 받은 임시 파일은 지운다


@pytest.mark.skipif(sys.platform != "win32", reason="자동 설치는 Windows만")
def test_native_install_keeps_exe_when_install_step_fails(home):
    fetch, download, _urls = _native_fakes()

    def run(args, cwd, timeout, env=None):
        return (1, "", "install failed") if "install" in args else (0, "2.1.319 (Claude Code)", "")
    exe = cs.install_native(lambda *a: None, None, run, fetch=fetch, download=download)
    assert exe == str(cs.tools_dir() / "claude-native" / "claude.exe") and Path(exe).read_bytes() == b"CLAUDE-EXE"
    assert cs.find_claude() == exe  # 다음에 켜도 찾는다


@pytest.mark.skipif(sys.platform != "win32", reason="자동 설치는 Windows만")
def test_native_checksum_mismatch_falls_back_to_npm_and_reports_both(home):
    fetch, download, _urls = _native_fakes(checksum="0" * 64)

    def native(say, cancel, r):
        return cs.install_native(say, cancel, r, fetch=fetch, download=download)
    with pytest.raises(cs.SetupError) as ei:
        cs.install_claude_code(run=FakeRun(npm_ok=False), native_installer=native,
                               node_installer=lambda *_a: _fake_node(cs.tools_dir() / "node"))
    assert "손상" in str(ei.value) and "npm" in str(ei.value)
    assert not (cs.tools_dir() / "claude-native").exists()  # 손상된 파일은 쓰지 않는다
