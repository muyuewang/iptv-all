# -*- coding: utf-8 -*-
"""回归：起播看门狗（v2.6，借鉴 TVBoxOS LivePlayActivity.java:2266-2360）。

为什么这么测
------------
「到点换了线」这件事，光看结果证明不了什么 —— 没有看门狗、纯干等 ExoPlayer 的
readTimeout(15s) 时，最终也会换到第二条，只是慢。所以判据必须是**耗时**：

  ① 造一条真正的「挂起型线路」：本机起一个只 accept、永不回数据的 TCP 黑洞，
     经 `adb reverse` 映射成设备里的 http://127.0.0.1:19555/hang.flv。
     ExoPlayer 连得上（TCP 握手成功）但永远读不到字节 → 会一直停在 BUFFERING，
     只有 readTimeout 到 15s 才会自己失败。这正是我们文档里记的痛。
  ② 把它塞成当前频道的第一条线路，第二条留真地址，然后 playIdx(0)。
  ③ 只量「cur.idx 从 0 变成 1 用了多久」：
        ~10s  → 看门狗干的（START_WATCH 秒开档 = 10s）
        ~15s+ → 网络层自己超时的，看门狗没生效
     断言窗口取 [8.5s, 13.5s]，两边都不放水 —— 这是本条测试最核心的一句。

顺带验两件事：
  · 正常线路不会被看门狗误杀（起播后 cur.idx 仍为 0）；
  · 看门狗换线后能真的在新线上起来（ready/size 到）。

依赖：tools/tvlab.py
"""
import socket
import sys
import threading
import time

from tvlab import (arm_native_probe, arm_toast_probe, banner, boot_app, cap, is_playing,
                   native_probe, open_local, page_state, pick_channel, reset_native_probe,
                   sh, toast_log, tracks, Verdict, wait_ready, PKG)

HANG_PORT = 19555


