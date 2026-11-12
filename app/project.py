"""프로젝트 폴더 읽기와 페이지 지정 해석."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.regions import RegionTemplate, load_template


@dataclass
class Project:
    root: Path
    source_pdf: Path
    template: RegionTemplate
    samples: dict  # page(1-based) -> 카드 샘플 dict
    regions_path: Path | None = None  # 영역 템플릿 파일 (그룹 안 파일이면 그룹의 것)


def load_project(root: Path) -> Project:
    root = Path(root).resolve()
    sources = sorted((root / "source").glob("*.pdf"))
    if len(sources) != 1:
        raise FileNotFoundError(f"{root / 'source'} 에 PDF가 정확히 하나 있어야 합니다 (찾은 수: {len(sources)})")
    regions = root / "regions.json"
    if not regions.exists() and (root.parent.parent / "group.json").exists():
        regions = root.parent.parent / "regions.json"  # 그룹 안의 파일 폴더: 그룹의 영역 템플릿을 쓴다
    if not regions.exists():
        raise FileNotFoundError(f"영역 템플릿이 없습니다: {regions} (영역 지정이 필요합니다)")
    template = load_template(regions, file_name=root.name)
    from app import align  # 자동 정렬 (파일 폴더의 align.json): 공통 영역을 카드마다 옮긴다

    template.align = align.page_transforms(align.load(root))
    samples: dict = {}
    sp = root / "samples.json"
    if sp.exists():
        for card in json.loads(sp.read_text(encoding="utf-8")).get("cards", []):
            samples[int(card["page"])] = card
    return Project(root=root, source_pdf=sources[0], template=template, samples=samples, regions_path=regions)


def parse_page_spec(spec: str, page_count: int) -> list[int]:
    """'1, 5, 12-14' -> [1, 5, 12, 13, 14] (1부터 세는 번호, 중복 제거, 입력 순서 유지)."""
    pages: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
            if lo > hi:
                raise ValueError(f"잘못된 범위: {part}")
            rng = range(lo, hi + 1)
        else:
            rng = [int(part)]
        for p in rng:
            if not 1 <= p <= page_count:
                raise ValueError(f"페이지 {p} 는 범위(1~{page_count}) 밖입니다")
            if p not in pages:
                pages.append(p)
    if not pages:
        raise ValueError("페이지가 지정되지 않았습니다")
    return pages


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_not_source(out_path: Path, source: Path) -> None:
    """결과 경로가 원본과 같거나 source/ 폴더 안이면 멈춘다 (절대 규칙 1)."""
    out_r = Path(out_path).resolve()
    src_r = Path(source).resolve()
    if out_r == src_r or src_r.parent in out_r.parents or out_r.parent == src_r.parent:
        raise PermissionError(f"원본 위치에 쓰려고 했습니다: {out_r}")
