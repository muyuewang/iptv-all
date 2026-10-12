# 借鉴方案 · TVBoxOS（q215613905/TVBoxOS）

> 本文基于**实际拉取源码逐行核对**（`_ref/TVBoxOS`，main 分支，2026-09-14 提交 `ab11d28`），
> 不是看 README 猜的。所有引用都带文件名+行号，可复查。
> 定位：回答「TVBoxOS 有没有值得我们抄的地方」，并给出可落地的改造清单。

---

## 0. 结论速览

> **落地进度（2026-10-11 更新）**：
> v2.6 做了第 1、2 项（起播看门狗 / 全源失败自动跳台，见 `开发手册.md` I / J 节）；
> v2.7 做了第 3、4 项 —— 其中第 3 项**推翻了原方案**：TVBoxOS 的
> `HlsErrorHandlingPolicy` 经实测是负收益（重试 6→22 次且仍然 fatal），已回退，
> 改用「原生 seek 到直播边缘同流自救」解决同一个问题；第 4 项把内核判死的粒度
> 从「频道」收窄到「线路」。
> v2.8 做了第 5 节「细节补全」里的**网速 / 分辨率角标**（`statBadge`，见 `开发手册.md` M 节）。
> 第 5 节里**缓冲上限下调 6s→3s 已做**；**时移回看**未做（要先确认后端接口支不支持
> `catchup` 参数）。

| 判断 | 项目 | 我们的现状 |
|---|---|---|
| ★★★ **强烈建议抄** | 起播看门狗（超时即换源，不等线路自己超时） | **✅ v2.6 已做** —— `armStartWatch()` 10s/15s（秒开/稳定），`_test_watchdog.py` 实测挂起线路 **~10s 换线**（原为等满 15s） |
| ★★★ **强烈建议抄** | 全源失败 → **自动跳下一个频道** | **✅ v2.6 已做** —— 失败卡 8s 倒计时自动跳台（`hopOpts()`），并加了参考实现所没有的防雪崩上限（连跳 ≤5） |
| ⚠️ **方案不可抄、但问题已解决** | HLS 切片错误 → 跳过坏切片继续播 | **v2.7** —— 它那份 policy 实测把坏片重试从 6 次放大到 **22 次**且仍然 fatal（返回 `null` 是**禁用** fallback，注释写反了），**已回退**；改用「`seekToDefaultPosition()` 跳到直播边缘同流自救」，实测坏片 6.6s 后恢复、后续分片全部正常 |
| ★★ 建议抄 | 换源前先「换解码内核重播同一条流」 | **✅ v2.7 已做** —— 第 1 级（原生报错 → 软解重播**同一条 URL**）本来就有；v2.7 把内核判死粒度从**频道**收窄到**线路**（`NATIVE.dead` → `NATIVE.deadIdx[i]`），换下一条线路时重获原生机会 |
| ✅ 我们已经做对了 | 换台快照（Bitmap 钉屏） | **v2.5.1 刚做完并实测通过**（FrozenFrame） |
| ✅ 顺手抄了 | 显示网速 / 分辨率角标（`LIVE_SHOW_NET_SPEED`/`_RESOLUTION`） | **✅ v2.8 已做** —— 底栏 `#statBadge` 三态循环，详见 4.3 |
| ⛔ 不建议抄 | 影视聚合架构（爬虫 jar/js/py） | 与「无广告直播客户端」定位相反 |
| ⛔ 不建议抄 | 引入 IJK/FFmpeg 软解内核 | 多 30–50MB so + NDK，我们已有 mpegts 软解兜底 |
| ⛔ 不建议抄 | doikki VideoView 整套 UI | 那是点播形态（进度条/倍速/字幕），我们直播控制条更贴 |

---

## 1. 两边的根本差异（先认清，才不会乱抄）

