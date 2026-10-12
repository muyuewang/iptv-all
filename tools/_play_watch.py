# -*- coding: utf-8 -*-
"""起播观测：打开 CCTV-1，逐 2s 轮询 video 状态 40s，输出时间线。

用法：
  python _play_watch.py                # 基线
  HOSTMAP=1 python _play_watch.py      # 加 --host-resolver-rules 绕 DNS 劫持
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "desktop"))
sys.path.insert(0, ROOT)
os.chdir(os.path.join(ROOT, "desktop"))

if os.environ.get("HOSTMAP"):
    # p.data.cctv.com 被路由器 DNS 劫持成 ::1 → 映射回真实 IPv4
    extra = " --host-resolver-rules=MAP p.data.cctv.com 115.182.216.178"
    old = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = old + extra
    print("已加 host-resolver-rules")

import native_app as na  # noqa: E402

Q = ("(function(){var vs=document.getElementsByTagName('video');"
     "if(!vs.length)return 'novideo@'+location.host;"
     "var v=vs[0];"
     "return JSON.stringify({host:location.host,rs:v.readyState,"
     "t:Math.round(v.currentTime*10)/10, p:v.paused, w:v.videoWidth,"
     "h:v.videoHeight, buf:(function(){try{return Math.round("
     "(v.buffered.length?v.buffered.end(v.buffered.length-1):0)*10)/10;"
     "}catch(e){return -1}})(), state:(window.__wvt_state||'')});})()")


def main():
    import threading
    # 用法：python _play_watch.py [频道名前缀] [频道名包含]
    want = sys.argv[1] if len(sys.argv) > 1 else "CCTV-1"
    contain = sys.argv[2] if len(sys.argv) > 2 else None
    app = na.NativeApp()
    finished = {"v": False}

    def pick():
        chs = app.wvt_channels
        return next((x for x in chs if x["name"].startswith(want)
                     and (contain is None or contain in x["name"])),
                    chs[0] if chs else None)

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
            print("找不到频道", want); finished["v"] = True; return
        print("打开频道:", c["name"], flush=True)
        app._open_channel(c.get("tid", "wvt"), c["id"], c["name"])
        # ★ 观测轮询放后台线程 —— _eval_sync 的等待泵绝不能跑在 Tk 回调里
        threading.Thread(target=watch, daemon=True).start()

    def watch():
        try:
            for n in range(45):
                emb = app.embed
                row = "T+%02ds " % (n * 2)
                if emb is None or not emb.alive():
                    row += "embed 不可用"
                else:
                    r = emb._eval_sync(Q, timeout=6)
                    if r is None:
                        row += "None(%s)" % getattr(emb, "last_eval_err", "?")
                    else:
                        row += str(r)
                print(row, flush=True)
                time.sleep(2)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print("watch 线程异常退出:", e, flush=True)
        finally:
            finished["v"] = True
            print("watch 结束", flush=True)

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
