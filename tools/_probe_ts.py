# -*- coding: utf-8 -*-
"""CCTV-1 .ts 分片失败探针（文档级注入版）。

探针注册为 AddScriptToExecuteOnDocumentCreated（独立 id，不随换台覆盖），
每次页面加载/重载都会执行：等 5s 采集 → fetch 测最后一个 .ts → XHR 复测。
结果写 window.__ts_probe，Python 侧轮询读取。
"""
import os
import sys
import time
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "desktop"))
sys.path.insert(0, ROOT)
os.chdir(os.path.join(ROOT, "desktop"))

import native_app as na  # noqa: E402

PROBE = r"""
(function(){
  if (window.__ts_probe_ran) return;
  window.__ts_probe_ran = true;
  window.__ts_probe = null;
  window.__ts_probe_done = false;
  setTimeout(function(){
    var out = {ua: navigator.userAgent, ref: document.referrer || '(none)',
               href: location.href, ts: [], fetch: null, xhr: null,
               readyState: document.readyState};
    try {
      var rs = performance.getEntriesByType('resource');
      var tss = rs.filter(function(r){return /\.ts(\?|$)/.test(r.name);});
      out.tsCount = tss.length;
      out.m3u8 = rs.filter(function(r){return /m3u8/.test(r.name);})
                   .slice(-3).map(function(r){
                     return {u: r.name, dur: Math.round(r.duration),
                             size: r.transferSize, st: r.responseStatus,
                             proto: r.nextHopProtocol};
                   });
      out.ts = tss.slice(-5).map(function(r){
        return {u: r.name.slice(-80), dur: Math.round(r.duration),
                size: r.transferSize, enc: r.encodedBodySize};
      });
      out.other = rs.filter(function(r){
                      return !/\.ts(\?|$)|m3u8/.test(r.name)
                          && /cctv|yangshipin|migu/i.test(r.name);})
                    .slice(-8).map(function(r){
                      return {u: r.name.slice(-90), st: r.responseStatus,
                              size: r.transferSize, dur: Math.round(r.duration)};
                    });
    } catch(e) { out.tsErr = String(e); }
    var last = null;
    try {
      var rs2 = performance.getEntriesByType('resource');
      var tss2 = rs2.filter(function(r){return /\.ts(\?|$)/.test(r.name);});
      if (tss2.length) last = tss2[tss2.length-1].name;
    } catch(e) {}
    if (!last) {
      // 没有 .ts 可测 → 改测 m3u8（同样能暴露状态码/CORS）
      try {
        var rs3 = performance.getEntriesByType('resource');
        var m3 = rs3.filter(function(r){return /m3u8/.test(r.name);});
        if (m3.length) last = m3[m3.length-1].name;
      } catch(e) {}
    }
    out.lastUrl = last;
    window.__ts_probe = out;   // 先落盘同步部分
    function done(){ window.__ts_probe_done = true; }
    if (!last) { out.note = 'no-ts-entry'; done(); return; }
    var ctrl = new AbortController();
    var t = setTimeout(function(){ try{ctrl.abort();}catch(e){} }, 8000);
    fetch(last, {signal: ctrl.signal, credentials: 'include'})
      .then(function(r){
        clearTimeout(t);
        return r.arrayBuffer().then(function(b){
          out.fetch = {status: r.status, len: b.byteLength,
                       ct: r.headers.get('content-type')};
          try {
            var x = new XMLHttpRequest();
            x.open('GET', last, true);
            x.timeout = 8000;
            x.onload = function(){ out.xhr = {status: x.status, len: (x.response||'').byteLength}; done(); };
            x.onerror = function(){ out.xhr = {error: 'onerror'}; done(); };
            x.ontimeout = function(){ out.xhr = {error: 'timeout'}; done(); };
            x.responseType = 'arraybuffer';
            x.send();
          } catch(e) { out.xhr = {error: String(e)}; done(); }
        });
      })
      .catch(function(e){
        clearTimeout(t);
        out.fetch = {error: String(e && e.message || e)};
        done();
      });
  }, 5000);
})()
"""

POLL = ("(function(){var o=window.__ts_probe;"
        "return JSON.stringify({done:!!window.__ts_probe_done,"
        "href:location.href,title:document.title,data:o||null});})()")


