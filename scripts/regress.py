"""실제 카드 회귀 비교: 시트 자르기 · 글자 지우기 · 한글 넣기 결과를 기준(baseline)과 비교한다.

    .venv/Scripts/python scripts/regress.py save            # 지금 결과를 기준으로 저장
    .venv/Scripts/python scripts/regress.py check           # 기준과 비교 -> test_runs/regress/runs/<시각>/report.html
    .venv/Scripts/python scripts/regress.py check --only 이세길 --pages 1-3
    .venv/Scripts/python scripts/regress.py list            # 비교할 대상 목록

비교 대상(case)은 자동으로 찾는다.
- pipeline: projects/<프로젝트>/groups/<그룹>/files/<파일> 중 원문(extract/)과 원본 PDF가 있는 파일.
  service.test_run으로 시험 폴더에서 지우기 -> 한글 넣기 (Claude는 부르지 않음, 프로젝트 폴더는 읽기만).
- sheet: 이미지 배열로 넣은 파일(file.json의 from_images)의 원본 이미지 + scripts/regress_cases.json의 이미지.
  images.cut_sheet로 다시 잘라 카드 상자를 비교한다.

결과·기준은 test_runs/regress/ 아래에 둔다 (카드 그림이라 git에 넣지 않는다: .gitignore의 test_runs/).
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "test_runs" / "regress"
CASES_FILE = ROOT / "scripts" / "regress_cases.json"
RENDER_DPI = 72  # 비교용 그림 해상도 (카드 한 장 약 180x250px: 빠르고 글자 자리·크기 변화는 보인다)
PIXEL_TOL = 32  # 한 화소의 RGB 차이가 이보다 크면 바뀐 화소
CHANGED_RATIO = 0.002  # 바뀐 화소가 이 비율을 넘으면 그 쪽이 바뀐 것
BOX_TOL = 3  # 카드 상자 꼭짓점이 이 px보다 많이 움직이면 바뀐 것


@dataclass
class Case:
    name: str  # "pipeline:프로젝트/그룹/파일" · "sheet:…/이미지"
    kind: str  # pipeline | sheet
    path: Path  # 파일 폴더(pipeline) 또는 이미지(sheet)
    cols: int = 1
    rows: int = 1
    frame: int = 0

    @property
    def folder(self) -> str:
        """기준·결과 폴더 이름 (경로 구분자·쓸 수 없는 글자를 바꾼다)."""
        return re.sub(r'[\\/:*?"<>|]+', "__", self.name)


@dataclass
class Outcome:
    case: Case
    state: str  # same | changed | input(입력 데이터도 바뀜) | new | failed
    details: list = field(default_factory=list)  # [{"item", "state", "ratio", "base", "now", "diff"}]
    error: str = ""


# ---------------------------------------------------------------- 대상 찾기
def project_roots(root: Path = ROOT) -> list[Path]:
    """저장소의 projects/, 앱 데이터 폴더(설정)의 projects/, 설치한 앱의 기본 데이터 폴더
    (문서/CardTranslater/projects). 같은 폴더는 한 번."""
    cands = [root / "projects"]
    try:
        from app import settings

        cands.append(settings.data_dir() / "projects")
    except Exception:  # noqa: BLE001 - 설정을 못 읽으면 나머지만
        pass
    cands.append(Path.home() / "Documents" / "CardTranslater" / "projects")
    out: list[Path] = []
    for p in cands:
        if p.is_dir() and all(p.resolve() != q.resolve() for q in out):
            out.append(p)
    return out


def discover(projects_roots: list[Path] | None = None, cases_file: Path | None = CASES_FILE,
             root: Path = ROOT) -> list[Case]:
    cases: list[Case] = []
    for projects_root in (project_roots(root) if projects_roots is None else [Path(p) for p in projects_roots]):
        cases += _discover_projects(projects_root)
    cases += _sheet_cases(cases_file, root)
    seen: dict[str, int] = {}
    for c in cases:  # 두 폴더에 같은 이름이 있으면 뒤의 것에 (2)
        seen[c.name] = seen.get(c.name, 0) + 1
        if seen[c.name] > 1:
            c.name += f" ({seen[c.name]})"
    return cases


def _discover_projects(projects_root: Path) -> list[Case]:
    cases: list[Case] = []
    for fd in sorted(projects_root.glob("*/groups/*/files/*")):
        if not fd.is_dir():
            continue
        rel = fd.relative_to(projects_root)
        project, group, file = rel.parts[0], rel.parts[2], rel.parts[4]
        if (fd / "extract" / "pages").is_dir() and list((fd / "source").glob("*.pdf")):
            cases.append(Case(f"pipeline:{project}/{group}/{file}", "pipeline", fd))
        info = _read_json(fd / "file.json").get("from_images") or {}
        for i, name in enumerate(info.get("files", [])):
            img = fd / "source" / "images" / name
            if img.exists():
                cases.append(Case(f"sheet:{project}/{file}/{name}", "sheet", img,
                                  int(info.get("cols", 1)), int(info.get("rows", 1))))
    return cases


def _sheet_cases(cases_file: Path | None, root: Path) -> list[Case]:
    cases: list[Case] = []
    for spec in (_read_json(cases_file).get("sheets", []) if cases_file else []):
        for f in sorted(glob.glob(str(root / spec["glob"]))):
            p = Path(f)
            cases.append(Case(f"sheet:{spec.get('label', p.parent.name)}/{p.name}", "sheet", p,
                              int(spec.get("cols", 1)), int(spec.get("rows", 1)), int(spec.get("frame", 0))))
    return cases


def _read_json(p: Path | None) -> dict:
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


# ---------------------------------------------------------------- 실행
def run_case(case: Case, out_dir: Path, pages: str | None = None) -> dict:
    """case를 실행해 out_dir에 비교할 결과를 쓴다. 돌려주는 값은 summary.json 내용."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    summary = run_sheet(case, out_dir) if case.kind == "sheet" else run_pipeline(case, out_dir, pages)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def run_sheet(case: Case, out_dir: Path) -> dict:
    from app import images

    cut = images.cut_sheet(case.path, case.cols, case.rows, frame=case.frame)
    boxes = [list(map(int, b)) for b in cut.boxes]
    _sheet_overlay(case.path, boxes, out_dir / "boxes.png", frame=case.frame)
    digest = hashlib.sha256(Path(case.path).read_bytes()).hexdigest()[:16]
    inputs = {f"image/{Path(case.path).name}": digest, "layout": f"{case.cols}x{case.rows}#{case.frame}"}
    return {"kind": "sheet", "cols": case.cols, "rows": case.rows, "boxes": boxes, "inputs": inputs,
            "size": list(cut.size), "warnings": [str(w) for w in getattr(cut, "warnings", []) or []]}


