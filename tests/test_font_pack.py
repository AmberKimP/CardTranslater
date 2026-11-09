"""글꼴 설치 파일 분리: 프로그램에는 기본 글꼴만, 나머지는 따로 설치한 글꼴 묶음에서 찾는다."""

import json
import sys
from pathlib import Path

from app import font_files, service
from tests.test_fonts import qapp  # noqa: F401 (화면 테스트용 QApplication)

REPO = Path(__file__).resolve().parent.parent


def _pack(tmp_path, monkeypatch, families=(), version=1):
    pack = tmp_path / "pack"
    pack.mkdir()
    monkeypatch.setenv(font_files.PACK_ENV, str(pack))
    (pack / "pack.json").write_text(json.dumps({"pack_version": version}), encoding="utf-8")
    if families:
        (pack / "catalog.json").write_text(json.dumps({"families": list(families)}), encoding="utf-8")
    return pack


def test_resolve_prefers_program_folder_then_font_pack(tmp_path, monkeypatch):
    pack = _pack(tmp_path, monkeypatch)
    sans = "fonts/notosanskr/NotoSansKR-Regular.ttf"
    assert font_files.resolve(sans) == font_files.ROOT / sans  # 프로그램에 들어 있는 기본 글꼴
    (pack / "zzpack").mkdir()
    (pack / "zzpack" / "Z-Regular.ttf").write_bytes(b"x")
    assert font_files.resolve("fonts/zzpack/Z-Regular.ttf") == pack / "zzpack" / "Z-Regular.ttf"
    assert font_files.resolve("fonts/zzpack/none.ttf") == font_files.ROOT / "fonts/zzpack/none.ttf"  # 어디에도 없음
    assert font_files.resolve(str(tmp_path / "abs.ttf")) == tmp_path / "abs.ttf"


def test_font_choices_and_status_include_pack_only_family(tmp_path, monkeypatch):
    src = REPO / "fonts" / "notosanskr" / "NotoSansKR-Regular.ttf"
    fam = {"name": "Zz Pack", "ko_name": "묶음", "dir": "zzpack", "source_files": ["ZzPack-Regular.ttf", "ZzPack-Bold.ttf"],
           "style": "sans"}
    pack = _pack(tmp_path, monkeypatch, [fam])
    (pack / "zzpack").mkdir()
    (pack / "zzpack" / "ZzPack-Regular.ttf").write_bytes(src.read_bytes())
    files = [c["file"] for c in service.font_choices()]
    assert "fonts/zzpack/ZzPack-Regular.ttf" in files  # 프로그램 목록에 없는 새 묶음 계열도 쓴다
    st = service.font_pack_status()
    assert st["installed"] == 1 and st["missing"] >= 1 and not st["ok"]  # Bold가 없다


def test_program_build_stages_only_bundled_fonts(tmp_path):
    sys.path.insert(0, str(REPO / "packaging"))
    from build import font_families, stage_fonts

    bundled = font_families(bundled=True)
    assert bundled == {"notosanskr"} and "gaegu" in font_families(bundled=False)
    stage_fonts(tmp_path / "s")
    tops = {p.relative_to(tmp_path / "s").parts[0] for p in (tmp_path / "s").rglob("*") if p.is_file()}
    assert tops == {"catalog.json", "notosanskr"}


def test_font_dialog_warns_when_font_pack_missing(qapp, monkeypatch):
    from app.qt import fonts_dialog
    from tests.test_fonts import _plan

    monkeypatch.setattr(service, "font_pack_status", lambda: {"wanted": 1, "installed": None, "missing": 50, "ok": False})
    dlg = fonts_dialog.FontPlanDialog(_plan())
    assert not dlg.pack_note.isHidden() and "CardTranslater-Fonts-1.exe" in dlg.pack_note.text()
    monkeypatch.setattr(service, "font_pack_status", lambda: {"wanted": 1, "installed": 1, "missing": 0, "ok": True})
    assert fonts_dialog.FontPlanDialog(_plan()).pack_note.isHidden()
