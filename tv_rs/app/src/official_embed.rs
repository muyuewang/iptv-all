//! 官网源「内嵌 WebView2 + 裸 COM 旁听」取流模块（治本路线）。
//!
//! ## 为什么是这条路线
//!
//! 官网源（WebViewTvLive `data/4/full.dat`）904 个频道里只有 54 个带直连流，
//! 其余 **850 个只给网页地址**（`tv.cctv.com/live/cctv1/`、`yangshipin.cn`、
//! 移动直播间……）。网页地址不是流，mpv 播不了；必须让**浏览器执行页面脚本**、
//! 由它发起 HLS 请求，再把那个 m3u8 抓出来交给 mpv。
//!
//! 两条取流途径，本模块选前者：
//!
//! | 方案 | 做法 | 缺点 |
//! |---|---|---|
//! | **A. 内嵌 WebView2（本文）** | 在本进程内开一个离屏 WebView2 窗加载官网页面，
//!   用**裸 COM 直调**挂 `WebResourceRequested` 旁听，抓到 m3u8 | 需要 COM 代码 |
//! | B. headless Edge + CDP | 起独立 Edge 子进程，CDP 旁听 `Network.responseReceived` |
//!   | 每次嗅探起一个 Edge（~2s 冷启动）、占额外内存、只能**猜** Referer/Origin |
//!
//! A 方案的关键优势：**能从旁听回调里读到浏览器真实发出的请求头**
//! （`ICoreWebView2HttpRequestHeaders::GetIterator` → `GetCurrentHeader`），
//! 把 **真实的** Referer / Origin / Cookie / User-Agent 原样交给 mpv。
//! 央视 CDN 缺 Referer 就 403，猜出来的 origin 未必等于浏览器实际发的那份。
//!
//! ## 实测（2026-10-25，探针 `probe_embed`）
//!
//! ```text
//! 页面 https://tv.cctv.com/live/cctv1/
//! 命中 https://ldncctvwbcdbyte.volcfcdn.com/ldncctvwbcd/cdrmldcctv1_1/index.m3u8?b=200-2100
//! 真实头: origin=https://tv.cctv.com
//!         referer=https://tv.cctv.com/
//!         user-agent=Mozilla/5.0 ... Edg/157.0.0.0
//! 耗时 15.4s（页面加载 + 播放器起播）
//! ```
//!
//! ## ★ 三个必须踩准的坑（都是实测定位，缺一个就抓不到流）
//!
//! 1. **窗口不能 `visible(false)`**。WebView2 的 `SetIsVisible(false)` 让 Chromium
//!    判定页面「不可见」→ 渲染挂起、`requestAnimationFrame` 停摆 → 直播播放器的
//!    起播逻辑**永不执行**（实测：hidden 窗只发 14 个静态资源请求就停住，40s 无 m3u8）。
//!    解法：**显示**但挪到屏幕外（`-32000,-32000`）——用户看不见，渲染却照常。
//!
//! 2. **Edge 启动参数必须与 CDP 方案对齐**（`--mute-audio --disable-gpu
//!    --no-sandbox --disable-extensions --no-first-run --no-default-browser-check`
//!    + 三个后台节流开关）。央视页面用 `document.write` 拉跨域脚本
//!    （`md5login.js` / `crypto-js.min.js` / `jsencrypt.js`），缺这些参数时
//!    Chromium 的「解析阻塞型跨域脚本」策略会把播放器脚本掐掉 —— **且不报任何错**
//!    （实测：页面只发 2 个请求、播放器完全不启动）。
//!
//! 3. **不能加 `--no-proxy-server`**。本机沙箱有 `http_proxy=127.0.0.1:14877`，
//!    但央视的静态资源**经代理才通**；加了这个参数后请求数从 208 掉回 14
//!    （页面直接加载失败）。反之 mpv 播流时**必须**直连（走代理会拿到错误页，
//!    表现为「有声音没图像」），故 `player.rs` 里对 mpv 子进程剥离代理变量。
//!
//! ## 语义要点
//!
//! - WebView2 的 `WebResourceRequested` **不 SetResponse 就是放行**，天然符合
//!   「旁听不拦截」（对应安卓 `shouldInterceptRequest 旁听 m3u8 必须 return null`）。
//! - 探针回调跑在 **WebView2 UI 线程**，嗅探结果要交给后台播放线程消费，
//!   故用 `mpsc` 通道跨线程投递。
//! - `with_webview` 在**主线程**调用时是**同步**执行的（tauri-runtime-wry 的
//!   `send_user_message` 命中 `current_thread().id() == main_thread_id` 分支直接
//!   处理），所以「建窗 → 挂探针 → 导航」可以保证探针先于导航就位。
//! - **每次嗅探开一个全新窗口**：复用同一窗口时，旧页面**迟到的请求**会串台
//!   （WebView2 0.39 的 `WebResourceRequestedEventArgs` 没有 `DocumentUri`，
//!   无法在回调里判断请求属于哪个文档）。开新窗从根上消除这个问题——
//!   WebView2 的浏览器进程是共享的，开窗远比起 Edge 便宜得多。

use std::sync::mpsc::{Receiver, Sender};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use webview2_com::Microsoft::Web::WebView2::Win32::{
    ICoreWebView2, ICoreWebView2WebResourceRequestedEventArgs,
};
use webview2_com::WebResourceRequestedEventHandler;

