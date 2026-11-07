package com.landrop.app;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.UnsupportedEncodingException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URL;
import java.net.URLDecoder;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 局域网点对点网络层：UDP 广播发现 + HTTP 收发（与桌面版协议一致）
 */
public class Net {

    public static final int DISCOVERY_PORT = 24250;
    public static final String APP_TAG = "landrop";
    public static final int APP_VER = 2;

    public static class Peer {
        public String id;
        public String name;
        public String os;
        public String ip;
        public int port;
        public long lastSeen;

        public JSONObject toJson() {
            JSONObject o = new JSONObject();
            try {
                o.put("id", id);
                o.put("name", name);
                o.put("os", os);
                o.put("ip", ip);
                o.put("port", port);
            } catch (Exception ignored) {
            }
            return o;
        }
    }

    public interface Callback {
        void onPeers(List<Peer> peers);
        void onIncomingText(String fromName, String text);
        /** key：文件绝对路径或 content:// URI，取决于当前接收目录类型 */
        void onIncomingFile(String fromName, String name, long size, String key);
        void onProgress(String name, long done, long total);
        void onLog(String text);
    }

    private final String myId;
    private String myName;
    private volatile Store store;   // 接收目录（可在运行中切换）
    private final Callback cb;

    private DatagramSocket udp;
    private ServerSocket server;
    public int httpPort = 0;
    private volatile boolean running = false;

    public final Map<String, Peer> peers = new ConcurrentHashMap<>();
    private Thread rxThread, txThread, httpThread, gcThread;

    public Net(String id, String name, Store store, Callback cb) {
        this.myId = id;
        this.myName = name;
        this.store = store;
        this.cb = cb;
    }

    public Store getStore() {
        return store;
    }

    /** 运行中切换接收目录，后续收到的文件立即落到新位置 */
    public void setStore(Store s) {
        if (s != null) this.store = s;
    }

    public String getId() {
        return myId;
    }

    public String getName() {
        return myName;
    }

    public void setName(String n) {
        if (n != null && n.trim().length() > 0) myName = n.trim();
    }

    public List<Peer> peerList() {
        List<Peer> list = new ArrayList<>(peers.values());
        Collections.sort(list, (a, b) -> a.name.compareToIgnoreCase(b.name));
        return list;
    }

    // ------------------------------------------------------------ 启动
    public void start() {
        running = true;
        try {
            server = new ServerSocket(0);
            httpPort = server.getLocalPort();
        } catch (IOException e) {
            cb.onLog("接收端口启动失败：" + e.getMessage());
        }
        try {
            udp = new DatagramSocket(DISCOVERY_PORT);
            udp.setReuseAddress(true);
            udp.setBroadcast(true);
        } catch (Exception e) {
            cb.onLog("发现端口占用：" + e.getMessage());
        }

        httpThread = new Thread(this::httpLoop, "http");
        httpThread.start();

        rxThread = new Thread(this::rxLoop, "udp-rx");
        rxThread.start();

        txThread = new Thread(this::txLoop, "udp-tx");
        txThread.start();

        gcThread = new Thread(this::gcLoop, "gc");
        gcThread.start();
    }

    public void stop() {
        running = false;
        try {
            if (udp != null) udp.close();
        } catch (Exception ignored) {
        }
        try {
            if (server != null) server.close();
        } catch (Exception ignored) {
        }
    }

    private byte[] announcePayload() {
        try {
            JSONObject o = new JSONObject();
            o.put("app", APP_TAG);
            o.put("ver", APP_VER);
            o.put("id", myId);
            o.put("name", myName);
            o.put("os", "Android");
            o.put("port", httpPort);
            return o.toString().getBytes("UTF-8");
        } catch (Exception e) {
            return new byte[0];
        }
    }

    private void txLoop() {
        while (running) {
            if (udp != null && httpPort > 0) {
                byte[] data = announcePayload();
                List<String> targets = new ArrayList<>();
                targets.add("255.255.255.255");
                for (Peer p : peers.values()) targets.add(p.ip);
                for (String t : targets) {
                    try {
                        udp.send(new DatagramPacket(data, data.length,
                                InetAddress.getByName(t), DISCOVERY_PORT));
                    } catch (Exception ignored) {
                    }
                }
            }
            sleep(3000);
        }
    }