| 维度 | 纯净电视（我们） | TVBoxOS |
|---|---|---|
| 形态 | 无广告直播客户端，**自己调原 App 接口、自主解密线路** | 影视聚合壳：点播 + 直播，内容靠远程 JSON 配置 + 爬虫（jar/QuickJS/Python） |
| 播放内核 | media3(ExoPlayer 1.4) / 老 ExoPlayer2(2.19) + mpegts.js(WebView MSE) | IJKPlayer(FFmpeg) + ExoPlayer2(doikki 封装) + 系统播放器，可调外部 App |
| 渲染 | 原生 TextureView / WebView `<video>` | TextureView / SurfaceView 可切 |
| 频道来源 | 原 App 后端（加密接口）+ 官网源 WebView 注入 | M3U / TXT / JSON 订阅 |
| 代码量 | 20,351 行（核心 + 三端） | `app` + `player` + `quickjs` + `pyramid` 四个模块 |
| 最低版本 | Android 4.4（你刻意兼容） | API 16 |

**一句话**：它的**内容层**跟我们完全不同（不值得抄），但它的**播放失败处理**做得比我们细，那部分直接可用。

---

## 2. ★ 最值得抄的 5 件事

### 2.1 起播看门狗 + 三级退让（**收益最大，建议第一个做**）

**它的证据**：`app/.../ui/activity/LivePlayActivity.java:2266-2360`

```java
public void playStateChanged(int playState) {
    mHandler.removeCallbacks(mConnectTimeoutChangeSourceRun);   // ① 每次状态变化先撤掉旧定时器
    switch (playState) {
        case STATE_PREPARED: case STATE_BUFFERED: case STATE_PLAYING:
            hideSwitchChannelSnapshot();
            currentLiveChangeSourceTimes = 0;                   // ③ 成功了：计数清零
            allowLiveSwitchPlayer = true;
            break;
        case STATE_ERROR: case STATE_PLAYBACK_COMPLETED:
            mHandler.postDelayed(mConnectTimeoutChangeSourceRun, 3500);          // 出错：3.5s 后换源
            break;
        case STATE_PREPARING: case STATE_BUFFERING:
            mHandler.postDelayed(mConnectTimeoutChangeSourceRun,
                (Hawk.get(HawkConfig.LIVE_CONNECT_TIMEOUT, 1) + 1) * 5000L);     // ② 默认 10s 还没画面就换源
            break;
    }
}

private Runnable mConnectTimeoutChangeSourceRun = new Runnable() {
    public void run() {
        if (switchLivePlayerAndReplay()) return;      // 第 1 级：换内核重播【同一条流】
        currentLiveChangeSourceTimes++;
        if (currentLiveChannelItem.getSourceNum() == currentLiveChangeSourceTimes) {
            currentLiveChangeSourceTimes = 0;
            playChannel(...getNextChannel(...));      // 第 3 级：本频道源全试完 → 自动跳下一个频道
        } else {
            playNextSource();                         // 第 2 级：换下一条源
        }
    }
};
```

**为什么这条对我们最值钱**：

- 它的超时是**统一的「从进入 PREPARING/BUFFERING 起算 10 秒」**，到点就换 —— 不管底下的连接是卡在哪。
- 我们的超时是**每条线路各自等 ExoPlayer 的 `readTimeoutMs=15000`**。文档 `开发手册.md` 里已经记了这条痛：**「挂起型线路是逐线等满超时才换 → 换台兜底整链可超 90s」**（e2e 步骤预算都被迫放宽到 150s）。
- 差别在于：**它把「等多久」这个决策权从网络层收回到播放器的状态机**。线路挂起时 ExoPlayer 会一直停在 `STATE_BUFFERING`，我们没有任何东西在计时，只能等 TCP 超时。

**我们的改法（不需要动 Java）**：

`player.html` 里已经有 `NATIVE.lastUrl` / `NATIVE.lastPos` / `NATIVE.stallT` 和 2.5s 的 `bindNativeWatch` 看护。把「8 秒位置不前进才换」补成**两级**：

```
① 起播看门狗（新增）：playIdx() 时起一个 10s 定时器；
   __onNativeState("size"/"ready") 或 soft 的 playing 事件到了就 clearTimeout。
   到点还没出帧 → 直接 nextLine()（不看位置、不等 readTimeout）。
② 卡顿看护（已有）：起播后 8s 零推进 → nextLine()。
```

只在 `player.html` 一个文件里加约 25 行，风险低，**直接对着「换台耗时」这个体感痛点**。
建议顺手把超时值做成 `设置` 项（对应它的 `LIVE_CONNECT_TIMEOUT`，1–3 档 → 10s/15s/20s）。

