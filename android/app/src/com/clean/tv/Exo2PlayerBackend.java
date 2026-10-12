package com.clean.tv;

import android.content.Context;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.TextureView;
import android.view.ViewGroup;
import android.widget.FrameLayout;

import com.google.android.exoplayer2.DefaultLoadControl;
import com.google.android.exoplayer2.DefaultRenderersFactory;
import com.google.android.exoplayer2.ExoPlayer;
import com.google.android.exoplayer2.MediaItem;
import com.google.android.exoplayer2.PlaybackException;
import com.google.android.exoplayer2.Player;
import com.google.android.exoplayer2.source.DefaultMediaSourceFactory;
import com.google.android.exoplayer2.trackselection.DefaultTrackSelector;
import com.google.android.exoplayer2.upstream.DefaultDataSource;
import com.google.android.exoplayer2.upstream.DefaultHttpDataSource;
import com.google.android.exoplayer2.Format;

/**
 * 老版 ExoPlayer 2.19.1 硬解后端 —— 兼容 Android 4.4（API 16+）。
 *
 * 为什么需要它：media3 1.4.x 要求 minSdk 21，Android 4.4 的 TV 盒子用不了。
 * 老版 ExoPlayer 2.19.1 支持到 API 16，同样走 MediaCodec 硬解，是 4.4 设备
 * 唯一能用的原生硬解库。API 与 media3 几乎一致，仅包名不同（com.google.android.exoplayer2）。
 */
public class Exo2PlayerBackend implements NativeBackend {

    private static final String TAG = "CleanTV/Exo2";

    private final Context ctx;
    private final FrameLayout host;
    private TextureView surface;
    private ExoPlayer player;
    private Listener listener;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private boolean attached = false;
    private String lastUrl = null;
    private volatile boolean hardwareDecode = false;
    /** 实时带宽估计（bps）—— 底栏角标显示「网速」用（v2.8）。
        ★ ExoPlayer 接口上**没有** getBandwidthMeter()，只能挂 AnalyticsListener
        的 onBandwidthEstimate 回调把值捞出来缓存。 */
    private volatile long bwEstimate = 0;
    private final com.google.android.exoplayer2.analytics.AnalyticsListener bwListener =
            new com.google.android.exoplayer2.analytics.AnalyticsListener() {
                @Override public void onBandwidthEstimate(
                        com.google.android.exoplayer2.analytics.AnalyticsListener.EventTime et,
                        int totalLoadTimeMs, long totalBytesLoaded, long bitrateEstimate) {
                    bwEstimate = bitrateEstimate;
                }
            };
    /** 退出播放层时钉在屏幕上的最后一帧（换台不黑屏，见 FrozenFrame 注释） */
    private final FrozenFrame frozen;

    /** 直播目标延迟见 {@link PlayTuning#liveTargetMs()}（「秒开 / 稳定」档位会影响它） */
    private static final String UA = MainActivity.APP_UA;

    public Exo2PlayerBackend(Context ctx, FrameLayout host) {
        this.ctx = ctx;
        this.host = host;
        this.frozen = new FrozenFrame(ctx, host);
    }

    @Override public void setListener(Listener l) { this.listener = l; }

    private void emit(final String state, final String detail) {
        if (listener == null) return;
        ui.post(new Runnable() {
            @Override public void run() { listener.onState(state, detail); }
        });
    }

    private DefaultRenderersFactory buildRenderers() {
        DefaultRenderersFactory f = new DefaultRenderersFactory(ctx);
        f.setExtensionRendererMode(DefaultRenderersFactory.EXTENSION_RENDERER_MODE_PREFER);
        // 硬解失败自动回退软解，避免个别老 TV 解码器缺失直接黑屏
        f.setEnableDecoderFallback(true);
        return f;
    }

    private void ensureSurface() {
        if (surface != null) return;
        surface = new TextureView(ctx);
        surface.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        surface.setOpaque(true);
        surface.setKeepScreenOn(true);
    }

