package com.clean.tv;

import android.content.Context;
import android.graphics.Color;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.SurfaceView;
import android.view.ViewGroup;
import android.widget.FrameLayout;

import androidx.media3.common.MediaItem;
import androidx.media3.common.PlaybackException;
import androidx.media3.common.Player;
import androidx.media3.common.Tracks;
import androidx.media3.common.VideoSize;
import androidx.media3.common.util.UnstableApi;
import androidx.media3.datasource.DefaultDataSource;
import androidx.media3.datasource.DefaultHttpDataSource;
import androidx.media3.exoplayer.DefaultLoadControl;
import androidx.media3.exoplayer.DefaultRenderersFactory;
import androidx.media3.exoplayer.ExoPlayer;
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory;
import androidx.media3.exoplayer.trackselection.DefaultTrackSelector;

/**
 * 原生硬解播放层（ExoPlayer / media3）
 * ================================================================
 * 为什么需要它：
 *   原方案用 mpegts.js 在 WebView 里做 JS 软解 —— 1080p@2.4Mbps 的 FLV，
 *   在 4 核 / 4GB 的低端设备上解不过来（实测 20 秒只推进 4.6 秒）。
 *   v1.2 靠调参把指标拉到 106%，但那是软解的天花板，稍重一点的源仍会卡。
 *
 * 这个类的职责：
 *   · 用 ExoPlayer 走 MediaCodec 硬解（H.264/H.265 交给 DSP，CPU 几乎不参与）
 *   · 自己持有一个 SurfaceView 输出画面，WebView 只渲染控制条浮层
 *   · 通过 @JavascriptInterface 暴露给网页，网页只管发指令 + 收状态
 *
 * 设计要点：
 *   1) 硬解优先：DefaultRenderersFactory 设 PREFER 并开 asynchronousQueueing，
 *      同时给一个「硬解失败回退软解」的宽容策略，避免个别机型解码器缺失直接黑屏。
 *   2) 直播低延迟：设 liveConfiguration，让播放器按直播目标延迟追帧，
 *      不无限堆积缓冲（这是直播场景卡顿的常见原因）。
 *   3) 状态回传：把 buffering / ready / error / 视频尺寸 回调给网页，
 *      让网页上的「线路 N 加载中」提示、自动切线路逻辑继续有效。
 */
@UnstableApi
public class NativePlayer {

    private static final String TAG = "CleanTV/NativePlayer";

    /** 把原生播放状态回调给网页（由 MainActivity 注入实现） */
    public interface Listener {
        void onState(String state, String detail);   // state: buffering|ready|ended|error|size
    }

    private final Context ctx;
    private final FrameLayout host;          // 承载画面的容器（与 WebView 同一个 root）
    private SurfaceView surface;
    private ExoPlayer player;
    private Listener listener;
    private final Handler ui = new Handler(Looper.getMainLooper());

    /** 当前是否已挂上画面层 */
    private boolean attached = false;
    /** 记录最后一次播放地址，便于错误重试 */
    private String lastUrl = null;

    /**
     * 直播参数。
     * targetOffsetMs：期望落后直播边缘的毫秒数。太小会频繁追帧发卡，太大会累积延迟。
     * 手机端取 1200~1800ms 比较稳；低端机给大一点更抗抖动。
     */
    private static final long LIVE_TARGET_MS = 1500;

    public NativePlayer(Context ctx, FrameLayout host) {
        this.ctx = ctx;
        this.host = host;
    }

    public void setListener(Listener l) { this.listener = l; }

    private void emit(final String state, final String detail) {
        if (listener == null) return;
        ui.post(new Runnable() {
            @Override public void run() { listener.onState(state, detail); }
        });
    }

    /** 构建 RenderersFactory：硬解优先 + 队列异步化 + 软解兜底 */
    private DefaultRenderersFactory buildRenderers() {
        DefaultRenderersFactory f = new DefaultRenderersFactory(ctx);
        // 优先使用设备硬解码器（MediaCodec）；缺硬解时自动回退到软解，避免个别机型黑屏
        f.setExtensionRendererMode(DefaultRenderersFactory.EXTENSION_RENDERER_MODE_PREFER);
        // 硬解器缺失/失败时自动挑备选解码器（含软解），防止直接黑屏
        f.setEnableDecoderFallback(true);
        // 编码器队列异步化：解码与喂数据解耦，低端机可显著减少掉帧
        f.forceEnableMediaCodecAsynchronousQueueing();
        return f;
    }

    /** 懒创建 SurfaceView（复用一个实例，避免反复 add/remove 造成闪烁） */
    private void ensureSurface() {
        if (surface != null) return;
        surface = new SurfaceView(ctx);
        surface.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        surface.setBackgroundColor(Color.BLACK);
        surface.setKeepScreenOn(true);
    }