### 2.2 全源失败 → 自动跳下一个频道

同上，`mConnectTimeoutChangeSourceRun` 的第三级。它的逻辑是：

> 本频道的 N 条源都试过一遍（`currentLiveChangeSourceTimes == getSourceNum()`）→ 计数归零 → 取下一个频道继续播。

我们现在的行为：全线路失败 → `showErrCard()` 停在失败卡上等用户点。对**直播**来说「换台」往往比「重试」更符合直觉（用户就是想看点什么）。而且我们是**无广告直播 App，用户多半是拿遥控器在刷台**。

**改法**：失败卡保留，但加一行倒计时「3 秒后自动换下一个频道」+ 一个「取消」按钮。8 秒足够了（比它的 3.5s 宽一点，因为我们的失败判定本身更慢）。

### 2.3 HLS 切片错误：跳过坏切片，而不是整条线路作废

**它的证据**：`player/src/main/java/xyz/doikki/videoplayer/exo/HlsErrorHandlingPolicy.java`（全文 63 行）

```java
public class HlsErrorHandlingPolicy extends DefaultLoadErrorHandlingPolicy {
    private static final int MAX_RETRIES = 3;      // 切片最多重试 3 次
    private static final long RETRY_DELAY_MS = 500; // 间隔 500ms

    @Override public int getMinimumLoadableRetryCount(int dataType) {
        if (dataType == C.DATA_TYPE_MEDIA) return MAX_RETRIES;   // 避免无限重试
        return super.getMinimumLoadableRetryCount(dataType);
    }

    @Override public FallbackSelection getFallbackSelectionFor(...) {
        if (isChunkError(loadErrorInfo)) return null;  // ★ 不用 fallback = 让 ExoPlayer 跳过这个切片继续播
        return super.getFallbackSelectionFor(...);
    }

    private boolean isChunkError(LoadErrorInfo i) { return i.exception instanceof IOException; }
}
```

**对我们的意义**：我们的 m3u8 是**官网源里的直连线路**（`WVTCore.isDirect(url)`）。直播 HLS 每隔几秒一个新切片，源站偶尔吐一个 404/超时是常态 —— 我们现在只要碰到就判这条线路死，然后整条兜底链再走一遍。这个 policy 让「一个切片坏了」不再等于「一条线路废了」。

> ⚠️ 注意：**只对 HLS 生效**。FLV/TS 直播没有"切片"概念，坏一片就是流断了，仍走换线路。

#### ❌ 实测更正（2026-10-11）：**这份 policy 不能抄**

我们把它原样实现了出来（两后端各一个文件，挂到
`DefaultMediaSourceFactory.setLoadErrorHandlingPolicy(...)`），然后造了一条真流对比 ——
本机起 HTTP 服务，提供 6 个**真实 TS 分片**（Apple 的 bipbop 测试流，首字节 `0x47`），
把第 3 个分片指向恒定 404，经 `adb reverse` 塞给设备里的 ExoPlayer。
脚本：`tools/_probe_hls.py`。

| | 基线（无 policy） | 挂上这份 policy |
|---|---|---|
| `/bad.ts` 被请求次数 | **6** | **22** |
| 坏片之后拿到的正常分片 | 0 | 0 |
| position 最大值 | 11883 | 11892 |

两边最终**都卡死**（position 停在 11883ms，正好是前两个分片的 12 秒），
而挂上 policy 的那次重试次数变成 **3.6 倍** —— **明确负收益**。

根因是那份实现的注释写错了：`getFallbackSelectionFor` 返回 `null` 在 ExoPlayer 里的
语义是「**不做** fallback」，而不是「跳过这个切片继续播」；`DefaultLoadErrorHandlingPolicy`
默认反而会对可恢复的 IOException 做 track fallback。它真正改到的只有「重试间隔固定
500ms」这一条 —— 让失败来得**更密**，不是更少。

**结论：代码已回退，没有进 v2.7。** 这条「★★ 建议抄」据此降级为「⛔ 实测无效」。

#### ✔ 真正有效的解法（已落地 v2.7）

问题本身是真的（实测确认「一个坏片 = 一条线路作废」），只是**解法不该在网络策略层**。
抓手在我们自己的错误处理里：直播的坏片是**暂时的** —— 坏片后面的分片全都是好的，
只是 ExoPlayer 已经卡在那个位置上了。**让它离开那个位置就能继续。**

