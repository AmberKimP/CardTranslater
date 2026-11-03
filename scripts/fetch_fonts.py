"""Download the Korean font catalog (all SIL OFL) from the Google Fonts GitHub repo.

Usage:  .venv/Scripts/python scripts/fetch_fonts.py
Needs:  fonttools (for making static weights out of variable fonts)

Fonts go to fonts/<family>/ with each family's OFL.txt. Variable fonts
(Noto Sans KR / Noto Serif KR) are also cut into static weights, because
PDF embedding needs one file per weight. Files that already exist are skipped.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "fonts"
RAW = "https://raw.githubusercontent.com/google/fonts/main/ofl/{dir}/{file}"


def download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  skip  {dest.relative_to(ROOT)}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
        f.write(r.read())
    tmp.replace(dest)
    print(f"  got   {dest.relative_to(ROOT)} ({dest.stat().st_size // 1024} KB)")


def make_static(var_path: Path, weights: dict[str, int]) -> list[str]:
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    made = []
    stem = var_path.name.split("[")[0]
    for style, wght in weights.items():
        out = var_path.parent / f"{stem}-{style}.ttf"
        made.append(out.name)
        if out.exists():
            print(f"  skip  {out.relative_to(ROOT)}")
            continue
        font = TTFont(var_path)
        instantiateVariableFont(font, {"wght": wght}, inplace=True, updateFontNames=True)
        font.save(out)
        print(f"  made  {out.relative_to(ROOT)} (wght={wght})")
    return made


def main() -> int:
    catalog = json.loads((FONTS / "catalog.json").read_text(encoding="utf-8"))
    for fam in catalog["families"]:
        print(fam["name"])
        d = FONTS / fam["dir"]
        download(RAW.format(dir=fam["dir"], file="OFL.txt"), d / "OFL.txt")
        for file in fam["source_files"]:
            download(RAW.format(dir=fam["dir"], file=file), d / file)
        if fam.get("static_weights"):
            var = d / fam["source_files"][0]
            make_static(var, fam["static_weights"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
