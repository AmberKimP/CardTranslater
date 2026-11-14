"""② 아이콘 정의 창(이름·뜻·세트 정하기)과 아이콘 사전 창."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QHeaderView, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)
from app import service
from app.qt.dialogs import (
    ask_choice, card, fit_to_screen, grey_note, hint_bar, page_title, set_hint, set_ok_text, show_problem, subtitle,
)
from app.qt.icon_place import IconPlaceDialog


# ================================================================ 아이콘 사전 (표)
class IconDictionaryPanel(QWidget):
    """게임 아이콘 사전 표: 한국어 이름·뜻·표시 크기를 고치고, ‘다시 정하기’로 원문에서 [[?]]로 되돌린다.
    아이콘 사전 창(새로 정할 아이콘이 없을 때)과 ② 아이콘 정의 창의 ‘사전 전체’ 탭이 함께 쓴다.
    allow_reset=False: 새 아이콘을 정하는 중에는 ‘다시 정하기’를 막는다 (되돌리면 찾은 결과가 맞지 않게 됨)."""

    COLS = ["그림", "이름", "한국어 이름", "뜻(원문)", "표시 크기", "쓰인 곳", "다시 정하기"]
    UNUSED = "지금 프로젝트에서는 안 쓰임 (다음 프로젝트에서 자동으로 알아봄)"

    def __init__(self, rows: list[dict], intro: str = "", parent=None, allow_reset: bool = True):
        super().__init__(parent)
        self.dirty = False  # 고친 것이 있음 (저장 전)
        # 지금 프로젝트에서 안 쓰인 아이콘은 아래로 (같은 무리 안에서는 받은 순서 그대로). rows()가 이 순서를 따른다
        self._rows = rows = sorted(rows, key=lambda r: not r.get("uses"))
        lab = subtitle(intro)
        lab.setVisible(bool(intro))
        reset_note = grey_note("‘다시 정하기’를 체크하면 그 아이콘을 원문에서 되돌리고, 저장한 뒤 다시 찾아 이름을 정합니다."
                               if allow_reset else
                               "사전은 게임 단위라 고치면 같은 게임의 다른 프로젝트에도 쓰입니다. "
                               "‘다시 정하기’는 새 아이콘 이름을 확정한 뒤 아이콘 사전에서 할 수 있습니다.")
        self.table = QTableWidget(len(rows), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setStyleSheet("QDoubleSpinBox { margin: 7px 6px; padding-top: 1px; padding-bottom: 1px; }"
                                 " QCheckBox { margin-left: 12px; }")
        self.scales: list[QDoubleSpinBox] = []
        self.resets: list[QCheckBox] = []
        for i, r in enumerate(rows):
            unused = not r.get("uses")
            img = QTableWidgetItem()
            if r.get("image"):
                pm = QPixmap(r["image"])
                if not pm.isNull():
                    pm = pm.scaledToHeight(40, Qt.TransformationMode.SmoothTransformation)
                    img.setData(Qt.ItemDataRole.DecorationRole, self._faded(pm) if unused else pm)
            img.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(i, 0, img)
            name = QTableWidgetItem(r["name"])
            name.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(i, 1, name)
            self.table.setItem(i, 2, QTableWidgetItem(r.get("ko", "")))
            self.table.setItem(i, 3, QTableWidgetItem(r.get("meaning", "")))
            sp = QDoubleSpinBox()
            sp.setRange(0.3, 4.0)  # ② 정의 줄(0.3~)과 같게: 작게 정한 크기가 저장할 때 바뀌지 않게
            sp.setSingleStep(0.1)
            sp.setValue(float(r.get("scale") or 1.1))
            sp.setToolTip("한글 글자 높이 대비 아이콘 높이")
            self.table.setCellWidget(i, 4, sp)
            self.scales.append(sp)
            uses = QTableWidgetItem(f"{r.get('uses', 0)}곳" + (f" · {self.UNUSED}" if unused else ""))
            uses.setFlags(Qt.ItemFlag.ItemIsEnabled)
            if unused:
                uses.setToolTip(self.UNUSED)
            self.table.setItem(i, 5, uses)
            if unused:  # 흐리게 (고치기는 그대로 된다)
                for j in (1, 2, 3, 5):
                    self.table.item(i, j).setForeground(QColor("#999"))
            cb = QCheckBox()
            cb.setToolTip("원문의 이 아이콘을 [[?]]로 되돌리고 사전에서 뺍니다. 다시 찾아 새로 묶고 이름을 정합니다")
            cb.setEnabled(allow_reset)
            cb.toggled.connect(self._touch)
            sp.valueChanged.connect(self._touch)
            self.table.setCellWidget(i, 6, cb)
            self.resets.append(cb)
            self.table.setRowHeight(i, 48)
        for i in range(len(rows)):
            self._paint_ko(i)
        self.dirty = False
        self.table.itemChanged.connect(self._item_changed)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(lab)
        lay.addWidget(reset_note)
        lay.addWidget(self.table, 1)

    def _touch(self, *_a):
        self.dirty = True

    def _item_changed(self, it):
        if it.column() == 2:
            self._paint_ko(it.row())
        self.dirty = True

    @staticmethod
    def _faded(pm: QPixmap) -> QPixmap:
        """안 쓰인 아이콘 그림: 흐리게."""
        out = QPixmap(pm.size())
        out.setDevicePixelRatio(pm.devicePixelRatio())
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setOpacity(0.35)
        p.drawPixmap(0, 0, pm)
        p.end()
        return out

    def _paint_ko(self, i: int):
        """한국어 이름이 빈 줄은 노랑 (입력하면 지움)."""
        it = self.table.item(i, 2)
        if it is None:
            return
        self.table.blockSignals(True)
        it.setBackground(QBrush(QColor("#FFE066")) if not it.text().strip() else QBrush())
        self.table.blockSignals(False)

    def rows(self) -> list[dict]:
        return [{"name": r["name"], "ko": self.table.item(i, 2).text().strip(),
                 "meaning": self.table.item(i, 3).text().strip(), "scale": self.scales[i].value(),
                 "reset": self.resets[i].isChecked()} for i, r in enumerate(self._rows)]


class IconDictionaryDialog(QDialog):
    """새로 정할 아이콘이 없을 때 여는 아이콘 사전 창 (표는 IconDictionaryPanel)."""

    COLS = IconDictionaryPanel.COLS
    UNUSED = IconDictionaryPanel.UNUSED

    def __init__(self, rows: list[dict], intro: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("아이콘 사전")
        fit_to_screen(self, 900, 560)
        self.panel = IconDictionaryPanel(rows, intro, self)
        self.table, self.scales, self.resets, self._rows = (self.panel.table, self.panel.scales, self.panel.resets,
                                                            self.panel._rows)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "저장")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(8)
        lay.addWidget(page_title("아이콘 사전", 14))
        lay.addWidget(self.panel, 1)
        lay.addWidget(bb)

    def rows(self) -> list[dict]:
        return self.panel.rows()



class IconReviewDialog(QDialog):
    """새로 찾은 아이콘 묶음의 이름·뜻·한국어 이름을 정한다 (엑셀 대신).

    ‘같은 아이콘’ 칸에서 다른 줄(번호)이나 사전에 있는 아이콘을 고르면 그 아이콘과 하나로 합친다
    (잘못 잘렸거나 작게 찍혀 따로 묶인 것). ‘카드 보기’로 그 아이콘이 나온 카드 자리를 확인하고,
    위치를 못 찾았거나 잘못 잘린 아이콘은 마우스로 직접 감싸 다시 잡는다."""

    COLS = ["그림", "같은 아이콘", "이름", "뜻(원문)", "한국어 이름", "아이콘 아님", "표시 크기", "등장", "카드 번호", "카드", "예문"]
    C_COUNT, C_PAGES, C_VIEW, C_EX = 7, 8, 9, 10
    NEW = "(새 아이콘)"

    def __init__(self, project_dir: Path, review: dict, parent=None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.result = None
        self.setWindowTitle("② 아이콘 정의")
        fit_to_screen(self, 1400, 640, maximize_below=0)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setIconSize(QSize(40, 40))
        self.table.setStyleSheet("QLineEdit, QComboBox, QDoubleSpinBox, QPushButton { margin: 5px 4px; }"
                                 " QLineEdit, QDoubleSpinBox { padding-top: 1px; padding-bottom: 1px; }"
                                 " QCheckBox { margin-left: 12px; }")
        self.table.horizontalHeader().setSectionResizeMode(self.C_EX, QHeaderView.ResizeMode.Stretch)
        self.table.setAutoScroll(False)  # 마우스를 표 아래쪽으로 옮길 때 저절로 내려가지 않게
        self.table.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        # 머리글을 누르면 그 열로 정렬 (다시 누르면 반대로). 줄 번호(‘3번’)는 보이는 순서대로 다시 매긴다
        self._sort: tuple[int, bool] | None = None  # (열, 내림차순)
        self._card_of: dict = {}  # 카드 번호로 정렬했을 때 묶음 -> ((파일, 쪽), 문제 카드인가)
        hh = self.table.horizontalHeader()
        hh.setSectionsClickable(True)
        hh.sectionClicked.connect(self._sort_by)
        self.steps_text = hint_bar("", "info")
        self.known_note = grey_note("")
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "이름 확정")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        # 탭 둘: 새 아이콘 (이름 정하기) / 사전 전체 (이미 정한 아이콘의 한국어 이름·뜻·크기 고치기)
        new_tab = QWidget()
        nl = QVBoxLayout(new_tab)
        nl.setContentsMargins(8, 8, 8, 8)
        nl.setSpacing(8)
        nl.addWidget(self.steps_text)
        nl.addWidget(self.known_note)
        nl.addWidget(self.table, 1)
        try:
            dict_rows = service.icon_dictionary(self.project_dir)
        except Exception:  # noqa: BLE001 - 사전을 못 읽어도 이름 정하기는 된다
            dict_rows = []
        self._dict_images = {r["name"]: r.get("image") for r in dict_rows}
        self.dict_panel = IconDictionaryPanel(dict_rows, "", None, allow_reset=False)
        self.dict_save = QPushButton("사전 저장")
        self.dict_save.setToolTip("이 탭에서 고친 한국어 이름·뜻·표시 크기를 게임 아이콘 사전에 저장합니다")
        self.dict_save.clicked.connect(self.save_dictionary)
        dict_tab = QWidget()
        dl = QVBoxLayout(dict_tab)
        dl.setContentsMargins(8, 8, 8, 8)
        dl.addWidget(self.dict_panel, 1)
        dl.addWidget(self.dict_save, alignment=Qt.AlignmentFlag.AlignRight)
        self.tabs = QTabWidget()
        self.tabs.addTab(new_tab, f"새 아이콘 ({len(review['groups'])})")
        self.tabs.addTab(dict_tab, f"사전 전체 ({len(dict_rows)})")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(8)
        lay.addWidget(page_title("② 아이콘 정의", 14))
        lay.addWidget(self.tabs, 1)
        lay.addWidget(bb)
        self._fill(review, {})

    def save_dictionary(self) -> bool:
        """‘사전 전체’ 탭에서 고친 것을 저장한다 (다시 정하기는 이 탭에서 막혀 있다)."""
        try:
            service.update_icon_dictionary(self.project_dir, self.dict_panel.rows())
        except Exception as e:  # noqa: BLE001
            show_problem(self, "사전을 저장하지 못했습니다", str(e), "")
            return False
        self.dict_panel.dirty = False
        return True

    def _dict_unsaved_ok(self) -> bool:
        """사전 탭에 저장 안 한 고침이 있으면 저장할지 묻는다. 계속해도 되면 True."""
        if not self.dict_panel.dirty:
            return True
        r = ask_choice(self, "아이콘 사전", "‘사전 전체’ 탭에서 고친 것을 저장할까요?",
                       [("저장", "accept"), ("저장 안 함", "destructive"), ("취소", "reject")], default=0)
        if r == 0:
            return self.save_dictionary()
        return r == 1

    def reject(self):
        if self._dict_unsaved_ok():
            super().reject()

    # ---- 표 채우기 (자리를 다시 잡으면 묶음이 바뀌어 다시 채운다; 적어 둔 값은 묶음별로 살린다)
    def _fill(self, review: dict, keep: dict):
        review = {**review, "groups": self._sorted(review["groups"], keep)}
        self.review = review
        groups = review["groups"]
        self.table.clearContents()
        self.table.setRowCount(len(groups))
        self.widgets = []
        for i, g in enumerate(groups):
            img = QTableWidgetItem("" if g["image"] else "위치 못 찾음")
            if g["image"]:
                img.setIcon(QIcon(QPixmap(g["image"])))
            else:
                img.setToolTip("못 찾은 이유: " + (g.get("reason") or "-") + "\n‘카드 보기’에서 아이콘을 끌어 감싸면 잡을 수 있습니다")
                img.setBackground(QBrush(QColor("#FFE066")))
            self.table.setItem(i, 0, img)
            same = QComboBox()
            same.addItem(self.NEW, None)
            for j, other in enumerate(groups):
                if j != i and other["image"]:
                    same.addItem(QIcon(QPixmap(other["image"])), f"{j + 1}번과 같음", ("row", j))
            for nm in review["known"]:
                im = getattr(self, "_dict_images", {}).get(nm)
                if im:  # 사전 그림을 보고 고를 수 있게
                    same.addItem(QIcon(QPixmap(im)), f"사전: {nm}", ("known", nm))
                else:
                    same.addItem(f"사전: {nm}", ("known", nm))
            same.setIconSize(QSize(24, 24))
            name, meaning, ko = QLineEdit(g["suggest"]), QLineEdit(g["meaning"]), QLineEdit(g["ko"])
            name.setPlaceholderText("영문 소문자·숫자·_")
            not_icon = QCheckBox()
            scale = QDoubleSpinBox()
            scale.setRange(0.3, 4.0)
            scale.setSingleStep(0.05)
            scale.setValue(1.1)
            view = QPushButton("카드 보기 · 자리 잡기")  # 찾은 아이콘도 자리를 다시 잡을 수 있다
            view.setToolTip("그 아이콘이 나온 카드를 보고, 위치가 틀리면 마우스로 끌어 다시 잡습니다")
            if not g["image"]:
                view.setStyleSheet("font-weight: bold")
            view.clicked.connect(lambda _c=False, gg=g["group"]: self.show_cards(gg))
            for j, w in ((1, same), (2, name), (3, meaning), (4, ko), (5, not_icon), (6, scale), (self.C_VIEW, view)):
                self.table.setCellWidget(i, j, w)
            cnt = QTableWidgetItem(str(g["count"]) + (f" (세트{g['set_size']})" if g.get("set_size", 1) > 1 else ""))
            if g.get("set_size", 1) > 1:
                cnt.setToolTip(f"붙어 있는 아이콘 {g['set_size']}개가 한 덩어리로 잡혀 하나의 아이콘(세트)으로 봅니다.\n"
                               "확정하면 원문의 붙은 [[?]]가 이 아이콘 하나가 됩니다. 따로 있는 아이콘이면 ‘카드 보기’에서 하나씩 잡으세요.")
            self.table.setItem(i, self.C_COUNT, cnt)
            pages = QTableWidgetItem(self._pages_text(g))
            pages.setToolTip("\n".join(w["label"] for w in g.get("where", [])))
            self.table.setItem(i, self.C_PAGES, pages)
            ex = QTableWidgetItem(g["examples"][0] if g["examples"] else "")
            ex.setToolTip("\n".join(g["examples"]))
            self.table.setItem(i, self.C_EX, ex)
            self.table.setRowHeight(i, 46)
            self.widgets.append((g["group"], name, meaning, ko, not_icon, scale, same))
        row_of = {g["group"]: i for i, g in enumerate(groups)}
        for i, g in enumerate(groups):  # 적어 둔 값 되살리기, 사전과 모양이 같으면 미리 고르기
            _g, name, meaning, ko, not_icon, scale, same = self.widgets[i]
            k = keep.get(g["group"])
            target = None
            if k:
                for edit, key in ((name, "name"), (meaning, "meaning"), (ko, "ko")):
                    if k[key].strip():  # 빈 칸은 새로 받은 제안을 살린다
                        edit.setText(k[key])
                not_icon.setChecked(k["not_icon"])
                scale.setValue(k["scale"])
                if k["same"] and k["same"][0] == "group" and k["same"][1] in row_of:
                    target = ("row", row_of[k["same"][1]])
                elif k["same"] and k["same"][0] == "known":
                    target = k["same"]
            if target is None and g.get("dict") and not (k and k["name"].strip()):
                target = ("known", g["dict"])  # 사전과 모양이 같음 (이름 제안이 사전 이름과 겹쳐 같은 아이콘으로 본 경우 포함)
            if target is not None:
                idx = same.findData(target)
                if idx >= 0:
                    same.setCurrentIndex(idx)
            if g.get("not_icon"):  # 카드 보기에서 ‘이 자리는 아이콘 아님’으로 정한 자리 (적어 둔 값보다 우선)
                not_icon.setChecked(True)
                same.setCurrentIndex(0)
            host = g.get("absorbed_group")
            host_label = (f"{row_of[host] + 1}번" if host in row_of else "") if host else \
                (f"[[{g['absorbed_name']}]]" if g.get("absorbed_name") else "")
            if host_label and g.get("not_icon"):  # 옆 아이콘 상자에 포함된 자리: 원문에서 뺀다
                meaning.clear()
                meaning.setPlaceholderText("")
                cell = self.table.item(i, 0)
                cell.setText(f"{host_label}에 포함")
                cell.setBackground(QBrush(QColor("#E8F0E0")))
                cell.setToolTip(f"{host_label} 아이콘의 그림이 이 자리까지 감싸 하나의 아이콘(세트)으로 합쳤습니다.\n"
                                "확정하면 이 자리의 [[?]]는 원문에서 빠집니다. 따로 있는 아이콘이면 ‘아이콘 아님’을 풀고 자리를 잡으세요.")
            same.currentIndexChanged.connect(lambda _x, r=i: self._sync_row(r))
            not_icon.toggled.connect(lambda _x, r=i: self._sync_row(r))
            name.textChanged.connect(lambda _t: self._paint_dups())
            self._sync_row(i)
        self._paint_dups()
        self.table.resizeColumnToContents(1)
        self.table.resizeColumnToContents(self.C_VIEW)
        for col, w in ((2, 150), (3, 170), (4, 150), (6, 100)):  # 입력 칸 글이 잘리지 않게
            self.table.setColumnWidth(col, w)
        self.table.setColumnWidth(self.C_COUNT, 64)  # 등장 수는 좁게
        self.table.resizeColumnToContents(self.C_PAGES)
        self.table.setColumnWidth(self.C_PAGES, min(max(self.table.columnWidth(self.C_PAGES), 72), 160))
        self._paint_card_bands(groups)
        hh = self.table.horizontalHeader()
        if self._sort:
            hh.setSortIndicatorShown(True)
            hh.setSortIndicator(self._sort[0], Qt.SortOrder.DescendingOrder if self._sort[1]
                                else Qt.SortOrder.AscendingOrder)
        else:
            hh.setSortIndicatorShown(False)
        n_issue = sum(1 for g in groups if g.get("issue"))
        n_lost = sum(1 for g in groups if not g["image"] and not g.get("not_icon"))  # 아이콘 아님으로 정한 자리는 뺀다
        set_hint(self.steps_text,
                 f"정할 아이콘 {len(groups)}개"
                 + (f" · 한국어 이름이 빈 {n_issue}개(노랑)는 맨 위" if n_issue else "")
                 + (f" · 위치 못 찾음 {n_lost}개: ‘카드 보기 · 자리 잡기’에서 아이콘을 끌어 감싸세요" if n_lost else "")
                 + "\n"
                 "1. 그림을 보고 ‘이름’(영문 소문자)과 ‘한국어 이름’을 채우세요.\n"
                 "2. 같은 아이콘이 따로 묶였으면 ‘같은 아이콘’에서 그 줄 번호를 고르세요.\n"
                 "3. 아이콘이 아니라 글자면 ‘아이콘 아님’을 체크하고 ‘뜻’ 칸에 그 글자를 적으세요 (비우면 원문에서 뺌).\n"
                 "4. 그림이 잘못 잘렸으면 ‘카드 보기’에서 다시 잡으세요.", "info")
        self.known_note.setText("사전에 이미 있는 이름: " + (", ".join(review["known"]) or "(없음)"))
        for w in self.widgets:  # 한국어 이름이 빈 줄은 노랑
            self._paint_ko_edit(w[3])
            w[3].textChanged.connect(lambda _t, e=w[3]: self._paint_ko_edit(e))

    def _capture(self) -> dict:
        """지금 표에 적힌 값 (묶음별). ‘같은 아이콘’의 줄 번호는 묶음 이름으로 바꿔 둔다."""
        out = {}
        for g, name, meaning, ko, not_icon, scale, same in self.widgets:
            d = same.currentData()
            if d and d[0] == "row":
                d = ("group", self.widgets[d[1]][0])
            out[g] = {"name": name.text(), "meaning": meaning.text(), "ko": ko.text(),
                      "not_icon": not_icon.isChecked(), "scale": scale.value(), "same": d}
        return out

    def _row_message(self, msg: str) -> str:
        """엔진의 ‘묶음: 문제’를 ‘문제: 3번 줄’로 (묶음 이름 ‘위치 못 찾음 o014’는 표에 보이지 않는다;
        자세히 보기는 문제별로 묶어 줄 번호를 나열한다)."""
        for i, w in enumerate(self.widgets):
            if msg.startswith(w[0] + ": "):
                rest = msg[len(w[0]) + 2:].replace("처리 열에 ‘아이콘 아님’", "‘아이콘 아님’ 체크")
                return f"{rest}: {i + 1}번 줄"
        return msg

    def _row_label(self, group: str) -> str:
        """카드 보기에 보일 이름: ‘14번’, 이름을 적어 둔 줄이면 ‘14번 crown’."""
        i = next((i for i, w in enumerate(self.widgets) if w[0] == group), None)
        if i is None:
            return "아이콘"
        _g, name, _m, _k, not_icon, _sc, same = self.widgets[i]
        d = same.currentData()
        nm = d[1] if d and d[0] == "known" else (
            self.widgets[d[1]][1].text().strip() if d and d[0] == "row" else name.text().strip())
        return f"{i + 1}번" + (f" {nm}" if nm and not not_icon.isChecked() else "")

    @staticmethod
    def _pages_text(g: dict) -> str:
        """그 아이콘이 나온 카드 번호 (쪽). 파일이 여럿이면 ‘파일:쪽’."""
        where = g.get("where", [])
        files = {w.get("file_dir") for w in where}
        seen, out = set(), []
        for w in where:
            t = f"{Path(str(w.get('file_dir'))).name[:12]}:{w['page']}" if len(files) > 1 else str(w["page"])
            if t not in seen:
                seen.add(t)
                out.append(t)
        return ", ".join(out)

    # ---- 정렬 (머리글 누르기)
    def _sort_by(self, col: int):
        if col == self.C_VIEW:  # 버튼 열은 정렬하지 않는다
            return
        desc = bool(self._sort and self._sort[0] == col and not self._sort[1])
        self._sort = (col, desc)
        self._fill(self.review, self._capture())

    def _sorted(self, groups: list[dict], keep: dict) -> list[dict]:
        if not self._sort:
            self._card_of = {}
            return list(groups)
        col, desc = self._sort
        if col == self.C_PAGES:
            return self._sorted_by_card(groups, keep, desc)
        self._card_of = {}

        def key(g):
            k = keep.get(g["group"], {})
            txt = {2: k.get("name", g["suggest"]), 3: k.get("meaning", g["meaning"]), 4: k.get("ko", g["ko"])}
            if col == 0:
                return (0 if g["image"] else 1,)
            if col == 1:
                same = k.get("same")
                return (0 if not same else 1, str(same[1]) if same else "")
            if col in txt:
                v = (txt[col] or "").strip().lower()
                return (v == "", v)  # 빈 칸은 뒤로
            if col == 5:
                return (bool(k.get("not_icon", g.get("not_icon"))),)
            if col == 6:
                return (k.get("scale", 1.1),)
            if col == self.C_COUNT:
                return (g["count"],)
            return ((g["examples"][0] if g["examples"] else ""),)

        out = sorted(groups, key=key, reverse=desc)
        if desc:  # 빈 칸은 내림차순에서도 뒤로
            out = [g for g in out if key(g)[0] is not True] + [g for g in out if key(g)[0] is True] \
                if col in (2, 3, 4) else out
        return out

    @staticmethod
    def _cards(g: dict) -> list[tuple[str, int]]:
        """그 아이콘이 나온 카드 (파일, 쪽) — 파일·쪽 순, 겹치지 않게."""
        return sorted({(str(w.get("file_dir") or ""), int(w["page"])) for w in g.get("where", [])})

    @staticmethod
    def _is_problem(g: dict, k: dict) -> bool:
        """확정 전에 손봐야 하는 줄: 위치 못 찾음·이름 빈 칸·한국어 이름 빈 칸 (‘아이콘 아님’이면 아님)."""
        if k.get("not_icon", g.get("not_icon")):
            return False
        same = k.get("same")
        named = bool(str(k.get("name", g.get("suggest") or "")).strip()) or bool(same)
        ko = str(k.get("ko", g.get("ko") or "")).strip()  # 화면에 적은 한국어 이름 (엔진의 issue는 처음 제안 기준)
        return not g.get("image") or not named or not ko

    def _sorted_by_card(self, groups: list[dict], keep: dict, desc: bool) -> list[dict]:
        """카드 번호로 묶어 정렬: 줄마다 첫 카드 아래에 두고, 문제 줄이 있는 카드를 맨 위로 (그 안에서도 문제 줄 먼저).
        내림차순은 카드 순서만 뒤집는다 (문제 카드는 그대로 위). 화면 띠 색을 위해 self._card_of에 줄의 카드를 남긴다."""
        first = {g["group"]: (self._cards(g) or [("", 0)])[0] for g in groups}
        bad = {g["group"]: self._is_problem(g, keep.get(g["group"], {})) for g in groups}
        bad_cards = {first[g] for g, b in bad.items() if b}
        order = sorted(set(first.values()), reverse=desc)
        rank = {c: i for i, c in enumerate(order)}
        out = sorted(groups, key=lambda g: (first[g["group"]] not in bad_cards, rank[first[g["group"]]],
                                            not bad[g["group"]], -g["count"]))
        self._card_of = {g["group"]: (first[g["group"]], first[g["group"]] in bad_cards) for g in out}
        return out

    def _paint_card_bands(self, groups: list[dict]) -> None:
        """카드 번호로 정렬했을 때: 같은 카드의 줄은 같은 띠 색 (문제 카드는 붉은 띠), 카드 번호 칸은 그 카드를 앞에."""
        if not self._card_of:
            return
        files = {c[0] for g in groups for c in self._cards(g)}
        prev, band = None, 0
        for i, g in enumerate(groups):
            card, bad = self._card_of[g["group"]]
            if card != prev:
                band ^= 1
                prev = card
            color = ("#FBE3E3" if band else "#F6D0D0") if bad else ("#EEF3FA" if band else "#FFFFFF")
            label = (f"{Path(card[0]).name[:12]}:{card[1]}" if len(files) > 1 else str(card[1])) if card[1] else ""
            rest = [t for t in self._pages_text(g).split(", ") if t and t != label]
            it = self.table.item(i, self.C_PAGES)
            it.setText(label + (f" (또 {', '.join(rest)})" if rest else ""))
            if bad:
                it.setToolTip("이 카드에 확정 전에 손봐야 할 아이콘이 있습니다 (위치 못 찾음·이름 빈 칸)\n" + it.toolTip())
            for col in (self.C_COUNT, self.C_PAGES, self.C_EX):
                cell = self.table.item(i, col)
                if cell is not None:
                    cell.setBackground(QBrush(QColor(color)))

    def select_group(self, group: str | None) -> None:
        """그 묶음의 줄을 골라 보이게 한다 (카드 보기를 닫은 뒤)."""
        i = next((i for i, w in enumerate(self.widgets) if w[0] == group), None) if group else None
        if i is None:
            return
        self.table.setCurrentCell(i, 2)
        self.table.selectRow(i)
        self.table.scrollToItem(self.table.item(i, 0), QTableWidget.ScrollHint.EnsureVisible)
        self.widgets[i][1].setFocus()

    def _after_place(self, r: dict) -> bool:
        """카드 보기에서 바꾼 뒤 표를 다시 채운다. 돌려줌: 새 줄이 예전 줄의 이름을 이어받았는가."""
        keep = self._capture()
        inherited = False
        if r.get("new_group") and r.get("old") in keep:
            # 다시 잡아 새 줄이 생김: 같은 아이콘의 자리를 고친 것이므로 예전 줄에 적어 둔 값을 이어받는다
            # (혼자였던 줄은 옮기고, 묶음에 남은 자리가 있으면 복사)
            if r.get("old_gone"):
                k = keep.pop(r["old"])
            else:  # 예전 줄에 다른 자리가 남음: 이름을 복사하면 같은 이름 두 줄이 된다 → ‘같은 아이콘: 예전 줄’로
                k = {**keep[r["old"]], "name": "", "meaning": "", "ko": "", "same": ("group", r["old"])}
            keep[r["group"]] = k
            inherited = bool(str(k.get("name", "")).strip()) or bool(k.get("same"))
            if r.get("dict"):
                keep[r["group"]]["same"] = ("known", r["dict"])
        sg = r.get("suggested")
        if sg in keep and r.get("suggest"):  # 카드 보기에서 새로 받은 제안 (세트로 합쳐 그림이 바뀜 등): 예전 값 대신
            keep[sg].update(name="", meaning="", ko="", same=None)
        review = service.icon_review(self.project_dir)
        if review:
            self._fill(review, keep)
        return inherited

    @staticmethod
    def _paint_ko_edit(edit: QLineEdit):
        edit.setStyleSheet("" if edit.text().strip() else "background: #FFE066")

    def _sync_row(self, r: int):
        _g, name, meaning, ko, not_icon, scale, same = self.widgets[r]
        merged = same.currentData() is not None
        for w in (name, ko, scale):
            w.setEnabled(not merged and not not_icon.isChecked())
        # 아이콘 아님: ‘뜻’ 칸에 그 자리 글자를 적는다 (적으면 [[?]]를 그 글자로, 비우면 뺀다)
        meaning.setEnabled(not merged)
        groups = getattr(self, "review", {}).get("groups") or []
        suggested = (groups[r].get("meaning") or "") if r < len(groups) else ""
        if not_icon.isChecked() and suggested and meaning.text().strip() == suggested.strip():
            # 모델이 제안한 아이콘 뜻은 그 자리 글자가 아니다: 비워 둔다 (그대로 두면 원문에 뜻 문장이 글자로 들어감)
            meaning.setProperty("stashed", meaning.text())
            meaning.setText("")
        elif not not_icon.isChecked() and meaning.property("stashed") and not meaning.text().strip():
            meaning.setText(meaning.property("stashed"))  # 다시 아이콘으로 하면 제안 뜻을 되살린다
            meaning.setProperty("stashed", "")
        if not_icon.isChecked():
            meaning.setPlaceholderText("그 자리 글자 (예: 最). 비우면 원문에서 뺌")
            meaning.setToolTip("아이콘이 아니라 글자를 잘못 읽은 것이면 여기에 그 글자를 적으세요")
        else:
            meaning.setPlaceholderText("")
            meaning.setToolTip("")
        same.setEnabled(not not_icon.isChecked())
        not_icon.setEnabled(not merged)
        self._paint_lost(r)
        if hasattr(self, "widgets") and len(self.widgets) == self.table.rowCount():
            self._paint_dups()

    LOST_COLOR, LOST_DONE_COLOR = "#FFE066", "#E0E0E0"  # 위치 못 찾음: 손볼 것(노랑) / 아이콘 아님으로 정함(회색)

    def _paint_lost(self, r: int) -> None:
        """‘위치 못 찾음’ 칸 색: 아이콘 아님을 체크하면 더 손볼 것이 없으므로 회색 (옆 아이콘에 포함된 칸은 그대로)."""
        cell = self.table.item(r, 0)
        groups = getattr(self, "review", {}).get("groups") or []
        if cell is None or r >= len(groups) or groups[r]["image"] or cell.text() != "위치 못 찾음":
            return
        done = self.widgets[r][4].isChecked()
        cell.setBackground(QBrush(QColor(self.LOST_DONE_COLOR if done else self.LOST_COLOR)))
        cell.setForeground(QColor("#777") if done else QColor("black"))

    def _dup_names(self) -> dict[str, list[int]]:
        """‘(새 아이콘)’ 줄끼리 이름이 같은 것 {이름: [줄 번호]}. 확정하면 첫 줄 그림 하나로 합쳐진다."""
        by: dict[str, list[int]] = {}
        for i, (_g, name, _m, _k, not_icon, _sc, same) in enumerate(self.widgets):
            nm = name.text().strip()
            if nm and same.currentData() is None and not not_icon.isChecked():
                by.setdefault(nm, []).append(i)
        return {nm: rows for nm, rows in by.items() if len(rows) > 1}

    def _paint_dups(self):
        """이름이 같은 새 아이콘 줄은 주황으로 (다른 아이콘인데 같은 이름이면 하나로 합쳐져 그림이 바뀐다)."""
        dups = {i: rows for rows in self._dup_names().values() for i in rows}
        for i, (_g, name, *_rest) in enumerate(self.widgets):
            rows = dups.get(i)
            if rows:
                others = ", ".join(f"{j + 1}번" for j in rows if j != i)
                name.setStyleSheet("background: #F4B183")
                name.setToolTip(f"{others}과 이름이 같습니다. 확정하면 하나의 아이콘(첫 줄 그림)으로 합쳐집니다.\n"
                                "다른 아이콘이면(예: 숫자가 붙은 금화) 이름을 바꾸세요 (예: gold_2).")
            else:
                name.setStyleSheet("")
                name.setToolTip("")

    def show_cards(self, group: str):
        """그 묶음이 나온 카드 자리를 보여 주고, 자리를 직접 잡게 한다."""
        g = next((x for x in self.review["groups"] if x["group"] == group), None)
        if g is None:
            return
        order = [w[0] for w in self.widgets]
        nxt = order[order.index(group) + 1] if group in order and order.index(group) + 1 < len(order) else None
        dlg = IconPlaceDialog(self.project_dir, g, self._row_label, self._after_place, self)
        dlg.exec()
        act, where = dlg.outcome
        if act == "not_icon":  # 다음 줄로 (아이콘 아님으로 뺀 줄은 끝났으므로)
            self.select_group(nxt if nxt in [w[0] for w in self.widgets] else where)
        elif act == "saved":  # 저장한 자리가 들어간 줄 (같은 모양 묶음이나 새 줄)
            self.select_group(where)

    def rows(self) -> list[dict]:
        """합치기를 풀어 각 줄의 최종 이름으로. 합치기가 돌고 돌면 ValueError."""
        final: dict[int, str] = {}

        def resolve(i: int, seen: tuple = ()) -> str:
            if i in final:
                return final[i]
            if i in seen:
                raise ValueError(f"{i + 1}번 줄의 ‘같은 아이콘’이 서로를 가리킵니다")
            _g, name, _m, _k, _ni, _sc, same = self.widgets[i]
            d = same.currentData()
            if d is None:
                v = name.text().strip()
            elif d[0] == "known":
                v = d[1]
            else:
                if self.widgets[d[1]][4].isChecked():
                    raise ValueError(f"{i + 1}번 줄이 ‘아이콘 아님’인 {d[1] + 1}번과 같다고 되어 있습니다")
                v = resolve(d[1], seen + (i,))
            final[i] = v
            return v

        def ends_in_dict(i: int, seen: tuple = ()) -> bool:
            """‘같은 아이콘’을 따라가면 사전 아이콘에 닿는가 (‘3번과 같음’ → 3번이 ‘사전: crown’)."""
            d = self.widgets[i][6].currentData()
            if d is None or i in seen:
                return False
            return d[0] == "known" or (d[0] == "row" and ends_in_dict(d[1], seen + (i,)))

        out = []
        for i, (g, n, m, k, ni, sc, same) in enumerate(self.widgets):
            target = same.currentData()
            src = self.widgets[target[1]] if target and target[0] == "row" else None
            if not ni.isChecked() and ends_in_dict(i):  # 사전 아이콘과 같음: 사전 값은 그대로 둔다
                out.append({"group": g, "name": resolve(i), "meaning": "", "ko": "", "not_icon": False, "text": "",
                            "scale": None, "keep_dict": True})
                continue
            out.append({"group": g, "name": "" if ni.isChecked() else resolve(i),
                        "meaning": "" if ni.isChecked() else (src[2] if src else m).text().strip(),
                        "ko": (src[3] if src else k).text().strip(),
                        "not_icon": ni.isChecked(), "text": m.text().strip() if ni.isChecked() else "",
                        "scale": (src[5] if src else sc).value()})
        return out

    def accept(self):
        if not self._dict_unsaved_ok():  # 사전 탭에서 고친 것 먼저 (확정이 사전을 다시 쓰므로)
            return
        try:
            rows = self.rows()
        except ValueError as e:
            show_problem(self, "아이콘 이름을 확정하지 못했습니다", str(e), "표시된 줄의 이름을 고친 뒤 다시 누르세요.")
            return
        dups = self._dup_names()
        if dups:
            lines = [f"• {nm}: " + ", ".join(f"{i + 1}번" for i in rows) for nm, rows in dups.items()]
            r = ask_choice(self, "같은 이름의 아이콘", "다른 줄인데 이름이 같은 아이콘이 있습니다 (주황):\n" + "\n".join(lines),
                           [("같은 아이콘이니 합치기", "accept"), ("돌아가서 이름 고치기", "reject")], default=1,
                           informative="합치면 하나의 아이콘이 되어 첫 줄의 그림만 씁니다. 숫자가 붙은 금화처럼 그림이 다르면 "
                                       "이름을 다르게 하세요 (예: gold, gold_2).")
            if r != 0:
                return
        r = service.confirm_icons(self.project_dir, rows)
        if r.state != "confirmed":
            show_problem(self, "아이콘 이름을 확정하지 못했습니다", "", "이름을 고친 뒤 다시 누르세요.",
                         items=[self._row_message(m) for m in r.blocking])
            return
        self.result = r
        super().accept()
