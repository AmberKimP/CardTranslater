"""명령줄 진입점.

    python -m app.cli erase --project projects/sample --pages 1-3
    python -m app.cli compose --project projects/sample --erase-run test_runs/<시각>-erase --pages 1-3

결과는 test_runs/<YYYYMMDD-HHMMSS>-erase/ , -compose/ 에 쓴다. 원본 PDF는 읽기만 한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np

from app import report
from app.erase import erase_page, find_background_image, load_image_rgb, render_on_image_grid
from app.project import ensure_not_source, load_project, parse_page_spec, sha256_file

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

    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.out_root).resolve() / f"{stamp}-erase"
    run_dir.mkdir(parents=True, exist_ok=False)
    ensure_not_source(run_dir / "x", src_path)

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
            "output_pdf": "erased.pdf",
            "output_page_index": i,
            **r.meta,
        }
        page_metas.append(meta)
        results.append(r)

    out_pdf = run_dir / "erased.pdf"
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
            safe_r = {"태그": "tag", "이름": "name", "본문": "body"}.get(pt["region"], "r")
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
            safe = {"태그": "tag", "이름": "name", "본문": "body"}.get(m["name"], f"r{page_metas[i]['regions'].index(m)}")
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
        "output_pdf": str(out_pdf),
        "page_boxes": box_checks,
        "meta_files": [str(mdir / f"page-{p:03d}.json") for p in pages],
    }
    (run_dir / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "source.sha256").write_text(
        f"before {sha_before}  {src_path.name}\nafter  {sha_after}  {src_path.name}\n", encoding="utf-8"
    )
    print(f"[erase] 결과: {run_dir}")
    if sha_before != sha_after:
        print("[erase] 경고: 원본 PDF 해시가 바뀌었습니다!", file=sys.stderr)
        return 2
    for meta in page_metas:
        for w in meta["warnings"]:
            print(f"  p{meta['source_page']}: {w}")
    return 0


def cmd_compose(args: argparse.Namespace) -> int:
    from app.typeset_compose import run_compose

    run_dir, code = run_compose(Path(args.project), Path(args.erase_run), args.pages, Path(args.out_root))
    rep = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    for status, keys in rep["status_summary"].items():
        print(f"  {status}: {len(keys)}  {', '.join(keys)}")
    print(f"[compose] 결과: {run_dir}")
    if code:
        print("[compose] 경고: 원본 또는 지운 PDF 해시가 바뀌었습니다!", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("erase", help="영역 글자 지우기 (레드랙션 + 배경 이미지 정리 + 태그 라벨 다시 그리기)")
    e.add_argument("--project", required=True)
    e.add_argument("--pages", required=True, help="예: 1-3 또는 1,5,12-14")
    e.add_argument("--out-root", default="test_runs")
    e.set_defaults(func=cmd_erase)
    c = sub.add_parser("compose", help="지운 PDF 위에 한글 번역 합성")
    c.add_argument("--project", required=True)
    c.add_argument("--erase-run", required=True, help="지우기 결과 폴더 (erased.pdf + meta/)")
    c.add_argument("--pages", required=True, help="예: 1-3 또는 1,5,12-14")
    c.add_argument("--out-root", default="test_runs")
    c.set_defaults(func=cmd_compose)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
