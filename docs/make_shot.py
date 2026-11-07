# -*- coding: utf-8 -*-
"""生成 README 用的界面截图（离屏渲染，不影响真实运行）"""
import os, sys, tempfile
from PIL import Image, ImageDraw

DESK = r"E:/WorkBuddy/软件开发/局域网文件互传/desktop"
OUT = r"E:/WorkBuddy/软件开发/局域网文件互传/docs"
sys.path.insert(0, DESK)
os.environ["QT_QPA_PLATFORM"] = "windows"
os.environ["LANDROP_DISCOVERY_PORT"] = "24297"

from PySide6.QtWidgets import QApplication
import app as A

os.makedirs(OUT, exist_ok=True)

# 造一张演示图片
img = Image.new("RGB", (560, 360), "#dbeafe")
d = ImageDraw.Draw(img)
d.rectangle([0, 240, 560, 360], fill="#93c5fd")
d.ellipse([380, 40, 470, 130], fill="#fbbf24")
d.rectangle([60, 150, 220, 250], fill="#1e40af")
d.rectangle([90, 180, 190, 210], fill="#e0f2fe")
demo = os.path.join(tempfile.gettempdir(), "landrop_demo_shot.png")
img.save(demo)

qa = QApplication([])
w = A.MainWindow()
try:
    w.core.stop()
except Exception:
    pass

peers = [
    {"id": "p1", "name": "小强的手机", "ip": "192.168.31.57", "os": "Android"},
    {"id": "p2", "name": "客厅笔记本", "ip": "192.168.31.88", "os": "Windows"},
]
w.refresh_peers(peers)
w.lst.item(0).setSelected(True)
w.on_select()

chat = w.chat
chat.append(A.bubble(
    '<b>来自 小强的手机</b><br>会议纪要发你邮箱了，附件记得看一下',
    mine=False, meta="小强的手机 · 20:38"))
chat.append(A.bubble(
    w.file_inner(demo, "设计稿预览.png", "482 KB", saved=False),
    mine=True, meta="你 · 20:41"))
chat.append(A.bubble(
    w.file_inner(os.path.join(tempfile.gettempdir(), "项目周报.pdf"), "项目周报-第45周.pdf", "1.2 MB", saved=True),
    mine=False, meta="小强的手机 · 20:42"))
chat.append(A.bubble(
    '收到，图片直接显示出来了 👍',
    mine=False, meta="小强的手机 · 20:43"))

w.resize(1000, 700)
w.show()
qa.processEvents()
w.grab().save(os.path.join(OUT, "desktop.png"))
print("saved", os.path.join(OUT, "desktop.png"))