    private void rxLoop() {
        if (udp == null) return;
        byte[] buf = new byte[2048];
        while (running) {
            try {
                DatagramPacket pkt = new DatagramPacket(buf, buf.length);
                udp.receive(pkt);
                String json = new String(pkt.getData(), 0, pkt.getLength(), "UTF-8");
                JSONObject o = new JSONObject(json);
                if (!APP_TAG.equals(o.optString("app"))) continue;
                String pid = o.optString("id");
                if (pid.isEmpty() || pid.equals(myId)) continue;
                Peer p = new Peer();
                p.id = pid;
                p.name = o.optString("name", "设备");
                p.os = o.optString("os", "Android");
                p.ip = pkt.getAddress().getHostAddress();
                p.port = o.optInt("port", 0);
                p.lastSeen = System.currentTimeMillis();
                peers.put(pid, p);
                cb.onPeers(peerList());
            } catch (Exception ignored) {
            }
        }
    }

    private void gcLoop() {
        while (running) {
            sleep(2000);
            boolean changed = false;
            long now = System.currentTimeMillis();
            Iterator<Map.Entry<String, Peer>> it = peers.entrySet().iterator();
            while (it.hasNext()) {
                Peer p = it.next().getValue();
                if (now - p.lastSeen > 12000) {
                    it.remove();
                    changed = true;
                }
            }
            if (changed) cb.onPeers(peerList());
        }
    }

    // ------------------------------------------------------------ 接收端
    private void httpLoop() {
        if (server == null) return;
        while (running) {
            try {
                Socket s = server.accept();
                new Thread(() -> handleHttp(s), "http-conn").start();
            } catch (Exception e) {
                if (running) sleep(200);
            }
        }
    }

    private void handleHttp(Socket socket) {
        try {
            socket.setSoTimeout(60000);
            InputStream in = socket.getInputStream();
            String requestLine = readLine(in);
            if (requestLine == null) return;
            String[] parts = requestLine.split(" ");
            if (parts.length < 2) return;
            String target = parts[1];

            String contentType = "";
            long contentLength = 0;
            while (true) {
                String line = readLine(in);
                if (line == null || line.isEmpty()) break;
                int idx = line.indexOf(':');
                if (idx > 0) {
                    String k = line.substring(0, idx).trim().toLowerCase();
                    String v = line.substring(idx + 1).trim();
                    if (k.equals("content-length")) contentLength = Long.parseLong(v);
                    else if (k.equals("content-type")) contentType = v;
                }
            }

            int q = target.indexOf('?');
            String path = q >= 0 ? target.substring(0, q) : target;
            Map<String, String> qs = q >= 0 ? parseQuery(target.substring(q + 1)) : new HashMap<>();

            if (path.equals("/ping")) {
                respond(socket, "{\"ok\":true,\"app\":\"landrop\"}");
                return;
            }
            if (!path.equals("/recv")) {
                respond(socket, "{\"error\":\"not found\"}");
                return;
            }

            String kind = qs.containsKey("kind") ? qs.get("kind") : "file";
            String fromName = qs.containsKey("from_name") ? qs.get("from_name") : "未知设备";

            if (kind.equals("text")) {
                byte[] drain = new byte[8192];
                long remain = contentLength;
                while (remain > 0) {
                    int n = in.read(drain, 0, (int) Math.min(drain.length, remain));
                    if (n < 0) break;
                    remain -= n;
                }
                cb.onIncomingText(fromName, qs.containsKey("text") ? qs.get("text") : "");
                respond(socket, "{\"ok\":true}");
                return;
            }

            String name = qs.containsKey("name") ? qs.get("name") : "文件";
            Store cur = store;
            Store.Sink sink;
            try {
                sink = cur.create(safeName(name));
            } catch (IOException e) {
                cb.onLog("无法写入接收目录：" + e.getMessage());
                respond(socket, "{\"error\":\"dest\"}");
                return;
            }
            long done = 0;
            try (OutputStream out = sink.out) {
                byte[] buf = new byte[64 * 1024];
                long remain = contentLength;
                while (remain > 0) {
                    int n = in.read(buf, 0, (int) Math.min(buf.length, remain));
                    if (n < 0) break;
                    out.write(buf, 0, n);
                    remain -= n;
                    done += n;
                    cb.onProgress(sink.name, done, contentLength);
                }
            }
            cb.onIncomingFile(fromName, sink.name, done, sink.key);
            respond(socket, "{\"ok\":true}");
        } catch (Exception e) {
            cb.onLog("接收失败：" + e.getMessage());
        } finally {
            try {
                socket.close();
            } catch (Exception ignored) {
            }
        }
    }

