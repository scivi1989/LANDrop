#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LAN Drop —— 局域网跨平台文件快传（飞秋式：自动发现设备 + 选人发送）
==================================================================
同一局域网内的任何设备（Windows / macOS / Linux / Android / iOS）打开网页即可：
  1. 自动出现在彼此的"在线设备"列表里（心跳保活，关掉页面自动下线）
  2. 选择一台或多台目标设备，发送文件 / 文字
  3. 收件方实时收到消息并下载

零第三方依赖，仅需 Python 3.8+
启动：python server.py [端口]        默认端口 8765
"""

import io
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

# ------------------------------------------------------------------ 配置
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
MSG_DIR = os.path.join(DATA_DIR, "messages")
INDEX_FILE = os.path.join(DATA_DIR, "messages.json")
ICON_FILE = os.path.join(BASE_DIR, "icon.png")
os.makedirs(MSG_DIR, exist_ok=True)

DEVICE_TTL = 25          # 秒，超过即视为离线
MAX_MESSAGES = 300       # 内存中保留的消息条数
LOCK = threading.Lock()

# ------------------------------------------------------------------ 数据
DEVICES = {}             # id -> {id, name, os, ip, ua, last_seen, first_seen}
MESSAGES = []            # [{id, ts, from_id, from_name, to:[ids]|"all", text, files:[{name,size,stored}]}]


def load_messages():
    global MESSAGES
    if os.path.exists(INDEX_FILE):
        try:
            with open(INDEX_FILE, "r", encoding="utf-8") as f:
                MESSAGES = json.load(f)
        except Exception:
            MESSAGES = []


def save_messages():
    try:
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(MESSAGES[-MAX_MESSAGES:], f, ensure_ascii=False)
    except Exception as e:
        print("保存消息索引失败:", e)


# ------------------------------------------------------------------ 工具
def get_lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def safe_name(name):
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r'[<>:"/|?*\x00-\x1f]', "_", name).strip(" .") or "未命名文件"
    return name[:120]


def detect_os(ua):
    u = (ua or "").lower()
    if "iphone" in u:
        return "iPhone"
    if "ipad" in u:
        return "iPad"
    if "android" in u:
        return "Android"
    if "windows" in u:
        return "Windows"
    if "mac os" in u or "macintosh" in u:
        return "Mac"
    if "linux" in u:
        return "Linux"
    return "设备"


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def online_devices():
    now = time.time()
    out = []
    for d in DEVICES.values():
        if now - d["last_seen"] <= DEVICE_TTL:
            out.append({
                "id": d["id"],
                "name": d["name"],
                "os": d["os"],
                "ip": d["ip"],
                "first_seen": int(d["first_seen"]),
            })
    out.sort(key=lambda x: x["first_seen"])
    return out


def purge_offline():
    now = time.time()
    for k in [k for k, v in DEVICES.items() if now - v["last_seen"] > DEVICE_TTL * 10]:
        DEVICES.pop(k, None)


def parse_multipart(body, boundary):
    """极简 multipart 解析，返回 [{name, filename, data}]"""
    items = []
    delim = b"--" + boundary.encode()
    for part in body.split(delim):
        if not part or part.startswith(b"--"):
            continue
        part = part.lstrip(b"\r\n")
        if b"\r\n\r\n" not in part:
            continue
        head, _, content = part.partition(b"\r\n\r\n")
        if content.endswith(b"\r\n"):
            content = content[:-2]
        m_name = re.search(rb'name="([^"]*)"', head)
        m_file = re.search(rb'filename="([^"]*)"', head)
        items.append({
            "name": m_name.group(1).decode("utf-8", "replace") if m_name else "",
            "filename": m_file.group(1).decode("utf-8", "replace") if m_file else None,
            "data": content,
        })
    return items


# ------------------------------------------------------------------ 页面
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#2563eb">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="LAN Drop">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon.png">
<title>LAN Drop · 局域网快传</title>
<style>
:root{--bg:#f2f5fa;--card:#fff;--ink:#111827;--sub:#6b7280;--line:#e6eaf2;
--pri:#2563eb;--pri-d:#1d4ed8;--ok:#16a34a;--danger:#dc2626;--sel:#eff6ff}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--ink);min-height:100vh;padding:12px 14px 40px;max-width:760px;margin:0 auto}
.topbar{display:flex;justify-content:space-between;align-items:center;padding:6px 2px 14px;gap:10px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:8px;font-size:19px;font-weight:700}
.brand .dot{width:9px;height:9px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 4px rgba(22,163,74,.15)}
.brand small{font-size:12px;font-weight:400;color:var(--sub);margin-left:2px}
.me{display:flex;align-items:center;gap:6px;background:#fff;border:1px solid var(--line);
border-radius:99px;padding:6px 12px;font-size:13px}
.me b{font-weight:600}
.me button{background:none;border:none;cursor:pointer;font-size:13px;padding:0 2px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px;margin-bottom:12px;
box-shadow:0 2px 8px rgba(17,24,39,.04)}
.card h2{font-size:14px;display:flex;align-items:center;gap:8px;margin-bottom:12px;color:#374151}
.badge{background:#eff6ff;color:var(--pri);font-size:12px;padding:1px 8px;border-radius:99px;font-weight:600}
.spacer{flex:1}
.devices{display:grid;grid-template-columns:repeat(auto-fill,minmax(108px,1fr));gap:10px}
.dev{position:relative;border:1.5px solid var(--line);border-radius:13px;padding:12px 8px;text-align:center;
cursor:pointer;transition:.15s;background:#fff;user-select:none}
.dev:hover{border-color:#c7d2fe;transform:translateY(-1px)}
.dev.on{border-color:var(--pri);background:var(--sel);box-shadow:0 0 0 3px rgba(37,99,235,.09)}
.dev.on::after{content:"✓";position:absolute;top:5px;right:7px;color:var(--pri);font-weight:700;font-size:12px}
.avatar{width:38px;height:38px;border-radius:11px;background:#eef2ff;display:flex;align-items:center;
justify-content:center;font-size:20px;margin:0 auto 7px}
.dev.on .avatar{background:#dbeafe}
.dname{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dip{font-size:11px;color:var(--sub);margin-top:3px}
.devn{color:var(--sub);font-size:13px;padding:16px 0;text-align:center}
textarea{width:100%;height:76px;border:1.5px solid var(--line);border-radius:12px;padding:10px 12px;
font-size:14px;resize:none;font-family:inherit;background:#fbfcfe}
textarea:focus{outline:none;border-color:var(--pri);background:#fff}
.row{display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap}
.btn{border:none;border-radius:11px;padding:10px 18px;font-size:14px;cursor:pointer;background:var(--pri);
color:#fff;transition:.15s;font-weight:500}
.btn:hover{background:var(--pri-d)}
.btn:disabled{opacity:.45;cursor:default}
.btn.ghost{background:#eef2f7;color:#374151}
.btn.ghost:hover{background:#e3e9f2}
.btn.sm{padding:6px 12px;font-size:12.5px;border-radius:9px}
.btn.send{flex:1;min-width:120px}
.files{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{background:#eff6ff;color:var(--pri);border-radius:8px;padding:4px 9px;font-size:12px;
display:flex;align-items:center;gap:5px;max-width:100%}
.chip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip i{cursor:pointer;font-style:normal;color:#93a4bd;font-weight:700}
.bar{height:6px;background:#e5e7eb;border-radius:99px;overflow:hidden;margin-top:10px;display:none}
.bar i{display:block;height:100%;width:0;background:var(--pri);border-radius:99px;transition:width .2s}
.hint{font-size:12.5px;color:var(--sub);margin-top:8px;min-height:16px}
.msgs{display:flex;flex-direction:column;gap:10px;max-height:46vh;overflow-y:auto}
.msg{display:flex;flex-direction:column;gap:6px;padding:10px 12px;border-radius:13px;background:#f8fafc;
border:1px solid #eef2f7;max-width:92%}
.msg.out{align-self:flex-end;background:#eff6ff;border-color:#dbeafe}
.msg .head{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--sub)}
.msg .head b{color:#374151;font-size:12px;font-weight:600}
.msg .txt{font-size:14px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.msg .fl{display:flex;flex-direction:column;gap:5px}
.fitem{display:flex;align-items:center;gap:8px;background:#fff;border:1px solid var(--line);border-radius:10px;
padding:8px 10px;text-decoration:none;color:var(--ink);font-size:13px}
.fitem:hover{border-color:#c7d2fe;background:#f8faff}
.fitem .fi{font-size:16px}
.fitem .fn{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fitem .fs{font-size:11px;color:var(--sub)}
.fitem .fd{color:var(--pri);font-weight:600;font-size:12px;white-space:nowrap}
.empty{color:var(--sub);font-size:13px;text-align:center;padding:18px 0}
.toast{position:fixed;left:50%;top:16px;transform:translateX(-50%) translateY(-90px);background:#111827;
color:#fff;padding:10px 18px;border-radius:99px;font-size:13.5px;opacity:0;transition:.28s;
pointer-events:none;max-width:92vw;z-index:99;box-shadow:0 8px 24px rgba(0,0,0,.18)}
.toast.show{transform:translateX(-50%) translateY(0);opacity:1}
@media(max-width:400px){.devices{grid-template-columns:repeat(auto-fill,minmax(92px,1fr))}}
</style>
</head>
<body>
<div class="topbar">
  <div class="brand"><span class="dot"></span>LAN Drop <small>局域网快传</small></div>
  <div class="me">我：<b id="meName">…</b><button id="rename" title="修改设备名">✏️</button></div>
</div>

<div class="card">
  <h2>在线设备 <span class="badge" id="onlineCount">0</span><span class="spacer"></span>
    <button class="btn ghost sm" id="selectAll">全选</button></h2>
  <div class="devices" id="devices"><div class="devn">正在查找设备…</div></div>
</div>

<div class="card">
  <h2>发送内容 <span class="spacer"></span><span id="targetInfo" style="font-size:12px;color:var(--sub)"></span></h2>
  <textarea id="text" placeholder="输入要发送的文字 / 链接，或直接粘贴…"></textarea>
  <div class="files" id="files"></div>
  <div class="bar" id="bar"><i id="barFill"></i></div>
  <div class="row">
    <button class="btn ghost" id="pick">📎 选择文件</button>
    <input type="file" id="file" multiple hidden>
    <button class="btn send" id="send">发送</button>
  </div>
  <div class="hint" id="hint"></div>
</div>

<div class="card">
  <h2>消息记录 <span class="spacer"></span>
    <button class="btn ghost sm" id="clear">清空显示</button></h2>
  <div class="msgs" id="msgs"><div class="empty">还没有消息</div></div>
</div>

<div class="toast" id="toast"></div>

<script>
(function(){
"use strict";
var $ = function(s){ return document.querySelector(s); };

/* ---------- 本机身份 ---------- */
function uuid(){
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g,"").slice(0,16);
  return "d" + Math.random().toString(36).slice(2,12) + Date.now().toString(36).slice(-4);
}
var ME = localStorage.getItem("ld_id");
if (!ME) { ME = uuid(); localStorage.setItem("ld_id", ME); }
var myName = localStorage.getItem("ld_name") || "";
var selected = {};        /* id -> true */
var devices = [];
var pendingFiles = [];
var lastIncomingTs = 0;
var isFirstSync = true;

function toast(msg){
  var t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(t._t); t._t = setTimeout(function(){ t.classList.remove("show"); }, 2400);
}
function esc(s){
  return String(s).replace(/[&<>"']/g, function(c){
    return ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[c];
  });
}
function size(n){
  if (n < 1024) return n + " B";
  var u = ["KB","MB","GB"], i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < 2);
  return n.toFixed(1) + " " + u[i];
}
function osIcon(os){
  var m = {"Windows":"💻","Mac":"🖥️","iPhone":"📱","iPad":"📱","Android":"📱","Linux":"🐧"};
  return m[os] || "💻";
}
function fileIcon(name){
  var e = String(name).split(".").pop().toLowerCase();
  var m = {"jpg":"🖼️","jpeg":"🖼️","png":"🖼️","gif":"🖼️","webp":"🖼️","svg":"🖼️","heic":"🖼️",
  "mp4":"🎬","mov":"🎬","avi":"🎬","mkv":"🎬","mp3":"🎵","wav":"🎵","flac":"🎵",
  "zip":"🗜️","rar":"🗜️","7z":"🗜️","tar":"🗜️","gz":"🗜️",
  "pdf":"📕","doc":"📄","docx":"📄","xls":"📊","xlsx":"📊","csv":"📊","ppt":"📽️","pptx":"📽️",
  "txt":"📝","md":"📝","apk":"🤖","exe":"⚙️","dmg":"⚙️"};
  return m[e] || "📦";
}
function timeStr(ts){
  var d = new Date(ts * 1000);
  var p = function(n){ return (n < 10 ? "0" : "") + n; };
  var today = new Date();
  var sameDay = d.toDateString() === today.toDateString();
  var hm = p(d.getHours()) + ":" + p(d.getMinutes());
  return sameDay ? hm : (d.getMonth() + 1) + "/" + d.getDate() + " " + hm;
}
function beep(){
  try {
    var C = window.AudioContext || window.webkitAudioContext;
    if (!C) return;
    var ctx = new C(), o = ctx.createOscillator(), g = ctx.createGain();
    o.type = "sine"; o.frequency.value = 880;
    g.gain.setValueAtTime(0.08, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
    o.connect(g); g.connect(ctx.destination);
    o.start(); o.stop(ctx.currentTime + 0.36);
  } catch (e) {}
}

/* ---------- 心跳同步 ---------- */
function sync(){
  var body = JSON.stringify({ id: ME, name: myName });
  fetch("/api/sync", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body
  }).then(function(r){ return r.json(); }).then(function(d){
    if (d && d.me) {
      myName = d.me.name;
      localStorage.setItem("ld_name", myName);
      $("#meName").textContent = myName;
    }
    devices = (d.devices || []).filter(function(x){ return x.id !== ME; });
    renderDevices();
    renderMessages(d.messages || []);
    isFirstSync = false;
  }).catch(function(){
    $("#onlineCount").textContent = "离线";
  });
}

function renderDevices(){
  var box = $("#devices");
  $("#onlineCount").textContent = devices.length + " 台在线";
  if (!devices.length) {
    box.innerHTML = '<div class="devn">暂无其他设备在线<br><small>让其他设备打开本页地址即可自动出现</small></div>';
    updateTargetInfo();
    return;
  }
  var html = "";
  for (var i = 0; i < devices.length; i++) {
    var d = devices[i];
    var on = selected[d.id] ? " on" : "";
    html += '<div class="dev' + on + '" data-id="' + esc(d.id) + '">' +
      '<div class="avatar">' + osIcon(d.os) + '</div>' +
      '<div class="dname" title="' + esc(d.name) + '">' + esc(d.name) + '</div>' +
      '<div class="dip">' + esc(d.ip) + '</div></div>';
  }
  box.innerHTML = html;
  var cards = box.querySelectorAll(".dev");
  for (var j = 0; j < cards.length; j++) {
    cards[j].onclick = function(){
      var id = this.getAttribute("data-id");
      if (selected[id]) { delete selected[id]; } else { selected[id] = true; }
      this.classList.toggle("on");
      updateTargetInfo();
    };
  }
  updateTargetInfo();
}

function targetIds(){
  return Object.keys(selected).filter(function(k){ return selected[k]; });
}
function updateTargetInfo(){
  var n = targetIds().length;
  $("#targetInfo").textContent = n ? ("已选 " + n + " 台设备") : "未选择设备";
  $("#send").textContent = n ? ("发送给 " + n + " 台设备") : "请先选择设备";
}

/* ---------- 消息渲染 ---------- */
var renderedIds = {};
function renderMessages(list){
  var box = $("#msgs");
  var hasNew = false;
  var html = "";
  var shown = 0;
  for (var i = 0; i < list.length; i++) {
    var m = list[i];
    var mine = (m.from_id === ME);
    var names = m.to === "all" ? "所有人" : (m.to_names || []).join("、");
    var who = mine ? ("我 → " + names) : (m.from_name + " → 我");
    html += '<div class="msg' + (mine ? " out" : "") + '">' +
      '<div class="head"><b>' + esc(who) + '</b><span>' + timeStr(m.ts) + '</span></div>';
    if (m.text) html += '<div class="txt">' + esc(m.text) + '</div>';
    if (m.files && m.files.length) {
      html += '<div class="fl">';
      for (var k = 0; k < m.files.length; k++) {
        var f = m.files[k];
        html += '<a class="fitem" href="/api/download?msg=' + encodeURIComponent(m.id) +
          '&i=' + k + '" download>' +
          '<span class="fi">' + fileIcon(f.name) + '</span>' +
          '<span class="fn">' + esc(f.name) + '</span>' +
          '<span class="fs">' + size(f.size) + '</span>' +
          '<span class="fd">下载</span></a>';
      }
      html += '</div>';
    }
    html += '</div>';
    if (!mine && m.ts > lastIncomingTs) { hasNew = true; }
    if (!mine && m.ts > lastIncomingTs) lastIncomingTs = m.ts;
    renderedIds[m.id] = true;
    shown++;
  }
  if (!shown) { box.innerHTML = '<div class="empty">还没有消息</div>'; return; }
  var atTop = box.scrollTop < 40;
  box.innerHTML = html;
  if (atTop) box.scrollTop = box.scrollHeight;
  if (hasNew && !isFirstSync) {
    beep();
    toast("📨 收到新消息");
    document.title = "● 新消息 · LAN Drop";
    setTimeout(function(){ document.title = "LAN Drop · 局域网快传"; }, 4000);
  }
}

/* ---------- 文件选择 ---------- */
$("#pick").onclick = function(){ $("#file").click(); };
$("#file").onchange = function(){
  var fs = $("#file").files;
  for (var i = 0; i < fs.length; i++) pendingFiles.push(fs[i]);
  $("#file").value = "";
  renderPending();
};
function renderPending(){
  var box = $("#files");
  var html = "";
  for (var i = 0; i < pendingFiles.length; i++) {
    html += '<div class="chip"><span>' + esc(pendingFiles[i].name) + '</span>' +
      '<i data-i="' + i + '">×</i></div>';
  }
  box.innerHTML = html;
  var xs = box.querySelectorAll("i");
  for (var j = 0; j < xs.length; j++) {
    xs[j].onclick = function(){
      pendingFiles.splice(parseInt(this.getAttribute("data-i"), 10), 1);
      renderPending();
    };
  }
}
document.addEventListener("paste", function(ev){
  var fs = ev.clipboardData && ev.clipboardData.files;
  if (fs && fs.length) {
    for (var i = 0; i < fs.length; i++) pendingFiles.push(fs[i]);
    renderPending();
    ev.preventDefault();
  }
});

/* ---------- 发送 ---------- */
$("#send").onclick = function(){
  var ids = targetIds();
  if (!ids.length) { toast("请先选择接收设备"); return; }
  var text = $("#text").value.trim();
  if (!text && !pendingFiles.length) { toast("请输入内容或选择文件"); return; }

  var fd = new FormData();
  fd.append("from", ME);
  fd.append("from_name", myName);
  fd.append("to", ids.join(","));
  fd.append("text", text);
  for (var i = 0; i < pendingFiles.length; i++) {
    fd.append("file", pendingFiles[i], pendingFiles[i].name);
  }

  var btn = $("#send"), bar = $("#bar"), fill = $("#barFill"), hint = $("#hint");
  btn.disabled = true; bar.style.display = "block"; fill.style.width = "0%";
  hint.textContent = "正在发送…";

  var x = new XMLHttpRequest();
  x.open("POST", "/api/send");
  x.upload.onprogress = function(e){
    if (e.lengthComputable) fill.style.width = (e.loaded / e.total * 100).toFixed(0) + "%";
  };
  x.onload = function(){
    btn.disabled = false; bar.style.display = "none";
    if (x.status === 200) {
      hint.textContent = "✅ 发送成功";
      $("#text").value = "";
      pendingFiles = []; renderPending();
      toast("✅ 已发送给 " + ids.length + " 台设备");
      sync();
      setTimeout(function(){ hint.textContent = ""; }, 2500);
    } else {
      hint.textContent = "❌ 发送失败：" + x.responseText;
    }
  };
  x.onerror = function(){
    btn.disabled = false; bar.style.display = "none";
    hint.textContent = "❌ 网络错误，请检查连接";
  };
  x.send(fd);
};

/* ---------- 其他交互 ---------- */
$("#selectAll").onclick = function(){
  var all = devices.length && targetIds().length === devices.length;
  selected = {};
  if (!all) { for (var i = 0; i < devices.length; i++) selected[devices[i].id] = true; }
  renderDevices();
};
$("#rename").onclick = function(){
  var n = prompt("给你的设备起个名字（其他设备会看到）", myName);
  if (n && n.trim()) {
    myName = n.trim().slice(0, 24);
    localStorage.setItem("ld_name", myName);
    $("#meName").textContent = myName;
    sync();
  }
};
$("#clear").onclick = function(){
  $("#msgs").innerHTML = '<div class="empty">还没有消息</div>';
  lastIncomingTs = Math.floor(Date.now() / 1000);
};

/* ---------- 启动 ---------- */
$("#meName").textContent = myName || "识别中…";
sync();
setInterval(sync, 3000);
window.addEventListener("focus", sync);
})();
</script>
</body>
</html>"""

