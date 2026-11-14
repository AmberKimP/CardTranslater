"""글꼴: 고르기·견본·설치된 글꼴·내 글꼴."""

from __future__ import annotations

from pathlib import Path

from app.service import _base
from app.service import _extract
from app.service import _settings


def font_choices() -> list[dict]:
    """고를 수 있는 한글 글꼴 [{"label", "family", "file"}] (fonts/에 실제로 있는 파일만)."""
    from app import font_files

    cat = font_files.catalog()  # 프로그램의 기본 글꼴 + 따로 설치한 글꼴 묶음
    out = []
    for fam in cat.get("families", []):
        files = {p.name: p for d in font_files.family_dirs(fam["dir"]) for p in d.glob("*.[ot]tf")}
        for f in sorted((p for p in files.values() if "[" not in p.name), key=lambda p: p.name):  # 가변 글꼴 원본 제외
            weight = f.stem.split("-")[-1] if "-" in f.stem else ""
            family = fam["name"] + (f" {weight}" if weight and weight != "Regular" else "")
            out.append({"label": f"{family} — {fam.get('ko_name', '')}", "family": family,
                        "file": f"fonts/{fam['dir']}/{f.name}", "style": fam.get("style", "")})
    return out


def font_pack_status() -> dict:
    """따로 설치하는 글꼴 묶음 상태 {"wanted", "installed", "missing", "ok"} (app.font_files.pack_status)."""
    from app import font_files

    return font_files.pack_status()


# ---------------------------------------------------------------- 한글 넣기 전 글꼴 고르기
FONT_PICK_MODEL = "claude-sonnet-5-5"  # 원문과 견본을 보고 비슷한 글꼴 고르기


def font_plans(project_dir: Path | str, group: str | None = None, files: list | None = None,
               refresh: bool = False, progress: _base.ProgressFn = _base._noop,
               cancel: _base.CancelToken | None = None) -> list[dict]:
    """한글 넣기 직전 글꼴 창에 보여 줄 계획 (지운 결과가 있는 파일마다 app.font_plan.build).
    추천은 처음 한 번만 만들고 fonts.json에 남긴다 (refresh=True면 다시). Claude를 쓸 수 없으면 글자 모양 값으로만."""
    from app import font_plan, workspace
    from app.extract import make_client
    from app.extract_cc import ClaudeCodeClient

    info = workspace.load_project_info(project_dir)
    dirs = [fd for fd in _extract.scoped_file_dirs(info, group, files) if (fd / "cleared" / "cleared.pdf").exists()]
    client = None
    need = refresh or any(font_plan.needs_recommend(fd) for fd in dirs)  # 다시 추천할 때도 Claude에게 (모양 값만으로 하지 않게)
    if need:
        eng = _settings.get_settings().engine
        try:
            if _settings.engine_status()[eng]:
                client = ClaudeCodeClient() if eng == "claude-code" else make_client()
        except Exception:  # noqa: BLE001 - 추천만 못 받는다
            client = None
    out = []
    for i, fd in enumerate(dirs):
        if cancel:
            cancel.check()
        key = workspace.file_key(fd)

        def say(done, total, msg, _i=i, _key=key):
            progress(_base.ProgressEvent("fonts", _i, len(dirs), f"{_key}: {msg}"))

        plan = font_plan.build(fd, client, FONT_PICK_MODEL, refresh=refresh, cancel=cancel, say=say)
        plan["file"] = key
        out.append(plan)
    progress(_base.ProgressEvent("done", len(dirs), len(dirs), "글꼴 추천 완료"))
    return out


def save_font_choices(file_dir: Path | str, regions: dict, cards: dict | None = None,
                      sentences: dict | None = None, sizes: dict | None = None,
                      aligns: dict | None = None, slots: dict | None = None) -> Path:
    """sizes {영역: 글자 크기 배율} (1.0 = 원문 크기). aligns {영역: left | center | right} (영역 설정대로면 뺀다).
    글꼴 창에서 고른 것: regions {영역: 글꼴 파일}, cards {쪽: {영역: 글꼴 파일}} (영역 기본과 다를 때만),
    sentences {쪽: {영역: {문장 번호: 글꼴 파일}}} (‘문장마다 글꼴’ 영역)."""
    from app import font_plan

    return font_plan.save(Path(file_dir), regions, cards, sentences, sizes, aligns, slots)