对 live 流，`Player.seekToDefaultPosition()` 正好就是「跳到最新可播位置」。
于是在 fatal 之前先自救一次（`Media3PlayerBackend.onPlayerError()` / `Exo2PlayerBackend` 同）：

```java
if (isHlsStreamError(e) && hlsRetry < HLS_SELF_RETRY_MAX) {   /* 当前是 .m3u8 + IO 类错误码 */
    hlsRetry++;
    emit("hlsskip", String.valueOf(hlsRetry));                /* 网页侧显示「正在跳到直播位置重试…」 */
    ui.post(() -> { player.seekToDefaultPosition(); player.prepare(); });
    return;                                                   /* ★ 不走 release + error */
}
emit("error", e.getErrorCodeName());                          /* 自救两次仍不行 → 才交给换线路 */
```

**实测（`tools/_probe_hls_live.py`）** —— 造一个**真直播**流（本机滚动窗口 playlist，
3 个分片、每 6 秒前进一格、无 ENDLIST，分片内容是真实 TS），让 `seg3` 在开播后
20 秒内恒定 404：

```
[ 6.2s] /seg3.ts 404  ★ 坏片开始（连拒 8 次）
[12.8s] /seg4.ts 200  ← ★ 自救生效：拿到下一个正常分片
[18.8s] /seg5.ts 200 → seg6/7/8/9/10/11 每 6 秒一个，一路播下去
日志: HLS stream error -> seek to live edge, same-stream retry 1/2 · 2/2
```

> 为什么必须用**直播**流测：VOD 里 `seekToDefaultPosition()` 是「回到开头」，
> 跳不出坏片；只有直播里它才是「跳到最新可播位置」。

### 2.4 换源前先「换解码内核重播同一条流」

**它的证据**：`LivePlayActivity.java:2327-2343`

```java
private boolean switchLivePlayerAndReplay() {
    if (!allowLiveSwitchPlayer || currentLiveChannelItem == null || mVideoView == null) return false;
    mHandler.removeCallbacks(mConnectTimeoutChangeSourceRun);
    mVideoView.release();
    if (!livePlayerManager.switchLivePlayer(mVideoView)) { allowLiveSwitchPlayer = false; return false; }
    allowLiveSwitchPlayer = false;
    mVideoView.setUrl(retryUrl, liveChannelHeader());
    mVideoView.start();
    return true;
}
```

它的换源第 1 级**不是换线路，而是换内核**（IJK ↔ EXO）**重播同一条 URL**。理由很实际：**有些流是"这个解码器解不了"，而不是"这条线路不通"** —— 换内核比换线路便宜（不用重新请求接口/测速），而且能救回那些线路本身没问题的频道。

**我们的对应物**：我们已经有「原生硬解 ↔ mpegts 软解」两条通道，现在的策略是**硬解失败才软解**（`NATIVE.dead` 标记）。可借鉴的是把顺序改成：

```
起播看门狗超时
  ├─ 第 1 级：换内核重播同一条 URL（native↔soft 互换，且这次不清 NATIVE.dead 以外的状态）
  ├─ 第 2 级：nextLine()
  └─ 第 3 级：全线路失败 → 自动换下一个频道（2.2）
```

**注意**：我们的 `NATIVE.dead` 是「本频道本次会话不再试原生」，用来防「硬解失败→切线→又硬解」死循环。引入第 1 级时要小心别把这个保护绕过去 —— 建议每轮只允许换内核一次（同一个 `openChannel` 会话内最多 1 次）。

#### ✔ 已落地 v2.7（分三处）

**① 「第 1 级」其实早就在了** —— 原生报错时我们已经会 `playSoft(cur.idx)`，
而那**本身就是换内核重播同一条 URL**（`cur.idx` 没动）。这一级不用新加，只是文案
以前叫「硬解失败，自动切换软解」，现在说清了它是什么：「换软解**重播同一条线路**」。

