package com.clean.tv;

import android.content.Context;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.TextureView;
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
 * media3(1.4.x) 硬解后端 —— Android 5.0+（minSdk 21）。
 * 注意：本类的任何方法都引用了 androidx.media3.*，Android 4.4 设备一旦加载本类
 * 就会 ClassNotFoundException，因此 MainActivity 必须按 SDK_INT 隔离加载（见其注释）。
 */
@UnstableApi
public class Media3PlayerBackend implements NativeBackend {

    private static final String TAG = "CleanTV/Media3";

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
        ★ ExoPlayer 接口上**没有** getBandwidthMeter()（media3 1.4 起不在接口暴露），
        只能挂 AnalyticsListener 的 onBandwidthEstimate 回调把值捞出来缓存。 */
    private volatile long bwEstimate = 0;
    private final androidx.media3.exoplayer.analytics.AnalyticsListener bwListener =
            new androidx.media3.exoplayer.analytics.AnalyticsListener() {
                @Override public void onBandwidthEstimate(
                        androidx.media3.exoplayer.analytics.AnalyticsListener.EventTime et,
                        int totalLoadTimeMs, long totalBytesLoaded, long bitrateEstimate) {
                    bwEstimate = bitrateEstimate;
                }
            };
    /** 退出播放层时钉在屏幕上的最后一帧（换台不黑屏，见 FrozenFrame 注释） */
    private final FrozenFrame frozen;

    /** 直播目标延迟见 {@link PlayTuning#liveTargetMs()}（「秒开 / 稳定」档位会影响它） */
    private static final String UA = MainActivity.APP_UA;