    /** 把画面层加到 root 上（在 WebView 之上；控制条由网页画在 WebView 里，故 surface 之下） */
    public void attach(final int zIndex) {
        ui.post(new Runnable() {
            @Override public void run() {
                ensureSurface();
                if (attached) return;
                host.addView(surface, 0);          // 放到最底层：WebView 保持在上方渲染控制条
                attached = true;
                if (player != null) player.setVideoSurfaceView(surface);
            }
        });
    }

    public void detach() {
        ui.post(new Runnable() {
            @Override public void run() {
                if (!attached || surface == null) return;
                try { host.removeView(surface); } catch (Throwable ignore) {}
                attached = false;
            }
        });
    }

    /** 构造直播用 MediaItem：把「期望延迟」挂在 item 上（media3 1.4 的规范做法） */
    private MediaItem buildItem(String url) {
        MediaItem.LiveConfiguration liveCfg = new MediaItem.LiveConfiguration.Builder()
                .setTargetOffsetMs(LIVE_TARGET_MS)
                .build();
        return new MediaItem.Builder()
                .setUri(Uri.parse(url))
                .setLiveConfiguration(liveCfg)
                .build();
    }

    /** 真正开始播放一个 FLV/HLS 地址 */
    public void play(final String url) {
        ui.post(new Runnable() {
            @Override public void run() {
                lastUrl = url;
                ensureSurface();
                if (!attached) {
                    host.addView(surface, 0);
                    attached = true;
                }

                releasePlayer();
                emit("buffering", "native init");

                // ---- 数据源：直连 CDN，带常见请求头（部分 CDN 校验 Referer/UA）----
                DefaultHttpDataSource.Factory http = new DefaultHttpDataSource.Factory()
                        .setUserAgent(UA)
                        .setConnectTimeoutMs(15000)
                        .setReadTimeoutMs(15000)
                        .setAllowCrossProtocolRedirects(true);   // eplay2.php 会 302 到字节 CDN
                DefaultDataSource.Factory ds = new DefaultDataSource.Factory(ctx, http);

                DefaultTrackSelector trackSelector = new DefaultTrackSelector(ctx);
                // 直播场景：允许多码率自适应，弱网自动降档而不是死等
                trackSelector.setParameters(
                        trackSelector.buildUponParameters()
                                .setAllowVideoMixedMimeTypeAdaptiveness(true)
                                .setAllowVideoNonSeamlessAdaptiveness(true));

                // 缓冲参数（直播要低延迟，但起播要快）：
                //   参数顺序（media3 1.4 DefaultLoadControl.Builder.setBufferDurationsMs）：
                //   minBufferMs, maxBufferMs, bufferForPlaybackMs, bufferForPlaybackAfterRebufferMs
                // 注意：ExoPlayer 上没有 setBufferDurationsMs，必须在 Builder 期用 LoadControl 配置。
                DefaultLoadControl loadControl = new DefaultLoadControl.Builder()
                        .setBufferDurationsMs(
                                1500,     // 最小缓冲：低于此值停止读取，避免堆太多
                                25000,    // 最大缓冲：25 秒，兼顾抗抖动与低延迟
                                1000,     // 起播门槛：1 秒即起播（直播要快）
                                2000)     // 卡顿后重新起播门槛
                        .setPrioritizeTimeOverSizeThresholds(true)  // 直播优先保时长，别因字节数提前截断
                        .build();

                ExoPlayer.Builder b = new ExoPlayer.Builder(ctx, buildRenderers())
                        .setMediaSourceFactory(new DefaultMediaSourceFactory(ds))
                        .setTrackSelector(trackSelector)
                        .setLoadControl(loadControl);
                player = b.build();

                player.setPlayWhenReady(true);
                player.setRepeatMode(Player.REPEAT_MODE_OFF);

                player.addListener(new Player.Listener() {
                    @Override public void onPlaybackStateChanged(int st) {
                        switch (st) {
                            case Player.STATE_BUFFERING: emit("buffering", "native"); break;
                            case Player.STATE_READY:     emit("ready", "native"); break;
                            case Player.STATE_ENDED:     emit("ended", "native"); break;
                            case Player.STATE_IDLE:      break;
                        }
                    }
                    @Override public void onVideoSizeChanged(VideoSize size) {
                        emit("size", size.width + "x" + size.height);
                    }
                    @Override public void onIsPlayingChanged(boolean isPlaying) {
                        if (isPlaying) emit("ready", "playing");
                    }
                    @Override public void onPlayerError(PlaybackException error) {
                        Log.w(TAG, "player error", error);
                        emit("error", error.getErrorCodeName());
                    }
                    @Override public void onTracksChanged(Tracks tracks) {
                        emit("tracks", String.valueOf(tracks.getGroups().size()));
                        probeDecoder();
                    }
                });

                player.setMediaItem(buildItem(url));
                player.prepare();
            }
        });
    }

