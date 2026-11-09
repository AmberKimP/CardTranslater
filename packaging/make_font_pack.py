"""글꼴 설치 파일 만들기 (Inno Setup 6의 ISCC.exe). 프로그램 설치 파일과 따로 둔다.

    .venv/Scripts/python scripts/fetch_fonts.py          # 먼저 fonts/에 글꼴을 받는다
    .venv/Scripts/python packaging/make_font_pack.py     # dist/CardTranslater-Fonts-<묶음 버전>.exe

- 프로그램에 들어 있는 기본 글꼴(catalog.json의 "bundled": true)을 뺀 모든 글꼴 계열을 넣는다.
- 설치 위치는 %LOCALAPPDATA%\\CardTranslater\\fontpack\\fonts (app/font_files.pack_dir). 프로그램을 업데이트해도 남는다.
- 글꼴 묶음 버전은 catalog.json의 pack_version. 글꼴을 더하거나 바꾸면 올리고 이 설치 파일만 다시 만든다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))
from build import font_families, stage_fonts  # noqa: E402
from make_installer import find_iscc  # noqa: E402


def main() -> int:
    cat = json.loads((ROOT / "fonts" / "catalog.json").read_text(encoding="utf-8"))
    ver = int(cat["pack_version"])
    fams = font_families(bundled=False)
    missing = []
    for fam in cat["families"]:
        if fam["dir"] not in fams:
            continue
        d = ROOT / "fonts" / fam["dir"]
        for f in fam["source_files"]:
            if "[" in f:
                stem = f.split("[")[0]
                missing += [f"{fam['dir']}/{stem}-{w}.ttf" for w in fam.get("static_weights", {})
                            if not (d / f"{stem}-{w}.ttf").exists()]
            elif not (d / f).exists():
                missing.append(f"{fam['dir']}/{f}")
    if missing:
        print("글꼴 파일이 없습니다 (먼저 scripts/fetch_fonts.py):", ", ".join(missing[:8]))
        return 1
    stage = ROOT / "build" / "fontpack_stage"
    n = stage_fonts(stage, fams)
    (stage / "pack.json").write_text(json.dumps({"pack_version": ver}), encoding="utf-8")
    print(f"[font pack] 글꼴 계열 {len(fams)}개, 파일 {n}개 (묶음 버전 {ver})")
    iscc = find_iscc()
    if iscc is None:
        print("Inno Setup이 없습니다: winget install JRSoftware.InnoSetup")
        return 1
    subprocess.run([str(iscc), f"/DPackVersion={ver}", str(ROOT / "packaging" / "fontpack.iss")], check=True)
    out = ROOT / "dist" / f"CardTranslater-Fonts-{ver}.exe"
    print(f"[font pack] {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
