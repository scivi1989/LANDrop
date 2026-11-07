package com.landrop.app;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * LAN Drop Android 版主界面：WebView 承载界面，Java 负责局域网点对点传输。
 */
public class MainActivity extends Activity implements Net.Callback {

    private static final int REQ_PICK = 1001;
    private static final int REQ_TREE = 1002;

    private WebView web;
    private Net net;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private SharedPreferences sp;
    private Store store;          // 当前接收目录（私有目录 或 用户指定目录）
    private File defaultDir;
    private List<String> pendingPeers = new ArrayList<>();
    private long lastProgressAt = 0;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);

        sp = getSharedPreferences("landrop", Context.MODE_PRIVATE);
        String id = sp.getString("id", null);
        if (id == null) {
            id = UUID.randomUUID().toString().replace("-", "").substring(0, 12);
            sp.edit().putString("id", id).apply();
        }
        String name = sp.getString("name", null);
        if (name == null) {
            name = "Android-" + id.substring(0, 4).toUpperCase();
            sp.edit().putString("name", name).apply();
        }

        defaultDir = new File(getExternalFilesDir(null), "接收");
        if (!defaultDir.exists()) defaultDir.mkdirs();
        store = resolveStartStore();

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        web.setWebViewClient(new WebViewClient());
        web.setWebChromeClient(new WebChromeClient());
        web.addJavascriptInterface(new Bridge(), "LD");
        web.setBackgroundColor(0xFFF2F5FA);

        setContentView(web);
        web.loadUrl("file:///android_asset/index.html");

        net = new Net(id, name, store, this);
        net.start();
    }

    /** 启动时恢复用户上次选定的接收目录；授权失效则回落到默认目录 */
    private Store resolveStartStore() {
        Store.SafStore saf = Store.restore(this, sp.getString("recv_uri", ""));
        if (saf != null) return saf;
        return new Store.FileStore(defaultDir);
    }

    // ----------------------------------------------------------- JS 桥
    public class Bridge {
        @JavascriptInterface
        public String info() {
            try {
                JSONObject o = new JSONObject();
                o.put("id", net.getId());
                o.put("name", net.getName());
                o.put("port", net.httpPort);
                o.put("dest", store.label());
                o.put("destKind", store.isSaf() ? "saf" : "private");
                return o.toString();
            } catch (Exception e) {
                return "{}";
            }
        }

        @JavascriptInterface
        public void setName(String n) {
            net.setName(n);
            sp.edit().putString("name", net.getName()).apply();
        }

        @JavascriptInterface
        public void sendText(String idsJson, String text) {
            List<Net.Peer> targets = resolvePeers(idsJson);
            new Thread(() -> {
                for (Net.Peer p : targets) {
                    try {
                        net.sendText(p, text);
                    } catch (Exception e) {
                        log("发送失败：" + e.getMessage());
                    }
                }
            }).start();
        }

        @JavascriptInterface
        public void pickFiles(String idsJson) {
            pendingPeers = idsOf(idsJson);
            ui.post(() -> {
                Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("*/*");
                i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                try {
                    startActivityForResult(i, REQ_PICK);
                } catch (Exception e) {
                    log("无法打开文件选择器：" + e.getMessage());
                }
            });
        }

        /** 生成图片缩略图 data URI（最长边 320px），供 WebView 直接内联显示 */
        @JavascriptInterface
        public String thumb(String key) {
            return thumbDataUri(key);
        }

        /**
         * 把剪贴板里的图片发给选中设备。返回 true 表示剪贴板里有图片、已开始发送；
         * 返回 false 时页面正常粘贴文字。
         */
        @JavascriptInterface
        public boolean pasteImage(String idsJson) {
            final List<String> ids = idsOf(idsJson);
            if (ids.isEmpty()) return false;
            final Uri src = clipboardImageUri();
            if (src == null) return false;
            new Thread(() -> {
                String name = "粘贴图片_" + new SimpleDateFormat("HHmmss", Locale.US)
                        .format(new Date()) + ".png";
                File tmp = new File(getCacheDir(), name);
                try (InputStream in = getContentResolver().openInputStream(src);
                     OutputStream out = new FileOutputStream(tmp)) {
                    if (in == null) throw new IOException("无法读取剪贴板图片");
                    byte[] buf = new byte[64 * 1024];
                    int n;
                    while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                } catch (Exception e) {
                    log("读取剪贴板图片失败：" + e.getMessage());
                    return;
                }
                for (String pid : ids) {
                    Net.Peer p = net.peers.get(pid);
                    if (p == null) continue;
                    try {
                        jsBubble("mine", "🖼 " + name, "我 → " + p.name);
                        net.sendFile(p, tmp);
                    } catch (Exception e) {
                        log("发送失败：" + e.getMessage());
                    }
                }
                //noinspection ResultOfMethodCallIgnored
                tmp.delete();
                post("window.onPasteDone && window.onPasteDone()");
            }).start();
            return true;
        }

        @JavascriptInterface
        public void openFile(String path) {
            ui.post(() -> {
                try {
                    Uri uri;
                    if (path != null && path.startsWith("content://")) {
                        uri = Uri.parse(path);
                    } else {
                        uri = FileProviderLite.uriFor(new File(path));
                    }
                    Intent i = new Intent(Intent.ACTION_VIEW);
                    i.setDataAndType(uri, getContentResolver().getType(uri));
                    i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                    startActivity(i);
                } catch (Exception e) {
                    log("无法打开文件：" + e.getMessage());
                }
            });
        }

        @JavascriptInterface
        public String destPath() {
            return store.label();
        }

        @JavascriptInterface
        public String destKind() {
            return store.isSaf() ? "saf" : "private";
        }

        /** 让用户用系统目录选择器指定接收目录 */
        @JavascriptInterface
        public void chooseDest() {
            ui.post(() -> {
                try {
                    Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
                    i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                            | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                            | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION
                            | Intent.FLAG_GRANT_PREFIX_URI_PERMISSION);
                    startActivityForResult(i, REQ_TREE);
                } catch (Exception e) {
                    log("无法打开目录选择器：" + e.getMessage());
                }
            });
        }

        /** 恢复为 App 默认接收目录 */
        @JavascriptInterface
        public void resetDest() {
            ui.post(() -> {
                store = new Store.FileStore(defaultDir);
                net.setStore(store);
                sp.edit().remove("recv_uri").apply();
                log("已恢复默认接收目录");
                post("window.onDestChanged && window.onDestChanged()");
            });
        }

        /** 接收目录里的文件列表（按时间倒序），供"接收文件"面板展示 */
        @JavascriptInterface
        public String listReceived() {
            return store.list().toString();
        }

        /** 尝试用系统文件管理器打开接收目录（部分机型/管理器不支持则提示路径） */
        @JavascriptInterface
        public void openFolder() {
            ui.post(() -> {
                boolean ok = false;
                try {
                    Intent i = new Intent(Intent.ACTION_VIEW);
                    if (store.isSaf() && store.treeUri() != null) {
                        i.setDataAndType(store.treeUri(), "vnd.android.document/directory");
                        i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
                    } else {
                        Uri uri = FileProviderLite.uriFor(defaultDir);
                        i.setDataAndType(uri, "vnd.android.document/directory");
                        i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
                    }
                    startActivity(i);
                    ok = true;
                } catch (Exception ignored) {
                }
                if (!ok) log("系统文件管理器打不开该目录，请按此路径查找：" + store.label());
            });
        }

        /** 复制接收目录路径到剪贴板 */
        @JavascriptInterface
        public void copyPath() {
            ui.post(() -> {
                try {
                    android.content.ClipboardManager cm =
                            (android.content.ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                    cm.setPrimaryClip(android.content.ClipData.newPlainText(
                            "LAN Drop 接收目录", store.label()));
                    log("接收目录路径已复制");
                } catch (Exception e) {
                    log("复制失败：" + e.getMessage());
                }
            });
        }
    }

    private List<String> idsOf(String idsJson) {
        List<String> out = new ArrayList<>();
        try {
            JSONArray arr = new JSONArray(idsJson);
            for (int i = 0; i < arr.length(); i++) out.add(arr.getString(i));
        } catch (Exception ignored) {
        }
        return out;
    }

    private List<Net.Peer> resolvePeers(String idsJson) {
        List<Net.Peer> out = new ArrayList<>();
        for (String id : idsOf(idsJson)) {
            Net.Peer p = net.peers.get(id);
            if (p != null) out.add(p);
        }
        return out;
    }

    // ----------------------------------------------------------- 文件选择结果
    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (resultCode != RESULT_OK || data == null) return;

        if (requestCode == REQ_TREE) {
            Uri tree = data.getData();
            if (tree == null) return;
            int flags = data.getFlags() & (Intent.FLAG_GRANT_READ_URI_PERMISSION
                    | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
            try {
                getContentResolver().takePersistableUriPermission(tree, flags);
            } catch (Exception e) {
                log("保存目录授权失败：" + e.getMessage());
            }
            Store next = new Store.SafStore(this, tree);
            // 先试写一个探针文件，确认真的可写（部分目录只读）
            if (!probeWritable(next)) {
                log("该目录不可写入，请换一个目录");
                return;
            }
            store = next;
            net.setStore(next);
            sp.edit().putString("recv_uri", tree.toString()).apply();
            log("接收目录已改为：" + next.label());
            post("window.onDestChanged && window.onDestChanged()");
            return;
        }

        if (requestCode != REQ_PICK) return;
        List<Uri> uris = new ArrayList<>();
        if (data.getClipData() != null) {
            for (int i = 0; i < data.getClipData().getItemCount(); i++) {
                uris.add(data.getClipData().getItemAt(i).getUri());
            }
        } else if (data.getData() != null) {
            uris.add(data.getData());
        }
        List<String> targets = new ArrayList<>(pendingPeers);
        new Thread(() -> sendUris(uris, targets)).start();
    }

    /** 写入一个极小的探针文件再删掉，验证目录可写 */
    private boolean probeWritable(Store s) {
        Store.Sink sink = null;
        try {
            sink = s.create(".landrop_write_test");
            sink.out.write(new byte[]{1});
            sink.out.close();
            sink = null;
            return true;
        } catch (Exception e) {
            return false;
        } finally {
            if (sink != null) {
                try {
                    sink.out.close();
                } catch (Exception ignored) {
                }
            }
        }
    }

    private void sendUris(List<Uri> uris, List<String> targetIds) {
        for (Uri uri : uris) {
            String name = queryName(uri);
            File tmp = new File(getCacheDir(), Net.safeName(name));
            try (InputStream in = getContentResolver().openInputStream(uri);
                 OutputStream out = new FileOutputStream(tmp)) {
                byte[] buf = new byte[64 * 1024];
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            } catch (Exception e) {
                log("读取文件失败：" + e.getMessage());
                continue;
            }
            for (String pid : targetIds) {
                Net.Peer p = net.peers.get(pid);
                if (p == null) continue;
                try {
                    jsBubble("mine", (isImageName(tmp.getName()) ? "🖼 " : "📎 ") + tmp.getName(),
                            "我 → " + p.name);
                    net.sendFile(p, tmp);
                } catch (Exception e) {
                    log("发送失败：" + e.getMessage());
                }
            }
            //noinspection ResultOfMethodCallIgnored
            tmp.delete();
        }
        post("window.onSendDone()");
    }

    private String queryName(Uri uri) {
        String name = null;
        try (android.database.Cursor c = getContentResolver().query(uri, null, null, null, null)) {
            if (c != null && c.moveToFirst()) {
                int idx = c.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME);
                if (idx >= 0) name = c.getString(idx);
            }
        } catch (Exception ignored) {
        }
        if (name == null) name = uri.getLastPathSegment();
        return name == null ? "文件" : name;
    }

    // ----------------------------------------------------------- Net 回调
    @Override
    public void onPeers(List<Net.Peer> peers) {
        JSONArray arr = new JSONArray();
        for (Net.Peer p : peers) arr.put(p.toJson());
        post("window.onPeers(" + arr + ")");
    }

    @Override
    public void onIncomingText(String fromName, String text) {
        try {
            JSONObject o = new JSONObject();
            o.put("from", fromName);
            o.put("text", text);
            post("window.onText(" + o + ")");
        } catch (Exception ignored) {
        }
    }

    @Override
    public void onIncomingFile(String fromName, String name, long size, String key) {
        try {
            JSONObject o = new JSONObject();
            o.put("from", fromName);
            o.put("name", name);
            o.put("size", size);
            o.put("path", key);
            boolean img = isImageName(name);
            o.put("image", img);
            if (img) o.put("thumb", thumbDataUri(key));
            post("window.onFile(" + o + ")");
        } catch (Exception ignored) {
        }
    }

    @Override
    public void onProgress(String name, long done, long total) {
        long now = System.currentTimeMillis();
        if (now - lastProgressAt < 120 && done < total) return;
        lastProgressAt = now;
        try {
            JSONObject o = new JSONObject();
            o.put("name", name);
            o.put("done", done);
            o.put("total", total);
            post("window.onProgress(" + o + ")");
        } catch (Exception ignored) {
        }
    }

    @Override
    public void onLog(String text) {
        log(text);
    }

    // ----------------------------------------------------------- 图片支持
    static boolean isImageName(String n) {
        if (n == null) return false;
        String s = n.toLowerCase(Locale.US);
        return s.endsWith(".png") || s.endsWith(".jpg") || s.endsWith(".jpeg")
                || s.endsWith(".gif") || s.endsWith(".bmp") || s.endsWith(".webp");
    }

    private InputStream openKey(String key) throws IOException {
        if (key != null && key.startsWith("content://")) {
            return getContentResolver().openInputStream(Uri.parse(key));
        }
        return new FileInputStream(new File(key));
    }

    private static byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        byte[] buf = new byte[64 * 1024];
        int n;
        while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
        return bo.toByteArray();
    }

    /** 读图片 → 缩小到最长边 320 → JPEG base64 data URI（失败返回空串） */
    private String thumbDataUri(String key) {
        InputStream in = null;
        try {
            in = openKey(key);
            if (in == null) return "";
            byte[] raw = readAll(in);
            BitmapFactory.Options bounds = new BitmapFactory.Options();
            bounds.inJustDecodeBounds = true;
            BitmapFactory.decodeByteArray(raw, 0, raw.length, bounds);
            if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return "";
            int scale = 1;
            while (bounds.outWidth / (scale * 2) >= 320 && bounds.outHeight / (scale * 2) >= 320) {
                scale *= 2;
            }
            BitmapFactory.Options opt = new BitmapFactory.Options();
            opt.inSampleSize = scale;
            Bitmap bm = BitmapFactory.decodeByteArray(raw, 0, raw.length, opt);
            if (bm == null) return "";
            int w = bm.getWidth(), h = bm.getHeight();
            float f = Math.min(1f, 320f / Math.max(w, h));
            if (f < 1f) {
                bm = Bitmap.createScaledBitmap(bm, Math.max(1, (int) (w * f)),
                        Math.max(1, (int) (h * f)), true);
            }
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            bm.compress(Bitmap.CompressFormat.JPEG, 82, bo);
            return "data:image/jpeg;base64,"
                    + Base64.encodeToString(bo.toByteArray(), Base64.NO_WRAP);
        } catch (Exception e) {
            return "";
        } finally {
            try {
                if (in != null) in.close();
            } catch (Exception ignored) {
            }
        }
    }

    /** 在主线程读系统剪贴板，取出其中的图片 URI（没有则 null） */
    private Uri clipboardImageUri() {
        final Uri[] found = new Uri[1];
        final CountDownLatch latch = new CountDownLatch(1);
        ui.post(() -> {
            try {
                ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                ClipData clip = cm == null ? null : cm.getPrimaryClip();
                if (clip != null) {
                    for (int i = 0; i < clip.getItemCount(); i++) {
                        Uri u = clip.getItemAt(i).getUri();
                        if (u == null) continue;
                        String t = getContentResolver().getType(u);
                        if (t != null && t.startsWith("image/")) {
                            found[0] = u;
                            break;
                        }
                    }
                }
            } catch (Exception ignored) {
            } finally {
                latch.countDown();
            }
        });
        try {
            latch.await(700, TimeUnit.MILLISECONDS);
        } catch (Exception ignored) {
        }
        return found[0];
    }

    // ----------------------------------------------------------- 工具
    private void log(String text) {
        post("window.onLog(" + JSONObject.quote(text) + ")");
    }

    private void jsBubble(String side, String title, String meta) {
        try {
            JSONObject o = new JSONObject();
            o.put("side", side);
            o.put("title", title);
            o.put("meta", meta);
            post("window.onBubble(" + o + ")");
        } catch (Exception ignored) {
        }
    }

    private void post(String js) {
        ui.post(() -> {
            if (web != null) web.evaluateJavascript(js, null);
        });
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (net != null) net.stop();
    }
}