    /** HLS 分片/播放列表错误的原地自救次数（见 onPlayerError）；每条流起播时清零。 */
    private static final int HLS_SELF_RETRY_MAX = 2;
    private int hlsRetry = 0;

    /** 同 {@code Media3PlayerBackend.isHlsStreamError}：m3u8 + 取数据类错误码。 */
    private boolean isHlsStreamError(PlaybackException e) {
        if (lastUrl == null || lastUrl.indexOf(".m3u8") < 0) return false;
        switch (e.errorCode) {
            case PlaybackException.ERROR_CODE_IO_BAD_HTTP_STATUS:
            case PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_FAILED:
            case PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_TIMEOUT:
            case PlaybackException.ERROR_CODE_IO_UNSPECIFIED:
                return true;
            default:
                return false;
        }
    }

    private MediaItem buildItem(String url) {
        MediaItem.LiveConfiguration liveCfg = new MediaItem.LiveConfiguration.Builder()
                .setTargetOffsetMs(PlayTuning.liveTargetMs()).build();
        return new MediaItem.Builder().setUri(Uri.parse(url)).setLiveConfiguration(liveCfg).build();
    }

    @Override public void play(final String url) { play(url, null); }

    @Override public void play(final String url, final String headersJson) {
        /* 提取到的流要带 Referer/Cookie（v2.9） */
        final java.util.Map<String, String> extra = SniffProbe.parseHeaders(headersJson);
        ui.post(new Runnable() {
            @Override public void run() {
                lastUrl = url;
                hlsRetry = 0;    /* 新的一条流：HLS 自救次数重新计（见 onPlayerError） */
                /* ★ 只释放旧播放器实例，不动画面层 —— 切线路/换台时画面不能黑一下。 */
                releasePlayerOnly();
                ensureSurface();
                if (!attached) { host.addView(surface, 0); attached = true; }
                /* 同上：保帧画面的 12 秒兜底，首帧一到就撤 */
                frozen.armAutoHide(12000);

                DefaultHttpDataSource.Factory http = new DefaultHttpDataSource.Factory()
                        .setUserAgent(UA).setConnectTimeoutMs(15000).setReadTimeoutMs(15000)
                        .setAllowCrossProtocolRedirects(true);
                /* ★ 官网源提取到的流：原样带上页面的 Referer / Cookie / Origin */
                if (extra != null) {
                    try { http.setDefaultRequestProperties(extra); } catch (Throwable ignore) {}
                }
                DefaultDataSource.Factory ds = new DefaultDataSource.Factory(ctx, http);

                DefaultTrackSelector ts = new DefaultTrackSelector(ctx);
                ts.setParameters(ts.buildUponParameters()
                        .setAllowVideoMixedMimeTypeAdaptiveness(true)
                        .setAllowVideoNonSeamlessAdaptiveness(true));

                DefaultLoadControl lc = new DefaultLoadControl.Builder()
                        .setBufferDurationsMs(PlayTuning.minBufferMs(), PlayTuning.maxBufferMs(),
                                PlayTuning.bufferForPlaybackMs(),
                                PlayTuning.bufferForPlaybackAfterRebufferMs())
                        .setPrioritizeTimeOverSizeThresholds(true).build();

                ExoPlayer.Builder b = new ExoPlayer.Builder(ctx, buildRenderers())
                        .setMediaSourceFactory(new DefaultMediaSourceFactory(ds))
                        .setTrackSelector(ts).setLoadControl(lc);
                player = b.build();
                ensureSurface();
                player.setVideoTextureView(surface);
                player.setPlayWhenReady(true);
                player.setRepeatMode(Player.REPEAT_MODE_OFF);
                player.addAnalyticsListener(bwListener);   /* 网速角标的数据源（v2.8） */

                player.addListener(new Player.Listener() {
                    @Override public void onPlaybackStateChanged(int st) {
                        switch (st) {
                            case Player.STATE_BUFFERING: emit("buffering", "native"); break;
                            case Player.STATE_READY:     emit("ready", "native"); break;
                            case Player.STATE_ENDED:     emit("ended", "native"); break;
                        }
                    }
                    @Override public void onVideoSizeChanged(com.google.android.exoplayer2.video.VideoSize s) { emit("size", s.width + "x" + s.height); }
                    @Override public void onRenderedFirstFrame() {
                        /* ★ 新流第一帧真的上屏了才撤保帧（同 Media3PlayerBackend） */
                        frozen.hide();
                        /* ★ 并报给网页当「出首帧」权威信号（起播看门狗靠它撤） */
                        emit("firstframe", "native");
                    }
                    @Override public void onIsPlayingChanged(boolean p) { if (p) emit("ready", "playing"); }
                    @Override public void onPlayerError(PlaybackException e) {
                        Log.w(TAG, "player error", e);
                        /* ★ HLS 分片级错误的「同流自救」（v2.7，同 Media3PlayerBackend）：
                           跳到直播边缘重播同一条流绕开坏片 —— 直播的坏片是暂时的，
                           后面的分片都是好的。自救失败才交给 JS 换线路。
                           实测证据见 tools/_probe_hls.py 与 Media3PlayerBackend 的注释。 */
                        if (isHlsStreamError(e) && hlsRetry < HLS_SELF_RETRY_MAX) {
                            hlsRetry++;
                            Log.i(TAG, "HLS stream error -> seek to live edge, same-stream retry "
                                    + hlsRetry + "/" + HLS_SELF_RETRY_MAX);
                            emit("hlsskip", String.valueOf(hlsRetry));
                            ui.post(new Runnable() {
                                @Override public void run() {
                                    if (player == null) return;
                                    player.seekToDefaultPosition();
                                    player.prepare();
                                }
                            });
                            return;
                        }
                        emit("error", e.getErrorCodeName());
                    }
                    @Override public void onTracksChanged(com.google.android.exoplayer2.Tracks t) {
                        emit("tracks", String.valueOf(t.getGroups().size()));
                        probeDecoder();
                    }
                });

                player.setMediaItem(buildItem(url));
                player.prepare();
            }
        });
    }

