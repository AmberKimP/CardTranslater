"""자동 정렬 (PRD 5.8, F21): 같은 형식 카드가 조금씩 밀려 있거나(스캔·재단) 크기가 다를 때,
기준 카드에서 그린 공통 영역을 카드마다 옮겨 맞춘다.

방법
- 기준 카드와 다른 카드를 작은 회색 그림(긴 변 ALIGN_LONG_PX)으로 그리고, 다른 카드는 기준 그림 크기로 늘린다
  (= 비율 좌표. 쪽 크기가 카드마다 조금 달라도 그대로 맞는다).
- 긴 선(테두리·글 상자 선)만 남긴 ‘구조 그림’을 만든다. 카드마다 다른 그림·글자는 여기서 빠진다.
- 후보 변환을 여럿 만든다: ORB 특징점 + RANSAC 닮음 변환(cv2.estimateAffinePartial2D: 이동·크기·작은 회전),
  위상 상관(cv2.phaseCorrelate: 이동만), 선 분포 맞추기(세로선 분포로 sx·dx, 가로선 분포로 sy·dy), 변환 없음.
  구조 그림을 기준에 맞춰 옮겼을 때 상관이 가장 높은 후보를 고른다 (특징점이 모자라도 다른 후보가 있다).
- 결과는 비율 좌표 변환 x' = x*sx + dx, y' = y*sy + dy. 회전은 재기만 하고 적용하지 않는다(영역은 축 정렬 사각형).
- 점수(score) = 맞춘 뒤 구조 상관(0~1). 점수가 낮거나 이동·크기 변화·회전이 크면 ‘정렬 확인 필요’(check).

저장: 파일 폴더의 align.json
    {"reference": {"file": "<파일 폴더 이름>", "page": n},
     "pages": {"3": {"dx", "dy", "sx", "sy", "rot", "score", "status"}}}
status: ref(기준 카드) / ok / check(정렬 확인 필요) / manual(손으로 맞춤).
영역에 적용하는 계산은 app.regions(align_rect)에 있다 (좌표 변환은 그곳 한 곳에만).
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import cv2
import numpy as np

ALIGN_FILE = "align.json"
ALIGN_LONG_PX = 420  # 맞출 때 그리는 회색 그림의 긴 변 (카드 한 장이면 약 120dpi: 1px ≈ 카드 높이의 0.25%)
ORB_FEATURES = 1500
MIN_MATCHES = 12  # RANSAC에 넘길 최소 짝 수 (모자라면 특징점 후보는 빼고 위상 상관·선 분포로)
RANSAC_PX = 3.0  # RANSAC 허용 오차 (작은 그림 px)
LINE_FRAC = 0.08  # 이 길이(쪽 크기 대비) 넘게 이어진 선만 구조로 본다 (테두리·글 상자 선. 글자·그림 무늬는 빠짐)
STRUCT_BLUR = 1.5  # 구조 그림을 조금 흐리게 (1~2px 어긋남에도 상관이 갑자기 떨어지지 않게)
SEARCH_SHIFT = 0.12  # 선 분포 맞추기에서 찾는 이동 범위 (쪽 크기 대비)
SEARCH_SCALE = 0.10  # 선 분포 맞추기에서 찾는 크기 변화 범위
REFINE_ROUNDS = 40  # 고른 후보를 다듬는 최대 횟수
MIN_GAIN = 0.005  # 변환 없음보다 상관이 이만큼도 나아지지 않으면 옮기지 않는다

# ‘정렬 확인 필요’ 기준 (실측 2026-11-12: 이세길 3×3 스캔 시트 같은 형식 0.70~0.97,
# 기본판 캐릭터 카드 같은 형식 0.62~0.97, 같은 파일의 다른 형식(턴 종료 카드) 0.16~0.41)
MIN_SCORE = 0.50  # 맞춘 뒤 구조 상관이 이보다 낮으면 같은 형식인지 의심
MAX_SHIFT = 0.06  # 카드 크기 대비 이동이 이보다 크면 확인
MAX_SCALE = 0.08  # 크기 변화(1에서 벗어난 정도)가 이보다 크면 확인
MAX_ROT_DEG = 2.0  # 회전이 이보다 크면 확인 (회전은 적용하지 않으므로)

STATUS_REF, STATUS_OK, STATUS_CHECK, STATUS_MANUAL = "ref", "ok", "check", "manual"
IDENTITY = {"dx": 0.0, "dy": 0.0, "sx": 1.0, "sy": 1.0, "rot": 0.0}


# ---------------------------------------------------------------- 그림
def render_gray(page, long_px: int = ALIGN_LONG_PX) -> np.ndarray:
    """PyMuPDF 쪽 -> 회색 그림 (긴 변 long_px)."""
    import pymupdf

    r = page.rect
    zoom = long_px / max(r.width, r.height)
    pm = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    return np.frombuffer(pm.samples, np.uint8).reshape(pm.h, pm.w).copy()


def _line_maps(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """세로로 긴 선(테두리·글 상자 옆선)과 가로로 긴 선의 세기. 그림·글자처럼 짧은 무늬는 지운다."""
    g = cv2.GaussianBlur(gray, (3, 3), 0).astype(np.float32)
    gx = np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3))
    gy = np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
    h, w = gray.shape
    kv, kh = max(3, int(LINE_FRAC * h)), max(3, int(LINE_FRAC * w))
    lv = cv2.morphologyEx(gx, cv2.MORPH_OPEN, np.ones((kv, 1), np.uint8))
    lh = cv2.morphologyEx(gy, cv2.MORPH_OPEN, np.ones((1, kh), np.uint8))
    return lv, lh


def _structure(lv: np.ndarray, lh: np.ndarray) -> np.ndarray:
    s = cv2.GaussianBlur(lv + lh, (0, 0), STRUCT_BLUR)
    return (s / (float(s.max()) or 1.0)).astype(np.float32)


def _ncc(a: np.ndarray, b: np.ndarray, mask: np.ndarray | None = None) -> float:
    if mask is not None:
        a, b = a[mask], b[mask]
    if a.size < 16:
        return 0.0
    a = a - a.mean()
    b = b - b.mean()
    d = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
    return float((a * b).sum()) / d if d > 0 else 0.0


# ---------------------------------------------------------------- 후보 변환
# 모든 행렬은 2×3, 기준 그림 px -> 대상 그림 px. 대상 그림은 기준 그림 크기로 늘려 두므로(비율 좌표와 같음)
# 변환 없음 = 단위 행렬이고, sx = a, dx = tx / 너비.
_EYE = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])


def _score(ref_s: np.ndarray, tgt_s: np.ndarray, m: np.ndarray) -> float:
    """대상 구조 그림을 기준 좌표로 옮겨 기준과 상관 (쪽 밖으로 나간 곳·가장자리 띠는 뺀다)."""
    h, w = ref_s.shape
    warped = cv2.warpAffine(tgt_s, m.astype(np.float32), (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=-1.0)
    mask = warped >= 0
    b = max(2, int(0.02 * min(h, w)))  # 재단선 영향 줄이기
    mask[:b, :] = mask[-b:, :] = False
    mask[:, :b] = mask[:, -b:] = False
    if mask.mean() < 0.6:  # 너무 많이 밀려 겹치는 곳이 적으면 믿지 않는다
        return 0.0
    return _ncc(ref_s, warped, mask)


def _orb_m(ref: np.ndarray, tgt: np.ndarray) -> np.ndarray | None:
    """ORB 특징점 + RANSAC 닮음 변환(이동·같은 비율 크기·회전). 짝이 모자라면 None."""
    orb = cv2.ORB_create(ORB_FEATURES)
    k1, d1 = orb.detectAndCompute(ref, None)
    k2, d2 = orb.detectAndCompute(tgt, None)
    if d1 is None or d2 is None or len(k1) < MIN_MATCHES or len(k2) < MIN_MATCHES:
        return None
    good = [p[0] for p in cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(d1, d2, k=2)
            if len(p) == 2 and p[0].distance < 0.8 * p[1].distance]
    if len(good) < MIN_MATCHES:
        return None
    src = np.float32([k1[g.queryIdx].pt for g in good]).reshape(-1, 1, 2)
    dst = np.float32([k2[g.trainIdx].pt for g in good]).reshape(-1, 1, 2)
    m, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=RANSAC_PX,
                                         maxIters=4000, confidence=0.995)
    if m is None or inl is None or int(inl.sum()) < MIN_MATCHES // 2:
        return None
    return m.astype(np.float64)


def _phase_m(ref_s: np.ndarray, tgt_s: np.ndarray) -> np.ndarray:
    """위상 상관으로 이동만."""
    h, w = ref_s.shape
    win = cv2.createHanningWindow((w, h), cv2.CV_32F)
    (sx, sy), _resp = cv2.phaseCorrelate(ref_s, tgt_s, win)
    return np.array([[1.0, 0.0, sx], [0.0, 1.0, sy]])


def _profile_fit(pr: np.ndarray, pt: np.ndarray) -> tuple[float, float]:
    """1차원 선 세기 분포 두 개를 맞춘다: pt(x*s + d) ≈ pr(x). (s, d). 거친 격자 뒤 그 둘레를 촘촘히."""
    n = len(pr)
    xs = np.arange(n, dtype=np.float64)
    if not (pr - pr.mean()).any() or not (pt - pt.mean()).any():
        return 1.0, 0.0

    def best(scales, shifts):
        top = (-2.0, 1.0, 0.0)
        for s in scales:
            pos = xs[None, :] * s + shifts[:, None]  # (이동 수, n)
            v = np.interp(pos, xs, pt, left=np.nan, right=np.nan)
            ok = ~np.isnan(v)
            cnt = np.maximum(ok.sum(axis=1), 1)
            v = np.where(ok, v, 0.0)
            ma = (ok * pr[None, :]).sum(axis=1) / cnt
            mv = v.sum(axis=1) / cnt
            va = np.where(ok, pr[None, :] - ma[:, None], 0.0)
            vb = np.where(ok, v - mv[:, None], 0.0)
            den = np.sqrt((va * va).sum(axis=1) * (vb * vb).sum(axis=1))
            c = np.where((den > 0) & (ok.mean(axis=1) >= 0.8), (va * vb).sum(axis=1) / np.where(den > 0, den, 1.0),
                         -2.0)
            i = int(np.argmax(c))
            if c[i] > top[0]:
                top = (float(c[i]), float(s), float(shifts[i]))
        return top

    lim = SEARCH_SHIFT * n
    _c, s0, d0 = best(np.arange(1 - SEARCH_SCALE, 1 + SEARCH_SCALE + 1e-9, 0.004), np.arange(-lim, lim + 0.01, 1.0))
    _c, s1, d1 = best(np.arange(s0 - 0.004, s0 + 0.004 + 1e-9, 0.001), np.arange(d0 - 1.5, d0 + 1.51, 0.25))
    return s1, d1


def _profile_m(ref_lines, tgt_lines) -> np.ndarray:
    """세로선 세기를 열마다 더한 분포로 sx·dx, 가로선 세기를 줄마다 더한 분포로 sy·dy (테두리·글 상자가 정한다)."""
    (rv, rh), (tv, th) = ref_lines, tgt_lines
    sx, dx = _profile_fit(rv.sum(axis=0), tv.sum(axis=0))
    sy, dy = _profile_fit(rh.sum(axis=1), th.sum(axis=1))
    return np.array([[sx, 0.0, dx], [0.0, sy, dy]])


def _refine(ref_s: np.ndarray, tgt_s: np.ndarray, m: np.ndarray, score: float) -> tuple[np.ndarray, float]:
    """고른 후보 둘레를 조금씩 움직여(이동 0.25px·크기 0.001 단위) 구조 상관이 더 높은 곳으로. 회전은 뺀다."""
    p = [float(m[0, 2]), float(m[1, 2]), float(m[0, 0]), float(m[1, 1])]  # dx, dy, sx, sy (px)
    steps = [(0, 0.25), (1, 0.25), (2, 0.001), (3, 0.001)]

    def mat(q):
        return np.array([[q[2], 0.0, q[0]], [0.0, q[3], q[1]]])

    best = _score(ref_s, tgt_s, mat(p))
    for _ in range(REFINE_ROUNDS):
        moved = False
        for i, st in steps:
            for sgn in (1, -1):
                q = list(p)
                q[i] += sgn * st * 2
                s = _score(ref_s, tgt_s, mat(q))
                if s <= best + 1e-5:
                    q[i] = p[i] + sgn * st
                    s = _score(ref_s, tgt_s, mat(q))
                if s > best + 1e-5:
                    p, best, moved = q, s, True
                    break
        if not moved:
            break
    return (mat(p), best) if best >= score else (m, score)


def classify(t: dict) -> str:
    """점수·이동·크기·회전으로 ok / check."""
    if t.get("score", 0.0) < MIN_SCORE:
        return STATUS_CHECK
    # 카드 가운데가 얼마나 옮겨졌나 (크기 변화로 생기는 이동 포함)
    cx = 0.5 * t["sx"] + t["dx"] - 0.5
    cy = 0.5 * t["sy"] + t["dy"] - 0.5
    if max(abs(cx), abs(cy), abs(t["dx"]), abs(t["dy"])) > MAX_SHIFT:
        return STATUS_CHECK
    if max(abs(t["sx"] - 1), abs(t["sy"] - 1)) > MAX_SCALE:
        return STATUS_CHECK
    if abs(t.get("rot", 0.0)) > MAX_ROT_DEG:
        return STATUS_CHECK
    return STATUS_OK


def estimate(ref: np.ndarray, tgt: np.ndarray) -> dict:
    """기준 그림 -> 대상 그림 정렬. {dx, dy, sx, sy, rot, score, status, method}.
    ref·tgt: 회색 그림 (크기가 달라도 된다: 대상은 기준 크기로 늘려 비율 좌표로 맞춘다)."""
    h, w = ref.shape
    if tgt.shape != ref.shape:
        tgt = cv2.resize(tgt, (w, h), interpolation=cv2.INTER_AREA)
    ref_l, tgt_l = _line_maps(ref), _line_maps(tgt)
    ref_s, tgt_s = _structure(*ref_l), _structure(*tgt_l)
    cands: dict[str, np.ndarray] = {"none": _EYE}
    m_orb = _orb_m(ref, tgt)
    if m_orb is not None:
        cands["orb"] = m_orb
    cands["phase"] = _phase_m(ref_s, tgt_s)
    cands["profile"] = _profile_m(ref_l, tgt_l)
    scores = {name: _score(ref_s, tgt_s, m) for name, m in cands.items()}
    name = max(scores, key=lambda k: scores[k])
    if scores[name] - scores["none"] < MIN_GAIN:  # 거의 그대로면 옮기지 않는다
        name = "none"
    m = cands[name]
    if name != "none":
        m, scores[name] = _refine(ref_s, tgt_s, m, scores[name])
    rot = 0.0
    if m_orb is not None and scores["orb"] >= scores[name] - 0.1:  # 회전은 특징점이 잘 맞을 때만 잰다
        rot = math.degrees(math.atan2(m_orb[1, 0], m_orb[0, 0]))
    out = {"dx": round(float(m[0, 2]) / w, 5), "dy": round(float(m[1, 2]) / h, 5),
           "sx": round(float(m[0, 0]), 5), "sy": round(float(m[1, 1]), 5), "rot": round(rot, 2),
           "score": round(max(0.0, scores[name]), 3), "method": name}
    out["status"] = classify(out)
    return out


# ---------------------------------------------------------------- 쪽 단위
def align_pages(ref_pdf: Path, ref_page: int, targets: list[tuple[Path, list[int]]],
                on_page=None, check_cancel=None) -> dict[tuple[str, int], dict]:
    """기준 PDF의 ref_page에 targets[(PDF, 쪽 목록)]의 쪽을 맞춘다. {(PDF 문자열, 쪽): 변환}.
    on_page(i, n, pdf, page): 쪽마다 (진행률). check_cancel(): 쪽마다 (취소면 예외)."""
    import pymupdf

    with pymupdf.open(ref_pdf) as d:
        ref = render_gray(d[ref_page - 1])
    n = sum(len(p) for _f, p in targets)
    out: dict[tuple[str, int], dict] = {}
    i = 0
    for pdf, pages in targets:
        with pymupdf.open(pdf) as d:
            for p in pages:
                if check_cancel:
                    check_cancel()
                if Path(pdf).resolve() == Path(ref_pdf).resolve() and p == ref_page:
                    out[(str(pdf), p)] = {**IDENTITY, "score": 1.0, "status": STATUS_REF}
                else:
                    out[(str(pdf), p)] = estimate(ref, render_gray(d[p - 1]))
                i += 1
                if on_page:
                    on_page(i, n, pdf, p)
    return out


# ---------------------------------------------------------------- align.json
def load(file_dir: Path | str) -> dict:
    """파일 폴더의 align.json (없거나 깨졌으면 빈 dict). 모양이 틀린 파일(pages가 dict가 아님 등)은 정렬 없음으로,
    dict가 아닌 쪽 항목은 빼고 돌려준다 (프로젝트 열기가 멈추지 않게)."""
    p = Path(file_dir) / ALIGN_FILE
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("pages", {}), dict):
        return {}
    if "reference" in data and not isinstance(data["reference"], dict):
        data["reference"] = None
    if "pages" in data:
        data["pages"] = {k: v for k, v in data["pages"].items() if isinstance(v, dict)}
    return data


def save(file_dir: Path | str, data: dict) -> None:
    """align.json에 쓴다. 맞춘 쪽이 하나도 없으면 파일을 지운다."""
    p = Path(file_dir) / ALIGN_FILE
    if not data.get("pages"):
        if p.exists():
            p.unlink()
        return
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for i in range(20):  # Windows: 다른 스레드가 마침 읽는 중이면 바꿔 쓰기를 거절한다
        try:
            tmp.replace(p)
            return
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.05)


def applied(entry: dict | None) -> bool:
    """이 정렬을 영역에 적용하나: 자동(ok)·손으로 맞춤(manual)은 적용. ‘정렬 확인 필요’는 점수가 MIN_SCORE 이상이면
    적용하고(이동·크기가 커서 확인만 필요), 점수가 낮으면(다른 형식 카드일 수 있어 믿을 수 없음) 적용하지 않는다."""
    if not isinstance(entry, dict):
        return False
    st = entry.get("status")
    if st in (STATUS_OK, STATUS_MANUAL):
        return True
    try:
        return st == STATUS_CHECK and float(entry.get("score") or 0.0) >= MIN_SCORE
    except (TypeError, ValueError):  # 숫자가 아닌 점수: 믿을 수 없는 정렬
        return False


def page_transforms(data: dict) -> dict[int, dict]:
    """align.json -> {쪽: {dx, dy, sx, sy}} (영역에 적용할 쪽만: 기준 카드·믿을 수 없는 정렬·변환 없음은 뺀다)."""
    out: dict[int, dict] = {}
    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, dict):  # 모양이 틀린 align.json: 정렬 없음
        return out
    for k, v in pages.items():
        if not str(k).isdigit() or not applied(v):
            continue
        try:
            t = {key: float(v.get(key, IDENTITY[key])) for key in ("dx", "dy", "sx", "sy")}
        except (TypeError, ValueError):
            continue
        if t["sx"] <= 0 or t["sy"] <= 0 or t == {key: IDENTITY[key] for key in t}:
            continue
        out[int(k)] = t
    return out