MANIFEST = {
    "name": "LAN Drop 局域网快传",
    "short_name": "LAN Drop",
    "description": "局域网内跨平台文件与文字快传，自动发现设备",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#f2f5fa",
    "theme_color": "#2563eb",
    "icons": [
        {"src": "/icon.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LANDrop/2.0"

    def log_message(self, fmt, *args):
        pass  # 静默，避免刷屏

    # ------------------------------------------------ 响应工具
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data, ctype="application/octet-stream", filename=None, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + quote(filename, safe=""))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    # ------------------------------------------------ GET
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        qs = parse_qs(url.query)

        if path in ("/", "/index.html"):
            ips = get_lan_ips()
            urls = " · ".join(f"http://{ip}:{PORT}" for ip in ips) or f"http://127.0.0.1:{PORT}"
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/manifest.json":
            self.send_bytes(json.dumps(MANIFEST, ensure_ascii=False).encode(),
                            "application/manifest+json")
            return

        if path == "/icon.png":
            if os.path.exists(ICON_FILE):
                with open(ICON_FILE, "rb") as f:
                    self.send_bytes(f.read(), "image/png")
            else:
                self.send_bytes(b"", "image/png", code=404)
            return

        if path == "/api/download":
            msg_id = (qs.get("msg") or [""])[0]
            try:
                idx = int((qs.get("i") or ["0"])[0])
            except ValueError:
                idx = 0
            with LOCK:
                msg = next((m for m in MESSAGES if m["id"] == msg_id), None)
            if not msg or idx >= len(msg.get("files", [])):
                return self.send_json({"error": "文件不存在"}, 404)
            fi = msg["files"][idx]
            fpath = os.path.join(MSG_DIR, msg_id, fi["stored"])
            if not os.path.isfile(fpath):
                return self.send_json({"error": "文件已被清理"}, 404)
            with open(fpath, "rb") as f:
                data = f.read()
            return self.send_bytes(data, "application/octet-stream", fi["name"])

        if path == "/api/zip":
            msg_id = (qs.get("msg") or [""])[0]
            with LOCK:
                msg = next((m for m in MESSAGES if m["id"] == msg_id), None)
            if not msg or not msg.get("files"):
                return self.send_json({"error": "没有文件"}, 404)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for fi in msg["files"]:
                    fp = os.path.join(MSG_DIR, msg_id, fi["stored"])
                    if os.path.isfile(fp):
                        z.write(fp, fi["name"])
            data = buf.getvalue()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            return self.send_bytes(data, "application/zip", f"LAN_Drop_{stamp}.zip")

        self.send_json({"error": "not found"}, 404)

    # ------------------------------------------------ POST
    def do_POST(self):
        url = urlparse(self.path)
        path = url.path

        # ---- 心跳 / 注册
        if path == "/api/sync":
            raw = self.read_body()
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                data = {}
            dev_id = str(data.get("id") or "").strip()[:32]
            if not dev_id:
                return self.send_json({"error": "缺少设备 id"}, 400)

            ip = self.client_address[0]
            ua = self.headers.get("User-Agent", "")
            now = time.time()
            with LOCK:
                d = DEVICES.get(dev_id)
                if d is None:
                    os_name = detect_os(ua)
                    default_name = f"{os_name}-{dev_id[:4].upper()}"
                    d = {"id": dev_id, "name": default_name, "os": os_name,
                         "ip": ip, "first_seen": now}
                    DEVICES[dev_id] = d
                d["last_seen"] = now
                d["ip"] = ip
                name = str(data.get("name") or "").strip()
                if name:
                    d["name"] = name[:24]

                # 与我有关系的消息：我发出的 + 发给我的（最近 12 小时）
                cutoff = now - 12 * 3600
                my_name = d["name"]
                id2name = {v["id"]: v["name"] for v in DEVICES.values()}
                id2name[dev_id] = my_name
                msgs = []
                for m in MESSAGES:
                    if m["ts"] < cutoff:
                        continue
                    to_me = m["to"] == "all" or dev_id in m["to"]
                    if m["from_id"] != dev_id and not to_me:
                        continue
                    item = dict(m)
                    if m["to"] == "all":
                        pass
                    else:
                        item["to_names"] = [id2name.get(t, "已离线设备") for t in m["to"]]
                    msgs.append(item)
                devs = [x for x in online_devices()]
                me_info = {"id": dev_id, "name": d["name"], "os": d["os"], "ip": ip}
                purge_offline()
            return self.send_json({"me": me_info, "devices": devs, "messages": msgs[-80:]})

        # ---- 发送消息
        if path == "/api/send":
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                return self.send_json({"error": "需要 multipart/form-data"}, 400)
            m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype)
            if not m:
                return self.send_json({"error": "缺少 boundary"}, 400)
            boundary = m.group(1) or m.group(2)
            body = self.read_body()
            items = parse_multipart(body, boundary)

            sender_id, sender_name, to_raw, text = "", "", "", ""
            files = []
            msg_id = uuid.uuid4().hex[:12]
            fdir = os.path.join(MSG_DIR, msg_id)
            for it in items:
                if it["filename"] is not None:
                    if not it["data"]:
                        continue
                    name = safe_name(unquote(it["filename"]))
                    os.makedirs(fdir, exist_ok=True)
                    stored = f"{len(files)}_{name}"
                    with open(os.path.join(fdir, stored), "wb") as f:
                        f.write(it["data"])
                    files.append({"name": name, "size": len(it["data"]), "stored": stored})
                elif it["name"] == "from":
                    sender_id = it["data"].decode("utf-8", "replace")
                elif it["name"] == "from_name":
                    sender_name = it["data"].decode("utf-8", "replace")
                elif it["name"] == "to":
                    to_raw = it["data"].decode("utf-8", "replace")
                elif it["name"] == "text":
                    text = it["data"].decode("utf-8", "replace")

            if not sender_id:
                return self.send_json({"error": "缺少发送方"}, 400)
            if not text.strip() and not files:
                return self.send_json({"error": "内容为空"}, 400)

            targets = [t.strip() for t in to_raw.split(",") if t.strip()]
            with LOCK:
                if not targets:
                    return self.send_json({"error": "请选择接收设备"}, 400)
                if not sender_name:
                    sender_name = DEVICES.get(sender_id, {}).get("name", "未知设备")
                msg = {
                    "id": msg_id,
                    "ts": int(time.time()),
                    "from_id": sender_id,
                    "from_name": sender_name,
                    "to": "all" if "all" in targets else targets,
                    "text": text,
                    "files": files,
                }
                MESSAGES.append(msg)
                if len(MESSAGES) > MAX_MESSAGES:
                    del MESSAGES[: len(MESSAGES) - MAX_MESSAGES]
                save_messages()
            nfiles = f"（{len(files)} 个文件）" if files else ""
            print(f"[{datetime.now():%H:%M:%S}] {sender_name} → "
                  f"{'所有人' if msg['to'] == 'all' else len(targets)} 台设备 {nfiles}")
            return self.send_json({"ok": True, "id": msg_id})

        self.send_json({"error": "not found"}, 404)


def main():
    load_messages()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    ips = get_lan_ips()
    print("=" * 58)
    print("  LAN Drop 已启动 —— 局域网跨平台文件快传")
    print("  在每台设备的浏览器中打开同一个地址：")
    for ip in ips:
        print(f"      http://{ip}:{PORT}")
    print("  手机可「添加到主屏幕」，像 App 一样使用")
    print("  按 Ctrl+C 停止服务")
    print("=" * 58)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