def _sheet_overlay(path: Path, boxes: list, out: Path, frame: int = 0, max_px: int = 900) -> None:
    from PIL import ImageDraw

    from app import images

    im = images.load_images(path)[frame]
    s = min(1.0, max_px / max(im.size))
    im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))))
    d = ImageDraw.Draw(im)
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        d.rectangle([x0 * s, y0 * s, x1 * s, y1 * s], outline=(255, 0, 80), width=3)
        d.text((x0 * s + 6, y0 * s + 4), str(i + 1), fill=(255, 0, 80))
    im.save(out)


def run_pipeline(case: Case, out_dir: Path, pages: str | None = None) -> dict:
    import pymupdf

    from app import service, workspace

    fd = case.path
    page_list = workspace.included_pages(fd)
    if pages:
        page_list = [p for p in service.parse_test_pages(fd, pages)]
    inputs = input_fingerprint(fd)
    before = snapshot(fd)
    with tempfile.TemporaryDirectory(prefix="regress-") as tmp:
        r = service.test_run(fd, workspace.pages_spec(page_list), out_root=Path(tmp))
        if r.state != "done":
            raise RuntimeError(r.reason or "시험 실행 실패")
        for kind, folder, pdf in (("erased", r.erase_dir, "erased.pdf"), ("composed", r.run_dir, "composed.pdf")):
            with pymupdf.open(Path(folder) / pdf) as doc:
                for i, page in enumerate(doc):
                    no = page_list[i] if i < len(page_list) and len(doc) == len(page_list) else i + 1
                    page.get_pixmap(dpi=RENDER_DPI).save(out_dir / f"{kind}-p{no:03d}.png")
        status = {k: list(v) if isinstance(v, (list, tuple)) else v for k, v in (r.status_summary or {}).items()}
    changed = [str(p) for p in diff_snapshot(before, snapshot(fd))]
    if changed:  # 시험 실행은 프로젝트를 바꾸면 안 된다 (앱에서 같은 때 고쳤을 수도 있다)
        raise RuntimeError("실행하는 동안 프로젝트 파일이 바뀌었습니다 (앱에서 고쳤거나 시험 실행이 바꿈): "
                           + ", ".join(changed[:5]))
    return {"kind": "pipeline", "pages": page_list, "status_summary": status, "inputs": inputs}


