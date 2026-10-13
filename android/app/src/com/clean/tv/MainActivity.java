package com.clean.tv;

import android.app.Activity;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

/**
 * 纯净电视 · WebView 壳
 *
 * 职责：
 *  1) 伪装 UA（服务器靠 UA 返回新版播放器页）
 *  2) 接管「全屏」—— WebView 的 requestFullscreen 在 Android 上只铺满页面，
 *     不会改变 Activity 朝向。手机竖屏时视频很小，所以这里必须用原生
 *     setRequestedOrientation 真正旋转成横屏，并把系统栏藏掉。
 *  3) 返回键：优先退出播放层，其次退出全屏，最后才结束 Activity
 *  4) 暴露 JS 桥，让网页按钮/双击/进入播放都能触发原生横屏
 */
public class MainActivity extends Activity {

    private WebView web;
    private FrameLayout root;
    private NativePlayer nplayer;
    /** 网页回调句柄：原生播放状态变化时调用网页 window.__onNativeState */
    private volatile boolean webReady = false;

    /**
     * 官网源「网页线路」用的覆盖层 WebView。
     * 官方直播页（CCTV 等）用 WASM+MSE 播放，流地址抓出来谁也用不了 ——
     * 只能真开一个 WebView 让浏览器自己播，再注入脚本把 <video> 铺满全屏。
     * 与 WebViewTV 同策略。null 表示当前没有打开。
     */
    private WebView pageView;
    private String pageInjectJs;

    /* ---------------- v2.10.9 单击手势（唤出半透明浮层） ----------------
       在 Activity 级 dispatchTouchEvent 里统一检测（官方页 pageView 盖在
       主 WebView 上面时，网页层的手势根本收不到触摸 —— 所以必须在原生层）。
       判定（v2.10.9 用户定稿，长按改单击）：短按（<450ms、位移<60px）即触发，
       按 **起始位置** 分：左半屏 = 频道列表 openChOv()；右半屏 = 设置 openSetOv()。
       是否唤出由网页侧 __nativeTapOverlay 最终判定 —— 点在控制条 / 确认卡 /
       线路面板等可点 UI 上时网页自己跳过，浮层不抢交互。
       大幅竖向移动（亮度/音量手势）不触发。触发时官方页 pageView 只降
       alpha 到 0.35（★ 不能 INVISIBLE —— 那会触发 Chromium 引擎级暂停媒体，
       视频直接断；半透明则页面继续渲染、视频一直可见，浮层从底下透出来），
       关浮层时恢复 1。 */
    private float gDownX, gDownY;
    private long gDownT;
    private boolean gDragged;

    /** 单击已确认：唤出浮层（right=true 设置 / false 频道列表） */
    private void fireOverlay(boolean right) {
        ovOpenUI = true;
        /* ★ v2.10.11：官方页未起播保持 alpha 0（黑底过渡中），别把原始网页拉到 0.35 */
        try { if (pageView != null && pagePlayingSeen) pageView.setAlpha(0.35f); } catch (Throwable ignore) {}
        // __ovGuardUntil：抬起产生的 click 别把刚打开的浮层关掉；__nativeTapAt 挡住
        // 同一次单击在网页 onTap 里的「切控制条」动作
        jsApp("window.__ovGuardUntil=Date.now()+900;window.__nativeTapAt=Date.now();"
            + "try{" + (right ? "openSetOv&&openSetOv()" : "openChOv&&openChOv()") + "}catch(e){}");
    }

    /** 落点是否点在网页的可点 UI（控制条/确认卡/线路面板/节目单）上 —— 由注入 JS 判定，
     *  命中则不唤浮层（浮层 alpha0.35 会盖住这些 UI 的点击）。坐标为原生物理像素，
     *  JS 侧按 devicePixelRatio 换算。 */
    private void fireOverlayIfFree(final boolean right, final float x, final float y) {
        if (web == null) { fireOverlay(right); return; }
        /* ★ v2.10.13：未起播（状态页显示中）直接唤出，不问 JS —— 主 web 此时只有
           状态页，不存在控制条/浮层等可点 UI；而官方页加载中渲染器忙，
           evaluateJavascript 往返可能被拖到数秒，用户点了几下全「没反应」
           （CCTV-4 欧洲这类重页面实测复现）。起播后仍走 JS 判定避开可点 UI。 */
        if (!pagePlayingSeen) { fireOverlay(right); return; }
        android.util.Log.i("CleanTV/LC", "tapFire right=" + right + " x=" + (int) x + " y=" + (int) y);
        try {
            web.evaluateJavascript(
                "try{(window.__nativeTapOverlay?__nativeTapOverlay("
                    + (int) x + "," + (int) y + "):1)?'1':'0'}catch(e){'1'}",
                new android.webkit.ValueCallback<String>() {
                    @Override public void onReceiveValue(String v) {
                        if (v != null && v.indexOf("0") >= 0) return;   // 命中 UI，跳过
                        fireOverlay(right);
                    }
                });
        } catch (Throwable t) { fireOverlay(right); }
    }

    /** 浮层 UI 是否开着（与 pageView alpha 同步维护）。
     *  ★ v2.10.5：开着时 pageView 虽然半透明但仍在最上层接收触摸（v2.10.3 改 alpha
     *  丢掉了 INVISIBLE 的「摘出触摸分发」副作用）——必须在 dispatchTouchEvent 里
     *  把触摸直接转发给底层主 WebView（浮层所在处），否则浮层点不到也关不掉。 */
    private boolean ovOpenUI = false;

    @Override
    public boolean dispatchTouchEvent(MotionEvent ev) {
        switch (ev.getActionMasked()) {
            case MotionEvent.ACTION_DOWN:
                gDownX = ev.getX(); gDownY = ev.getY();
                gDownT = ev.getEventTime();
                gDragged = false;
                break;
            case MotionEvent.ACTION_MOVE:
                /* 大幅移动（亮度/音量手势、滑动）不算单击 */
                if (Math.abs(ev.getY() - gDownY) > 60 || Math.abs(ev.getX() - gDownX) > 60)
                    gDragged = true;
                break;
            case MotionEvent.ACTION_UP:
                /* v2.10.9 长按改单击：短按（<450ms、无明显拖动）唤浮层。
                   是否点在控制条等可点 UI 上由网页 __nativeTapOverlay 判定（异步）。 */
                if (!ovOpenUI && !gDragged && ev.getEventTime() - gDownT < 450) {
                    fireOverlayIfFree(gDownX * 2 > Math.max(1, root.getWidth()), gDownX, gDownY);
                }
                break;
        }
        /* ★ 浮层开着：触摸绕过半透明的 pageView，直接送主 WebView（长按手势检测在上面已做完，
           状态一致，整段事件流都走同一条路）。 */
        if (ovOpenUI && web != null) {
            try { web.dispatchTouchEvent(ev); } catch (Throwable ignore) {}
            return true;
        }
        /* ★ v2.10.13：官方页加载中（未起播）pageView 虽 alpha0 但仍在最上层收触摸 ——
           用户点下的位置被隐形官方页吃掉，等起播淡入后这些点击恰好落在页面控件上
           就「凭空生效」（用户实测：加载中点不动，加载完乱跳）。直接吞掉 ——
           唤浮层已在上面 UP 分支处理完（ovOpenUI=true 走不到这里）。
           ★ 不能用 INVISIBLE：会触发 Chromium 引擎级暂停，页面起播判定可能死锁。 */
        if (!pagePlayingSeen && pageView != null) {
            return true;
        }
        return super.dispatchTouchEvent(ev);
    }

