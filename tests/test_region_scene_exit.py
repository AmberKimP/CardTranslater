"""영역 지정(0F) 장면을 누른 뒤 프로세스가 깨끗이 끝나는지 (예전: 쪽 그림에 parentItem()을 불러 힙이 깨지고
끝날 때 0xC0000374로 죽었다 — 테스트는 다 통과해도 pytest가 실패 코드로 끝남). 따로 띄운 프로세스의 종료 코드로 본다."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r'''
import sys
sys.path.insert(0, {root!r})
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsView
app = QApplication([])
from app.qt.region_editor import PageScene, RegionItem
sc = PageScene(); v = QGraphicsView(sc)
pm = QPixmap(400, 300); pm.fill(QColor("white"))
pi = sc.addPixmap(pm); pi.setZValue(-1)
sc.page_rect = QRectF(0, 0, 400, 300)
it = RegionItem(QRectF(200, 50, 100, 60), {{"name": "본문", "kind": "text"}})
sc.addItem(it)
v.resize(500, 400); v.show(); app.processEvents()
for x, y in ((50, 60), (250, 80), (50, 200)):  # 쪽 그림, 영역, 다시 쪽 그림
    QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, v.mapFromScene(QPointF(x, y)))
    app.processEvents()
assert sc.region_at(QPointF(250, 80)) is it and sc.region_at(QPointF(50, 60)) is None
v.close()
print("OK")
'''


def test_clicking_region_scene_then_exit_does_not_crash(tmp_path):
    script = tmp_path / "scene_click.py"
    script.write_text(SCRIPT.format(root=str(ROOT)), encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=60)
    assert r.returncode == 0 and "OK" in r.stdout, (r.returncode, r.stdout[-500:], r.stderr[-1500:])
