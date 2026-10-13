/* ============================================================================
 * 纯净电视直播 · 官网源适配层（JS 版）—— 安卓 WebView 端
 *
 * 与 shared/wvt.py 逐条对齐：同一份 AES-256-CBC 密钥派生、同一套镜像回退、
 * 同一组内置快照。改任一版都要同步另一版。
 *
 * 数据来源：github.com/hxh19950701/WebViewTvLive 的 data/4/*.dat
 *   ┌ server_config.dat  加密 JSON：UA / 代理列表
 *   ├ full.dat           加密 + gzip JSON：904 个频道、1114 条线路
 *   └ webpage_adapter.dat 加密 JSON：官网页面「找 video / 找播放按钮」的适配规则
 *
 * 加密方案（逆向自 APK 的 com.vasthread.webviewtv.app.LiveApplication）：
 *   key = reverse("com.vasthread.webviewtv.app.LiveApplication".slice(0,32))
 *   iv  =        "com.vasthread.webviewtv.app.LiveApplication".slice(0,16)
 *   AES-256-CBC -> gzip -> JSON
 *
 * 依赖：无（AES 自带；gzip 用浏览器原生 DecompressionStream，缺失则只用内置快照）
 * ==========================================================================*/
(function (root) {
  "use strict";

  /* ------------------------------------------------------------ 常量 */
  var APP_CLASS = "com.vasthread.webviewtv.app.LiveApplication";
  var KEY_STR = APP_CLASS.slice(0, 32).split("").reverse().join("");
  var IV_STR = APP_CLASS.slice(0, 16);

  var GH_RAW = "https://raw.githubusercontent.com/hxh19950701/WebViewTvLive/main";
  var DATA_FILES = {
    config: "data/4/server_config.dat",
    channels: "data/4/full.dat",
    adapters: "data/4/webpage_adapter.dat"
  };
  var DEFAULT_PROXIES = [
    "https://ghproxy.net", "https://gh-proxy.com/", "https://ghfast.top/",
    "https://gitdl.cn", "https://ghproxy.cc", "https://dgithub.xyz"
  ];
  var HOST_PROXIES = ["https://raw.gitmirror.com", "https://kkgithub.com"];
  var JSDELIVR_CDNS = ["https://fastly.jsdelivr.net", "https://cdn.jsdelivr.net"];

  var RAW_TTL = 24 * 3600 * 1000;   // 毫秒
  var HTTP_TIMEOUT = 12000;
  var TOTAL_BUDGET = 45000;
  var CACHE_KEY = "wvt_cache_v1";

  var MEDIA_EXT = [".m3u8", ".flv", ".ts", ".mp4", ".mkv", ".mpd"];

  /* ------------------------------------------------------------ AES-256-CBC */
  var SBOX = hexArr(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0b7fd932" +
    "6363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b27509832c1a1b6e" +
    "5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cfd0efaafb434d33854" +
    "5f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2cd0c13ec5f974417c4a77" +
    "e3d645d197360814fdc222a908846eeb814de5e0bdbe0323a0a4906245cc2d3ac6291" +
    "95e479e7c8376d8dd54ea96c56f4ea657aae08ba78252e1ca6b4c6e8dd741f4bbd8b" +
    "8a703eb5664803f60e613557b986c11d9ee1f8981169d98e949b1e87e9ce5528df8ca" +
    "1890dbfe6426841992d0fb054bb16");
  var INV_SBOX = hexArr(
    "52096ad53036a538bf40a39e81f3d7fb7ce339829b2fff87348e4344c4dee9cb547b9" +
    "432a6c2233dee4c950b42fac34e082ea16628d924b2765ba2496d8bd12572f8f66486" +
    "689816d4a45ccc5d65b6926c704850fdedb9da5e154657a78d9d8490d8ab008cbcd30" +
    "af7e45805b8b34506d02c1e8fca3f0f02c1afbd0301138a6b3a9111414f67dcea97f2" +
    "cfcef0b4e67396ac7422e7ad3585e2f937e81c75df6e47f11a711d29c5896fb7620ea" +
    "a18be1bfc563e4bc6d279209adbc0fe78cd5af41fdda8338807c731b11210592780ec" +
    "5f60517fa919b54a0d2de57a9f93c99cefa0e03b4dae2af5b0c8ebbb3c8353996117" +
    "2b047eba77d626e169146355210c7d");
  var RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36];

  function hexArr(h) {
    var a = new Array(h.length >> 1);
    for (var i = 0; i < a.length; i++) a[i] = parseInt(h.substr(i * 2, 2), 16);
    return a;
  }

  function xtime(a) {
    a <<= 1;
    if (a & 0x100) a ^= 0x11b;
    return a & 0xff;
  }

  function gmul(a, b) {
    var r = 0;
    while (b) {
      if (b & 1) r ^= a;
      a = xtime(a);
      b >>= 1;
    }
    return r & 0xff;
  }

  function expandKey(key) {
    var nk = key.length >> 2;
    if (nk !== 4 && nk !== 6 && nk !== 8) throw new Error("AES 密钥长度不支持");
    var nr = nk + 6;
    var w = [];
    for (var i = 0; i < nk; i++) w.push([key[i * 4], key[i * 4 + 1], key[i * 4 + 2], key[i * 4 + 3]]);
    for (var j = nk; j < 4 * (nr + 1); j++) {
      var t = w[j - 1].slice();
      if (j % nk === 0) {
        t = [t[1], t[2], t[3], t[0]];
        t = [SBOX[t[0]], SBOX[t[1]], SBOX[t[2]], SBOX[t[3]]];
        t[0] ^= RCON[(j / nk | 0) - 1];
      } else if (nk > 6 && j % nk === 4) {
        t = [SBOX[t[0]], SBOX[t[1]], SBOX[t[2]], SBOX[t[3]]];
      }
      var prev = w[j - nk];
      w.push([prev[0] ^ t[0], prev[1] ^ t[1], prev[2] ^ t[2], prev[3] ^ t[3]]);
    }
    var rks = [];
    for (var r = 0; r <= nr; r++) {
      var rk = new Array(16);
      for (var c = 0; c < 4; c++) {
        var word = w[r * 4 + c];
        rk[c * 4] = word[0]; rk[c * 4 + 1] = word[1];
        rk[c * 4 + 2] = word[2]; rk[c * 4 + 3] = word[3];
      }
      rks.push(rk);
    }
    return rks;
  }

  function invShiftRows(s) {
    for (var r = 1; r < 4; r++) {
      var row = [s[0 * 4 + r], s[1 * 4 + r], s[2 * 4 + r], s[3 * 4 + r]];
      row = row.slice(-r).concat(row.slice(0, -r));      // 循环右移 r
      for (var c = 0; c < 4; c++) s[c * 4 + r] = row[c];
    }
  }

  function invMixColumns(s) {
    for (var c = 0; c < 4; c++) {
      var o = c * 4;
      var a0 = s[o], a1 = s[o + 1], a2 = s[o + 2], a3 = s[o + 3];
      s[o]     = gmul(a0, 14) ^ gmul(a1, 11) ^ gmul(a2, 13) ^ gmul(a3, 9);
      s[o + 1] = gmul(a0, 9)  ^ gmul(a1, 14) ^ gmul(a2, 11) ^ gmul(a3, 13);
      s[o + 2] = gmul(a0, 13) ^ gmul(a1, 9)  ^ gmul(a2, 14) ^ gmul(a3, 11);
      s[o + 3] = gmul(a0, 11) ^ gmul(a1, 13) ^ gmul(a2, 9)  ^ gmul(a3, 14);
    }
  }

  function decryptBlock(blk, rks, nr) {
    var s = new Array(16);
    for (var i = 0; i < 16; i++) s[i] = blk[i] ^ rks[nr][i];
    for (var rnd = nr - 1; rnd > 0; rnd--) {
      invShiftRows(s);
      for (var k = 0; k < 16; k++) s[k] = INV_SBOX[s[k]];
      for (var k2 = 0; k2 < 16; k2++) s[k2] ^= rks[rnd][k2];
      invMixColumns(s);
    }
    invShiftRows(s);
    for (var k3 = 0; k3 < 16; k3++) s[k3] = INV_SBOX[s[k3]];
    for (var k4 = 0; k4 < 16; k4++) s[k4] ^= rks[0][k4];
    return s;
  }

  /** 字节数组 -> 字符串。频道名是 UTF-8（含中文），必须按 UTF-8 解码。 */
  function bytesToStr(bytes) {
    if (typeof root.TextDecoder === "function") {
      return new root.TextDecoder("utf-8").decode(bytes);
    }
    // 兜底（WebView < 4.4，实际不会走到）：手工处理 UTF-8
    var s = "", i = 0, n = bytes.length;
    while (i < n) {
      var c = bytes[i++], cp;
      if (c < 0x80) cp = c;
      else if (c < 0xe0) cp = ((c & 0x1f) << 6) | (bytes[i++] & 0x3f);
      else if (c < 0xf0) cp = ((c & 0x0f) << 12) | ((bytes[i++] & 0x3f) << 6) | (bytes[i++] & 0x3f);
      else {
        cp = ((c & 0x07) << 18) | ((bytes[i++] & 0x3f) << 12) |
             ((bytes[i++] & 0x3f) << 6) | (bytes[i++] & 0x3f);
        cp -= 0x10000;
        s += String.fromCharCode(0xd800 + (cp >> 10), 0xdc00 + (cp & 0x3ff));
        continue;
      }
      s += String.fromCharCode(cp);
    }
    return s;
  }

  function strToBytes(str) {
    var b = new Uint8Array(str.length);
    for (var i = 0; i < str.length; i++) b[i] = str.charCodeAt(i) & 0xff;
    return b;
  }

  var _keyCache = null;
  function aesCbcDecrypt(data, keyStr, ivStr) {
    if (!_keyCache) _keyCache = { rks: null, nr: 0, iv: null };
    if (!_keyCache.rks) {
      var kb = strToBytes(keyStr);
      var r = expandKey(kb);
      _keyCache.rks = r;
      _keyCache.nr = r.length - 1;
      _keyCache.iv = strToBytes(ivStr);
    }
    var rks = _keyCache.rks, nr = _keyCache.nr;
    var n = data.length - (data.length % 16);
    if (!n) throw new Error("密文长度非法");
    var out = new Uint8Array(n);
    var prev = _keyCache.iv;
    for (var off = 0; off < n; off += 16) {
      var blk = data.subarray(off, off + 16);
      var dec = decryptBlock(blk, rks, nr);
      for (var i = 0; i < 16; i++) out[off + i] = dec[i] ^ prev[i];
      prev = blk;
    }
    var pad = out[n - 1];
    if (pad >= 1 && pad <= 16) {
      var ok = true;
      for (var p = n - pad; p < n; p++) { if (out[p] !== pad) { ok = false; break; } }
      if (ok) return out.subarray(0, n - pad);
    }
    return out;
  }

  /* ------------------------------------------------------------ gzip */
  function gunzip(bytes) {
    if (typeof root.DecompressionStream === "function" && root.Response) {
      try {
        var ds = new root.DecompressionStream("gzip");
        var w = ds.writable.getWriter();
        w.write(bytes);
        w.close();
        return new root.Response(ds.readable).arrayBuffer().then(function (ab) {
          return new Uint8Array(ab);
        });
      } catch (e) { /* 落到下面的返回 */ }
    }
    return Promise.reject(new Error("当前 WebView 不支持流式解压（需 Chrome 80+）"));
  }

  /** .dat -> JSON 对象 */
  function decryptDatToJson(buf) {
    var raw = aesCbcDecrypt(new Uint8Array(buf), KEY_STR, IV_STR);
    // gzip magic
    if (raw[0] === 0x1f && raw[1] === 0x8b) {
      return gunzip(raw).then(function (plain) {
        return JSON.parse(bytesToStr(plain));
      });
    }
    return Promise.resolve(JSON.parse(bytesToStr(raw)));
  }

  /* ------------------------------------------------------------ 下载（多镜像） */
  function candidates(rel) {
    var urls = [], seen = {};
    function add(u) { if (u && !seen[u]) { seen[u] = 1; urls.push(u); } }
    var path = GH_RAW.replace("https://raw.githubusercontent.com", "") + "/" + rel;
    for (var i = 0; i < HOST_PROXIES.length; i++) add(HOST_PROXIES[i] + path);
    add(GH_RAW + "/" + rel);
    for (var j = 0; j < DEFAULT_PROXIES.length; j++) {
      var p = DEFAULT_PROXIES[j];
      add((p.charAt(p.length - 1) === "/" ? p.slice(0, -1) : p) + "/" + GH_RAW + "/" + rel);
    }
    for (var k = 0; k < JSDELIVR_CDNS.length; k++) {
      add(JSDELIVR_CDNS[k] + "/gh/hxh19950701/WebViewTvLive@main/" + rel);
    }
    return urls;
  }

  function fetchTimeout(url, ms) {
    if (typeof AbortController !== "function") return fetch(url);
    var ac = new AbortController();
    var timer = setTimeout(function () { ac.abort(); }, ms);
    return fetch(url, { signal: ac.signal, cache: "no-store" }).then(function (r) {
      clearTimeout(timer);
      return r;
    }, function (e) {
      clearTimeout(timer);
      throw e;
    });
  }

  /** 依次尝试镜像，返回 ArrayBuffer */
  function download(rel, proxies) {
    var list = candidates(rel);
    if (proxies && proxies.length) {
      var extra = [];
      for (var i = 0; i < proxies.length; i++) {
        extra.push(proxies[i].replace(/\/$/, "") + "/" + GH_RAW + "/" + rel);
      }
      list = extra.concat(list);
    }
    var deadline = Date.now() + TOTAL_BUDGET;
    var idx = 0;
    function next() {
      if (idx >= list.length || Date.now() > deadline) {
        return Promise.reject(new Error("所有镜像均不可用"));
      }
      var u = list[idx++];
      var left = deadline - Date.now();
      if (left <= 1500) return Promise.reject(new Error("超时"));
      return fetchTimeout(u, Math.min(HTTP_TIMEOUT, left)).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.arrayBuffer();
      }).then(function (ab) {
        if (!ab || ab.byteLength < 64) throw new Error("响应过短");
        return { data: ab, url: u };
      }).catch(function () { return next(); });
    }
    return next();
  }

  /* ------------------------------------------------------------ 本地缓存 */
  function readCache() {
    try {
      var o = JSON.parse(localStorage.getItem(CACHE_KEY) || "null");
      if (o && o.t && Date.now() - o.t < RAW_TTL && o.channels && o.channels.length) return o;
    } catch (e) { }
    return null;
  }

  function writeCache(obj) {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(obj)); } catch (e) { }
  }

  /* ------------------------------------------------------------ 线路优先级（v2.10.24 修正）
     v2.10.9 依据一条「tv.cctv.com 官网直播页起播要等很久」的用户观察，把央视频
     提到第一、把央视网直播页压到兜底。2026-10 在模拟器上按「冷/热各两次」重新
     实测 CCTV-1 三条线路，结论**反转**：
       · tv.cctv.com/live/cctv1/            4561 / 3248ms  ← 最快
       · www.yangshipin.cn/tv/home?pid=...  5618~6297 / 5510ms
       · m-live.cctvnews.cctv.com/...       7435 / 6538ms  ← 最慢
     原因在「页面 ready → 出画」这一段：央视网直播页重（页面 ready 要 3.0s），
     但用的是老 HLS 播放器，ready 后 1.5s 就出画；央视频是 SPA + WASM/MSE，
     页面 ready 只要 1.4s，之后却还要再等 4.1s。合计央视网快 1.5~2s。
     新序：tv.cctv.com(0) > 央视频(1) > 其它(2) > 移动直播间(3)。
     只排序不删除 —— 播不动照旧由 pageFailed / 看门狗换下一条。 */
  function urlRank(u) {
    var s = String(u || "");
    if (s.indexOf("tv.cctv.com") >= 0) return 0;
    if (s.indexOf("yangshipin") >= 0) return 1;
    if (s.indexOf("cctvnews") >= 0 || s.indexOf("liveRoom") >= 0) return 3;
    return 2;
  }
  function normalizeChannel(c) {
    if (!c || !c.urls || c.urls.length < 2) return c;
    var arr = [];
    for (var i = 0; i < c.urls.length; i++) arr.push({ u: c.urls[i], r: urlRank(c.urls[i].url), i: i });
    arr.sort(function (a, b) { return (a.r - b.r) || (a.i - b.i); });
    var out = [];
    for (var k = 0; k < arr.length; k++) out.push(arr[k].u);
    return { group: c.group, name: c.name, urls: out };
  }
  function normalizeChannels(chans) {
    if (!chans || !chans.length) return chans;
    var out = [];
    for (var i = 0; i < chans.length; i++) out.push(normalizeChannel(chans[i]));
    return out;
  }

  /* ------------------------------------------------------------ 快照 -> 频道表 */
  function parseSnapshot(snap) {
    if (!snap) return { channels: [], adapters: [] };
    var out = [];
    var rows = snap.channels || [];
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (!r || r.length < 3) continue;
      var group = r[0] || "其他", name = r[1], urls = r[2] || [], extras = r[3] || null;
      var list = [];
      for (var j = 0; j < urls.length; j++) {
        list.push({ url: urls[j], extra: (extras && extras[j]) || "" });
      }
      if (name && list.length) out.push({ group: group, name: name, urls: list });
    }
    return { channels: out, adapters: snap.adapters || [] };
  }

  /* ------------------------------------------------------------ 对外 API */
  var CORE = {
    APP_CLASS: APP_CLASS,
    DATA_FILES: DATA_FILES,
    DEFAULT_PROXIES: DEFAULT_PROXIES,
    KEY_STR: KEY_STR,
    IV_STR: IV_STR,

    /** 密钥自检：与 Python 版必须完全一致 */
    keyInfo: function () {
      return {
        key: KEY_STR,
        keyLen: KEY_STR.length,
        iv: IV_STR,
        aes: "AES-256-CBC"
      };
    },

    /** 直接可播的直链？（m3u8/flv/ts/mp4…） */
    isDirect: function (url) {
      if (!url) return false;
      var u = url.split("#")[0].toLowerCase();
      for (var i = 0; i < MEDIA_EXT.length; i++) {
        if (u.indexOf(MEDIA_EXT[i]) >= 0) return true;
      }
      return false;
    },

    /** 内置快照（离线可用） */
    snapshot: function () {
      return parseSnapshot(root.WVT_SNAPSHOT);
    },

    /** 取适配规则里匹配某个 URL 域名的那条 */
    matchAdapter: function (url, adapters) {
      adapters = adapters || (root.WVT_SNAPSHOT && root.WVT_SNAPSHOT.adapters) || [];
      if (!url) return null;
      var host = String(url).replace(/^https?:\/\//i, "").split("/")[0].toLowerCase();
      for (var i = 0; i < adapters.length; i++) {
        var key = String(adapters[i].url || "").toLowerCase();
        if (!key) continue;
        if (host === key || host.indexOf("." + key) >= 0 || host.indexOf(key) >= 0) {
          return adapters[i];
        }
      }
      return null;
    },

    /** 默认兜底适配规则（找不到时用这个） */
    defaultAdapter: function () {
      return {
        url: "",
        ua: "PC",
        videoElementCondition: "e.tagName === 'VIDEO'",
        needEnterFullscreenButton: false,
        enterFullscreenButtonCondition: "true",
        needPlayButton: false,
        playButtonCondition: "true",
        needPreClick: false,
        preClickElementCondition: "true",
        needPreClick2: false,
        preClickElementCondition2: "true"
      };
    },

    /**
     * 载入频道表。
     * 顺序：本地缓存 -> 内置快照（立刻可用）-> 后台联网刷新
     * @returns {Promise<{channels, adapters, from}>}
     */
    load: function (opts) {
      opts = opts || {};
      var self = this;
      return Promise.resolve().then(function () {
        var cached = opts.force ? null : readCache();
        if (cached) {
          return { channels: cached.channels, adapters: cached.adapters || [], from: "cache" };
        }
        var snap = self.snapshot();
        if (snap.channels.length) {
          if (opts.refresh) self.refresh();          // 后台悄悄更新
          return { channels: snap.channels, adapters: snap.adapters, from: "builtin" };
        }
        return self.refresh();
      }).then(function (res) {
        /* v2.10.9：出口统一按线路优先级重排（详见 normalizeChannel 注释） */
        res.channels = normalizeChannels(res.channels);
        return res;
      });
    },

    /** 联网拉取并解密最新频道表 */
    refresh: function () {
      var self = this;
      return self._loadConfig().then(function (cfg) {
        var proxies = (cfg && cfg.githubProxies) || DEFAULT_PROXIES;
        return download(DATA_FILES.channels, proxies);
      }).then(function (r) {
        return decryptDatToJson(r.data);
      }).then(function (chans) {
        if (!chans || !chans.length) throw new Error("频道表为空");
        var snap = self.snapshot();
        var obj = { t: Date.now(), channels: chans, adapters: snap.adapters || [] };
        writeCache(obj);
        return download(DATA_FILES.adapters).then(function (r2) {
          return decryptDatToJson(r2.data);
        }).then(function (ads) {
          if (ads && ads.length) { obj.adapters = ads; writeCache(obj); }
          return { channels: chans, adapters: obj.adapters, from: "network" };
        }).catch(function () {
          return { channels: chans, adapters: obj.adapters, from: "network" };
        });
      }).then(function (res) {
        res.channels = res.channels.map(function (c) { return c; });
        return res;
      }).catch(function (e) {
        var snap = self.snapshot();
        if (snap.channels.length) {
          return { channels: snap.channels, adapters: snap.adapters, from: "builtin", error: String(e) };
        }
        throw e;
      });
    },

    /** .dat -> JSON（诊断/自检用；与 Python 的 decrypt_dat_json 同一份算法） */
    decryptDatJson: function (arrayBuffer) {
      return decryptDatToJson(arrayBuffer);
    },

    _loadConfig: function () {
      return download(DATA_FILES.config).then(function (r) {
        return decryptDatToJson(r.data);
      }).catch(function () { return { githubProxies: DEFAULT_PROXIES }; });
    }
  };

  root.WVTCore = CORE;

  // 便于排查：把密钥信息打进控制台
  try { console.log("[WVTCore] " + JSON.stringify(CORE.keyInfo()) + " 内置频道 " +
        (CORE.snapshot().channels.length) + " 个"); } catch (e) { }
})(window);