/// 页面 UA。与 `official_web.rs`（CDP 方案）保持一致，避免站点按 UA 分支。
pub const UA: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
                  (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0";

/// 官网直播窗「网页净化」注入脚本。
///
/// ## 目标
/// 官网源网页线路（tv.cctv.com / yangshipin.cn / m-live.cctvnews…）直播页自带
/// 大量非视频 UI（导航栏、页头页脚、频道标签、播放器露出式控制条、弹窗、广告、
/// 教程文字），直接露出来就是「原生网页的样子」——与用户要的「纯视频、和 mpv
/// 接管一样」相悖。本脚本在直播窗加载页面**运行期持续**把这些元素藏掉，只留
/// 播放视频，并把它全屏铺满直播窗（黑底），视觉上等价于 mpv 直出画面。
///
/// ## 为什么必须「运行时持续」而不是静态 CSS
/// 央视系页面是 `document.write` + 跨域脚本动态生成 DOM，播放器容器、控制条、
/// 广告都是**页面脚本执行到一半才插入**的。静态注入 CSS 在 document 解析早期
/// 只覆盖到初始骨架，后插入的节点不受影响。所以用 MutationObserver 监听
/// `subtree` 变更 + 定时器兜底，新节点一出现就立刻净化。
///
/// ## 净化策略（保守，宁漏勿伤视频）
/// 1. 全站黑底 + 隐藏滚动条（直播窗是纯画面容器，不该有白边/滚动条）。
/// 2. 强制 <video> 及其直接容器铺满：video 用 `object-fit:contain` 保持比例、
///    黑边不露网页，容器去掉内外边距、背景透明。
/// 3. 隐藏「确定非视频」的高频选择器：导航、页头、页脚、侧栏、搜索框、
///    评论、推荐、下载浮层、观看历史、广告 iframe（针对 tv.cctv.com /
///    yangshipin.cn / m-live.cctvnews 实际观察到的 DOM 结构）。
/// 4. MutationObserver + setInterval 双保险：新插入的节点若命中隐藏规则立即藏。
/// 5. 兜底：把 body 背景设为黑，任何漏网的白色容器也会被 body 黑底衬黑。
pub const PURE_VIDEO_INJECT: &str = r#"
(function () {
  if (window.__pureVideo) return;   // 幂等：同一文档只跑一份
  window.__pureVideo = true;

  // ────────────────────────────────────────────────────────────────────
  // 策略：**不猜类名**，只保留 <video> 的祖先链，其余兄弟节点全部隐藏。
  //
  // 为什么放弃「选择器黑名单 + 黑色遮罩」（v2.10.46 及以前的方案，已证无效）：
  //   · 央视/央视频/地方台的导航栏、二维码浮层、侧栏、自定义控制条的类名
  //     各不相同且随版本变（截图实证：`.nav`/`.header` 一个都没命中）；
  //   · 黑色遮罩压不住页面自己的高 z-index 合成层（导航照样穿透露出来）。
  // 而「祖先链」是纯结构关系，与类名无关 —— video 在哪，就只留到 video 的
  // 那条路径，路径外的可见节点（导航/页头/页脚/侧栏/广告/二维码/控制条）
  // 一律 display:none。换任何站点都不用改代码。
  //
  // 安全性：display:none 只影响渲染，节点仍在 DOM —— 页面脚本照旧能读写
  // （播放器若依赖这些节点做逻辑不会崩）；video 及其祖先链保持可见，
  // 所以 MSE/CCA 解码管线不受影响。
  // ────────────────────────────────────────────────────────────────────

  // ── 1) 全局 CSS：禁滚动 + 藏滚动条 + 藏原生控件伪元素 ──────────────
  function injectCss() {
    if (document.getElementById('__pure_css')) return;
    var st = document.createElement('style');
    st.id = '__pure_css';
    st.textContent =
      'html,body{overflow:hidden!important;margin:0!important;padding:0!important;' +
      'background:#000!important;height:100%!important;width:100vw!important;' +
      // ★ 站点常给 body 设 min-width（央视是 ~1200px）→ 视口只有 742 时文档被撑宽，
      //   横向内容溢出被裁。必须把 min-width/min-height 归零。
      'min-width:0!important;min-height:0!important;max-width:100vw!important;' +
      'max-height:100vh!important;position:relative!important;}' +
      '*{scrollbar-width:none!important;-ms-overflow-style:none!important;}' +
      '::-webkit-scrollbar{width:0!important;height:0!important;display:none!important;}' +
      'video::-webkit-media-controls,'
      + 'video::-webkit-media-controls-enclosure,'
      + 'video::-webkit-media-controls-panel,'
      + 'video::-webkit-media-controls-play-button,'
      + 'video::-webkit-media-controls-timeline,'
      + 'video::-webkit-media-controls-current-time-display,'
      + 'video::-webkit-media-controls-time-remaining-display,'
      + 'video::-webkit-media-controls-volume-slider,'
      + 'video::-webkit-media-controls-fullscreen-button'
      + '{display:none!important;-webkit-appearance:none!important;opacity:0!important;}';
    try { (document.head || document.documentElement).appendChild(st); } catch (e) {}
  }

  // ── 2) 选主视频：面积最大 / 已解出帧 / 正在播的优先 ────────────────
  function pickVideo() {
    var vs = document.querySelectorAll('video');
    var best = null, bestScore = -1;
    for (var i = 0; i < vs.length; i++) {
      var v = vs[i];
      var score = (v.videoWidth || 1) * (v.videoHeight || 1);
      if (v.readyState >= 2) score *= 10;   // 已解出元数据/帧
      if (v.paused === false) score *= 3;   // 正在播
      if (score > bestScore) { bestScore = score; best = v; }
    }
    return best;
  }

  // ── 3) 干掉原生控件 ───────────────────────────────────────────────
  // ★★ 关键 bug（旧版）：`setAttribute('controls','false')` 完全无效 ——
  //    HTML 布尔属性只要**存在**就算 true，controls="false" 照样显示控件。
  //    必须 removeAttribute；再用 defineProperty 劫持 property，让页面脚本
  //    之后再 `video.controls = true` 也设不上。
  function killControls(v) {
    try { if (v.hasAttribute('controls')) v.removeAttribute('controls'); } catch (e) {}
    try {
      v.setAttribute('disablepictureinpicture', '');
      v.setAttribute('controlslist', 'nodownload nofullscreen noremoteplayback');
    } catch (e) {}
    if (v.__ctlHijacked) return;
    try {
      Object.defineProperty(v, 'controls', {
        configurable: true,
        get: function () { return false; },
        set: function () { /* 吞掉页面的赋值 */ }
      });
      v.__ctlHijacked = true;
    } catch (e) {}
  }

  // ── 4) 核心：隔离出 video 祖先链，其余兄弟全隐藏 ───────────────────
  function isolate(v) {
    // 祖先链：video → 父 → … → body（不含 html）
    var chain = [], n = v;
    while (n && n !== document.documentElement) { chain.push(n); n = n.parentElement; }
    if (chain.length < 2) return;   // video 直接挂在 html 下，无可隔离

    // 4a) 每个祖先节点上：只留链上的那个子节点，其余兄弟全藏
    for (var i = 1; i < chain.length; i++) {
      var up = chain[i], keep = chain[i - 1], kids = up.children;
      for (var j = kids.length - 1; j >= 0; j--) {
        var k = kids[j];
        if (k === keep) continue;
        var tag = k.tagName ? k.tagName.toUpperCase() : '';
        // script/style/link 留着（藏了会断脚本/样式），它们本来不占位
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LINK' || tag === 'META') continue;
        if (k.style && k.style.getPropertyValue('display') === 'none') continue;  // 已藏，省一次写
        try { k.style.setProperty('display', 'none', 'important'); } catch (e) {}
        try { k.setAttribute('aria-hidden', 'true'); } catch (e) {}
      }
    }

    // 4b) 链上各级容器：去边距/边框/白底/溢出裁剪，撑满
    for (var m = 0; m < chain.length; m++) {
      var el = chain[m];
      if (!el.style) continue;
      el.style.setProperty('margin', '0', 'important');
      el.style.setProperty('padding', '0', 'important');
      el.style.setProperty('border', '0', 'important');
      el.style.setProperty('background', '#000', 'important');
      el.style.setProperty('overflow', 'hidden', 'important');
      el.style.setProperty('max-width', 'none', 'important');
      el.style.setProperty('max-height', 'none', 'important');
      el.style.setProperty('box-shadow', 'none', 'important');
      el.style.setProperty('min-width', '0', 'important');
      el.style.setProperty('min-height', '0', 'important');
      if (m === 0) {
        // ★ video 自身：只撑尺寸 + contain，**不改 position**
        //   （强改 fixed 会把它拎出解码容器，破坏 CCA/MSE → 永久黑屏）
        el.style.setProperty('width', '100%', 'important');
        el.style.setProperty('height', '100%', 'important');
        el.style.setProperty('object-fit', 'contain', 'important');
        try { var p = el.play(); if (p && p.catch) p.catch(function () {}); } catch (e) {}
      } else {
        el.style.setProperty('width', '100%', 'important');
        el.style.setProperty('height', '100%', 'important');
      }
    }

    // 4c) 最外层可见容器（body 的那个子节点）钉死视口
    var outer = chain[chain.length - 2];
    if (outer && outer.style && outer !== document.body) {
      outer.style.setProperty('position', 'fixed', 'important');
      outer.style.setProperty('left', '0', 'important');
      outer.style.setProperty('top', '0', 'important');
      outer.style.setProperty('right', 'auto', 'important');
      outer.style.setProperty('bottom', 'auto', 'important');
      outer.style.setProperty('width', '100vw', 'important');
      outer.style.setProperty('height', '100vh', 'important');
      outer.style.setProperty('z-index', '900000', 'important');
      outer.style.setProperty('transform', 'none', 'important');
    }
  }

  // ── 5) 揭幕控制（治「加载期露网页」）──────────────────────────────
  // ★ 用户实测的两个毛病都出在这里：
  //   ① 页面加载的 1~4 秒里 <video> 还不存在，旧 sweep 直接 return
  //      → **整站赤裸**，导航栏 / 二维码 / 「正在播放」全露（截图实证）；
  //   ② 央视的 <video> 从出现到能出画还要几秒，这期间同样没有任何遮挡。
  //
  // 解决：文档创建的**第一刻**就用 `filter:brightness(0)` 把整页压黑。
  //   ★ 为什么用 filter 而不是黑色遮罩层：遮罩靠 z-index 生效，实测压不住
  //     页面的高 z 合成层（导航直接透出来）；而 filter 作用在 <html> 上，
  //     **不受任何 z-index 影响**，连 iframe 一起黑掉。
  //   ★ 附带好处：CSS filter 天然可过渡 → 揭幕时就是一段淡入动画。
  var revealed = false;

  function blackout() {
    var de = document.documentElement;
    if (revealed) return;
    if (!de || !de.style) {
      // ★ documentElement 还没建好 → 10ms 后重试。
      //   这一段是「WebView2 画布默认白底」唯一能压缩的地方：越早把过滤层盖上去，
      //   白屏就越短（实测等 DOMContentLoaded 要 800ms+，10ms 重试能压到一两帧）。
      if (!blackout.__t) {
        blackout.__t = setTimeout(function () { blackout.__t = 0; blackout(); }, 10);
      }
      return;
    }
    de.style.setProperty('background', '#000', 'important');
    de.style.setProperty('filter', 'brightness(0)', 'important');
    if (de.__pureTrans !== '1') {   // 过渡只设一次
      de.style.setProperty('transition', 'filter .38s ease-out', 'important');
      de.__pureTrans = '1';
    }
  }

  function reveal() {
    if (revealed) return;
    revealed = true;
    var de = document.documentElement;
    // 0.38s 内 brightness 0→1：黑幕淡出，画面淡入（ease-out 收尾柔和）
    if (de && de.style) de.style.setProperty('filter', 'brightness(1)', 'important');
  }

  /// 视频「真能看了」：已解出当前帧、有画面尺寸、没暂停。
  function playing(v) {
    if (!v) return false;
    if (v.readyState < 2) return false;   // HAVE_CURRENT_DATA 以下 = 还没有可用帧
    if (!v.videoWidth) return false;      // 帧尺寸为 0 = 还没解出画面
    if (v.paused) return false;
    return true;
  }

  function sweep() {
    try {
      injectCss();
      blackout();                 // 未揭幕前，每轮都确保是黑的
      var de = document.documentElement;
      if (de) {
        de.style.setProperty('overflow', 'hidden', 'important');
        de.style.setProperty('min-width', '0', 'important');
      }
      if (document.body) {
        document.body.style.setProperty('background', '#000', 'important');
        document.body.style.setProperty('overflow', 'hidden', 'important');
        document.body.style.setProperty('margin', '0', 'important');
        document.body.style.setProperty('padding', '0', 'important');
        // ★ 站点 min-width（央视 ~1200px）会把文档撑宽 → 与视口同宽
        document.body.style.setProperty('min-width', '0', 'important');
        document.body.style.setProperty('max-width', '100vw', 'important');
        document.body.style.setProperty('width', '100vw', 'important');
      }
      var v = pickVideo();
      // ★★ 这里是「看不到网页」的最后一道闸：**视频没真出画就永远保持全黑**。
      //   早期版本为了「别让用户面对永久黑屏」加了 15s 超时揭幕 —— 实测证明那是
      //   **错的方向**：央视频这类站点拉流要 5~20s，超时一到就把导航栏/二维码/
      //   「正在播放」全露出来（用户截图实证），比黑屏难看得多。
      //   现在改为：这里只管黑；「一直不出画怎么办」交给 Rust 侧
      //   `wait_live_playing` 判定 —— 不出画就关窗换下一条线路，全线失败弹窗择路。
      if (!v) return;             // 还没有 video（没起播 / 在跨域 iframe 里）
      v.muted = false;            // 保证有声
      killControls(v);
      if (!playing(v)) return;    // 有 video 但还没出画 → 继续黑着
      // ★ 顺序很重要：**先把布局隔离到最终形态，再揭幕**。
      //   反过来会「先露出原布局、紧接着重排」→ 用户看到画面跳一下。
      isolate(v);
      if (!revealed) {
        requestAnimationFrame(function () { setTimeout(reveal, 60); });
      }
    } catch (e) { /* 净化失败不能影响播放 */ }
  }

  function start() {
    blackout();
    sweep();
    // 页面脚本会持续插节点（document.write / 路由重渲染）→ 变更即重扫
    try {
      new MutationObserver(function () { sweep(); })
        .observe(document.documentElement, { childList: true, subtree: true });
    } catch (e) {}
    // 定时兜底：有些站点改的是已存在节点的 class/属性，不会触发 childList
    setInterval(sweep, 400);
  }

  // ★★ 立刻压黑 —— 不等 DOMContentLoaded（那时导航栏早就画出来了）。
  //    本脚本在「文档已创建、页面脚本尚未执行」时注入，documentElement 一定存在。
  blackout();

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
    // document.write 会让文档反复重建，DOMContentLoaded 可能不触发 → 再兜一层
    setTimeout(start, 800);
  } else {
    start();
  }
})();
"#;

