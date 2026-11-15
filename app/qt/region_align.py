"""영역 지정 창의 자동 정렬(카드마다 위치 맞추기)과 미리 보기 패널·뺄 쪽 (region_editor에서 나눔)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QMessageBox

from app import service
from app.qt.dialogs import show_problem

if TYPE_CHECKING:
    from pathlib import Path


class RegionAlignPreviewMixin:
    """RegionEditor의 정렬·미리 보기 부분. RegionEditor가 만든 위젯과 상태를 쓴다."""

    def _align_label(self) -> str:
        """지금 카드의 정렬 상태 글 (공통 영역 그룹, 따로 영역이 아닌 카드)."""
        if self.is_per_card() or getattr(self, "_ov_key", None):
            return ""
        e = self._align_pages().get(self.page_spin.value())
        if not e:
            return ""
        st, score = e.get("status"), float(e.get("score") or 0.0)
        if st == "ref":
            return "정렬 기준 카드"
        if st == "manual":
            return "정렬: 손으로 맞춤"
        if st == "check":
            return f"정렬 확인 필요 (점수 {score:.2f}" + (")" if e.get("applied") else ", 적용 안 함)")
        return f"정렬: 자동 (점수 {score:.2f})"

    # ------------------------------------------------------------ 미리보기 (오른쪽 4×4)
    def _toggle_preview(self, on: bool):
        self.side_scroll.setVisible(not on)
        self.preview.setVisible(on)
        w = max(800, self.split.width())
        if on:
            self.split.setSizes([int(w * 0.47), 0, int(w * 0.53)])
            self._sync_preview(force=True)
        else:
            self.split.setSizes([int(w * 0.72), int(w * 0.28), 0])
        self.view.fit_again()

    def _sync_preview(self, force: bool = False):
        if not hasattr(self, "preview") or not self.preview.isVisible():
            return
        fd = self.current_file()
        if force or self.preview.file_dir != fd:
            self.preview.set_file(fd, list(range(1, self._page_count(fd) + 1)), self.page_spin.value())
        else:
            self.preview.show_page(self.page_spin.value())

    def _page_count(self, fd: Path) -> int:
        cache = self.__dict__.setdefault("_count_cache", {})
        if fd not in cache:
            cache[fd] = service.page_count(fd)  # PDF를 연다: 미리보기를 그릴 때마다 열지 않게
        return cache[fd]

    def _excluded(self, fd: Path | None = None) -> set[int]:
        fd = fd or self.current_file()
        shown = set(self._shown(fd))
        n = self._page_count(fd)
        return set() if len(shown) >= n else set(range(1, n + 1)) - shown

    def _preview_regions(self, page: int) -> list[dict]:
        """미리보기에 그릴 그 카드의 영역 (지금 카드는 편집 중인 값). protect는 kind="protect"."""
        t = None  # 공통 영역(기준 카드 좌표)이면 그 카드의 자동 정렬만큼 옮겨 그린다
        if page == self.page_spin.value():
            regions, protect = self._items_to_dicts()
            t = self._view_t
        else:
            key = f"{self.current_file().name}/{page}"
            ov = self._cards().get(key)
            if self.is_per_card() or ov is not None:
                regions, protect = (ov or {}).get("regions", []), (ov or {}).get("protect_rects", [])
            else:
                regions, protect = self._common if self._ov_key else self._items_to_dicts()
                t = self._align_t(page=page)
        if t:
            regions, protect = [self._aligned_dict(r, t) for r in regions], [self._aligned_dict(p, t) for p in protect]
        return list(regions) + [{**p, "kind": "protect"} for p in protect]

    def _preview_info(self, page: int) -> tuple[bool, bool, bool, str]:
        """(지금 카드?, 뺐나?, 따로 영역?, 정렬 상태: "" | ref | ok | check | manual)."""
        key = f"{self.current_file().name}/{page}"
        ov = not self.is_per_card() and key in self.overrides()
        st = "" if self.is_per_card() or ov else (self._align_pages().get(page) or {}).get("status", "")
        return page == self.page_spin.value(), page in self._excluded(), ov, st

    def _open_from_preview(self, page: int):
        if page in self._excluded():
            self.status.setText(f"{page}쪽은 뺀 쪽입니다. 되살리면(뺌 체크 해제) 편집할 수 있습니다")
            return
        self.page_spin.setValue(page)
        self.view.setFocus()

    def _preview_card_action(self, page: int, act: str):
        if self.is_per_card():
            self.status.setText("카드마다 영역 지정 중에는 카드마다 이미 따로 영역입니다")
            return
        if page in self._excluded():
            self.status.setText(f"{page}쪽은 뺀 쪽입니다")
            return
        if page != self.page_spin.value():
            self.page_spin.setValue(page)
        self.override_btn.setChecked(act == "override")

    def set_page_excluded(self, page: int, on: bool):
        """미리보기에서 뺄 쪽 바꾸기 (바로 file.json에 쓴다: 영역 저장과 따로)."""
        fd = self.current_file()
        excl = self._excluded(fd)
        new = sorted(excl | {page}) if on else sorted(excl - {page})
        if on and len(new) >= self._page_count(fd):
            QMessageBox.information(self, "뺄 쪽", "모든 쪽을 뺄 수는 없습니다")
            self.preview.refresh()
            return
        try:
            service.set_pages(fd, new)
        except ValueError as e:
            show_problem(self, "뺄 쪽을 바꾸지 못했습니다", str(e))
            return
        self.__dict__.setdefault("_shown_cache", {}).pop(fd, None)
        self.status.setText(f"{page}쪽을 뺐습니다" if on else f"{page}쪽을 되살렸습니다")
        if on and page == self.page_spin.value():
            self._change_page()  # 뺀 쪽에서는 가까운 보일 쪽으로
        else:
            self._load_page()  # ‘/ 20 (뺀 쪽 n개 건너뜀)’
        self.preview.refresh()

    # ------------------------------------------------------------ 자동 정렬
    def _align_pages(self, fd: Path | None = None) -> dict:
        """{쪽: 정렬 값} (파일마다 한 번 읽어 둔다)."""
        fd = fd or self.current_file()
        if fd not in self._align_cache:
            try:
                self._align_cache[fd] = service.align_info(fd)["pages"]
            except Exception:  # noqa: BLE001 - 정렬 파일이 깨졌으면 정렬 없음으로
                self._align_cache[fd] = {}
        return self._align_cache[fd]

    def _align_t(self, fd: Path | None = None, page: int | None = None) -> dict | None:
        """그 카드(기본: 지금 카드)에 적용하는 정렬 변환 (공통 영역 그룹에서만. 없으면 None).
        따로 영역 카드인지는 부르는 쪽이 본다."""
        if self.is_per_card():
            return None
        fd = fd or self.current_file()
        e = self._align_pages(fd).get(self.page_spin.value() if page is None else int(page))
        if not e or not e.get("applied"):
            return None
        return {k: float(e.get(k, d)) for k, d in (("dx", 0.0), ("dy", 0.0), ("sx", 1.0), ("sy", 1.0))}

    @staticmethod
    def _aligned_dict(r: dict, t: dict | None) -> dict:
        """영역 dict의 rect만 정렬만큼 옮긴 사본."""
        return {**r, "rect": service.align_rect(r["rect"], t)} if t and r.get("rect") else dict(r)

    def _reapply_alignment(self):
        """정렬 값이 바뀐 뒤: 상자를 같은 기준 좌표의 새 자리로 옮긴다 (영역 자체는 바뀌지 않음)."""
        ratios = [(it, self.ratio_of(it)) for it in self.items()]
        self._view_t = None if (self.is_per_card() or self._ov_key) else self._align_t()
        for it, r in ratios:
            self._place(it, r)
        sel = self.selected()
        if sel is not None:
            self._to_form(sel)
        self._update_card_info()
        if hasattr(self, "preview"):
            self.preview.schedule()

    def _align_reason_blocked(self) -> str:
        """지금 카드의 정렬을 손으로 옮길 수 없는 까닭 (옮길 수 있으면 "")."""
        if self.is_per_card():
            return "카드마다 영역을 정하는 그룹은 정렬 대신 영역을 직접 옮기세요"
        if self._ov_key:
            return "따로 영역 카드는 정렬 대신 영역을 직접 옮기세요"
        if (self._align_pages().get(self.page_spin.value()) or {}).get("status") == "ref":
            return "정렬 기준 카드입니다. 기준 카드에서는 영역 자체를 옮기세요"
        return ""

    def nudge_alignment(self, dx_px: float, dy_px: float):
        """Ctrl+화살표: 이 카드의 정렬만 옮긴다 (모든 상자 함께, 바로 저장)."""
        why = self._align_reason_blocked()
        if why:
            self.status.setText(why)
            return
        pr = self.scene.page_rect
        if pr.isEmpty():
            return
        fd, page = self.current_file(), self.page_spin.value()
        try:
            e = service.nudge_alignment(fd, page, dx_px / pr.width(), dy_px / pr.height())
        except (OSError, ValueError) as ex:
            show_problem(self, "정렬을 옮기지 못했습니다", str(ex))
            return
        if e is None:
            self.status.setText("정렬 기준 카드입니다. 기준 카드에서는 영역 자체를 옮기세요")
            return
        self._align_cache.pop(fd, None)
        self._reapply_alignment()
        self.status.setText(f"{page}쪽 정렬을 옮겼습니다 (손으로 맞춤, 바로 저장)")

    def reset_alignment(self):
        """이 카드 정렬 되돌리기 (손으로 맞춤 -> 자동 값, 자동 -> 정렬 없음)."""
        if self.is_per_card() or self._ov_key:
            return
        fd, page = self.current_file(), self.page_spin.value()
        e = service.reset_alignment(fd, page)
        self._align_cache.pop(fd, None)
        self._reapply_alignment()
        self.status.setText(f"{page}쪽 정렬을 자동 정렬 값으로 되돌렸습니다" if e else f"{page}쪽 정렬을 없앴습니다 (공통 영역 그대로)")

    def run_alignment(self):
        """‘자동 정렬’: 그룹의 모든 카드를 기준 카드에 맞춘다 (작업 스레드, 진행률 창)."""
        if self.is_per_card():
            QMessageBox.information(self, "자동 정렬", "카드마다 영역을 정하는 그룹은 자동 정렬하지 않습니다")
            return
        if self.dirty and not self._confirm_leave():  # 정렬은 저장된 영역(기준 카드·따로 영역 카드)을 본다
            return
        from app.qt import worker

        job = worker.Job(service.align_group, list(self.file_dirs))
        worker.run_with_dialog(self, "카드 맞추는 중", job, self._alignment_done,
                               lambda m: show_problem(self, "자동 정렬을 하지 못했습니다", m))

    def _alignment_done(self, res):
        if res.state != "done":
            QMessageBox.information(self, "자동 정렬", res.reason)
            return
        self._align_cache = {}
        self._reapply_alignment()
        self._sync_preview(force=True)
        msg = f"맞춘 카드 {res.ok_count}장, 정렬 확인 필요 {res.check_count}장"
        self.status.setText(msg)
        more = ("\n\n‘정렬 확인 필요’ 카드는 미리보기(G)의 ‘정렬 확인 카드만’으로 모아 보고, Ctrl+화살표로 맞추거나 "
                "‘이 카드 정렬 되돌리기’를 누르세요." if res.check_count else "")
        QMessageBox.information(self, "자동 정렬", msg + more + "\n\n이미 글자를 읽은(추출한) 카드는 자동으로 다시 읽지 않습니다. "
                                "지운 카드는 다음 ‘글자 지우기’에서 맞춘 자리로 다시 지웁니다.")