    @Override public void switchTo(final String url) { switchTo(url, null); }

    @Override public void switchTo(final String url, final String headersJson) {
        /* 请求头变了只能重建播放器（DataSource 在 build 时定死）→ 落到 play() */
        if (SniffProbe.parseHeaders(headersJson) != null) { play(url, headersJson); return; }
        ui.post(new Runnable() {
            @Override public void run() {
                if (player == null) { play(url); return; }
                lastUrl = url;
                hlsRetry = 0;    /* 换到另一条流：自救次数重新计 */
                emit("buffering", "switch");
                player.setMediaItem(buildItem(url));
                player.prepare();
            }
        });
    }

    @Override public void pause()  { ui.post(new Runnable() { @Override public void run() { if (player != null) player.setPlayWhenReady(false); } }); }
    @Override public void resume() { ui.post(new Runnable() { @Override public void run() { if (player != null) player.setPlayWhenReady(true); } }); }
    @Override public void stop()   { ui.post(new Runnable() { @Override public void run() { release(); } }); }

    @Override public String stats() {
        if (player == null) return "{}";
        try {
            StringBuilder sb = new StringBuilder(160);
            sb.append("{\"state\":").append(player.getPlaybackState());
            sb.append(",\"playing\":").append(player.isPlaying());
            sb.append(",\"buffered\":").append(player.getBufferedPosition());
            sb.append(",\"pos\":").append(player.getCurrentPosition());
            sb.append(",\"bufferedMs\":").append(player.getBufferedPosition() - player.getCurrentPosition());
            Format vf = player.getVideoFormat();
            if (vf != null) {
                sb.append(",\"vcodec\":\"").append(vf.sampleMimeType == null ? "" : vf.sampleMimeType).append("\"");
                sb.append(",\"w\":").append(vf.width).append(",\"h\":").append(vf.height);
                sb.append(",\"fps\":").append(vf.frameRate);
                /* 当前视频轨的标称码率（bps）—— 底栏角标显示用（v2.8） */
                sb.append(",\"br\":").append(vf.bitrate);
            }
            sb.append(",\"hw\":").append(hardwareDecode ? 1 : 0);
            /* 带宽估计（bps）：由 bwListener 的回调持续刷新。流不动时会衰减到 0，
               角标侧把 0 当作「暂无数据」处理（v2.8）。 */
            sb.append(",\"bw\":").append(bwEstimate);
            sb.append("}");
            return sb.toString();
        } catch (Throwable t) { return "{}"; }
    }