/// Edge/WebView2 启动参数（★ 缺任何一个都可能让播放器起不来，见模块注释坑②）。
///
/// ★★ **主窗口与嗅探窗必须用完全相同的这串参数**。原因：WebView2 的
///   `additional_browser_args` 是**环境级**（`CoreWebView2EnvironmentOptions`），
///   同一进程内若第二个 WebView2 用**不同**的环境选项去建，会直接报
///   `HRESULT(0x8007139F) ERROR_INVALID_STATE`（"组或资源的状态不是执行请求
///   操作的正确状态"）—— 实测：主窗口已占环境后，嗅探窗建窗必失败。
///   参数一致时 WebView2 会复用同一环境，第二个窗口正常创建。
///   （`user_agent` 是 **webview 级**（`SetUserAgent`），不参与环境比对，
///     故两个窗口可以各自设 UA。）
pub const BROWSER_ARGS: &str = "--disable-gpu \
     --no-sandbox \
     --disable-extensions \
     --no-first-run \
     --no-default-browser-check \
     --autoplay-policy=no-user-gesture-required \
     --disable-background-timer-throttling \
     --disable-backgrounding-occluded-windows \
     --disable-renderer-backgrounding \
     --disable-features=msEdgeSidebarV2,msEdgeShoppingAssistant";

/// 统一的浏览器参数（主窗口 / 嗅探窗共用）。
///
/// `extra` 里的附加参数（当前只有自动化用的 `--remote-debugging-port`）
/// **必须同时加到两个窗口**，否则环境选项不一致 → 建窗报 0x8007139F。
pub fn browser_args(extra: &str) -> String {    let mut s = String::from(BROWSER_ARGS);
    if !extra.trim().is_empty() {
        s.push(' ');
        s.push_str(extra.trim());
    }
    s
}

/// 嗅探结果：真实流地址 + 浏览器实际发出的请求头。
///
/// ★ 复用 `official_web::WebStream`（同一个类型），这样两级嗅探（内嵌 → CDP）
///   的结果可以互换，主流程不必关心是哪一级抓到的。
pub use crate::official_web::WebStream;

/// 判定一个 URL 是否是「可交给 mpv 的媒体流」。
///
/// 口径与 `official_web::is_media_stream` 一致：**路径本体**要有 `.m3u8` /
/// `.flv` / `.ts` 后缀。`m3u8.cec-tv.com.cn/?url=...m3u8` 这种中转口返回的是
/// HTML 播放页，不能当流。
pub fn is_media_stream(url: &str) -> bool {
    let lower = url.to_ascii_lowercase();
    let path = lower.split(['?', '#']).next().unwrap_or("");
    path.ends_with(".m3u8") || path.ends_with(".flv") || path.ends_with(".ts")
}

/// 全局投递通道：探针（WebView2 UI 线程）→ 嗅探等待者（后台播放线程）。
///
/// 每次嗅探换一对新的通道，旧通道的发送端自然失效 → 不会有陈旧命中串台。
struct Dispatcher {
    tx: Mutex<Option<Sender<WebStream>>>,
}

impl Dispatcher {
    fn global() -> &'static Dispatcher {
        static D: std::sync::OnceLock<Dispatcher> = std::sync::OnceLock::new();
        D.get_or_init(|| Dispatcher { tx: Mutex::new(None) })
    }

    /// 装入本次嗅探的发送端，返回接收端。
    fn arm(&self) -> Receiver<WebStream> {
        let (tx, rx) = std::sync::mpsc::channel();
        *self.tx.lock().unwrap() = Some(tx);
        rx
    }

    /// 投递命中（探针回调里调用）。
    fn deliver(&self, stream: WebStream) {
        let tx = self.tx.lock().unwrap().clone();
        if let Some(tx) = tx {
            let _ = tx.send(stream);
        }
    }
}

/// 官网源「可见直播窗」管理：WebView2 窗口落在播放区，页面自带 `<video>` 直接播。
///
/// ## 为什么这条路线比「嗅探 → 交 mpv」更优（2026-10-26 定调）
///
/// 官网源 850/904 频道只有网页地址，早先是「开离屏窗嗅探出 m3u8 → 交给 mpv」。
/// 但网页线路有三座 mpv 迈不过去的山：
///   · **CDRM 私有加扰**：央视系 m3u8 是私加密，mpv 抓到的流解码即花屏/黑屏；
///   · **证书链不完整**：境外直播 CDN 证书无效，mpv 必须 `--tls-verify=no`；
///   · **CORS/Referer 校验**：缺 Referer 直接 403，mpv 只能「猜」头。
/// 而页面自己的 `<video>` 由浏览器原生解码、原生处理这三关 —— 直接让 WebView2
/// 窗口落在播放区持续播，**绕开所有三座山**，还能拿到声音（`--mute-audio` 已删）。
///
/// ## 与「离屏嗅探窗」的关键差异
///
/// | | 离屏嗅探窗（旧） | 可见直播窗（新） |
/// |---|---|---|
/// | 窗口位置 | -32000,-32000 屏幕外 | 播放区（前台可见） |
/// | 命中后 | 抓 m3u8 关窗交 mpv | **不关窗**，页面继续播 |
/// | 音频 | 静音（无关紧要） | 有声（用户要） |
/// | 承载方式 | 独立顶层窗 + move_offscreen | owned window + 播放区坐标 |
///
/// ## 生命周期
///
/// - `open_live_window`：建窗（owned window，owner=主窗口）→ 导航页面，**不关窗**。
/// - `close_live_window`：换台/停止时回收（换台前必须先关，否则多个渲染进程常驻）。
/// - 直播窗复用主窗口的**同一套环境参数**（`browser_args`），否则建窗报 0x8007139F。
pub static LIVE_WINDOW: Mutex<Option<tauri::WebviewWindow>> = Mutex::new(None);

