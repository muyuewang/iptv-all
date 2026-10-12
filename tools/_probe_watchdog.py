# -*- coding: utf-8 -*-
"""诊断：起播看门狗为什么没在 10/15s 到点换线。

现象（_test_watchdog.py 实测）：注入挂起线路后 cur.idx 24s 不动，
且 logcat 里没有 readTimeout 的报错 —— 看门狗与网络层超时**双双未触发**。

本探针记录：
  · __onNativeState 的完整事件时序（带时间戳）—— 看是否有「切换瞬间的
    size/ready」把看门狗误清掉（markFirstFrame 被假事件调用）；
  · 每秒采样 __startWatchT / NATIVE.firstFrame / cur.idx —— 看定时器到底
    有没有布上、什么时候没的。
"""
import socket
import sys
import threading
import time

from tvlab import (banner, boot_app, open_local, pick_channel, reset_native_probe,
                    sh, wait_ready, PKG)

HANG_PORT = 19555


class HangServer:
    def __init__(self, port=HANG_PORT):
        self.port = port
        self.conns = []
        self.alive = True
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(8)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.alive:
            try:
                c, _ = self.srv.accept()
            except Exception:
                return
            self.conns.append(c)

    def close(self):
        self.alive = False
        try:
            self.srv.close()
        except Exception:
            pass


NLOG = ("(function(){window.__nlog=[];"
        "if(!window.__nlogW){window.__nlogW=1;var o=window.__onNativeState;"
        "window.__onNativeState=function(s,d){"
        " try{window.__nlog.push([Date.now(),String(s),String(d||'')]);}catch(e){}"
        " if(o) return o(s,d);};}"
        "return 1;})()")

STATE = ("(function(){return JSON.stringify({"
         "idx:(typeof cur!=='undefined')?cur.idx:-9,"
         "ff:(typeof NATIVE!=='undefined')?NATIVE.firstFrame:'?',"
         "armed:!!__startWatchT,"
         "watchMs:(typeof startWatchMs==='function')?startWatchMs():-1,"
         "tune:typeof TUNE!=='undefined'?TUNE:'?',"
         "lab:(window.__lab&&window.__lab.state)||'',"
         "vsize:(window.__lab&&window.__lab.vsize)||'',"
         "nlog:(window.__nlog||[]).slice(-8)"
         "});})()")


def wait_list(cdp, timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        n = cdp.eval_js("document.querySelectorAll('#list .ch').length", 8)
        try:
            if int(n) > 0:
                return int(n)
        except Exception:
            pass
        time.sleep(1.5)
    return 0


def main():
    banner("诊断：看门狗为何不触发")
    sh(["start-server"])
    sh(["wait-for-device"])
    hang = HangServer()
    sh(["reverse", "tcp:%d" % HANG_PORT, "tcp:%d" % HANG_PORT])

    cdp = boot_app()
    if not cdp:
        print("!! 无 devtools", flush=True)
        hang.close()
        return 2
    try:
        open_local(cdp)
        if wait_list(cdp) == 0:
            print("!! 列表空", flush=True)
            return 3
        print("open:", pick_channel(cdp, "广东"), flush=True)
        print("ready:", wait_ready(cdp), flush=True)
        print("typeof armStartWatch =",
              cdp.eval_js("typeof armStartWatch", 8),
              "| startWatchMs =", cdp.eval_js("typeof startWatchMs==='function'?startWatchMs():-1", 8),
              "| TUNE =", cdp.eval_js("typeof TUNE!=='undefined'?TUNE:'?'", 8), flush=True)

        reset_native_probe(cdp)
        cdp.eval_js(NLOG, 8)
        print("注入:", cdp.eval_js(
            "(function(){var real=cur.urls[cur.idx];"
            "cur.urls=['http://127.0.0.1:%d/hang.flv',real];cur.extra=[];"
            "window.__nlog.length=0; playIdx(0);"
            "return JSON.stringify({n:cur.urls.length,savedIdx:real?'ok':'NO'});})()"
            % HANG_PORT, 15), flush=True)
        time.sleep(1.2)
        print("黑洞连接数:", len(hang.conns), flush=True)

        t0 = time.time()
        while time.time() - t0 < 20:
            st = cdp.json(STATE, 8) or {}
            print("[+%4.1fs] idx=%s ff=%s armed=%s watchMs=%s lab=%s vsize=%s"
                  % (time.time() - t0, st.get("idx"), st.get("ff"), st.get("armed"),
                     st.get("watchMs"), st.get("lab"), st.get("vsize")), flush=True)
            time.sleep(2.0)
        print("事件时序:", flush=True)
        for row in (cdp.json(STATE, 8) or {}).get("nlog") or []:
            print("   ", row, flush=True)
    finally:
        try:
            cdp.eval_js("(function(){try{closePlayer();}catch(e){}return 1;})()", 8)
        except Exception:
            pass
        time.sleep(0.8)
        sh(["shell", "am", "force-stop", PKG])
        sh(["reverse", "--remove", "tcp:%d" % HANG_PORT])
        hang.close()
    print("=== DONE ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
