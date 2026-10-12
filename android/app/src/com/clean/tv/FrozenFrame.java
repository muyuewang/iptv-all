package com.clean.tv;

import android.content.Context;
import android.graphics.Bitmap;
import android.os.Looper;
import android.util.Log;
import android.view.TextureView;
import android.view.ViewGroup;
import android.widget.FrameLayout;
import android.widget.ImageView;

/**
 * 换台保帧：退出播放层时把最后一帧抓成 Bitmap 钉在屏幕上。
 *
 * 为什么要抓帧（而不是「保留 TextureView」这条看似更省事的路）
 * ------------------------------------------------------------------
 * ExoPlayer.release() 会把纹理内容一起清掉。实测（tools/_probe_freeze.py）：
 * BACK 之后手工把 WebView 转透明，整屏平均亮度 0.0（纯黑根视图）——
 * 说明那会儿帧已经没了，光留着 TextureView 是白留。所以必须自己抓一张。
 *
 * 层级关系（host 是 root FrameLayout）
 * ------------------------------------------------------------------
 *      surface（TextureView，最底）  <  本视图  <  WebView
 * 插在 WebView 下面、画面层上面 —— 网页在 frame-on 模式里把 body 置透明、
 * 藏掉频道列表，这一帧就透出来了。新流 onRenderedFirstFrame 上屏后才撤。
 *
 * 时序（★ 定时器不能放在 capture 里）
 * ------------------------------------------------------------------
 *   capture()       退出播放层时抓帧（必须在主线程）
 *   armAutoHide()   真正开始播新流时才起 12s 兜底 —— 万一 onRenderedFirstFrame
 *                   一直不来（流挂了），别让一张静止画面永远盖在视频上。
 *                   不能放在 capture：用户可能在列表里翻一会儿再开台，
 *                   12 秒后帧就被收掉了，正是我们要避免的黑屏。
 *   hide()          新流首帧上屏 / 彻底释放 / 退出 App 时撤掉并回收
 */
public final class FrozenFrame {
    private static final String TAG = "CleanTV/FrozenFrame";

    private final Context ctx;
    private final FrameLayout host;
    private ImageView view;
    private Bitmap bmp;
    private Runnable autoHide;

    public FrozenFrame(Context ctx, FrameLayout host) {
        this.ctx = ctx;
        this.host = host;
    }

    /**
     * 抓当前画面并钉到屏幕上。必须在主线程调用。
     *
     * @param tv 正在播放的 TextureView（可为 null —— 软解/首次播放时没有）
     * @return 是否抓到了
     */
    public boolean capture(TextureView tv) {
        Bitmap b = null;
        try {
            if (tv != null && tv.isAvailable()) b = tv.getBitmap();
        } catch (Throwable t) {
            Log.w(TAG, "getBitmap failed", t);
        }
        if (b == null) {
            /* ★ 抓不到 ≠ 要清掉。surface 已摘（重复调用）或纹理还没就绪时，
               这里必须原样保留已经钉好的那一帧 —— 否则会出现「先抓成功、
               再被第二次调用清掉」，实测 BACK 时就是这个把帧弄丢的。 */
            Log.w(TAG, "capture skipped (no surface / not available), keep=" + (view != null));
            return false;
        }
        hide();
        bmp = b;
        try {
            view = new ImageView(ctx);
            view.setScaleType(ImageView.ScaleType.FIT_CENTER);
            view.setImageBitmap(b);
            view.setLayoutParams(new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
            /* 插在 WebView 之前 —— 这样它盖住画面层、又被网页浮层盖住 */
            int idx = host.getChildCount();
            for (int i = 0; i < host.getChildCount(); i++) {
                if (host.getChildAt(i) instanceof android.webkit.WebView) { idx = i; break; }
            }
            host.addView(view, idx);
        } catch (Throwable t) {
            Log.w(TAG, "present failed", t);
            hide();
            return false;
        }
        int px = 0;
        try { px = b.getPixel(b.getWidth() / 2, b.getHeight() / 2); } catch (Throwable t) {}
        Log.i(TAG, "frozen frame captured " + b.getWidth() + "x" + b.getHeight()
                + " center=#" + Integer.toHexString(px) + " hostN=" + host.getChildCount()
                + " viewN=" + host.indexOfChild(view));
        return true;
    }

    /** 开始播新流时调用：起 12 秒兜底，防止首帧事件不来导致静止画面盖住视频。 */
    public void armAutoHide(final long ms) {
        if (view == null) return;
        if (Looper.myLooper() != Looper.getMainLooper()) {
            host.post(new Runnable() { @Override public void run() { armAutoHide(ms); } });
            return;
        }
        final ImageView v = view;
        autoHide = new Runnable() { @Override public void run() { hide(); } };
        try { v.postDelayed(autoHide, ms); } catch (Throwable t) {}
    }

    /** 撤掉保帧并回收 Bitmap。可在任意线程调用。 */
    public void hide() {
        final ImageView v = view;
        final Runnable t = autoHide;
        final Bitmap b = bmp;
        view = null;
        autoHide = null;
        bmp = null;
        Log.i(TAG, "frozen frame hide: hadView=" + (v != null));
        Runnable r = new Runnable() {
            @Override public void run() {
                if (v != null) {
                    try { if (t != null) v.removeCallbacks(t); } catch (Throwable x) {}
                    try { v.setImageBitmap(null); } catch (Throwable x) {}
                    try { if (v.getParent() != null) host.removeView(v); } catch (Throwable x) {}
                }
                if (b != null) {
                    try { if (!b.isRecycled()) b.recycle(); } catch (Throwable x) {}
                }
            }
        };
        if (Looper.myLooper() == Looper.getMainLooper()) r.run();
        else host.post(r);
    }

    public boolean isShowing() { return view != null; }
}
