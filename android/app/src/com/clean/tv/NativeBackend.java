package com.clean.tv;

import android.widget.FrameLayout;

/**
 * 原生硬解播放后端接口 —— 屏蔽 media3(5.0+) 与 老ExoPlayer2(4.4) 的差异。
 *
 * 两个实现：
 *   Media3PlayerBackend  → androidx.media3.* ，Android 5.0+（minSdk 21）
 *   Exo2PlayerBackend    → com.google.android.exoplayer2.* ，Android 4.4+（API 16）
 *
 * MainActivity 按 Build.VERSION.SDK_INT 选一个；NativePlayer 只面对本接口。
 */
public interface NativeBackend {

    /** 状态回调：state ∈ buffering|ready|ended|error|size|decoder */
    interface Listener {
        void onState(String state, String detail);
    }

    void setListener(Listener l);

    /** 开始播放（内部保证纹理 view 已挂好） */
    void play(String url);

    /** 切线路：换源不销毁播放器，避免黑屏等待 */
    void switchTo(String url);

    void pause();
    void resume();
    void stop();

    /** 播放统计 JSON（卡顿判定用）。必须线程安全（JS 桥线程会调）。 */
    String stats();

    /** 是否命中硬件解码 */
    boolean isHardwareDecode();

    boolean isPlaying();

    /** 彻底释放（退出播放层） */
    void release();
}
