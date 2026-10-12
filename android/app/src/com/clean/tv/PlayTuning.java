package com.clean.tv;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * 播放缓冲档位（v2.5）。
 *
 * 由网页的「秒开 / 稳定」按钮经 {@code NativeBridge.setTuneProfile} 切换，
 * 写进 SharedPreferences 持久化；两个后端（media3 / ExoPlayer2）在构建播放器时读它。
 *
 * 关键认知：{@code bufferForPlaybackMs} 是「必须缓冲满多久才出第一帧」——
 * 这是起播快慢最直接的旋钮。过去两处后端都硬编码 1500ms，意味着任何频道
 * 都要先等 1.5 秒才开始出画面。
 *   秒开 fast（默认）：700ms，WiFi / 宽带下开台体感明显更快
 *   稳定 stable     ：1500ms + 更大缓冲，弱网 / 移动网络下更不容易卡
 *
 * 注意：本类只碰 volatile 与 SharedPreferences，任何线程调用都安全
 * （网页桥方法跑在 WebView 的 JavaBridge 线程上，不能碰播放器对象）。
 */
public final class PlayTuning {

    public static final String FAST = "fast";
    public static final String STABLE = "stable";

    private static final String PREF = "tv_tune";
    private static final String KEY = "profile";

    private static volatile String profile = FAST;

    private PlayTuning() {}

    /** 启动时读一次持久化的档位 */
    public static void init(Context ctx) {
        try {
            SharedPreferences sp = ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE);
            profile = STABLE.equals(sp.getString(KEY, FAST)) ? STABLE : FAST;
        } catch (Throwable ignore) {
            profile = FAST;
        }
    }

    public static void setProfile(Context ctx, String p) {
        profile = STABLE.equals(p) ? STABLE : FAST;
        try {
            ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)
               .edit().putString(KEY, profile).apply();
        } catch (Throwable ignore) {}
    }

    public static String getProfile() { return profile; }
    public static boolean isStable() { return STABLE.equals(profile); }

    /** 直播不需要大底仓：v2.6 起从 6s 降到 3s（对齐 TVBoxOS 的 IJK 直播档
     *  max_cached_duration / min-frames 思路）—— 底仓越浅，起播后落到「实时」
     *  越快，内存与追帧压力也更小。max 仍留 30s，给弱网一点回旋。
     *
     *  ★★ 隐式不变量（踩过的坑）：media3 的 {@code DefaultLoadControl.Builder
     *  .setBufferDurationsMs()} 要求
     *      minBufferMs >= bufferForPlaybackAfterRebufferMs
     *  否则**直接抛 IllegalArgumentException 崩在主线程**（实测：minBuffer 2500
     *  配「稳定」档的 3000 → 一开台就 FATAL EXCEPTION）。这个约束在编译期
     *  完全看不出来，只有真机跑到才会炸。所以这里用 {@code Math.max} 把下限
     *  写死，以后调档位也不会再踩。
     *
     *  另注意：真正决定「多久出第一帧」的是 {@link #bufferForPlaybackMs()}，
     *  这三个是稳态缓冲，别混为一谈。 */
    public static int minBufferMs() { return Math.max(3000, bufferForPlaybackAfterRebufferMs()); }
    public static int maxBufferMs() { return 30000; }

    /** ★ 起播门槛：「秒开」与「稳定」差的就是这一个值 */
    public static int bufferForPlaybackMs() { return isStable() ? 1500 : 700; }

    /** 卡顿后的恢复门槛：比首播略高，避免刚好一点又开始卡 */
    public static int bufferForPlaybackAfterRebufferMs() { return isStable() ? 3000 : 1600; }

    /** 直播目标延迟（media3 LiveConfiguration.targetOffsetMs） */
    public static long liveTargetMs() { return isStable() ? 2000 : 1500; }
}
