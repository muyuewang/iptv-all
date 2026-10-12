# -*- coding: utf-8 -*-
"""复用换台探针：复刻 check_wvt_embed 的「首播 → 同组换台」流程，
每 4s 读页面内部（注入标记/日志/状态/注册脚本id），定位复用静默的根因。"""
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

PROBE = ("JSON.stringify({href:location.href.slice(0,70),"
         "rs:document.readyState,"
         "inj:!!window.__wvtInjected,"
         "nat:!!window.NativeTV,"
         "st:(window.__wvt_state||''),"
         "reason:(window.__wvt_reason||''),"
         "log:(window.__wvtLog||[]).slice(-8)})")


def main():
    app = na.NativeApp()
    try:
        _wvt = na._wvt
        chans = _wvt.load_channels(cache_dir=".")
        ads = _wvt.load_adapters(cache_dir=".")
        app._after_wvt_load(chans, ads)
    except Exception as e:
        print("预载失败:", e)
    app._set_src("wvt")

    # 与 check 相同的选台逻辑：广东卫视 → 同组下一个非 4K
    first = next(x for x in app.wvt_channels if x["name"] == "广东卫视")
    alt = None
    for x in app.wvt_channels:
        if x.get("group") == first.get("group") and x["id"] != first["id"] \
                and "4K" not in x["name"].upper():
            alt = x
            break
    def u0(c):
        u = c["urls"][0]
        return (u.get("url") if isinstance(u, dict) else str(u))[:60]

    print("首播:", first["name"], u0(first))
    print("复用:", alt["name"], u0(alt))

    def poll(tag, t0, deadline, next_fn):
        el = time.time() - t0
        raw = None
        try:
            raw = app.embed._eval_sync(PROBE, timeout=5) if app.embed else None
        except Exception as e:
            raw = "ERR:%r" % e
        iid = getattr(app.embed, "_inject_id", None)
        le = getattr(app.embed, "last_eval_err", None)
        try:
            nproc = sum(1 for _ in os.popen(
                'tasklist /FI "IMAGENAME eq msedgewebview2.exe" /NH'))
        except Exception:
            nproc = -1
        print("  [%s %4.1fs] inject_id=%s eval_err=%r wv2proc=%d -> %s" % (
            tag, el, (iid or "None")[:20] if iid else "None",
            le, nproc,
            (raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False))
            if raw else "None"), flush=True)
        if el < deadline:
            app.root.after(4000, lambda: poll(tag, t0, deadline, next_fn))
        else:
            next_fn()

    def step2():
        print("== 复用换台 ==", flush=True)
        app._open_channel(alt["tid"], alt["id"], alt["name"])
        app.root.after(600, lambda: poll("复用", time.time(), 50,
                                         lambda: app.root.after(200, app.root.destroy)))

    def step1():
        app._open_channel(first["tid"], first["id"], first["name"])
        app.root.after(600, lambda: poll("首播", time.time(), 40, step2))

    app.root.after(1000, step1)
    app.root.mainloop()


if __name__ == "__main__":
    main()