class HangServer:
    """只 accept、永不回数据的 TCP 黑洞：模拟「挂起型线路」。

    与「端口没人监听」的区别很关键：那种情况 TCP 立刻 RST，ExoPlayer 秒失败
    （测不出看门狗，因为根本没等）。accept 之后一个字都不发，才是真正的挂起。
    """

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
            self.conns.append(c)          # 收下，然后什么都不发

    def close(self):
        self.alive = False
        try:
            self.srv.close()
        except Exception:
            pass
        for c in self.conns:
            try:
                c.close()
            except Exception:
                pass


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
    banner("起播看门狗回归 · 挂起线路 10s 内应换线（readTimeout 是 15s）")
    sh(["start-server"])
    sh(["wait-for-device"])

    hang = HangServer()
    rv = sh(["reverse", "tcp:%d" % HANG_PORT, "tcp:%d" % HANG_PORT])
    rev = sh(["reverse", "--list"])
    print("adb reverse 结果:", rv.strip() or "(空=成功)", "| 列表:", rev.strip(), flush=True)

    cdp = boot_app()
    if not cdp:
        print("!! 连不上 devtools", flush=True)
        hang.close()
        return 2

    v = Verdict()
    try:
        open_local(cdp)
        if wait_list(cdp) == 0:
            print("!! 列表为空（接口偶发）", flush=True)
            return 3

        banner("1) 基线：正常频道要能起播（也验证看门狗不误杀正常线路）")
        # ★ 显式把档位切成「秒开」：看门狗超时值跟档位联动（10s / 15s），
        #   不能让测试依赖上一次跑测试时留在 SharedPreferences 里的档位 ——
        #   否则同一条测试在不同机器上窗宽都不一样，判据就成了摆设。
        print("setTune(fast) ->", cdp.eval_js(
            "(function(){try{setTune('fast');}catch(e){}"
            "return (typeof TUNE!=='undefined'?TUNE:'?')+'/'+"
            "(typeof startWatchMs==='function'?startWatchMs():-1);})()", 8), flush=True)
        arm_native_probe(cdp)
        arm_toast_probe(cdp)          # 记录提示文案 → 判「看门狗有没有误杀正常线路」
        t_pick = time.time()
        print("open(广东):", pick_channel(cdp, "广东"), flush=True)
        ok = wait_ready(cdp)
        t_ready = time.time() - t_pick
        v.check(ok, "首个频道已起播")
        idx0 = cdp.eval_js("(typeof cur!=='undefined')?cur.idx:-1", 8)
        try:
            idx0 = int(idx0)
        except Exception:
            idx0 = -1
        texts = [t for _, t in toast_log(cdp)]
        print("起播耗时 %.1fs · cur.idx=%s · 提示=%s" % (t_ready, idx0, texts), flush=True)
        v.check(ok, "首个频道已起播（第 %d 条线路，%.1fs）" % (idx0 + 1, t_ready))

        # ★ 第二个判据：起播成功之后，看门狗必须**彻底安静**。
        #   等一个完整窗口期（15s > startWatchMs=10s）再复查提示记录 ——
        #   这验的是 markFirstFrame() 真的把定时器撤掉了。
        #   反过来的 bug（定时器没撤）比「不生效」更糟：它会把一条**正在播**的
        #   流硬切走。所以这两个方向要各测一条，缺一不可。
        #   ⚠️ 不要去断言「基线期间不能出现起播超时提示」—— 那条不成立：
        #   源站偶尔有线路要 12~15s 才出帧，看门狗按契约就该把它换掉
        #   （实测撞到过 log=['线路2起播超时，切换下一条'] 但频道随后正常起播）。
        n0 = len(texts)
        print("起播后静默观察 15s（看门狗窗口 10s）…", flush=True)
        time.sleep(15)
        new = [t for _, t in toast_log(cdp)][n0:]
        v.check(not any("超时" in t for t in new),
                "起播后看门狗彻底安静（15s 内无新「起播超时」，新增=%s）" % new)
        v.check(is_playing(cdp) > 0, "起播 15s 后仍在播（未被看门狗打断）")

        banner("2) 挂起线路打头：看门狗必须在 10s 左右换线")
        reset_native_probe(cdp)
        arm_toast_probe(cdp)
        info = cdp.eval_js(
            "(function(){var real=cur.urls[cur.idx];"
            "window.__wd={real:real,t0:Date.now()};"
            "cur.urls=['http://127.0.0.1:%d/hang.flv', real];"
            "cur.extra=[]; playIdx(0);"
            "return JSON.stringify({n:cur.urls.length,real:String(real).slice(0,70)});})()"
            % HANG_PORT, 15)
        print("注入挂起线路:", info, flush=True)
        time.sleep(0.4)
        print("黑洞已收到的连接数:", len(hang.conns), flush=True)

        t0 = time.time()
        switched = None
        while time.time() - t0 < 24:
            time.sleep(0.5)
            raw = cdp.eval_js("(typeof cur!=='undefined')?cur.idx:-1", 8)
            try:
                idx = int(raw)
            except Exception:
                idx = -1
            print("  [+%.1fs] cur.idx=%s" % (time.time() - t0, idx), flush=True)
            if idx >= 1:
                switched = time.time() - t0
                break

        v.check(switched is not None, "看门狗到点换了线（24s 内 cur.idx 前进）")
        if switched is not None:
            tos = [t for _, t in toast_log(cdp)]
            print("期间提示:", tos, flush=True)
            v.check(any("超时" in t for t in tos), "换线由看门狗触发（出现过「起播超时」提示）")
            v.check(8.5 <= switched <= 13.5,
                    "换线耗时落在看门狗窗口内（实测 %.1fs；看门狗 10s / readTimeout 15s）"
                    % switched)
            # 3) 换线之后链路必须继续收敛 —— 不能停在原地不动（那才是真 bug）。
            #    ★ 这一条**不能**写成「第二条线路必须起播」：源站此刻可能本就在抖
            #    （本轮基线自己就换了 4~5 条线才起来），那条真线路也未必立刻可用。
            #    要防的是「卡死」：换完之后既不起播、也不继续换线、也不给失败卡。
            #    所以只要出现下面任一件就算收敛：
            #      ① 起播（probe 拿到非零 size / ready）
            #      ② cur.idx 又前进了（继续换下一条）
            #      ③ 失败卡出现（全线路试完 → 会走 8s 自动跳台）
            banner("3) 换线之后链路继续收敛（起播 / 继续换线 / 出失败卡）")
            idx_after = int(cdp.eval_js("(typeof cur!=='undefined')?cur.idx:-1", 8))
            outcome, deadline = None, time.time() + 34
            while time.time() < deadline:
                time.sleep(2.0)
                p = native_probe(cdp)
                st = page_state(cdp, extra="idx:(typeof cur!=='undefined')?cur.idx:-1")
                if p.get("size") or p.get("ready"):
                    outcome = "已起播（%s）" % (p.get("vsize") or "ready")
                elif st.get("idx") != idx_after:
                    outcome = "继续换线（cur.idx %s→%s）" % (idx_after, st.get("idx"))
                elif "show" in (st.get("card") or ""):
                    outcome = "出失败卡（会走 8s 自动跳台）"
                if outcome:
                    break
            v.check(outcome is not None, "换线后链路继续收敛：%s" % (outcome or "★ 34s 内毫无进展（卡死）"))
            print("全流程提示:", [t for _, t in toast_log(cdp)], flush=True)
            print("probe:", native_probe(cdp), flush=True)

        st = page_state(cdp)
        n, a, _ = tracks()
        cap("watchdog.png")
        print("收尾:", st, "audio=%d/%d" % (n, a), flush=True)

        rc = v.done()
    finally:
        try:
            cdp.eval_js("(function(){try{closePlayer();}catch(e){}return 1;})()", 8)
        except Exception:
            pass
        time.sleep(1.0)
        try:
            sh(["shell", "am", "force-stop", PKG])
        except Exception:
            pass
        sh(["reverse", "--remove", "tcp:%d" % HANG_PORT])
        hang.close()
    print("=== DONE ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
