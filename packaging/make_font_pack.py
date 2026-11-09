"""글꼴 설치 파일 만들기 (Inno Setup 6의 ISCC.exe). 프로그램 설치 파일과 따로 둔다.

    .venv/Scripts/python scripts/fetch_fonts.py          # 먼저 fonts/에 글꼴을 받는다
    .venv/Scripts/python packaging/make_font_pack.py     # dist/CardTranslater-Fonts-<묶음 버전>-<글꼴 수>fonts.exe

- 프로그램에 들어 있는 기본 글꼴(catalog.json의 "bundled": true)을 뺀 모든 글꼴 계열을 넣는다.
- 설치 위치는 %LOCALAPPDATA%\\CardTranslater\\fontpack\\fonts (app/font_files.pack_dir). 프로그램을 업데이트해도 남는다.
- 글꼴 묶음 버전은 catalog.json의 pack_version. 글꼴을 더하거나 바꾸면 올리고 이 설치 파일만 다시 만든다.
- --if-changed: 넣을 글꼴 파일이 마지막으로 만든 글꼴 설치 파일(packaging/fontpack_stamp.json)과 같으면 만들지 않는다
  (make_installer.py가 부른다). 글꼴이 바뀌었는데 pack_version이 그대로면 멈춘다 (앱이 옛 묶음을 알아보지 못한다).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))
sys.path.insert(0, str(ROOT))
from build import font_families, stage_fonts  # noqa: E402
from make_installer import find_iscc  # noqa: E402


STAMP = ROOT / "packaging" / "fontpack_stamp.json"  # 마지막으로 만든 글꼴 설치 파일의 글꼴 (git에 둔다)


def fonts_hash(stage: Path) -> str:
    """글꼴 설치 파일에 들어갈 글꼴·라이선스 파일의 해시 (catalog.json·pack.json은 빼고: 이름표만 바뀐 것은 무시)."""
    h = hashlib.sha256()
    for f in sorted(p for p in stage.rglob("*") if p.is_file() and p.name not in ("catalog.json", "pack.json")):
        h.update(f.relative_to(stage).as_posix().encode("utf-8") + b"\0")
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    if_changed = "--if-changed" in (sys.argv[1:] if argv is None else argv)
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
    n_fonts = sum(1 for p in stage.rglob("*") if p.suffix.lower() in (".ttf", ".otf"))
    from app.font_files import pack_file_name, pack_files

    want = len(pack_files(cat))
    if n_fonts != want:  # 앱이 안내하는 파일 이름(글꼴 수)과 실제 설치 파일이 같아야 한다
        print(f"글꼴 수가 맞지 않습니다: 넣은 글꼴 {n_fonts}개, catalog.json 기준 {want}개")
        return 1
    from app.font_files import pack_file_name as _name

    digest = fonts_hash(stage)
    stamp = json.loads(STAMP.read_text(encoding="utf-8")) if STAMP.exists() else None
    if if_changed:
        if stamp is None and (ROOT / "dist" / _name(cat)).exists():  # 처음: 지금 있는 설치 파일을 기준으로 삼는다
            stamp = {"pack_version": ver, "fonts": n_fonts, "hash": digest}
            STAMP.write_text(json.dumps(stamp, indent=2) + "\n", encoding="utf-8")
        if stamp and stamp.get("hash") == digest:
            print(f"[font pack] 글꼴이 바뀌지 않아 글꼴 설치 파일을 만들지 않습니다 ({_name(cat)} 그대로)")
            return 0
    if stamp and stamp.get("hash") != digest and int(stamp.get("pack_version", 0)) >= ver:
        print(f"[font pack] 글꼴이 바뀌었습니다. fonts/catalog.json의 pack_version을 {ver + 1}로 올린 뒤 다시 하세요")
        return 1
    (stage / "pack.json").write_text(json.dumps({"pack_version": ver, "fonts": n_fonts}), encoding="utf-8")
    print(f"[font pack] 글꼴 계열 {len(fams)}개, 글꼴 {n_fonts}개, 파일 {n}개 (묶음 버전 {ver})")
    iscc = find_iscc()
    if iscc is None:
        print("Inno Setup이 없습니다: winget install JRSoftware.InnoSetup")
        return 1
    name = pack_file_name(cat)
    subprocess.run([str(iscc), f"/DPackVersion={ver}", f"/DOutputName={Path(name).stem}",
                    str(ROOT / "packaging" / "fontpack.iss")], check=True)
    out = ROOT / "dist" / name
    STAMP.write_text(json.dumps({"pack_version": ver, "fonts": n_fonts, "hash": digest}, indent=2) + "\n",
                     encoding="utf-8")
    print(f"[font pack] {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
