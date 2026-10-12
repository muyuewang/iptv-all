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

    /**
     * 开始播放 + 附加请求头（v2.9）。
     *
     * 官网源「提取到的流地址」几乎都校验 Referer / Cookie —— 不带就是 403。
     * headersJson 是 {"Referer":..,"Cookie":..} 形式的 JSON（来自嗅探时的页面上下文）。
     *
     * ★ 这条路径**必须全量重建播放器**：请求头是在构建 DataSource 时设的，
     *   switchTo 复用播放器实例改不了。所以别把它接到 switchTo 上。
     */
    void play(String url, String headersJson);

    /** 切线路：换源不销毁播放器，避免黑屏等待 */
    void switchTo(String url);

    /** 切线路 + 附加请求头（请求头变了就会重建数据源，见实现） */
    void switchTo(String url, String headersJson);

    void pause();
    void resume();
    void stop();

    /**
     * 冻结：只释放播放器实例，**保留画面层与最后一帧**。
     *
     * 为什么需要：退出播放层时若把画面层一起摘掉（release），下次开台就是
     * 纯黑等首帧（实测 BACK → 点台有 3.36s 全黑）。冻结之后这一帧还在，
     * 网页把 WebView 转回透明就能立刻看到「上一个台」，新流首帧到达时
     * 自然被替换 —— 这才是真正的无缝换台。
     *
     * @return true 表示确实有一块画面层被保留下来（没有则返回 false，
     *         网页据此决定要不要提前转透明，否则会露出 black 根视图）
     */
    boolean freeze();

    /** 播放统计 JSON（卡顿判定用）。必须线程安全（JS 桥线程会调）。 */
    String stats();

    /** 是否命中硬件解码 */
    boolean isHardwareDecode();

    boolean isPlaying();

    /** 彻底释放（退出播放层） */
    void release();
}
