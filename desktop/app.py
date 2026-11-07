#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LAN Drop 桌面版 —— 局域网跨平台文件快传
=====================================
双击运行即为独立软件：自动发现局域网内同样运行本软件的设备，
点选设备后发送文件或文字，点对点直传，无需浏览器、无需服务器。

依赖：PySide6（界面）+ 标准库（网络）。已打包版本无需安装 Python。
"""

import base64
import html
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

from PySide6.QtCore import Qt, QTimer, QSize, QUrl, QEvent, QBuffer, QByteArray
from PySide6.QtGui import (
    QAction, QDesktopServices, QFont, QIcon, QImage, QKeySequence, QPixmap, QColor,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QSizePolicy, QTextBrowser, QVBoxLayout, QWidget,
)
from PySide6.QtWidgets import QAbstractItemView

import core

APP_NAME = "LAN Drop"
APP_VER = "2.2"
APP_AUTHOR = "scivi"
APP_COPYRIGHT = "© 2026 scivi · 保留所有权利"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".landrop_desktop.json")
DEFAULT_DEST = os.path.join(os.path.expanduser("~"), "Downloads", "LAN Drop 接收")
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tif", ".tiff"}


def is_image_file(path):
    return os.path.splitext(path)[1].lower() in IMG_EXT


def thumb_data_uri(path, max_w=300, max_h=220):
    """生成缩略图的 data URI（线程安全：只用 QImage，不碰 QPixmap）。

    返回 (data_uri, 宽, 高)；不是图片或读取失败返回 None。
    """
    try:
        img = QImage(path)
        if img.isNull() or img.width() <= 0:
            return None
        w, h = img.width(), img.height()
        scale = min(1.0, max_w / float(w), max_h / float(h))
        if scale < 1.0:
            img = img.scaled(max(1, int(w * scale)), max(1, int(h * scale)),
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)
        ba = QByteArray()
        buf = QBuffer(ba)
        if not buf.open(QBuffer.WriteOnly):
            return None
        if img.hasAlphaChannel():
            ok = img.save(buf, "PNG")
            mime = "image/png"
        else:
            ok = img.save(buf, "JPG", 85)
            mime = "image/jpeg"
        buf.close()
        if not ok:
            return None
        data = bytes(ba)
        if not data:
            return None
        return f"data:{mime};base64,{base64.b64encode(data).decode()}", img.width(), img.height()
    except Exception:
        return None

OS_EMOJI = {"Windows": "💻", "Mac": "🖥️", "Linux": "🐧", "iPhone": "📱", "iPad": "📱", "Android": "📱"}


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
    except Exception:
        pass


def bubble(text_html, mine=False, meta=""):
    bg = "#2563eb" if mine else "#ffffff"
    fg = "#ffffff" if mine else "#111827"
    border = "none" if mine else "1px solid #e6eaf2"
    align = "right" if mine else "left"
    return (
        f'<table width="100%" cellspacing="0" cellpadding="0" style="margin:4px 0;">'
        f'<tr><td align="{align}">'
        f'<div style="font-size:11px;color:#8b95a5;margin-bottom:2px;">{meta}</div>'
        f'<table cellspacing="0" cellpadding="9" style="background:{bg};border:{border};'
        f'border-radius:10px;">'
        f'<tr><td style="color:{fg};font-size:13px;">{text_html}</td></tr></table>'
        f'</td></tr></table>'
    )


class ChatBrowser(QTextBrowser):
    """消息区：所有链接一律交给 anchorClicked 处理，禁止窗口内导航。

    否则点击 file:// 链接会把文件内容当作文档加载进聊天区，
    二进制文件（图片等）会显示成满屏乱码。
    """

    def setSource(self, url, *args, **kwargs):
        pass

    def doSetSource(self, url, *args, **kwargs):
        pass


class DestDialog(QDialog):
    """接收文件保存位置设置：选择目录 / 打开 / 恢复默认。"""

    def __init__(self, current, parent=None):
        super().__init__(parent)
        self.setWindowTitle("接收文件保存位置")
        self.setMinimumWidth(560)
        self.value = current
        self.setStyleSheet("""
            QWidget { background:#f4f6fb; color:#111827;
                      font-family:'Microsoft YaHei','PingFang SC',sans-serif; }
            QLabel { font-size:12.5px; }
            QLineEdit { background:#ffffff; border:1px solid #e3e8f0; border-radius:8px;
                        padding:7px 10px; font-size:12px; color:#111827; }
            QPushButton { background:#2563eb; color:#ffffff; border:none; border-radius:8px;
                          padding:7px 14px; font-size:12.5px; }
            QPushButton:hover { background:#1d4ed8; }
            QPushButton#ghost { background:#eef2f8; color:#374151; }
            QPushButton#ghost:hover { background:#e3e9f4; }
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(9)

        tip = QLabel("收到的文件将保存到这个文件夹：")
        lay.addWidget(tip)

        row = QHBoxLayout()
        row.setSpacing(7)
        self.ed = QLineEdit(current)
        self.ed.setReadOnly(True)
        btn_pick = QPushButton("更改…")
        btn_pick.setObjectName("ghost")
        btn_pick.clicked.connect(self.pick)
        row.addWidget(self.ed, 1)
        row.addWidget(btn_pick)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(7)
        btn_open = QPushButton("打开文件夹")
        btn_open.setObjectName("ghost")
        btn_open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.ed.text())))
        btn_def = QPushButton("恢复默认")
        btn_def.setObjectName("ghost")
        btn_def.clicked.connect(lambda: self.ed.setText(DEFAULT_DEST))
        row2.addWidget(btn_open)
        row2.addWidget(btn_def)
        row2.addStretch(1)
        lay.addLayout(row2)

        hint = QLabel("提示：路径可放在任意磁盘或共享目录；保留空目录会被自动创建。")
        hint.setStyleSheet("color:#8b95a5;font-size:11px;")
        lay.addWidget(hint)

        acts = QHBoxLayout()
        acts.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.setObjectName("ghost")
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("保存")
        btn_ok.clicked.connect(self.accept)
        acts.addWidget(btn_cancel)
        acts.addWidget(btn_ok)
        lay.addLayout(acts)

    def pick(self):
        folder = QFileDialog.getExistingDirectory(self, "选择接收文件夹", self.ed.text())
        if folder:
            self.ed.setText(os.path.normpath(folder))

    def accept(self):
        path = self.ed.text().strip()
        if not path:
            QMessageBox.information(self, APP_NAME, "请选择一个文件夹")
            return
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"无法使用该文件夹：{e}")
            return
        if not os.access(path, os.W_OK):
            QMessageBox.warning(self, APP_NAME, "该文件夹没有写入权限，请换一个")
            return
        self.value = path
        super().accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        cfg = load_config()
        self.cfg = cfg
        self.dest_dir = cfg.get("dest_dir") or DEFAULT_DEST
        self.core = core.Core(name=cfg.get("name"), dest_dir=self.dest_dir)
        self.core.name = self.core.name or core.default_name()
        self.peers = {}
        self.selection = []
        self.busy = False

        self.setWindowTitle(f"{APP_NAME} · 局域网快传 · {APP_AUTHOR} 开发")
        self.resize(880, 620)
        self.setMinimumSize(720, 520)
        self._set_icon()
        self._build_ui()
        self.statusBar().showMessage(
            f"{APP_NAME} v{APP_VER} · {APP_AUTHOR} 开发 · {APP_COPYRIGHT} · MIT 开源许可")
        self.core.start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.pump_events)
        self.timer.start(180)

        self.say_system(f"已就绪 · 本机 {core.local_ip()}:{self.core.port}")
        self.say_system(f"{APP_NAME} v{APP_VER} · {APP_AUTHOR} 开发 · {APP_COPYRIGHT}")
        self.say_system("正在查找局域网内的其他设备…")
        self.say_system("提示：Ctrl+V 可直接粘贴图片发送，收到的图片会显示在消息里")

    # ---------------------------------------------------------- UI
    def _set_icon(self):
        for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png"),
                  os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icon.png")):
            if os.path.exists(p):
                self.setWindowIcon(QIcon(p))
                self.app_icon = QIcon(p)
                return
        self.app_icon = QIcon()

    def _build_ui(self):
        root = QWidget()
        root.setStyleSheet("""
            QWidget { background:#f4f6fb; color:#111827;
                      font-family:'Microsoft YaHei','PingFang SC',sans-serif; }
            QLabel#title { font-size:16px; font-weight:700; }
            QLabel#sub { color:#6b7280; font-size:11px; }
            QLabel#author { background:#e8f0ff; color:#2563eb; font-size:11px;
                            font-weight:600; border-radius:9px; padding:3px 9px; }
            QStatusBar { background:#f4f6fb; color:#8b95a5; font-size:11px; }
            QLineEdit { background:#ffffff; border:1px solid #e3e8f0; border-radius:8px;
                        padding:6px 10px; font-size:12px; }
            QLineEdit:focus { border:1px solid #2563eb; }
            QListWidget { background:#ffffff; border:1px solid #e6eaf2; border-radius:10px;
                          padding:6px; outline:none; }
            QListWidget::item { padding:9px 8px; border-radius:8px; margin-bottom:4px; }
            QListWidget::item:selected { background:#e8f0ff; color:#111827; }
            QListWidget::item:hover { background:#f2f6ff; }
            QTextBrowser { background:#ffffff; border:1px solid #e6eaf2; border-radius:10px;
                           padding:6px; }
            QPushButton { background:#2563eb; color:#ffffff; border:none; border-radius:9px;
                          padding:8px 16px; font-size:13px; }
            QPushButton:hover { background:#1d4ed8; }
            QPushButton:disabled { background:#b9c8e6; }
            QPushButton#ghost { background:#eef2f8; color:#374151; }
            QPushButton#ghost:hover { background:#e3e9f4; }
            QProgressBar { border:none; background:#e6eaf2; border-radius:4px; height:8px;
                           text-align:center; font-size:10px; color:#6b7280; }
            QProgressBar::chunk { background:#2563eb; border-radius:4px; }
            QFrame#card { background:#ffffff; border:1px solid #e6eaf2; border-radius:12px; }
        """)

        # 顶栏
        top = QHBoxLayout()
        logo = QLabel("🌐")
        logo.setStyleSheet("font-size:20px;")
        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        t = QLabel("LAN Drop")
        t.setObjectName("title")
        self.lbl_status = QLabel("正在启动…")
        self.lbl_status.setObjectName("sub")
        title_box.addWidget(t)
        title_box.addWidget(self.lbl_status)
        top.addWidget(logo)
        top.addLayout(title_box)
        lbl_author = QLabel(f"{APP_AUTHOR} 开发")
        lbl_author.setObjectName("author")
        lbl_author.setCursor(Qt.PointingHandCursor)
        lbl_author.setToolTip(f"{APP_NAME} v{APP_VER}\n作者：{APP_AUTHOR}\n{APP_COPYRIGHT}\n开源许可：MIT")
        lbl_author.mousePressEvent = lambda e: self.show_about()
        top.addWidget(lbl_author)
        top.addStretch(1)
        top.addWidget(QLabel("我的设备名："))
        self.ed_name = QLineEdit(self.core.name)
        self.ed_name.setFixedWidth(150)
        self.ed_name.editingFinished.connect(self.on_rename)
        top.addWidget(self.ed_name)
        btn_folder = QPushButton("📂 接收文件夹")
        btn_folder.setObjectName("ghost")
        btn_folder.setToolTip(f"打开接收文件夹\n{self.dest_dir}")
        btn_folder.clicked.connect(self.open_dest)
        self.btn_folder = btn_folder
        top.addWidget(btn_folder)
        btn_gear = QPushButton("⚙")
        btn_gear.setObjectName("ghost")
        btn_gear.setFixedWidth(38)
        btn_gear.setToolTip("设置接收文件保存位置")
        btn_gear.clicked.connect(self.open_dest_settings)
        top.addWidget(btn_gear)

        # 主体：左设备列表 + 右消息
        body = QHBoxLayout()
        body.setSpacing(12)

        left = QVBoxLayout()
        head = QHBoxLayout()
        self.lbl_devices = QLabel("在线设备 (0)")
        self.lbl_devices.setStyleSheet("font-weight:600;font-size:13px;")
        btn_all = QPushButton("全选")
        btn_all.setObjectName("ghost")
        btn_all.setFixedWidth(56)
        btn_all.clicked.connect(self.select_all)
        head.addWidget(self.lbl_devices)
        head.addStretch(1)
        head.addWidget(btn_all)
        self.lst = QListWidget()
        self.lst.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.lst.itemSelectionChanged.connect(self.on_select)
        self.lst.setMinimumWidth(230)
        self.lst.setMaximumWidth(280)
        left.addLayout(head)
        left.addWidget(self.lst, 1)

        right = QVBoxLayout()
        self.chat = ChatBrowser()
        self.chat.anchorClicked.connect(self.on_anchor)
        right.addWidget(self.chat, 1)

        bottom = QHBoxLayout()
        btn_paste = QPushButton("🖼")
        btn_paste.setObjectName("ghost")
        btn_paste.setFixedWidth(46)
        btn_paste.setToolTip("粘贴剪贴板里的图片发送（快捷键 Ctrl+V）")
        btn_paste.clicked.connect(lambda: self.try_paste_image(force=True))
        btn_file = QPushButton("📎 发送文件")
        btn_file.clicked.connect(self.pick_files)
        self.ed_msg = QLineEdit()
        self.ed_msg.setPlaceholderText("输入文字…（回车发送；Ctrl+V 可粘贴图片，文件也可拖进窗口）")
        self.ed_msg.returnPressed.connect(self.send_text)
        self.ed_msg.installEventFilter(self)
        self.btn_send = QPushButton("发送")
        self.btn_send.setFixedWidth(96)
        self.btn_send.clicked.connect(self.send_text)
        bottom.addWidget(btn_paste)
        bottom.addWidget(btn_file)
        bottom.addWidget(self.ed_msg, 1)
        bottom.addWidget(self.btn_send)
        right.addLayout(bottom)

        body.addLayout(left, 0)
        body.addLayout(right, 1)

        self.progress = QProgressBar()
        self.progress.setFixedHeight(10)
        self.progress.hide()

        lay = QVBoxLayout(root)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(10)
        lay.addLayout(top)
        lay.addLayout(body, 1)
        lay.addWidget(self.progress)
        self.setCentralWidget(root)

        self.setAcceptDrops(True)

    # ---------------------------------------------------------- 事件
    def pump_events(self):
        while not self.core.events.empty():
            kind, payload = self.core.events.get()
            if kind == "peers":
                self.refresh_peers(payload)
            elif kind == "message":
                self.on_message(payload)
            elif kind == "log":
                self.lbl_status.setText(payload)
            elif kind == "bubble":
                self.chat.append(bubble(payload["html"], mine=payload.get("mine", True),
                                        meta=payload.get("meta", "")))
                sb = self.chat.verticalScrollBar()
                sb.setValue(sb.maximum())
            elif kind == "progress_out":
                self.on_progress(payload, outgoing=True)
            elif kind == "progress_in":
                self.on_progress(payload, outgoing=False)

    def refresh_peers(self, peers):
        self.peers = {p["id"]: p for p in peers}
        keep = {i.data(Qt.UserRole) for i in self.lst.selectedItems()}
        self.lst.clear()
        for p in peers:
            emoji = OS_EMOJI.get(p["os"], "💻")
            item = QListWidgetItem(f'{emoji}  {p["name"]}\n      {p["ip"]}')
            item.setData(Qt.UserRole, p["id"])
            item.setToolTip(f'{p["name"]} · {p["ip"]} · {p["os"]}')
            self.lst.addItem(item)
            if p["id"] in keep:
                item.setSelected(True)
        self.lbl_devices.setText(f"在线设备 ({len(peers)})")
        if peers:
            self.lbl_status.setText(f"已发现 {len(peers)} 台设备 · 本机端口 {self.core.port}")
        else:
            self.lbl_status.setText(f"暂无其他设备 · 本机 {core.local_ip()}:{self.core.port}")
        self.update_send_state()

    def on_select(self):
        self.selection = [i.data(Qt.UserRole) for i in self.lst.selectedItems()]
        self.update_send_state()

    def update_send_state(self):
        n = len(self.selection)
        self.btn_send.setText(f"发送给 {n} 台" if n > 1 else "发送")
        self.btn_send.setEnabled(bool(n))

    def select_all(self):
        self.lst.selectAll()

    def on_rename(self):
        name = self.ed_name.text().strip()[:24]
        if name and name != self.core.name:
            self.core.name = name
            self.cfg["name"] = name
            save_config(self.cfg)
            self.say_system(f"设备名已改为「{name}」，其他设备 3 秒内会刷新")

    def open_dest(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.dest_dir))

    def show_about(self):
        QMessageBox.about(
            self, f"关于 {APP_NAME}",
            f"<b>{APP_NAME}</b> v{APP_VER}<br>局域网跨平台文件快传<br><br>"
            f"作者：<b>{APP_AUTHOR}</b><br>{APP_COPYRIGHT}<br>开源许可：MIT<br><br>"
            "UDP 广播发现 + TCP 点对点直传，文件不经过任何第三方服务器。")

    def open_dest_settings(self):
        dlg = DestDialog(self.dest_dir, self)
        if dlg.exec() != QDialog.Accepted:
            return
        self.apply_dest(dlg.value)

    def apply_dest(self, path):
        """切换接收目录：内核、配置、界面提示同步更新（立即生效）。"""
        if not path or path == self.dest_dir:
            return
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"无法创建该文件夹：{e}")
            return
        self.dest_dir = path
        self.core.dest_dir = path          # 内核每次接收都读该属性，立即生效
        self.cfg["dest_dir"] = path
        save_config(self.cfg)
        self.btn_folder.setToolTip(f"打开接收文件夹\n{path}")
        self.say_system(f"接收目录已改为：{path}")

    def on_anchor(self, url):
        """点击消息里的链接：图片直接打开；「打开所在文件夹」定位到文件。"""
        path = url.toLocalFile()
        if not path:
            return
        want_dir = url.query() == "dir=1"
        if not want_dir and os.path.isfile(path) and is_image_file(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            return
        if os.path.isdir(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            return
        if sys.platform == "win32":
            try:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
                return
            except Exception:
                pass
        folder = os.path.dirname(path) or self.dest_dir
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    # ---------------------------------------------------------- 消息渲染
    def say_system(self, text):
        self.chat.append(
            f'<div style="color:#8b95a5;font-size:11.5px;margin:4px 0;">· {text}</div>')

    def on_message(self, m):
        who = m.get("from_name", "未知设备")
        ts = time.strftime("%H:%M", time.localtime(m.get("ts", time.time())))
        if m.get("kind") == "text":
            self.chat.append(bubble(
                m.get("text", "").replace("<", "&lt;").replace("\n", "<br>"),
                meta=f'{who} · {ts}'))
            self.notify(f"{who} 发来消息", m.get("text", "")[:80])
        else:
            path = m.get("path", "")
            size = core.human_size(m.get("size", 0))
            inner = self.file_inner(path, m.get("name", ""), size, saved=True)
            self.chat.append(bubble(inner, meta=f'{who} · {ts}'))
            self.notify("收到文件", f'{m.get("name")}（{size}）')
        sb = self.chat.verticalScrollBar()
        sb.setValue(sb.maximum())

    def file_inner(self, path, name, size_text, saved=False):
        """文件消息内容；图片直接内联缩略图预览，点击即可打开。"""
        name_html = html.escape(name or os.path.basename(path))
        f_url = QUrl.fromLocalFile(path).toString()
        dir_url = QUrl.fromLocalFile(path)
        dir_url.setQuery("dir=1")
        dir_url = dir_url.toString()
        tail = " · 已保存" if saved else ""
        link = (f'<a href="{dir_url}" style="color:#2563eb;font-size:11.5px;">'
                f'打开所在文件夹</a>')
        if is_image_file(path):
            t = thumb_data_uri(path)
            if t:
                data, w, h = t
                return (f'<a href="{f_url}"><img src="{data}" width="{w}" height="{h}"></a>'
                        f'<br><b>{name_html}</b>'
                        f'<span style="font-size:11px;"> · {size_text}{tail}</span><br>{link}')
        return (f'📎 <b>{name_html}</b><br>'
                f'<span style="font-size:11px;">{size_text}{tail}</span><br>{link}')

    # ---------------------------------------------------------- 粘贴图片
    def save_clip_image(self, img):
        folder = os.path.join(tempfile.gettempdir(), "LAN Drop 粘贴")
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime("粘贴图片_%Y%m%d_%H%M%S")
        path = os.path.join(folder, stamp + ".png")
        n = 1
        while os.path.exists(path):
            path = os.path.join(folder, f"{stamp}_{n}.png")
            n += 1
        if not img.save(path, "PNG"):
            return None
        return path

    def try_paste_image(self, silent=False, force=False):
        """把剪贴板里的图片（或复制的图片文件）作为文件发送。返回是否已处理。"""
        cb = QApplication.clipboard()
        md = cb.mimeData()
        paths = []
        if md is not None and md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls()
                     if u.isLocalFile() and is_image_file(u.toLocalFile())]
        if not paths:
            # 剪贴板里同时有文字时优先粘贴文字（除非用户点了 🖼 按钮）
            if not force and md is not None and md.hasText() and md.text().strip():
                return False
            img = cb.image()
            if img is None or img.isNull():
                if not silent:
                    QMessageBox.information(self, APP_NAME, "剪贴板里没有图片")
                return False
            saved = self.save_clip_image(img)
            if not saved:
                if not silent:
                    QMessageBox.warning(self, APP_NAME, "图片保存失败，请换一张试试")
                return False
            paths = [saved]
        if not self.selection:
            QMessageBox.information(self, APP_NAME, "请先在左侧选择接收设备")
            return True
        self.send_files(paths)
        return True

    def eventFilter(self, obj, ev):
        if obj is self.ed_msg and ev.type() == QEvent.KeyPress:
            if ev.matches(QKeySequence.Paste) or (
                    ev.key() == Qt.Key_V and ev.modifiers() & Qt.ControlModifier):
                if self.try_paste_image(silent=True):
                    return True
        return super().eventFilter(obj, ev)

    def keyPressEvent(self, e):
        if e.matches(QKeySequence.Paste) and self.try_paste_image(silent=True):
            return
        super().keyPressEvent(e)

    def notify(self, title, msg):
        QApplication.alert(self, 1200)
        try:
            self.tray.showMessage(title, msg, self.app_icon, 3000)
        except Exception:
            pass

    def on_progress(self, p, outgoing=True):
        total = max(1, p.get("total", 1))
        done = p.get("sent", p.get("received", 0))
        if outgoing:
            pct = int(done * 100 / total)
            self.progress.show()
            self.progress.setValue(pct)
            self.progress.setFormat(f'发送中 {pct}%')
            if pct >= 100:
                QTimer.singleShot(900, self.progress.hide)

    # ---------------------------------------------------------- 发送
    def pick_files(self):
        if not self.selection:
            QMessageBox.information(self, APP_NAME, "请先在左侧选择接收设备")
            return
        files, _ = QFileDialog.getOpenFileNames(self, "选择要发送的文件")
        if files:
            self.send_files(files)

    def send_text(self):
        text = self.ed_msg.text().strip()
        if not text:
            return
        if not self.selection:
            QMessageBox.information(self, APP_NAME, "请先在左侧选择接收设备")
            return
        self.ed_msg.clear()
        targets = list(self.selection)

        def work():
            for pid in targets:
                try:
                    self.core.send_text(pid, text)
                except Exception as e:
                    self.core.emit("log", f"发送失败：{e}")
        threading.Thread(target=work, daemon=True).start()
        ts = time.strftime("%H:%M")
        names = "、".join(self.peers.get(p, {}).get("name", "设备") for p in targets)
        self.chat.append(bubble(text.replace("<", "&lt;").replace("\n", "<br>"),
                                mine=True, meta=f'我 → {names} · {ts}'))
        sb = self.chat.verticalScrollBar()
        sb.setValue(sb.maximum())

    def send_files(self, files):
        if not self.selection:
            QMessageBox.information(self, APP_NAME, "请先在左侧选择接收设备")
            return
        if self.busy:
            QMessageBox.information(self, APP_NAME, "正在发送中，请稍候")
            return
        targets = list(self.selection)
        names = "、".join(self.peers.get(p, {}).get("name", "设备") for p in targets)
        ts = time.strftime("%H:%M")

        def work():
            self.busy = True
            try:
                for path in files:
                    size = core.human_size(os.path.getsize(path))
                    for pid in targets:
                        pname = self.peers.get(pid, {}).get("name", "设备")
                        try:
                            self.core.send_file(pid, path, progress=lambda s, t: self.core.emit(
                                "progress_out", {"sent": s, "total": t}))
                            self.core.emit("bubble", {
                                "html": self.file_inner(path, os.path.basename(path), size),
                                "mine": True, "meta": f'我 → {pname} · {ts}',
                            })
                        except Exception as e:
                            self.core.emit("log", f"发送 {os.path.basename(path)} 失败：{e}")
            finally:
                self.busy = False
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------- 拖拽
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        paths = [p for p in paths if os.path.isfile(p)]
        if paths:
            self.send_files(paths)

    # ---------------------------------------------------------- 关闭
    def closeEvent(self, e):
        try:
            self.core.stop()
        except Exception:
            pass
        e.accept()


def main():
    if "--version" in sys.argv:
        print(f"{APP_NAME} {APP_VER} · {APP_AUTHOR} 开发 · {APP_COPYRIGHT}")
        return
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(True)
    w = MainWindow()
    try:
        from PySide6.QtWidgets import QSystemTrayIcon
        w.tray = QSystemTrayIcon(w.app_icon)
        w.tray.show()
    except Exception:
        w.tray = None
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
