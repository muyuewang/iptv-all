# -*- coding: utf-8 -*-
"""注入链探针：打开 CCTV-1 / 央视频 VIP 页，25s 后读页面内部状态。
查：注入是否执行(__wvtInjected)、日志(__wvtLog)、站点错误检测是否命中。"""
import os
import sys
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "desktop"))
sys.path.insert(0, ROOT)
os.chdir(os.path.join(ROOT, "desktop"))

import native_app as na          # noqa: E402

TARGETS = [
    ("CCTV-1", "https://tv.cctv.com/live/cctv1/", {}),
    ("VIP世界地理", "https://www.yangshipin.cn/tv/home?pid=600099637", {}),
]

PROBE = ("JSON.stringify({href:location.href.slice(0,80),"
         "rs:document.readyState,"
         "inj:!!window.__wvtInjected,"
         "nat:!!window.NativeTV,"
         "st:(window.__wvt_state||''),"
         "reason:(window.__wvt_reason||''),"
         "log:(window.__wvtLog||[]).slice(-12),"
         "v:(function(){var vs=document.querySelectorAll('video');"
         "if(!vs.length)return 'NO-VIDEO';"
         "var v=vs[0];return [v.paused?'paused':'playing',v.readyState,"
         "(v.currentSrc||'').slice(0,50)];})()})")


def main():
    app = na.NativeApp()
    inject = app._load_inject()

    def run(i):
        name, url, _ = TARGETS[i]
        print("== %s %s" % (name, url), flush=True)
        app._show_web(url, "")
        watch(name, i, time.time())

    def watch(name, i, t0):
        el = time.time() - t0
        try:
            raw = app.embed._eval_sync(PROBE, timeout=6) if app.embed else None
            info = json.loads(raw) if raw else None
        except Exception as e:
            info = {"err": repr(e)}
        if info:
            print("  [%4.1fs] %s" % (el, json.dumps(info, ensure_ascii=False)),
                  flush=True)
        else:
            print("  [%4.1fs] probe=None" % el, flush=True)
        if el < 26:
            app.root.after(6000, lambda: watch(name, i, t0))
        elif i + 1 < len(TARGETS):
            app._stop_embed(wait=False)
            app.root.after(1500, lambda: run(i + 1))
        else:
            app.root.after(300, app.root.destroy)

    app.root.after(1200, lambda: run(0))
    app.root.mainloop()


if __name__ == "__main__":
    main()
