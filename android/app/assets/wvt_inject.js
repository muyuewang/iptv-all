/* ============================================================================
 * 纯净电视直播 · 官网页面注入脚本（网页线路专用）
 *
 * 这条路线承载「官网源」里的网页线路：官方直播页在 WebView 里真实打开，
 * 本脚本按 webpage_adapter.dat 的规则找到 <video>，把它钉成全屏固定层并自动起播。
 *
 * 为什么必须这样：CCTV 等官网用 WASM + MSE（blob: 源）播放，抓出来的地址
 * 交给 mpv/ExoPlayer 都播不了 —— 只有让浏览器自己播才行（WebViewTV 同策略）。
 *
 * 注入时机：每次 onPageFinished（含站内跳转）都会调用一次。
 * 依赖全局量：window.wvt_adapter（适配规则）、window.wvt_extra（频道附加参数）
 *
 * ── 三条经验教训（都是实测踩出来的）────────────────────────────────────
 * 1) 不要把 <video> 搬出原来的 DOM 子树。
 *    CCTV 这类站点用 MSE 挂 blob: 源，元素一搬家播放就断（readyState 掉回 0
 *    且不再拉流），而且站点的播放器逻辑还在原地等元素 —— 画面永远黑。
 *    正确做法：让元素留在原地，把它自己钉成 position:fixed 全屏 + 最大 z-index。
 * 2) 别用内联样式去「顺手清理」自己的容器。
 *    早期版本把 video 搬进自建容器 #__wvt_host，然后 unclip() 沿祖先链设
 *    position:static / z-index:auto —— 宿主自己就是 video 的祖先，于是被自己
 *    清成了普通流内元素，全屏没生效，站点的播放浮层照样盖在上面。
 *    现在 unclip() 遇到 #__wvt_host 立即停止。
 * 3) 站点的浮层要用「谁盖着就收谁」的通用办法处理，而不是硬编码站点类名。
 *    钉成全屏固定层后，用 elementsFromPoint 从中心/四角探测；在 video 之前的
 *    那几个（= 盖在 video 上的）收掉，video 之后的不管，祖先和后代一概不动。
 * 4) 尺寸别用 100vw/100vh，也别只信 100%：页面坐标系可能被放大。
 *    有的站点在 <html> 上设 zoom:2，有的用 width=980 的宽视口 —— 此时
 *    「视口」比真实屏幕大一圈，video 钉上去就超出屏幕被裁掉（用户反馈
 *    「全屏很大、显示不全」）。现在统一按 window.innerWidth/Height 显式给 px，
 *    并在每轮 tick 里用实测 rect 与视口比对，超了就按比例缩回去。
 * ==========================================================================*/