/// 建一个落在播放区的「可见直播窗」，导航到官网页面后**不关窗**（页面自带
/// `<video>` 直接播）。必须在主线程调用（Tauri 建窗要求）。
///
/// * `app` —— AppHandle。
/// * `page_url` —— 官网页面（如 `https://tv.cctv.com/live/cctv1/`）。
/// * `x, y, w, h` —— 播放区**屏幕坐标**（物理像素，由 `VideoHost::place_over_client`
///   换算好）；窗口直接用这组坐标定位（覆盖整个播放区）。
///
/// 返回是否建窗成功。
pub fn open_live_window(
    app: &tauri::AppHandle,
    page_url: &str,
    x: i32,
    y: i32,
    w: i32,
    h: i32,
) -> bool {
    use tauri::{WebviewUrl, WebviewWindowBuilder};

    // 回收上一个直播窗（换台前必须先关，见模块注释生命周期）。
    close_live_window();

    static COUNTER: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
    let n = COUNTER.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let label = format!("official-live-{}-{n}", std::process::id());

    // ★★ Tauri 的 `.position()` / `.inner_size()` 收的是**逻辑像素**，而
    //   `stage_rect` 是**物理像素**。125% 缩放下直接传物理值 → 建窗位置被再放大
    //   1.25 倍：实测播放区 (702,164) 945x753 的窗，出生在 (878,205) 1181x941，
    //   要等 SetWindowPos 才纠正 —— 这正是「空白弹窗一闪」的物理来源。
    //   先除 scale_factor，让第一帧就落在正确位置。
    let sf = {
        use tauri::Manager;   // get_webview_window 由 Manager trait 提供
        app.get_webview_window("main")
            .and_then(|w| w.scale_factor().ok())
            .filter(|s| *s > 0.1)
            .unwrap_or(1.0)
    };

    let builder = WebviewWindowBuilder::new(
        app,
        &label,
        WebviewUrl::External(page_url.parse().unwrap_or_else(|_| "https://example.invalid/".parse().unwrap())),
    )
    .title("官网源直播")
    // 窗口直接落在播放区（屏幕坐标，换算成逻辑像素），覆盖整个播放区。
    .position(x as f64 / sf, y as f64 / sf)
    .inner_size((w.max(16) as f64) / sf, (h.max(16) as f64) / sf)
    .skip_taskbar(true)
    .decorations(false)
    .focusable(false)
    .resizable(false)
    // ★★ 建窗时先**不可见** —— 治「一点频道，屏幕左上角闪一个空白弹窗」。
    //   根因：Tauri 的 `.position()` 在 Windows 实测不生效（见 move_offscreen 注释），
    //   窗口建出来先落在系统默认位置（屏幕左上 32,32），几十毫秒后才被
    //   SetWindowPos 挪进播放区 —— 中间那几帧就是用户看到的「空白弹窗一闪而过」。
    //   改成「不可见建窗 → 摆好位置 → 0 透明度显示 → 淡入」，错位的那一帧永远看不到。
    //   ★ 与「坑①」的区别：坑①是**长时间** `SetIsVisible(false)` 让 Chromium 判定
    //     页面不可见、渲染挂起；这里不可见只持续 build→show 的几十毫秒，随即显示，
    //     不影响播放器起播（实测验证）。
    .visible(false)
    // ★★ 窗口/WebView2 的**默认底色是白的**。页面 DOM 还没建立的那 0.5~1s 里，
    //   注入脚本的 `filter:brightness(0)` 无从生效（还没有元素可作用），
    //   于是先露出一块纯白 —— 实测 t=1.54~2.24s 非黑占比 100%（就是这块白）。
    //   把 WebView2 背景色设成黑，这块白就消失了。
    .background_color(tauri::window::Color(0, 0, 0, 255))
    .user_agent(UA)
    // ★★ 网页净化：官网直播页会带导航栏/页头页脚/播放器控制条/文字/广告，
    //   直接露出来「原生网页的样子」就背离了「纯视频、和 mpv 一样」的目标。
    //   这里注入一套常驻初始化脚本，持续把非视频元素隐藏、把 <video> 全屏铺满。
    //   （页面脚本是运行期 document.write + 跨域脚本动态生成的 DOM，光靠静态
    //   CSS 抓不到后生成的播放器，必须用 MutationObserver + 定时器持续净化。）
    .initialization_script(PURE_VIDEO_INJECT)
    // ★ 与主窗口完全相同（否则 WebView2 报 0x8007139F 建窗失败）。
    .additional_browser_args(&browser_args(&debug_port_args()));

    let win = match builder.build() {
        Ok(w) => w,
        Err(e) => {
            eprintln!("[embed] 直播窗创建失败: {e}");
            return false;
        }
    };

    // ★★ 让 WebView2 在「页面还没渲染出内容」时就是**黑底**。
    //   `.background_color()` 改的是**窗口**背景，WebView2 **页面自身**的默认底色
    //   仍然是白的 —— 实测窗口刚变不透明的那 0.5~1s 会闪一块 97% 白屏
    //   （DOM 还没建，注入脚本的 filter 无从下手）。这里直接改 controller 的
    //   DefaultBackgroundColor，从渲染源头就是黑的。
    if let Err(e) = win.with_webview(|pw| {
        use webview2_com::Microsoft::Web::WebView2::Win32::{
            ICoreWebView2Controller2, COREWEBVIEW2_COLOR,
        };
        use windows::core::Interface;
        unsafe {
            let controller = pw.controller();
            // ★ SetDefaultBackgroundColor 只在 ICoreWebView2Controller2 上，
            //   基础接口没有这个方法 → 必须 cast。
            if let Ok(c2) = controller.cast::<ICoreWebView2Controller2>() {
                let c = COREWEBVIEW2_COLOR { A: 255, R: 0, G: 0, B: 0 };
                let _ = c2.SetDefaultBackgroundColor(c);
            }
        }
    }) {
        eprintln!("[embed] 设置 WebView2 默认黑底失败: {e}");
    }

    // ★ 建窗后**再挪一次**到播放区：Tauri 的 .position() 实测不生效（见 move_offscreen 注释），
    //   必须拿到 HWND 后用 SetWindowPos 摆正。
    if let Ok(win_hwnd) = win.hwnd() {
        let raw = win_hwnd.0 as usize as u64;
        // 登记本窗 HWND（判定/淡入线程据此校验归属）
        LIVE_HWND.store(raw, std::sync::atomic::Ordering::SeqCst);
        // ★★ 三步顺序不能变（每一步都是实测踩出来的）：
        //   ① 先摆到播放区 —— 窗口必须**在屏幕上的正确位置**，否则 Chromium 判定
        //      页面不可见 → 渲染挂起、播放器永不起播。
        //      （实测：把窗口挪到 -32000 屏幕外，CCTV-1 的 video 一直 readyState=0、
        //        尺寸 0x0 —— 正是「坑①」的重演。）
        place_live_window(raw, x, y, w, h);
        //   ② 设 owner（保证恒在主窗口 WebView2 渲染子窗之上）
        apply_live_owner(raw);
        //   ③ **owner 设完再设透明** —— 实测 `SetWindowLongPtrW(GWLP_HWNDPARENT)`
        //      会把 WS_EX_LAYERED 冲掉；先设后设的顺序反了，窗口就以不透明状态
        //      露出 WebView2 白底。设完之后窗口「在播，但在屏幕上完全透明」，
        //      用户看到的是主窗口自己的「正在加载官网源线路…」占位层。
        set_layered_alpha(raw, 0);
        let _ = win.show();
    } else {
        // 拿不到 HWND 也要显示，否则窗口永远看不见。
        let _ = win.show();
    }

    *LIVE_WINDOW.lock().unwrap() = Some(win);
    true
}

