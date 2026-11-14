"""자기 점검·라이선스, 삭제·초기화, API 키·Claude 로그인, 로그, 이미지 넣기."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from app import applog
from app.claude_setup import find_claude
from app.service import _base
from app.service import _extract
from app.service import _fonts
from app.service import _settings


def self_check() -> dict:
    """설치(exe) 점검: 글꼴·라이브러리·한글 그리기가 되는지. {"ok": bool, ...}."""
    import pymupdf

    from app.typeset import ROOT, TextStyle, finalize, typeset_region

    from app import font_files, settings

    out: dict = {"root": str(ROOT), "data_dir": str(_settings.data_dir()), "frozen": settings.is_frozen()}
    fonts = _fonts.font_choices()
    out["fonts"] = len(fonts)
    libs = {}
    for name in ("pymupdf", "cv2", "numpy", "openpyxl", "PIL", "anthropic", "fontTools"):
        try:
            m = __import__(name)
            libs[name] = getattr(m, "__version__", getattr(m, "VersionBind", "ok"))
        except Exception as e:  # noqa: BLE001
            libs[name] = f"없음: {e}"
    out["libs"] = libs
    try:
        doc = pymupdf.open()
        page = doc.new_page(width=200, height=100)
        ff = font_files.resolve(fonts[0]["file"] if fonts else "fonts/notosanskr/NotoSansKR-Regular.ttf")
        r = typeset_region(page, (10, 10, 190, 60), "한글 점검", TextStyle(font_file=str(ff), size_pt=10))
        finalize(doc)
        data = doc.tobytes()
        text = pymupdf.open(stream=data, filetype="pdf")[0].get_text()
        out["typeset"] = bool(r.drawn and "한글점검" in "".join(text.split()))
    except Exception as e:  # noqa: BLE001
        out["typeset"] = f"실패: {e}"
    out["engine"] = _settings.engine_status()
    out["release_notes"] = bool(release_notes())
    out["license"] = "BEER-WARE" in license_text() and "AGPL" in license_text()
    from app import parallel

    # ⑦·⑧ 동시 처리: 작업 프로세스가 뜨는지 (안 되면 한 프로세스로 하지만 느리다 — 빌드가 잘못된 것이므로 점검 실패로 본다).
    # 동시 처리를 끈 환경(테스트)이면 None: 점검하지 않는다
    out["parallel"] = parallel.self_check()
    out["font_pack"] = font_files.pack_status()  # 글꼴 묶음은 따로 설치하므로 점검 실패로 보지 않는다
    out["ok"] = bool(out["fonts"]) and out["typeset"] is True and out["release_notes"] and out["license"] and out["parallel"] is not False and not any(
        str(v).startswith("없음") for v in libs.values())
    return out


def license_text() -> str:
    """도움말 > 라이선스: 이 프로그램의 라이선스(Beerware)와 제3자 구성 요소 고지 (마크다운). 없으면 ''."""
    from app import release_notes as rn

    return rn.license_text()


def release_notes() -> str:
    """사용자용 릴리스 노트 글(버전별 새 기능·고친 점, 최신이 위). 없거나 읽지 못하면 ''."""
    from app import release_notes as rn

    return rn.load()


# ---------------------------------------------------------------- 화면용: 지우기(휴지통) — 프로젝트·그룹·파일
def _to_trash(path: Path, root: Path, label: str) -> Path:
    """완전히 지우지 않고 데이터 폴더의 .trash/<시각>-<이름>으로 옮긴다 (되살릴 수 있게)."""
    path = Path(path).resolve()
    trash = Path(root).resolve() / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
    n = 1
    while dest.exists():
        n += 1
        dest = trash / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}-{n}"
    shutil.move(str(path), str(dest))
    return dest


def delete_project(project_dir: Path | str) -> Path:
    """프로젝트 폴더를 휴지통(.trash)으로. 게임 용어집·아이콘 사전(games/)은 그대로 둔다."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    return _to_trash(info.dir, info.dir.parent.parent, f"project-{info.dir.name}")