_OUTPUT_DIRS = {"cleared", "translated", "work", "compare"}  # 결과·작업 폴더: 입력이 아니다


def input_fingerprint(fd: Path) -> dict[str, str]:
    """지우기·한글 넣기에 들어가는 입력 파일 -> sha256 앞 16자.
    파일 폴더(결과 폴더 제외), 그룹의 regions.json, 게임의 용어집·아이콘 사전, 프로젝트 번역 캐시.
    기준과 지금이 다르면 결과가 바뀐 까닭이 코드가 아니라 데이터일 수 있다."""
    fd = Path(fd)
    files: dict[str, Path] = {}
    for p in fd.rglob("*"):
        rel = p.relative_to(fd)
        if p.is_file() and not (set(rel.parts[:-1]) & _OUTPUT_DIRS) and not rel.parts[0].startswith("."):
            files[f"file/{rel.as_posix()}"] = p
    group, project = fd.parents[1], fd.parents[3]
    for name in ("regions.json", "group.json"):
        files[f"group/{name}"] = group / name
    files["project/translate/cache.json"] = project / "translate" / "cache.json"
    try:
        from app import workspace

        game = workspace.load_project_info(project).game_dir
        files["game/glossary.json"] = game / "glossary.json"
        for p in (game / "icons").glob("*"):
            if p.is_file():
                files[f"game/icons/{p.name}"] = p
    except Exception:  # noqa: BLE001 - 게임 폴더를 못 찾으면 파일 폴더만
        pass
    return {k: hashlib.sha256(p.read_bytes()).hexdigest()[:16] for k, p in sorted(files.items()) if p.is_file()}