    public Media3PlayerBackend(Context ctx, FrameLayout host) {
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
        f.setEnableDecoderFallback(true);
        f.forceEnableMediaCodecAsynchronousQueueing();
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

    /**
     * 是不是「值得原地重播一次」的 HLS 错误。
     *
     * <p>两道条件：① 当前播的确实是 m3u8（FLV/TS 直播没有分片概念，坏一片就是流断了，
     * 该走换线路）；② 错误码属于「取不到数据」这一类（404/5xx、连不上、超时）。
     * 解码器类错误不走这里 —— 那该由网页侧换内核（软解）处理。
     */
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
        /* 提取到的流要带 Referer/Cookie（v2.9）。解析失败 = null = 老行为。
           解析放在 ui.post 之前（纯字符串处理，不涉及播放器对象）。 */
        final java.util.Map<String, String> extra = SniffProbe.parseHeaders(headersJson);
        ui.post(new Runnable() {
            @Override public void run() {
                android.util.Log.i(TAG, "play() url=" + url.substring(0, Math.min(60, url.length())));
                lastUrl = url;
                hlsRetry = 0;    /* 新的一条流：HLS 自救次数重新计（见 onPlayerError） */
                /* ★ 只释放旧播放器实例，不动画面层 —— 切线路/换台时画面不能黑一下。 */
                releasePlayerOnly();
                ensureSurface();
                if (!attached) { host.addView(surface, 0); attached = true; }
                /* 保帧画面可能还钉着（上一次退出播放层留下的）—— 起兜底定时器，
                   首帧一到就撤（onRenderedFirstFrame），不来也不会永远盖着。 */
                frozen.armAutoHide(12000);

                DefaultHttpDataSource.Factory http = new DefaultHttpDataSource.Factory()
                        .setUserAgent(UA).setConnectTimeoutMs(15000).setReadTimeoutMs(15000)
                        .setAllowCrossProtocolRedirects(true);
                /* ★ 官网源提取到的流：原样带上页面的 Referer / Cookie / Origin，
                   少一个服务器就 403（实测 m.gdtv.cn 这类站点都校验 Referer）。 */
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
                    @Override public void onVideoSizeChanged(VideoSize s) { emit("size", s.width + "x" + s.height); }
                    @Override public void onRenderedFirstFrame() {
                        /* ★ 新流第一帧真的上屏了 —— 这时才撤掉保帧画面。
                           之前用「ready / size」撤不行：那两个都早于出图，
                           撤早了会露出 TextureView 的黑底，等于白保。 */
                        frozen.hide();
                        /* ★ 同时报给网页：这是「出首帧」唯一权威信号。
                           网页的起播看门狗必须靠它（或 ready / 非 0x0 的 size）撤，
                           不能靠 size 事件 —— 切换媒体项时 media3 会先发一次
                           VideoSize.UNKNOWN(0x0)，会把看门狗瞬间撤掉（实测踩到）。 */
                        emit("firstframe", "native");
                    }
                    @Override public void onIsPlayingChanged(boolean p) { if (p) emit("ready", "playing"); }
                    @Override public void onPlayerError(PlaybackException e) {
                        Log.w(TAG, "player error", e);
                        /* ★ HLS 分片级错误的「同流自救」（v2.7）
                           实测（tools/_probe_hls.py，把第 3 个分片指向 404）：
                           默认策略下 ExoPlayer 会一路 fatal ——
                               /seg0 200 → /seg1 200 → /bad 404 ×6
                               → HlsMediaChunk.prepareExtraction 抛 IOException
                               → ExoPlayerImplInternal.handleIoException
                           position 停在 11883ms（正好是前两个分片的 12 秒）再不动，
                           坏片之后**一个正常分片都没再请求过** —— 也就是
                           「一个分片坏掉 = 一条线路作废」。
                           但直播里的坏片是**暂时的**：坏片后面的分片全都是好的，
                           只是 ExoPlayer 已经卡在那个位置上了。所以先自救 ——
                           跳到直播边缘重播**同一条流**绕开坏片（对 live，
                           seekToDefaultPosition() 就是「跳到最新可播位置」）。
                           自救也失败，才把 error 交给 JS 走换线路。 */
                        if (isHlsStreamError(e) && hlsRetry < HLS_SELF_RETRY_MAX) {
                            hlsRetry++;
                            Log.i(TAG, "HLS stream error -> seek to live edge, same-stream retry "
                                    + hlsRetry + "/" + HLS_SELF_RETRY_MAX);
                            emit("hlsskip", String.valueOf(hlsRetry));
                            ui.post(new Runnable() {
                                @Override public void run() {
                                    if (player == null) return;
                                    player.seekToDefaultPosition();   /* live：跳到直播边缘 */
                                    player.prepare();
                                }
                            });
                            return;      /* ★ 不走 release + error：画面层继续留着 */
                        }
                        emit("error", e.getErrorCodeName());
                        // ★ 出错后延迟自释放：JS 会回落软解（nextLine），
                        //   原生 player 若挂着，AudioSink 的 AudioTrack 可能
                        //   保持 started 泄漏音频通道（后台残声隐患之一）。
                        ui.post(new Runnable() {
                            @Override public void run() { release(); }
                        });
                    }
                    @Override public void onTracksChanged(Tracks t) {
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
        /* ★ 请求头变了就只能重建播放器（DataSource 是 build 时定死的），
           直接落到 play()。没有额外头时依旧走「换源不换实例」的快路径。 */
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
    @Override public void stop()   {
        android.util.Log.i(TAG, "stop() called, player=" + (player != null));
        ui.post(new Runnable() { @Override public void run() { release(); } });
    }

    @Override public String stats() {
        if (player == null) return "{}";
        try {
            StringBuilder sb = new StringBuilder(160);
            sb.append("{\"state\":").append(player.getPlaybackState());
            sb.append(",\"playing\":").append(player.isPlaying());
            sb.append(",\"buffered\":").append(player.getBufferedPosition());
            sb.append(",\"pos\":").append(player.getCurrentPosition());
            sb.append(",\"bufferedMs\":").append(player.getBufferedPosition() - player.getCurrentPosition());
            androidx.media3.common.Format vf = player.getVideoFormat();
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
            androidx.media3.common.Format vf = player == null ? null : player.getVideoFormat();
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
        /* ★ 线程安全：JS 侧 __appPause 经 NativeTV.nativeStop → destroy() 到达这里时
           跑在 WebView 的 JavaBridge 线程上 —— media3 严格校验「必须在创建线程
           （main）访问」，直接调会抛 IllegalStateException 且被上层吞掉，
           而 player=null 照样执行 → ExoPlayer 实例永久泄漏、后台续响
           （实测 HOME 残声根因之二，2026-10-11 logcat 铁证）。
           处理：先在当前线程摘下引用（player=null，保证 stop/play 的空转判断
           一致），再按需投递回主线程执行真正的 stop+release。 */
        androidx.media3.exoplayer.ExoPlayer p;
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
        androidx.media3.exoplayer.ExoPlayer p;
        synchronized (this) { p = player; player = null; }
        if (p != null) doRelease(p);
    }

    /**
     * 冻结：抓下最后一帧钉在屏幕上，然后释放播放器并摘掉画面层。
     *
     * 与 release() 的差别只有「抓帧」这一步 —— 实测 release() 本身会把纹理
     * 内容清空（揭帧后整屏亮度 0.0），所以光保留 TextureView 是没用的。
     * 线程安全处理与 release() 一致（JS 桥线程会调进来）。
     */
    @Override public boolean freeze() {
        final boolean has = (surface != null);
        Runnable r = new Runnable() {
            @Override public void run() {
                frozen.capture(surface);      /* 先抓（此时 TextureView 还在播放） */
                releasePlayerOnly();
                detachSurface();
            }
        };
        if (Looper.myLooper() == Looper.getMainLooper()) r.run();
        else ui.post(r);
        return has;
    }

    private void doRelease(androidx.media3.exoplayer.ExoPlayer p) {
        try { p.stop(); } catch (Throwable t) { android.util.Log.w(TAG, "stop() threw", t); }
        try { p.release(); } catch (Throwable t) { android.util.Log.w(TAG, "release() threw", t); }
        android.util.Log.i(TAG, "release() done");
    }

    /**
     * ★ 把画面层从宿主视图树摘掉。退出播放层（BACK / nativeStop / destroy）时
     *   WebView 要重新露出来；TextureView 留在树里会一直显示最后一帧，
     *   而 WebView 背景是透明的 —— 用户看到的就是「返回后视频还在、列表不见了」
     *   （实测：BACK 后截图整屏都是竖屏裁切的视频残留帧）。
     *   置 null 让下次 play 重建，免得复用已 detach 的 TextureView。
     */
    private void detachSurface() {
        if (surface == null) return;
        try {
            if (surface.getParent() != null) host.removeView(surface);
        } catch (Throwable t) {}
        surface = null;
        attached = false;
        android.util.Log.i(TAG, "surface detached");
    }
}
