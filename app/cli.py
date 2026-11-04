"""명령줄 진입점.

7단계 글자 지우기 -> projects/<p>/cleared/  (한 번만)
    python -m app.cli erase --project projects/sample --pages 1-30
번역 검수 엑셀 만들기 -> projects/<p>/번역_검수.xlsx
    python -m app.cli export-excel --project projects/sample
8단계 한글 넣기 -> projects/<p>/translated/  (지우기를 다시 하지 않음. 엑셀을 고칠 때마다 반복)
    python -m app.cli compose --project projects/sample --excel projects/sample/번역_검수.xlsx

--test를 붙이면 프로젝트 폴더 대신 test_runs/<YYYYMMDD-HHMMSS>-erase|compose/ 에 쓴다.
원본 PDF는 읽기만 한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np

from app import report
from app.erase import erase_page, find_background_image, load_image_rgb, render_on_image_grid
from app.project import ensure_not_source, load_project, parse_page_spec, sha256_file
from app.typeset_compose import safe_name

SCHEMA = "cardtranslater.erase-page/2"  # 2: 원본 이미지 유지 + patches


def _boxes(page) -> dict:
    doc = page.parent
    out = {"mediabox": [round(v, 3) for v in page.mediabox], "cropbox": [round(v, 3) for v in page.cropbox]}
    for key in ("BleedBox", "TrimBox", "ArtBox"):
        out[key] = doc.xref_get_key(page.xref, key)[1]
    return out


def cmd_erase(args: argparse.Namespace) -> int:
    import pymupdf

    project = load_project(Path(args.project))
    src_path = project.source_pdf
    sha_before = sha256_file(src_path)

    final_dir, run_dir = _output_dirs(args, project.root, "cleared", "erase")
    run_dir.mkdir(parents=True, exist_ok=False)
    ensure_not_source(run_dir / "x", src_path)
    pdf_name = "erased.pdf" if args.test else "cleared.pdf"

    src_bytes = src_path.read_bytes()  # 원본은 메모리로만 읽는다
    src_doc = pymupdf.open(stream=src_bytes, filetype="pdf")
    pages = parse_page_spec(args.pages, src_doc.page_count)

    out_doc = pymupdf.open(stream=src_bytes, filetype="pdf")
    out_doc.select([p - 1 for p in pages])

    page_metas = []
    results = []
    for i, pno in enumerate(pages):
        print(f"[erase] page {pno} ...", flush=True)
        r = erase_page(out_doc[i], project.template, project.samples.get(pno), seed=pno)
        meta = {
            "schema": SCHEMA,
            "source_pdf": str(src_path),
            "source_page": pno,
            "output_pdf": pdf_name,
            "output_page_index": i,
            **r.meta,
        }
        page_metas.append(meta)
        results.append(r)

    out_pdf = run_dir / pdf_name
    ensure_not_source(out_pdf, src_path)
    out_doc.save(out_pdf, garbage=3, deflate=True)
    out_doc.close()

    # ---- 검증 + 비교 이미지
    check = pymupdf.open(out_pdf)
    box_checks = []
    for i, pno in enumerate(pages):
        sp, op = src_doc[pno - 1], check[i]
        sb, ob = _boxes(sp), _boxes(op)
        box_checks.append({"source_page": pno, "same": sb == ob, "source": sb, "output": ob})

        bg = find_background_image(sp)
        raw = load_image_rgb(src_doc, bg.xref)
        before, _ = render_on_image_grid(sp, bg, raw)
        bg_o = find_background_image(op)
        raw_o = load_image_rgb(check, bg_o.xref)
        after, _ = render_on_image_grid(op, bg_o, raw_o)
        if raw_o.shape != raw.shape:
            page_metas[i]["warnings"].append(f"이미지 크기가 바뀜 {raw.shape} -> {raw_o.shape}")

        # 원본 배경 JPEG 스트림이 바이트 그대로인지
        s_raw = src_doc.xref_stream_raw(bg.xref)
        o_raw = check.xref_stream_raw(bg_o.xref)
        bgm = page_metas[i]["background_image"]
        bgm["stream_sha256_source"] = hashlib.sha256(s_raw).hexdigest()
        bgm["stream_sha256_output"] = hashlib.sha256(o_raw).hexdigest()
        bgm["stream_untouched"] = s_raw == o_raw
        bgm["filter"] = check.xref_get_key(bg_o.xref, "Filter")[1]
        bgm["output_xref"] = bg_o.xref

        # 패치: 저장 뒤 xref·압축 방식 다시 확인 (garbage 정리로 번호가 바뀔 수 있음)
        infos = [x for x in op.get_image_info(xrefs=True) if x.get("xref", 0) not in (0, bg_o.xref)]
        outside = np.ones(raw.shape[:2], bool)
        for pt in page_metas[i]["patches"]:
            match = min(infos, key=lambda x: sum(abs(a - b) for a, b in zip(x["bbox"], pt["bbox_pt"])))
            pt["xref"] = match["xref"]
            pt["filter"] = check.xref_get_key(match["xref"], "Filter")[1]
            pt["stream_bytes"] = len(check.xref_stream_raw(match["xref"]))
            x0, y0, x1, y1 = pt["bbox_px"]
            outside[y0:y1, x0:x1] = False
        diff = np.abs(after.astype(int) - before.astype(int)).max(axis=2)
        page_metas[i]["verify"] = {
            "outside_patches_changed_px_at_image_grid": int((diff[outside] > 0).sum()),
            "outside_patches_max_diff": int(diff[outside].max()) if outside.any() else 0,
        }

        # 쪽당 용량 (한 쪽짜리 PDF로 따로 저장했을 때)
        def one_page_size(d, idx):
            t = pymupdf.open()
            t.insert_pdf(d, from_page=idx, to_page=idx)
            n = len(t.tobytes(garbage=3, deflate=True))
            t.close()
            return n

        page_metas[i]["verify"]["single_page_pdf_bytes"] = {
            "source": one_page_size(src_doc, pno - 1),
            "output": one_page_size(check, i),
        }

        # 다른 해상도(600dpi)에서 패치 경계 확인용 확대
        for pt in page_metas[i]["patches"]:
            r = pymupdf.Rect(pt["bbox_pt"])
            clip = pymupdf.Rect(r.x0 - 4, r.y0 - 4, min(r.x0 + 60, r.x1 + 4), r.y1 + 4) & sp.rect
            a = sp.get_pixmap(dpi=600, clip=clip, alpha=False)
            b = op.get_pixmap(dpi=600, clip=clip, alpha=False)
            A = np.frombuffer(a.samples, np.uint8).reshape(a.h, a.w, 3)
            B = np.frombuffer(b.samples, np.uint8).reshape(b.h, b.w, 3)
            safe_r = safe_name(pt["region"], [m["name"] for m in page_metas[i]["regions"]].index(pt["region"]))
            pth = run_dir / "compare" / f"p{pno:03d}_seam600_{safe_r}.png"
            report.save(report.stacked([("ORIGINAL 600dpi (left edge of patch)", A), ("RESULT 600dpi", B)], scale=1), pth)
            pt["seam_png_600dpi"] = str(pth)

        pdir = run_dir / "compare"
        tag = f"p{pno:03d}"
        report.save(report.side_by_side(before, after), pdir / f"{tag}_card.png")
        res = results[i]
        cx0, cy0, cx1, cy1 = res.crop_px
        overlay_full = before.copy()
        overlay_full[cy0:cy1, cx0:cx1] = report.mask_overlay(
            before[cy0:cy1, cx0:cx1], res.clean.flat_mask, res.clean.inpaint_mask
        )
        for m in page_metas[i]["regions"]:
            x0, y0, x1, y1 = m["work_rect_px"]
            pad = 14
            x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
            x1, y1 = min(raw.shape[1], x1 + pad), min(raw.shape[0], y1 + pad)
            img = report.stacked(
                [
                    ("ORIGINAL (rendered)", before[y0:y1, x0:x1]),
                    ("RESULT (rendered, labels redrawn)", after[y0:y1, x0:x1]),
                    ("RESULT image layers only (original + patch, no vector)", results[i].cleaned[y0:y1, x0:x1]),
                    ("MASK red=flat/gradient fill, blue=inpaint", overlay_full[y0:y1, x0:x1]),
                ],
                scale=2,
            )
            safe = safe_name(m["name"], page_metas[i]["regions"].index(m))
            path = report.save(img, pdir / f"{tag}_{safe}.png")
            m["compare_png"] = str(path)
        page_metas[i]["compare_card_png"] = str(pdir / f"{tag}_card.png")
    check.close()
    src_doc.close()

    sha_after = sha256_file(src_path)
    mdir = run_dir / "meta"
    mdir.mkdir()
    for meta in page_metas:
        (mdir / f"page-{meta['source_page']:03d}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    run = {
        "command": "erase",
        "project": str(project.root),
        "pages": pages,
        "source_pdf": str(src_path),
        "source_sha256_before": sha_before,
        "source_sha256_after": sha_after,
        "source_unchanged": sha_before == sha_after,
        "output_pdf": str(final_dir / pdf_name),
        "page_boxes": box_checks,
        "meta_files": [str(mdir / f"page-{p:03d}.json") for p in pages],
    }
    (run_dir / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "source.sha256").write_text(
        f"before {sha_before}  {src_path.name}\nafter  {sha_after}  {src_path.name}\n", encoding="utf-8"
    )
    if sha_before != sha_after:
        print(f"[erase] 경고: 원본 PDF 해시가 바뀌었습니다! 결과는 {run_dir}에 남겨 둠", file=sys.stderr)
        return 2
    _commit(run_dir, final_dir)
    print(f"[erase] 결과: {final_dir}")
    for meta in page_metas:
        for w in meta["warnings"]:
            print(f"  p{meta['source_page']}: {w}")
    return 0


def _output_dirs(args, project_root: Path, folder: str, kind: str) -> tuple[Path, Path]:
    """(최종 폴더, 작업 폴더). 프로젝트 모드는 임시 폴더에 다 쓴 뒤 _commit으로 바꿔 넣는다."""
    if args.test:
        d = Path(args.out_root).resolve() / f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}"
        return d, d
    final = project_root / folder
    tmp = project_root / f".{folder}-{time.strftime('%Y%m%d-%H%M%S')}"
    return final, tmp


def _commit(tmp: Path, final: Path) -> None:
    """다 쓴 작업 폴더를 최종 폴더로 바꿔 넣는다 (예전 결과는 지움)."""
    if tmp == final:
        return
    if final.exists():
        shutil.rmtree(final)
    tmp.rename(final)


def cmd_compose(args: argparse.Namespace) -> int:
    from app.excel_io import import_translation
    from app.typeset_compose import run_compose

    project = load_project(Path(args.project))
    cleared = Path(args.cleared) if args.cleared else project.root / "cleared"
    if not cleared.exists():
        print(f"[compose] 지운 결과가 없습니다: {cleared}\n  먼저 erase를 실행하세요 (7단계).", file=sys.stderr)
        return 1
    cards, source = None, "samples.json"
    if args.excel:
        imp = import_translation(project, Path(args.excel))
        print(f"[compose] 엑셀 {args.excel}: {imp.rows}행")
        for b in imp.blocking:
            print(f"  [멈춤] {b}", file=sys.stderr)
        if imp.blocking:
            return 1
        for i in imp.issues:
            print(f"  [확인] {i}")
        if imp.issues and not args.yes:
            print("[compose] 위 항목을 확인한 뒤 계속하려면 --yes를 붙여 다시 실행하세요.", file=sys.stderr)
            return 1
        cards, source = imp.cards, str(Path(args.excel).resolve())

    final_dir, run_dir = _output_dirs(args, project.root, "translated", "compose")
    out_name = "composed.pdf" if args.test else "translated.pdf"
    run_dir, code = run_compose(project.root, cleared, args.pages, run_dir, cards=cards,
                                translation_source=source, out_name=out_name)
    rep = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    for status, keys in rep["status_summary"].items():
        print(f"  {status}: {len(keys)}  {', '.join(keys)}")
    if code:
        print(f"[compose] 경고: 원본 또는 지운 PDF 해시가 바뀌었습니다! 결과는 {run_dir}에 남겨 둠", file=sys.stderr)
        return code
    _commit(run_dir, final_dir)
    print(f"[compose] 결과: {final_dir}")
    return 0


def cmd_export_excel(args: argparse.Namespace) -> int:
    import pymupdf

    from app.excel_io import export_translation

    project = load_project(Path(args.project))
    out = Path(args.out) if args.out else project.root / "번역_검수.xlsx"
    if out.exists() and not args.force:
        print(f"[export-excel] 이미 있습니다: {out}\n  검수한 내용을 덮어쓰지 않도록 멈춥니다. 덮어쓰려면 --force.",
              file=sys.stderr)
        return 1
    with pymupdf.open(project.source_pdf) as d:
        pages = parse_page_spec(args.pages or f"1-{d.page_count}", d.page_count)
    export_translation(project, out, pages)
    print(f"[export-excel] {out}  ({len(pages)}쪽 x {len(project.template.regions)}영역)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("erase", help="영역 글자 지우기 (레드랙션 + 배경 이미지 정리 + 태그 라벨 다시 그리기)")
    e.add_argument("--project", required=True)
    e.add_argument("--pages", required=True, help="예: 1-3 또는 1,5,12-14")
    e.add_argument("--test", action="store_true", help="프로젝트 cleared/ 대신 test_runs/<시각>-erase/ 에 쓴다")
    e.add_argument("--out-root", default="test_runs")
    e.set_defaults(func=cmd_erase)
    x = sub.add_parser("export-excel", help="번역 검수 엑셀(번역_검수.xlsx) 만들기")
    x.add_argument("--project", required=True)
    x.add_argument("--pages", help="기본: 전체 쪽")
    x.add_argument("--out", help="기본: 프로젝트/번역_검수.xlsx")
    x.add_argument("--force", action="store_true", help="이미 있는 엑셀을 덮어쓴다")
    x.set_defaults(func=cmd_export_excel)
    c = sub.add_parser("compose", help="8단계: 지운 카드(cleared/) 위에 한글 넣기 (지우기는 다시 하지 않음)")
    c.add_argument("--project", required=True)
    c.add_argument("--excel", help="검수한 번역 엑셀. 없으면 samples.json의 한글 초안")
    c.add_argument("--cleared", "--erase-run", dest="cleared",
                   help="지운 결과 폴더. 기본: 프로젝트/cleared/ (테스트 실행 폴더도 가능)")
    c.add_argument("--pages", help="기본: 지운 결과에 있는 쪽 전부")
    c.add_argument("--yes", action="store_true", help="엑셀 확인 항목이 있어도 계속한다")
    c.add_argument("--test", action="store_true", help="프로젝트 translated/ 대신 test_runs/<시각>-compose/ 에 쓴다")
    c.add_argument("--out-root", default="test_runs")
    c.set_defaults(func=cmd_compose)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
