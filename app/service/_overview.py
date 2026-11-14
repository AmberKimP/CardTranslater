"""화면용 프로젝트 목록·개요·다음 단계, 쪽 고르기·쪽 그림."""

from __future__ import annotations

import json
from pathlib import Path

from app.service import _review
from app.service import _translate_erase


# ================================================================ 화면용 (PySide6 등)
# 화면은 아래 함수와 위의 작업 함수만 부른다. 결과는 경로·상태를 담은 dict/dataclass로 준다.
REGION_KINDS = ("text", "tag_pills")
# 글자 지우기 방식 (그림 카드, app.erase_text). [(키, 화면 문구)]
ERASE_MODES = [("auto", "자동 (배경 보고 고름)"), ("solid", "단색: 주변 색으로 덮기"),
               ("pattern", "무늬: 주변 무늬로 채우기"), ("rect_avg", "그림: 사각형 + 평균색"),
               ("inpaint", "그림: 뒤 그림 유추해 채우기"),
               ("picture", "복잡한 그림 배경: 획 모양으로 찾아 그림 결 따라 채우기")]  # app.erase_picture
ALIGNS = ("left", "center", "right")


def list_projects(root: Path | None = None) -> list[dict]:
    """[{"name", "game", "status", "dir"}] (데이터 폴더의 프로젝트)."""
    from app import workspace

    return workspace.list_projects(Path(root) if root else workspace.DEFAULT_ROOT)


STATUS_LABEL = {
    "extracting": "추출 중", "glossary_review": "용어집 검증 중", "glossary_confirmed": "용어집 확정",
    "translating": "번역 중", "translated": "번역 완료",
}


def project_overview(project_dir: Path | str) -> dict:
    """프로젝트 현황: 그룹·파일별 쪽 수·뺀 쪽·추출·지우기·한글 넣기 상태와 다음에 할 일."""
    from app import workspace
    from app.extract import load_extracted

    from app.regions import load_template

    info = workspace.load_project_info(project_dir)
    translated = _translated_files(info) if info.status == "translating" else {}
    groups = []
    for g in workspace.list_groups(info):
        files = []
        rpath = g["dir"] / "regions.json"
        tdata = json.loads(rpath.read_text(encoding="utf-8")) if rpath.exists() else {}
        per_card = tdata.get("layout") == "per_card"
        for name in g["files"]:
            fd = g["dir"] / "files" / name
            n = workspace.page_count(fd)
            inc = workspace.included_pages(fd)
            all_inc = workspace.included_pages(fd, limited=False)
            done = set(load_extracted(fd)) & set(inc)
            if per_card:
                tpl = load_template(rpath, file_name=name)
                no_regions = [p for p in inc if not tpl.has_regions(p)]
            else:
                no_regions = [] if rpath.exists() else list(inc)
            files.append({
                "name": name, "key": workspace.file_key(fd), "dir": fd, "pages": n,
                "included": len(inc), "excluded": sorted(set(range(1, n + 1)) - set(all_inc)),
                "exclude_reason": workspace.file_settings(fd).get("exclude_reason", ""),
                "extracted": len(done), "erased": (fd / "cleared" / "cleared.pdf").exists(),
                "composed": (fd / "translated" / "translated.pdf").exists(),
                "review_xlsx": _translate_erase.review_xlsx_for(fd),
                "no_regions": no_regions,
            })
        n_regions = (len({r.get("name") for ps in (tdata.get("page_regions") or {}).values()
                          for r in ps.get("regions", [])}) if per_card else g["regions"])
        for f in files:  # 파일마다 번역이 끝났나 (그룹만 번역해도 그 그룹은 한글 넣기를 할 수 있게)
            if info.status == "translated":
                f["translated"] = True
            elif f["key"] in translated:
                f["translated"] = translated[f["key"]]
            else:  # 번역할 칸이 없는 파일(그림만 있는 카드 등): 추출이 끝났으면 번역할 것도 끝
                f["translated"] = info.status == "translating" and f["included"] > 0 and f["extracted"] >= f["included"]
        groups.append({"name": g["name"], "dir": g["dir"], "regions": n_regions, "per_card": per_card,
                       "has_regions": rpath.exists() and (not per_card or any(
                           len(f["no_regions"]) < f["included"] for f in files)),
                       "files": files})
    review = None
    if info.status in ("translating", "translated"):
        try:
            review = _review.review_progress(info.dir)
        except Exception:  # noqa: BLE001 - 검수 진행을 못 세어도 화면은 열린다
            review = None
    return {"name": info.name, "game": info.game, "dir": info.dir, "status": info.status,
            "status_label": STATUS_LABEL.get(info.status, info.status),
            "glossary_version": info.extra.get("glossary_version"), "groups": groups,
            "card_limit": workspace.card_limit(info),
            "review": review,
            "next_step": _next_step(info, groups, review), "next_key": _next_key(info, groups, review)}


