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
        out.append({"name": info["name"], "dir": g, "terms": terms, "projects": projects})
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
    """(프로젝트, 새 게임을 만들었는지). 게임이 없으면 만든다. 같은 이름의 프로젝트가 있으면 FileExistsError."""
    gdir = game_dir(game, root)
    new_game = not (gdir / "game.json").exists()
    if new_game:
        _write(gdir / "game.json", {"name": game.strip(), "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "source_language": source_language})
    pdir = projects_dir(root) / _safe(name)
    if pdir.exists():
        raise FileExistsError(str(pdir))
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
