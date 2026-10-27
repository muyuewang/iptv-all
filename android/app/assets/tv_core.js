/* ============================================================================
 * 纯净电视直播 · 共享核心（JS 版）—— 安卓 WebView 端
 * 与 shared/core.py 逻辑逐行对齐，改动任一都要同步另一份。
 * ==========================================================================*/
(function (root) {
  "use strict";

  var APP_UA = "Mozilla/5.0 (Linux; Android 9; SM-N9700 Build/PQ3B.190801.01311438; wv) " +
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 " +
    "Mobile Safari/537.36 diashizhb LT-APP/48/517/YM-RT/";

  // 当前生效的接口域名（apiGet 命中哪个就记住哪个，解密与抓取必须同源）
  var API = "https://api1.2026016.xyz";

  // 同源镜像（实测均可返回同一套频道数据）；主站不通时按顺序回退
  var API_MIRRORS = [
    "https://api1.2026016.xyz",
    "https://api.2026016.xyz",
    "https://api2.2026016.xyz"
  ];

  var _prefMirror = null;      // 手动指定镜像（设置页可改）

  // (tid, 显示名)
  var CATS = [
    ["tv", "综合"], ["ty", "体育"], ["ys", "央视"],
    ["ws", "卫视"], ["wintv123", "极速港澳台"], ["gt", "港澳台"]
  ];

  var CACHE_TTL = 600;          // 秒

  /* ---------------------------------------------------------------- 基础 */
  function $(id) { return document.getElementById(id); }

  function b64d(s) {
    try { return atob(s); } catch (e) { return null; }
  }

  function b64e(s) { return btoa(s); }

  function norm(s) { return (s || "").replace(/\s|　/g, ""); }

  /** 手动指定接口镜像（host 例：https://api2.2026016.xyz 或 api2.2026016.xyz）；传空=自动 */
  function setApi(host) {
    host = (host || "").trim();
    if (!host) { _prefMirror = null; return; }
    if (host.indexOf("http") !== 0) host = "https://" + host;
    _prefMirror = host.replace(/\/+$/, "");
  }
  function getApi() { return API; }

  /**
   * 请求接口（path 以 / 开头），自动在镜像间回退。
   * 命中后把 API 切到该镜像 —— 保证同一轮抓取的页面与解密用同一个域名。
   */
  function apiGet(path, timeoutMs) {
    var hosts = [];
    if (_prefMirror) hosts.push(_prefMirror);
    [API].concat(API_MIRRORS).forEach(function (m) { if (hosts.indexOf(m) < 0) hosts.push(m); });
    var i = 0;
    function tryNext(lastErr) {
      if (i >= hosts.length) return Promise.reject(lastErr || new Error("所有接口镜像均不可用"));
      var m = hosts[i++];
      // ★ 超时封顶（与 core.py api_get 对齐）：主域名 8s、后续镜像 5s ——
      //   不封顶的话一个挂死的主站就串行吃满 15s×N 个域名，表现为「半天出不来」。
      var t = (m === API) ? Math.min(timeoutMs || 15000, 8000)
                          : Math.min(timeoutMs || 15000, 5000);
      var ctl = (typeof AbortController !== "undefined") ? new AbortController() : null;
      var timer = ctl ? setTimeout(function () { ctl.abort(); }, t) : null;
      return fetch(m + path, ctl ? { signal: ctl.signal } : undefined)
        .then(function (r) { return r.text(); })
        .then(function (t) {
          if (timer) clearTimeout(timer);
          if (!t || t.length < 200) throw new Error("响应异常");
          API = m;
          return t;
        })
        .catch(function (e) { if (timer) clearTimeout(timer); return tryNext(e); });
    }
    return tryNext(null);
  }

  /* ---------------------------------------------------------------- 频道列表 */
  function parseChannels(html, tid) {
    var out = [], seen = {};
    var re = /act=play[^'"]*&tid=([a-z0-9]+)&id=(\d+)['"]?[^>]*>([^<]+)</g, m;
    while ((m = re.exec(html))) {
      if (m[1] !== tid) continue;
      if (seen[m[2]]) continue;
      seen[m[2]] = 1;
      out.push({ id: m[2], name: m[3].trim() });
    }
    return out;
  }

  var _cache = {};

  function fetchChannels(tid) {
    var now = Date.now() / 1000;
    if (_cache[tid] && now - _cache[tid].t < CACHE_TTL) return Promise.resolve(_cache[tid].v);
    return apiGet("/iptve.php?tid=" + tid + "&app=517")
      .then(function (h) {
        var v = parseChannels(h, tid);
        if (v.length) return v;
        // ★ 双路由回退（#94 对齐 Rust 端）：app 路由 404/空 → 退普通路由（不带 app）
        //   再抓一次。港澳台 gt/wintv123 等分类历史上有「app 路由重 404」的问题，
        //   普通路由作为实时化的保险。
        return apiGet("/iptve.php?tid=" + tid).then(function (h2) {
          return parseChannels(h2, tid);
        });
      })
      .then(function (v) {
        _cache[tid] = { t: now, v: v };
        return v;
      });
  }

  /* ---------------------------------------------------------------- 解密 */
  /**
   * 从一段 JS 赋值块里按变量名取值（不使用 eval —— strict 模式下 eval 的
   * var 不会泄漏到外层作用域，那正是老版本线路解不出来的根因）。
   * 支持 `A = "x"`、`A = B`、`A = "x"."y".split("").reverse().join("")` 三种形态。
   */
  function evalEnv(block) {
    // 先把 "..." .split("").reverse().join("") 就地反转成字面量
    var s = block.replace(/"([^"]*)"\s*\.split\(""\)\s*\.reverse\(\)\s*\.join\(""\)/g,
      function (_, p1) { return '"' + p1.split("").reverse().join("") + '"'; });
    var env = {};
    // 收集所有赋值语句
    var re = /(?:var\s+)?([A-Za-z_$][\w$]*)\s*=\s*([^;]+);/g, m;
    while ((m = re.exec(s))) {
      var name = m[1], expr = m[2].trim();
      if (/^[A-Za-z_$][\w$]*$/.test(expr)) {          // 变量引用
        env[name] = env[expr] !== undefined ? env[expr] : "";
      } else {                                        // 字面量拼接
        var parts = expr.match(/"([^"]*)"/g) || [];
        env[name] = parts.map(function (q) { return q.slice(1, -1); }).join("");
      }
    }
    return env;
  }

  /**
   * 还原播放页全部线路。
   * @returns {Array<string>} urls（失败返回 []）
   */
  function decryptPage(html, tid, id) {
    try {
      var scripts = [], re = /<script[^>]*>([\s\S]*?)<\/script>/g, m;
      while ((m = re.exec(html))) scripts.push(m[1]);

      var assign = null, entSrc = null;
      for (var i = 0; i < scripts.length; i++) {
        var s = scripts[i];
        if (!assign && /var\s+\w+\s*=\s*""\s*;/.test(s) &&
            /split\(""\)\.reverse\(\)/.test(s) && !/function/.test(s)) assign = s;
        if (!entSrc && /function\s+\w+\s*\([^)]*\)\s*\{[^}]*\.reverse\(\)[^}]*\}/.test(s)) entSrc = s;
      }
      if (!assign || !entSrc) return [];

      var mKey = entSrc.match(/\(\s*\w+\s*,\s*(\w+)\s*\)/);
      var mTok = entSrc.match(/"token="\+(\w+)\s*,\s*"token="\+(\w+)/);
      if (!mKey || !mTok) return [];

      var env = evalEnv(assign);
      var KEY = env[mKey[1]], OLD = env[mTok[1]], NEW = env[mTok[2]];
      if (!KEY || !OLD || !NEW) return [];

      var opts = [], or = /<option value="([^"]+)"/g, om;
      while ((om = or.exec(html))) opts.push(om[1]);
      if (!opts.length) return [];

      // 用第 1 条线路的已知明文反推 XOR key
      var s0 = b64d(opts[0].split("").reverse().join(""));
      if (!s0) return [];
      // ★★ 两个候选明文模板都必须试（与 core.py / core.rs 对齐）：
      //   源站对**部分频道**（湖南/广州系、CCTV 付费频道、CETV、部分地方卫视、
      //   港澳台卫视…）的第 1 条线路明文**少了 `&type=.flv`**，
      //   标准模板 base64 后 136 字节而密文只 124 字节 → 长度对不上 → 反推失败，
      //   整个频道一条线路都解不出（表现就是「线路解析失败」）。
      var cands = [
        API + "/eplay2.php?token=" + OLD + "&tid=" + tid + "&id=" + id + "&p=0&type=.flv",
        API + "/eplay2.php?token=" + OLD + "&tid=" + tid + "&id=" + id + "&p=0"
      ];
      var key = null;
      for (var ci = 0; ci < cands.length; ci++) {
        var out0 = b64e(cands[ci]);
        if (s0.length !== out0.length) continue;
        key = [];
        for (var j = 0; j < s0.length; j++) key.push(s0.charCodeAt(j) ^ out0.charCodeAt(j));
        break;
      }
      if (!key) return [];

      var urls = [];
      for (var k = 0; k < opts.length; k++) {
        var st = b64d(opts[k].split("").reverse().join(""));
        if (!st) continue;
        var o2 = "";
        for (var t = 0; t < st.length; t++) o2 += String.fromCharCode(st.charCodeAt(t) ^ key[t % key.length]);
        var raw = b64d(o2);
        if (!raw) continue;
        var fin = raw.split("token=" + OLD).join("token=" + NEW).split(KEY).join("");
        if (/^https?:\/\//.test(fin)) {
          if (fin.indexOf("type=") < 0) fin += "&type=.flv";
          if (urls.indexOf(fin) < 0) urls.push(fin);
        }
      }
      return urls;
    } catch (e) { return []; }
  }

  /* ---------------------------------------------------------------- EPG */
  function parseEpg(html) {
    var out = [], seg = (html.split('id="myEpg"')[1] || "").split("</ul>")[0];
    var re = /<li[^>]*>.*?<span>\s*([0-9]{1,2}:[0-9]{2})\s*([^<]*)<\/span>/g, m;
    while ((m = re.exec(seg))) out.push({ time: m[1], name: m[2].trim() });
    return out;
  }

  /** 抓播放页 -> Promise<{urls, epg}> */
  function fetchPlay(tid, id) {
    return apiGet("/iptve.php?act=play&tid=" + tid + "&id=" + id)
      .then(function (h) {
        return { urls: decryptPage(h, tid, id), epg: parseEpg(h) };
      });
  }

  /**
   * ★ 自愈版抓播放页（三端拉平，#92/#93 对齐 Rust 端）：
   *   抓播放页失败 / 解不出线路（urls 为空）时，带新请求自动重抓一次 ——
   *   源站播放页 token 按 IP/时段轮换 + 服务端临时抽风，重抓即好，代价仅 1~3s。
   *   fetch_play 原版保持单次语义不变，这里包一层重试。
   */
  function fetchPlayRetry(tid, id, retries) {
    retries = (retries === undefined) ? 2 : retries;
    function once() {
      return fetchPlay(tid, id).then(function (res) {
        return res;
      }).catch(function (e) {
        return { urls: [], epg: [], __err: String(e && e.message || e) };
      });
    }
    return once().then(function (first) {
      if (first.urls && first.urls.length) return first;
      return once().then(function (second) {
        // 两次都空 → 返回第二次的（带 __err 说明），调用方据此走跨分类/弹窗。
        if (second.urls && second.urls.length) return second;
        second.__retried = true;
        return second;
      });
    });
  }

  /* ---------------------------------------------------------------- 台标 */
  function logoSrc(name, LOGOS) {
    LOGOS = LOGOS || root.LOGOS || {};
    var n = norm(name);
    if (LOGOS[n]) return LOGOS[n];
    var alias = n.replace(/綜合|综合/, "").replace(/頻道|频道/, "").replace(/HD/ig, "");
    if (LOGOS[alias]) return LOGOS[alias];
    for (var k in LOGOS) {
      if (k.length >= 3 && n.length >= 2 && (n.indexOf(k) >= 0 || k.indexOf(n) >= 0)) return LOGOS[k];
    }
    return "";
  }

  /* ---------------------------------------------------------------- 收藏 */
  var FAV_KEY = "fav";

  function favArr() {
    try { return JSON.parse(localStorage.getItem(FAV_KEY) || "[]"); } catch (e) { return []; }
  }

  function favSave(a) {
    try { localStorage.setItem(FAV_KEY, JSON.stringify(a)); } catch (e) {}
  }

  function isFav(tid, id) {
    return favArr().some(function (f) { return f.tid === tid && f.id === id; });
  }

  function toggleFav(tid, id, name) {
    var a = favArr(), i = -1;
    for (var k = 0; k < a.length; k++) { if (a[k].tid === tid && a[k].id === id) { i = k; break; } }
    if (i >= 0) a.splice(i, 1); else a.unshift({ tid: tid, id: id, name: name });
    favSave(a);
    return i < 0;   // true=已收藏
  }

  /* ---------------------------------------------------------------- 导出 */
  root.TVCore = {
    APP_UA: APP_UA,
    API: API,
    API_MIRRORS: API_MIRRORS,
    CATS: CATS,
    $: $,
    norm: norm,
    b64d: b64d,
    b64e: b64e,
    apiGet: apiGet,
    setApi: setApi,
    getApi: getApi,
    parseChannels: parseChannels,
    fetchChannels: fetchChannels,
    decryptPage: decryptPage,
    parseEpg: parseEpg,
    fetchPlay: fetchPlay,
    fetchPlayRetry: fetchPlayRetry,
    logoSrc: logoSrc,
    favArr: favArr,
    favSave: favSave,
    isFav: isFav,
    toggleFav: toggleFav
  };
})(window);