/// 截取窗口区域，返回「非黑像素占比」（0.0~1.0）。取不到返回 `None`。
///
/// ★ 为什么用**截屏**判出画，而不是让页面 JS 通知 Rust：
///   IPC 要走 `__TAURI_INTERNALS__`，远程页面（tv.cctv.com / yangshipin.cn）
///   受 Tauri 的远程域 IPC 权限限制，不可靠；而截屏**直接反映用户看到的画面**，
///   零权限问题。同理走不通「页面 title / location.hash」那类旁路（SPA 路由会抢）。
///
/// 实测判据（`tools/monitor_live_transition.py` 同口径）：
///   · 加载期：整页被 `filter:brightness(0)` 压黑 → 非黑 **0.7%**
///   · 出画后：真实电视画面 → 非黑 **47~65%**
fn live_nonblack_ratio(hwnd: u64) -> Option<f32> {
    if hwnd == 0 {
        return None;
    }
    #[cfg(windows)]
    {
        use windows::Win32::Foundation::{HWND, RECT};
        use windows::Win32::Graphics::Gdi::{
            BitBlt, CreateCompatibleBitmap, CreateCompatibleDC, DeleteDC, DeleteObject, GetDC,
            GetDIBits, ReleaseDC, SelectObject, BITMAPINFO, BI_RGB, DIB_RGB_COLORS, SRCCOPY,
        };
        use windows::Win32::UI::WindowsAndMessaging::GetWindowRect;
        unsafe {
            let h = HWND(hwnd as *mut std::ffi::c_void);
            let mut r = RECT::default();
            if GetWindowRect(h, &mut r).is_err() {
                return None;
            }
            let w = r.right - r.left;
            let ht = r.bottom - r.top;
            if w < 32 || ht < 32 {
                return None;
            }

            let sdc = GetDC(None);
            if sdc.is_invalid() {
                return None;
            }
            let mem = CreateCompatibleDC(Some(sdc));
            let bmp = CreateCompatibleBitmap(sdc, w, ht);
            let old = SelectObject(mem, bmp.into());

            let mut out = None;
            if BitBlt(mem, 0, 0, w, ht, Some(sdc), r.left, r.top, SRCCOPY).is_ok() {
                let mut bi = BITMAPINFO::default();
                bi.bmiHeader.biSize = std::mem::size_of_val(&bi.bmiHeader) as u32;
                bi.bmiHeader.biWidth = w;
                bi.bmiHeader.biHeight = -ht;      // 负值 = 自上而下
                bi.bmiHeader.biPlanes = 1;
                bi.bmiHeader.biBitCount = 32;
                bi.bmiHeader.biCompression = BI_RGB.0;
                let total = (w * ht) as usize;
                let mut buf = vec![0u8; total * 4];
                let lines = GetDIBits(
                    mem,
                    bmp,
                    0,
                    ht as u32,
                    Some(buf.as_mut_ptr() as *mut _),
                    &mut bi,
                    DIB_RGB_COLORS,
                );
                if lines > 0 {
                    let step = std::cmp::max(1, total / 4000);
                    let mut nz = 0usize;
                    let mut cnt = 0usize;
                    let mut i = 0usize;
                    while i < total {
                        let o = i * 4;
                        // BGRA：三通道和 > 30 视为非黑（与监视脚本同口径）
                        if buf[o] as u32 + buf[o + 1] as u32 + buf[o + 2] as u32 > 30 {
                            nz += 1;
                        }
                        cnt += 1;
                        i += step;
                    }
                    out = Some(nz as f32 / cnt.max(1) as f32);
                }
            }

            SelectObject(mem, old);
            let _ = DeleteObject(bmp.into());
            let _ = DeleteDC(mem);
            ReleaseDC(None, sdc);
            out
        }
    }
    #[cfg(not(windows))]
    {
        let _ = hwnd;
        None
    }
}

/// 解析 `ExecuteScript` 返回的 JSON 字符串：`"\"4,1280,0\""` → `(4, 1280, false)`
fn parse_video_state(raw: &str) -> Option<(u32, u32, bool)> {
    let s = raw.trim().trim_matches('"').trim();
    if s.is_empty() || s == "-1" {
        return None;
    }
    let mut it = s.split(',');
    let rs = it.next()?.trim().parse::<u32>().ok()?;
    let w = it.next()?.trim().parse::<u32>().ok()?;
    let p = it.next()?.trim();
    Some((rs, w, p == "1"))
}

/// 问直播页里 `<video>` 的真实状态：`(readyState, videoWidth, paused)`。
///
/// ## 为什么必须用 ExecuteScript，而不是截屏
/// 截屏判定要求窗口**不透明**，但窗口一旦不透明，页面在 `filter` 生效前的那
/// 0.5~1s 就会闪白（实测非黑 97.5%）；而让窗口一直透明，截屏抓到的又是下面的
/// 占位层 —— 两难。`ExecuteScript` 直接问页面要状态，**与窗口透明度、遮挡、
/// DPI 全都无关**，是 WebView2 的官方接口。
///
/// ## 为什么必须投到主线程
/// `with_webview` 只在主线程才是同步执行的；从后台线程直接调会失效。
/// 这里用 `run_on_main_thread` 派发，再用 mpsc 等回调结果（模式同建窗）。
pub fn query_video_state(
    app: &tauri::AppHandle,
    win: tauri::WebviewWindow,
) -> Option<(u32, u32, bool)> {
    let (tx, rx) = std::sync::mpsc::channel::<Option<(u32, u32, bool)>>();
    let tx_main = tx.clone();
    let dispatched = app
        .run_on_main_thread(move || {
            use webview2_com::ExecuteScriptCompletedHandler;
            use windows::core::HSTRING;
            let tx_h = tx_main.clone();
            let r = win.with_webview(move |pw| unsafe {
                let webview = match pw.controller().CoreWebView2() {
                    Ok(w) => w,
                    Err(_) => {
                        let _ = tx_h.send(None);
                        return;
                    }
                };
                // 只问三个数：readyState / 帧宽 / 是否暂停
                let js = HSTRING::from(
                    "(function(){var v=document.querySelector('video');\
                     if(!v)return '-1';\
                     return v.readyState+','+(v.videoWidth||0)+','+(v.paused?1:0);})()",
                );
                let tx_cb = tx_h.clone();
                // ★ 回调签名：webview2-com 的 completed_callback 已把 PCWSTR 转成 String
                //   （`ClosureArg for PCWSTR => Output = String`），所以第二个参数直接收 String。
                let handler = ExecuteScriptCompletedHandler::create(Box::new(
                    move |_hr, res: String| {
                        // 结果经 JSON 编码回来，形如 "\"4,1280,0\""，由 parse_video_state 去壳
                        let _ = tx_cb.send(parse_video_state(&res));
                        Ok(())
                    },
                ));
                if webview.ExecuteScript(&js, &handler).is_err() {
                    let _ = tx_h.send(None);
                }
            });
            if r.is_err() {
                let _ = tx_main.send(None);
            }
        })
        .is_ok();
    if !dispatched {
        return None;
    }
    rx.recv_timeout(Duration::from_millis(2000)).ok().flatten()
}

/// 这条线路「到底有没有出画」的判定结果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LiveVerdict {
    /// 确实出画了
    Playing,
    /// 还没出画（超时）
    NotPlaying,
    /// 窗口已被换台/关闭（不是本线程该管的事了）
    Superseded,
}

/// 等直播窗真正出画（阻塞，只能在后台线程调用）。
///
/// * `grace_ms` —— 起播宽限。页面要加载 + 播放器起播，这段里必然没帧，不查询。
/// * `total_ms` —— 宽限之后最多再等多久。
///
/// 判据：`readyState >= 2`（有当前帧）且 `videoWidth > 0`（帧尺寸非零）。
/// 连续 2 次满足才算，避开瞬时抖动。
pub fn wait_live_playing(
    app: &tauri::AppHandle,
    hwnd: u64,
    grace_ms: u64,
    total_ms: u64,
) -> LiveVerdict {
    if hwnd == 0 {
        return LiveVerdict::NotPlaying;
    }
    std::thread::sleep(Duration::from_millis(grace_ms));
    let t0 = Instant::now();
    let mut hits = 0u32;
    while (t0.elapsed().as_millis() as u64) < total_ms {
        // 换台/关闭 → 立即收手
        if LIVE_HWND.load(std::sync::atomic::Ordering::SeqCst) != hwnd {
            return LiveVerdict::Superseded;
        }
        let win = LIVE_WINDOW
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .as_ref()
            .cloned();
        if let Some(w) = win {
            if let Some((rs, vw, _paused)) = query_video_state(app, w) {
                if rs >= 2 && vw > 0 {
                    hits += 1;
                    if hits >= 2 {
                        return LiveVerdict::Playing;
                    }
                } else {
                    hits = 0;
                }
            }
        }
        std::thread::sleep(Duration::from_millis(900));
    }
    if LIVE_HWND.load(std::sync::atomic::Ordering::SeqCst) != hwnd {
        return LiveVerdict::Superseded;
    }
    LiveVerdict::NotPlaying
}

