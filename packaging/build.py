"""Windows exe 빌드 (PyInstaller 폴더 방식, PRD 6장).

    .venv/Scripts/python packaging/build.py            # dist/CardTranslater/CardTranslater.exe
    .venv/Scripts/python packaging/build.py --no-test  # 빌드만 (자가 점검 생략)

- 글꼴은 정적 파일(*.ttf/*.otf)과 catalog.json·라이선스(OFL.txt)만 넣는다 (가변 글꼴 원본 [wght]는 뺀다).
- 빌드 뒤 exe를 --selftest로 실행해 글꼴·라이브러리·한글 그리기·화면 생성을 점검한다.
- .env(API 키)와 projects/·games/는 넣지 않는다. exe는 데이터 폴더(기본 문서/CardTranslater)를 쓴다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.version import VERSION  # noqa: E402
NAME = "CardTranslater"
EXCLUDE = ["tkinter", "pytest", "_pytest", "matplotlib", "IPython", "PyQt5", "PyQt6",
           "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "PySide6.QtQuick",
           "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization"]


def stage_fonts(dst: Path) -> int:
    if dst.exists():
        shutil.rmtree(dst)
    n = 0
    for f in (ROOT / "fonts").rglob("*"):
        if f.is_dir():
            continue
        keep = (f.suffix.lower() in (".ttf", ".otf") and "[" not in f.name) or f.name in ("catalog.json", "OFL.txt")
        if keep:
            out = dst / f.relative_to(ROOT / "fonts")
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
            n += 1
    return n


def build() -> Path:
    work = ROOT / "build"
    fonts = work / "fonts_stage"
    n = stage_fonts(fonts)
    print(f"[build] 글꼴 {n}개 파일")
    info = work / "build_info.json"  # 로그에 남길 버전 (app/applog.build_info)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    info.write_text(json.dumps({"version": VERSION, "built": time.strftime("%Y-%m-%d %H:%M"),
                                "commit": commit.stdout.strip() + ("+수정" if dirty else "")}, ensure_ascii=False),
                    encoding="utf-8")
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", NAME,
            "--distpath", str(work / "dist"), "--workpath", str(work / "pyi"), "--specpath", str(work),
            "--paths", str(ROOT), "--add-data", f"{fonts}{os.pathsep}fonts",
            "--add-data", f"{ROOT / 'assets'}{os.pathsep}assets",  # 로그인 화면 그림
            "--add-data", f"{info}{os.pathsep}.",
            "--add-data", f"{ROOT / 'CHANGELOG.md'}{os.pathsep}."]  # 도움말 > 릴리스 노트
    for m in EXCLUDE:
        args += ["--exclude-module", m]
    args.append(str(ROOT / "packaging" / "launch.py"))
    subprocess.run(args, check=True, cwd=ROOT)
    # build/dist에 만든 뒤 dist/<NAME>로 옮긴다. 폴더 자체는 지우지 않고 안만 비우므로, 그 폴더를 터미널·탐색기가
    # 열고 있어도 된다. 실행 중인 exe가 있으면 파일이 잠겨 여기서 실패한다 (먼저 끈다).
    out = ROOT / "dist" / NAME
    out.mkdir(parents=True, exist_ok=True)
    for child in out.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    shutil.copytree(work / "dist" / NAME, out, dirs_exist_ok=True)
    shutil.copy2(ROOT / "CHANGELOG.md", out / "릴리스 노트.md")  # 설치 폴더에서도 바로 열어 볼 수 있게
    exe = out / f"{NAME}.exe"
    print(f"[build] {exe}")
    return exe


def selftest(exe: Path) -> bool:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "selftest.json"
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        r = subprocess.run([str(exe), "--selftest", str(out)], env=env, timeout=180)
        res = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {"ok": False, "error": "결과 없음"}
    print("[selftest]", json.dumps(res, ensure_ascii=False, indent=2))
    return r.returncode == 0 and res.get("ok")


def size_mb(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6


if __name__ == "__main__":
    exe = build()
    print(f"[build] 크기: {size_mb(exe.parent):.0f} MB")
    if "--no-test" not in sys.argv:
        ok = selftest(exe)
        print("[selftest]", "통과" if ok else "실패")
        sys.exit(0 if ok else 1)