def delete_game(game: Path | str, root: Path | None = None, with_projects: bool = False) -> Path:
    """게임 폴더(games/<게임>: 용어집·아이콘 사전·검수 엑셀)를 휴지통(.trash)으로. 게임 이름이나 폴더 경로를 받는다.
    with_projects=True면 그 게임의 프로젝트도 모두 휴지통으로 옮긴다 (아니면 프로젝트는 그대로).
    마지막으로 연 프로젝트가 이 게임 것이면 설정에서 지운다. 게임 폴더가 옮겨진 휴지통 경로를 돌려준다."""
    from app import settings, workspace

    root = Path(root) if root else workspace.DEFAULT_ROOT
    g = Path(game)
    if g.is_dir() and (g / "game.json").exists():
        gdir = g.resolve()
        root = gdir.parent.parent
        name = workspace._read(gdir / "game.json").get("name", gdir.name)
    else:
        name = str(game)
        gdir = workspace.game_dir(name, root).resolve()
        if not (gdir / "game.json").exists():
            raise FileNotFoundError(f"게임 폴더가 없습니다: {name}")
    mine = [p for p in workspace.list_projects(root) if p["game"] == name]
    gone = {Path(p["dir"]).resolve() for p in mine}
    if with_projects:
        for p in mine:
            delete_project(p["dir"])
    dest = _to_trash(gdir, root, f"game-{gdir.name}")
    if not with_projects:  # 남은 프로젝트: 용어집·아이콘 사전이 없어졌으니 ‘용어집 확정·번역 끝’ 상태가 맞지 않는다
        for p in mine:
            try:
                _extract.back_to_extracting(workspace.load_project_info(p["dir"]))
            except (OSError, ValueError, KeyError):
                pass
    s = settings.load()
    if s.last_project:
        lp = Path(s.last_project)
        if with_projects and lp.resolve() in gone or not lp.exists():
            s.last_project = ""
            settings.save(s)
    return dest


RESET_WHAT = {
    "all":("extract", "cleared", "translated"),  # 글자 추출부터 다시
    "output": ("cleared", "translated"),  # 지운 카드·한글 카드만 다시
}


def reset_files(project_dir: Path | str, files: list | None = None, what: str = "output") -> dict:
    """초기화: 고른 파일(없으면 전체)의 작업 결과를 휴지통(.trash)으로 옮긴다. 파일·영역·뺄 쪽 설정은 그대로.
    (삭제와 다름: 삭제는 그룹·파일 자체를 프로젝트에서 뺀다.)
    what="all"이면 추출 결과까지 -> 프로젝트 상태를 ‘추출 중’으로 되돌린다. 돌려준다 {"moved": [...], "files": n}."""
    from app import workspace

    if what not in RESET_WHAT:
        raise ValueError(f"모르는 초기화: {what}")
    info = workspace.load_project_info(project_dir)
    root = workspace.data_root(info)
    moved = []
    fds = _extract.scoped_file_dirs(info, None, files)
    for fd in fds:
        for sub in RESET_WHAT[what]:
            d = fd / sub
            if d.exists():
                moved.append(str(_to_trash(d, root, f"{info.name}-{fd.parent.parent.name}-{fd.name}-{sub}")))
    if what == "all" and fds and info.status != "extracting":
        workspace.set_status(info, "extracting")
    return {"moved": moved, "files": len(fds)}


def rename_group(project_dir: Path | str, old: str, new: str) -> Path:
    """그룹 이름을 바꾼다 (새 그룹 폴더). 검수 엑셀은 다시 만들기 전까지 예전 이름으로도 찾는다."""
    from app import workspace

    return workspace.rename_group(workspace.load_project_info(project_dir), old, new)