/// 出画后把直播窗淡入（窗口此时已在播放区，只是 alpha=0，用户看不见）。
///
/// ★ 万一 layered 在某台机器上不生效，最坏结果是「画面直接出现」（没有淡入），
///   而不是露网页 —— 因为窗口内容此刻已经是纯视频了。
pub fn show_live_window_at(hwnd: u64, _x: i32, _y: i32, _w: i32, _h: i32, fade_ms: u64) {
    if hwnd == 0 {
        return;
    }
    // 再确认一次 layered 还在（保险：某些系统事件会清掉 EXSTYLE）
    set_layered_alpha(hwnd, 0);
    if fade_ms > 0 {
        fade_in_window(hwnd, 0, fade_ms);
    } else {
        set_layered_alpha(hwnd, 255);
    }
}

/// 出画后把直播窗淡入（260ms ease-out）。
pub fn fade_in_live(hwnd: u64, ms: u64) {
    fade_in_window(hwnd, 0, ms);
}

/// 当前直播窗 HWND（0 = 没有）。用于淡入线程逐步校验窗口归属。
///
/// ★ 为什么需要：淡入要跨 380ms + 240ms 在后台线程里分步改透明度。
///   这段时间里用户可能换台（旧窗销毁、新窗建立），若系统把同一个 HWND 值
///   复用给别的窗口，步进线程就会去改**别人**的透明度。每步比对一次即可避免。
pub static LIVE_HWND: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// 给窗口加 `WS_EX_LAYERED` 并设整体不透明度（0 = 全透明，255 = 不透明）。
///
/// 用途：建窗后先设 0，摆好位置再渐变到 255 —— 用户看到的是「柔和淡入」，
/// 而不是「空白窗口一闪」。
///
/// ★ 为什么 layered 不会让 Chromium 判定窗口不可见（与「坑①」无关）：
///   可见性判定看的是 `IsWindowVisible`；layered 的 alpha 只影响**合成输出**，
///   页面照常渲染、播放器照常起播。
fn set_layered_alpha(hwnd: u64, alpha: u8) {
    if hwnd == 0 {
        return;
    }
    #[cfg(windows)]
    {
        use windows::Win32::Foundation::{COLORREF, HWND};
        use windows::Win32::UI::WindowsAndMessaging::{
            GetWindowLongPtrW, SetLayeredWindowAttributes, SetWindowLongPtrW, GWL_EXSTYLE,
            LWA_ALPHA, WS_EX_LAYERED,
        };
        unsafe {
            let h = HWND(hwnd as *mut std::ffi::c_void);
            let ex = GetWindowLongPtrW(h, GWL_EXSTYLE);
            if ex & (WS_EX_LAYERED.0 as isize) == 0 {
                let _ = SetWindowLongPtrW(h, GWL_EXSTYLE, ex | (WS_EX_LAYERED.0 as isize));
            }
            let _ = SetLayeredWindowAttributes(h, COLORREF(0), alpha, LWA_ALPHA);
        }
    }
}

/// 把窗口从当前透明度渐变到 255（淡入），总时长 `ms`。
///
/// * `delay_ms` —— 淡入前的等待。给页面一点时间把 DOM 建起来，让注入脚本的
///   `filter:brightness(0)` 先生效；这段里窗口是全透明的，用户看到的是下面的
///   播放区（纯黑），不会有白色/网页闪过。
///
/// 在**后台线程**步进：建窗发生在主线程，不能在这里 sleep 卡住 UI。
/// `SetLayeredWindowAttributes` 可以从任意线程调用。
///
/// 曲线用 ease-out（`1-(1-t)²`）：起步快、收尾柔和，观感比线性自然。
fn fade_in_window(hwnd: u64, delay_ms: u64, ms: u64) {
    if hwnd == 0 {
        return;
    }
    std::thread::spawn(move || {
        if delay_ms > 0 {
            std::thread::sleep(Duration::from_millis(delay_ms));
        }
        const STEPS: u32 = 22;
        let sleep_ms = (ms / STEPS as u64).max(1);
        for i in 1..=STEPS {
            // ★ 换台时旧窗已销毁 / HWND 可能被系统复用 → 每步校验归属，不是本窗就退出
            if LIVE_HWND.load(std::sync::atomic::Ordering::SeqCst) != hwnd {
                return;
            }
            let t = i as f64 / STEPS as f64;
            let eased = 1.0 - (1.0 - t).powi(2);
            let a = (255.0 * eased).round().clamp(0.0, 255.0) as u8;
            set_layered_alpha(hwnd, a);
            std::thread::sleep(Duration::from_millis(sleep_ms));
        }
        if LIVE_HWND.load(std::sync::atomic::Ordering::SeqCst) == hwnd {
            set_layered_alpha(hwnd, 255);
        }
    });
}

/// 把直播窗 HWND 设为当前进程主窗口的 owned window（`GWLP_HWNDPARENT`），
/// 保证直播窗恒在 owner（主窗口）之上，不被主窗口的 WebView2 渲染子窗盖住。
///
/// owner 从全局 `MAIN_OWNER` 读取（setup 时由 main.rs 写入主窗口 HWND）。
fn apply_live_owner(hwnd: u64) {
    let owner = MAIN_OWNER.load(std::sync::atomic::Ordering::SeqCst);
    if owner == 0 || hwnd == 0 {
        return;
    }
    #[cfg(windows)]
    {
        use windows::Win32::Foundation::HWND;
        use windows::Win32::UI::WindowsAndMessaging::{SetWindowLongPtrW, GWLP_HWNDPARENT};
        unsafe {
            let _ = SetWindowLongPtrW(
                HWND(hwnd as *mut std::ffi::c_void),
                GWLP_HWNDPARENT,
                owner as isize,
            );
        }
    }
}

/// live 窗全局 owner（主窗口 HWND）。由 main.rs 在主窗口建好后写入。
pub static MAIN_OWNER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// 把直播窗摆到指定屏幕坐标（Win32 直调）。
/// 主窗口尺寸/位置变化（含进出全屏）时，把直播窗同步到新的播放区。
///
/// ★★ 少了这一步就会出现「全屏后画面只占中间一小块」：官网源的画面是
///   独立的 WebView2 窗口，它的几何只在建窗那一次按当时的播放区摆过；
///   之后前端 `report_stage_rect` 只移动了 mpv 承载窗，直播窗还停在旧位置。
/// ★ 用 SetWindowPos（不经 layered/dwf），不会影响当前的淡入透明度。
pub fn sync_live_geometry(x: i32, y: i32, w: i32, h: i32) {
    let hwnd = LIVE_HWND.load(std::sync::atomic::Ordering::SeqCst);
    if hwnd == 0 {
        return;
    }
    place_live_window(hwnd, x, y, w, h);
}

fn place_live_window(hwnd: u64, x: i32, y: i32, w: i32, h: i32) {
    if hwnd == 0 {
        return;
    }
    #[cfg(windows)]
    {
        use windows::Win32::Foundation::HWND;
        use windows::Win32::UI::WindowsAndMessaging::{
            SetWindowPos, HWND_TOP, SWP_NOACTIVATE, SWP_SHOWWINDOW,
        };
        unsafe {
            let _ = SetWindowPos(
                HWND(hwnd as *mut std::ffi::c_void),
                Some(HWND_TOP),
                x,
                y,
                w.max(16),
                h.max(16),
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            );
        }
    }
}

