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

    private static final long LIVE_TARGET_MS = 1500;
    private static final String UA = MainActivity.APP_UA;

    public Media3PlayerBackend(Context ctx, FrameLayout host) {
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
                    @Override public void onVideoSizeChanged(VideoSize s) { emit("size", s.width + "x" + s.height); }
                    @Override public void onIsPlayingChanged(boolean p) { if (p) emit("ready", "playing"); }
                    @Override public void onPlayerError(PlaybackException e) {
                        Log.w(TAG, "player error", e);
                        emit("error", e.getErrorCodeName());
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
        if (player != null) {
            try { player.stop(); } catch (Throwable ignore) {}
            try { player.release(); } catch (Throwable ignore) {}
            player = null;
        }
    }
}
