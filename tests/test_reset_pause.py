"""초기화한 파일은 뒤에서 지우기가 바로 다시 지우지 않는다 (⑦ 글자 지우기를 누르면 풀림)."""

from __future__ import annotations

from pathlib import Path

from app import service, workspace
from tests.test_service import make_project


def _project(tmp_path):
    src = make_project(tmp_path / "src", pages=2)
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    workspace.add_files(info, [src / "source" / "cards.pdf"], "카드", new_group=True,
                        regions_from=src / "regions.json")
    return info, workspace.file_dirs(info)[0]


def test_reset_pauses_background_erase_until_erase_step(tmp_path):
    info, fd = _project(tmp_path)
    assert service.files_needing_erase(info.dir) == [fd]  # 아직 안 지움: 뒤에서 지울 차례
    assert service.erase_project(info.dir).state == "done"
    assert service.files_needing_erase(info.dir) == []

    for what in ("output", "all"):
        service.reset_files(info.dir, None, what)
        assert not (fd / "cleared").exists()
        assert service.erase_paused(fd)
        assert service.files_needing_erase(info.dir) == []  # 초기화했으니 뒤에서 다시 지우지 않는다
        assert service.files_needing_erase(info.dir, include_paused=True) == [fd]  # ⑦을 누르면 지울 차례

    service.set_erase_paused(fd, False)
    service.reset_files(info.dir, None, "output")
    r = service.erase_project(info.dir)  # ⑦ 글자 지우기를 누름: 풀리고 지운다
    assert r.state == "done" and (fd / "cleared" / "cleared.pdf").exists()
    assert not service.erase_paused(fd)
    assert Path(fd / "file.json").exists()


def test_erase_button_uses_paused_files():
    """⑦ 글자 지우기 버튼은 초기화로 멈춘 파일도 ‘지울 차례’로 본다 (1.24.1: 이미 지웠다고 보고 진행하지 않던 문제)."""
    import inspect

    from app.qt.results import OutputActions

    assert "include_paused=True" in inspect.getsource(OutputActions.erase)