/// 关掉当前存活的直播窗（换台/停止时调用，避免渲染进程堆积）。
pub fn close_live_window() {
    // 先清登记，让还在跑的淡入线程立刻收手（避免它在窗口销毁后继续发 Win32 调用）
    LIVE_HWND.store(0, std::sync::atomic::Ordering::SeqCst);
    let win = LIVE_WINDOW
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .take();
    if let Some(win) = win {
        // ★★ 不在调用方线程上 `close()` —— 本函数会被 `play_channel` 这种
        //   **同步命令**调用（主线程），而销毁一个 WebView2 窗口要通知渲染进程
        //   退出，实测可达数百毫秒；卡住主线程就是「切台偶尔无响应」。
        //   放到后台线程关，调用方立即返回。
        //   （代价：换台瞬间可能旧窗与新窗短暂并存，随即旧窗消失。）
        std::thread::spawn(move || {
            let _ = win.close();
        });
    }
}

/// 直播窗重新置顶（主窗口获焦/移动/缩放后，owned 窗口可能被系统重排到其下）。
pub fn raise_live_window() {
    #[cfg(windows)]
    {
        let win = LIVE_WINDOW.lock().unwrap();
        if let Some(w) = win.as_ref() {
            if let Ok(h) = w.hwnd() {
                use windows::Win32::Foundation::HWND;
                use windows::Win32::UI::WindowsAndMessaging::{
                    SetWindowPos, HWND_TOP, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE,
                    SWP_SHOWWINDOW,
                };
                unsafe {
                    let _ = SetWindowPos(
                        HWND(h.0 as *mut std::ffi::c_void),
                        Some(HWND_TOP),
                        0,
                        0,
                        0,
                        0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
                    );
                }
            }
        }
    }
}

/// 裸 COM 探针：挂到指定 webview 上，旁听全部 http(s) 请求。
///
/// 只读 URI 与请求头，**绝不 SetResponse**（= 放行，页面照常播放）。
fn attach_probe(platform: tauri::webview::PlatformWebview) {
    use windows::core::HSTRING;

    let webview: ICoreWebView2 = match unsafe { platform.controller().CoreWebView2() } {
        Ok(w) => w,
        Err(e) => {
            eprintln!("[embed] 拿 ICoreWebView2 失败: {e}");
            return;
        }
    };

    // 全量过滤器：回调里自己判媒体流。WebView2 的过滤 API 只支持前缀匹配，
    // 做不成任意域名联合白名单，所以用 "*" + 回调内判定。
    let filter = HSTRING::from("*");
    if let Err(e) = unsafe {
        webview.AddWebResourceRequestedFilter(
            &filter,
            webview2_com::Microsoft::Web::WebView2::Win32::COREWEBVIEW2_WEB_RESOURCE_CONTEXT_ALL,
        )
    } {
        eprintln!("[embed] AddWebResourceRequestedFilter 失败: {e}");
        return;
    }

    let handler = WebResourceRequestedEventHandler::create(Box::new(
        move |_sender: Option<ICoreWebView2>,
              args: Option<ICoreWebView2WebResourceRequestedEventArgs>| {
            let Some(args) = args else {
                return Ok(());
            };
            let request = match unsafe { args.Request() } {
                Ok(r) => r,
                Err(_) => return Ok(()),
            };
            let mut uri = windows::core::PWSTR::null();
            if let Err(e) = unsafe { request.Uri(&mut uri) } {
                eprintln!("[embed] request.Uri() 失败: {e}");
                return Ok(());
            }
            let uri_str = unsafe { uri.to_string().unwrap_or_default() };

            if !is_media_stream(&uri_str) {
                return Ok(());
            }
            // ★ 命中媒体流：把浏览器**真实发出**的请求头一并取走。
            //   央视 CDN 缺 Referer 会 403，而「猜」出来的 origin 未必等于
            //   浏览器实际发的那份，这里拿到的是原值。
            let headers = read_request_headers(&request);
            eprintln!(
                "[embed][HIT] {} 头={:?}",
                uri_str,
                headers.iter().map(|(k, _)| k).collect::<Vec<_>>()
            );
            Dispatcher::global().deliver(WebStream {
                url: uri_str,
                headers,
            });
            Ok(())
        },
    ));

    let mut token: i64 = 0;
    if let Err(e) = unsafe { webview.add_WebResourceRequested(&handler, &mut token) } {
        eprintln!("[embed] add_WebResourceRequested 失败: {e}");
        return;
    }
    eprintln!("[embed] 旁听已挂载 token={token}");
    // handler 由 WebView2 的 COM 引用计数兜底，泄漏避免提前 drop 致回调失效。
    std::mem::forget(handler);
}

/// 读出请求的真实头部（name 小写化）。
///
/// 走 COM 的 `Headers()` → `GetIterator()` → `MoveNext()`/`GetCurrentHeader()`。
/// 只取播放必需的四个：`referer` / `origin` / `cookie` / `user-agent`。
fn read_request_headers(
    request: &webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2WebResourceRequest,
) -> Vec<(String, String)> {
    const KEEP: [&str; 4] = ["referer", "origin", "cookie", "user-agent"];
    let mut out: Vec<(String, String)> = Vec::new();

    let headers = match unsafe { request.Headers() } {
        Ok(h) => h,
        Err(_) => return out,
    };
    let iter = match unsafe { headers.GetIterator() } {
        Ok(i) => i,
        Err(_) => return out,
    };
    loop {
        let mut has = windows::core::BOOL::default();
        if unsafe { iter.MoveNext(&mut has) }.is_err() || !has.as_bool() {
            break;
        }
        let mut name = windows::core::PWSTR::null();
        let mut value = windows::core::PWSTR::null();
        if unsafe { iter.GetCurrentHeader(&mut name, &mut value) }.is_err() {
            continue;
        }
        let n = unsafe { name.to_string().unwrap_or_default() };
        let v = unsafe { value.to_string().unwrap_or_default() };
        let lower = n.to_ascii_lowercase();
        if KEEP.contains(&lower.as_str()) && !v.is_empty() {
            out.push((lower, v));
        }
    }
    out
}

/// 嗅探一个官网页面，抓真实流地址 + 请求头。
///
/// * `app` —— Tauri AppHandle（用来建嗅探窗）。
/// * `page_url` —— 官网页面（如 `https://tv.cctv.com/live/cctv1/`）。
/// * `timeout` —— 最长等待。
///
/// 失败返回 Err（调用方据此换下一条线路 / 回落本机源）。
///
/// ★ 本函数**阻塞**，只能在后台线程里调。
pub fn sniff_stream(
    app: &tauri::AppHandle,
    page_url: &str,
    timeout: Duration,
) -> Result<WebStream, String> {
    let rx = Dispatcher::global().arm();

    // 建嗅探窗 → 挂探针 → 导航。
    //
    // ★ 三步必须在一个批次里完成，且探针要在导航**之前**挂好，否则会漏掉
    //   页面首个请求（首页 HTML 里的 m3u8 线索往往就在第一批请求里）。
    //   `with_webview` 在主线程调用是同步的，所以这里把整批动作投到主线程。
    let url = page_url.to_string();
    let app2 = app.clone();
    let dispatched = app
        .run_on_main_thread(move || {
            open_and_navigate(&app2, url);
        })
        .is_ok();
    if !dispatched {
        return Err("无法派发嗅探窗导航（主线程忙）".into());
    }

    let deadline = Instant::now() + timeout;
    loop {
        let remain = deadline.saturating_duration_since(Instant::now());
        if remain.is_zero() {
            close_live_sniff_window();
            return Err(format!("官网源网页 {}s 内未抓到媒体流", timeout.as_secs()));
        }
        match rx.recv_timeout(remain.min(Duration::from_millis(400))) {
            Ok(stream) => {
                eprintln!("[embed] 命中 url.len={}", stream.url.len());
                // ★ 命中即收窗：流地址和真实请求头都已拿到，后续 mpv 自己拉流，
                //   不需要页面继续跑（留着只会白占一个渲染进程）。
                close_live_sniff_window();
                return Ok(stream);
            }
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                close_live_sniff_window();
                return Err("官网源网页嗅探通道断开".into());
            }
        }
    }
}

/// 自动化用的调试端口参数（若有）。
///
/// ★ 主窗口与嗅探窗**都要**带上它：环境选项必须逐字一致，否则第二个窗口
///   建不出来（0x8007139F）。未设 `CLEAN_TV_CDP_PORT` 时两窗都不带，一致。
/// 直播窗**不启用**远程调试（返回空）。
///
/// ★★ 实测两种做法都不行，必须彻底关掉：
///   · 与主窗口同一个 `--remote-debugging-port` → 第二个 WebView2 初始化
///     **挂死 5~6 秒**：主线程周期性「阻塞约 6000ms」（Windows 标「未响应」），
///     同时建窗回执 `recv_timeout(5s)` 超时 → 每条线路都被判失败 → 官网源全挂。
///   · 改成「主端口 + 1」→ 建窗**直接失败**（WebView2 起不来）。
///   CDP 只留给主窗口（查界面 DOM 足够），官网源页面用 `diag` 日志排查。
fn debug_port_args() -> String {
    String::new()
}