def delete_group(project_dir: Path | str, group: str) -> Path:
    """그룹(영역 템플릿과 그 안의 파일 결과)을 휴지통으로."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    gdir = workspace.group_dir(info, group)
    if not (gdir / "group.json").exists():
        raise KeyError(f"그룹이 없습니다: {group}")
    dest = _to_trash(gdir, info.dir.parent.parent, f"group-{info.dir.name}-{gdir.name}")
    if not workspace.file_dirs(info):  # 파일이 하나도 남지 않으면 처음부터
        _extract.back_to_extracting(info)
    return dest


def delete_file(file_dir: Path | str) -> Path:
    """그룹에서 파일 하나(원본 사본·추출·지운 카드·한글 카드)를 휴지통으로. 원래 PDF는 건드리지 않는다."""
    from app import workspace

    fd = Path(file_dir).resolve()
    info = workspace.project_of(fd)
    if info is None or fd.parent.name != "files":
        raise ValueError(f"그룹 안의 파일 폴더가 아닙니다: {fd}")
    dest = _to_trash(fd, info.dir.parent.parent, f"file-{info.dir.name}-{fd.parent.parent.name}-{fd.name}")
    if not workspace.file_dirs(info):  # 파일이 하나도 남지 않으면 처음부터
        _extract.back_to_extracting(info)
    return dest


# ---------------------------------------------------------------- 화면용: 로그인 (Claude 구독 / API 키)
def api_key_info() -> dict:
    """{"has_key", "source", "masked"}: 키 값 자체는 돌려주지 않는다."""
    from app.credentials import masked
    from app.extract import api_key_source

    k, src = api_key_source()
    return {"has_key": bool(k), "source": src, "masked": masked(k)}


def set_api_key(key: str, *, validate: bool = True, client_factory=None) -> tuple[bool, str]:
    """API 키를 확인한 뒤 암호화해 저장한다. (성공, 메시지). validate=False면 확인 없이 저장."""
    from app.credentials import save_api_key
    from app.extract import make_client

    key = (key or "").strip()
    if not key.startswith("sk-"):
        return False, "API 키 형식이 아닙니다 (sk-ant-… 로 시작)"
    if validate:
        try:
            client = (client_factory or make_client)(key)
            client.models.list(limit=1)  # 토큰을 쓰지 않는 확인
        except Exception as e:  # noqa: BLE001
            msg = str(getattr(e, "message", e))
            if "401" in msg or "authentication" in msg.lower() or "invalid" in msg.lower():
                return False, "키가 맞지 않습니다 (인증 실패)"
            return False, f"키를 확인하지 못했습니다: {msg[:200]}"
    save_api_key(key)
    return True, "API 키를 확인하고 이 컴퓨터에 암호화해 저장했습니다"


def clear_api_key() -> None:
    from app.credentials import delete_api_key

    delete_api_key()


def claude_login_status(check: bool = False, runner=None) -> dict:
    """Claude Code(구독) 상태: {"installed", "logged_in": True/False/None, "message"}.
    check=True면 아주 짧은 요청(Haiku)을 한 번 보내 로그인을 확인한다 (사용량이 조금 든다)."""
    from app.extract import Paused
    from app.extract_cc import CardError, ClaudeCodeClient, NotAvailable

    exe = find_claude()
    if not exe and runner is None:
        return {"installed": False, "logged_in": False,
                "message": "Claude Code가 설치되어 있지 않습니다 (claude 명령 없음)"}
    if not check:
        return {"installed": True, "logged_in": None, "message": "Claude Code를 찾았습니다 (로그인 확인 전)"}
    try:
        client = ClaudeCodeClient(exe=exe or "claude", runner=runner)
        client.ask_json("Reply with ok=true.", {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                                "required": ["ok"], "additionalProperties": False},
                        "claude-haiku-5-5", "You are a health check.", _settings.data_dir() / "work" / "login-check")
    except NotAvailable as e:
        if "Git for Windows" in str(e):
            return {"installed": True, "logged_in": False, "message": str(e)[:300]}
        return {"installed": True, "logged_in": False,
                "message": f"로그인되어 있지 않습니다 — ‘로그인 창 열기’에서 로그인하세요 ({str(e)[:160]})"}
    except Paused as e:
        return {"installed": True, "logged_in": True, "message": f"로그인됨 (지금은 사용 한도: {e.reason[:120]})"}
    except CardError as e:
        return {"installed": True, "logged_in": None, "message": f"확인하지 못했습니다: {str(e)[:200]}"}
    return {"installed": True, "logged_in": True, "message": "Claude에 로그인되어 있습니다"}


def launch_claude_login() -> None:
    """새 터미널 창에서 claude를 띄운다 (사용자가 /login 으로 로그인)."""
    import subprocess
    import sys

    exe = find_claude()
    if not exe:
        raise OSError("Claude Code가 설치되어 있지 않습니다 — ‘Claude Code 설치’를 누르세요")
    if sys.platform == "win32":
        guide = ("echo ===== Claude 로그인 ===== & "
                 "echo  처음이면 안내가 차례로 나옵니다: 글자 색은 아무거나 Enter, "
                 "로그인 방법은 Claude account with subscription 을 골라 Enter, 브라우저에서 승인. & "
                 "echo  코드를 붙여 넣으라고 하면 브라우저의 코드를 복사해 마우스 오른쪽 클릭 후 Enter. & "
                 "echo  안내 없이 바로 ^> 입력 칸이 나오면 /login 을 입력하고 Enter. & "
                 "echo  로그인이 끝나면 이 창을 닫고 CardTranslater에서 [로그인 확인]을 누르세요. & echo.")
        subprocess.Popen(["cmd", "/c", "start", "Claude 로그인", "cmd", "/k", f'{guide} & "{exe}"'])
    else:
        subprocess.Popen(["x-terminal-emulator", "-e", exe])


def install_claude_code(progress: _base.ProgressFn = _base._noop, cancel: _base.CancelToken | None = None) -> str:
    """Claude Code가 없으면 Node.js(없을 때만, 사용자 폴더)와 Claude Code(npm)를 설치한다. claude 경로를 돌려준다.
    실패하면 claude_setup.SetupError (메시지는 그대로 보여 줘도 된다)."""
    from app import claude_setup

    def say(stage, done, total, message):
        progress(_base.ProgressEvent(stage, done, total, message))

    return claude_setup.install_claude_code(say, cancel)


def start_logging() -> Path:
    """동작 로그를 열고 시작 정보(버전·Windows·데이터 폴더·엔진·Claude Code 위치)를 남긴다. 로그 파일 경로."""
    from app import applog

    path = applog.setup()
    applog.log_startup({"data_dir": str(_settings.data_dir()), "engine": _settings.engine_status(), "claude": find_claude()})
    return path


def log_folder() -> Path:
    """동작 로그 폴더 (테스트하는 사람이 보내 줄 파일)."""
    from app import applog

    return Path(applog._state["file"]).parent if applog._state.get("file") else applog.log_dir()


def log_job_time(name: str, secs: float, state: str) -> None:
    """화면 작업 하나가 끝날 때 사이클 시간표 한 줄 (‘[시간] 작업 ① 글자 추출 …’)."""
    applog.log_job_time(name, secs, state)


def timing_report() -> str:
    """로그의 단계별 시간만 모은 표 (오래된 것부터)."""
    return applog.timing_report()


def export_logs(dest: Path) -> Path:
    """로그 파일과 환경 정보(버전·Windows·엔진·Claude Code 위치)를 zip 하나로 묶는다. API 키는 가린다."""
    from app import applog

    extra = {"data_dir": str(_settings.data_dir()), "engine": _settings.engine_status(), "claude": find_claude()}
    return applog.export(Path(dest), extra)


def set_engine(engine: str) -> None:
    """로그인 방식(엔진)만 바꾼다: claude-code(Claude 구독) | api(API 키)."""
    from app import settings

    s = settings.load()
    s.engine = engine
    settings.save(s)


# ---------------------------------------------------------------- 이미지(JPG·PNG) 카드 넣기 (F27)
@dataclass
class ImageAddResult:
    group: str
    file_dir: Path | None
    pages: int = 0
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    has_regions: bool = False


def image_preview(image: Path | str, cols: int = 1, rows: int = 1) -> tuple[bytes, dict]:
    """자를 자리를 표시한 미리 보기 PNG와 {"cards", "gutters_found", "warnings", "size"}."""
    from app import images

    png, cut = images.preview_png(image, cols, rows)
    return png, {"cards": len(cut.boxes), "gutters_found": cut.gutters_found, "warnings": cut.warnings,
                 "size": cut.size}


def image_display(image: Path | str, max_px: int = 1600) -> tuple[bytes, tuple[int, int], float]:
    """수동으로 카드 자리를 그릴 때 보여 줄 그림: (PNG, 원본 크기 (w, h), 축소 배율). 화면 px / 배율 = 원본 px."""
    from app import images

    return images.display_png(image, max_px)


def card_reading_order(boxes: list) -> list:
    """카드 상자를 PDF 쪽 순서(위 줄부터, 줄 안에서는 왼쪽부터)로."""
    from app import images

    return images.reading_order(boxes)


def pdf_page_count(pdf: Path | str) -> int:
    """PDF 파일의 쪽 수 (열 수 없으면 0)."""
    import pymupdf

    try:
        with pymupdf.open(pdf) as doc:
            return doc.page_count
    except Exception:  # noqa: BLE001 - 깨진 PDF는 넣을 때 따로 알린다
        return 0


def pdf_sheet_images(pdf: Path | str, out_dir: Path | str) -> list[Path]:
    """한 쪽에 카드가 여러 장인 PDF: 쪽마다 PNG로 바꿔 out_dir에 둔다 (이미지 시트처럼 배열·수동으로 자르려고)."""
    from app import images

    return images.pdf_to_images(pdf, out_dir)


def add_images(project_dir: Path | str, files: list, group: str, *, cols: int = 1, rows: int = 1,
               name: str | None = None, new_group: bool = False,
               regions_from: Path | str | None = None, boxes: dict | None = None,
               origin_pdf: Path | str | None = None) -> ImageAddResult:
    """이미지 여러 장을 카드 한 장이 한 쪽인 PDF 하나로 바꿔 그룹에 넣는다 (파일 이름 name, 없으면 첫 이미지 이름).
    한 이미지에 카드가 가로 cols × 세로 rows로 놓여 있으면 잘라서 넣는다. 원본 이미지는 source/images/에 사본을 둔다.
    boxes: 수동으로 영역 지정 {이미지 경로: [(x0, y0, x1, y1) 원본 px, …]} (있으면 cols·rows 대신).
    origin_pdf: 이미지가 PDF 쪽을 그린 것(pdf_sheet_images)이면 그 PDF. 사본을 source/images/에 함께 둔다."""
    import tempfile

    from app import images, workspace

    info = workspace.load_project_info(project_dir)
    imgs = [Path(f) for f in files if images.is_image(f)]
    skipped = [f"{Path(f).name}: 이미지가 아님" for f in files if not images.is_image(f)]
    if not imgs:
        raise ValueError("넣을 이미지가 없습니다 (JPG·PNG·WEBP·BMP·TIFF)")
    stem = (name or imgs[0].stem).strip()
    with tempfile.TemporaryDirectory() as td:
        pdf = Path(td) / f"{stem}.pdf"
        res = images.images_to_pdf(imgs, pdf, cols, rows, boxes=boxes)
        r = workspace.add_files(info, [pdf], group, new_group=new_group, regions_from=regions_from)
    skipped += r["skipped"]
    if not r["added"]:
        return ImageAddResult(group, None, 0, res.warnings, skipped, r["has_regions"])
    fd = Path(r["added"][0])
    keep = fd / "source" / "images"
    keep.mkdir(parents=True, exist_ok=True)
    for f in imgs:  # 원본 이미지 사본 (나중에 다시 자를 때·확인용)
        shutil.copyfile(f, keep / f.name)
    if origin_pdf:  # source/ 바로 아래에는 카드 PDF 하나만 둔다 (원래 PDF는 images/에)
        shutil.copyfile(origin_pdf, keep / Path(origin_pdf).name)
    settings = workspace.file_settings(fd)
    settings["from_images"] = {"files": [f.name for f in imgs], "cols": cols, "rows": rows,
                               **({"manual": True} if boxes is not None else {}),
                               **({"pdf": Path(origin_pdf).name} if origin_pdf else {}),
                               "sheets": [c.layout() for c in res.sheets]}  # 결과를 원래 배열로 다시 합칠 때
    workspace._write(fd / "file.json", settings)
    if fd is not None:
        _extract.back_to_extracting(workspace.load_project_info(project_dir))  # 새 카드: add_files와 같은 이유
    return ImageAddResult(group, fd, res.pages, res.warnings, skipped, r["has_regions"])