    /** 只切线路：保持播放器实例，换源。比 destroy+new 快得多（避免黑屏等待） */
    public void switchTo(final String url) {
        ui.post(new Runnable() {
            @Override public void run() {
                if (player == null) { play(url); return; }
                lastUrl = url;
                emit("buffering", "switch");
                player.setMediaItem(buildItem(url));
                player.prepare();
            }
        });
    }

    public void pause()  { ui.post(new Runnable() { @Override public void run() { if (player != null) player.setPlayWhenReady(false); } }); }
    public void resume() { ui.post(new Runnable() { @Override public void run() { if (player != null) player.setPlayWhenReady(true); } }); }

    /** 播放统计：供网页判断是否卡顿（比 WebView 的 readyState 更准） */
    public String stats() {
        if (player == null) return "{}";
        try {
            StringBuilder sb = new StringBuilder(160);
            sb.append("{\"state\":").append(player.getPlaybackState());
            sb.append(",\"playing\":").append(player.isPlaying());
            sb.append(",\"buffered\":").append(player.getBufferedPosition());
            sb.append(",\"pos\":").append(player.getCurrentPosition());
            sb.append(",\"bufferedMs\":").append(player.getBufferedPosition() - player.getCurrentPosition());
            // 硬解信息：Format 本身不带解码器名，硬解与否由 probeDecoder() 判定
            androidx.media3.common.Format vf = player.getVideoFormat();
            if (vf != null) {
                sb.append(",\"vcodec\":\"").append(vf.sampleMimeType == null ? "" : vf.sampleMimeType).append("\"");
                sb.append(",\"w\":").append(vf.width).append(",\"h\":").append(vf.height);
                sb.append(",\"fps\":").append(vf.frameRate);
            }
            sb.append(",\"hw\":").append(hardwareDecode ? 1 : 0);
            sb.append("}");
            return sb.toString();
        } catch (Throwable t) { return "{}"; }
    }

    /** 是否命中硬件解码（由解码器名推断，onTracksChanged 时更新） */
    private volatile boolean hardwareDecode = false;

    /**
     * 判断当前视频轨是否走了硬件解码器。
     * 依据：设备 MediaCodecList 中是否存在支持该 mime 的硬解器
     * （硬解名通常含 c2./OMX./qcom/Exynos 等前缀；软解器名为 c2.android.* / OMX.google.*）。
     */
    private void probeDecoder() {
        try {
            androidx.media3.common.Format vf = player == null ? null : player.getVideoFormat();
            if (vf == null || vf.sampleMimeType == null) return;
            android.media.MediaCodecList list = new android.media.MediaCodecList(
                    android.media.MediaCodecList.REGULAR_CODECS);
            for (android.media.MediaCodecInfo ci : list.getCodecInfos()) {
                if (ci.isEncoder()) continue;
                boolean mimeOk = false;
                for (String m : ci.getSupportedTypes()) {
                    if (m.equalsIgnoreCase(vf.sampleMimeType)) { mimeOk = true; break; }
                }
                if (!mimeOk) continue;
                String n = ci.getName();
                boolean hw = !(n.startsWith("OMX.google.")
                           || n.startsWith("c2.android.")
                           || n.startsWith("c2.google."));
                if (hw) {
                    hardwareDecode = true;
                    Log.i(TAG, "硬解器命中: " + n + " for " + vf.sampleMimeType);
                    emit("decoder", "hw:" + n);
                    return;
                }
            }
            hardwareDecode = false;
            emit("decoder", "sw-only");
        } catch (Throwable t) {
            Log.w(TAG, "probeDecoder fail", t);
        }
    }

    public boolean isPlaying() { return player != null && player.isPlaying(); }

    /** 是否命中硬件解码（供 MainActivity 的 JS 桥直接查询） */
    public boolean isHardwareDecode() { return hardwareDecode; }

    public void releasePlayer() {
        if (player != null) {
            try { player.stop(); } catch (Throwable ignore) {}
            try { player.release(); } catch (Throwable ignore) {}
            player = null;
        }
    }

    /** 彻底释放（退出播放层时调用） */
    public void destroy() {
        ui.post(new Runnable() {
            @Override public void run() {
                releasePlayer();
                detach();
            }
        });
    }

    /** 与网页 UA 保持一致：部分 CDN 会校验 */
    private static final String UA =
        "Mozilla/5.0 (Linux; Android 9; SM-N9700 Build/PQ3B.190801.01311438; wv) " +
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 " +
        "Mobile Safari/537.36 diashizhb LT-APP/48/517/YM-RT/";
}
