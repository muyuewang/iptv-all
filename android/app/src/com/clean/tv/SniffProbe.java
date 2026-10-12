package com.clean.tv;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.HashMap;
import java.util.Iterator;
import java.util.Map;

/**
 * 官网页「提取到的流地址」可播性探测（v2.9）。
 *
 * 背景：安卓端一直缺「从官网页面把真实流地址抓出来、交给原生硬解」的能力。
 * 桌面端要靠 CDP + 外部浏览器（冷启动 30s+，见 desktop/cdp.py），而安卓的
 * WebView 本来就加载着那个官方直播页 —— MainActivity 用
 * WebViewClient.shouldInterceptRequest 就能在页面自己发请求的瞬间把 *.m3u8
 * 截下来，不需要外部浏览器、不需要调试端口，成本低一个数量级。
 *
 * ★ 但「截到地址」≠「能播」，所以必须探测：
 *   · 央视系（cctv.com / 央视频）的流是 TS 载荷级**私有加扰**（路径带 cdrm），
 *     列表里没有 #EXT-X-KEY、分片首字节也还是 0x47 —— 探测会「通过」，
 *     但交给播放器解出来全是花屏。花屏是**静默的**（播放器不报错、也不花 CPU），
 *     没法自动回退，所以央视系由 MainActivity 直接按域名/路径拦掉，不走这里。
 *   · 标准 HLS 加密（有 #EXT-X-KEY 且非 NONE）也不提取 —— 网页端自己会解，
 *     我们没必要替它解一遍，切过去反而可能因为取不到 key 而失败。
 *   · 剩下的：递归下钻 master→media playlist，取第一个分片前 16 字节，
 *     认 TS 同步字 0x47 或 fMP4 的 ftyp/styp/moof 盒子。
 *
 * ★ 判据不能是「HTTP 200」—— 加扰流照样 200（实测：mpv rc=0 但每帧报
 *   h264: error while decoding MB）。也不能只看分片首字节 —— 加扰流首字节
 *   也是 0x47。所以真正的判据只能是「站点可信度」+ 明文容器识别，见上。
 *
 * 这套判据与 tools/_probe_wvt_hitrate.py 完全对应（15 个地方台实测：
 * 嗅到 11/15，判定可播 10/15，与真实 mpv 播放结果一致）。
 */
final class SniffProbe {

    /** 探测结论 */
    static final class Result {
        /** 是否判定「明文可播」 */
        boolean ok;
        /** 判定可播时：该交给播放器的 media playlist 地址（已下钻到最内层） */
        String url;
        /** 容器类型：ts / fmp4 */
        String kind = "";
        /** 不可播时的原因（给日志与 UI 用，别让用户对着黑屏猜） */
        String reason = "";
    }

    private static final int CONNECT_MS = 4000;
    private static final int READ_MS = 4000;
    private static final int MAX_BYTES = 128 * 1024;
    /** master → media playlist 最多下钻层数（正常 1 层就够，多码率嵌套最多 2） */
    private static final int MAX_DEPTH = 3;

    private SniffProbe() {}

    /**
     * 探测一个 m3u8 是否明文可播。
     *
     * @param playlistUrl 嗅探到的地址（通常是 media playlist，也可能是 master）
     * @param headers     原样带上页面的 Referer / Cookie / UA —— 少一个就 403
     * @return 结论；永不抛异常、永不返回 null
     */
    static Result probe(String playlistUrl, Map<String, String> headers) {
        Result r = new Result();
        StringBuilder err = new StringBuilder();
        try {
            String cur = playlistUrl;
            for (int depth = 0; depth < MAX_DEPTH; depth++) {
                String body = getText(cur, headers, err);
                if (body == null) { r.reason = "播放列表拉取失败" + err; return r; }
                if (body.indexOf("#EXTM3U") < 0) { r.reason = "不是 m3u8 播放列表"; return r; }
                /* 标准 HLS 加密：网页端自己会解，不抢这活 */
                if (hasKey(body)) { r.reason = "标准 HLS 加密（保持网页播放）"; return r; }

                if (body.indexOf("#EXT-X-STREAM-INF") >= 0) {
                    /* master playlist → 下钻第一条子列表 */
                    String sub = firstNonComment(body);
                    if (sub == null) { r.reason = "master 里没有子列表"; return r; }
                    cur = abs(cur, sub);
                    continue;
                }
                /* media playlist → 取第一个分片验证容器 */
                String seg = firstNonComment(body);
                if (seg == null) { r.reason = "playlist 里没有分片"; return r; }
                byte[] head = getBytes(abs(cur, seg), headers, 32, err);
                if (head == null || head.length < 8) { r.reason = "分片取回失败" + err; return r; }
                if ((head[0] & 0xFF) == 0x47) { r.ok = true; r.url = cur; r.kind = "ts"; return r; }
                String b4 = new String(head, 4, 4, "ISO-8859-1");
                if (b4.equals("ftyp") || b4.equals("styp") || b4.equals("moof")) {
                    r.ok = true; r.url = cur; r.kind = "fmp4"; return r;
                }
                r.reason = "分片不是明文 TS/fMP4（疑似加密或私有加扰）";
                return r;
            }
            r.reason = "playlist 下钻层数过多";
        } catch (Throwable t) {
            r.reason = "探测异常：" + t;
        }
        return r;
    }