def main():
    import threading
    app = na.NativeApp()
    finished = {"v": False}

    def pick():
        chs = app.wvt_channels
        return next((x for x in chs if x["name"].startswith("CCTV-1")
                     and "综合" in x["name"]), chs[0] if chs else None)

    def step():
        try:
            _wvt = na._wvt
            chans = _wvt.load_channels(cache_dir=".")
            ads = _wvt.load_adapters(cache_dir=".")
            app._after_wvt_load(chans, ads)
        except Exception as e:
            print("预载失败:", e)
        app._set_src("wvt")
        app.root.after(500, open_ch)

    def open_ch():
        c = pick()
        if not c:
            print("找不到 CCTV-1"); finished["v"] = True; return
        print("打开频道:", c["name"])
        # 预创建 embed，把探针注册成文档级脚本 —— 赶在 play() 导航之前
        if app.embed is None:
            app.embed = na._embed_mod.EmbeddedBrowser(
                na.DATA_DIR, log=app._log,
                schedule=lambda ms, fn: app.root.after(int(ms), fn))
        app.embed.register_extra(PROBE)
        app.root.after(300, do_open)

    def do_open():
        c = pick()
        app._open_channel(c.get("tid", "wvt"), c["id"], c["name"])
        # ★ 轮询放后台线程 —— _eval_sync 的等待泵绝不能跑在 Tk 回调里
        threading.Thread(target=poll_bg, daemon=True).start()

    def poll_bg():
        time.sleep(25)   # 文档加载 5s 后采集 + fetch/xhr ~16s 上限
        emb = app.embed
        for n in range(16):
            r = emb._eval_sync(POLL, timeout=8)
            if r is None:
                print("poll#%d eval=None err=%s" % (
                    n, getattr(emb, "last_eval_err", "?")), flush=True)
                time.sleep(1.5)
                continue
            try:
                d = json.loads(r) if isinstance(r, str) else r
            except Exception:
                d = {"data": None}
            data = d.get("data")
            waiting = (not data) or (not d.get("done"))
            if waiting:
                time.sleep(1.5)
                continue
            report(d)
            break
        else:
            print("轮询窗口耗尽（最后状态见上）", flush=True)
        finished["v"] = True

    def report(d):
        print("=" * 72)
        try:
            print("当前 href:", d.get("href"), "| title:", d.get("title"),
                  "| 探针完成:", d.get("done"))
            data = d.get("data")
            if not data:
                print("数据未落盘（探针没随文档执行？）")
                return
            print("UA:", data.get("ua"))
            print("ref:", data.get("ref"))
            print("探针时 href:", data.get("href"), "| readyState:",
                  data.get("readyState"))
            print("m3u8 最近 3 条:")
            for t in data.get("m3u8", []):
                print("   st=%s size=%s dur=%sms proto=%s %s" % (
                    t.get("st"), t.get("size"), t.get("dur"),
                    t.get("proto"), t.get("u")))
            print("其他 cctv 相关请求:")
            for t in data.get("other", []):
                print("   st=%s size=%s dur=%sms %s" % (
                    t.get("st"), t.get("size"), t.get("dur"), t.get("u")))
            print(".ts 总数:", data.get("tsCount"), "最近 5 条:")
            for t in data.get("ts", []):
                print("   dur=%sms size=%s enc=%s %s" % (
                    t.get("dur"), t.get("size"), t.get("enc"), t.get("u")))
            print("fetch 直连:", json.dumps(data.get("fetch"),
                                            ensure_ascii=False)[:400])
            print("xhr:", json.dumps(data.get("xhr"), ensure_ascii=False))
            if data.get("note"):
                print("note:", data.get("note"))
            if data.get("tsErr"):
                print("tsErr:", data.get("tsErr"))
        except Exception as e:
            print("报告解析失败:", e)
        print("=" * 72)

    def done():
        app.root.after(200, app.root.destroy)

    def done():
        pass  # 轮询线程结束后由 check 销毁窗口

    def check():
        if finished["v"]:
            app.root.after(200, app.root.destroy)
        else:
            app.root.after(300, check)

    app.root.after(1200, step)
    app.root.after(1500, check)
    app.root.mainloop()


if __name__ == "__main__":
    main()
