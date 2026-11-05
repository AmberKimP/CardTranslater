"""시작할 때 로그인 방식 고르기: Claude 구독(Claude Code)으로 로그인 / API 키 사용."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton,
    QVBoxLayout,
)

from app import service
from app.qt import worker
from app.qt.dialogs import show_error, show_failure, show_problem


# 시작 화면 그림 (exe에서는 assets/가 함께 묶인다: packaging/build.py)
LOGIN_IMAGE = Path(__file__).resolve().parent.parent.parent / "assets" / "login.jpg"


def login_banner(width: int) -> QLabel | None:
    """로그인 창 위의 그림. 화면이 낮으면 높이를 줄인다. 그림 파일이 없으면 None."""
    if not LOGIN_IMAGE.exists():
        return None
    pm = QPixmap(str(LOGIN_IMAGE))
    if pm.isNull():
        return None
    scr = QGuiApplication.primaryScreen()
    max_h = int(scr.availableGeometry().height() * 0.42) if scr else 420
    pm = pm.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
    if pm.height() > max_h:
        pm = pm.scaledToHeight(max_h, Qt.TransformationMode.SmoothTransformation)
    lab = QLabel()
    lab.setPixmap(pm)
    lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return lab


class LoginDialog(QDialog):
    """고른 방식은 설정(엔진)에 저장한다. API 키는 확인한 뒤 이 컴퓨터에 암호화해 저장한다."""

    def __init__(self, parent=None, first_run: bool = True):
        super().__init__(parent)
        self.setWindowTitle("CardTranslater — 로그인")
        self.setMinimumWidth(620)
        self.group = QButtonGroup(self)
        self.r_claude = QRadioButton("Claude로 로그인 (Claude 구독 · Claude Code)")
        self.r_api = QRadioButton("API 키 사용 (Anthropic API · 사용한 만큼 요금)")
        for r in (self.r_claude, self.r_api):
            f = r.font()
            f.setBold(True)
            r.setFont(f)
            self.group.addButton(r)

        # ---- Claude 구독
        self.claude_status = QLabel()
        self.claude_status.setWordWrap(True)
        self.b_check = QPushButton("로그인 확인")
        self.b_check.setToolTip("아주 짧은 요청을 한 번 보내 로그인 상태를 확인합니다 (사용량이 조금 듭니다)")
        self.b_check.clicked.connect(self.check_claude)
        self.b_login = QPushButton("로그인 창 열기")
        self.b_login.setToolTip("새 창에서 claude를 띄웁니다. /login 을 입력해 로그인한 뒤 ‘로그인 확인’을 누르세요")
        self.b_login.clicked.connect(self.open_claude_login)
        crow = QHBoxLayout()
        crow.addWidget(self.b_check)
        crow.addWidget(self.b_login)
        crow.addStretch(1)

        # ---- API 키
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("sk-ant-… (붙여 넣기)")
        self.b_save = QPushButton("확인하고 저장")
        self.b_save.clicked.connect(self.save_key)
        self.b_clear = QPushButton("저장한 키 지우기")
        self.b_clear.clicked.connect(self.clear_key)
        self.api_status = QLabel()
        self.api_status.setWordWrap(True)
        krow = QHBoxLayout()
        krow.addWidget(self.key, 1)
        krow.addWidget(self.b_save)
        krow.addWidget(self.b_clear)

        note = QLabel("키는 이 컴퓨터의 이 사용자만 풀 수 있게 암호화해 저장합니다 (소스 코드·프로젝트 폴더에는 남지 않음).")
        note.setStyleSheet("color: #666")
        note.setWordWrap(True)

        def box(*widgets_or_layouts):
            fr = QFrame()
            fr.setFrameShape(QFrame.Shape.StyledPanel)
            lay = QVBoxLayout(fr)
            for w in widgets_or_layouts:
                (lay.addLayout if isinstance(w, QHBoxLayout) else lay.addWidget)(w)
            return fr

        self.start = QPushButton("시작" if first_run else "확인")
        self.start.setDefault(True)
        self.start.clicked.connect(self.accept)
        quit_ = QPushButton("끝내기" if first_run else "취소")
        quit_.clicked.connect(self.reject)
        brow = QHBoxLayout()
        brow.addStretch(1)
        brow.addWidget(self.start)
        brow.addWidget(quit_)

        title = QLabel("어떤 방식으로 Claude를 쓸까요?")
        f = title.font()
        f.setPointSize(f.pointSize() + 3)
        f.setBold(True)
        title.setFont(f)
        lay = QVBoxLayout(self)
        banner = login_banner(620) if first_run else None  # 프로그램을 시작할 때만
        if banner is not None:
            lay.addWidget(banner)
        self.banner = banner
        lay.addWidget(title)
        lay.addWidget(box(self.r_claude, self.claude_status, crow))
        lay.addWidget(box(self.r_api, krow, self.api_status, note))
        lay.addLayout(brow)

        (self.r_api if service.get_settings().engine == "api" else self.r_claude).setChecked(True)
        self.group.buttonToggled.connect(lambda *_a: self._sync())
        self._refresh_claude(service.claude_login_status(check=False))
        self._refresh_api()
        self._sync()

    def engine(self) -> str:
        return "api" if self.r_api.isChecked() else "claude-code"

    # ---- 표시
    def _refresh_claude(self, st: dict):
        mark = {True: "● ", False: "✕ ", None: "○ "}[st["logged_in"]]
        color = {True: "#2e7d32", False: "#c62828", None: "#666"}[st["logged_in"]]
        self.claude_status.setText(mark + st["message"])
        self.claude_status.setStyleSheet(f"color: {color}")
        self.b_check.setEnabled(st["installed"])
        self._claude = st

    def _refresh_api(self):
        info = service.api_key_info()
        if info["has_key"]:
            self.api_status.setText(f"● 키 있음: {info['masked']} ({info['source']})")
            self.api_status.setStyleSheet("color: #2e7d32")
        else:
            self.api_status.setText("○ 저장한 키가 없습니다")
            self.api_status.setStyleSheet("color: #666")
        self.b_clear.setEnabled(info["source"] == "저장한 키")
        self._api = info

    def _sync(self):
        api = self.r_api.isChecked()
        for w in (self.key, self.b_save, self.b_clear, self.api_status):
            w.setEnabled(api if w is not self.b_clear else api and self._api["source"] == "저장한 키")
        for w in (self.b_check, self.b_login, self.claude_status):
            w.setEnabled(not api and (w is not self.b_check or self._claude["installed"]))

    # ---- 동작
    def check_claude(self):
        job = worker.Job(service.claude_login_status, check=True, with_progress=False)
        worker.run_with_dialog(self, "Claude 로그인 확인 중", job, self._refresh_claude,
                               lambda m: show_failure(self, "로그인을 확인하지 못했습니다", m), cancellable=False)

    def open_claude_login(self):
        try:
            service.launch_claude_login()
        except OSError as e:
            show_problem(self, "로그인 창을 열지 못했습니다", str(e),
                         "Claude Code가 설치되어 있는지 확인하세요. 아니면 ‘API 키 사용’을 고르세요.")
            return
        self.claude_status.setText("○ 새 창에서 /login 으로 로그인한 뒤 ‘로그인 확인’을 누르세요")

    def save_key(self):
        key = self.key.text().strip()
        if not key:
            QMessageBox.information(self, "API 키", "키를 붙여 넣으세요")
            return
        job = worker.Job(service.set_api_key, key, with_progress=False)
        worker.run_with_dialog(self, "API 키 확인 중", job, self._key_saved,
                               lambda m: show_failure(self, "API 키를 확인하지 못했습니다", m), cancellable=False)

    def _key_saved(self, r):
        ok, msg = r
        if ok:
            QMessageBox.information(self, "API 키", msg)
        else:
            show_problem(self, "API 키를 저장하지 못했습니다", msg,
                         "키를 처음부터 끝까지 다시 복사해 붙여 넣고 ‘확인하고 저장’을 누르세요.")
        if ok:
            self.key.clear()
        self._refresh_api()
        self._sync()

    def clear_key(self):
        if QMessageBox.question(self, "API 키", "이 컴퓨터에 저장한 API 키를 지울까요?") == QMessageBox.StandardButton.Yes:
            service.clear_api_key()
            self._refresh_api()
            self._sync()

    def accept(self):
        eng = self.engine()
        if eng == "api" and not self._api["has_key"]:
            show_error(self, "API 키가 없습니다", "‘API 키 사용’을 골랐지만 저장한 키가 없습니다.",
                       "키를 붙여 넣고 ‘확인하고 저장’을 누르세요. 아니면 ‘Claude로 로그인’을 고르세요.")
            return
        if eng == "claude-code" and not self._claude["installed"]:
            if QMessageBox.question(self, "Claude Code 없음",
                                    "Claude Code가 설치되어 있지 않아 추출·번역을 할 수 없습니다. 그래도 시작할까요?\n"
                                    "(영역 지정·지우기·한글 넣기·결과 보기는 됩니다)") != QMessageBox.StandardButton.Yes:
                return
        service.set_engine(eng)
        super().accept()


def ask_login(parent=None, first_run: bool = True) -> bool:
    return LoginDialog(parent, first_run).exec() == QDialog.DialogCode.Accepted

