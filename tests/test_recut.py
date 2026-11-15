"""이미지 시트 다시 자르기: 지금 방식으로 자르면 달라지는지 보기, 다시 자르기(휴지통·뺄 쪽 이어받기·실패하면 되돌림)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app import service, workspace
from app.service import _manage

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _sheet(path: Path) -> Path:
    im = Image.new("RGB", (900, 600), "white")
    d = ImageDraw.Draw(im)
    for x in (30, 480):
        d.rectangle([x, 30, x + 390, 540], fill=(200, 60, 60))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, dpi=(300, 300))
    return path


def _project(tmp_path, **kw):
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    r = service.add_images(info.dir, [_sheet(tmp_path / "in" / "s.png")], "A", cols=2, rows=1, new_group=True, **kw)
    return info, r.file_dir


def _shift_saved_boxes(fd: Path, dx: int) -> None:
    """예전 버전이 잘못 자른 것처럼 file.json에 남은 카드 자리를 옮긴다."""
    data = json.loads((fd / "file.json").read_text(encoding="utf-8"))
    for sheet in data["from_images"]["sheets"]:
        sheet["boxes"] = [[b[0] + dx, b[1], b[2] + dx, b[3]] for b in sheet["boxes"]]
    (fd / "file.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_recut_check_same_and_changed(tmp_path):
    _info, fd = _project(tmp_path)
    chk = service.recut_check(fd)
    assert chk.can and not chk.changed and chk.cards_before == chk.cards_after == 2
    _shift_saved_boxes(fd, 25)
    chk = service.recut_check(fd)
    assert chk.can and chk.changed and chk.moved_px == 25


def test_recut_check_refuses_pdf_manual_and_missing_images(tmp_path):
    from tests.test_service import make_project

    src = make_project(tmp_path / "src")
    info, _ = workspace.create_project("q", "G", root=tmp_path)
    workspace.add_files(info, [src / "source" / "cards.pdf"], "P", new_group=True)
    pdf_fd = workspace.file_dirs(info)[0]
    chk = service.recut_check(pdf_fd)
    assert not chk.can and "이미지" in chk.reason

    _info2, fd = _project(tmp_path / "m", boxes={str(tmp_path / "m" / "in" / "s.png"): [(30, 30, 420, 540)]})
    assert not service.recut_check(fd).can

    _info3, fd3 = _project(tmp_path / "x")
    for p in (fd3 / "source" / "images").iterdir():
        p.unlink()
    chk = service.recut_check(fd3)
    assert not chk.can and "원본 이미지" in chk.reason


def test_recut_file_replaces_file_keeps_exclusions_and_trashes_old(tmp_path):
    info, fd = _project(tmp_path)
    (fd / "extract" / "pages").mkdir(parents=True, exist_ok=True)
    (fd / "extract" / "pages" / "page-001.json").write_text("{}", encoding="utf-8")
    workspace.set_excluded_pages(fd, [2], "뒷면")
    _shift_saved_boxes(fd, 25)
    res = service.recut_file(fd)
    new = res.file_dir
    assert new.name == fd.name and new.parent == fd.parent and res.pages == 2
    assert not (new / "extract").exists()  # 카드 그림이 바뀌었으니 추출부터 다시
    data = json.loads((new / "file.json").read_text(encoding="utf-8"))
    assert data["exclude_pages"] == [2] and data["exclude_reason"] == "뒷면"
    assert not service.recut_check(new).changed  # 지금 방식대로 다시 잘렸다
    trash = list((info.dir.parent.parent / ".trash").iterdir())
    assert len(trash) == 1 and (trash[0] / "extract" / "pages" / "page-001.json").exists()
    assert sorted(p.name for p in (new / "source" / "images").iterdir()) == ["s.png"]


def test_recut_file_restores_old_file_when_adding_fails(tmp_path, monkeypatch):
    _info, fd = _project(tmp_path)
    _shift_saved_boxes(fd, 25)
    before = sorted(str(p.relative_to(fd)) for p in fd.rglob("*"))

    def boom(*a, **k):
        raise OSError("디스크 꽉 참")

    monkeypatch.setattr(_manage, "add_images", boom)
    with pytest.raises(OSError):
        service.recut_file(fd)
    assert sorted(str(p.relative_to(fd)) for p in fd.rglob("*")) == before


def test_recut_file_restores_after_partial_new_folder_and_keeps_project_state(tmp_path, monkeypatch):
    info, fd = _project(tmp_path)
    _shift_saved_boxes(fd, 25)
    pj = info.dir / "project.json"
    data = json.loads(pj.read_text(encoding="utf-8"))
    data["status"], data["glossary_version"] = "translated", 3
    pj.write_text(json.dumps(data), encoding="utf-8")
    before = sorted(str(p.relative_to(fd)) for p in fd.rglob("*"))
    real = _manage.add_images

    def half(project_dir, files, group, **kw):  # 새 폴더를 만든 뒤(사본 복사 중) 실패
        real(project_dir, files, group, **kw)
        raise OSError("복사 중 실패")

    monkeypatch.setattr(_manage, "add_images", half)
    with pytest.raises(OSError):
        service.recut_file(fd)
    assert sorted(str(p.relative_to(fd)) for p in fd.rglob("*")) == before
    after = json.loads(pj.read_text(encoding="utf-8"))
    assert after["status"] == "translated" and after["glossary_version"] == 3  # 진행 상태도 그대로


def test_recut_file_locked_folder_changes_nothing(tmp_path, monkeypatch):
    _info, fd = _project(tmp_path)
    _shift_saved_boxes(fd, 25)
    before = sorted(str(p.relative_to(fd)) for p in fd.rglob("*"))
    real_rename = os.rename

    def locked(a, b):
        if Path(a) == fd:
            raise PermissionError("다른 프로그램이 사용 중")
        return real_rename(a, b)

    monkeypatch.setattr(_manage.os, "rename", locked)
    with pytest.raises(OSError, match="열려 있는"):
        service.recut_file(fd)
    assert sorted(str(p.relative_to(fd)) for p in fd.rglob("*")) == before


def test_recut_file_refuses_unchanged_reason(tmp_path):
    from tests.test_service import make_project

    src = make_project(tmp_path / "src")
    info, _ = workspace.create_project("q", "G", root=tmp_path)
    workspace.add_files(info, [src / "source" / "cards.pdf"], "P", new_group=True)
    with pytest.raises(ValueError):
        service.recut_file(workspace.file_dirs(info)[0])


def test_overview_marks_image_files(tmp_path):
    info, fd = _project(tmp_path)
    files = [f for g in service.project_overview(info.dir)["groups"] for f in g["files"]]
    assert [f["from_images"] for f in files] == [True]


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_file_menu_offers_recut_only_for_image_files(qapp):
    from app.qt.main import ProjectPage

    page = ProjectPage.__new__(ProjectPage)  # 메뉴 목록만 본다 (화면은 만들지 않음)
    for name in ("edit_regions", "exclude_pages", "open_folder", "reset_selected", "delete_selected", "recut_selected"):
        setattr(page, name, lambda: None)
    labels = [e[0] for e in ProjectPage._menu_items(page, "file", {"from_images": True}) if e]
    assert "다시 자르기…" in labels and labels.index("다시 자르기…") < labels.index("삭제…")
    assert "다시 자르기…" not in [e[0] for e in ProjectPage._menu_items(page, "file", {"from_images": False}) if e]
    assert "다시 자르기…" not in [e[0] for e in ProjectPage._menu_items(page, "file") if e]
