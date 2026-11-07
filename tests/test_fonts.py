"""한글 넣기 전 글꼴 고르기: 모양 재기·추천·저장·한글 넣기 반영·글꼴 창 (실제 Claude는 부르지 않는다)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from app import font_match, font_plan, service, workspace
from app.typeset import ROOT

SANS = "fonts/notosanskr/NotoSansKR-Regular.ttf"
SANS_B = "fonts/notosanskr/NotoSansKR-Bold.ttf"
SERIF = "fonts/notoserifkr/NotoSerifKR-Regular.ttf"
GAEGU = "fonts/gaegu/Gaegu-Regular.ttf"


@pytest.fixture(autouse=True)
def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CARDTRANSLATER_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr("app.settings.REPO_ROOT", tmp_path / "repo")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _text_mask(font_file: str, text="한글 글꼴 견본 가나다라", size=60) -> np.ndarray:
    f = ImageFont.truetype(str(ROOT / font_file), size)
    l, t, r, b = f.getbbox(text)
    im = Image.new("L", (r - l + 20, b - t + 20), 255)
    ImageDraw.Draw(im).text((10 - l, 10 - t), text, font=f, fill=0)
    return np.asarray(im) < 128


# ---------------------------------------------------------------- 모양 재기·순위
def test_shape_features_tell_weight_apart():
    reg = font_match.shape_features(_text_mask(SANS))
    bold = font_match.shape_features(_text_mask(SANS_B))
    assert reg and bold and bold["stroke"] > reg["stroke"] * 1.2 and bold["density"] > reg["density"]
    assert font_match.shape_features(np.zeros((50, 50), bool)) is None


def test_rank_fonts_puts_same_font_first():
    src = font_match.shape_features(_text_mask(SANS_B, size=80))
    cands = [{"file": f, "label": f, "style": s} for f, s in
             ((SERIF, "serif"), (GAEGU, "handwriting"), (SANS_B, "sans"), (SANS, "sans"))]
    ranked = font_match.rank_fonts([src], cands)
    assert ranked[0]["file"] == SANS_B and ranked[0]["reason"]
    assert [r["score"] for r in ranked] == sorted(r["score"] for r in ranked)
    assert font_match.rank_fonts([None], cands)[0]["score"] is None  # 원문을 못 쟀으면 순서 그대로


def test_font_info_and_file_check(tmp_path):
    info = font_match.font_info(ROOT / SANS)
    assert info["hangul"] and info["embeddable"] and "Noto Sans KR" in info["family"]
    (tmp_path / "x.ttf").write_bytes(b"not a font")
    assert service.font_file_info(tmp_path / "x.ttf")["ok"] is False
    assert service.font_file_info(ROOT / SANS)["ok"] is True
    assert service.font_path(SANS) == ROOT / SANS


def test_sample_sheet(tmp_path):
    out = font_match.render_sample_sheet([{"file": SANS}, {"file": SERIF}], "길드의 아이돌", tmp_path / "s.png")
    im = Image.open(out)
    assert im.height > 100 and (np.asarray(im.convert("L")) < 128).sum() > 500


# ---------------------------------------------------------------- 저장·쪽별 적용
def test_save_load_overrides(tmp_path):
    font_plan.save(tmp_path, {"이름": GAEGU, "본문": SANS}, {2: {"이름": SERIF, "본문": SANS}, 3: {}})
    data = font_plan.load(tmp_path)
    assert data["regions"] == {"이름": GAEGU, "본문": SANS}
    assert data["cards"] == {"2": {"이름": SERIF}}  # 영역 기본과 같은 것은 빼고 저장
    assert font_plan.overrides_for_page(data, 2) == {"이름": SERIF, "본문": SANS}
    assert font_plan.overrides_for_page(data, 5) == {"이름": GAEGU, "본문": SANS}
    assert font_plan.overrides_for_page({}, 1) == {}


# ---------------------------------------------------------------- 실제 흐름 (이미지 카드 프로젝트)
def _image_project(tmp_path):
    from tests.test_images import _grid

    info, _ = workspace.create_project("p", "G", root=tmp_path)
    im = Image.new("RGB", (900, 600), "white")
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(str(ROOT / SANS_B), 40)
    for k, x in enumerate((30, 480)):
        d.rectangle([x, 30, x + 390, 540], fill=(240, 235, 220))
        d.text((x + 30, 60), "CARD NAME", font=f, fill=(20, 20, 20))
    im.save(tmp_path / "in.png", dpi=(300, 300))
    r = service.add_images(info.dir, [tmp_path / "in.png"], "이미지", cols=2, rows=1, new_group=True)
    g = next(g for g in service.project_overview(info.dir)["groups"] if g["name"] == "이미지")
    service.save_region_template(g["dir"], {"regions": [
        {"name": "이름", "rect": {"x": 0.03, "y": 0.04, "w": 0.9, "h": 0.2}}]})
    assert service.erase(r.file_dir, "1-2").ok
    return info, r.file_dir


def test_plan_without_claude_and_compose_uses_choice(tmp_path, monkeypatch):
    info, fd = _image_project(tmp_path)
    monkeypatch.setattr("app.font_plan.card_texts", lambda _fd: {1: {"이름": "카드 이름"}, 2: {"이름": "두번째"}})
    plans = service.font_plans(info.dir)  # 설정 엔진을 쓸 수 없으면 모양 값으로만
    assert len(plans) == 1
    p = plans[0]
    reg = p["regions"][0]
    assert reg["name"] == "이름" and reg["recommended"] and reg["selected"] == reg["recommended"][0]["file"]
    assert [c["page"] for c in reg["cards"]] == [1, 2] and reg["cards"][0]["text"] == "카드 이름"
    assert font_plan.load(fd)["recommended"]  # 추천은 저장해 두고
    calls = []
    monkeypatch.setattr("app.font_plan.recommend", lambda *a, **k: calls.append(1) or ({}, ""))
    service.font_plans(info.dir)
    assert not calls  # 다시 열 때는 다시 추천하지 않는다

    service.save_font_choices(fd, {"이름": GAEGU}, {2: {"이름": SERIF}})
    res = service._compose_with_cards(fd, {1: {"이름": {"ko": "카드 이름"}}, 2: {"이름": {"ko": "두번째"}}}, None, None)
    assert res.state == "done"
    rep = json.loads((fd / "translated" / "report.json").read_text(encoding="utf-8"))
    used = {pd["page"]: pd["regions"]["이름"].get("font_file") for pd in rep["pages_detail"]}
    assert used == {1: GAEGU, 2: SERIF}  # 영역 기본 + 카드별
    doc = pymupdf.open(fd / "translated" / "translated.pdf")
    names = [{f[3] for f in doc.get_page_fonts(i)} for i in range(2)]
    assert any("Gaegu" in n for n in names[0]) and any("Serif" in n for n in names[1])
    assert any(u["file"] == GAEGU for u in service.font_plans(info.dir)[0]["used"])  # 쓴 글꼴로 목록에


class FakeCC:
    """Claude Code 대신: 받은 지시문을 남기고 후보 2번·1번을 고른다."""

    engine = "claude-code"

    def __init__(self):
        self.prompts = []

    def ask_json(self, prompt, schema, model, system, work_dir, images=None):
        from app.extract_cc import CCReply

        self.prompts.append((prompt, [Path(i).name for i in images or []]))
        regions = [json.loads(ln)["region"] for ln in prompt.splitlines()[1:]]
        return CCReply({"regions": [{"region": r, "ranked": [{"id": 2, "reason": "획이 비슷함"},
                                                              {"id": 1, "reason": "무난함"}]} for r in regions]},
                       model, 0.0, {})


def test_recommend_with_claude_pick(tmp_path, monkeypatch):
    _info, fd = _image_project(tmp_path)
    monkeypatch.setattr("app.font_plan.card_texts", lambda _fd: {1: {"이름": "카드 이름"}})
    cc = FakeCC()
    rec, note = font_plan.recommend(fd, cc)
    assert not note and len(cc.prompts) == 1  # 파일 하나에 한 번
    assert set(cc.prompts[0][1]) == {"r1_original.png", "r1_samples.png"}
    picks = rec["이름"]
    assert [p["by"] for p in picks[:2]] == ["claude", "claude"] and picks[0]["reason"] == "획이 비슷함"
    assert len(picks) <= font_plan.MAX_RECOMMENDED and len({p["file"] for p in picks}) == len(picks)


def test_recommend_falls_back_when_claude_fails(tmp_path, monkeypatch):
    _info, fd = _image_project(tmp_path)
    monkeypatch.setattr("app.font_plan.card_texts", lambda _fd: {})

    class Broken(FakeCC):
        def ask_json(self, *a, **k):
            from app.extract_cc import NotAvailable

            raise NotAvailable("Not logged in")

    rec, note = font_plan.recommend(fd, Broken())
    assert "모양 값으로만" in note and rec["이름"] and all(p["by"] != "claude" for p in rec["이름"])


# ---------------------------------------------------------------- 글꼴 창
def _plan(fd="C:/x"):
    return [{"file": "G/파일", "file_dir": fd, "note": "", "used": [{"file": GAEGU, "label": "개구"}],
             "regions": [{"name": "이름", "kind": "text", "selected": SANS_B,
                          "recommended": [{"file": SANS_B, "label": "본고딕 Bold", "reason": "굵기 비슷", "by": "claude"},
                                          {"file": SERIF, "label": "본명조", "reason": "", "by": "shape"}],
                          "cards": [{"page": 1, "text": "카드 이름", "selected": None},
                                    {"page": 2, "text": "두번째", "selected": SERIF}]}]}]


def test_font_dialog_defaults_and_choices(qapp):
    from app.qt.fonts_dialog import ADD, FontPlanDialog

    dlg = FontPlanDialog(_plan())
    top = dlg.tree.topLevelItem(0)
    cb = dlg.tree.itemWidget(top, 1)
    assert cb.currentData() == SANS_B and cb.currentText().startswith("★")
    files = [cb.itemData(i) for i in range(cb.count())]
    assert files[:2] == [SANS_B, SERIF] and GAEGU in files and files[-1] == ADD
    assert top.isExpanded()  # 카드별 선택이 있으면 펼쳐 보인다
    c1, c2 = dlg.tree.itemWidget(top.child(0), 1), dlg.tree.itemWidget(top.child(1), 1)
    assert c1.currentText() == "(영역과 같음)" and c2.currentData() == SERIF
    cb.setCurrentIndex(cb.findData(GAEGU))
    c1.setCurrentIndex(c1.findData(SANS_B))
    c2.setCurrentIndex(0)  # 영역과 같음으로 되돌림
    assert dlg.choices() == [{"file_dir": "C:/x", "regions": {"이름": GAEGU}, "cards": {1: {"이름": SANS_B}}, "sentences": {},
                              "sizes": {"이름": 1.0}}]


def test_font_dialog_add_font(qapp, monkeypatch):
    from app.qt import fonts_dialog

    dlg = fonts_dialog.FontPlanDialog(_plan())
    top = dlg.tree.topLevelItem(0)
    cb = dlg.tree.itemWidget(top, 1)
    new = str(ROOT / "fonts" / "jua" / "Jua-Regular.ttf")
    monkeypatch.setattr(dlg, "_add_font", lambda: {"file": new, "label": "주아"})
    cb.setCurrentIndex(cb.findData(fonts_dialog.ADD))
    assert cb.currentData() == new  # 추가한 글꼴이 이 줄에 선택되고
    c1 = dlg.tree.itemWidget(top.child(0), 1)
    assert c1.findData(new) >= 0 and c1.currentText() == "(영역과 같음)"  # 다른 줄 목록에도 생긴다
    monkeypatch.setattr(dlg, "_add_font", lambda: None)  # 취소하면 예전 선택으로
    cb.setCurrentIndex(cb.findData(fonts_dialog.ADD))
    assert cb.currentData() == new


def test_add_font_dialog_filter(qapp):
    from app.qt.fonts_dialog import AddFontDialog

    fonts = [{"file": str(ROOT / SANS), "label": "Noto Sans KR"}, {"file": str(ROOT / GAEGU), "label": "Gaegu"}]
    dlg = AddFontDialog(fonts)
    dlg.search.setText("gae")
    assert [dlg.list.item(i).isHidden() for i in range(2)] == [True, False]
    dlg.list.setCurrentRow(1)
    dlg.accept()
    assert dlg.chosen["label"] == "Gaegu"


def test_font_dialog_preview_is_above_list(qapp):
    """사용자 요구: 원본/미리 보기 그림 묶음이 목록(트리) 위에 있다."""
    from app.qt.fonts_dialog import FontPlanDialog

    dlg = FontPlanDialog(_plan())
    assert dlg.split.widget(1) is dlg.tree
    top = dlg.split.widget(0)
    assert top.isAncestorOf(dlg.orig) and top.isAncestorOf(dlg.sample) and not top.isAncestorOf(dlg.tree)
    assert dlg.tree.headerItem().text(0) == "영역 / 카드 / 문장"
    item = dlg.tree.topLevelItem(0)
    dlg.tree.setCurrentItem(item)
    assert "이름 영역" in dlg.where.text()
    cb = dlg.tree.itemWidget(item, 1)
    assert cb.currentText().startswith("★") and "굵기 비슷" not in cb.currentText()  # 이유는 툴팁으로
    assert "‘이름’ 영역의 모든 카드" in dlg.size_label.text()
    cb.setCurrentIndex(cb.findData(GAEGU))
    assert item.background(0).color().name().upper() == "#CFE2F3"
    dlg.b_all_rec.click()
    assert cb.currentData() == SANS_B and item.background(0).color().name().upper() != "#CFE2F3"


def test_font_picked_in_one_region_is_offered_in_others(qapp):
    """영역 1에서 고른 글꼴이 영역 2·카드 목록에도 보인다 (다른 줄의 선택은 그대로)."""
    from app.qt.fonts_dialog import FontPlanDialog

    other = "fonts/sunflower/Sunflower-Medium.ttf"
    plan = _plan()
    plan[0]["used"] = []
    plan[0]["regions"].append({"name": "본문", "kind": "text", "selected": SERIF,
                               "recommended": [{"file": SERIF, "label": "본명조", "reason": "", "by": "shape"}],
                               "cards": [{"page": 1, "text": "본문 글", "selected": None}]})
    plan[0]["regions"][0]["recommended"].append({"file": other, "label": "해바라기", "reason": "", "by": "shape"})
    dlg = FontPlanDialog(plan)
    r1, r2 = dlg.tree.topLevelItem(0), dlg.tree.topLevelItem(1)
    cb1, cb2 = dlg.tree.itemWidget(r1, 1), dlg.tree.itemWidget(r2, 1)
    assert SANS_B in [cb2.itemData(i) for i in range(cb2.count())]  # 처음 고른 것(영역 1의 선택)도 보인다
    assert cb2.findData(other) < 0
    cb1.setCurrentIndex(cb1.findData(other))
    assert cb2.findData(other) >= 0 and cb2.currentData() == SERIF  # 목록에 넣고 선택은 그대로
    tip = cb2.itemData(cb2.findData(other), 3)
    assert "이 창에서 고른" in tip
    dlg.tree.expandItem(r2)
    card = dlg.tree.itemWidget(r2.child(0), 1)
    assert card.findData(other) >= 0