/// 把窗口硬挪到屏幕外（Win32 直调）。
///
/// ★ 坑（实测）：Tauri 的 `.position(-32000,-32000)` 在 Windows 上**不生效**——
///   嗅探窗最终落在屏幕 (32,32)，整个浮在主界面左上角（用户看得见、点得到）。
///   tao 建窗时写的初始位置被系统忽略了；必须等拿到 HWND 后用
///   `SetWindowPos` 再挪一次。
///
/// ★ 只能挪位置、**不能隐藏**：WebView2 的 `SetIsVisible(false)` 会让 Chromium
///   判定页面不可见 → 渲染挂起 → 播放器永不起播（见模块注释坑①）。
fn move_offscreen(hwnd: u64) {
    if hwnd == 0 {
        return;
    }
    #[cfg(windows)]
    {
        use windows::Win32::Foundation::HWND;
        use windows::Win32::UI::WindowsAndMessaging::{
            SetWindowPos, HWND_TOP, SWP_NOACTIVATE, SWP_NOSIZE,
        };
        unsafe {
            let _ = SetWindowPos(
                HWND(hwnd as *mut std::ffi::c_void),
                Some(HWND_TOP),
                -32000,
                -32000,
                0,
                0,
                SWP_NOACTIVATE | SWP_NOSIZE,
            );
        }
    }
}

/// 当前存活的嗅探窗（全局只保留一个）。
///
/// ★ 必须回收：不关的话每换一次台就多一个 WebView2 渲染进程常驻
///   （850 个频道 = 850 个进程），内存直接爆。
static LIVE_SNIFF: Mutex<Option<tauri::WebviewWindow>> = Mutex::new(None);

/// 登记新嗅探窗，并把上一个关掉。
fn register_sniff_window(win: tauri::WebviewWindow) {
    let old = LIVE_SNIFF.lock().unwrap().replace(win);
    if let Some(old) = old {
        let _ = old.close();
    }
}

/// 关掉当前存活的嗅探窗（延迟一小会儿，让在途请求跑完）。
fn close_live_sniff_window() {
    let win = LIVE_SNIFF.lock().unwrap().take();
    if let Some(win) = win {
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(1500));
            let _ = win.close();
        });
    }
}

/// 在主线程：开一个离屏嗅探窗 → 挂探针 → 导航到目标页。
///
/// 每次嗅探都开**新窗**（不复用），避免上一轮页面迟到的请求串台。
/// 窗口离屏 + 不在任务栏 + 无边框，用户完全看不见。
fn open_and_navigate(app: &tauri::AppHandle, page_url: String) {
    use tauri::{WebviewUrl, WebviewWindowBuilder};

    // 窗口 label 必须唯一（Tauri 要求），用递增计数保证每次不同。
    static COUNTER: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
    let n = COUNTER.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let label = format!("official-sniff-{}-{n}", std::process::id());

    let builder = WebviewWindowBuilder::new(
        app,
        &label,
        WebviewUrl::External("https://example.invalid/".parse().unwrap()),
    )
    .title("官网源嗅探")
    // 离屏 + 极小 + 无边框 + 不上任务栏：用户看不见，也不抢焦点。
    .inner_size(320.0, 180.0)
    .position(-32000.0, -32000.0)
    .skip_taskbar(true)
    .decorations(false)
    .focusable(false)
    // ★ 必须 visible(true)：hidden 窗会让 Chromium 挂起渲染 → 播放器永不起播
    //   （见模块注释坑①）。用「显示但挪到屏幕外」代替隐藏。
    .visible(true)
    .user_agent(UA)
    // ★ 与主窗口**完全相同**的环境参数（否则 WebView2 报 0x8007139F 建窗失败）。
    .additional_browser_args(&browser_args(&debug_port_args()));

    let win = match builder.build() {
        Ok(w) => w,
        Err(e) => {
            eprintln!("[embed] 嗅探窗创建失败: {e}");
            return;
        }
    };

    // ★ 建窗后**再挪一次**离屏：Tauri 的 .position() 实测不生效
    //   （否则嗅探窗会浮在主界面左上角，见 move_offscreen 注释）。
    if let Ok(h) = win.hwnd() {
        move_offscreen(h.0 as u64);
    }

    // 登记（同时回收上一个嗅探窗）。
    register_sniff_window(win.clone());

    // 探针必须在导航前挂好。
    if let Err(e) = win.with_webview(attach_probe) {
        eprintln!("[embed] with_webview 失败: {e}");
        return;
    }

    // 导航到目标页。
    match page_url.parse() {
        Ok(u) => {
            if let Err(e) = win.navigate(u) {
                eprintln!("[embed] navigate 失败: {e}");
            }
        }
        Err(e) => eprintln!("[embed] URL 解析失败: {e}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn media_stream_detection() {
        assert!(is_media_stream("https://a.com/live/index.m3u8?b=1"));
        assert!(is_media_stream("https://a.com/live/x.flv"));
        assert!(is_media_stream("https://a.com/seg/1.ts"));
        // 中转口返回 HTML，不是流
        assert!(!is_media_stream("https://m3u8.cec-tv.com.cn/?url=http://b/live.m3u8"));
        assert!(!is_media_stream("https://tv.cctv.com/live/cctv1/"));
        assert!(!is_media_stream(""));
    }

    #[test]
    fn dispatcher_rearm_replaces_channel() {
        let d = Dispatcher::global();
        let _rx1 = d.arm();
        let rx2 = d.arm();
        // 旧通道的发送端已被替换 → 投递只会进新通道。
        d.deliver(WebStream {
            url: "https://x/live.m3u8".into(),
            headers: vec![],
        });
        let got = rx2.recv_timeout(Duration::from_millis(500)).unwrap();
        assert_eq!(got.url, "https://x/live.m3u8");
    }

    #[test]
    fn browser_args_contain_critical_switches() {        // 这几个开关是「播放器能起播」的前提，删任何一个都会静默失败。
        for must in [
            "--disable-gpu",
            "--no-sandbox",
            "--disable-extensions",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ] {
            assert!(
                BROWSER_ARGS.contains(must),
                "浏览器参数缺少 {must}（会导致播放器静默不启动）"
            );
        }
        // ★ 反向断言：绝不能加 --no-proxy-server（央视静态资源经代理才通）。
        assert!(
            !BROWSER_ARGS.contains("--no-proxy-server"),
            "--no-proxy-server 会让页面加载失败（实测请求数 208→14）"
        );
        // ★ 反向断言：官网源改用 WebView2 直接直播后必须**有声**，不能再静音。
        assert!(
            !BROWSER_ARGS.contains("--mute-audio"),
            "--mute-audio 会让 WebView2 直播无声（官网源已改页面内 <video> 直接播）"
        );
    }

    #[test]
    fn pure_video_inject_hides_native_controls() {
        // ★★ 锁死历史 bug：`setAttribute('controls','false')` 对 HTML 布尔属性
        //    完全无效（只要存在就是 true），截图实证底部原生进度条仍露出。
        assert!(
            !PURE_VIDEO_INJECT.contains(r#"setAttribute('controls', "#),
            "controls 是布尔属性，setAttribute('controls','false') 依然显示控件 → 必须 removeAttribute"
        );
        assert!(
            PURE_VIDEO_INJECT.contains("removeAttribute('controls')"),
            "必须 removeAttribute('controls') 才能去掉原生播放控件"
        );
        // defineProperty 劫持：页面之后 video.controls = true 也设不上
        assert!(
            PURE_VIDEO_INJECT.contains("Object.defineProperty(v, 'controls'"),
            "必须劫持 controls property，否则页面脚本会把它设回 true"
        );
    }

    #[test]
    fn pure_video_inject_isolates_by_ancestor_chain() {
        // ★ 净化策略必须是「只留 video 祖先链 + 其余兄弟隐藏」，
        //   而不是靠猜类名的黑名单（实测央视导航一个都没命中）。
        assert!(
            PURE_VIDEO_INJECT.contains("chain.push(n)"),
            "必须用祖先链隔离，不能退回类名黑名单"
        );
        assert!(
            PURE_VIDEO_INJECT.contains("if (k === keep) continue;"),
            "必须保留祖先链上的子节点、隐藏其余兄弟"
        );
    }
}