def snapshot(folder: Path) -> dict[str, tuple[int, str]]:
    """폴더 안 파일 -> (크기, sha256). 시험 실행이 프로젝트를 건드리지 않았는지 확인할 때 쓴다."""
    out = {}
    for p in Path(folder).rglob("*"):
        if p.is_file():
            out[str(p.relative_to(folder))] = (p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def diff_snapshot(a: dict, b: dict) -> list[str]:
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


# ---------------------------------------------------------------- 비교
def compare_images(a: Path, b: Path, diff_out: Path | None = None, tol: int = PIXEL_TOL) -> float:
    """바뀐 화소 비율 (0~1). 크기가 다르면 1. diff_out을 주면 바뀐 곳을 빨갛게 칠한 그림을 쓴다."""
    import numpy as np
    from PIL import Image

    with Image.open(a) as ia, Image.open(b) as ib:
        x = np.asarray(ia.convert("RGB"), dtype=np.int16)
        y = np.asarray(ib.convert("RGB"), dtype=np.int16)
    if x.shape != y.shape:
        if diff_out:
            Image.fromarray(y.astype(np.uint8)).save(diff_out)
        return 1.0
    mask = np.abs(x - y).max(axis=2) > tol
    if diff_out is not None:
        out = (y * 0.35 + 165).astype(np.uint8)  # 흐린 새 결과 위에
        out[mask] = (230, 0, 60)  # 바뀐 화소는 빨강
        Image.fromarray(out).save(diff_out)
    return float(mask.mean())


def compare_boxes(a: list, b: list) -> int:
    """카드 상자 목록 비교 -> 가장 많이 움직인 꼭짓점 거리(px). 개수가 다르면 큰 수."""
    if len(a) != len(b):
        return 10 ** 6
    return max((abs(int(u) - int(v)) for ba, bb in zip(a, b) for u, v in zip(ba, bb)), default=0)


def compare_case(case: Case, base: Path, now: Path) -> Outcome:
    if not (base / "summary.json").exists():
        return Outcome(case, "new", [{"item": "기준 없음", "state": "new"}])
    sb, sn = _read_json(base / "summary.json"), _read_json(now / "summary.json")
    details = []
    if case.kind == "sheet":
        ib, in_ = sb.get("inputs"), sn.get("inputs")
        if ib is not None and in_ is not None and ib != in_:
            details.append({"item": "입력(시트 이미지·배열)이 기준 때와 다름 — 아래 차이는 코드가 아니라 데이터 때문일 수 있다",
                            "state": "input"})
        moved = compare_boxes(sb.get("boxes", []), sn.get("boxes", []))
        st = "same" if moved <= BOX_TOL else "changed"
        note = "상자 수 다름" if moved >= 10 ** 6 else f"최대 {moved}px 움직임"
        diff = now / "boxes-diff.png"
        if st == "changed":
            compare_images(base / "boxes.png", now / "boxes.png", diff)
        details.append({"item": f"카드 상자 {len(sn.get('boxes', []))}개 ({note})", "state": st,
                        "base": base / "boxes.png", "now": now / "boxes.png",
                        "diff": diff if st == "changed" else None})
    else:
        if sb.get("pages") != sn.get("pages"):
            details.append({"item": f"처리한 쪽이 다름: {sb.get('pages')} -> {sn.get('pages')}", "state": "changed"})
        names = sorted({p.name for p in base.glob("*-p*.png")} | {p.name for p in now.glob("*-p*.png")})
        for name in names:
            b, n = base / name, now / name
            if not b.exists() or not n.exists():
                details.append({"item": name, "state": "changed", "ratio": 1.0,
                                "base": b if b.exists() else None, "now": n if n.exists() else None})
                continue
            diff = now / name.replace(".png", "-diff.png")
            ratio = compare_images(b, n, diff)
            st = "changed" if ratio > CHANGED_RATIO else "same"
            if st == "same":
                diff.unlink(missing_ok=True)
            details.append({"item": name, "state": st, "ratio": ratio, "base": b, "now": n,
                            "diff": diff if st == "changed" else None})
        ib, in_ = sb.get("inputs"), sn.get("inputs")
        if ib is not None and in_ is not None and ib != in_:
            moved = diff_snapshot(ib, in_)
            details.insert(0, {"item": f"입력 데이터가 기준 때와 다름 ({len(moved)}개 파일: "
                                       + ", ".join(moved[:6]) + (" …" if len(moved) > 6 else "")
                                       + ") — 아래 차이는 코드가 아니라 데이터 때문일 수 있다", "state": "input"})
        ssb, ssn = sb.get("status_summary", {}), sn.get("status_summary", {})
        if ssb != ssn:
            details.append({"item": f"한글 넣기 문제 목록이 바뀜: {_counts(ssb)} -> {_counts(ssn)}", "state": "changed"})
    state = "same"
    if any(d["state"] == "changed" for d in details):
        state = "input" if any(d["state"] == "input" for d in details) else "changed"
    return Outcome(case, state, details)


def _counts(summary: dict) -> dict:
    return {k: (len(v) if isinstance(v, list) else v) for k, v in summary.items() if v}


# ---------------------------------------------------------------- 보고서
_LABEL = {"same": "같음", "changed": "바뀜", "input": "바뀜 (입력 데이터도 바뀜)", "new": "기준 없음", "failed": "실패"}


def write_report(outcomes: list[Outcome], run_dir: Path) -> Path:
    def rel(p):
        return html.escape(os.path.relpath(p, run_dir).replace("\\", "/")) if p else ""

    rows, body = [], []
    for o in outcomes:
        rows.append(f"<tr class='{o.state}'><td>{html.escape(o.case.name)}</td><td>{_LABEL[o.state]}</td>"
                    f"<td>{html.escape(o.error)}</td></tr>")
        if o.state not in ("changed", "input", "failed"):
            continue
        body.append(f"<h2 class='{o.state}'>{html.escape(o.case.name)} — {_LABEL[o.state]}</h2>")
        if o.error:
            body.append(f"<pre>{html.escape(o.error)}</pre>")
        for d in o.details:
            if d["state"] == "same":
                continue
            ratio = f" · 바뀐 화소 {d['ratio'] * 100:.2f}%" if "ratio" in d else ""
            body.append(f"<h3>{html.escape(d['item'])}{ratio}</h3><div class='row'>")
            for title, key in (("기준", "base"), ("지금", "now"), ("차이", "diff")):
                if d.get(key):
                    body.append(f"<figure><figcaption>{title}</figcaption><img src='{rel(d[key])}'></figure>")
            body.append("</div>")
    n = {k: sum(o.state == k for o in outcomes) for k in _LABEL}
    page = f"""<!DOCTYPE html><html lang="ko"><meta charset="utf-8"><title>회귀 비교</title>
<style>body{{font-family:sans-serif;margin:24px}}table{{border-collapse:collapse}}td{{border:1px solid #ccc;padding:4px 8px}}
tr.changed td,h2.changed{{color:#b45309}}tr.input td,h2.input{{color:#7c3aed}}tr.failed td,h2.failed{{color:#b91c1c}}tr.new td{{color:#2563eb}}
.row{{display:flex;gap:12px;flex-wrap:wrap}}figure{{margin:0}}img{{max-width:420px;border:1px solid #ddd}}</style>
<h1>회귀 비교 ({time.strftime('%Y-%m-%d %H:%M')})</h1>
<p>같음 {n['same']} · 바뀜 {n['changed']} · 입력도 바뀜 {n['input']} · 기준 없음 {n['new']} · 실패 {n['failed']}.
바뀐 것이 의도한 변화면 <code>scripts/regress.py save</code>로 기준을 새로 저장한다.</p>
<table>{''.join(rows)}</table>{''.join(body)}</html>"""
    out = run_dir / "report.html"
    out.write_text(page, encoding="utf-8")
    (run_dir / "report.json").write_text(json.dumps(
        [{"case": o.case.name, "state": o.state, "error": o.error,
          "changed": [d["item"] for d in o.details if d["state"] != "same"]} for o in outcomes],
        ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ---------------------------------------------------------------- 명령
def save(cases: list[Case], out: Path = DEFAULT_OUT, pages: str | None = None, log=print) -> list[Outcome]:
    res = []
    for c in cases:
        log(f"[save] {c.name}")
        final = out / "baseline" / c.folder
        tmp = final.with_name(final.name + ".new")
        try:
            run_case(c, tmp, pages)  # 다 만든 뒤에만 바꿔 넣는다: 실패하면 예전 기준이 그대로 남는다
            if final.exists():
                shutil.rmtree(final)
            tmp.rename(final)
            res.append(Outcome(c, "same"))
        except Exception as e:  # noqa: BLE001 - 한 대상이 실패해도 나머지는 저장
            shutil.rmtree(tmp, ignore_errors=True)
            res.append(Outcome(c, "failed", error=f"{type(e).__name__}: {e}"))
            log(f"  실패: {e}")
    return res


def check(cases: list[Case], out: Path = DEFAULT_OUT, pages: str | None = None, log=print
          ) -> tuple[list[Outcome], Path]:
    run_dir = out / "runs" / time.strftime("%Y%m%d-%H%M%S")
    n = 2
    while run_dir.exists():
        run_dir = out / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}"
        n += 1
    res = []
    for c in cases:
        log(f"[check] {c.name}")
        now = run_dir / c.folder
        try:
            run_case(c, now, pages)
            o = compare_case(c, out / "baseline" / c.folder, now)
        except Exception as e:  # noqa: BLE001
            o = Outcome(c, "failed", error=f"{type(e).__name__}: {e}")
        log(f"  {_LABEL[o.state]}" + (f": {o.error}" if o.error else ""))
        res.append(o)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_report(res, run_dir)
    return res, run_dir


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 콘솔(cp949)에서도 한글 이름이 깨지지 않게
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="실제 카드 회귀 비교")
    ap.add_argument("command", choices=["save", "check", "list"])
    ap.add_argument("--only", action="append", default=[], help="이름에 이 글자가 든 대상만 (여러 번 가능)")
    ap.add_argument("--kind", choices=["pipeline", "sheet"], help="이 종류만")
    ap.add_argument("--pages", help="pipeline 대상의 쪽 (예: 1-3). 기준과 같은 쪽으로 비교해야 한다")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)
    cases = [c for c in discover() if (not a.only or any(s in c.name for s in a.only))
             and (not a.kind or c.kind == a.kind)]
    if a.command == "list":
        for c in cases:
            print(c.name)
        return 0
    if not cases:
        print("비교할 대상이 없습니다 (projects/ 또는 scripts/regress_cases.json)")
        return 2
    if a.command == "save":
        res = save(cases, a.out, a.pages)
        bad = [o for o in res if o.state == "failed"]
        print(f"기준 저장: {len(res) - len(bad)}개 · 실패 {len(bad)}개 -> {a.out / 'baseline'}")
        return 1 if bad else 0
    res, run_dir = check(cases, a.out, a.pages)
    bad = [o for o in res if o.state != "same"]
    print(f"같음 {len(res) - len(bad)} · 다름/실패 {len(bad)} -> {run_dir / 'report.html'}")
    return 1 if bad else 0


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.exit(main())