**② 真正该改的是 `NATIVE.dead` 的粒度**（v2.7 已改）。
它原来是**频道级**的 —— 线路 1 硬解失败一次，本频道**后续所有线路**都直走软解，
等于让一条坏线路连坐了本来原生能播的线路；多线路频道上用户会莫名看到
「明明别的线路硬解能播，却一直是软解」。现在收窄成**线路级**：

```js
NATIVE.deadIdx = {};                 /* openChannel：新频道清空整张表 */
NATIVE.deadIdx[cur.idx] = true;      /* __onNativeState("error")：只标记**这一条**线路 */
if(NATIVE.ok && !NATIVE.deadIdx[i]){ /* playIdx：按线路下标判断 → 换线路时重获原生机会 */
```

**③ 原方案没提、但我们实测撞到的一处：HLS 分片错误也走「同流重播」** ——
见上面 2.3 的「✔ 真正有效的解法」（`seekToDefaultPosition()` 跳到直播边缘）。
两者是**不同层**的自救：分片问题在**流的位置**上救，解码问题在**内核**上救。

### 2.5 直播细节清单（低成本、直接抄数值）

**它的证据**：`app/.../player/IjkMediaPlayer.java` 的 `setOptions()`

```java
if (Hawk.get(HawkConfig.PLAYER_IS_LIVE, false)) {
    mMediaPlayer.setOption(OPT_CATEGORY_PLAYER, "max_cached_duration", 300);   // 直播：缓存上限 300ms！很低
    mMediaPlayer.setOption(OPT_CATEGORY_FORMAT, "flush_packets", 1);           // 立刻吐包，别攒
    mMediaPlayer.setOption(OPT_CATEGORY_PLAYER, "min-frames", 1);              // 1 帧就能起
    mMediaPlayer.setOption(OPT_CATEGORY_CODEC,  "threads", "1");               // 解码线程 1（省 CPU）
} else {
    mMediaPlayer.setOption(OPT_CATEGORY_PLAYER, "max_cached_duration", 3000);  // 点播：3s
    mMediaPlayer.setOption(OPT_CATEGORY_CODEC,  "threads", "2");
}
// 公共：
mMediaPlayer.setOption(OPT_CATEGORY_FORMAT, "dns_cache_clear", 1);
mMediaPlayer.setOption(OPT_CATEGORY_FORMAT, "dns_cache_timeout", -1);          // DNS 缓存永久有效
mMediaPlayer.setOption(OPT_CATEGORY_FORMAT, "safe", 0);
mMediaPlayer.setOption(OPT_CATEGORY_FORMAT, "protocol_whitelist", "ijkio,ffio,...");
```

**可以直接对照的**：

| 它的做法 | 我们的对应物 | 建议 |
|---|---|---|
| 直播 `max_cached_duration=300ms` | `PlayTuning.minBufferMs()=6000`、`bufferForPlaybackMs=700/1500` | 我们的「秒开」档已经是同一思路，但 6s 的 min buffer 偏大 —— **直播场景可以降到 2~3s** |
| 直播解码 `threads=1` | 我们没有这个旋钮 | 低端机可考虑给 mpegts 侧关 Worker（我们 `TIER==="low"` 已经关了 ✅） |
| `dns_cache_clear/dns_cache_timeout=-1` | 我们没有显式 DNS 缓存 | 安卓侧可忽略（系统 DNS 有缓存），**桌面端有意义** |
| 直播 m3u8 走本地代理解决 DNS（`ControlManager.proxy`） | 我们官网源靠 WebView 自己解析 | 不必抄 |
| 时移/回看 `catchup` / `catchup-source` / `catchup-replace` | 我们没有 | 见 4.3（可选） |
| 显示网速 / 分辨率 / 时间（`LIVE_SHOW_NET_SPEED` / `_RESOLUTION` / `_TIME`） | 我们有 `stats()` 但 UI 没暴露 | **✅ v2.8 已做**，见 4.3（时间我们本来就有 `#clockNow`，没重复做） |
| `noAd(flag)`：识别 tx/youku/qq/… 推广告线路 | —— | **不要抄**（我们本来就只有纯净源） |

### 2.6 换台快照 —— 它印证了我们刚做的方向 ✅

**它的证据**：`LivePlayActivity.java:1767-1795`

