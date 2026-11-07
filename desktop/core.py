#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LAN Drop 内核 —— 局域网点对点发现与传输（纯标准库，无 GUI 依赖）

协议
----
发现：UDP 广播 2425 端口，每 3 秒一次，载荷 JSON
      {"app":"landrop","id":..,"name":..,"os":..,"port":..}
传输：向对方 HTTP 端口 POST /recv?name=..&from_id=..&from_name=..&kind=file|text
      body 为文件原始字节（流式），或空（文字消息放 query）

同一个程序既做发送端又做接收端，无需中心服务器。

作者：scivi  ·  © 2026 scivi  ·  MIT 开源许可
"""

import http.client
import json
import os
import platform
import queue
import re
import socket
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote, urlencode

APP_TAG = "landrop"
APP_VER = 2
# 2425 常被飞秋等软件占用，这里用 24250，可用环境变量覆盖
DISCOVERY_PORT = int(os.environ.get("LANDROP_DISCOVERY_PORT", "24250"))
ANNOUNCE_INTERVAL = 3.0     # 秒
PEER_TTL = 12.0             # 秒，超过未收到广播即视为离线
CHUNK = 256 * 1024


# ------------------------------------------------------------------ 工具
def safe_name(name):
    name = os.path.basename(str(name).replace("\\", "/"))
    name = re.sub(r'[<>:"/|?*\x00-\x1f]', "_", name).strip(" .") or "未命名文件"
    return name[:150]


def unique_path(folder, filename):
    base, ext = os.path.splitext(filename)
    p = os.path.join(folder, filename)
    n = 1
    while os.path.exists(p):
        p = os.path.join(folder, f"{base}({n}){ext}")
        n += 1
    return p


def human_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def os_label():
    s = platform.system()
    if s == "Windows":
        return "Windows"
    if s == "Darwin":
        return "Mac"
    return s or "Linux"


def default_name():
    host = socket.gethostname() or "设备"
    return f"{host}"[:24]


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# ------------------------------------------------------------------ 内核
class Core:
    """局域网点对点内核：自动发现 + 收发文件/文字"""

    def __init__(self, name=None, dest_dir=None, http_port=0, events=None):
        self.id = uuid.uuid4().hex[:12]
        self.name = (name or default_name())[:24]
        self.os = os_label()
        self.dest_dir = dest_dir or os.path.join(os.path.expanduser("~"), "Downloads", "LAN Drop 接收")
        os.makedirs(self.dest_dir, exist_ok=True)

        self.peers = {}            # id -> {id,name,os,ip,port,last_seen}
        self.events = events if events is not None else queue.Queue()
        self._stop = False
        self._lock = threading.Lock()

        # --- HTTP 接收端
        self.httpd = ThreadingHTTPServer(("0.0.0.0", http_port), self._make_handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]

        # --- UDP 发现
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        try:
            self.udp.bind(("", DISCOVERY_PORT))
        except OSError as e:
            self.emit("log", f"UDP 端口 {DISCOVERY_PORT} 被占用：{e}（可能已有实例在运行）")
        self.udp.settimeout(1.0)

    # ------------------------------------------------ 事件
    def emit(self, kind, payload=None):
        try:
            self.events.put_nowait((kind, payload))
        except queue.Full:
            pass

    # ------------------------------------------------ 启动/停止
    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        threading.Thread(target=self._udp_loop, daemon=True).start()
        threading.Thread(target=self._announce_loop, daemon=True).start()
        threading.Thread(target=self._cleanup_loop, daemon=True).start()
        self.emit("log", f"已就绪 · {local_ip()}:{self.port}")

    def stop(self):
        self._stop = True
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.udp.close()
        except Exception:
            pass

    # ------------------------------------------------ 广播
    def _payload(self):
        return json.dumps({
            "app": APP_TAG, "ver": APP_VER, "id": self.id, "name": self.name,
            "os": self.os, "port": self.port,
        }, ensure_ascii=False).encode()

    def _announce_loop(self):
        ip = local_ip()
        subnet_bc = ".".join(ip.split(".")[:3] + ["255"]) if ip.count(".") == 3 else "255.255.255.255"
        while not self._stop:
            data = self._payload()
            targets = [("255.255.255.255", DISCOVERY_PORT), (subnet_bc, DISCOVERY_PORT)]
            with self._lock:
                for p in self.peers.values():
                    targets.append((p["ip"], DISCOVERY_PORT))
            for t in targets:
                try:
                    self.udp.sendto(data, t)
                except OSError:
                    pass
            time.sleep(ANNOUNCE_INTERVAL)

    def _udp_loop(self):
        while not self._stop:
            try:
                data, addr = self.udp.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                info = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            if info.get("app") != APP_TAG or info.get("id") == self.id:
                continue
            pid = str(info.get("id"))
            if not pid:
                continue
            now = time.time()
            changed = False
            with self._lock:
                old = self.peers.get(pid)
                peer = {
                    "id": pid,
                    "name": str(info.get("name") or "未知设备")[:24],
                    "os": str(info.get("os") or "设备"),
                    "ip": addr[0],
                    "port": int(info.get("port") or 0),
                    "last_seen": now,
                }
                if not old or old.get("name") != peer["name"] or old.get("ip") != peer["ip"]:
                    changed = True
                self.peers[pid] = peer
            if changed:
                self.emit("peer", peer)
                self.emit("peers", self.peer_list())

    def _cleanup_loop(self):
        while not self._stop:
            time.sleep(2)
            now = time.time()
            gone = []
            with self._lock:
                for pid, p in list(self.peers.items()):
                    if now - p["last_seen"] > PEER_TTL:
                        gone.append(self.peers.pop(pid))
            for g in gone:
                self.emit("log", f"{g['name']} 已离线")
            if gone:
                self.emit("peers", self.peer_list())

    def peer_list(self):
        with self._lock:
            return sorted(self.peers.values(), key=lambda p: p["name"])

    # ------------------------------------------------ 接收端
    def _make_handler(self):
        core = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if urlparse(self.path).path == "/ping":
                    return self._json({"ok": True, "app": APP_TAG, "id": core.id,
                                       "name": core.name, "os": core.os})
                self._json({"error": "not found"}, 404)

            def do_POST(self):
                u = urlparse(self.path)
                if u.path != "/recv":
                    return self._json({"error": "not found"}, 404)
                qs = parse_qs(u.query)
                g = lambda k, d="": (qs.get(k) or [d])[0]
                kind = g("kind", "file")
                frm_id = g("from_id")
                frm_name = unquote(g("from_name", "未知设备"))
                length = int(self.headers.get("Content-Length") or 0)

                if kind == "text":
                    text = unquote(g("text"))
                    if length:
                        self.rfile.read(length)
                    core.emit("message", {
                        "kind": "text", "from_id": frm_id, "from_name": frm_name,
                        "text": text, "ts": time.time(),
                    })
                    return self._json({"ok": True})

                name = safe_name(unquote(g("name", "文件")))
                try:
                    os.makedirs(core.dest_dir, exist_ok=True)
                except OSError:
                    pass
                dest = unique_path(core.dest_dir, name)
                received = 0
                try:
                    with open(dest, "wb") as f:
                        remain = length
                        while remain > 0:
                            chunk = self.rfile.read(min(CHUNK, remain))
                            if not chunk:
                                break
                            f.write(chunk)
                            remain -= len(chunk)
                            received += len(chunk)
                            core.emit("progress_in", {"name": name, "received": received, "total": length})
                except Exception as e:
                    core.emit("log", f"接收 {name} 失败：{e}")
                    return self._json({"error": str(e)}, 500)

                core.emit("message", {
                    "kind": "file", "from_id": frm_id, "from_name": frm_name,
                    "name": name, "size": os.path.getsize(dest), "path": dest,
                    "ts": time.time(),
                })
                self._json({"ok": True, "saved": os.path.basename(dest)})

        return H

    # ------------------------------------------------ 发送端
    def send_file(self, peer_id, path, progress=None):
        peer = self.peers.get(peer_id)
        if not peer:
            raise RuntimeError("设备已离线")
        size = os.path.getsize(path)
        name = os.path.basename(path)
        q = urlencode({
            "name": name, "from_id": self.id, "from_name": self.name, "kind": "file",
        })
        conn = http.client.HTTPConnection(peer["ip"], peer["port"], timeout=60)
        try:
            conn.putrequest("POST", "/recv?" + q)
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(size))
            conn.endheaders()
            sent = 0
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    conn.send(chunk)
                    sent += len(chunk)
                    if progress:
                        progress(sent, size)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"对方拒绝：{body[:80]!r}")
            self.emit("log", f"已发送 {name} → {peer['name']}（{human_size(size)}）")
            return True
        finally:
            conn.close()

    def send_text(self, peer_id, text):
        peer = self.peers.get(peer_id)
        if not peer:
            raise RuntimeError("设备已离线")
        q = urlencode({
            "from_id": self.id, "from_name": self.name, "kind": "text",
            "text": text,
        })
        conn = http.client.HTTPConnection(peer["ip"], peer["port"], timeout=30)
        try:
            conn.request("POST", "/recv?" + q, body=b"",
                         headers={"Content-Length": "0"})
            resp = conn.getresponse()
            resp.read()
            if resp.status != 200:
                raise RuntimeError("发送失败")
            self.emit("log", f"已发送文字 → {peer['name']}")
            return True
        finally:
            conn.close()


# ------------------------------------------------------------------ 自检
if __name__ == "__main__":
    import sys
    import tempfile

    print("内核自检：在本机启动两个实例并互传文件")
    tmp = tempfile.mkdtemp(prefix="landrop_selftest_")
    qa, qb = queue.Queue(), queue.Queue()
    a = Core(name="实例A", dest_dir=os.path.join(tmp, "A"), events=qa)
    b = Core(name="实例B", dest_dir=os.path.join(tmp, "B"), events=qb)
    a.start()
    b.start()
    print(f"  A 端口 {a.port} / B 端口 {b.port}")

    deadline = time.time() + 15
    while time.time() < deadline and not (a.peer_list() and b.peer_list()):
        time.sleep(0.5)
    pa, pb = a.peer_list(), b.peer_list()
    print("  A 发现:", [(p["name"], p["ip"], p["port"]) for p in pa])
    print("  B 发现:", [(p["name"], p["ip"], p["port"]) for p in pb])
    if not pa or not pb:
        print("❌ 发现失败：两台设备没有互相看到（检查防火墙/UDP 2425）")
        sys.exit(1)

    # 传文件
    src = os.path.join(tmp, "测试文档.txt")
    with open(src, "w", encoding="utf-8") as f:
        f.write("LAN Drop 内核自检文件\n" * 100)
    target_id = pa[0]["id"]          # A 眼中的 B
    a.send_file(target_id, src)
    # 传文字：B → A
    b.send_text(pb[0]["id"], "内核自检：文字消息")

    time.sleep(0.8)
    files = os.listdir(os.path.join(tmp, "B"))
    print("  B 收到的文件:", files)
    got_text = []
    while not qa.empty():
        k, v = qa.get()
        if k == "message":
            got_text.append(v)
    print("  A 收到的消息:", got_text)
    ok = files and got_text and got_text[0].get("text") == "内核自检：文字消息"
    a.stop()
    b.stop()
    print("✅ 内核自检通过" if ok else "❌ 内核自检失败")
    sys.exit(0 if ok else 1)
