package com.landrop.app;

import android.content.ContentResolver;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.provider.DocumentsContract;
import android.webkit.MimeTypeMap;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.util.Arrays;
import java.util.Comparator;

/**
 * 接收目录抽象：支持两种落盘位置
 * 1) FileStore —— App 私有外部目录（默认，不需要任何权限）
 * 2) SafStore  —— 用户自己通过系统目录选择器（SAF）指定的任意目录
 *                 （下载/相册/外置 SD 卡等，持久化授权后重启仍有效）
 */
public abstract class Store {

    /** 一次写入的落盘位置 */
    public static class Sink {
        public OutputStream out;
        public String name;   // 最终显示名（可能被系统重名规则改写）
        public String key;    // File 绝对路径 或 content:// URI，用于后续打开
    }

    public abstract String label();

    public abstract boolean isSaf();

    public abstract Sink create(String safeName) throws IOException;

    /** 已接收文件列表（按时间倒序），元素为 {name,size,path,time} */
    public abstract JSONArray list();

    public Uri treeUri() {
        return null;
    }

    // ------------------------------------------------------------ 私有目录实现
    public static class FileStore extends Store {
        public final File dir;

        public FileStore(File dir) {
            this.dir = dir;
            if (!dir.exists()) dir.mkdirs();
        }

        @Override
        public String label() {
            return dir.getAbsolutePath();
        }

        @Override
        public boolean isSaf() {
            return false;
        }

        @Override
        public Sink create(String safeName) throws IOException {
            File f = Net.uniqueFile(dir, safeName);
            Sink s = new Sink();
            s.name = f.getName();
            s.key = f.getAbsolutePath();
            s.out = new FileOutputStream(f);
            return s;
        }

        @Override
        public JSONArray list() {
            JSONArray arr = new JSONArray();
            File[] fs = dir.listFiles();
            if (fs == null) return arr;
            Arrays.sort(fs, new Comparator<File>() {
                @Override
                public int compare(File a, File b) {
                    return Long.compare(b.lastModified(), a.lastModified());
                }
            });
            for (File f : fs) {
                if (!f.isFile()) continue;
                try {
                    JSONObject o = new JSONObject();
                    o.put("name", f.getName());
                    o.put("size", f.length());
                    o.put("path", f.getAbsolutePath());
                    o.put("time", f.lastModified());
                    arr.put(o);
                } catch (Exception ignored) {
                }
            }
            return arr;
        }
    }

    // ------------------------------------------------------------ SAF 目录实现
    public static class SafStore extends Store {
        private final Context ctx;
        private final Uri tree;

        public SafStore(Context ctx, Uri tree) {
            this.ctx = ctx;
            this.tree = tree;
        }

        @Override
        public Uri treeUri() {
            return tree;
        }

        @Override
        public boolean isSaf() {
            return true;
        }

        private ContentResolver res() {
            return ctx.getContentResolver();
        }

        private String treeDocId() {
            try {
                return DocumentsContract.getTreeDocumentId(tree);
            } catch (Exception e) {
                return null;
            }
        }

        /** 目录显示名：优先取 provider 的 DISPLAY_NAME，失败则按 docId 拼一个可读路径 */
        @Override
        public String label() {
            String id = treeDocId();
            String name = null;
            if (id != null) {
                try {
                    Uri doc = DocumentsContract.buildDocumentUriUsingTree(tree, id);
                    try (Cursor c = res().query(doc,
                            new String[]{DocumentsContract.Document.COLUMN_DISPLAY_NAME},
                            null, null, null)) {
                        if (c != null && c.moveToFirst()) name = c.getString(0);
                    }
                } catch (Exception ignored) {
                }
                String path = id;
                if (path.startsWith("primary:")) {
                    path = path.substring("primary:".length());
                    if (path.isEmpty()) return "内部存储";
                    return "内部存储/" + path.replace(':', '/');
                }
                int idx = path.indexOf(':');
                if (idx > 0 && idx == 4) {   // 形如 1A2B-3C4D:xxx（外置卡）
                    String tail = path.substring(idx + 1);
                    return "存储卡/" + (tail.isEmpty() ? "" : tail);
                }
                if (name != null && !name.isEmpty()) return name;
            }
            return tree.toString();
        }

        @Override
        public Sink create(String safeName) throws IOException {
            String id = treeDocId();
            if (id == null) throw new IOException("接收目录无效，请重新选择");
            Uri parent = DocumentsContract.buildDocumentUriUsingTree(tree, id);
            String mime = mimeOf(safeName);
            Uri doc;
            try {
                doc = DocumentsContract.createDocument(res(), parent, mime, safeName);
            } catch (Exception e) {
                throw new IOException("无法写入该目录（权限可能已失效）");
            }
            if (doc == null) throw new IOException("无法在该目录创建文件");
            Sink s = new Sink();
            s.key = doc.toString();
            s.name = displayName(doc, safeName);
            s.out = res().openOutputStream(doc, "w");
            if (s.out == null) throw new IOException("无法打开写入流");
            return s;
        }

        private String displayName(Uri doc, String fallback) {
            try (Cursor c = res().query(doc,
                    new String[]{DocumentsContract.Document.COLUMN_DISPLAY_NAME},
                    null, null, null)) {
                if (c != null && c.moveToFirst()) {
                    String n = c.getString(0);
                    if (n != null && !n.isEmpty()) return n;
                }
            } catch (Exception ignored) {
            }
            return fallback;
        }

        @Override
        public JSONArray list() {
            JSONArray arr = new JSONArray();
            String id = treeDocId();
            if (id == null) return arr;
            Uri children = DocumentsContract.buildChildDocumentsUriUsingTree(tree, id);
            String[] cols = {
                    DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                    DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                    DocumentsContract.Document.COLUMN_SIZE,
                    DocumentsContract.Document.COLUMN_LAST_MODIFIED,
            };
            try (Cursor c = res().query(children, cols, null, null,
                    DocumentsContract.Document.COLUMN_LAST_MODIFIED + " DESC")) {
                if (c == null) return arr;
                while (c.moveToNext()) {
                    String docId = c.getString(0);
                    String name = c.getString(1);
                    if (docId == null || name == null) continue;
                    try {
                        JSONObject o = new JSONObject();
                        o.put("name", name);
                        o.put("size", c.isNull(2) ? 0 : c.getLong(2));
                        o.put("path", DocumentsContract.buildDocumentUriUsingTree(tree, docId).toString());
                        o.put("time", c.isNull(3) ? 0 : c.getLong(3));
                        arr.put(o);
                    } catch (Exception ignored) {
                    }
                }
            } catch (Exception ignored) {
            }
            return arr;
        }
    }

    static String mimeOf(String name) {
        int dot = name.lastIndexOf('.');
        if (dot > 0 && dot < name.length() - 1) {
            String ext = name.substring(dot + 1).toLowerCase();
            String m = MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext);
            if (m != null) return m;
        }
        return "application/octet-stream";
    }

    /** 取回持久化授权的目录；授权失效（例如 SD 卡拔出）时返回 null */
    public static SafStore restore(Context ctx, String uriStr) {
        if (uriStr == null || uriStr.isEmpty()) return null;
        try {
            Uri uri = Uri.parse(uriStr);
            boolean ok = false;
            for (android.content.UriPermission p : ctx.getContentResolver().getPersistedUriPermissions()) {
                if (p.getUri().equals(uri) && p.isWritePermission()) {
                    ok = true;
                    break;
                }
            }
            if (!ok) return null;
            return new SafStore(ctx, uri);
        } catch (Exception e) {
            return null;
        }
    }
}
