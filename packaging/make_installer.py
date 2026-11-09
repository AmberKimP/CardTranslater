"""설치 파일 만들기 (Inno Setup 6의 ISCC.exe).

    .venv/Scripts/python packaging/build.py            # 먼저 dist/CardTranslater/
    .venv/Scripts/python packaging/make_installer.py   # dist/CardTranslater-Setup-<버전>-<날짜>.exe

글꼴 설치 파일은 글꼴이 바뀌었을 때만 이어서 만든다 (make_font_pack.py --if-changed).

버전은 app/version.py, 뒤에 만든 날짜(YYYYMMDD)를 붙인다 (예: CardTranslater-Setup-1.0.0-20261108.exe). Inno Setup이 없으면: winget install JRSoftware.InnoSetup
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.version import VERSION  # noqa: E402


def find_iscc() -> Path | None:
    cands = [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
             Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"), Path(r"C:\Program Files\Inno Setup 6\ISCC.exe")]
    found = shutil.which("iscc")
    if found:
        cands.insert(0, Path(found))
    return next((c for c in cands if c.exists()), None)


def main() -> int:
    exe = ROOT / "dist" / "CardTranslater" / "CardTranslater.exe"
    if not exe.exists():
        print("먼저 빌드하세요: .venv/Scripts/python packaging/build.py")
        return 1
    import json

    info = ROOT / "dist" / "CardTranslater" / "_internal" / "build_info.json"
    built = json.loads(info.read_text(encoding="utf-8")).get("version") if info.exists() else None
    if built != VERSION:  # 빌드가 실패해 예전 프로그램이 남아 있으면 새 버전 이름으로 묶지 않는다
        print(f"빌드 결과가 지금 버전이 아닙니다 (빌드 {built}, 지금 {VERSION}). 먼저 packaging/build.py를 성공시키세요"
              " (dist의 CardTranslater.exe가 켜져 있으면 끄고).")
        return 1
    iscc = find_iscc()
    if iscc is None:
        print("Inno Setup이 없습니다: winget install JRSoftware.InnoSetup")
        return 1
    name = f"{VERSION}-{time.strftime('%Y%m%d')}"
    subprocess.run([str(iscc), f"/DAppVersion={VERSION}", f"/DSetupName={name}",
                    str(ROOT / "packaging" / "installer.iss")], check=True)
    out = ROOT / "dist" / f"CardTranslater-Setup-{name}.exe"
    print(f"[installer] {out} ({out.stat().st_size / 1e6:.1f} MB)")
    sys.path.insert(0, str(ROOT / "packaging"))
    from make_font_pack import main as font_pack

    return font_pack(["--if-changed"])


if __name__ == "__main__":
    sys.exit(main())
