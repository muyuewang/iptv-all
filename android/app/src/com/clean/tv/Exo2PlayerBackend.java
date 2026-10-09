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

    private static final long LIVE_TARGET_MS = 1500;
    private static final String UA = MainActivity.APP_UA;

    public Exo2PlayerBackend(Context ctx, FrameLayout host) {
        this.ctx = ctx;
        this.host = host;
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

    private MediaItem buildItem(String url) {
        MediaItem.LiveConfiguration liveCfg = new MediaItem.LiveConfiguration.Builder()
                .setTargetOffsetMs(LIVE_TARGET_MS).build();
        return new MediaItem.Builder().setUri(Uri.parse(url)).setLiveConfiguration(liveCfg).build();
    }

    @Override public void play(final String url) {
        ui.post(new Runnable() {
            @Override public void run() {
                lastUrl = url;
                ensureSurface();
                if (!attached) { host.addView(surface, 0); attached = true; }
                release();

                DefaultHttpDataSource.Factory http = new DefaultHttpDataSource.Factory()
                        .setUserAgent(UA).setConnectTimeoutMs(15000).setReadTimeoutMs(15000)
                        .setAllowCrossProtocolRedirects(true);
                DefaultDataSource.Factory ds = new DefaultDataSource.Factory(ctx, http);

                DefaultTrackSelector ts = new DefaultTrackSelector(ctx);
                ts.setParameters(ts.buildUponParameters()
                        .setAllowVideoMixedMimeTypeAdaptiveness(true)
                        .setAllowVideoNonSeamlessAdaptiveness(true));

                DefaultLoadControl lc = new DefaultLoadControl.Builder()
                        .setBufferDurationsMs(6000, 30000, 1500, 3000)
                        .setPrioritizeTimeOverSizeThresholds(true).build();

                ExoPlayer.Builder b = new ExoPlayer.Builder(ctx, buildRenderers())
                        .setMediaSourceFactory(new DefaultMediaSourceFactory(ds))
                        .setTrackSelector(ts).setLoadControl(lc);
                player = b.build();
                ensureSurface();
                player.setVideoTextureView(surface);
                player.setPlayWhenReady(true);
                player.setRepeatMode(Player.REPEAT_MODE_OFF);

                player.addListener(new Player.Listener() {
                    @Override public void onPlaybackStateChanged(int st) {
                        switch (st) {
                            case Player.STATE_BUFFERING: emit("buffering", "native"); break;
                            case Player.STATE_READY:     emit("ready", "native"); break;
                            case Player.STATE_ENDED:     emit("ended", "native"); break;
                        }
                    }
                    @Override public void onVideoSizeChanged(com.google.android.exoplayer2.video.VideoSize s) { emit("size", s.width + "x" + s.height); }
                    @Override public void onIsPlayingChanged(boolean p) { if (p) emit("ready", "playing"); }
                    @Override public void onPlayerError(PlaybackException e) {
                        Log.w(TAG, "player error", e);
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

    @Override public void switchTo(final String url) {
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
            }
            sb.append(",\"hw\":").append(hardwareDecode ? 1 : 0);
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
        if (player != null) {
            try { player.stop(); } catch (Throwable ignore) {}
            try { player.release(); } catch (Throwable ignore) {}
            player = null;
        }
    }
}