    private void respond(Socket s, String json) {
        try {
            byte[] body = json.getBytes("UTF-8");
            String head = "HTTP/1.1 200 OK\r\n"
                    + "Content-Type: application/json; charset=utf-8\r\n"
                    + "Content-Length: " + body.length + "\r\n"
                    + "Connection: close\r\n\r\n";
            OutputStream out = s.getOutputStream();
            out.write(head.getBytes("UTF-8"));
            out.write(body);
            out.flush();
        } catch (Exception ignored) {
        }
    }

    private static String readLine(InputStream in) throws IOException {
        StringBuilder sb = new StringBuilder();
        int c;
        while ((c = in.read()) != -1) {
            if (c == '\n') break;
            if (c != '\r') sb.append((char) c);
            if (sb.length() > 8192) break;
        }
        if (c == -1 && sb.length() == 0) return null;
        return sb.toString();
    }

    private static Map<String, String> parseQuery(String q) {
        Map<String, String> map = new HashMap<>();
        if (q == null || q.isEmpty()) return map;
        for (String pair : q.split("&")) {
            int i = pair.indexOf('=');
            try {
                if (i > 0) {
                    map.put(URLDecoder.decode(pair.substring(0, i), "UTF-8"),
                            URLDecoder.decode(pair.substring(i + 1), "UTF-8"));
                }
            } catch (UnsupportedEncodingException ignored) {
            }
        }
        return map;
    }

    // ------------------------------------------------------------ 发送端
    public void sendText(Peer peer, String text) throws IOException {
        String q = "from_id=" + enc(myId) + "&from_name=" + enc(myName)
                + "&kind=text&text=" + enc(text);
        HttpURLConnection c = open(peer, q, 0);
        c.setFixedLengthStreamingMode(0);
        c.getOutputStream().close();
        int code = c.getResponseCode();
        c.disconnect();
        if (code != 200) throw new IOException("对方拒绝（" + code + "）");
        cb.onLog("已发送文字给 " + peer.name);
    }

    public void sendFile(Peer peer, File f) throws IOException {
        String q = "name=" + enc(f.getName()) + "&from_id=" + enc(myId)
                + "&from_name=" + enc(myName) + "&kind=file";
        long size = f.length();
        HttpURLConnection c = open(peer, q, size);
        c.setFixedLengthStreamingMode(size);
        long sent = 0;
        try (OutputStream os = c.getOutputStream();
             FileInputStream fis = new FileInputStream(f)) {
            byte[] buf = new byte[64 * 1024];
            int n;
            while ((n = fis.read(buf)) > 0) {
                os.write(buf, 0, n);
                sent += n;
                cb.onProgress(f.getName(), sent, size);
            }
        }
        int code = c.getResponseCode();
        c.disconnect();
        if (code != 200) throw new IOException("对方拒绝（" + code + "）");
        cb.onLog("已发送 " + f.getName() + " → " + peer.name);
    }

    private HttpURLConnection open(Peer peer, String query, long length) throws IOException {
        URL url = new URL("http://" + peer.ip + ":" + peer.port + "/recv?" + query);
        HttpURLConnection c = (HttpURLConnection) url.openConnection();
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setConnectTimeout(8000);
        c.setReadTimeout(60000);
        c.setRequestProperty("Content-Type", "application/octet-stream");
        return c;
    }

    public static String enc(String s) {
        try {
            return URLEncoder.encode(s == null ? "" : s, "UTF-8");
        } catch (UnsupportedEncodingException e) {
            return "";
        }
    }

    public static String safeName(String name) {
        String n = name.replace('\\', '/');
        int i = n.lastIndexOf('/');
        if (i >= 0) n = n.substring(i + 1);
        n = n.replaceAll("[<>:\"/|?*\\x00-\\x1f]", "_").trim();
        if (n.isEmpty()) n = "文件";
        return n.length() > 120 ? n.substring(0, 120) : n;
    }

    public static File uniqueFile(File dir, String name) {
        File f = new File(dir, name);
        if (!f.exists()) return f;
        int dot = name.lastIndexOf('.');
        String base = dot > 0 ? name.substring(0, dot) : name;
        String ext = dot > 0 ? name.substring(dot) : "";
        int n = 1;
        while (f.exists()) {
            f = new File(dir, base + "(" + n + ")" + ext);
            n++;
        }
        return f;
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
        }
    }
}
