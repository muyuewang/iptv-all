package com.clean.tv;

import android.app.Activity;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
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

        /** 是否有可用的原生播放器（ExoPlayer 已编入且未异常） */
        @JavascriptInterface
        public boolean hasNativePlay() {
            return nplayer != null;
        }

        /** 开始播放（url 为解密后的 FLV/HLS 直链） */
        @JavascriptInterface
        public void nativePlay(final String url) {
            if (nplayer == null) return;
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    nplayer.attach(0);
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

        @JavascriptInterface
        public void nativePause()  { if (nplayer != null) nplayer.pause(); }
        @JavascriptInterface
        public void nativeResume() { if (nplayer != null) nplayer.resume(); }

        /** 退出播放层：释放解码器与画面层，把 WebView 重新露出来 */
        @JavascriptInterface
        public void nativeStop() {
            if (nplayer == null) return;
            nplayer.destroy();
            runOnUiThread(new Runnable() {
                @Override public void run() { web.bringToFront(); }
            });
        }

        /** 取播放统计（JSON 字符串），网页用来做卡顿判定 */
        @JavascriptInterface
        public String nativeStats() {
            return nplayer == null ? "{}" : nplayer.stats();
        }

        /** 是否命中硬件解码 */
        @JavascriptInterface
        public boolean nativeHwDecode() {
            return nplayer != null && nplayer.isHardwareDecode();
        }
    }

    // 关键：服务器靠该 UA 才返回新版播放器页
    private static final String APP_UA =
        "Mozilla/5.0 (Linux; Android 9; SM-N9700 Build/PQ3B.190801.01311438; wv) " +
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 " +
        "Mobile Safari/537.36 diashizhb LT-APP/48/517/YM-RT/";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        try { WebView.setWebContentsDebuggingEnabled(true); } catch (Throwable t) {}

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
        // ★ 解码/渲染优化：让 WebView 用 GPU 合成 + 硬件解码视频
        try { web.setLayerType(View.LAYER_TYPE_HARDWARE, null); } catch (Throwable t) {}
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
        });

        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        boolean isTV = (getResources().getConfiguration().uiMode & Configuration.UI_MODE_TYPE_MASK)
                            == Configuration.UI_MODE_TYPE_TELEVISION
                    || getPackageManager().hasSystemFeature(PackageManager.FEATURE_LEANBACK)
                    || "tv".equalsIgnoreCase(android.os.Build.DEVICE);
        // TV / 平板默认就是横屏大屏，保持传感器自适应；手机则默认竖屏（频道列表），
        // 点全屏时才由 JS 桥切成横屏。
        applyOrientation(isTV);
        web.loadUrl("file:///android_asset/player.html" + (isTV ? "?tv=1" : ""));
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

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            // 交给网页处理：优先关播放层 → 其次退全屏 → 都没有则结束
            web.evaluateJavascript(
                "(function(){try{" +
                "var p=document.getElementById('player');" +
                "if(p&&p.classList.contains('show')){" +
                "  if(p.classList.contains('fs')&&window.setFsMode){window.setFsMode(false);return '2';}" +
                "  document.getElementById('btnClose').click();return '1';" +
                "}" +
                "}catch(e){}return '0';})()",
                new android.webkit.ValueCallback<String>() {
                    @Override public void onReceiveValue(String value) {
                        if (value == null || value.indexOf("1") < 0) {
                            if (value != null && value.indexOf("2") >= 0) return;  // 只是退了全屏
                            finish();
                        }
                    }
                });
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }
}
