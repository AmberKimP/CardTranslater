"""글꼴 고르기·시트 이미지·로그 폴더 버튼의 빈틈 메우기 (실제 Claude는 부르지 않는다)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from PIL import Image

from app import font_match, font_plan, images, service, typeset_compose, workspace
from tests.test_fonts import GAEGU, SANS_B, SERIF, _image_project, _plan, _settings, qapp  # noqa: F401
from tests.test_images import _grid


# ---------------------------------------------------------------- 추천 캐시
def test_recommendation_cache_is_redone_when_cleared_run_changes(tmp_path, monkeypatch):
    """지운 결과(cleared/run.json)가 바뀌면 저장해 둔 추천을 버리고 다시 추천한다."""
    info, fd = _image_project(tmp_path)
    calls = []
    real = font_plan.recommend

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr("app.font_plan.recommend", spy)
    font_plan.build(fd)
    font_plan.build(fd)
    assert len(calls) == 1  # 같으면 캐시
    run = fd / "cleared" / "run.json"
    run.write_text(run.read_text(encoding="utf-8") + "\n ", encoding="utf-8")
    font_plan.build(fd)
    assert len(calls) == 2
    font_plan.build(fd, refresh=True)
    assert len(calls) == 3


# ---------------------------------------------------------------- 없는 글꼴 파일
def test_missing_font_file_in_fonts_json(tmp_path):
    """fonts.json의 글꼴 파일이 없어졌으면(지웠거나 다른 PC의 경로) 영역 글꼴로 넣고 그 칸에 경고를 남긴다."""
    info, fd = _image_project(tmp_path)
    service.save_font_choices(fd, {"이름": "C:/nowhere/Gone.ttf"}, {})
    res = service._compose_with_cards(fd, {1: {"이름": {"ko": "가나다"}}, 2: {"이름": {"ko": "라마"}}}, None, None)
    assert res.state == "done"
    rep = json.loads((fd / "translated" / "report.json").read_text(encoding="utf-8"))
    for pd in rep["pages_detail"]:
        r = pd["regions"]["이름"]
        assert "font_file" not in r and r["font_missing"] == "C:/nowhere/Gone.ttf"
        assert any("Gone.ttf" in w for w in r["warnings"])
    assert "C:/nowhere/Gone.ttf" not in font_plan.project_used_fonts(fd)


# ---------------------------------------------------------------- 카드별 override (tag_pills)
def test_per_card_font_override_for_tag_pills_region():
    """tag_pills 영역도 영역 기본·카드별 글꼴 선택이 그 쪽에서 적용된다 (report에 font_file)."""
    data = {"regions": {"태그": SANS_B}, "cards": {"2": {"태그": SERIF}}}
    assert font_plan.overrides_for_page(data, 1) == {"태그": SANS_B}
    assert font_plan.overrides_for_page(data, 2) == {"태그": SERIF}
    assert font_plan.overrides_for_page({}, 1) == {}
    doc, sdoc = pymupdf.open(), pymupdf.open()
    pg = doc.new_page(width=200, height=100)
    src = sdoc.new_page(width=200, height=100)
    meta = {"regions": [{"name": "태그", "kind": "tag_pills", "rect_pt": [10, 10, 190, 40],
                         "tag_labels": [{"bbox_pt": [10, 10, 90, 30], "color": [0.8, 0.2, 0.2], "radius_pt": 10}]}]}
    for pno, want in ((1, SANS_B), (2, SERIF)):
        rec = typeset_compose.compose_page(pg, src, meta, {"태그": {"ko": ["전사"]}},
                                           fonts=font_plan.overrides_for_page(data, pno))
        assert rec["태그"]["font_file"] == want


# ---------------------------------------------------------------- 시트 이미지
def test_attach_sheet_images_error_keeps_compose_done(tmp_path, monkeypatch):
    """시트 합치기가 실패해도 한글 넣기는 done이고, sheet_error에 이유가 담기며 translated.pdf는 남는다."""
    info, fd = _image_project(tmp_path)

    def boom(*a, **k):
        raise ValueError("원본 크기가 다름")

    monkeypatch.setattr(images, "compose_sheets", boom)
    res = service._compose_with_cards(fd, {}, None, None)
    assert res.state == "done" and not res.sheet_images
    assert "원본 크기가 다름" in res.sheet_error
    assert (fd / "translated" / "translated.pdf").exists()


def test_sheet_images_with_excluded_pages(tmp_path):
    """뺀 쪽(지우기·한글 넣기에서 빠진 쪽)은 시트 이미지에서 원본 그대로 남는다."""
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    boxes = _grid(tmp_path / "in" / "s.png")
    r = service.add_images(info.dir, [tmp_path / "in" / "s.png"], "이미지", cols=3, rows=2, new_group=True)
    g = next(g for g in service.project_overview(info.dir)["groups"] if g["name"] == "이미지")
    service.save_region_template(g["dir"], {"regions": [
        {"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.15}}]})
    assert service.erase(r.file_dir, "1-2").ok  # 3~6쪽은 뺀다
    res = service._compose_with_cards(r.file_dir, {}, None, None)
    assert res.state == "done" and res.sheet_error is None and len(res.sheet_images) == 1
    out = np.asarray(Image.open(res.sheet_images[0]).convert("RGB"))
    orig = np.asarray(Image.open(tmp_path / "in" / "s.png").convert("RGB"))
    for k in (2, 3, 4, 5):
        b = boxes[k]
        assert (out[b[1]:b[3], b[0]:b[2]] == orig[b[1]:b[3], b[0]:b[2]]).all()


# ---------------------------------------------------------------- font_info fsType
def _font_with_fstype(tmp_path, fs_type):
    from fontTools.ttLib import TTFont

    f = TTFont(str(font_plan.resolve(SANS_B)))
    f["OS/2"].fsType = fs_type
    out = tmp_path / "f.ttf"
    f.save(str(out))
    return out


@pytest.mark.parametrize("fs_type, ok", [(0, True), (0x0004, True), (0x0008, True), (0x0002, False),
                                         (0x0002 | 0x0004, True), (0x0100, True)])
def test_font_info_fstype_embeddable(tmp_path, fs_type, ok):
    """fsType: 내장 금지(0x2)만 쓸 수 없다. 미리보기·편집 허용(0x4·0x8)이 같이 있으면 쓸 수 있다."""
    info = font_match.font_info(_font_with_fstype(tmp_path, fs_type))
    assert info and info["hangul"] and info["embeddable"] is ok


def test_font_info_rejects_ttc_and_broken(tmp_path):
    """.ttc와 깨진 파일은 None."""
    (tmp_path / "a.ttc").write_bytes(b"x")
    (tmp_path / "b.ttf").write_bytes(b"not a font")
    assert font_match.font_info(tmp_path / "a.ttc") is None and font_match.font_info(tmp_path / "b.ttf") is None


# ---------------------------------------------------------------- 글꼴 창: 펼치지 않은 카드
def test_choices_keep_old_card_picks_when_not_expanded(qapp):
    """카드 목록을 펼치지 않았어도 이미 저장된 카드별 선택(2쪽)은 choices()에 그대로 담긴다."""
    from app.qt.fonts_dialog import FontPlanDialog

    plan = _plan()
    dlg = FontPlanDialog(plan)
    dlg.tree.topLevelItem(0).setExpanded(False)
    assert dlg.choices()[0]["cards"] == {2: {"이름": SERIF}}


def test_save_drops_card_pick_equal_to_region_default(tmp_path):
    """카드 선택이 영역 기본과 같으면 저장하지 않는다."""
    font_plan.save(tmp_path, {"이름": SANS_B}, {1: {"이름": SANS_B}, 2: {"이름": SERIF}})
    assert font_plan.load(tmp_path)["cards"] == {"2": {"이름": SERIF}}


# ---------------------------------------------------------------- 글꼴 창을 띄우는 한글 넣기 단추
class _FakePage:
    def __init__(self, d):
        self.project_dir = Path(d)

    def scope(self):
        return (None, None)

    def _selected(self):  # 한글을 넣을 수 있는 그룹을 고른 채 (한글 넣기가 그룹을 묻지 않게)
        return ("group", {"name": "g", "files": [{"erased": True, "translated": True, "composed": False}]})

    def refresh(self):
        pass


def _hook(monkeypatch, plans, dialog_result):
    from app.qt import fonts_dialog, results, worker

    log = {"jobs": [], "saved": [], "dialog": 0, "compose": []}

    def fake_run(parent, title, job, on_done, on_failed=None, cancellable=True):
        log["jobs"].append((title, job.fn))
        if job.fn is service.font_plans:
            on_done(plans)
        elif job.fn is service.compose_project:
            log["compose"].append(job.kwargs)
        return job

    class Dlg:
        def __init__(self, p, parent=None):
            log["dialog"] += 1

        def exec(self):
            return dialog_result

        def choices(self):
            return [{"file_dir": "D", "regions": {"이름": SANS_B}, "cards": {1: {"이름": SERIF}},
                     "sentences": {1: {"이름": {0: GAEGU}}}, "sizes": {"이름": 1.2},
                     "aligns": {"이름": "center"}}]

    monkeypatch.setattr(worker, "run_with_dialog", fake_run)
    monkeypatch.setattr(fonts_dialog, "FontPlanDialog", Dlg)
    monkeypatch.setattr(service, "save_font_choices", lambda *a: log["saved"].append(a))
    return results.OutputActions, log


def test_compose_button_cancel_does_not_compose(qapp, tmp_path, monkeypatch):
    """글꼴 창에서 취소하면 저장도 한글 넣기도 하지 않는다."""
    cls, log = _hook(monkeypatch, _plan(), 0)
    cls(_FakePage(tmp_path)).compose()
    assert log["dialog"] == 1 and not log["saved"] and not log["compose"]


def test_compose_button_ok_saves_then_composes(qapp, tmp_path, monkeypatch):
    """확인하면 고른 글꼴을 저장한 뒤 한글 넣기를 돌린다 (accept_issues=False)."""
    cls, log = _hook(monkeypatch, _plan(), 1)
    cls(_FakePage(tmp_path)).compose()
    assert log["saved"] == [("D", {"이름": SANS_B}, {1: {"이름": SERIF}}, {1: {"이름": {0: GAEGU}}}, {"이름": 1.2},
                             {"이름": "center"}, None)]
    assert len(log["compose"]) == 1 and log["compose"][0]["accept_issues"] is False


def test_compose_button_accept_skips_font_dialog(qapp, tmp_path, monkeypatch):
    """엑셀 확인 뒤 accept=True로 부르면 글꼴 창·추천 없이 바로 한글 넣기."""
    cls, log = _hook(monkeypatch, _plan(), 0)
    cls(_FakePage(tmp_path)).compose(accept=True)
    assert log["dialog"] == 0 and [j[0] for j in log["jobs"]] == ["한글 넣기"]
    assert log["compose"][0]["accept_issues"] is True


def test_compose_button_without_plans_skips_dialog(qapp, tmp_path, monkeypatch):
    """지운 결과가 있는 파일이 없어 계획이 비면 글꼴 창 없이 한글 넣기로 간다."""
    cls, log = _hook(monkeypatch, [], 0)
    cls(_FakePage(tmp_path)).compose()
    assert log["dialog"] == 0 and len(log["compose"]) == 1


# ---------------------------------------------------------------- 로그 폴더 단추
def test_open_log_folder_opens_service_folder(qapp, tmp_path, monkeypatch):
    """open_log_folder는 로그 폴더를 만들고 탐색기(QDesktopServices)로 연다."""
    from PySide6.QtGui import QDesktopServices

    from app.qt import dialogs

    folder = tmp_path / "logs" / "x"
    opened = []
    monkeypatch.setattr(service, "log_folder", lambda: folder)
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda u: opened.append(u.toLocalFile()) or True))
    assert dialogs.open_log_folder() == folder and folder.is_dir()
    assert Path(opened[0]) == folder


def test_open_log_folder_reports_error(qapp, tmp_path, monkeypatch):
    """폴더를 만들 수 없으면 열지 않고 오류를 알린다."""
    from PySide6.QtGui import QDesktopServices

    from app.qt import dialogs

    blocker = tmp_path / "file"
    blocker.write_text("x")
    errs, opened = [], []
    monkeypatch.setattr(service, "log_folder", lambda: blocker / "sub")
    monkeypatch.setattr(dialogs, "show_error", lambda *a, **k: errs.append(a))
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda u: opened.append(u) or True))
    dialogs.open_log_folder()
    assert errs and not opened


def test_settings_and_login_have_log_folder_buttons(qapp, monkeypatch):
    """설정 창과 로그인 창에 로그 폴더 단추가 있고, 누르면 open_log_folder가 불린다."""
    from app.qt import dialogs, login

    called = []
    monkeypatch.setattr(dialogs, "open_log_folder", lambda parent=None: called.append("settings"))
    monkeypatch.setattr(login, "open_log_folder", lambda parent=None: called.append("login"), raising=False)
    s = dialogs.SettingsDialog()
    assert s.b_log.text() == "폴더 열기" and s.log_path.text() == str(service.log_folder())
    s.b_log.click()
    lg = login.LoginDialog(first_run=False)
    assert lg.b_log.text() == "로그 폴더 열기"
    lg.b_log.click()
    assert called == ["settings", "login"]


# ---------------------------------------------------------------- 글자 크기 배율
def test_size_scale_multiplies_start_size_but_not_fixed():
    """글꼴 창의 글자 크기 배율은 원문 기준 시작 크기에 곱하고, 엑셀 고정 크기에는 쓰지 않는다."""
    from app.typeset import TextStyle

    base = TextStyle.from_region_style({}, size_pt=10.0)
    big = TextStyle.from_region_style({"size_scale": 1.5}, size_pt=10.0)
    assert big.start_size() == pytest.approx(base.start_size() * 1.5)
    ink = TextStyle.from_region_style({"size_scale": 0.8}, ink_height_pt=5.0)
    assert ink.start_size() == pytest.approx(TextStyle.from_region_style({}, ink_height_pt=5.0).start_size() * 0.8)
    fixed = TextStyle.from_region_style({"size_scale": 2.0}, size_pt=10.0, fixed_size_pt=7.0)
    assert fixed.start_size() == 7.0


def test_save_sizes_round_trip_and_keep(tmp_path):
    """sizes는 1.0이 아닌 것만 저장, None이면 예전 것을 그대로 둔다."""
    font_plan.save(tmp_path, {"이름": SANS_B}, {}, None, {"이름": 1.2, "본문": 1.0})
    assert font_plan.size_scales(font_plan.load(tmp_path)) == {"이름": 1.2}
    font_plan.save(tmp_path, {"이름": SANS_B}, {})
    assert font_plan.size_scales(font_plan.load(tmp_path)) == {"이름": 1.2}
    font_plan.save(tmp_path, {"이름": SANS_B}, {}, None, {"이름": 1.0})
    assert font_plan.size_scales(font_plan.load(tmp_path)) == {}


def test_compose_page_applies_size_scale():
    """compose_page(sizes=)로 그 영역의 시작 크기가 배율만큼 커진다."""
    doc, sdoc = pymupdf.open(), pymupdf.open()
    meta = {"regions": [{"name": "본문", "kind": "text", "rect_pt": [10, 10, 290, 90],
                         "text_layer": {"size_pt": 8.0}}]}
    starts = []
    for sizes in (None, {"본문": 1.5}):
        pg = doc.new_page(width=300, height=100)
        src = sdoc.new_page(width=300, height=100)
        rec = typeset_compose.compose_page(pg, src, meta, {"본문": {"ko": "짧은 글"}}, sizes=sizes)
        starts.append(rec["본문"]["start_size_pt"])
    assert starts[1] == pytest.approx(starts[0] * 1.5)


def test_save_aligns_round_trip_and_keep(tmp_path):
    """aligns는 left/center/right만 저장(영역 설정대로 ''는 뺌), None이면 예전 것을 그대로 둔다."""
    font_plan.save(tmp_path, {"이름": SANS_B}, {}, None, None, {"이름": "center", "본문": "", "x": "bad"})
    assert font_plan.align_choices(font_plan.load(tmp_path)) == {"이름": "center"}
    font_plan.save(tmp_path, {"이름": SANS_B}, {})
    assert font_plan.align_choices(font_plan.load(tmp_path)) == {"이름": "center"}
    font_plan.save(tmp_path, {"이름": SANS_B}, {}, None, None, {})
    assert font_plan.align_choices(font_plan.load(tmp_path)) == {}


def test_compose_page_applies_align_choice():
    """compose_page(aligns=)로 그 영역의 한글이 왼쪽 < 가운데 < 오른쪽 자리에 놓인다."""
    doc, sdoc = pymupdf.open(), pymupdf.open()
    meta = {"regions": [{"name": "본문", "kind": "text", "rect_pt": [10, 10, 290, 90],
                         "text_layer": {"size_pt": 8.0}}]}
    xs = []
    for al in ("left", "center", "right"):
        pg = doc.new_page(width=300, height=100)
        src = sdoc.new_page(width=300, height=100)
        typeset_compose.compose_page(pg, src, meta, {"본문": {"ko": "짧은 글"}}, aligns={"본문": al})
        words = pg.get_text("words")
        assert words
        xs.append((min(w[0] for w in words), max(w[2] for w in words)))
    assert xs[0][0] < xs[1][0] < xs[2][0]
    assert xs[0][0] == pytest.approx(10, abs=3)
    assert xs[2][1] == pytest.approx(290, abs=3)
    assert (xs[1][0] - 10) == pytest.approx(290 - xs[1][1], abs=3)