    /** 传给注入脚本的适配规则与频道附加参数 */
    private String pageAdapterJson = "{}";
    private String pageExtra = "";
    /**
     * 覆盖层「代次」。每次 showPage 自增；closePage 延后 150ms 才真正拆 WebView，
     * 若这段时间里已经开了新的一页（换下一条线路就是这种），代次对不上就直接放弃，
     * 免得把新页面拆了。
     */
    private int pageSeq = 0;
    /** 本页是否已经报过「起不来」；成功起播或换页时复位，避免反复触发切线路 */
    private boolean pageFailedSent = false;
    /** 官网页是否已起播（原生看门狗用：起播后不再判死） */
    private boolean pagePlayingSeen = false;
    /** 是否 TV 设备：BACK 在播放层的两段式（先退全屏）只留给遥控器；手机一步退播放 */
    private boolean isTvDevice = false;

    /* ==================== 官网页流地址嗅探（v2.9） ====================
       官网源 94.5% 的线路是「官方直播页」，网页端用 WASM+MSE 自己播 —— 但那是
       软解，电视盒子/低端机上 CPU 吃满。安卓的 WebView 本来就加载着那个页面，
       用 WebViewClient.shouldInterceptRequest 就能在页面自己发请求的瞬间把
       *.m3u8 截下来，零额外开销（不像桌面端要另起浏览器 + CDP，冷启动 30s+）。
       截到 ≠ 能播，所以还要后台探测一遍（SniffProbe），结论推给网页侧决定用不用。 */
    /** 当前官网页地址（嗅探请求的 Referer 基准） */
    private volatile String pageUrlNow = "";
    /** 当前官网页的 UA（嗅探/探测请求必须带 —— CDN 常见做法是拦掉空 UA 或 Java 默认 UA）。
        ★ 必须在 showPage（UI 线程）时抓好存下：maybeSniff 跑在网络 IO 线程，
          跨线程读 pageView.getSettings() 会踩 WebView 的非线程安全。 */
    private volatile String pageUaNow = "";
    /** 最近一次已处理的嗅探地址（HLS 会周期刷新 playlist，同一地址只处理一次） */
    private volatile String sniffLastSeen = "";
    /** 本页已探测的地址条数上限（见 maybeSniff 里「第一条常常是废的」说明） */
    private static final int MAX_SNIFF_TRIES = 5;
    private volatile int sniffTries = 0;
    /** 本页是否已有可播结论（或已判死）—— 有就收工，不再看后续 m3u8 */
    private volatile boolean sniffDone = false;
    /** ★ v2.10.23 已做过「提前注入」的页号（= pageSeq）。进度 25% 先打一次
        注入脚本，避免同一页在加载过程中重复注入。 */
    private volatile int pageInjectedSeq = -1;

    /** 桌面 / 移动两套 UA —— 适配规则里的 ua 字段决定用哪套 */
    private static final String UA_PC =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";
    private static final String UA_MOBILE =
        "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36";

    /**
     * 网页通过 window.NativeTV.* 调用原生能力。
     * 命名前缀 native* 的属于「原生播放器」通道，其余是「界面/方向」通道。
     */
    public class NativeBridge {
        /* ---------------- 界面 / 方向 ---------------- */

        @JavascriptInterface
        public void setLandscape(final boolean on) {
            runOnUiThread(new Runnable() {
                @Override public void run() { applyOrientation(on); }
            });
        }

        /** 网页全屏状态变化时同步系统栏（沉浸式） */
        @JavascriptInterface
        public void setImmersive(final boolean on) {
            runOnUiThread(new Runnable() {
                @Override public void run() { applySystemUi(on); }
            });
        }

        /** 返回网页一个能力标记，方便网页决定走原生还是走 Web Fullscreen API */
        @JavascriptInterface
        public boolean hasNativeFs() { return true; }

        /** 屏幕是否已处于横屏（按物理宽高判断） */
        @JavascriptInterface
        public boolean isLandscape() {
            Configuration c = getResources().getConfiguration();
            return c.orientation == Configuration.ORIENTATION_LANDSCAPE;
        }

        /* ---------------- 原生硬解播放器通道 ----------------
           返回 true 表示「已接管」，网页就不要再用 mpegts.js 软解。
           网页侧调用约定见 player.html 的 NatPlay 封装。 */

        /** 是否有可用的原生播放器（按系统版本已挑好硬解后端） */
        @JavascriptInterface
        public boolean hasNativePlay() {
            return nplayer != null && nplayer.available();
        }