    /** 列表里是否声明了真加密（METHOD=NONE 不算） */
    private static boolean hasKey(String body) {
        int i = 0;
        while (true) {
            int k = body.indexOf("#EXT-X-KEY", i);
            if (k < 0) return false;
            int e = body.indexOf('\n', k);
            String line = body.substring(k, e < 0 ? body.length() : e);
            if (line.indexOf("METHOD=NONE") < 0) return true;
            i = k + 10;
        }
    }

    /** 取第一条非注释、非空行（HLS 里 = 下一条子列表 / 第一个分片） */
    private static String firstNonComment(String body) {
        int i = 0, n = body.length();
        while (i < n) {
            int e = body.indexOf('\n', i);
            if (e < 0) e = n;
            String line = body.substring(i, e).trim();
            i = e + 1;
            if (line.length() > 0 && line.charAt(0) != '#') return line;
        }
        return null;
    }

    /** 相对地址 → 绝对地址（HLS 里绝大多数是相对当前列表目录） */
    private static String abs(String base, String rel) {
        try {
            if (rel.startsWith("http://") || rel.startsWith("https://")) return rel;
            return new URL(new URL(base), rel).toString();
        } catch (Throwable t) {
            return rel;
        }
    }

    private static String getText(String url, Map<String, String> headers, StringBuilder err) {
        byte[] b = getBytes(url, headers, MAX_BYTES, err);
        if (b == null) return null;
        try { return new String(b, "UTF-8"); } catch (Throwable t) { return null; }
    }

    /** GET 前 n 字节；失败返回 null，并把原因追加到 err（便于定位 403/404/超时） */
    private static byte[] getBytes(String url, Map<String, String> headers, int n, StringBuilder err) {
        HttpURLConnection c = null;
        InputStream in = null;
        try {
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(CONNECT_MS);
            c.setReadTimeout(READ_MS);
            c.setInstanceFollowRedirects(true);
            c.setRequestProperty("Accept", "*/*");
            c.setRequestProperty("Connection", "close");
            if (headers != null) {
                for (Map.Entry<String, String> e : headers.entrySet()) {
                    String k = e.getKey();
                    String v = e.getValue();
                    if (k != null && v != null && k.length() > 0 && v.length() > 0) {
                        c.setRequestProperty(k, v);
                    }
                }
            }
            int code = c.getResponseCode();
            if (code < 200 || code >= 300) {
                if (err != null) err.append("(HTTP ").append(code).append(")");
                return null;
            }
            in = c.getInputStream();
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int total = 0, r;
            while (total < n && (r = in.read(buf, 0, Math.min(buf.length, n - total))) > 0) {
                bos.write(buf, 0, r);
                total += r;
            }
            return bos.toByteArray();
        } catch (Throwable t) {
            if (err != null) err.append("(").append(t.getClass().getSimpleName()).append(")");
            return null;
        } finally {
            try { if (in != null) in.close(); } catch (Throwable ignore) {}
            try { if (c != null) c.disconnect(); } catch (Throwable ignore) {}
        }
    }

    /**
     * 「带 Referer/Cookie 的流地址」→ 交给播放器的请求头 Map。
     *
     * ★ User-Agent **保留**（不再剔除）：页面就是用它的 UA 从服务器换到这条流的
     *   地址，播放器用同一个 UA 请求分片才最一致 —— 换成播放器那套 App UA
     *   有可能被 CDN 判成「另一个客户端」而拒掉。
     *   （媒体3 里 defaultRequestProperties 的 User-Agent 会覆盖工厂的 UA，见实现。）
     */
    static Map<String, String> parseHeaders(String json) {
        if (json == null || json.length() < 3) return null;
        try {
            org.json.JSONObject o = new org.json.JSONObject(json);
            HashMap<String, String> m = new HashMap<String, String>();
            Iterator<String> it = o.keys();
            while (it.hasNext()) {
                String k = it.next();
                String lk = k.toLowerCase();
                /* accept-encoding 不能带给播放器：media3 的 data source 自己会协商，
                   手工设成 gzip 而它不去解压就是一堆乱码 */
                if (lk.equals("accept-encoding")) continue;
                String v = o.optString(k, "");
                if (v != null && v.length() > 0) m.put(k, v);
            }
            return m.isEmpty() ? null : m;
        } catch (Throwable t) {
            return null;
        }
    }
}