```java
private void showSwitchChannelSnapshot() {
    Bitmap bitmap = null;
    try { bitmap = mVideoView.doScreenShot(); } catch (Throwable ignored) {}   // 就是 TextureView.getBitmap()
    if (bitmap != null) { switchChannelSnapshotImage.setImageBitmap(bitmap); ... }
    switchChannelSnapshotOverlay.setVisibility(View.VISIBLE);
}
private void hideSwitchChannelSnapshot() { ... }   // STATE_PLAYING/BUFFERED/ERROR 时收起
```

和我 v2.5.1 的做法**是同一个思路**（抓 Bitmap → 铺在播放器之上），而且它也是只在「上一台确实在播」时才抓（`playChannel():1807` `showPreviousFrame = ... mVideoView.isPlaying()`）。

**两处我们可以做得更好 / 需要对齐的差异**：

1. **抓帧时机**：它是在**换台动作发生时**抓（`playChannel` 里），我们是在**退播放层时**抓（BACK）。后果：它换台全程有快照；我们从列表开台也有（因为帧一直留着）。**各有取舍** —— 我们多留一帧内存（<1MB），换来「回列表再开台」也无缝。
2. **撤帧时机**：它按**播放状态机**（BUFFERED/PLAYING）撤；我们按 **`onRenderedFirstFrame()`** 撤。**我们的更准** —— `BUFFERED` 可能早于第一帧上屏，撤早了会露出 TextureView 黑底。
3. **已知坑（我们踩过、它没处理）**：`ExoPlayer.release()` 会**清空纹理内容**。它 `showSwitchChannelSnapshot()` 在 `mVideoView.release()`（`switchLivePlayerAndReplay`）之前没抓帧 —— 也就是说它「换内核」那条路径上，快照可能是空的。我们把抓帧放在 `releasePlayerOnly()` **之前**，是对的。

结论：**这条不用抄，我们已经走通了**，但可以照它补一条：**只在「上一台在播」时才留帧**（我们现在的 `NATIVE.frozen` 是"有 surface 就留"，语义上略宽）。

---

## 3. 不建议抄的部分（以及理由）

| 项 | 为什么不要 |
|---|---|
| **影视聚合架构**（JSON 配置 + jar/QuickJS/Python 爬虫 + 解析线路） | 我们的定位是「**无广告**直播客户端」，自研解密直连；引入聚合层等于把「广告/解析线路」这个我们刻意避开的东西请回来 |
| **引入 IJK/FFmpeg 第三内核** | 多 30–50MB so、需要 NDK 工具链、拖累 APK 体积（我们现在的 APK 只有 5MB）、且 4.4 兼容包袱重。我们已有 mpegts.js 软解兜底，**只有在实测碰到"两套都解不了"的流时才值得考虑** |
| **doikki VideoView 整套 UI 层** | 它是点播播放器形态（进度条/倍速/字幕/手势 seek）。我们直播控制条（+ 秒开档位 + 失败卡）比它更贴场景；整套换掉是负收益 |
| **外部播放器调用**（MX/VLC/Kodi/附近TVBox） | 与「精简、开箱即用」的产品定位相反 |
| **Hawk/Room/OkGo/Glide 一整套依赖** | 我们是手写零依赖构建（aapt2+d8 手写），引入这些等于换掉整个构建方式 |

---

## 4. 落地清单（按性价比排序，全部在我们现有架构内）

### 4.1 起播看门狗（半天，建议本周做） ✅ 已落地（v2.6）

> 实现落在 `player.html` 的 `armStartWatch()` / `markFirstFrame()` / `startWatchMs()`，
> 超时值跟「秒开/稳定」档联动（10s / 15s）而没新开设置项；官网网页线路不布防
> （走页面自己的看门狗）。
> **回归 `tools/_test_watchdog.py`**：本机起一个只 accept、永不回数据的 TCP 黑洞当挂起线路，
> 只量「cur.idx 前进耗时」—— 实测落在看门狗窗口内（~10s），而不是 readTimeout 的 ~15s。
> ⚠️ 两个实现陷阱见 `开发手册.md` I 节：`nativeStop()` 里不能撤定时器（playSoft 会先调它）；
> `nextLine(silent)` 避免同一件事弹两条 toast。

