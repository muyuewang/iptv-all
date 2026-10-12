# -*- coding: utf-8 -*-
"""EME 密钥系统可用性探测 v2：结果增量写 window slot，轮询读取。"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "desktop"))
sys.path.insert(0, ROOT)
os.chdir(os.path.join(ROOT, "desktop"))

import native_app as na  # noqa: E402

PROBE = r"""
(function(){
  if (window.__eme) return 'already';
  var out = window.__eme = {ua: navigator.userAgent.slice(-40), has_api: !!navigator.requestMediaKeySystemAccess};
  function test(name, ktype) {
    if (!navigator.requestMediaKeySystemAccess) {
      out[name] = 'API不存在'; return Promise.resolve();
    }
    var cfg = [
      {initDataType:'cenc', videoCapabilities:[
          {contentType:'video/mp4;codecs="avc1.64001f"'}]},
      {initDataType:'keyids', videoCapabilities:[
          {contentType:'video/webm;codecs="vp9"'}]}
    ];
    return navigator.requestMediaKeySystemAccess(ktype, cfg)
      .then(function(a){ return a.createMediaKeys().then(function(mk){
          out[name] = 'OK session=' + !!mk; }); })
      .catch(function(e){ out[name] = 'NO:' + (e.name||'') + ' ' +
          String(e.message||'').slice(0,80); });
  }
  test('clearkey', 'org.w3.clearkey')
    .then(function(){ return test('playready', 'com.microsoft.playready'); })
    .then(function(){ return test('widevine', 'com.widevine.alpha'); })
    .then(function(){ out.done = true; })
    .catch(function(e){ out.err = String(e); out.done = true; });
  return 'fired';
})()
"""

POLL = "(function(){return JSON.stringify(window.__eme||null);})()"


def main():
    app = na.NativeApp()
    finished = {"v": False}

    def step():
        if app.embed is None:
            app.embed = na._embed_mod.EmbeddedBrowser(
                na.DATA_DIR, log=app._log,
                schedule=lambda ms, fn: app.root.after(int(ms), fn))
        threading.Thread(target=probe_bg, daemon=True).start()

    def probe_bg():
        emb = app.embed
        # ★ EME 只在安全上下文暴露的可能性 —— 用 HTTPS 页面测，不用 about:blank
        url = os.environ.get("EME_URL", "https://tv.cctv.com/")
        ok = emb.play(url, "{}", "", "",
                      app.video_host.winfo_id(), 640, 360)
        print("play(%s) →" % url, ok, flush=True)
        time.sleep(3)   # 等页面就绪
        sec = emb._eval_sync("(function(){return 'secure='+isSecureContext"
                             "+' host='+location.host})()", timeout=10)
        print("上下文:", sec, flush=True)
        r0 = emb._eval_sync(PROBE, timeout=10)
        print("注入:", r0, flush=True)
        for i in range(15):
            time.sleep(1)
            r = emb._eval_sync(POLL, timeout=6)
            if r and r != "null":
                import json
                try:
                    d = json.loads(r) if isinstance(r, str) else r
                except Exception:
                    continue
                if d.get("done") or d.get("err"):
                    print("EME:", json.dumps(d, ensure_ascii=False), flush=True)
                    break
                # 中间态也打出来
                print("  …", json.dumps(d, ensure_ascii=False), flush=True)
        else:
            print("EME 探测超时（最后 err=%s）" % getattr(emb, "last_eval_err", "?"),
                  flush=True)
        finished["v"] = True

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