def font_preview(file_dir: Path | str, page: int, region: str, font_file: str | None,
                 scale: float = 1.0, align: str | None = None, sentence: int | None = None,
                 sentence_slots: dict | None = None, base_font: str | None = None) -> dict | None:
    """글꼴 창 미리 보기: 원본·지운 카드의 그 영역 그림(PNG bytes, 같은 배율)과 한글 넣기에 쓸 글자 색·크기.
    {"original_png", "cleared_png", "composed_png" (한글 넣기와 똑같이 그린 그림, 못 그리면 None),
     "zoom" (px/pt), "w_pt", "h_pt", "color", "align", "size_pt" (고른 글꼴·배율), "kind"}. 만들 수 없으면 None.
    align: 글꼴 창에서 고른 정렬 (없으면 영역 설정), sentence: ‘문장마다 글꼴’ 영역에서 그 글꼴을 쓸 문장 번호."""
    from app import font_plan

    src = font_plan.preview_source(Path(file_dir), page, region)
    if src is None:
        return None
    composed = font_plan.preview_render(Path(file_dir), page, region, font_file, scale, align, sentence,
                                        slots=sentence_slots, base_font=base_font)
    return {**src, "size_pt": font_plan.preview_size(src, font_file, scale), "composed_png": composed}


def font_label(file: str) -> str:
    """글꼴 파일의 보이는 이름."""
    from app import font_plan

    return font_plan.label_for(file)


def font_path(file: str) -> Path:
    """글꼴 파일의 실제 경로 (fonts/... 상대 경로는 앱 폴더 기준)."""
    from app import font_plan

    return font_plan.resolve(file)


_INSTALLED_FONTS: list[dict] | None = None


def installed_fonts(refresh: bool = False) -> list[dict]:
    """PC에 설치된 글꼴 중 한글이 있고 PDF에 넣어도 되는 것 [{"file", "label", "family"}] (처음 한 번 몇 초)."""
    global _INSTALLED_FONTS
    from app import font_match

    if _INSTALLED_FONTS is None or refresh:
        _INSTALLED_FONTS = font_match.installed_fonts()
    return _INSTALLED_FONTS


def font_cache_dir() -> Path:
    """글꼴 모음(.ttc)에서 꺼낸 글꼴을 두는 곳."""
    import os

    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "CardTranslater" / "fonts"


def prepare_font(file: str) -> str:
    """고른 글꼴을 한글 넣기에 쓸 수 있는 파일로: 글꼴 모음의 한 글꼴('모음.ttc#2')이면 .ttf로 꺼낸 경로."""
    from app import font_match

    return str(font_match.extract_face(file, font_cache_dir()))


USER_FONT_DIR = "내 글꼴"  # 데이터 폴더 안: 사용자가 글꼴 파일을 넣어 두면 ‘글꼴 추가…’ 목록에 나온다
FONT_EXTS = (".ttf", ".otf", ".ttc", ".otc")


def user_font_dir(create: bool = True) -> Path:
    """사용자 글꼴 폴더 (데이터 폴더\\내 글꼴). create=True면 없을 때 만든다."""
    d = _settings.data_dir() / USER_FONT_DIR
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def user_fonts() -> dict:
    """사용자 글꼴 폴더의 글꼴 (하위 폴더 포함, 부를 때마다 새로 읽는다).
    {"dir", "fonts": [{"file", "label", "family", ...}], "skipped": [{"name", "reason"}]}.
    한글이 없거나 PDF에 넣을 수 없는 글꼴은 skipped에 이유와 함께."""
    from app import font_match

    d = user_font_dir(create=False)
    fonts, skipped, seen = [], [], set()
    files = sorted(p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in FONT_EXTS) if d.is_dir() else []
    for p in files:
        fs = font_match.faces(p)
        if not fs:
            skipped.append({"name": p.name, "reason": "읽을 수 없는 글꼴 파일"})
            continue
        for info in fs:
            if not info["hangul"]:
                skipped.append({"name": p.name, "reason": "한글 글자가 없음"})
            elif not info["embeddable"]:
                skipped.append({"name": p.name, "reason": "PDF에 넣는 것이 허락되지 않은 글꼴 (라이선스)"})
            elif info["file"] not in seen:
                seen.add(info["file"])
                fonts.append(info)
    fonts.sort(key=lambda x: x["label"].lower())
    return {"dir": str(d), "fonts": fonts, "skipped": skipped}


def font_file_info(path: Path | str) -> dict:
    """사용자가 고른 글꼴 파일 검사: {"ok", "file", "label", "reason"}.
    글꼴 모음(.ttc)이면 한글이 있고 PDF에 넣어도 되는 첫 글꼴을 고른다."""
    from app import font_match

    if Path(str(path)).suffix.lower() in (".ttc", ".otc") and "#" not in str(path):
        fs = font_match.faces(path)
        good = [f for f in fs if f["hangul"] and f["embeddable"]]
        info = (good or fs or [None])[0]
    else:
        info = font_match.font_info(path)
    if info is None:
        return {"ok": False, "file": str(path), "label": Path(path).name,
                "reason": "읽을 수 없는 글꼴입니다 (.ttf·.otf·.ttc 글꼴 파일을 고르세요)"}
    if not info["hangul"]:
        return {"ok": False, **info, "reason": "한글 글자가 없는 글꼴입니다"}
    if not info["embeddable"]:
        return {"ok": False, **info, "reason": "PDF에 넣는 것이 허락되지 않은 글꼴입니다 (글꼴 라이선스)"}
    return {"ok": True, **info, "reason": ""}