**位置**：`android/app/assets/player.html`
**新增**：
```js
/* 起播看门狗：从点台起算 N 秒还没出首帧就换下一条，不等线路自己超时。
   为什么需要：挂起型线路会一直停在 CONNECTING/BUFFERING，ExoPlayer 的
   readTimeout(15s) 逐条等满 —— 实测换台兜底整链可超 90s。
   借鉴 TVBoxOS LivePlayActivity.java:2296-2300（PREPARING/BUFFERING 时
   起 (LIVE_CONNECT_TIMEOUT+1)*5s 定时器，到点即换源）。 */
var START_WATCH_MS = 10000;     // 未来可做成设置项（10/15/20s）
function armStartWatch(){
  clearTimeout(__startWatchT);
  __startWatchT = setTimeout(function(){
    if(__sessionClosed) return;
    if(NATIVE.active && NATIVE.firstFrame) return;   // 已出帧
    toast("线路"+(cur.idx+1)+"起播超时，切换下一条", {type:"warn"});
    nextLine();
  }, START_WATCH_MS);
}
```
清定时器的地方：`__onNativeState("size"/"ready")`、mpegts 的 `playing` 事件、
`playSoft`/`playWebLine` 进入时、`closePlayer()`。

**验收**：`tools/_probe_seam.py` 扩一个 `watchdog` 模式；
新增 `tools/check_switch_speed.py`（v2.6 路线图里已经预留了这个名字）采样「点台→首帧」耗时。

### 4.2 HLS 切片容错 ✅ 已落地（v2.7，但**换了做法**）

**原计划**：照抄 `HlsErrorHandlingPolicy` 挂到 `setLoadErrorHandlingPolicy`。
**实测结论**：❌ 不可抄（负收益，见 2.3 的实测更正）—— 已回退。

**实际做法**：改在**错误处理层**自救 —— `Media3PlayerBackend.onPlayerError()` /
`Exo2PlayerBackend.onPlayerError()` 里，识别出「当前是 m3u8 + IO 类错误码」就先
`seekToDefaultPosition() + prepare()` 跳到直播边缘重播**同一条流**（最多 2 次），
失败才 `emit("error")` 交给网页侧换线路。网页侧收到 `hlsskip` 显示
「分片异常，跳到直播位置重试…」并复位卡顿计时（否则 8s 卡顿看护会在自救期间抢跑换线）。

**验收**（实测通过）：`tools/_probe_hls_live.py` —— 本机造一条**真直播**流
（滚动窗口 playlist，3 分片 / 每 6s 前进一格 / 无 ENDLIST / 分片是真实 TS），
让 `seg3` 在开播后 20 秒内恒定 404：

```
[ 6.2s] /seg3.ts 404 ×8   ← 坏片（连拒 8 次）
[12.8s] /seg4.ts 200      ← 自救生效
[18.8s] seg5 · 24.8s seg6 · … 一路播到 seg11
```

> 配套还修了一个连带 bug：卡顿看护的「位置是否推进」判据从 `pos > lastPos + 200`
> 改成 `|pos - lastPos| > 200` —— 直播的 position 是相对当前 window 算的，
> playlist 一滑动就会往回跳，旧判据会把「画面明明在动」误判成零推进。

### 4.3 细节补全（各 ~1 小时，可批量做）

- **缓冲上限下调** ✅ 已做（v2.6）：`PlayTuning.minBufferMs()` 6000 → **3000**
  （直播不需要 6s 底仓）；参考它 `max_cached_duration=300`（IJK 直播档）与 `min-frames=1`。
  > ⚠️ **不能直接取 2500** —— media3 要求 `minBufferMs >= bufferForPlaybackAfterRebufferMs`，
  > 而「稳定」档的后者是 3000，取 2500 会让 `setBufferDurationsMs()` **抛异常崩主线程**
  > （实测踩到，`logcat -b crash` 才看得见）。现在写成
  > `Math.max(3000, bufferForPlaybackAfterRebufferMs())`，把这个不变量焊死在代码里。
- **失败卡自动换台** ✅ 已做（v2.6）：实现为「8s 倒计时自动跳下一个频道 + 按钮可接管」
  （比原建议的 3s 宽 —— 我们的失败判定本身更慢，见 `hopOpts()`）；
  参考 `LivePlayActivity.java:2351-2358`。另加了参考实现**没有**的防雪崩上限（连跳 ≤5 次）。
