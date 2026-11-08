"""게임·프로젝트·그룹 (PRD 2장 ‘게임·프로젝트·그룹’).

폴더 구조 (작업 공간 = 저장소 루트, 둘 다 git에 올리지 않음)

    games/<게임>/game.json                 게임 정보
    games/<게임>/glossary.json             확정 용어집 (게임 단위, 모든 프로젝트가 함께 씀)
    projects/<프로젝트>/project.json       {"game", "status", ...}
    projects/<프로젝트>/groups/<그룹>/group.json
    projects/<프로젝트>/groups/<그룹>/regions.json     그룹의 영역 템플릿 (같은 카드 형식)
    projects/<프로젝트>/groups/<그룹>/files/<파일>/source/<파일>.pdf
                                                        이 폴더가 지금까지의 ‘프로젝트 폴더’와 같은 모양이라
                                                        추출·지우기·한글 넣기를 그대로 쓴다 (extract/, cleared/, ...)

프로젝트 상태: extracting -> glossary_review -> glossary_confirmed -> translating ...
예전 방식의 projects/<이름>/(source/ + regions.json)도 그대로 쓸 수 있다 (project.json 없음).
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
STATUSES = ("extracting", "glossary_review", "glossary_confirmed", "translating", "translated")
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe(name: str) -> str:
    """폴더 이름으로 쓸 수 있는 이름 (한글·일본어는 그대로, 경로 문자만 막음)."""
    n = _BAD.sub("_", name.strip()).strip(". ")
    if not n:
        raise ValueError(f"쓸 수 없는 이름: {name!r}")
    return n


def _read(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def games_dir(root: Path = DEFAULT_ROOT) -> Path:
    return Path(root) / "games"


def projects_dir(root: Path = DEFAULT_ROOT) -> Path:
    return Path(root) / "projects"


# ---------------------------------------------------------------- 게임
def list_games(root: Path = DEFAULT_ROOT) -> list[dict]:
    out = []
    d = games_dir(root)
    for g in sorted(d.iterdir()) if d.exists() else []:
        if not (g / "game.json").exists():
            continue
        info = _read(g / "game.json")
        gl = g / "glossary.json"
        terms = len(_read(gl).get("terms", [])) if gl.exists() else 0
        projects = [p["name"] for p in list_projects(root) if p["game"] == info["name"]]
        files = {"glossary_review": (g / "용어집_검증.xlsx").exists(), "icon_review": (g / "아이콘_검증.xlsx").exists(),
                 "translation_review": (g / "번역_검수.xlsx").exists()}
        out.append({"name": info["name"], "dir": g, "terms": terms, "projects": projects, "files": files})
    return out


def game_dir(game: str, root: Path = DEFAULT_ROOT) -> Path:
    return games_dir(root) / _safe(game)


# ---------------------------------------------------------------- 프로젝트
@dataclass
class ProjectInfo:
    name: str
    game: str
    dir: Path
    status: str = "extracting"
    source_language: str = "ja"
    extra: dict = field(default_factory=dict)

    @property
    def game_dir(self) -> Path:
        return self.dir.parent.parent / "games" / _safe(self.game)


def create_project(name: str, game: str, source_language: str = "ja",
                   root: Path = DEFAULT_ROOT) -> tuple[ProjectInfo, bool]:
    """(프로젝트, 새 게임을 만들었는지). 게임이 없으면 만든다.
    같은 게임 안에 같은 이름의 프로젝트가 있으면 FileExistsError (다른 게임이면 같은 이름도 된다)."""
    gdir = game_dir(game, root)
    new_game = not (gdir / "game.json").exists()
    if new_game:
        _write(gdir / "game.json", {"name": game.strip(), "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "source_language": source_language})
    gname = _read(gdir / "game.json")["name"]
    pd = projects_dir(root)
    for f in (pd.glob("*/project.json") if pd.exists() else []):  # 같은 게임 안에 같은 이름은 안 된다
        d = _read(f)
        if d.get("game") == gname and d.get("name", "").strip() == name.strip():
            raise FileExistsError(str(f.parent))
    pdir = pd / _safe(name)
    if pdir.exists():  # 다른 게임의 같은 이름 프로젝트: 폴더 이름만 다르게 (화면에는 프로젝트 이름 그대로)
        pdir = pd / _safe(f"{name} ({gname})")
        n = 2
        while pdir.exists():
            pdir = pd / _safe(f"{name} ({gname}) {n}")
            n += 1
    _write(pdir / "project.json", {"name": name.strip(), "game": _read(gdir / "game.json")["name"],
                                   "status": "extracting", "source_language": source_language,
                                   "created": time.strftime("%Y-%m-%d %H:%M:%S")})
    return load_project_info(pdir), new_game


def load_project_info(project_dir: Path | str) -> ProjectInfo:
    d = Path(project_dir).resolve()
    f = d / "project.json"
    if not f.exists():
        raise FileNotFoundError(f"{d}는 게임에 속한 프로젝트가 아닙니다 (project.json 없음). new-project로 만드세요")
    data = _read(f)
    return ProjectInfo(data["name"], data["game"], d, data.get("status", "extracting"),
                       data.get("source_language", "ja"),
                       {k: v for k, v in data.items() if k not in ("name", "game", "status", "source_language")})


def set_status(info: ProjectInfo, status: str, **extra) -> None:
    assert status in STATUSES, status
    data = _read(info.dir / "project.json")
    data["status"] = status
    data.update(extra)
    _write(info.dir / "project.json", data)
    info.status = status


def data_root(info: ProjectInfo) -> Path:
    """games/·projects/가 들어 있는 데이터 폴더."""
    return info.dir.parent.parent


def game_projects(info: ProjectInfo) -> list[ProjectInfo]:
    """같은 게임의 모든 프로젝트 (이름 순, 자기 자신 포함)."""
    out = []
    pd = info.dir.parent
    for d in sorted(pd.iterdir()) if pd.exists() else []:
        f = d / "project.json"
        if f.exists() and _read(f).get("game") == info.game:
            out.append(load_project_info(d))
    if not any(p.dir == info.dir for p in out):
        out.append(info)
    return out


def game_file_dirs(info: ProjectInfo) -> list[Path]:
    """같은 게임의 모든 프로젝트·그룹·파일 폴더 (용어집·아이콘·검수 엑셀은 게임 단위)."""
    return [fd for p in game_projects(info) for fd in file_dirs(p)]


def game_file_key(file_dir: Path) -> str:
    """'프로젝트/그룹/파일' (게임 안에서 겹치지 않는 이름)."""
    fd = Path(file_dir)
    return f"{fd.parents[3].name}/{fd.parent.parent.name}/{fd.name}"


def card_limit(info: ProjectInfo) -> int | None:
    """시험 모드: 파일마다 앞에서부터 이 장수까지만 추출·번역·검수·지우기·한글 넣기 (없으면 전체)."""
    n = info.extra.get("card_limit")
    return int(n) if isinstance(n, int) and n > 0 else None


def set_card_limit(info: ProjectInfo, n: int | None) -> None:
    """n이 None이거나 0 이하면 시험 모드를 끈다 (전체 카드)."""
    data = _read(info.dir / "project.json")
    if n and int(n) > 0:
        data["card_limit"] = int(n)
        info.extra["card_limit"] = int(n)
    else:
        data.pop("card_limit", None)
        info.extra.pop("card_limit", None)
    _write(info.dir / "project.json", data)


def limit_pages(file_dir: Path, pages: list[int]) -> list[int]:
    """프로젝트가 시험 모드면 앞에서부터 card_limit장만."""
    info = project_of(Path(file_dir))
    n = card_limit(info) if info else None
    return pages[:n] if n else pages


def list_projects(root: Path = DEFAULT_ROOT) -> list[dict]:
    out = []
    d = projects_dir(root)
    for p in sorted(d.iterdir()) if d.exists() else []:
        if (p / "project.json").exists():
            data = _read(p / "project.json")
            out.append({"name": data["name"], "game": data["game"], "status": data.get("status"), "dir": p})
    return out


# ---------------------------------------------------------------- 그룹
def groups_dir(info: ProjectInfo) -> Path:
    return info.dir / "groups"


def list_groups(info: ProjectInfo) -> list[dict]:
    out = []
    d = groups_dir(info)
    for g in sorted(d.iterdir()) if d.exists() else []:
        if not (g / "group.json").exists():
            continue
        regions = g / "regions.json"
        n_regions = len(_read(regions).get("regions", [])) if regions.exists() else 0
        files = [f.name for f in sorted((g / "files").iterdir())] if (g / "files").exists() else []
        out.append({"name": _read(g / "group.json")["name"], "dir": g, "files": files, "regions": n_regions})
    return out


def group_dir(info: ProjectInfo, group: str) -> Path:
    return groups_dir(info) / _safe(group)


def group_names(gdir: Path) -> set[str]:
    """그룹의 이름들: 지금 폴더 이름과 예전 폴더 이름(이름을 바꾸기 전). 검수 엑셀이 예전 이름으로 적혀 있어도 찾게."""
    g = Path(gdir)
    names = {g.name}
    f = g / "group.json"
    if f.exists():
        names |= set(_read(f).get("old_names") or [])
    return names


def rename_group(info: ProjectInfo, old: str, new: str) -> Path:
    """그룹 이름을 바꾼다 (폴더도 새 이름으로). 예전 폴더 이름은 group.json에 남겨 검수 엑셀이 계속 찾게 한다.
    같은 이름의 다른 그룹이 있거나 이름이 비면 ValueError. 파일이 열려 있어 폴더를 못 옮기면 OSError."""
    new = (new or "").strip()
    src = group_dir(info, old)
    if not (src / "group.json").exists():
        raise KeyError(f"그룹이 없습니다: {old}")
    if not new:
        raise ValueError("새 이름이 비었습니다")
    dst = group_dir(info, new)
    same_folder = src.name.casefold() == dst.name.casefold()  # 대소문자만 바뀜 (Windows는 같은 폴더로 본다)
    if not same_folder and dst.exists():
        raise ValueError(f"같은 이름의 그룹이 이미 있습니다: {new}")
    data = _read(src / "group.json")
    old_display = data.get("name") or src.name
    olds = [n for n in (data.get("old_names") or []) if n != dst.name]
    if src.name != dst.name and src.name not in olds:
        olds.append(src.name)
    data["name"] = new
    if olds:
        data["old_names"] = olds
    if src.name != dst.name:
        if same_folder:  # 대소문자만 다르면 임시 이름을 거쳐 바꾼다
            tmp = src.with_name(src.name + ".renaming")
            src.rename(tmp)
            tmp.rename(dst)
        else:
            src.rename(dst)
    _write(dst / "group.json", data)
    # 다른 그룹 파일이 이 그룹을 이름으로 가리키는 연결(쪽을 다른 그룹으로 옮긴 것)도 새 이름으로
    for fj in groups_dir(info).glob("*/files/*/file.json"):
        fdata = _read(fj)
        links = fdata.get("pages_in_groups") or {}
        changed = False
        for key in list(links):
            if key in (old, old_display, src.name):
                links[new] = sorted(set(links.get(new, [])) | set(links.pop(key)))
                changed = True
        if changed:
            fdata["pages_in_groups"] = links
            _write(fj, fdata)
    return dst


def add_files(info: ProjectInfo, files: list[Path | str], group: str, *, new_group: bool = False,
              regions_from: Path | str | None = None) -> dict:
    """파일을 그룹에 넣는다. new_group=True면 그룹을 새로 만든다 (이미 있으면 FileExistsError).
    기존 그룹에 넣는데 그룹이 없으면 KeyError (화면이 그룹 목록을 보여 주고 다시 고르게 한다).
    regions_from: 새 그룹의 영역 템플릿으로 복사할 regions.json. 없으면 영역 지정(2단계)이 필요하다."""
    gdir = group_dir(info, group)
    exists = (gdir / "group.json").exists()
    if new_group and exists:
        raise FileExistsError(f"그룹이 이미 있습니다: {group}")
    if not new_group and not exists:
        raise KeyError(f"그룹이 없습니다: {group}")
    if new_group:
        _write(gdir / "group.json", {"name": group.strip(), "created": time.strftime("%Y-%m-%d %H:%M:%S")})
    if regions_from:
        shutil.copyfile(regions_from, gdir / "regions.json")
    added, skipped = [], []
    for f in files:
        f = Path(f)
        if f.suffix.lower() != ".pdf":
            skipped.append(f"{f.name}: PDF만 지원")
            continue
        dest = gdir / "files" / _safe(f.stem) / "source" / f.name
        if dest.exists():
            skipped.append(f"{f.name}: 이미 들어 있음")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dest)  # 원본은 읽기만 한다 (사본으로 작업)
        added.append(dest.parent.parent)
    return {"group": group, "added": added, "skipped": skipped, "has_regions": (gdir / "regions.json").exists()}


def move_pages_to_group(file_dir: Path, pages: list[int], group: str, *, new_group: bool = False,
                        regions_from: Path | str | None = None) -> Path:
    """한 PDF 안에 형식이 다른 카드가 섞여 있을 때: 고른 쪽을 다른 그룹에서 처리하게 한다.
    같은 PDF를 그 그룹에도 넣고(이미 있으면 그대로) 그 그룹에서는 고른 쪽만 처리하게 하며,
    이 그룹에서는 그 쪽을 뺀다. 돌려주는 값: 그 그룹의 파일 폴더."""
    src_dir = Path(file_dir)
    info = project_of(src_dir)
    if info is None:
        raise ValueError("게임 프로젝트의 파일이 아닙니다")
    pages = sorted({int(p) for p in pages})
    n = page_count(src_dir)
    if not pages or any(not 1 <= p <= n for p in pages):
        raise ValueError(f"쪽을 1~{n} 안에서 고르세요")
    src_group = _read(src_dir.parent.parent / "group.json")["name"]
    if group == src_group:
        raise ValueError("지금 그룹과 같은 그룹입니다")
    pdf = sorted((src_dir / "source").glob("*.pdf"))[0]
    gdir = group_dir(info, group)
    target = gdir / "files" / src_dir.name
    tpdf = target / "source" / pdf.name
    if tpdf.exists():
        import hashlib

        h = lambda f: hashlib.sha256(Path(f).read_bytes()).hexdigest()  # noqa: E731
        if h(tpdf) != h(pdf):
            raise FileExistsError(f"'{group}' 그룹에 이름이 같은 다른 파일이 있습니다: {src_dir.name}")
        if new_group:
            new_group = False
    else:
        add_files(info, [pdf], group, new_group=new_group, regions_from=regions_from)
        set_excluded_pages(target, list(range(1, n + 1)), f"{src_group} 그룹의 쪽")  # 처음엔 모두 뺀 채로
    tset = file_settings(target)
    t_excl = set(tset.get("exclude_pages", [])) - set(pages)
    set_excluded_pages(target, sorted(t_excl), tset.get("exclude_reason") or f"{src_group} 그룹의 쪽")
    sset = file_settings(src_dir)
    s_excl = set(sset.get("exclude_pages", [])) | set(pages)
    set_excluded_pages(src_dir, sorted(s_excl), sset.get("exclude_reason") or "다른 그룹에서 처리")
    for d, other, moved in ((src_dir, group, pages), (target, src_group, None)):
        data = file_settings(d)
        links = data.setdefault("pages_in_groups", {})
        if moved is not None:
            links[other] = sorted(set(links.get(other, [])) | set(moved))
        else:
            links[other] = sorted(set(range(1, n + 1)) - set(included_pages(target, limited=False)))
        _write(d / "file.json", data)
    return target


def file_dirs(info: ProjectInfo, group: str | None = None) -> list[Path]:
    """파일 폴더 목록 (그룹을 주면 그 그룹만). 파일 폴더 = 추출·지우기·한글 넣기를 하는 단위."""
    out = []
    for g in list_groups(info):
        if group is None or g["name"] == group:
            out += [g["dir"] / "files" / f for f in g["files"]]
    return out


def project_of(file_dir: Path) -> ProjectInfo | None:
    """그룹 안 파일 폴더 -> 그 프로젝트 (게임 프로젝트가 아니면 None)."""
    p = Path(file_dir).resolve()
    proj = p.parent.parent.parent.parent  # files/<파일> -> <그룹> -> groups -> <프로젝트>
    if (p.parent.parent / "group.json").exists() and (proj / "project.json").exists():
        return load_project_info(proj)
    return None


def file_key(file_dir: Path) -> str:
    """'그룹/파일' (용어 예문·번역 행 key 앞부분)."""
    return f"{file_dir.parent.parent.name}/{file_dir.name}"


# ---------------------------------------------------------------- 파일별 설정 (뺄 쪽)
def file_settings(file_dir: Path) -> dict:
    """<파일 폴더>/file.json. {"exclude_pages": [1], "exclude_reason": "..."}"""
    f = Path(file_dir) / "file.json"
    return _read(f) if f.exists() else {}


def set_excluded_pages(file_dir: Path, pages: list[int], reason: str = "") -> dict:
    data = file_settings(file_dir)
    data["exclude_pages"] = sorted(set(int(p) for p in pages))
    if reason:
        data["exclude_reason"] = reason
    _write(Path(file_dir) / "file.json", data)
    return data


def page_count(file_dir: Path) -> int:
    import pymupdf

    pdfs = sorted((Path(file_dir) / "source").glob("*.pdf"))
    if not pdfs:
        return 0
    with pymupdf.open(pdfs[0]) as d:
        return d.page_count


def included_pages(file_dir: Path, limited: bool = True) -> list[int]:
    """처리할 쪽 (1부터). 그룹 형식과 다른 쪽(예: 규칙 카드)을 뺀 나머지.
    file.json이 없는 폴더(예전 방식 프로젝트 포함)는 모든 쪽.
    limited=True면 프로젝트 시험 모드(card_limit)를 적용한다. 뺀 쪽을 보여 줄 때는 False."""
    excl = set(file_settings(file_dir).get("exclude_pages", []))
    pages = [p for p in range(1, page_count(file_dir) + 1) if p not in excl]
    return limit_pages(file_dir, pages) if limited else pages


def pages_spec(pages: list[int]) -> str:
    """[1,2,3,5,7,8] -> '1-3,5,7-8'"""
    out, i = [], 0
    pages = sorted(pages)
    while i < len(pages):
        j = i
        while j + 1 < len(pages) and pages[j + 1] == pages[j] + 1:
            j += 1
        out.append(str(pages[i]) if i == j else f"{pages[i]}-{pages[j]}")
        i = j + 1
    return ",".join(out)