        /** 开始播放（url 为解密后的 FLV/HLS 直链） */
        @JavascriptInterface
        public void nativePlay(final String url) {
            if (nplayer == null || !nplayer.available()) return;
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    nplayer.play(url);
                }
            });
        }

        /** 切线路：换源不销毁播放器，避免黑屏等待 */
        @JavascriptInterface
        public void nativeSwitch(final String url) {
            if (nplayer == null) return;
            nplayer.switchTo(url);
        }

        /**
         * 播放官网源「提取到的流地址」（v2.9）。
         *
         * headersJson = {"Referer":..,"Cookie":..}，来自 __onSniffResult 的透传 ——
         * 提取到的流几乎都校验 Referer/Cookie，不带就是 403。
         * ★ 必须全量重建播放器（请求头在构建 DataSource 时设死），所以走 play 不走 switchTo。
         */
        @JavascriptInterface
        public void nativePlayExt(final String url, final String headersJson) {
            if (nplayer == null || !nplayer.available()) return;
            if (url == null || url.length() == 0) return;
            runOnUiThread(new Runnable() {
                @Override public void run() { nplayer.play(url, headersJson); }
            });
        }

        @JavascriptInterface
        public void nativePause()  { if (nplayer != null) nplayer.pause(); }
        @JavascriptInterface
        public void nativeResume() { if (nplayer != null) nplayer.resume(); }

        /**
         * ★ 强制暂停主 WebView 的媒体（软解 <video> 的声音通道）。
         * 实测（Android 9 / Chromium 91）：closePlayer 里 v.pause()+load() 之后，
         * WebView 内部的 OpenSL ES 播放器仍在出声（dumpsys media.audio_flinger
         * active track 不归零）—— 只有 web.onPause() 能真正停掉它。
         * 场景：BACK 关播放层时 Activity 还在前台，onPause() 生命周期不会触发，
         * 必须由网页显式调用。恢复由 resumeWebMedia() 在下次起播时触发，
         * 不能定时自动恢复 —— 否则页面的「回前台恢复播放」逻辑会把已清理的
         * video 重新 play()，音频通道又挂起来。
         */
        @JavascriptInterface
        public void pauseWebMedia() {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    try { if (web != null) web.onPause(); } catch (Throwable ignore) {}
                }
            });
        }

        /** 起播前恢复 WebView 媒体处理（配对 pauseWebMedia） */
        @JavascriptInterface
        public void resumeWebMedia() {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    try { if (web != null) web.onResume(); } catch (Throwable ignore) {}
                }
            });
        }

        /** 退出播放层：释放解码器与画面层，把 WebView 重新露出来 */
        @JavascriptInterface
        public void nativeStop() {
            if (nplayer == null) return;
            nplayer.destroy();
            runOnUiThread(new Runnable() {
                @Override public void run() { web.bringToFront(); }
            });
        }

        /**
         * 退出播放层（保帧版）：释放解码器但**保留画面层与最后一帧**。
         * 下次开台网页立刻把 WebView 转透明，上一台的画面就在那里 ——
         * 不再有「BACK → 点台」之间那段全黑（实测 3.36s）。
         * 返回 false 表示没有画面层可保留，网页不应提前转透明。
         */
        @JavascriptInterface
        public boolean nativeFreeze() {
            if (nplayer == null) return false;
            return nplayer.freeze();
        }

        /** 取播放统计（JSON 字符串），网页用来做卡顿判定。
         *
         *  ★★ 必须弹回主线程再读（v2.6 踩坑，实测确诊）：
         *  media3 的 ExoPlayer 有线程校验 —— 只能在创建它的线程（这里是主线程）
         *  上访问，否则抛 {@code IllegalStateException("Player is accessed on the
         *  wrong thread")}。而 {@code @JavascriptInterface} 的方法跑在 WebView 的
         *  JavaBridge 线程上，直调必抛，异常被 {@code backend.stats()} 的 catch
         *  吞成 "{}"。实测（tools/_probe_stats.py）播放中 4/4 都返回 {} ——
         *  等于网页侧那个 8s「零推进」卡顿看护（bindNativeWatch）**从来没生效过**。
         *
         *  这里 post 到主线程并最多等 300ms。不会死锁：主线程从不等待 JS
         *  （evaluateJavascript 是异步投递），阻塞的只是 JavaBridge 线程自己。
         */
        @JavascriptInterface
        public String nativeStats() {
            if (nplayer == null) return "{}";
            if (android.os.Looper.myLooper() == android.os.Looper.getMainLooper()) {
                return nplayer.stats();
            }
            final String[] box = {"{}"};
            final java.util.concurrent.CountDownLatch latch =
                    new java.util.concurrent.CountDownLatch(1);
            /* 用主线程 Handler 而不是 web.post：不依赖 WebView 还活着 */
            boolean posted = new android.os.Handler(android.os.Looper.getMainLooper())
                    .post(new Runnable() {
                        @Override public void run() {
                            try { box[0] = nplayer.stats(); }
                            catch (Throwable ignore) { }
                            finally { latch.countDown(); }
                        }
                    });
            if (!posted) return "{}";
            try { latch.await(300, java.util.concurrent.TimeUnit.MILLISECONDS); }
            catch (InterruptedException e) { Thread.currentThread().interrupt(); }
            return box[0];
        }

        /** 是否命中硬件解码 */
        @JavascriptInterface
        public boolean nativeHwDecode() {
            return nplayer != null && nplayer.isHardwareDecode();
        }

        /* ---------------- 播放档位 ----------------
           网页底栏的「秒开 / 稳定」按钮切这里。只写 volatile + SharedPreferences，
           不碰播放器对象 —— 本方法跑在 WebView 的 JavaBridge 线程上，碰播放器
           会触发 media3 的线程校验异常（v2.4.8 那个坑）。下一次 play() 生效。 */
        @JavascriptInterface
        public void setTuneProfile(final String p) {
            PlayTuning.setProfile(MainActivity.this, p);
        }

        @JavascriptInterface
        public String getTuneProfile() { return PlayTuning.getProfile(); }

        /** 手势调亮度（0~1），改系统亮度 */
        @JavascriptInterface
        public void setBrightness(final float v) {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    try {
                        WindowManager.LayoutParams lp = getWindow().getAttributes();
                        lp.screenBrightness = Math.max(0.01f, Math.min(1f, v));
                        getWindow().setAttributes(lp);
                    } catch (Throwable t) {}
                }
            });
        }

        /** 手势调音量（0~1），改媒体音量 */
        @JavascriptInterface
        public void setVolume(final float v) {
            try {
                android.media.AudioManager am =
                        (android.media.AudioManager) getSystemService(AUDIO_SERVICE);
                if (am != null) {
                    int max = am.getStreamMaxVolume(android.media.AudioManager.STREAM_MUSIC);
                    am.setStreamVolume(android.media.AudioManager.STREAM_MUSIC,
                            Math.round(max * Math.max(0f, Math.min(1f, v))), 0);
                }
            } catch (Throwable t) {}
        }

        /* ---------------- 官网源「网页线路」覆盖层 ---------------- */

        /** 打开官方直播页并注入取流脚本（url=页面地址，adapterJson/extra 来自频道表） */
        @JavascriptInterface
        public void openPage(final String url, final String adapterJson, final String extra) {
            runOnUiThread(new Runnable() {
                @Override public void run() { showPage(url, adapterJson, extra); }
            });
        }

        /** 关掉官方页面层，回到自己的频道列表 */
        @JavascriptInterface
        public void closePage() {
            // 这个调用来自 pageView 自己的 JS 线程：延后一点再 destroy，
            // 免得在脚本执行中途把 WebView 拆掉。
            final int seq = pageSeq;
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    root.postDelayed(new Runnable() {
                        @Override public void run() { hidePage(seq); }
                    }, 150);
                }
            });
        }

        @JavascriptInterface
        public boolean hasWebPage() { return true; }

        /** v2.10：半透明浮层开/关 —— 开时把官方页降到 0.35 alpha（媒体继续渲染），关时恢复。
         *  ★ v2.10.11：官方页**起播前**（pagePlayingSeen=false，还在黑底过渡 alpha 0）
         *  不得见光 —— 实测加载中点击唤出浮层，alpha 被拉到 0.35/恢复 1，原始网页和
         *  原始播放器就透出来了。未起播时开/关都维持 0，起播后 fadeInPage 自然接管。 */
        @JavascriptInterface
        public void setPageOverlay(final boolean on) {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    ovOpenUI = on;   /* 与触摸转发闸门同步（见 ovOpenUI 注释）；pageView 为 null（本机源）也要更新 */
                    try {
                        if (pageView != null) {
                            if (!pagePlayingSeen) { pageView.setAlpha(0f); }
                            else {
                                /* ★ v2.10.3：降 alpha 不 INVISIBLE —— INVISIBLE 会触发
                                   Chromium 引擎级暂停媒体（onVisibilityChanged），
                                   视频直接断；半透明则官方页继续渲染，浮层透出来 */
                                pageView.setAlpha(on ? 0.35f : 1f);
                            }
                            android.util.Log.i("CleanTV/LC", "setPageOverlay " + on
                                + " playing=" + pagePlayingSeen
                                + " alpha=" + pageView.getAlpha()
                                + " vis=" + pageView.getVisibility()
                                + " zTop=" + (root.getChildAt(root.getChildCount()-1) == pageView));
                        }
                    } catch (Throwable ignore) {}
                }
            });
        }

        /** v2.10：TV「确认退出」卡的退出按钮（finish 会走 onDestroy 释放播放器） */
        @JavascriptInterface
        public void exitApp() {
            runOnUiThread(new Runnable() {
                @Override public void run() { finish(); }
            });
        }

        /** 注入脚本判定「视频真的在播了」——切横屏并让网页收起加载遮罩 */
        @JavascriptInterface
        public void pagePlaying() {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    pagePlayingSeen = true;     // 已起播：看门狗收兵
                    pageFailedSent = false;      // 已起播：允许下次超时再触发
                    applyOrientation(true);
                    /* ★ v2.10.13：见 fadeInPage 内的 ovOpenUI 守卫（浮层开着不能拉亮） */
                    fadeInPage();
                    try {
                        web.evaluateJavascript(
                            "window.__onWebPagePlaying&&window.__onWebPagePlaying()", null);
                    } catch (Throwable ignore) {}
                }
            });
        }

        /**
         * 注入脚本判定「这条线路起不来」（站点提示 / 播放器空转 / 超时）。
         * reason 是站点给的原因文本（VIP 专享、播出安排变更…），透传给网页侧
         * 展示 —— 用户得知道为什么播不了，而不是对着黑屏猜。
         * 交给网页侧决定换下一条线路还是收工。
         */
        @JavascriptInterface
        public void pageFailed(final String reason) {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    if (pageFailedSent) return;
                    pageFailedSent = true;
                    String lit;
                    try {
                        lit = org.json.JSONObject.quote(reason == null ? "" : reason);
                    } catch (Throwable t) { lit = "\"\""; }
                    try {
                        web.evaluateJavascript(
                            "window.__onWebPageFailed&&window.__onWebPageFailed(" +
                            lit + ")", null);
                    } catch (Throwable ignore) {}
                }
            });
        }
    }

    // 关键：服务器靠该 UA 才返回新版播放器页（后端播放器复用同一 UA）
    static final String APP_UA =
        "Mozilla/5.0 (Linux; Android 9; SM-N9700 Build/PQ3B.190801.01311438; wv) " +
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 " +
        "Mobile Safari/537.36 diashizhb LT-APP/48/517/YM-RT/";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        try { WebView.setWebContentsDebuggingEnabled(true); } catch (Throwable t) {}
        /* ★ 播放档位（秒开 / 稳定）：读上次的选择，两个后端构建播放器时会用 */
        PlayTuning.init(this);

        // ★ 关键：硬件加速决定 1080p 解码流畅度，缺失会被降级到 CPU 软解
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_HARDWARE_ACCELERATED,
                             WindowManager.LayoutParams.FLAG_HARDWARE_ACCELERATED);
        // 保持屏幕常亮（直播场景）
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        root = new FrameLayout(this);
        web = new WebView(this);
        // ★ 原生硬解：WebView 背景透明，让下方的 SurfaceView 画面透出来
        //   （网页在 native-on 模式下也会把 body/列表置透明，只留控制条浮层）
        web.setBackgroundColor(Color.TRANSPARENT);
        root.setBackgroundColor(Color.BLACK);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(true);
        try { s.setAllowFileAccessFromFileURLs(true); } catch (Throwable t) {}
        try { s.setAllowUniversalAccessFromFileURLs(true); } catch (Throwable t) { }
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setUserAgentString(APP_UA);
        if (Build.VERSION.SDK_INT >= 21) {
            s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        }
        // ★ 解码/渲染优化：WebView 默认就是硬件加速渲染。
        //   注意不要 setLayerType(LAYER_TYPE_HARDWARE)——离屏层会破坏透明度，
        //   导致底层 SurfaceView 的视频画面透不出来（黑屏）。
        try { s.setRenderPriority(WebSettings.RenderPriority.HIGH); } catch (Throwable t) {}
        if (Build.VERSION.SDK_INT >= 26) {
            try { web.setRendererPriorityPolicy(WebView.RENDERER_PRIORITY_IMPORTANT, false); } catch (Throwable t) {}
        }
        try { s.setCacheMode(WebSettings.LOAD_DEFAULT); } catch (Throwable t) {}
        // 允许网页用 <video playsinline> 内联播放并配合原生横屏
        try { s.setSupportMultipleWindows(false); } catch (Throwable t) {}

        // JS 桥：全屏/横屏 + 原生硬解播放器
        web.addJavascriptInterface(new NativeBridge(), "NativeTV");

        // ★ 原生硬解播放器：ExoPlayer 承载画面，WebView 只画控制条浮层。
        //   注意顺序——surface 必须加在 WebView 之下，WebView 里的控制条才能盖住它。
        nplayer = new NativePlayer(this, root);
        nplayer.setListener(new NativePlayer.Listener() {
            @Override
            public void onState(final String state, final String detail) {
                // 把状态透传给网页（网页用 window.__onNativeState 接收）
                if (!webReady || web == null) return;
                runOnUiThread(new Runnable() {
                    @Override public void run() {
                        try {
                            web.evaluateJavascript(
                                "window.__onNativeState&&window.__onNativeState(" +
                                jsStr(state) + "," + jsStr(detail) + ")", null);
                        } catch (Throwable ignore) {}
                    }
                });
            }
        });

        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                webReady = true;
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onShowCustomView(View view, CustomViewCallback cb) {
                // 若网页启用了原生 <video> 全屏（Web Fullscreen API），同样转横屏
                root.addView(view, new FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
                applyOrientation(true);
            }
            @Override
            public void onHideCustomView() {
                root.removeAllViews();
                root.addView(web, new FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
                applyOrientation(false);
            }
            /* ★ JS console → logcat（v2.10.9）：网页侧 JS 报错在电视盒子上无法
               可视化调试，转发到 logcat tag "CleanTV/JS"（adb logcat -s CleanTV/JS）。 */
            @Override
            public boolean onConsoleMessage(android.webkit.ConsoleMessage cm) {
                android.util.Log.i("CleanTV/JS", "[" + cm.messageLevel() + "] "
                        + cm.message() + " @" + cm.sourceId() + ":" + cm.lineNumber());
                return true;
            }
        });

        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        boolean isTV = (getResources().getConfiguration().uiMode & Configuration.UI_MODE_TYPE_MASK)
                            == Configuration.UI_MODE_TYPE_TELEVISION
                    || getPackageManager().hasSystemFeature(PackageManager.FEATURE_LEANBACK)
                    || "tv".equalsIgnoreCase(android.os.Build.DEVICE);
        isTvDevice = isTV;
        // TV / 平板默认就是横屏大屏，保持传感器自适应；手机则默认竖屏（频道列表），
        // 点全屏时才由 JS 桥切成横屏。
        applyOrientation(isTV);
        web.loadUrl("file:///android_asset/player.html" + (isTV ? "?tv=1" : ""));

        /* ★ v2.10.16 pageView 预热：官方页 WebView 的「实例创建 + Chromium 渲染
           引擎冷启动」在 TV 低端盒子上要 0.5-2s，过去全摊在用户第一次切网页
           线路的那次换台里。启动 5s 后（主页面就绪、首屏稳定）在后台把 pageView
           建好、load 一个 about:blank 把渲染进程拉起来 —— 第一次网页线路换台
           直接复用热实例。代价 ~20MB 常驻内存，起播提速换得。
           · about:blank 不触发 onPageFinished/注入（上面已有非 http(s) 守卫）；
           · 销毁仍走 hidePage 全链（onDestroy 兜底），无泄漏路径。 */
        web.postDelayed(new Runnable() {
            @Override public void run() {
                try {
                    WebView v = ensurePageView();
                    if (v != null) v.loadUrl("about:blank");
                    android.util.Log.i("CleanTV/LC", "pageView 预热完成");
                } catch (Throwable t) {
                    android.util.Log.i("CleanTV/LC", "pageView 预热失败 " + t);
                }
            }
        }, 5000);
    }

    /** 把字符串安全地嵌进 JS 字面量 */
    private static String jsStr(String s) {
        if (s == null) return "\"\"";
        StringBuilder sb = new StringBuilder(s.length() + 8);
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':  sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n");  break;
                case '\r': sb.append("\\r");  break;
                case '<':  sb.append("\\u003c"); break;   // 防止 </script> 提前闭合
                default:
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
            }
        }
        sb.append('"');
        return sb.toString();
    }

    /**
     * 真正切换屏幕方向。
     * @param landscape true=横屏（全屏播放），false=竖屏（频道列表）
     *
     * 用 SENSOR_LANDSCAPE / SENSOR_PORTRAIT 而非 LANDSCAPE / PORTRAIT，
     * 这样横屏时可左右翻转（用户把手机转 180° 依然正看），竖屏亦然。
     */
    private void applyOrientation(boolean landscape) {
        try {
            if (landscape) {
                // 如果设备本身是横屏大屏（TV/平板），用 fullSensor 让它自适应
                setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE);
            } else {
                setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT);
            }
        } catch (Throwable t) {}
        applySystemUi(landscape);
    }

    /** 全屏时藏系统栏（沉浸式），退出时恢复 */
    private void applySystemUi(boolean immersive) {
        try {
            View decor = getWindow().getDecorView();
            if (immersive) {
                decor.setSystemUiVisibility(
                        View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                      | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                      | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                      | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                      | View.SYSTEM_UI_FLAG_FULLSCREEN
                      | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
            } else {
                decor.setSystemUiVisibility(View.SYSTEM_UI_FLAG_VISIBLE);
            }
        } catch (Throwable t) {}
    }

    /* ==================== 官网源：官方页面覆盖层 ==================== */

    /** 读取 assets/wvt_inject.js（只读一次） */
    private String injectJs() {
        if (pageInjectJs != null) return pageInjectJs;
        java.io.InputStream in = null;
        try {
            in = getAssets().open("wvt_inject.js");
            java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            pageInjectJs = new String(bos.toByteArray(), "UTF-8");
        } catch (Throwable t) {
            pageInjectJs = "";
        } finally {
            try { if (in != null) in.close(); } catch (Throwable ignore) {}
        }
        return pageInjectJs;
    }

    private WebView ensurePageView() {
        if (pageView != null) return pageView;
        WebView v = new WebView(this);
        v.setBackgroundColor(Color.BLACK);
        /* ★ v2.10.13：官方页不参与按键焦点 —— WebView 默认可聚焦，showPage 把它
           挂上来后可能夺走主 WebView 的焦点，网页线路加载中按 DPAD/数字键换台
           全被官方页吃掉（用户实测：状态页「正在拉起播放」时换台无效）。
           官方页只需要触摸和渲染，不需要按键；锁死后系统焦点始终留在主 WebView，
           换台/数字选台/OK 在加载中照样通。 */
        v.setFocusable(false);
        v.setFocusableInTouchMode(false);
        WebSettings s = v.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        // ★ 系统字体缩放（设置里的「字体大小」）会连带放大网页文字，把布局撑开
        //   —— 官网页面的播放器是自适应布局，一放大就「显示不全」。锁死 100%。
        try { s.setTextZoom(100); } catch (Throwable t) {}
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setDisplayZoomControls(false);
        try { s.setSupportMultipleWindows(false); } catch (Throwable t) {}
        if (Build.VERSION.SDK_INT >= 21) {
            s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        }
        try { s.setCacheMode(WebSettings.LOAD_DEFAULT); } catch (Throwable t) {}
        v.addJavascriptInterface(new NativeBridge(), "NativeTV");

        v.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                view.loadUrl(url);          // 站内跳转留在本 WebView，不要甩给系统浏览器
                return true;
            }
            /**
             * ★ 官网页自己请求 *.m3u8 的瞬间把它截下来（v2.9）。
             *
             * 这个方法跑在 WebView 的**网络 IO 线程**上：只能改几个 volatile 字段，
             * 绝不能碰 UI / 播放器。
             * 返回 null = 「我不处理，走默认网络栈」—— 页面自己的 MSE 播放完全不受影响，
             * 我们只是「旁听」了一下请求地址。若返回一个 WebResourceResponse 就会
             * 截断页面自己的拉流，那是灾难。
             */
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest req) {
                try {
                    if (req != null && req.getUrl() != null) {
                        /* ★ 把 WebView 自己发的头也带出来做探测 —— 服务端校验的往往
                           不只是 Referer（还可能是 Accept / Sec-Fetch-* / 自定义头）。
                           WebResourceRequest 出了本回调就失效，所以立刻复制一份。 */
                        java.util.HashMap<String, String> hd = null;
                        try {
                            java.util.Map<String, String> raw = req.getRequestHeaders();
                            if (raw != null && !raw.isEmpty()) hd = new java.util.HashMap<String, String>(raw);
                        } catch (Throwable ignore) {}
                        maybeSniff(req.getUrl().toString(), hd);
                    }
                } catch (Throwable ignore) {}
                return null;
            }
            @Override
            public void onPageFinished(WebView view, String url) {
                /* ★ v2.10.16：pageView 预热会 load 一个 about:blank，也会走到这里 ——
                   非真实网页一律静默，别把「页面已就绪」回调打进主 WebView
                   （那会误推进 webState、误改状态行文案）。 */
                if (url == null || !(url.startsWith("http://") || url.startsWith("https://"))) return;
                runOnUiThread(new Runnable() {
                    @Override public void run() {
                        try {
                            if (web != null) {
                                web.evaluateJavascript(
                                    "window.__onWebPageReady&&window.__onWebPageReady()", null);
                            }
                        } catch (Throwable ignore) {}
                    }
                });
                injectIntoPage(view);
            }
        });
        v.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onShowCustomView(View view, CustomViewCallback cb) {
                applyOrientation(true);
            }
            @Override
            public void onHideCustomView() {
                applyOrientation(true);
            }
            @Override
            public void onProgressChanged(WebView view, int p) {
                // 很多直播页是 SPA，初始加载完才挂视频；进度到 100 再补一次注入
                // ★ v2.10.16：预热页（about:blank）不注入 —— 别往空页上打注入脚本
                try {
                    String u = view.getUrl();
                    if (u == null || !(u.startsWith("http://") || u.startsWith("https://"))) return;
                } catch (Throwable t) {}
                /* ★ v2.10.23 提前注入：过去只在 onPageFinished 打脚本，实测重站点
                   要 1.3~1.5s 才 finished，注入脚本再花 ~0.9s 摸到 <video>，官方页
                   自己起播也被拖在这后面。wvt_inject.js 本身就是「轮询等 <video>
                   + 钉全屏 + 自动起播」的自适应逻辑，其文件头注释写明它是按
                   document-start 设计（DOM 没成形照样跑），所以进度 25% 时
                   （文档头已就绪、body 还没下完）就可以先打上去。
                   注入脚本自带 __wvtInjected 幂等守卫，重复注入只会走 wvt_resume；
                   onPageFinished 与 p==100 仍各补一次（SPA 站换 <video> 时用）。 */
                if (p >= 25 && pageInjectedSeq != pageSeq) {
                    pageInjectedSeq = pageSeq;      /* 本页只提前注入一次 */
                    injectIntoPage(view);
                }
                if (p == 100) injectIntoPage(view);
            }
            @Override
            public boolean onConsoleMessage(android.webkit.ConsoleMessage cm) {
                // 注入脚本（wvt_inject.js）与提取链（嗅探）的日志落到 logcat，方便排查：
                //   adb logcat -s CleanTV
                if (cm != null && cm.message() != null
                        && (cm.message().indexOf("[WVT]") >= 0
                            || cm.message().indexOf("[SNIFF]") >= 0)) {
                    android.util.Log.i("CleanTV", cm.message());
                }
                return true;
            }
        });
        pageView = v;
        return v;
    }

    /** 把适配规则 + 注入脚本打到官方页面上 */
    private void injectIntoPage(WebView view) {
        if (view == null) return;
        String js = injectJs();
        if (js.length() == 0) return;
        String pre = "window.wvt_adapter=" + pageAdapterJson + ";" +
                     "window.wvt_extra=" + jsStr(pageExtra) + ";";
        try { view.evaluateJavascript(pre + js, null); } catch (Throwable ignore) {}
    }

    private void showPage(String url, String adapterJson, String extra) {
        if (url == null || url.length() == 0) return;
        pageSeq++;                       // 新的一页 —— 让还没落地的 closePage 失效
        pageFailedSent = false;
        /* 嗅探状态按页重置（v2.9）：上一条线路的提取结论绝不能带到这一条 */
        pageUrlNow = (url == null) ? "" : url;
        sniffLastSeen = "";
        sniffTries = 0;
        sniffDone = false;
        pageAdapterJson = (adapterJson != null && adapterJson.length() > 2
                           && adapterJson.charAt(0) == '{'
                           && adapterJson.endsWith("}")) ? adapterJson : "{}";
        pageExtra = (extra == null) ? "" : extra;
        WebView v = ensurePageView();
        boolean mobile = pageAdapterJson.indexOf("\"ua\":\"MOBILE\"") >= 0
                      || pageAdapterJson.indexOf("\"ua\": \"MOBILE\"") >= 0;
        try { v.getSettings().setUserAgentString(mobile ? UA_MOBILE : UA_PC); }
        catch (Throwable t) {}
        /* ★ UA 在 UI 线程抓好存字段（见 pageUaNow 说明）——必须在 setUserAgentString
           之后取，否则拿到的是上一页的 UA。 */
        try {
            String ua = v.getSettings().getUserAgentString();
            pageUaNow = (ua == null || ua.length() == 0) ? UA_PC : ua;
        } catch (Throwable ignore) { pageUaNow = UA_PC; }
        try { v.stopLoading(); } catch (Throwable t) {}
        if (v.getParent() == null) {
            root.addView(v, new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        }
        v.bringToFront();
        v.loadUrl(url);
        /* ★ v2.10.8 过渡：官方页先压在黑底下面（alpha 0）。页面从加载→白底渲染→
           自家转圈→未起播海报（大播放键 + 「× 返回」）这一整段丑态全部不见天日，
           用户看到的是网页层的黑底频道信息页（player.html #status）。
           淡入时机（二选一先到）：
             a) 注入脚本报 playing（reportPlaying，见下）→ 立刻淡入，直接接画面；
             b) 20s 兜底 → 也淡入，别让用户干看黑屏（40s 看门狗照旧判死换线）。 */
        try { v.setAlpha(0f); } catch (Throwable t) {}
        final int fseq = pageSeq;
        root.postDelayed(new Runnable() {
            @Override public void run() {
                if (fseq != pageSeq || pagePlayingSeen) return;   // 已换页/已起播
                fadeInPage();
            }
        }, 20000);
        applyOrientation(true);
        armPageWatchdog();
    }

    /** 官网页淡入（v2.10.8）：页面层从黑底信息页平滑过渡到官方页画面。
     *  ★ v2.10.13：浮层开着时直接早退 —— 加载中唤出浮层（pageView 压到 0/0.35）
     *  后官方页起播，fadeInPage 会把 alpha 拉回 1，官方页全亮盖住浮层，
     *  用户看到「点了没反应」。守卫放这里覆盖所有调用点；
     *  浮层关闭走 setPageOverlay(false) 恢复 1。 */
    private void fadeInPage() {
        if (pageView == null) return;
        if (ovOpenUI) return;
        try {
            if (pageView.getAlpha() >= 1f) return;
            pageView.animate().alpha(1f).setDuration(450).start();
        } catch (Throwable ignore) {
            try { pageView.setAlpha(1f); } catch (Throwable ig) {}
        }
    }

    /**
     * ★ 原生看门狗：官方页 40s 内既没报 playing 也没报 failed 就由原生判死。
     *   页面 JS 把渲染器主线程拖死时，player.html 的定时器和注入脚本的
     *   20s 超时全都会停摆（同一个渲染器）—— 用户只会看到黑屏干等。
     *   判死动作直接调网页层的 __onWebPageFailed（走既有的换线/收工流程）。
     */
    private void armPageWatchdog() {
        final int wseq = pageSeq;
        pagePlayingSeen = false;
        root.postDelayed(new Runnable() {
            @Override public void run() {
                if (wseq != pageSeq) return;        // 已换页/已关闭
                if (pagePlayingSeen) return;        // 起播了，收兵
                if (pageView == null) return;       // 页面已退出
                try {
                    web.evaluateJavascript(
                        "window.__onWebPageFailed&&" +
                        "window.__onWebPageFailed(\"页面长时间无响应\")", null);
                } catch (Throwable ignore) {}
            }
        }, 40000);
    }

    /* ---------------- 官网页流地址嗅探（v2.9） ---------------- */

    /**
     * 官网页自己发出 *.m3u8 / *.mpd 请求时把它截下来（跑在网络 IO 线程）。
     *
     * ★★ 为什么不是「只认第一条」（v2.9 实测踩到的坑，见 tools/_diag_wvt_m3u8.py）：
     *   m.gdtv.cn 这类站点**第一次**请求的 m3u8 是**拿不到数据的**
     *   （PerformanceResourceTiming 里 transferSize=0），页面随即用新 token
     *   请求第二条才成功 —— 真正在播的是第二条。只认第一条的话，探测永远打在
     *   那条废地址上，结果恒 403。所以这里允许逐条探测（上限 MAX_SNIFF_TRIES），
     *   谁探通就用谁；一旦拿到 ok 结论就收工（sniffDone）。
     */
    private void maybeSniff(String url, java.util.Map<String, String> reqHeaders) {
        if (url == null || sniffDone) return;
        String low = url.toLowerCase();
        if (low.indexOf(".m3u8") < 0 && low.indexOf(".mpd") < 0) return;
        if (url.equals(sniffLastSeen)) return;        /* playlist 周期刷新，去重 */
        if (sniffTries >= MAX_SNIFF_TRIES) return;
        sniffLastSeen = url;
        sniffTries++;
        android.util.Log.i("CleanTV/SNIFF", "hit#" + sniffTries + " " + url);
        android.util.Log.i("CleanTV/SNIFF", "webview-sent-headers=" + reqHeaders);

        /* ★ 央视系：TS 载荷私有加扰（路径里带 cdrm）—— 探测会「通过」
           （无 EXT-X-KEY、分片首字节也是 0x47），但原生播出来全是花屏，
           而且花屏是**静默的**（播放器不报错），没法自动回退。
           所以这里直接拦掉，保持网页播放。判据来自 官网源m3u8提取实测.md。 */
        String blocked = blockedReason(low);
        if (blocked != null) {
            sniffDone = true;
            pushSniffResult(false, "", "", blocked, url, "{}");
            return;
        }

        final String page = pageUrlNow;
        final java.util.Map<String, String> sent =
                (reqHeaders == null) ? null : new java.util.HashMap<String, String>(reqHeaders);
        final java.util.Map<String, String> hdrs = sniffHeaders(url, page, sent);
        final String hdrsJson = mapToJson(hdrs);
        new Thread(new Runnable() {
            @Override public void run() {
                SniffProbe.Result r = SniffProbe.probe(url, hdrs);
                /* ★ 403 重试：页面刚发出的 m3u8 请求，它响应里的 Set-Cookie 可能还没
                   落进 CookieManager —— 我们那次请求就是「裸奔」的。
                   等 1.2s 重新取一次 cookie 再试，往往就过了。 */
                if (!r.ok && r.reason.indexOf("403") >= 0) {
                    try { Thread.sleep(1200); } catch (Throwable ignore) {}
                    SniffProbe.Result r2 = SniffProbe.probe(url, sniffHeaders(url, page, sent));
                    if (r2.ok) { r = r2; } else { r.reason = r.reason + " ｜重试:" + r2.reason; }
                }
                if (r.ok) {
                    android.util.Log.i("CleanTV/SNIFF", "PROBE-OK kind=" + r.kind
                            + " url=" + r.url);
                    sniffDone = true;
                    pushSniffResult(true, r.url, r.kind, "", page, hdrsJson);
                } else {
                    /* 失败不推给网页：让 SNIFF.last 只承载「确实可用」的结论，
                       网页侧不必区分「探测过但失败」与「还没结论」。 */
                    android.util.Log.i("CleanTV/SNIFF", "probe-miss " + r.reason);
                }
            }
        }, "sniff-probe").start();
    }

    /** 央视系的流提取了也播不了（实测：300 帧里 182~416 次 h264 解码错误） */
    private static String blockedReason(String lowUrl) {
        String[] bad = {"cctv.com", "cctv.cn", "cntv.cn", "yangshipin", "ysp.cctv", "cctvpic", "/cdrm"};
        for (String b : bad) {
            if (lowUrl.indexOf(b) >= 0) return "央视系内容加扰，保持网页播放";
        }
        return null;
    }

    /**
     * 提取请求要带的头。
     *
     * ★ 以 **WebView 自己发的头**为准（sent），再补上 Referer / Origin / Cookie / UA ——
     *   服务端校验的往往不只是 Referer，还可能是 Accept-Language 或 Sec-Fetch-* 这类
     *   我们猜不出来的东西。第一版只带了猜的四个头，实测被 403（logcat 见
     *   「webview-sent-headers=」那行才是真相）。
     */
    private java.util.Map<String, String> sniffHeaders(String streamUrl, String pageUrl,
                                                       java.util.Map<String, String> sent) {
        java.util.HashMap<String, String> h = new java.util.HashMap<String, String>();
        /* 剔除三类头：
           · accept-encoding —— 手工设上它 HttpURLConnection 就不再自动解压，
             而我们读的是原始字节，「分片首字节 0x47」的判定会被压缩数据打乱；
           · host / connection / content-length / range —— 由连接自己管理，手工设会冲突；
           · referer / origin / user-agent / cookie —— 下面按「页面上下文」统一重设，
             免得 sent 里的名字大小写和我们的两个版本同时存在。 */
        if (sent != null) {
            for (java.util.Map.Entry<String, String> e : sent.entrySet()) {
                String k = e.getKey(), v = e.getValue();
                if (k == null || v == null || v.length() == 0) continue;
                String lk = k.toLowerCase();
                if (lk.equals("accept-encoding") || lk.equals("host") || lk.equals("connection")
                        || lk.equals("content-length") || lk.equals("range")
                        || lk.equals("referer") || lk.equals("origin")
                        || lk.equals("user-agent") || lk.equals("cookie")) continue;
                h.put(k, v);
            }
        }
        try {
            String ref = (pageUrl == null || pageUrl.length() == 0) ? streamUrl : pageUrl;
            h.put("Referer", ref);
            String org = originOf(ref);
            if (org != null) h.put("Origin", org);
            String ua = pageUaNow;
            if (ua == null || ua.length() == 0) ua = UA_PC;
            h.put("User-Agent", ua);
            if (!h.containsKey("Accept")) h.put("Accept", "*/*");
            /* Cookie：目标域优先，取不到再退页面域（反盗链常同时校验 Referer + cookie） */
            String ck = null;
            try { ck = CookieManager.getInstance().getCookie(streamUrl); } catch (Throwable ignore) {}
            if (ck == null || ck.length() == 0) {
                try { ck = CookieManager.getInstance().getCookie(ref); } catch (Throwable ignore) {}
            }
            if (ck != null && ck.length() > 0) h.put("Cookie", ck);
        } catch (Throwable ignore) {}
        return h;
    }

    /** scheme://host[:port] */
    private static String originOf(String u) {
        try {
            Uri x = Uri.parse(u);
            String sc = x.getScheme(), ho = x.getAuthority();
            if (sc == null || ho == null) return null;
            return sc + "://" + ho;
        } catch (Throwable t) { return null; }
    }

    private static String mapToJson(java.util.Map<String, String> m) {
        try {
            org.json.JSONObject o = new org.json.JSONObject();
            for (java.util.Map.Entry<String, String> e : m.entrySet()) o.put(e.getKey(), e.getValue());
            return o.toString();
        } catch (Throwable t) { return "{}"; }
    }

    /**
     * 把提取结论推给网页侧（player.html 决定「优选切换」还是「留作兜底」）。
     * 每条线路只推一次（调用方在此之前已置 sniffDone）；网页侧还会用 page 字段
     * 比对当前线路，换线后旧结果自动作废。
     */
    private void pushSniffResult(final boolean ok, final String url, final String kind,
                                 final String reason, final String page, final String hdrsJson) {
        String hs = (hdrsJson == null || hdrsJson.length() < 3) ? "{}" : hdrsJson;
        hs = hs.replace("<", "\\u003c");     /* 防止 </script> 之类的意外闭合 */
        final String json = "{\"ok\":" + ok
                + ",\"url\":" + jsStr(url == null ? "" : url)
                + ",\"kind\":" + jsStr(kind == null ? "" : kind)
                + ",\"reason\":" + jsStr(reason == null ? "" : reason)
                + ",\"page\":" + jsStr(page == null ? "" : page)
                + ",\"headers\":" + hs + "}";
        runOnUiThread(new Runnable() {
            @Override public void run() {
                try {
                    if (web != null) {
                        web.evaluateJavascript(
                            "window.__onSniffResult&&window.__onSniffResult(" + json + ")", null);
                    }
                } catch (Throwable ignore) {}
            }
        });
    }

    private void hidePage(int seq) {
        android.util.Log.i("CleanTV/LC", "hidePage seq=" + seq + " pageSeq=" + pageSeq
                + " pageView=" + (pageView != null));
        if (seq != pageSeq) return;      // 期间已经换了新的一页，别拆错
        if (pageView == null) return;
        WebView v = pageView;
        pageView = null;
        pageUrlNow = "";                 /* 页面没了，嗅探的 Referer 基准也一并清掉 */
        try { v.stopLoading(); } catch (Throwable t) {}
        try { v.loadUrl("about:blank"); } catch (Throwable t) {}
        try {
            if (v.getParent() != null) root.removeView(v);
            v.removeAllViews();
            v.destroy();
        } catch (Throwable ignore) {}
        try { if (web != null) web.bringToFront(); } catch (Throwable ignore) {}
        try {
            if (web != null) {
                web.evaluateJavascript(
                    "(function(){try{var f=window.__onWebPageClosed;"
                    + "if(typeof f!=='function')return 'nofn';f();return 'ok';}"
                    + "catch(e){return 'EXC:'+e;}})()",
                    new android.webkit.ValueCallback<String>() {
                        @Override public void onReceiveValue(String v) {
                            android.util.Log.i("CleanTV/LC", "__onWebPageClosed -> " + v);
                        }
                    });
            }
        } catch (Throwable ignore) {}
    }

    @Override
    public void onConfigurationChanged(Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
        // 声明了 configChanges 后 Activity 不会重建，手动通知网页调整布局
        try {
            final boolean land = newConfig.orientation == Configuration.ORIENTATION_LANDSCAPE;
            web.evaluateJavascript(
                "(function(){try{window.dispatchEvent(new Event('resize'));" +
                "if(window.onOrientationChanged)window.onOrientationChanged(" + land + ");" +
                "}catch(e){}})()", null);
        } catch (Throwable t) {}
    }

    @Override
    protected void onDestroy() {
        // ★ 必须显式释放 ExoPlayer：否则 MediaCodec 实例与 Surface 会泄漏，
        //   部分机型下次进 App 会因「解码器被占用」起播失败。
        try { hidePage(pageSeq); } catch (Throwable ignore) {}
        try { if (nplayer != null) nplayer.destroy(); } catch (Throwable ignore) {}
        nplayer = null;
        try {
            if (web != null) {
                root.removeView(web);
                web.destroy();
                web = null;
            }
        } catch (Throwable ignore) {}
        super.onDestroy();
    }

    /**
     * ★ HOME/Recents 的最早回调（onPause 之前）：此刻 renderer 还活着，
     *   evaluateJavascript 能立即执行 —— 页面 __appPause 在这里把播放引擎
     *   彻底卸载（实测 onPagePaused 里再 evaluate 会被自己的
     *   web.onPause/pauseTimers 冻结排队，JS 根本没跑，这就是 HOME 残声
     *   多轮修不掉的真正原因）。
     */
    @Override
    protected void onUserLeaveHint() {
        super.onUserLeaveHint();
        android.util.Log.i("CleanTV/LC", "onUserLeaveHint");
        try { if (nplayer != null) nplayer.stop(); } catch (Throwable ignore) {}
        jsApp("window.__appPause&&window.__appPause()");
    }

    @Override
    protected void onPause() {
        super.onPause();
        android.util.Log.i("CleanTV/LC", "onPause");
        // ★ 先让页面 JS 暂停（__appPause：软解 v.pause + 原生 nativePause 双通道）。
        //   实测：HOME 后 WebView 里 document.hidden 不变（View 仍 attached），
        //   网页的 visibilitychange 守卫根本不触发 —— 必须原生直接驱动 JS；
        //   且 web.onPause() 停不掉 MSE 音频（Chromium 91），只有 JS 暂停有效。
        //   （onUserLeaveHint 已提前驱动过一次，这里对锁屏等路径兜底。）
        jsApp("window.__appPause&&window.__appPause()");
        // ★ 原生通道直接彻底释放（stop=backend.release）：不能依赖页面 JS ——
        //   HOME 时 evaluateJavascript 是异步排队的，renderer 随即被冻结，
        //   __appPause 里的 nativeStop 可能永远没跑，ExoPlayer 的 AudioTrack
        //   就在后台继续实时出声（实测 piid AudioTrack 残留的根因）。
        //   回前台由 __appResume 重连流（重新选路）。
        try { if (nplayer != null) nplayer.stop(); } catch (Throwable ignore) {}
        // 挂起 WebView 渲染/网络（省电；__appPause 已先执行完）。
        // ★ setVisibility(INVISIBLE) 是关键：实测 HOME 后 WebView 的 page visibility
        //   仍是 visible（View 树不随 window 隐藏而变化），Chromium 因此认为页面
        //   可见、媒体引擎继续灌数据 —— 显式把 WebView 置 INVISIBLE 触发
        //   onVisibilityChanged → Chromium 引擎级暂停媒体。onResume 恢复 VISIBLE。
        try { web.setVisibility(View.INVISIBLE); } catch (Throwable ignore) {}
        try { if (web != null) web.onPause(); } catch (Throwable ignore) {}
        try { web.pauseTimers(); } catch (Throwable ignore) {}
        // ★ 终极手段：把主 WebView 从视图树摘下来 —— 触发 Chromium
        //   onDetachedFromWindow，引擎级停掉一切媒体输出。实测 JS 卸载、
        //   muted、pauseTimers、INVISIBLE 全都停不住泄漏的 AudioTrack，
        //   只有 detach 有效（同步 Java 操作，不依赖页面 JS）。
        //   DOM/JS 状态都保留在 WebView 里，onResume 重新 attach 即恢复。
        try { root.removeView(web); } catch (Throwable ignore) {}
        // 官网页面层同理：后台别继续放声音
        try { if (pageView != null) pageView.onPause(); } catch (Throwable ignore) {}
        try { if (pageView != null) pageView.setVisibility(View.INVISIBLE); } catch (Throwable ignore) {}
    }

    @Override
    protected void onResume() {
        super.onResume();
        // 回前台：先把 WebView 挂回视图树（onPause 时 detach 过）
        try {
            if (web != null && web.getParent() == null) {
                root.addView(web, new FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
            }
        } catch (Throwable ignore) {}
        // 页面 JS 根据进后台前是否在播来恢复（不自动起播未在播的）
        try { web.resumeTimers(); } catch (Throwable ignore) {}
        try { web.setVisibility(View.VISIBLE); } catch (Throwable ignore) {}
        jsApp("window.__appResume&&window.__appResume()");
        try { if (web != null) web.onResume(); } catch (Throwable ignore) {}
        try { if (pageView != null) pageView.setVisibility(View.VISIBLE); } catch (Throwable ignore) {}
        try { if (pageView != null) pageView.onResume(); } catch (Throwable ignore) {}
    }

    @Override
    protected void onStop() {
        super.onStop();
        android.util.Log.i("CleanTV/LC", "onStop");
        // onStop 再兜一道（部分设备 Home 键路径 onPause 后 WebView 仍可能出声）
        jsApp("window.__appPause&&window.__appPause()");
        try { if (nplayer != null) nplayer.stop(); } catch (Throwable ignore) {}
        try { web.setVisibility(View.INVISIBLE); } catch (Throwable ignore) {}
        try { if (web != null) web.onPause(); } catch (Throwable ignore) {}
        try { web.pauseTimers(); } catch (Throwable ignore) {}
        try { if (pageView != null) pageView.onPause(); } catch (Throwable ignore) {}
        try { if (pageView != null) pageView.setVisibility(View.INVISIBLE); } catch (Throwable ignore) {}
    }

    /** 在主 WebView 上执行一小段 JS（页面级暂停/恢复钩子） */
    private void jsApp(String expr) {
        try {
            if (web != null) web.evaluateJavascript(expr, null);
        } catch (Throwable ignore) {}
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        // v2.10 TV 遥控器：菜单键 = 设置浮层（OK 键=频道列表由网页 keydown 处理）
        if (keyCode == KeyEvent.KEYCODE_MENU) {
            fireOverlay(true);   /* TV 菜单键 = 设置浮层 */
            return true;
        }
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            android.util.Log.i("CleanTV/LC", "BACK pageView=" + (pageView != null)
                    + " pageSeq=" + pageSeq);
            doBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    /**
     * BACK：先问网页 __tvBack（浮层开着 → 关浮层；播放中 → 确认退出卡）。
     * ★ 必须先于 pageView 检查：浮层开着时 pageView 只是临时 INVISIBLE、引用还在，
     *   先查 pageView 会把官方页关掉而不是关浮层（异步 JS，'1'=已消费）。
     * 网页没消费（'0'）才走 doBackReal（关官方页 / finish）。
     */
    private void doBack() {
        if (web != null) {
            try {
                web.evaluateJavascript(
                    "(function(){try{if(window.__tvBack)return __tvBack();}catch(e){}return '0';})()",
                    new android.webkit.ValueCallback<String>() {
                        @Override public void onReceiveValue(String value) {
                            if (value == null || value.indexOf("1") < 0) doBackReal();
                        }
                    });
            } catch (Throwable t) { doBackReal(); }
        } else {
            doBackReal();
        }
    }

    /** BACK 兜底：关官方页（置 __userExit，见 v2.9 注释）→ 都没有则退出 App */
    private void doBackReal() {
        if (pageView != null) {
            jsApp("window.__userExit=true");
            hidePage(pageSeq);
            return;
        }
        finish();
    }
}
