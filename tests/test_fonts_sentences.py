"""문장 단위 글꼴, 글꼴 모음(.ttc), 추천 계열 보정, 시트 합치기(그룹 나눔·시험 실행), 동작 로그(예외·강제 종료)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from app import font_match, font_plan, service, workspace
from app.typeset import ROOT, TextStyle, typeset_region
from app.typeset_layout import check_tags, parse_markup, sentence_spans, sentences, tag_counts, wrap_sentence_fonts
from tests.test_fonts import GAEGU, SANS, SANS_B, SERIF, _settings, qapp  # noqa: F401

JUA = "fonts/jua/Jua-Regular.ttf"


# ---------------------------------------------------------------- 문장 나누기·글꼴 태그
def test_sentence_split_keeps_tags_icons_and_quotes():
    t = "창고에 목소리가 울린다.\n“좋아 좋아, 판매가\n호조로군.” 다음은 <b>피규어?</b> [[coin]] 문이 열렸다! 끝"
    assert sentences(t) == ["창고에 목소리가 울린다.", "“좋아 좋아, 판매가 호조로군.”", "다음은 피규어?",
                            "[[coin]] 문이 열렸다!", "끝"]
    assert sentences("빈 줄\n\n다음 문단") == ["빈 줄", "다음 문단"]
    assert sentences("") == [] and sentence_spans("   ") == []


def test_wrap_sentence_fonts_balanced_per_line_and_crossing_tags():
    t = "첫 문장이다. 둘째는 <b>굵게\n이어진다.</b> 셋째."
    w, keys = wrap_sentence_fonts(t, {1: GAEGU, "2": GAEGU})
    assert keys == {"f1": GAEGU}  # 같은 글꼴은 태그 하나로
    assert w.count("<f1>") == 3 and w.count("</f1>") == 3
    for line in w.split("\n"):  # 줄마다 따로 배치해도 글꼴 태그 짝이 맞는다 (<b>가 줄을 넘는 것은 원래 글의 일)
        assert line.count("<f1>") == line.count("</f1>")
    assert parse_markup("<f1>한 줄</f1> 끝")[1] == []
    spans, err = parse_markup(w)
    assert not err and {s.fmt.font_tag for s in spans if "둘째" in s.text} == {"f1"}
    assert any(s.fmt.bold and s.fmt.font_tag == "f1" for s in spans)  # <b>와 엇갈려도 된다
    assert tag_counts(w) == tag_counts(t) and check_tags(w, t) == []  # 원문과 비교할 때 f 태그는 세지 않는다
    assert parse_markup("<f1>a<f2>b</f2></f1>")[1]  # 글꼴 태그 중첩은 오류
    assert wrap_sentence_fonts(t, {}) == (t, {})


def test_typeset_draws_each_sentence_in_its_font():
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=120)
    text, keys = wrap_sentence_fonts("고딕 문장이다. 개구 문장이다.", {1: GAEGU})
    st = TextStyle.from_region_style({"font": {"file": SANS}, "tag_fonts": keys}, ROOT, size_pt=12)
    res = typeset_region(page, (10, 10, 290, 110), text, st)
    assert res.drawn and not res.errors
    names = {f[3] for f in page.get_fonts()}
    assert any("Gaegu" in n for n in names) and any("NotoSans" in n.replace(" ", "") for n in names)
    st2 = TextStyle.from_region_style({"font": {"file": SANS}}, ROOT, size_pt=12)
    bad = typeset_region(doc.new_page(), (10, 10, 290, 110), "<f9>정해지지 않음</f9>", st2)
    assert any("글꼴이 정해지지 않은 태그" in e for e in bad.errors)


# ---------------------------------------------------------------- 실제 흐름: 문장마다 글꼴 영역
def _project_with_mode(tmp_path, mode):
    from tests.test_fonts import _image_project

    info, fd = _image_project(tmp_path)
    g = next(g for g in service.project_overview(info.dir)["groups"] if g["name"] == "이미지")
    t = service.load_region_template(g["dir"])
    if mode:
        t["regions"][0].setdefault("style", {})["font_mode"] = mode
    service.save_region_template(g["dir"], t)
    return info, fd


def test_sentence_mode_plan_save_and_compose(tmp_path, monkeypatch):
    info, fd = _project_with_mode(tmp_path, "sentence")
    texts = {1: {"이름": "첫 문장. 둘째 문장."}, 2: {"이름": "하나뿐"}}
    monkeypatch.setattr("app.font_plan.card_texts", lambda _fd: texts)
    plan = service.font_plans(info.dir)[0]
    reg = plan["regions"][0]
    assert reg["font_mode"] == "sentence"
    assert [x["text"] for x in reg["cards"][0]["sentences"]] == ["첫 문장.", "둘째 문장."]
    service.save_font_choices(fd, {"이름": SANS}, {}, {1: {"이름": {1: GAEGU}}})
    assert font_plan.load(fd)["sentences"] == {"1": {"이름": {"1": GAEGU}}}
    assert font_plan.sentences_for_page(font_plan.load(fd), 1) == {"이름": {1: GAEGU}}
    again = service.font_plans(info.dir)[0]["regions"][0]["cards"][0]["sentences"]
    assert [x["selected"] for x in again] == [None, GAEGU]  # 다시 열면 고른 것이 보인다
    cards = {p: {"이름": {"ko": t["이름"]}} for p, t in texts.items()}
    assert service._compose_with_cards(fd, cards, None, None).state == "done"
    rep = json.loads((fd / "translated" / "report.json").read_text(encoding="utf-8"))
    r1 = rep["pages_detail"][0]["regions"]["이름"]
    assert r1["sentence_fonts"] == {"1": GAEGU} and r1["font_file"] == SANS
    doc = pymupdf.open(fd / "translated" / "translated.pdf")
    assert any("Gaegu" in f[3] for f in doc.get_page_fonts(0))
    assert not any("Gaegu" in f[3] for f in doc.get_page_fonts(1))
    assert GAEGU in font_plan.project_used_fonts(fd)  # 문장에서 쓴 글꼴도 ‘쓴 글꼴’ 목록에


def test_cell_mode_ignores_sentence_choices(tmp_path, monkeypatch):
    info, fd = _project_with_mode(tmp_path, None)  # 기본: 한 칸 모두 같은 글꼴
    monkeypatch.setattr("app.font_plan.card_texts", lambda _fd: {1: {"이름": "첫. 둘."}})
    reg = service.font_plans(info.dir)[0]["regions"][0]
    assert reg["font_mode"] == "cell" and reg["cards"][0]["sentences"] == []
    service.save_font_choices(fd, {"이름": SANS}, {}, {1: {"이름": {1: GAEGU}}})
    service._compose_with_cards(fd, {1: {"이름": {"ko": "첫. 둘."}}}, None, None)
    doc = pymupdf.open(fd / "translated" / "translated.pdf")
    assert not any("Gaegu" in f[3] for f in doc.get_page_fonts(0))


def test_missing_font_gets_status_shown_in_viewer(tmp_path):
    from tests.test_fonts import _image_project

    _info, fd = _image_project(tmp_path)
    service.save_font_choices(fd, {"이름": "C:/nowhere/Gone.ttf"}, {})
    service._compose_with_cards(fd, {1: {"이름": {"ko": "가나다"}}}, None, None)
    pages = service.result_pages(fd / "translated")
    assert "글꼴 없음" in pages[0]["status"]
    from app.qt.results import ResultViewer

    assert ResultViewer.problems(["글꼴 없음"])  # 문제 있는 쪽으로 걸러진다


# ---------------------------------------------------------------- 글꼴 창: 문장 줄
def _sent_plan():
    return [{"file": "G/파일", "file_dir": "C:/x", "note": "", "used": [],
             "regions": [{"name": "본문", "kind": "text", "font_mode": "sentence", "selected": SANS,
                          "recommended": [{"file": SANS, "label": "본고딕", "reason": "", "by": "claude",
                                           "original_style": "고딕, 보통"},
                                          {"file": GAEGU, "label": "개구", "reason": "", "by": "shape"}],
                          "cards": [{"page": 1, "text": "첫. 둘.", "selected": None,
                                     "sentences": [{"idx": 0, "text": "첫.", "selected": None},
                                                   {"idx": 1, "text": "둘.", "selected": GAEGU}]}]}]}]


def test_font_dialog_sentence_rows(qapp):
    from app.qt.fonts_dialog import ROLE_KIND, FontPlanDialog

    dlg = FontPlanDialog(_sent_plan())
    top = dlg.tree.topLevelItem(0)
    assert "(문장마다)" in top.text(0) and "고딕, 보통" in top.toolTip(0)
    assert top.isExpanded()  # ‘문장마다’ 영역은 펼쳐 보인다
    assert top.child(0).data(0, ROLE_KIND)[0] == "slot"  # 문장 자리 줄이 카드보다 위
    card = top.child(1)
    assert card.isExpanded() and card.childCount() == 2
    s0, s1 = (dlg.tree.itemWidget(card.child(i), 1) for i in range(2))
    assert s0.currentText() == "(위 설정대로)" and s1.currentData() == GAEGU
    dlg.tree.setCurrentItem(card.child(0))
    dlg._preview()
    assert dlg.sample_note.text().startswith("위와 같음")
    s0.setCurrentIndex(s0.findData(SANS))
    s1.setCurrentIndex(0)
    out = dlg.choices()[0]
    assert out["sentences"] == {1: {"본문": {0: SANS}}} and out["regions"] == {"본문": SANS}


# ---------------------------------------------------------------- 영역 편집기: 글꼴 단위
def test_region_editor_font_mode(qapp, tmp_path):
    from app.qt.region_editor import RegionEditor
    from tests.test_qt import _project

    info = _project(tmp_path)
    g = service.project_overview(info.dir)["groups"][0]
    gdir = Path(g["dir"])
    (gdir / "regions.json").write_text(json.dumps({"source_language": "ja", "reference_page": 1, "protect_rects": [],
        "regions": [{"name": "본문", "kind": "text", "rect": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.3}},
                    {"name": "태그", "kind": "tag_pills", "rect": {"x": 0.1, "y": 0.5, "w": 0.3, "h": 0.1}}]},
        ensure_ascii=False), encoding="utf-8")
    ed = RegionEditor(gdir, [Path(f["dir"]) for f in g["files"]])
    body, tag = sorted(ed.items(), key=lambda i: i.name != "본문")
    body.setSelected(True)
    ed._to_form(body)
    assert ed.f_font_mode.currentData() == "cell" and ed.f_font_mode.isEnabled()  # 기본: 한 칸 모두 같은 글꼴
    ed.f_font_mode.setCurrentIndex(ed.f_font_mode.findData("sentence"))
    ed._from_form()
    assert body.data["style"]["font_mode"] == "sentence"
    ed.f_font_mode.setCurrentIndex(ed.f_font_mode.findData("cell"))
    ed._from_form()
    assert "font_mode" not in (body.data.get("style") or {})  # 기본은 적지 않는다
    body.setSelected(False)
    tag.setSelected(True)
    ed._to_form(tag)
    assert not ed.f_font_mode.isEnabled()  # 태그 라벨은 칸 단위만


# ---------------------------------------------------------------- 글꼴 모음(.ttc)
def _make_ttc(tmp_path) -> Path:
    from fontTools.ttLib import TTCollection, TTFont

    c = TTCollection()
    c.fonts = [TTFont(str(ROOT / SANS)), TTFont(str(ROOT / GAEGU))]
    out = tmp_path / "pair.ttc"
    c.save(str(out))
    return out


def test_ttc_faces_extract_and_check(tmp_path, monkeypatch):
    ttc = _make_ttc(tmp_path)
    fs = font_match.faces(ttc)
    assert [f["file"] for f in fs] == [f"{ttc}#0", f"{ttc}#1"] and "Gaegu" in fs[1]["family"]
    assert font_match.split_face(f"{ttc}#1") == (ttc, 1) and font_match.split_face("a.ttf") == (Path("a.ttf"), None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    out = Path(service.prepare_font(f"{ttc}#1"))
    assert out.parent == tmp_path / "local" / "CardTranslater" / "fonts" and out.suffix == ".ttf"
    assert pymupdf.Font(fontfile=str(out)).has_glyph(ord("한"))
    assert service.prepare_font(str(ROOT / SANS)) == str(ROOT / SANS)  # 모음이 아니면 그대로
    info = service.font_file_info(ttc)  # 파일로 고르면 한글이 있는 첫 글꼴
    assert info["ok"] and info["file"] == f"{ttc}#0"


def test_installed_fonts_include_ttc_faces(tmp_path, monkeypatch):
    d = tmp_path / "Fonts"
    d.mkdir()
    ttc = _make_ttc(tmp_path)
    (d / "pair.ttc").write_bytes(ttc.read_bytes())
    monkeypatch.setattr(font_match, "_font_dirs", lambda: [d])
    files = [f["file"] for f in font_match.installed_fonts()]
    assert files == sorted(files, key=lambda f: f) or len(files) == 2
    assert {Path(f.split("#")[0]).name for f in files} == {"pair.ttc"} and len(files) == 2


# ---------------------------------------------------------------- 추천: 계열 보정
def test_order_by_category_moves_mismatched_picks():
    styles = {c["file"]: c["style"] for c in service.font_choices()}
    picks = [{"file": "fonts/notosanskr/NotoSansKR-Medium.ttf", "original_style": "고딕(산세리프), 중간"},
             {"file": "fonts/notoserifkr/NotoSerifKR-Bold.ttf"}, {"file": SANS_B}]
    assert [p["file"] for p in font_plan.order_by_category(picks, styles)][-1].startswith("fonts/notoserifkr")
    serif_first = [{"file": SANS, "original_style": "명조(세리프), 보통"}, {"file": SERIF}]
    out = font_plan.order_by_category(serif_first, styles)
    assert out[0]["file"] == SERIF and out[0]["original_style"].startswith("명조")  # 원문 판단은 1순위에 남긴다
    assert font_plan.order_by_category([{"file": GAEGU, "original_style": "손글씨"}], styles)[0]["file"] == GAEGU


def test_shape_fallback_puts_handwriting_behind():
    from tests.test_fonts import _text_mask

    src = font_match.shape_features(_text_mask(SANS_B, size=80))
    cands = [{"file": f, "label": f, "style": s} for f, s in ((GAEGU, "handwriting"), (SANS_B, "sans"))]
    assert font_match.rank_fonts([src], cands)[0]["file"] == SANS_B


# ---------------------------------------------------------------- 시트: 그룹을 나눈 카드·시험 실행
def _sheet_project(tmp_path):
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    im = Image.new("RGB", (900, 600), "white")  # 글자가 있는 카드 두 장 (한글 크기를 원문 글자에서 잰다)
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(str(ROOT / SANS_B), 40)
    for x in (30, 480):
        d.rectangle([x, 30, x + 390, 540], fill=(240, 235, 220))
        d.text((x + 30, 60), "CARD NAME", font=f, fill=(20, 20, 20))
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    im.save(tmp_path / "in" / "s.png", dpi=(300, 300))
    r = service.add_images(info.dir, [tmp_path / "in" / "s.png"], "A", cols=2, rows=1, new_group=True)
    for g in service.project_overview(info.dir)["groups"]:
        if g["name"] == "A":
            service.save_region_template(g["dir"], {"regions": [
                {"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.15}}]})
    return info, r.file_dir


def test_sheet_combines_cards_from_moved_group(tmp_path):
    info, fd = _sheet_project(tmp_path)
    other = service.move_pages_to_group(fd, [2], "B", new_group=True)
    gb = next(g for g in service.project_overview(info.dir)["groups"] if g["name"] == "B")
    service.save_region_template(gb["dir"], {"regions": [{"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.15}}]})
    owner, members = service.sheet_family(other)
    assert owner == fd and {m.resolve() for m in members} == {fd.resolve(), other.resolve()}
    assert service.erase(fd, "1").ok and service.erase(other, "2").ok
    service._compose_with_cards(fd, {1: {"이름": {"ko": "가나다"}}}, None, None)
    res = service._compose_with_cards(other, {2: {"이름": {"ko": "라마바"}}}, None, None)
    assert res.sheet_error is None and [p.name for p in res.sheet_images] == ["s.png"]
    a = Image.open(fd / "translated" / "images" / "s.png")  # 원래 그룹 결과에도 같은 시트를 다시 썼다
    b = Image.open(other / "translated" / "images" / "s.png")
    assert list(a.getdata()) == list(b.getdata())
    lay = service.image_sheet_layouts(fd)[0]
    orig = Image.open(tmp_path / "in" / "s.png").convert("RGB")
    for box in lay["boxes"]:  # 두 칸 모두 한글을 넣은 카드 (원본과 다름)
        assert list(b.convert("RGB").crop(box).getdata()) != list(orig.crop(box).getdata())


def test_test_run_writes_sheet_with_tested_pages_only(tmp_path):
    info, fd = _sheet_project(tmp_path)
    r = service.test_run(fd, "1", out_root=tmp_path / "runs")
    assert r.state == "done"
    imgs = list((r.run_dir / "images").glob("*.png"))
    assert [p.name for p in imgs] == ["s.png"]
    lay = service.image_sheet_layouts(fd)[0]
    got = Image.open(imgs[0]).convert("RGB")
    orig = Image.open(tmp_path / "in" / "s.png").convert("RGB")
    box2 = lay["boxes"][1]
    assert list(got.crop(box2).getdata()) == list(orig.crop(box2).getdata())  # 시험하지 않은 2쪽은 원본
    assert not (fd / "translated").exists()  # 프로젝트 결과는 그대로


def test_viewer_sheet_button(qapp, tmp_path, monkeypatch):
    from app.qt import results

    d = tmp_path / "translated"
    d.mkdir()
    (d / "report.json").write_text(json.dumps({"pages": [], "pages_detail": []}), encoding="utf-8")
    v = results.ResultViewer([("f", d)])
    assert not v.sheet_btn.isVisibleTo(v)
    (d / "images").mkdir()
    Image.new("RGB", (10, 10)).save(d / "images" / "s.png")
    v.reload()
    assert v.sheet_btn.isVisibleTo(v)
    opened = []
    monkeypatch.setattr(results, "_open", opened.append)
    v.open_sheets()
    assert opened == [d / "images" / "s.png"]
    Image.new("RGB", (10, 10)).save(d / "images" / "t.png")
    v.open_sheets()
    assert opened[-1] == d / "images"  # 여럿이면 폴더


# ---------------------------------------------------------------- 동작 로그: 예외·강제 종료 (10번)
def test_excepthooks_write_to_log(tmp_path):
    code = r'''
import sys, threading
from pathlib import Path
from app import applog
p = applog.setup(Path(sys.argv[1]))
t = threading.Thread(target=lambda: 1 / 0, name="worker-x")
t.start(); t.join()
raise ValueError("마지막 sk-ant-api03-SECRETSECRET")
'''
    r = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(ROOT))
    assert r.returncode != 0
    log = (tmp_path / "cardtranslater.log").read_text(encoding="utf-8")
    assert "작업 스레드에서 처리되지 않은 예외 (worker-x)" in log and "ZeroDivisionError" in log
    assert "처리되지 않은 예외" in log and "ValueError" in log and "SECRETSECRET" not in log


def test_hard_crash_leaves_stack_in_crash_log(tmp_path):
    code = r'''
import sys, faulthandler
from pathlib import Path
from app import applog
applog.setup(Path(sys.argv[1]))
faulthandler._sigsegv()
'''
    r = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(ROOT))
    assert r.returncode != 0
    crash = (tmp_path / "crash.log").read_text(encoding="utf-8", errors="replace")
    assert "=== 시작" in crash and "Fatal Python error" in crash and "File" in crash



# ---------------------------------------------------------------- 문장 자리별 글꼴 (모든 카드)
def test_slot_fonts_first_and_last_first_then_middle_if_present():
    from app.font_plan import slot_fonts

    sl = {"first": "A", "last": "Z", "mid": {"1": "B", "2": "C"}}
    assert slot_fonts(sl, 4) == {0: "A", 1: "B", 2: "C", 3: "Z"}
    assert slot_fonts(sl, 3) == {0: "A", 1: "B", 2: "Z"}  # 마지막 문장이 가운데 2번째보다 먼저
    assert slot_fonts(sl, 2) == {0: "A", 1: "Z"}
    assert slot_fonts(sl, 1) == {0: "A"}  # 한 문장이면 첫 문장 글꼴
    assert slot_fonts({"mid": {"1": "B"}}, 5) == {1: "B"}  # 정하지 않은 자리는 영역 글꼴
    assert slot_fonts(None, 3) == {}


def test_sentences_for_page_spreads_slots_and_card_picks_win():
    from app.font_plan import sentences_for_page

    data = {"sentence_slots": {"본문": {"first": "A", "last": "Z"}},
            "sentences": {"2": {"본문": {"1": "X"}}}}
    card1 = {"page": 1, "본문": {"ko": "하나. 둘. 셋."}}
    assert sentences_for_page(data, 1, card1) == {"본문": {0: "A", 2: "Z"}}
    card2 = {"page": 2, "본문": {"ko": "하나. 둘."}}
    assert sentences_for_page(data, 2, card2) == {"본문": {0: "A", 1: "X"}}  # 카드에서 고른 문장이 먼저
    assert sentences_for_page(data, 2) == {"본문": {1: "X"}}  # 번역을 모르면 카드에서 고른 것만


def test_font_dialog_slot_rows_saved_for_all_cards(qapp):
    from app.qt.fonts_dialog import ROLE_KIND, FontPlanDialog

    plan = _sent_plan()
    plan[0]["regions"][0].update(max_sentences=3, slot_page=1,
                                 slots={"last": GAEGU})
    dlg = FontPlanDialog(plan)
    top = dlg.tree.topLevelItem(0)
    kinds = [top.child(i).data(0, ROLE_KIND) for i in range(top.childCount())]
    assert [k[3] for k in kinds if k[0] == "slot"] == ["first", "mid:1", "last"]
    combos = {top.child(i).data(0, ROLE_KIND)[3]: dlg.tree.itemWidget(top.child(i), 1)
              for i in range(top.childCount()) if top.child(i).data(0, ROLE_KIND)[0] == "slot"}
    assert combos["last"].currentData() == GAEGU and combos["first"].currentText() == "(영역 글꼴대로)"
    combos["first"].setCurrentIndex(combos["first"].findData(GAEGU))
    combos["mid:1"].setCurrentIndex(combos["mid:1"].findData(SANS))
    out = dlg.choices()[0]
    assert out["slots"] == {"본문": {"first": GAEGU, "mid": {"1": SANS}, "last": GAEGU}}


def test_slot_fonts_saved_and_used_by_compose(tmp_path, monkeypatch):
    from app import font_plan

    info, fd = _project_with_mode(tmp_path, "sentence")
    font_plan.save(fd, {}, slots={"이름": {"first": GAEGU, "mid": {"3": SANS}}})
    data = font_plan.load(fd)
    assert data["sentence_slots"] == {"이름": {"first": GAEGU, "mid": {"3": SANS}}}
    card = {"page": 1, "이름": {"ko": "첫 문장. 둘째 문장."}}
    assert font_plan.sentences_for_page(data, 1, card) == {"이름": {0: GAEGU}}  # 4번째 문장은 없어 안 씀
    cards = {1: {"이름": {"ko": "첫 문장. 둘째 문장."}}, 2: {"이름": {"ko": "하나뿐"}}}
    assert service._compose_with_cards(fd, cards, None, None).state == "done"
    rep = json.loads((fd / "translated" / "report.json").read_text(encoding="utf-8"))
    assert rep["pages_detail"][0]["regions"]["이름"]["sentence_fonts"] == {"0": GAEGU}
    assert rep["pages_detail"][1]["regions"]["이름"]["sentence_fonts"] == {"0": GAEGU}  # 다른 카드에도
