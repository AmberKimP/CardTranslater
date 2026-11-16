"""영역 지정 창의 오른쪽 입력란: 고른 영역 <-> 입력란, 글꼴 목록, 카드별 서식 덮어쓰기 (region_editor에서 나눔)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QColorDialog, QDoubleSpinBox

from app import service
from app.qt.region_scene import ALIGN_LABELS, KIND_LABELS

if TYPE_CHECKING:
    from app.qt.region_scene import RegionItem


class RegionFormMixin:
    """RegionEditor의 입력란 부분. RegionEditor가 만든 위젯(self.f_*)과 상태를 쓴다."""

    def used_fonts(self, exclude: RegionItem | None = None) -> list[tuple[str, str, list[str]]]:
        """다른 영역에서 이미 고른 글꼴 [(파일, 이름, [쓰는 곳])]: 이 창의 영역 + 프로젝트의 다른 그룹."""
        used: dict[str, tuple[str, list[str]]] = {}

        def add(font, where: str):
            f = (font or {}).get("file")
            if f:
                fam = font.get("family") or next((x["family"] for x in self.fonts if x["file"] == f), Path(f).stem)
                used.setdefault(f, (fam, []))[1].append(where)

        for it in self.items():
            if it is not exclude and it.kind != "protect":
                add((it.data.get("style") or {}).get("font"), it.name)
        for g in self.groups:
            if g["dir"] == self.group_dir:
                continue
            for r in service.load_region_template(g["dir"]).get("regions", []):
                add((r.get("style") or {}).get("font"), f"{g['name']}/{r.get('name')}")
        return [(f, fam, where) for f, (fam, where) in used.items()]

    def _fill_fonts(self, exclude: RegionItem | None = None):
        """글꼴 목록: 맨 위에 다른 영역에서 쓰는 글꼴(굵게), 그다음 기본값과 전체 목록."""
        self.f_font.blockSignals(True)
        self.f_font.clear()
        used = self.used_fonts(exclude)
        bold = QFont(self.f_font.font())
        bold.setBold(True)
        for f, fam, where in used:
            label = next((x["label"] for x in self.fonts if x["file"] == f), fam)
            more = " …" if len(where) > 3 else ""
            self.f_font.addItem(f"{label}  (쓰는 곳: {', '.join(where[:3])}{more})", f)
            self.f_font.setItemData(self.f_font.count() - 1, bold, Qt.ItemDataRole.FontRole)
        if used:
            self.f_font.insertSeparator(self.f_font.count())
        self.f_font.addItem("(기본: Noto Sans KR)", "")
        top = {f for f, _fam, _w in used}
        for f in self.fonts:
            if f["file"] not in top:
                self.f_font.addItem(f["label"], f["file"])
        self.f_font.blockSignals(False)

    def _ratio_spin(self):
        s = QDoubleSpinBox()
        s.setRange(0, 1)
        s.setDecimals(4)
        s.setSingleStep(0.002)
        return s

    def _toggle_override(self, on: bool):
        if getattr(self, "_syncing_override", False) or self.is_per_card():
            return
        key = self.card_key()
        if on and self._ov_key != key:
            # 공통 영역은 이 카드를 불러올 때(또는 저장할 때) 모양 그대로 둔다 (기준 카드 좌표).
            # 지금 화면에서 고친 것은 이 카드의 따로 영역이 된다
            snap = getattr(self, "_shared_snap", None)
            self._common = copy.deepcopy(snap) if snap is not None else self._items_to_dicts()
            self._view_t = None  # 따로 영역은 이 카드에서 보이는 자리 그대로 (자동 정렬을 적용한 자리)
            for it in self.items():
                it._placed = None
            regions, protect = self._items_to_dicts()
            self._cards()[key] = {"regions": copy.deepcopy(regions), "protect_rects": copy.deepcopy(protect)}
            self._ov_key = key
            self.status.setText(f"{self.page_spin.value()}쪽만 따로 영역을 씁니다. 이 카드에서 고친 영역은 이 카드에만 씁니다 "
                                "(다른 카드는 그대로)")
            if hasattr(self, "preview"):
                self.preview.schedule()
        elif not on and self._ov_key == key:
            self._cards().pop(key, None)
            self._clear_items()
            self._view_t = self._align_t()
            self._load_dicts(*self._common)
            self._ov_key = None
            self.status.setText(f"{self.page_spin.value()}쪽을 공통 영역으로 되돌렸습니다")
        self._mark_dirty()

    @staticmethod
    def _override_with_common_style(ov: dict, common: list[dict]) -> dict:
        """따로 영역은 위치·크기만 따로: 같은 이름의 공통 영역이 있으면 서식·지우기 방식 등은 공통 영역 것을 쓴다
        (공통 영역의 글꼴을 나중에 바꿔도 따로 영역 카드만 옛 글꼴로 남지 않게)."""
        by = {r.get("name"): r for r in common}
        regions = []
        for r in ov.get("regions", []):
            c = by.get(r.get("name"))
            regions.append({**copy.deepcopy(c), "rect": r["rect"]} if c else r)
        return {**ov, "regions": regions}

    def overrides(self) -> list[str]:
        """따로 영역을 둔 카드 키 (공통 영역 그룹)."""
        if self.is_per_card():
            return []
        return list(self._cards())

    # ------------------------------------------------------------ 폼 <-> 영역
    def _to_form(self, it: RegionItem | None):
        self._loading = True
        try:
            if it is None:
                return
            st = it.data.get("style") or {}
            er = it.data.get("erase") or {}
            self.f_name.setText(it.name)
            self.f_kind.setCurrentIndex([k for k, _t in KIND_LABELS].index(it.kind))
            r = self.ratio_of(it)
            for sp, k in ((self.f_x, "x"), (self.f_y, "y"), (self.f_w, "w"), (self.f_h, "h")):
                sp.setValue(r[k])
            self.f_rotate.setChecked(int(st.get("rotate", 0) or 0) == 180)
            self.f_align.setCurrentIndex(max(0, [k for k, _t in ALIGN_LABELS].index(st.get("align", ""))
                                             if st.get("align", "") in dict(ALIGN_LABELS) else 0))
            self.f_color.setText(st.get("color", ""))
            ff = (st.get("font") or {}).get("file", "")
            self._fill_fonts(exclude=it)
            idx = self.f_font.findData(ff)
            if idx < 0 and ff:  # 목록에 없는 글꼴 파일도 보여 준다
                self.f_font.addItem(f"{(st.get('font') or {}).get('family') or Path(ff).stem} (목록 밖)", ff)
                idx = self.f_font.count() - 1
            self.f_font.setCurrentIndex(max(0, idx))
            self.f_font_mode.setCurrentIndex(max(0, self.f_font_mode.findData(st.get("font_mode") or "cell")))
            self.f_icons.setCurrentIndex(max(0, self.f_icons.findData(st.get("icons") or "")))
            self.f_erase_mode.setCurrentIndex(max(0, self.f_erase_mode.findData(er.get("mode") or "auto")))
            self.f_keep_tall.setValue(float(er.get("keep_ink_taller_than_pt") or 0))
            self.f_keep_above.setChecked(bool(er.get("keep_ink_above_text")))
            self.f_notes.setPlainText(it.data.get("notes", ""))
            prot = it.kind == "protect"
            for w in (self.f_rotate, self.f_align, self.f_color, self.f_font, self.f_keep_tall, self.f_keep_above):
                w.setEnabled(not prot)
            self.f_erase_mode.setEnabled(it.kind == "text")
            self.f_icons.setEnabled(it.kind == "text")
            self.f_font_mode.setEnabled(it.kind == "text")  # 태그 라벨은 라벨마다 짧은 글이라 칸 단위만
            self.f_ocr_exclude.setEnabled(prot)
            self.f_ocr_exclude.setChecked(bool(it.data.get("ocr_exclude")) and prot)
            self._sync_kind_fields(it.kind)
        finally:
            self._loading = False

    def _sync_kind_fields(self, kind: str):
        """종류에 맞는 칸만: 보호 영역은 한글 넣기·글자 지우기 묶음을 숨기고, 보호 영역 칸은 보호일 때만."""
        prot = kind == "protect"
        self.g_style.setVisible(not prot)
        self.g_erase.setVisible(not prot)
        self.f_ocr_exclude.setVisible(prot)
        lab = self._basic_form.labelForField(self.f_ocr_exclude)
        if lab is not None:
            lab.setVisible(prot)

    def _from_form(self, *_a):
        if self._loading:
            return
        it = self.selected()
        if it is None:
            return
        d = it.data
        old_name = d.get("name", "")
        old_settings = copy.deepcopy({k: d.get(k) for k in ("style", "erase", "notes", "kind")})
        d["name"] = self.f_name.text().strip()
        it.kind = self.f_kind.currentData()
        if it.kind != "protect":
            d["kind"] = it.kind
        r = QRectF(self.f_x.value(), self.f_y.value(), self.f_w.value(), self.f_h.value())
        if r.width() > 0 and r.height() > 0:
            new = self.rect_from_ratio({"x": r.x(), "y": r.y(), "w": r.width(), "h": r.height()})
            if (new.topLeft() - it.rect().topLeft()).manhattanLength() > 0.5 or \
                    abs(new.width() - it.rect().width()) > 0.5 or abs(new.height() - it.rect().height()) > 0.5:
                it.set_scene_rect(new)
        st = d.setdefault("style", {})
        if self.f_rotate.isChecked():
            st["rotate"] = 180
        else:
            st.pop("rotate", None)
        if self.f_align.currentData():  # 자동이면 적지 않는다 (그림 카드는 원문 줄의 정렬을 따름)
            st["align"] = self.f_align.currentData()
        else:
            st.pop("align", None)
        color = self.f_color.text().strip()
        if color:
            st["color"] = color
        else:
            st.pop("color", None)
        ff = self.f_font.currentData()
        old = st.get("font") or {}
        if ff and old.get("file") != ff:
            fam = next((f["family"] for f in self.fonts if f["file"] == ff), Path(ff).stem)
            font = {"family": fam, "file": ff}
            if old.get("fallback_file"):
                font["fallback_file"] = old["fallback_file"]
            font["chosen"] = "영역 편집기에서 고름"
            st["font"] = font
        elif not ff:
            st.pop("font", None)
        if self.f_font_mode.currentData() == "sentence" and it.kind == "text":
            st["font_mode"] = "sentence"
        else:
            st.pop("font_mode", None)  # 기본(한 칸 모두 같은 글꼴)은 적지 않는다
        if self.f_icons.currentData() == "none" and it.kind == "text":
            st["icons"] = "none"  # 글자만 (아이콘 없음)
        else:
            st.pop("icons", None)  # 기본(섞여 있음)은 적지 않는다
        er = d.setdefault("erase", {})
        mode = self.f_erase_mode.currentData()
        if mode and mode != "auto" and it.kind == "text":
            er["mode"] = mode
        else:
            er.pop("mode", None)
        if self.f_keep_tall.value() > 0:
            er["keep_ink_taller_than_pt"] = round(self.f_keep_tall.value(), 1)
        else:
            er.pop("keep_ink_taller_than_pt", None)
        if self.f_keep_above.isChecked():
            er["keep_ink_above_text"] = True
        else:
            er.pop("keep_ink_above_text", None)
        if not er:
            d.pop("erase", None)
        if it.kind == "protect":
            if self.f_ocr_exclude.isChecked():
                d["ocr_exclude"] = True
            else:
                d.pop("ocr_exclude", None)
        notes = self.f_notes.toPlainText().strip()
        if notes:
            d["notes"] = notes
        else:
            d.pop("notes", None)
        if not st:
            d.pop("style", None)
        new_settings = {k: d.get(k) for k in ("style", "erase", "notes", "kind")}
        if new_settings != old_settings or d["name"] != old_name:
            n = self._sync_same_name(old_name, it)
            if n:
                self.status.setText(f"다른 카드의 ‘{old_name}’ 영역 {n}개에도 적용했습니다")
        it.refresh()
        self._sync_kind_fields(it.kind)
        self._rebuild_list(it)
        self._mark_dirty()

    def _pick_color(self):
        c = QColorDialog.getColor(QColor(self.f_color.text() or "#000000"), self, "글자 색")
        if c.isValid():
            self.f_color.setText(c.name())
            self._from_form()