- **网速 / 分辨率角标** ✅ 已做（v2.8）：底栏 `#statBadge` 三态循环
  （关 / 简 `1080P · 软解` / 详 `1920×1080 · 软解 · ↓2.4M · 缓冲1.0s`），点击切换、
  存 `localStorage`。参考它的 `LIVE_SHOW_NET_SPEED/RESOLUTION/TIME` 三个开关，
  但我们合成一个「点一下循环」的入口（没有设置页，多一个开关就多一个无处安放的 UI）。
  > 实测两处坑（详见 `开发手册.md` M 节）：① `fps`/`br` 在没声明 FRAME-RATE 的
  > HLS 上是 `Format.NO_VALUE(-1)`，直接格式化会显示「-1fps」→ 过滤掉，改显示
  > 「硬解 / 软解」；② `ExoPlayer` 接口没有 `getBandwidthMeter()`（media3 1.4 起
  > 不在接口上），带宽估计只能挂 `AnalyticsListener.onBandwidthEstimate` 捞。
  > 验收：`tools/_probe_stats_badge.py`（三态循环 + 退出后定时器必须停）。
- **（可选）时移回看**：它的 TXT 源支持 `catchup="append"` + `catchup-source="?starttime=${(b)yyyyMMddHHmmss}"`。
  我们的本机源接口有 EPG，若后端也支持回看参数，这是差异化亮点 —— 但要先确认接口。

### 4.4 明确不做
- 不引入 IJK/FFmpeg；不加外部播放器调用；不改单文件 `player.html` 架构。

---

## 5. 证据索引（可复查）

| 结论 | 文件 | 行号 |
|---|---|---|
| 起播超时换源 + 三级退让 | `app/src/main/java/com/github/tvbox/osc/ui/activity/LivePlayActivity.java` | 2266–2360 |
| 换内核重播同一条流 | 同上 | 2327–2343 |
| 换台快照（Bitmap 钉屏） | 同上 | 1767–1795 |
| 只在「上一台在播」才抓帧 | 同上 | 1807 |
| 播放器工厂 / 内核选择 | `app/src/main/java/com/github/tvbox/osc/util/PlayerHelper.java` | `updateCfg()` |
| IJK 直播/点播 option 分组 | `app/src/main/java/com/github/tvbox/osc/player/IjkMediaPlayer.java` | `setOptions()` |
| IJK 流类型分支（RTSP/M3U8/缓存） | 同上 | `setDataSource()` |
| HLS 跳过坏切片 | `player/src/main/java/xyz/doikki/videoplayer/exo/HlsErrorHandlingPolicy.java` | 全文 63 行 |
| Exo 数据源 / 格式推断 / 512M 缓存 | `player/src/main/java/xyz/doikki/videoplayer/exo/ExoMediaSourceHelper.java` | 70–241 |
| 直播配置键（超时/网速/分辨率/跨分组/时移） | `app/src/main/java/com/github/tvbox/osc/util/HawkConfig.java` | 15–59 |
| TXT/M3U 订阅解析（含 catchup） | `app/src/main/java/com/github/tvbox/osc/util/live/TxtSubscribe.java` | 19–29 |
| EPG 名称模糊匹配（内置字典 + 网络兜底） | `app/src/main/java/com/github/tvbox/osc/util/EpgNameFuzzyMatch.java` | `init()` |
| 广告源关键词（**不抄**） | `app/src/main/java/com/github/tvbox/osc/util/DefaultConfig.java` | 194–206 |

---

## 6. 一页话总结

TVBoxOS 的**内容架构**跟我们没关系（它是聚合壳，我们是自研解密直播客户端），
但它的**播放失败处理**比我们成熟一个档次，核心就一句话：

> **把「等多久」的决策权从网络层收回到播放器状态机** ——
> 给起播设一个统一的看门狗（10s），到点不问原因直接换；
> 换的顺序是「先换内核 → 再换线路 → 全挂就换台」；
> 单个 HLS 切片坏了不算线路坏。

这三条都是我们现有架构内的**纯增量改动**（不引入任何新依赖），
预计合计 1~1.5 天，直接命中「换台慢 / 挂起线路拖满超时」这个已记录在案的痛点。