(function () {
  "use strict";
  /* 日志缓冲区先建好：脚本在 document-start 阶段跑，此时 DOM 可能还没成形，
     任何一步抛异常都会让后面的 log() 全无踪影 —— 先有个容器才好定位。 */
  try { window.__wvtLog = window.__wvtLog || []; } catch (e) { }

  function log(s) {
    try { console.log("[WVT] " + s); } catch (e) { }
    /* 同时留一份在页面上：桌面端是 CDP 驱动（拿不到 console 事件），
       安卓端能读 logcat —— 两边都靠 __wvtLog 定位问题。 */
    try {
      window.__wvtLog.push(s);
      if (window.__wvtLog.length > 60) window.__wvtLog.shift();
    } catch (e) { }
  }

  if (window.__wvtInjected) {
    try { window.wvt_resume(); }
    catch (e) { log("wvt_resume 失败：" + (e && e.message)); }
    return;
  }
  window.__wvtInjected = true;

  var AD = window.wvt_adapter || {};
  var EXTRA = window.wvt_extra || "";
  var HOST_ID = "__wvt_host";
  var CLOSE_ID = "__wvt_close";

  /* 超过这个时间还没见到画面 → 判定本线路不可用，让原生换下一条。
     官网页面本身加载慢（SPA + 播放器 SDK）很正常，别设太短。
     ★ v2.10.17 老内核快速判死：本注入脚本就跑在官方页 WebView 里，检测
     MediaSource / WebAssembly —— 缺 MSE 的老内核（2017 年前的 TV 盒子
     常见）官方页自己就会弹「浏览器不支持视频播放」，等再久 video 也不会
     出现，20s 白等 × 多条线路 = 一分多钟黑屏。缩到 8s 快速换线。 */
  var FAIL_MS = (window.MediaSource && window.SourceBuffer
                 && typeof WebAssembly !== "undefined") ? 20000 : 8000;
  /* 更快的判据：播放器已经把源挂上、play() 也调过了，却连续这么多秒时间轴
     纹丝不动 —— 说明站点自己就没在播（CCTV-1 会直接提示「播出安排变更，
     暂不支持播放该时段内容」）。这种情况不用干等 20 秒。 */
  var STUCK_TICKS = 12;

  var video = null;
  var entered = false;
  var failed = false;
  var tries = 0;
  var t0 = Date.now();
  var swept = 0;
  var stuck = 0;
  /* ★ v2.10.13g 起播后保活复核：onPlaying 只是「瞬时已播 >0.2s」的一次性判定，
     native 随即 fadeInPage 把官方页拉到全亮。若视频之后停住（缓冲失败 / 直播流
     断掉 / 站点给的是大播放按钮海报帧），entered=true 后没有任何判死机制 ——
     用户就会看到官方页原始界面全亮透出且永不换线（v2.10.13 用户实测截图）。
     复核判据：时间轴连续 10s 不推进（含 paused/ended）即报 pageFailed 换线；
     native pageFailedSent 在 pagePlaying 时已重置，起播后再报依然有效。 */
  var STALL_MS = 10000;
  var lastCT = -1;
  var lastMoveT = 0;
  var stallReported = false;
  /* 被收掉的站点浮层的文本存档（错误检测用，见 sweepOverlays） */
  var sweptTexts = [];
  /* ★ 只允许存在一条轮询链。
     onPageFinished 和 onProgressChanged(100) 都会注入；第二次会走 wvt_resume()。
     早期版本 wvt_resume() 直接再 setTimeout(tick) 而没清掉旧定时器 —— 于是两条链
     并行跑，tick 一秒被调两次，「空转 12 秒」实际 5 秒就触发，把本来能起来的
     线路误判成失败（湖北卫视央视频线路就是这么被误杀的）。 */
  var timer = null;

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(tick, ms);
  }

  function call(name, arg) {
    try {
      if (window.NativeTV && typeof NativeTV[name] === "function") {
        return arg === undefined ? NativeTV[name]() : NativeTV[name](arg);
      }
    } catch (e) { }
    return null;
  }

  /* ---------------------------------------------------------- 找元素 */
  /** 按适配规则挑出「最可能正在播」的那个元素 */
  function pickElement(cond) {
    var list = [];
    if (cond) {
      var nodes = document.all, extra = EXTRA;
      for (var i = 0; i < nodes.length; i++) {
        var e = nodes[i];
        try { if (eval(cond)) list.push(e); } catch (err) { }
      }
    }
    if (!list.length) {
      var vs = document.getElementsByTagName("video");
      for (var j = 0; j < vs.length; j++) list.push(vs[j]);
    }
    var first = null, sized = null, playing = null;
    for (var k = 0; k < list.length; k++) {
      var x = list[k];
      if (!first) first = x;
      if (x.tagName !== "VIDEO") continue;
      if (x.videoWidth > 0 || (!x.paused && x.readyState > 0)) { playing = x; break; }
      if (!sized) {
        var r = x.getBoundingClientRect();
        if (r.width > 2 && r.height > 2) sized = x;
      }
    }
    return playing || sized || first;
  }

  function findByCond(cond) { return pickElement(cond); }
  function pickVideo() { return pickElement(AD.videoElementCondition); }

  /* ---------------------------------------------------------- 解开祖先裁切 */
  /**
   * 解除祖先链上的「裁切 / 隐藏 / 层叠上下文」。
   * CCTV 这类站点会把 <video> 或它的祖先设成 display:none，此时元素根本没被
   * 渲染（getBoundingClientRect 全 0），只改宽高没用。
   *
   * ★ 到 body / html / 自建容器 #__wvt_host 就停 —— 尤其是宿主容器，
   *   它必须保留 position:fixed / z-index，否则全屏失效且会被站点浮层压住。
   */
  function unclip(el) {
    var p = el.parentNode, n = 0, hid = 0;
    while (p && p.nodeType === 1 && p !== document.body &&
           p !== document.documentElement && p.id !== HOST_ID && n++ < 24) {
      try {
        var st = p.style, cs = getComputedStyle(p);
        st.setProperty("transform", "none", "important");
        st.setProperty("filter", "none", "important");
        st.setProperty("perspective", "none", "important");
        st.setProperty("backdrop-filter", "none", "important");
        st.setProperty("contain", "none", "important");
        st.setProperty("will-change", "auto", "important");
        st.setProperty("overflow", "visible", "important");
        st.setProperty("clip-path", "none", "important");
        if (cs.display === "none") { st.setProperty("display", "block", "important"); hid++; }
        if (cs.visibility === "hidden") st.setProperty("visibility", "visible", "important");
        if (cs.opacity === "0") st.setProperty("opacity", "1", "important");
        /* 祖先只要 position:relative + z-index 就自成一个层叠上下文，
           video 的 z-index 再大也出不去 —— 必须清掉。 */
        st.setProperty("z-index", "auto", "important");
        st.setProperty("isolation", "auto", "important");
      } catch (e) { }
      p = p.parentNode;
    }
    return hid;
  }

  /* ---------------------------------------------------------- 视口归一化 */
  /**
   * 返回 [宽, 高]（CSS px）。
   *
   * 优先级：visualViewport > window.innerWidth/Height > documentElement.clientWidth。
   *
   * 为什么首选 visualViewport：position:fixed 的全屏层就是挂在「视觉视口」上的，
   * 而 Android WebView 开了 setUseWideViewPort 后布局视口是 980px、真实可视区域
   * 只有几百 px，两者差一大截 —— 照布局视口钉尺寸，画面就会「很大、显示不全」。
   * visualViewport.width/height 正是那个真实可视区域（WebView 61+ 都支持）。
   */
  function viewSize() {
    try {
      var vv = window.visualViewport;
      if (vv && vv.width > 2 && vv.height > 2) return [vv.width, vv.height];
    } catch (e) { }
    var w = window.innerWidth, h = window.innerHeight;
    if (!w || !h) {
      w = w || document.documentElement.clientWidth || 0;
      h = h || document.documentElement.clientHeight || 0;
    }
    return [w || 0, h || 0];
  }

  /** 把页面的 viewport / zoom 掰回 1:1，免得坐标系被整体放大 */
  function normalizeViewport() {
    try {
      var head = document.head || document.documentElement;
      var m = document.querySelector('meta[name="viewport"]');
      if (!m) {
        m = document.createElement("meta");
        m.setAttribute("name", "viewport");
        head.appendChild(m);
      }
      m.setAttribute("content",
        "width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no");
      document.documentElement.style.setProperty("zoom", "1", "important");
      document.documentElement.style.setProperty("-webkit-text-size-adjust", "none", "important");
      if (document.body) document.body.style.setProperty("zoom", "1", "important");
    } catch (e) { }
  }

  /**
   * 每轮 tick 复查一次 zoom。
   *
   * 有的站点（CCTV 系）会在 <html> 上设 zoom:2 —— 这是从**样式表**来的，
   * 行内样式看不出来，但 position:fixed 的元素也跟着被放大 2 倍，
   * 于是 980px 宽的 video 实际占了 1960px，屏幕装不下 → 用户看到
   * 「全屏很大、显示不全」。用 computed style 查出来按行内 !important 压掉。
   */
  function killZoom() {
    try {
      var de = document.documentElement;
      var z = getComputedStyle(de).zoom;
      if (z && z !== "1" && z !== "normal" && z !== "auto") {
        de.style.setProperty("zoom", "1", "important");
        log("页面 zoom=" + z + " 已压回 1");
      }
      if (document.body) {
        var zb = getComputedStyle(document.body).zoom;
        if (zb && zb !== "1" && zb !== "normal" && zb !== "auto") {
          document.body.style.setProperty("zoom", "1", "important");
        }
      }
    } catch (e) { }
  }

  /* ---------------------------------------------------------- 钉成全屏固定层 */
  /**
   * 把 video 自己钉成「视口固定 + 全屏 + 最高层级」。
   *
   * 一律用 px 显式给尺寸（取自 viewSize()），再叠一层 fitBox() 实测校正：
   * 只要发现盒子明显超出视口就按比例缩回去，小于视口就撑满。
   * 这样无论页面怎么设 zoom / viewport，画面都是「刚好铺满」。
   */
  function pinVideo(v) {
    try {
      var st = v.style, vs = viewSize();
      st.setProperty("position", "fixed", "important");
      st.setProperty("left", "0", "important");
      st.setProperty("top", "0", "important");
      st.setProperty("width", (vs[0] > 2 ? Math.round(vs[0]) + "px" : "100%"), "important");
      st.setProperty("height", (vs[1] > 2 ? Math.round(vs[1]) + "px" : "100%"), "important");
      st.setProperty("min-width", "0", "important");
      st.setProperty("min-height", "0", "important");
      st.setProperty("max-width", "none", "important");
      st.setProperty("max-height", "none", "important");
      st.setProperty("margin", "0", "important");
      st.setProperty("padding", "0", "important");
      st.setProperty("border", "0", "important");
      st.setProperty("transform", "none", "important");
      st.setProperty("display", "block", "important");
      st.setProperty("visibility", "visible", "important");
      st.setProperty("opacity", "1", "important");
      st.setProperty("object-fit", "contain", "important");
      st.setProperty("background", "#000", "important");
      st.setProperty("pointer-events", "auto", "important");
      st.setProperty("z-index", "2147483640", "important");
    } catch (e) { }
  }

  /** 实测校正：超出视口就缩回去，不足就撑满（兜住各种奇怪的坐标系） */
  function fitBox(v) {
    try {
      var vs = viewSize(), w = vs[0], h = vs[1];
      if (w < 3 || h < 3) return;
      var r = v.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) return;
      var st = v.style;
      if (r.width > w * 1.12 || r.height > h * 1.12) {
        var k = Math.min(w / r.width, h / r.height);
        st.setProperty("width", Math.max(2, Math.round(r.width * k)) + "px", "important");
        st.setProperty("height", Math.max(2, Math.round(r.height * k)) + "px", "important");
        log("画布超出视口，按 " + k.toFixed(2) + " 缩回 " +
            Math.round(r.width * k) + "×" + Math.round(r.height * k));
      } else if (r.width < w * 0.88 || r.height < h * 0.88) {
        st.setProperty("width", Math.round(w) + "px", "important");
        st.setProperty("height", Math.round(h) + "px", "important");
      }
    } catch (e) { }
  }

  /** video 是否已经铺满视口（fixed 生效的判据） */
  function coversViewport(v) {
    try {
      var r = v.getBoundingClientRect();
      var vs = viewSize(), w = vs[0], h = vs[1];
      if (!w || !h) return true;
      return r.width >= w * 0.85 && r.height >= h * 0.85 &&
             Math.abs(r.left) < w * 0.1 && Math.abs(r.top) < h * 0.1;
    } catch (e) { return true; }
  }

  /* ---------------------------------------------------------- 自建容器（兜底） */
  var HOST_CSS =
    "position:fixed!important;left:0!important;top:0!important;" +
    "width:100%!important;height:100%!important;margin:0!important;padding:0!important;" +
    "background:#000!important;z-index:2147483600!important;overflow:hidden!important;" +
    "display:block!important;visibility:visible!important;opacity:1!important;" +
    "transform:none!important;filter:none!important;contain:none!important;" +
    "clip-path:none!important;isolation:auto!important;";

  /**
   * 只有在「原地钉 fixed 没生效」时才用的兜底容器。
   * 注意：每次调用都重刷样式 —— 否则任何一次 unclip / 站点改写都会把它打回原形。
   */
  function hostBox() {
    var host = document.getElementById(HOST_ID);
    if (!host) {
      host = document.createElement("div");
      host.id = HOST_ID;
      (document.body || document.documentElement).appendChild(host);
    } else if (host !== host.parentNode.lastElementChild) {
      host.parentNode.appendChild(host);          // 永远保持在最后（最上层）
    }
    try { host.style.cssText = HOST_CSS; } catch (e) { }
    /* HOST_CSS 里的 100% 是「布局视口」的 100%，宽视口下会超出屏幕 —— 用实测
       视觉视口尺寸覆盖掉。 */
    try {
      var vs = viewSize();
      if (vs[0] > 2 && vs[1] > 2) {
        host.style.setProperty("width", Math.round(vs[0]) + "px", "important");
        host.style.setProperty("height", Math.round(vs[1]) + "px", "important");
      }
    } catch (e) { }
    return host;
  }

  /* ---------------------------------------------------------- 收掉盖在 video 上的浮层 */
  /**
   * 通用去浮层：在若干探测点上取元素栈，栈里排在 video 之前的
   * （= 绘制层级比 video 高的）收掉。
   * 祖先和后代一律跳过 —— 藏了祖先就连 video 一起没了。
   */
  function sweepOverlays(v) {
    var pts = [[0.5, 0.5], [0.2, 0.5], [0.8, 0.5], [0.5, 0.15], [0.5, 0.85]];
    var hit = [];
    var vs = viewSize();
    for (var p = 0; p < pts.length; p++) {
      var list;
      try { list = document.elementsFromPoint(vs[0] * pts[p][0], vs[1] * pts[p][1]); }
      catch (e) { continue; }
      if (!list || !list.length) continue;
      var vi = Array.prototype.indexOf.call(list, v);
      if (vi <= 0) continue;            /* 0=video 已是最上层；-1=没命中，都别动 */
      for (var i = 0; i < vi; i++) {
        var e = list[i];
        if (!e || e.nodeType !== 1) continue;
        if (e === document.body || e === document.documentElement) continue;
        if (e.id === CLOSE_ID || e.id === HOST_ID) continue;
        if (e.contains(v) || v.contains(e)) continue;
        try {
          if (e.__wvtHidden) continue;
          e.__wvtHidden = 1;
          /* ★ 收掉之前留一份文本：站点的错误提示（VIP 专享弹窗、播出安排
             变更提示）往往就盖在 video 上，会被这里收掉 —— 之后 body.innerText
             就查不到它了，错误检测全靠这份存档。 */
          try {
            var tx = (e.innerText || e.textContent || "").replace(/\s+/g, " ");
            if (tx) sweptTexts.push(tx.slice(0, 400));
            if (sweptTexts.length > 40) sweptTexts.shift();
          } catch (te) { }
          e.style.setProperty("display", "none", "important");
          hit.push(e.tagName.toLowerCase() + (e.id ? "#" + e.id : "") +
                   (typeof e.className === "string" && e.className
                     ? "." + e.className.trim().split(/\s+/).slice(0, 2).join(".") : ""));
        } catch (err) { }
      }
    }
    if (hit.length && swept < 4) {
      swept++;
      log("收掉盖在画面上的浮层 " + hit.length + " 个：" + hit.slice(0, 6).join(", "));
    }
  }

  function neutralizePage() {
    try {
      document.documentElement.style.setProperty("overflow", "hidden", "important");
      document.documentElement.style.setProperty("background", "#000", "important");
      document.body.style.setProperty("overflow", "hidden", "important");
      document.body.style.setProperty("background", "#000", "important");
    } catch (e) { }
  }

  /* ---------------------------------------------------------- 组装 */
  function fill() {
    if (!video) return;
    var v = video;
    killZoom();
    unclip(v);
    pinVideo(v);
    neutralizePage();
    /* 原地钉完先探测一次；确实没铺满（祖先里还有 transform/filter 之类的
       包含块，把 fixed 拽回局部坐标系了）才动用兜底容器搬家 */
    if (!coversViewport(v)) {
      var host = hostBox();
      if (v.parentNode !== host) {
        host.appendChild(v);
        pinVideo(v);
        log("原地固定未生效，video 搬入兜底全屏容器");
      }
    }
    fitBox(v);
    sweepOverlays(v);
  }

  /* ---------------------------------------------------------- 起播 */
  function setupVideo(v) {
    if (v.__wvtSetup) return;
    v.__wvtSetup = true;
    try {
      v.autoplay = true;
      v.muted = false;
      v.defaultMuted = false;
      v.volume = 1;
      v.removeAttribute("controls");
      v.setAttribute("playsinline", "");
      v.setAttribute("webkit-playsinline", "");
      if (!v.poster) v.poster = "";
    } catch (e) { }
    /* 只在「真的开始播」时通知原生。别监听 play —— 那只是 play() 被调用，
       此时可能一秒数据都还没来（CCTV 就是这种：play 事件立刻触发，画面却
       一直是站点的播放按钮浮层）。 */
    v.addEventListener("playing", function () { onPlaying(); });
    v.addEventListener("canplay", function () { fill(); });
    v.addEventListener("error", function () {
      /* 只报前 3 次：有的站点会用一个必然失败的 <source> 探路，
         1s 一条会把 logcat 刷满，反而盖住真正有用的信息。 */
      if ((v.__wvtErrN = (v.__wvtErrN || 0) + 1) <= 3) {
        log("video error code=" + (v.error && v.error.code));
      }
      video = null;                     // 重新找下一个元素再试
    });
    log("video setup ok");
  }

  function onPlaying() {
    fill();
    if (entered || stallReported) return;   // 报死后不再反复进入（换线由 pageFailed 驱动）
    if (!video || video.currentTime <= 0.2) return;      // 没真的出画面，不算
    entered = true;
    lastCT = video.currentTime;                          // 保活复核基线
    lastMoveT = Date.now();
    log("playing -> notify native（已播 " + video.currentTime.toFixed(1) + "s）");
    call("pagePlaying");
  }

  function clickPlay() {
    if (AD.needPlayButton === false) return false;
    var cond = AD.playButtonCondition || "true";
    var el = findByCond(cond);
    if (el && typeof el.click === "function") {
      try { el.click(); log("clicked play button"); return true; } catch (e) { }
    }
    return false;
  }

  function preClick() {
    if (AD.needPreClick) {
      var el = findByCond(AD.preClickElementCondition);
      if (el && el.click) { try { el.click(); log("pre-click"); } catch (e) { } }
    }
    if (AD.needPreClick2) {
      var el2 = findByCond(AD.preClickElementCondition2);
      if (el2 && el2.click) { try { el2.click(); log("pre-click2"); } catch (e) { } }
    }
  }

  function enterFullscreenBtn() {
    if (!AD.needEnterFullscreenButton) return;
    var el = findByCond(AD.enterFullscreenButtonCondition);
    if (el && el.click) { try { el.click(); log("clicked fullscreen button"); } catch (e) { } }
  }

  /* ---------------------------------------------------------- 诊断 */
  /** 起不来的时候打一行「为什么」到 logcat，方便定位是站点限制还是脚本问题 */
  function diag() {
    var s = "url=" + location.href;
    try {
      var v = document.getElementsByTagName("video")[0];
      if (!v) s += " video=无";
      else s += " video[paused=" + v.paused + " rs=" + v.readyState +
                " net=" + v.networkState + " t=" + v.currentTime.toFixed(1) +
                " err=" + (v.error ? v.error.code : "-") + "]";
    } catch (e) { }
    try {
      var ids = ["error_msg_player", "loading_player"],
          txt = [];
      for (var i = 0; i < ids.length; i++) {
        var e = document.getElementById(ids[i]);
        if (e && e.innerText && e.innerText.trim()) {
          txt.push(ids[i] + "=\"" + e.innerText.trim().slice(0, 40) + "\"");
        }
      }
      if (txt.length) s += " 站点提示：" + txt.join(" ");
    } catch (e) { }
    return s;
  }

  /* ---------------------------------------------------------- 站点错误提示 */
  /**
   * 站点自己弹出的「播不了」提示——这不是我们的 bug，但更不能让用户对着
   * 黑屏干等 20 秒超时。命中一条就立刻带原因报失败，让原生换线路。
   *
   * 词表只收「错误弹窗/提示条才会出现的完整说法」，不收「VIP」「付费」这类
   * 会出现在频道列表角标、推荐位里的短词，避免误杀能正常播的线路。
   * 顺序即优先级：VIP 类放最前（文案最稳）。
   */
  var SITE_ERRS = [
    ["会员专享", "VIP会员专享，需开通会员"],
    ["VIP专享", "VIP专享内容，需开通会员"],
    ["开通会员方可", "需开通会员观看"],
    ["暂不支持播放", "该时段暂不支持播放"],
    ["播出安排变更", "播出安排变更，该时段暂不可看"],
    ["暂停播出", "节目暂停播出"],
    ["节目暂时无法", "节目暂时无法播放"],
    ["信号中断", "信号中断"],
    ["内容已下线", "内容已下线"]
  ];

  /**
   * 扫描站点是否在展示「播不了」的提示。
   * 取材三处：body 可见文本（innerText，只算显示出来的）+ 被我们收掉的浮层
   * 文本存档（弹窗往往刚出现就被 sweepOverlays 收掉）+ CCTV 固定的
   * error_msg_player 元素。全部没命中返回 null，命中返回 [原因, 证据]。
   */
  function detectSiteError() {
    var hay = [];
    try {
      var t = document.body ? document.body.innerText : "";
      if (t) hay.push(t);
    } catch (e) { }
    for (var i = 0; i < sweptTexts.length; i++) hay.push(sweptTexts[i]);
    try {
      var em = document.getElementById("error_msg_player");
      if (em) {
        var et = (em.innerText || em.textContent || "").trim();
        if (et) hay.push(et);
      }
    } catch (e) { }
    if (!hay.length) return null;
    for (var k = 0; k < SITE_ERRS.length; k++) {
      var word = SITE_ERRS[k][0];
      for (var j = 0; j < hay.length; j++) {
        if (hay[j] && hay[j].indexOf(word) >= 0) return SITE_ERRS[k];
      }
    }
    return null;
  }

  /* ---------------------------------------------------------- 轮询 */
  /* ★ v2.10.8：隐藏官方页自己的「× 返回」角标（央视频/央视网播放页左上角）。
     我们自己有 BACK 确认卡，页面这个返回既没用又毁画面（用户实测反馈）。
     按经验教训 3 的思路按「内容+位置+尺寸」识别，不硬编码站点类名：
     文本恰为「返回/×返回/Back」、自身很小、位于视口左上区域 —— 三条都满足才收。
     每 5 个 tick 跑一次（tickBody 高频，querySelectorAll 全树扫不能每秒来）。 */
  function hideBackBadge() {
    try {
      var all = document.querySelectorAll("body *");
      for (var i = 0; i < all.length; i++) {
        var el = all[i];
        if (el.__wvtBackHidden) continue;
        /* ★ 用 innerText（只算可见文本）：textContent 会把 aria/隐藏层文案也
           算进来，长度直接超限 —— v2.10.8 首版实测「返回」两字永远匹配不上 */
        var txt = (el.innerText || "").replace(/\s+/g, "");
        if (!txt || txt.length > 4) continue;
        if (txt.indexOf("返回") < 0 && txt.toLowerCase() !== "back" && txt !== "×") continue;
        var r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0 || r.width > 160 || r.height > 70) continue;
        if (r.left > window.innerWidth * 0.35 || r.top > window.innerHeight * 0.35) continue;
        el.style.setProperty("display", "none", "important");
        el.__wvtBackHidden = 1;
        log("已隐藏官方页返回角标：" + (el.className || el.tagName) + " <" + txt + ">");
      }
    } catch (e) { log("hideBackBadge 异常（吞掉）：" + (e && e.message)); }
  }

  function tick() {
    /* ★ 整体兜底：tick 里任何一步抛异常都不能断掉轮询链 —— 链一断，
       「报失败 → 换线路」永远不会发生，用户只能对着黑屏干等
       （v2.4.6 实测：个别站点页面上某步异常就会造成零反馈）。 */
    try {
      tickBody();
    } catch (e) {
      log("tick 异常（已吞掉，链继续）：" + (e && e.message));
    }
    /* ★ v2.10.20 起播头 2 秒用 400ms 快轮询：视频元素出现、play() 生效、
       时间轴开始推进这几个时刻都靠轮询发现，1s 一轮平均白搭 0.5s；
       前 5 轮加密到 400ms，起播判定能早 0.3~0.6s（之后回到 1s，不增负担）。 */
    schedule(tries < 5 ? 400 : (tries < 40 ? 1000 : 2000));
  }

  function tickBody() {
    tries++;
    if (tries % 5 === 1) hideBackBadge();
    if (!video) {
      video = pickVideo();
      if (video) { setupVideo(video); fill(); }
    } else if (!document.documentElement.contains(video)) {
      log("video 已从 DOM 移除，重新找");
      video = null; entered = false;
      lastCT = -1; lastMoveT = Date.now();   // 新 video 重新做保活基线，防拿旧基线误判
    }
    if (video) {
      fill();
      try {
        if (video.paused && !video.ended) {
          var p = video.play();
          if (p && p.catch) p.catch(function () { clickPlay(); });
        }
      } catch (e) { clickPlay(); }
      if (video.paused && tries % 3 === 0) clickPlay();
      if (!entered && tries === 3) preClick();
      if (!entered && tries === 6) enterFullscreenBtn();
      if (!entered && !video.paused && video.currentTime > 0.2) onPlaying();
      /* 「在播但时间轴不动」计数：播放器已经把源挂上、play() 也调过了，时间轴却
         纹丝不动 —— 多半是被站点限制挡住了。要求 currentSrc 非空，免得把
         「刚找到 video、站点还没挂源」的正常加载过程算进来。 */
      var dead = !video.paused && video.currentTime <= 0.2 && !!video.currentSrc;
      stuck = dead ? stuck + 1 : 0;
      /* ★ v2.10.13g 起播后保活复核：时间轴 10s 不推进（paused/ended/缓冲死）
         → 报 pageFailed 让原生换下一条线路。正常直播 currentTime 每秒都在涨，
         网络抖动 10s 宽限足够；画面真停了不能让官方页原始界面全亮挂在那。 */
      if (entered && !stallReported) {
        var ct = video.currentTime;
        if (ct > lastCT + 0.01) { lastCT = ct; lastMoveT = Date.now(); }
        if (Date.now() - lastMoveT > STALL_MS) {
          stallReported = true;
          entered = false;               // 回到未起播态：pageFailed 网页侧会换线
          log("起播后画面停住（时间轴 " +
              ((Date.now() - lastMoveT) / 1000).toFixed(1) + "s 未推进），请求换线");
          call("pageFailed", "画面停住了，正在换下一条线路");
        }
      }
    }
    /* ★ 站点自己说「播不了」（VIP 专享弹窗 / 播出安排变更提示…）——
       立刻带原因报失败，别让用户黑屏干等 20s 超时。
       只在没起播时查：画面都出来了就不再碰词表，免得页面角落的宣传
       文案误杀正常播放的线路。 */
    if (!entered && !failed &&
        !(video && !video.paused && video.currentTime > 0.2)) {
      var siteErr = detectSiteError();
      if (siteErr) {
        failed = true;
        log("站点提示「" + siteErr[1] + "」（命中词：" + siteErr[0] +
            "），立即换线 | " + diag());
        call("pageFailed", siteErr[1]);
      }
    }
    /* 迟迟见不到画面 → 交给原生换下一条线路（多数频道有多条官网线路） */
    if (!entered && !failed &&
        (Date.now() - t0 > FAIL_MS || stuck >= STUCK_TICKS)) {
      failed = true;
      log("判定本线路起不来（" +
          (stuck >= STUCK_TICKS ? "播放器空转 " + stuck + "s" : "超时 " +
           Math.round(FAIL_MS / 1000) + "s") + "），请求切换下一条 | " + diag());
      call("pageFailed", stuck >= STUCK_TICKS ? "线路长时间无响应" : "页面加载超时");
    }
  }

  /* v2.10.13：注入「✕ 返回」角标（mountClose）已退役 —— v2.10 改版后 BACK
     统一走原生确认卡（doBack → __tvBack），网页层这个返回既无用又毁画面
     （用户实测反馈 CCTV-5+ 页面左上角挂着碍事）。CLOSE_ID 保留给
     sweepOverlays 的豁免清单（防旧标记残留误收）。 */

  window.wvt_resume = function () {
    /* 已经在播了就别推倒重来 —— 晚到的 onPageFinished / onProgressChanged
       会再调一次注入，这里只需保持全屏样式即可。 */
    if (entered) { fill(); return; }
    entered = false;
    failed = false;
    tries = 0;
    t0 = Date.now();
    stuck = 0;
    lastCT = -1;              // v2.10.13g 保活复核基线一并重置
    lastMoveT = Date.now();
    stallReported = false;
    video = null;
    schedule(600);
  };

  try {
    /* 旋屏 / 窗口尺寸变化后重钉一次 —— 否则画面会停留在旧尺寸（看起来就是「显示不全」） */
    var refit = function () {
      if (!video) return;
      pinVideo(video);
      fitBox(video);
      sweepOverlays(video);
    };
    window.addEventListener("resize", refit);
    window.addEventListener("orientationchange", function () { setTimeout(refit, 350); });
    try {
      /* 视觉视口单独变了（软键盘弹出、系统字体/缩放进阶）也要重钉 */
      if (window.visualViewport) window.visualViewport.addEventListener("resize", refit);
    } catch (e) { }
    normalizeViewport();
    schedule(800);
    log("注入完成，适配规则=" + (AD.url || "(兜底)") + " FAIL_MS=" + FAIL_MS);
  } catch (e) {
    log("注入脚本异常：" + (e && e.message));
  }
})();
