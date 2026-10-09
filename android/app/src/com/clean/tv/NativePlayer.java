package com.clean.tv;

import android.content.Context;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.widget.FrameLayout;

/**
 * 原生硬解播放门面 —— 对外统一接口，内部按系统版本选择后端：
 *   · Android 5.0+（API 21+）：Media3PlayerBackend（androidx.media3 1.4.x）
 *   · Android 4.4（API 16-20）：Exo2PlayerBackend（老 ExoPlayer 2.19.1）
 *
 * ★ 关键：media3 类只允许在 API 21+ 上被加载。Android 4.4 若触碰任何
 *   androidx.media3.* 会 ClassNotFoundException。所以这里用反射按版本加载
 *   media3 后端，绝不静态 import；老 ExoPlayer 后端则可静态引用（它本身支持 API 16）。
 */
public class NativePlayer {

    private static final String TAG = "CleanTV/NativePlayer";

    public interface Listener {
        void onState(String state, String detail);
    }

    private NativeBackend backend;      // 实际干活的后端
    private Listener listener;

    public NativePlayer(Context ctx, FrameLayout host) {
        backend = createBackend(ctx, host);
    }

    /** 按 SDK_INT 挑后端。media3 后端走反射，避免 4.4 下类加载崩溃。 */
    private static NativeBackend createBackend(Context ctx, FrameLayout host) {
        if (Build.VERSION.SDK_INT >= 21) {
            try {
                // 反射加载 Media3PlayerBackend（本类不静态引用 media3）
                Class<?> c = Class.forName("com.clean.tv.Media3PlayerBackend");
                NativeBackend b = (NativeBackend) c.getConstructor(Context.class, FrameLayout.class)
                        .newInstance(ctx, host);
                Log.i(TAG, "硬解后端：media3（Android 5.0+）");
                return b;
            } catch (Throwable t) {
                Log.w(TAG, "media3 后端加载失败，回退老 ExoPlayer2", t);
            }
        }
        // API 16-20（Android 4.4 等老设备）或 media3 加载失败：用老 ExoPlayer2
        try {
            Log.i(TAG, "硬解后端：老 ExoPlayer2（Android 4.4 兼容）");
            return new Exo2PlayerBackend(ctx, host);
        } catch (Throwable t) {
            Log.e(TAG, "老 ExoPlayer2 后端也加载失败，无原生硬解可用", t);
            return new NoopBackend();
        }
    }

    public void setListener(Listener l) {
        this.listener = l;
        if (backend != null) {
            backend.setListener(new NativeBackend.Listener() {
                @Override public void onState(String state, String detail) {
                    if (listener != null) listener.onState(state, detail);
                }
            });
        }
    }

    /** 是否有可用的原生硬解后端 */
    public boolean available() {
        return backend != null && !(backend instanceof NoopBackend);
    }

    public void play(String url)   { if (backend != null) backend.play(url); }
    public void switchTo(String url){ if (backend != null) backend.switchTo(url); }
    public void pause()            { if (backend != null) backend.pause(); }
    public void resume()           { if (backend != null) backend.resume(); }
    public void stop()             { if (backend != null) backend.stop(); }

    public String stats() {
        if (backend == null) return "{}";
        String s = backend.stats();
        return s == null ? "{}" : s;
    }

    public boolean isHardwareDecode() { return backend != null && backend.isHardwareDecode(); }
    public boolean isPlaying()        { return backend != null && backend.isPlaying(); }

    public void destroy() {
        if (backend != null) {
            backend.release();
        }
    }

    /** 无后端可用时的空实现（理论上不会发生，双后端都加载失败时兜底） */
    private static class NoopBackend implements NativeBackend {
        @Override public void setListener(Listener l) {}
        @Override public void play(String url) {}
        @Override public void switchTo(String url) {}
        @Override public void pause() {}
        @Override public void resume() {}
        @Override public void stop() {}
        @Override public String stats() { return "{}"; }
        @Override public boolean isHardwareDecode() { return false; }
        @Override public boolean isPlaying() { return false; }
        @Override public void release() {}
    }
}