    private void probeDecoder() {
        try {
            Format vf = player == null ? null : player.getVideoFormat();
            if (vf == null || vf.sampleMimeType == null) return;
            android.media.MediaCodecList list = new android.media.MediaCodecList(android.media.MediaCodecList.REGULAR_CODECS);
            for (android.media.MediaCodecInfo ci : list.getCodecInfos()) {
                if (ci.isEncoder()) continue;
                boolean ok = false;
                for (String m : ci.getSupportedTypes()) if (m.equalsIgnoreCase(vf.sampleMimeType)) { ok = true; break; }
                if (!ok) continue;
                String n = ci.getName();
                if (!(n.startsWith("OMX.google.") || n.startsWith("c2.android.") || n.startsWith("c2.google."))) {
                    hardwareDecode = true;
                    emit("decoder", "hw:" + n);
                    return;
                }
            }
            hardwareDecode = false;
            emit("decoder", "sw-only");
        } catch (Throwable t) {}
    }

    @Override public boolean isHardwareDecode() { return hardwareDecode; }
    @Override public boolean isPlaying() { return player != null && player.isPlaying(); }

    @Override public void release() {
        /* ★ 线程安全：JS 侧 nativeStop 会从 WebView JavaBridge 线程到达这里。
           老 ExoPlayer2 虽不做严格线程校验，仍统一投递回主线程执行（同
           Media3PlayerBackend.release 的修法），避免竞态。 */
        com.google.android.exoplayer2.ExoPlayer p;
        synchronized (this) { p = player; player = null; }
        if (Looper.myLooper() == Looper.getMainLooper()) {
            if (p != null) doRelease(p);
            detachSurface();
            frozen.hide();
        } else {
            ui.post(new Runnable() { @Override public void run() {
                if (p != null) doRelease(p);
                detachSurface();
                frozen.hide();
            }});
        }
    }

    /** 只释放播放器实例，保留画面层（play 内部换 url 时用） */
    private void releasePlayerOnly() {
        com.google.android.exoplayer2.ExoPlayer p;
        synchronized (this) { p = player; player = null; }
        if (p != null) doRelease(p);
    }

    /**
     * 冻结：抓下最后一帧钉在屏幕上，然后释放播放器并摘掉画面层。
     * 与 release() 的差别只有「抓帧」—— release() 会把纹理内容清空（实测亮度 0.0），
     * 光保留 TextureView 是白留。同 Media3PlayerBackend.freeze 的处理方式。
     */
    @Override public boolean freeze() {
        final boolean has = (surface != null);
        Runnable r = new Runnable() {
            @Override public void run() {
                frozen.capture(surface);
                releasePlayerOnly();
                detachSurface();
            }
        };
        if (Looper.myLooper() == Looper.getMainLooper()) r.run();
        else ui.post(r);
        return has;
    }

    /**
     * ★ 把画面层从宿主视图树摘掉（同 Media3PlayerBackend.detachSurface）：
     *   退出播放层后 WebView 要重新露出来，留在树里的 TextureView 会一直
     *   显示最后一帧，而 WebView 背景透明 → 用户看到「返回后视频还在」。
     */
    private void detachSurface() {
        if (surface == null) return;
        try {
            if (surface.getParent() != null) host.removeView(surface);
        } catch (Throwable t) {}
        surface = null;
        attached = false;
    }

    private void doRelease(com.google.android.exoplayer2.ExoPlayer p) {
        try { p.stop(); } catch (Throwable ignore) {}
        try { p.release(); } catch (Throwable ignore) {}
    }
}
