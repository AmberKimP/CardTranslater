"""한글 넣기 직전 글꼴 고르기 창.

- 항목(영역)마다 글꼴을 고른다. 기본 선택은 원본과 느낌이 가장 비슷한 추천(★).
- 항목을 펼치면 카드(문장)마다 따로 고를 수 있다. 기본은 ‘항목과 같음’.
- 목록: 추천 글꼴 + 이 프로젝트에서 쓴 적이 있는 글꼴 + 이 창에서 추가한 글꼴, 맨 아래 ‘글꼴 추가…’
  (PC에 설치된 글꼴 목록 또는 글꼴 파일).
화면은 app.service만 부른다.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from app import service
from app.qt.dialogs import fit_to_screen, set_ok_text, show_error

ADD = "__add__"
SAME = ""  # 카드 줄: 항목과 같음
ROLE_KIND = Qt.ItemDataRole.UserRole  # 트리 항목: ("region", plan_idx, region_name) | ("card", plan_idx, name, page)

_families: dict[str, str | None] = {}


def qt_family(file: str) -> str | None:
    """글꼴 파일을 화면 미리 보기용으로 등록하고 Qt 글꼴 이름을 돌려준다."""
    if file not in _families:
        fid = QFontDatabase.addApplicationFont(str(service.font_path(file)))
        fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
        _families[file] = fams[0] if fams else None
    return _families[file]


class AddFontDialog(QDialog):
    """PC에 설치된 글꼴(한글이 있고 PDF에 넣어도 되는 것) 중 하나를 고르거나, 글꼴 파일을 고른다."""

    def __init__(self, fonts: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("글꼴 추가")
        self.resize(560, 560)
        self.chosen: dict | None = None
        self.search = QLineEdit()
        self.search.setPlaceholderText("이름으로 찾기")
        self.search.textChanged.connect(self._filter)
        self.list = QListWidget()
        for f in fonts:
            it = QListWidgetItem(f["label"])
            it.setData(Qt.ItemDataRole.UserRole, f)
            it.setToolTip(f["file"])
            self.list.addItem(it)
        self.list.currentItemChanged.connect(self._preview)
        self.list.itemDoubleClicked.connect(lambda *_a: self.accept())
        self.sample = QLabel("가나다라 한글 글꼴 견본 ABC 123")
        self.sample.setMinimumHeight(60)
        self.sample.setStyleSheet("background: white; border: 1px solid #ccc; padding: 6px")
        note = QLabel(f"PC에 설치된 글꼴 중 한글이 있고 PDF에 넣어도 되는 글꼴 {len(fonts)}개 (.ttc 글꼴 모음은 빠짐)")
        note.setStyleSheet("color: #666")
        note.setWordWrap(True)
        b_file = QPushButton("파일에서 찾기…")
        b_file.clicked.connect(self._from_file)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "추가")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(b_file)
        row.addStretch(1)
        row.addWidget(bb)
        lay = QVBoxLayout(self)
        lay.addWidget(self.search)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.sample)
        lay.addWidget(note)
        lay.addLayout(row)

    def _filter(self, text: str):
        t = text.strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(t) and t not in it.text().lower())

    def _preview(self, it, _prev=None):
        if it is None:
            return
        fam = qt_family(it.data(Qt.ItemDataRole.UserRole)["file"])
        if fam:
            self.sample.setFont(QFont(fam, 18))

    def _from_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "글꼴 파일", "", "글꼴 (*.ttf *.otf)")
        if not path:
            return
        info = service.font_file_info(path)
        if not info["ok"]:
            show_error(self, "이 글꼴은 쓸 수 없습니다", info["reason"], "다른 글꼴 파일을 고르세요.")
            return
        self.chosen = info
        super().accept()

    def accept(self):
        it = self.list.currentItem()
        if it is None or it.isHidden():
            return
        self.chosen = it.data(Qt.ItemDataRole.UserRole)
        super().accept()


class FontPlanDialog(QDialog):
    """plans: service.font_plans() 결과. 확인하면 choices()로 파일마다 고른 것을 돌려준다."""

    def __init__(self, plans: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("한글 글꼴 고르기")
        fit_to_screen(self, 1100, 720)
        self.plans = plans
        self.added: list[dict] = []  # 이 창에서 추가한 글꼴 {"file", "label"}
        self.combos: list[tuple[QComboBox, str]] = []  # (콤보, 종류: region|card)

        title = QLabel("한글 글꼴 고르기")
        f = title.font()
        f.setPointSize(f.pointSize() + 3)
        f.setBold(True)
        title.setFont(f)
        help_ = QLabel("항목마다 글꼴을 고릅니다. ★가 원본과 느낌이 가장 비슷한 추천입니다.\n"
                       "항목을 펼치면 카드(문장)마다 따로 고를 수 있습니다. 목록 맨 아래 ‘글꼴 추가…’로 PC의 다른 글꼴도 쓸 수 있습니다.")
        help_.setWordWrap(True)
        notes = [f"{p['file']}: {p['note']}" for p in plans if p.get("note")]
        self.note = QLabel("\n".join(notes))
        self.note.setStyleSheet("color: #a15c00")
        self.note.setWordWrap(True)
        self.note.setVisible(bool(notes))

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["항목 / 카드", "글꼴", "번역"])
        self.tree.setColumnWidth(0, 230)
        self.tree.setColumnWidth(1, 380)
        self.tree.itemExpanded.connect(self._expand)
        self.tree.currentItemChanged.connect(lambda *_a: self._preview())

        self.sample = QLabel()
        self.sample.setMinimumHeight(70)
        self.sample.setWordWrap(True)
        self.sample.setStyleSheet("background: white; border: 1px solid #ccc; padding: 8px")
        self.sample_note = QLabel()
        self.sample_note.setStyleSheet("color: #666")

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "이대로 한글 넣기")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(title)
        lay.addWidget(help_)
        lay.addWidget(self.note)
        lay.addWidget(self.tree, 1)
        lay.addWidget(QLabel("미리 보기 (고른 줄의 번역을 고른 글꼴로)"))
        lay.addWidget(self.sample)
        lay.addWidget(self.sample_note)
        lay.addWidget(bb)
        self._build()

    # ---- 목록
    def _options(self, pi: int, name: str, card: bool) -> list[tuple[str, str, str]]:
        """콤보 항목 [(보이는 글, 파일, 설명)]. 구분선은 ("", "-", "")."""
        plan = self.plans[pi]
        reg = next(r for r in plan["regions"] if r["name"] == name)
        out: list[tuple[str, str, str]] = []
        if card:
            out.append(("(항목과 같음)", SAME, "이 항목에서 고른 글꼴을 씁니다"))
        seen = set()
        for i, r in enumerate(reg["recommended"]):
            mark = "★ " if i == 0 else "    "
            by = {"claude": "추천", "shape": "모양 비슷", "default": "기본"}.get(r.get("by", ""), "")
            out.append((f"{mark}{r['label']}" + (f"  · {r['reason']}" if r.get("reason") else ""), r["file"],
                        f"{by}: {r.get('reason', '')}"))
            seen.add(r["file"])
        others = [u for u in plan.get("used", []) + self.added if u["file"] not in seen]
        if reg.get("selected") and reg["selected"] not in seen and all(u["file"] != reg["selected"] for u in others):
            others.insert(0, {"file": reg["selected"], "label": Path(reg["selected"]).stem})
        if others:
            out.append(("", "-", ""))
            for u in others:
                if u["file"] in seen:
                    continue
                seen.add(u["file"])
                src = "추가한 글꼴" if u in self.added else "이 프로젝트에서 쓴 글꼴"
                out.append((f"    {u['label']}", u["file"], src))
        out.append(("", "-", ""))
        out.append(("글꼴 추가…", ADD, "PC에 설치된 다른 글꼴이나 글꼴 파일을 추가합니다"))
        return out

    def _fill(self, combo: QComboBox, pi: int, name: str, card: bool, selected: str | None):
        combo.blockSignals(True)
        combo.clear()
        for text, file, tip in self._options(pi, name, card):
            if file == "-":
                combo.insertSeparator(combo.count())
                continue
            combo.addItem(text, file)
            combo.setItemData(combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
        idx = combo.findData(selected if selected is not None else (SAME if card else None))
        combo.setCurrentIndex(max(idx, 0))
        combo.setProperty("prev", combo.currentData())
        combo.blockSignals(False)

    def _combo(self, item: QTreeWidgetItem, pi: int, name: str, card: bool, selected: str | None) -> QComboBox:
        cb = QComboBox()
        cb.setProperty("meta", (pi, name, card))
        self._fill(cb, pi, name, card, selected)
        cb.currentIndexChanged.connect(lambda _i, c=cb, it=item: self._changed(c, it))
        self.tree.setItemWidget(item, 1, cb)
        self.combos.append((cb, "card" if card else "region"))
        return cb

    def _build(self):
        many = len(self.plans) > 1
        for pi, plan in enumerate(self.plans):
            parent = self.tree.invisibleRootItem()
            if many:
                parent = QTreeWidgetItem([plan["file"]])
                self.tree.addTopLevelItem(parent)
                parent.setExpanded(True)
            for reg in plan["regions"]:
                first = next((c["text"] for c in reg["cards"] if c["text"]), "")
                it = QTreeWidgetItem([reg["name"], "", first[:60]])
                it.setData(0, ROLE_KIND, ("region", pi, reg["name"]))
                it.setToolTip(2, first)
                parent.addChild(it)
                if reg["cards"]:
                    it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                self._combo(it, pi, reg["name"], False, reg["selected"])
                if any(c["selected"] for c in reg["cards"]):  # 카드별로 고른 것이 있으면 펼쳐 보인다
                    it.setExpanded(True)
        top = self.tree.topLevelItem(0)
        if top is not None:
            self.tree.setCurrentItem(top.child(0) if many and top.childCount() else top)

    def _expand(self, item: QTreeWidgetItem):
        kind = item.data(0, ROLE_KIND)
        if not kind or kind[0] != "region" or item.childCount():
            return
        _k, pi, name = kind
        reg = next(r for r in self.plans[pi]["regions"] if r["name"] == name)
        for c in reg["cards"]:
            ch = QTreeWidgetItem([f"{c['page']}쪽", "", c["text"][:80] or "(번역 없음)"])
            ch.setData(0, ROLE_KIND, ("card", pi, name, c["page"]))
            ch.setToolTip(2, c["text"])
            item.addChild(ch)
            self._combo(ch, pi, name, True, c["selected"] or SAME)

    # ---- 고르기
    def _changed(self, combo: QComboBox, item: QTreeWidgetItem):
        if combo.currentData() == ADD:
            new = self._add_font()
            if new is None:
                combo.blockSignals(True)
                combo.setCurrentIndex(max(combo.findData(combo.property("prev")), 0))
                combo.blockSignals(False)
                return
            if all(a["file"] != new["file"] for a in self.added):
                self.added.append(new)
            for cb, _kind in self.combos:  # 모든 목록에 추가하고, 이 줄은 새 글꼴로
                pi, name, card = cb.property("meta")
                keep = new["file"] if cb is combo else cb.currentData()
                self._fill(cb, pi, name, card, keep)
        combo.setProperty("prev", combo.currentData())
        self.tree.setCurrentItem(item)
        self._preview()

    def _add_font(self) -> dict | None:
        from PySide6.QtGui import QGuiApplication

        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            fonts = service.installed_fonts()  # 처음 한 번은 몇 초 걸린다
        finally:
            QGuiApplication.restoreOverrideCursor()
        dlg = AddFontDialog(fonts, self)
        if not dlg.exec() or not dlg.chosen:
            return None
        return {"file": dlg.chosen["file"], "label": dlg.chosen["label"]}

    def _row_font(self, item: QTreeWidgetItem) -> str | None:
        cb = self.tree.itemWidget(item, 1)
        if cb is None:
            return None
        f = cb.currentData()
        if f == SAME and item.parent() is not None:
            return self._row_font(item.parent())
        return f if f not in (ADD, SAME) else None

    def _preview(self):
        item = self.tree.currentItem()
        if item is None:
            return
        text = item.toolTip(2) or item.text(2) or "가나다라 한글 글꼴 견본"
        f = self._row_font(item)
        fam = qt_family(f) if f else None
        if fam:
            self.sample.setFont(QFont(fam, 16))
        self.sample.setText(text[:200])
        cb = self.tree.itemWidget(item, 1)
        note = cb.currentText().strip() if cb else ""
        if cb is not None and cb.currentData() == SAME and item.parent() is not None:
            pcb = self.tree.itemWidget(item.parent(), 1)
            note = "항목과 같음: " + (pcb.currentText().strip().lstrip("★ ") if pcb else "")
        self.sample_note.setText(note)

    # ---- 결과
    def choices(self) -> list[tuple[str, dict, dict]]:
        """[(파일 폴더, {영역: 글꼴}, {쪽: {영역: 글꼴}})]. 펼치지 않은 항목의 카드별 선택은 예전 것을 그대로 둔다."""
        out = []
        for pi, plan in enumerate(self.plans):
            regions, cards = {}, {}
            for reg in plan["regions"]:  # 펼치지 않은 항목: 예전 카드별 선택 유지
                for c in reg["cards"]:
                    if c["selected"]:
                        cards.setdefault(c["page"], {})[reg["name"]] = c["selected"]
            out.append((plan["file_dir"], regions, cards))
        it = self.tree.invisibleRootItem()
        stack = [it.child(i) for i in range(it.childCount())]
        while stack:
            item = stack.pop()
            stack += [item.child(i) for i in range(item.childCount())]
            kind = item.data(0, ROLE_KIND)
            cb = self.tree.itemWidget(item, 1)
            if not kind or cb is None:
                continue
            f = cb.currentData()
            if kind[0] == "region":
                out[kind[1]][1][kind[2]] = f
            else:
                _k, pi, name, page = kind
                regs = out[pi][2].setdefault(page, {})
                if f in (SAME, ADD, None):
                    regs.pop(name, None)
                else:
                    regs[name] = f
        return [(fd, regions, {pg: r for pg, r in cards.items() if r}) for fd, regions, cards in out]