_TRANSLATED_CACHE: dict = {}


def _translated_files(info) -> dict[str, bool]:
    """{파일 키: 처리할 칸이 모두 (지금 용어집으로) 번역됐나} (번역 캐시 기준). 그룹만 번역해 프로젝트가
    ‘번역 중’일 때 쓴다. 번역할 칸이 없는 파일은 여기 없다 (현황에서 추출이 끝났으면 번역된 것으로 본다).
    번역 캐시·용어집·추출 파일이 바뀌지 않았으면 다시 세지 않는다 (화면을 새로 고칠 때마다 부른다)."""
    from app import glossary, workspace
    from app.translate import _stale, cache_path, load_cache, lookup

    def mt(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    try:
        fds = workspace.file_dirs(info)
        stamp = (str(info.dir), mt(cache_path(info.dir)), mt(glossary.glossary_path(info)),
                 tuple((str(fd), max([mt(p) for p in (fd / "extract").glob("**/page-*.json")] + [mt(fd / "file.json")]))
                       for fd in fds))
        if _TRANSLATED_CACHE.get("stamp") == stamp:
            return _TRANSLATED_CACHE["value"]
        cache = load_cache(info.dir)
        terms = glossary.load_glossary(info).get("terms", [])
        total: dict[str, int] = {}
        done: dict[str, int] = {}
        for s_ in glossary.collect_sources(info):
            k = s_.key.split("#", 1)[0]
            total[k] = total.get(k, 0) + 1
            hit = lookup(cache, s_.text)
            if hit is not None and not (isinstance(s_.text, str) and _stale(hit, s_.text, terms)):
                done[k] = done.get(k, 0) + 1
        value = {k: done.get(k, 0) >= n for k, n in total.items()}
        _TRANSLATED_CACHE.update(stamp=stamp, value=value)
        return value
    except Exception:  # noqa: BLE001 - 현황 표시는 못 해도 화면은 열린다
        return {}


def _next_key(info, groups: list[dict], review: dict | None = None) -> str:
    """다음에 누를 단계 버튼 키 (화면의 ‘다음 할 일’ 버튼). _next_step 문장과 같은 순서로 판단한다.
    add | regions | extract | finish | glossary | translate | erase | compose | results"""
    if not groups or not any(g["files"] for g in groups):
        return "add"
    if any(not g["has_regions"] for g in groups):
        return "regions"
    if info.status == "extracting":
        if any(f["no_regions"] for g in groups if g.get("per_card") for f in g["files"]):
            return "regions"
        left = [f for g in groups for f in g["files"] if f["extracted"] < f["included"]]
        return "extract" if left else "finish"
    if info.status == "glossary_review":
        return "glossary"
    if info.status in ("glossary_confirmed", "translating"):
        return "translate"
    files = [f for g in groups for f in g["files"]]
    if not all(f["erased"] for f in files):
        return "erase"
    if review and review.get("must_left") and not all(f["composed"] for f in files):
        return "check"  # 꼭 고칠 칸이 남았으면 한글 넣기 전에 번역 검수
    if not all(f["composed"] for f in files):
        return "compose"
    return "results"


def _next_step(info, groups: list[dict], review: dict | None = None) -> str:
    if not groups or not any(g["files"] for g in groups):
        return "파일을 넣으세요"
    no_regions = [g["name"] for g in groups if not g["has_regions"]]
    if no_regions:
        return f"영역을 지정하세요: {', '.join(no_regions)}"
    missing = [f"{f['key']} {len(f['no_regions'])}장" for g in groups if g.get("per_card") for f in g["files"]
               if f["no_regions"]]
    if missing and info.status == "extracting":
        return f"카드마다 영역을 지정하세요 (남은 카드: {', '.join(missing)})"
    left = [f["key"] for g in groups for f in g["files"] if f["extracted"] < f["included"]]
    if info.status == "extracting":
        return f"글자를 추출하세요 ({len(left)}개 파일 남음)" if left else "‘④ 용어집 만들기’로 용어집을 만드세요"
    if info.status == "glossary_review":
        return "용어집 검증 엑셀을 고쳐서 가져오세요"
    if info.status in ("glossary_confirmed", "translating"):
        return "번역을 시작하세요 (끝나면 검수 엑셀까지 만듭니다)"
    files = [f for g in groups for f in g["files"]]
    if not all(f["erased"] for f in files):
        return "글자를 지우세요"
    if review and review.get("must_left") and not all(f["composed"] for f in files):
        return (f"번역을 검수하세요 (꼭 고칠 칸 {review['must_left']}개 · "
                f"확인 {review.get('checked', 0)}/{review.get('total', 0)})")
    if not all(f["composed"] for f in files):
        return "한글을 넣으세요"
    return "결과를 확인하세요"


def set_pages(file_dir: Path | str, exclude: list[int], reason: str = "") -> dict:
    """파일에서 뺄 쪽을 정한다. {"excluded": [...], "included": [...]}."""
    from app import workspace

    n = workspace.page_count(Path(file_dir))
    bad = [p for p in exclude if not 1 <= int(p) <= n]
    if bad:
        raise ValueError(f"없는 쪽: {bad} (1~{n})")
    workspace.set_excluded_pages(Path(file_dir), exclude, reason)
    return {"excluded": sorted(set(exclude)), "included": workspace.included_pages(Path(file_dir), limited=False)}


def move_pages_to_group(file_dir: Path | str, pages: list[int], group: str, *, new_group: bool = False) -> Path:
    """한 파일의 쪽 일부를 다른 그룹(형식)으로 보낸다. 같은 PDF가 그 그룹에도 들어가 그 쪽만 처리되고,
    이 그룹에서는 빠진다. 새 그룹이면 영역 지정이 필요하다."""
    from app import workspace

    return workspace.move_pages_to_group(Path(file_dir), pages, group, new_group=new_group)


def page_count(file_dir: Path | str) -> int:
    from app import workspace

    return workspace.page_count(Path(file_dir))


def shown_pages(file_dir: Path | str) -> list[int]:
    """화면(영역 지정)에서 보일 쪽: 뺄 쪽을 뺀 나머지 (시험 모드 제한은 적용하지 않음). 다 뺐으면 모든 쪽."""
    from app import workspace

    fd = Path(file_dir)
    return workspace.included_pages(fd, limited=False) or list(range(1, workspace.page_count(fd) + 1))


def page_image(file_dir: Path | str, page: int, dpi: int = 110) -> tuple[bytes, tuple[float, float]]:
    """파일 폴더 원본의 한 쪽을 PNG로. (PNG, (너비 pt, 높이 pt))."""
    import pymupdf

    src = next((Path(file_dir) / "source").glob("*.pdf"))
    with pymupdf.open(src) as d:
        if not 1 <= page <= d.page_count:
            raise ValueError(f"없는 쪽: {page}")
        pg = d[page - 1]
        return pg.get_pixmap(dpi=dpi, alpha=False).tobytes("png"), (pg.rect.width, pg.rect.height)


def pdf_page_image(pdf: Path | str, page: int, dpi: int = 110) -> bytes:
    """임의 PDF의 한 쪽 PNG (결과 PDF 미리보기 등)."""
    import pymupdf

    with pymupdf.open(pdf) as d:
        return d[page - 1].get_pixmap(dpi=dpi, alpha=False).tobytes("png")
