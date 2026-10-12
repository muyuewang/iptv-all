# -*- coding: utf-8 -*-
"""v2.5 UX 冒烟：控制条(含秒开档位) / Toast 分类组件 / 播放失败卡 / 档位切换落盘

全部走 CDP 驱动 + 截图，不改任何业务状态（失败卡最后会关掉）。
用法：python -u tools/_smoke_v25.py

（原来自带一份 CDP 客户端，已改为复用 tools/tvlab.py —— 见该文件头注释。）
"""
import sys
import time

from tvlab import Verdict, banner, boot_app, cap, page_state, sh, PKG

PORT = 9335          # 与 _bench_start(9336) / _bench_buf(9337) 错开，便于并行排查


def toast_stack(cdp):
    return cdp.json("JSON.stringify({n:document.querySelectorAll('#toasts .tst').length,"
                    "hasBtn:!!document.querySelector('#toasts .tst .act')})")


def main():
    cdp = boot_app(port=PORT)
    if not cdp:
        print("!! 连不上 devtools", flush=True)
        return 2
    v = Verdict()

    banner("1) Toast 分类组件")
    cdp.eval_js(
        "(function(){"
        "toast('已切换到官网源');"
        "toast('线路2卡顿，切换下一条',{type:'warn'});"
        "toast('全部线路均失败',{type:'err',duration:30000,"
        "  action:{label:'重试',fn:function(){window.__retried=1;}}});"
        "return 1;})()", 10)
    time.sleep(0.6)
    t = toast_stack(cdp) or {}
    print("toast stack:", t, flush=True)
    v.check(t.get("n") == 3, "三条 Toast 同时入栈 (n=%s)" % t.get("n"))
    v.check(t.get("hasBtn") is True, "err 型 Toast 带操作按钮 (hasBtn=%s)" % t.get("hasBtn"))
    cap("v25_1_toast.png")
    cdp.eval_js("(function(){clearToasts();return 1;})()", 8)

    banner("2) 播放失败卡 + 原因人话化")
    cdp.eval_js(
        "(function(){"
        "window.__reasonSeen=humanReason('由于播出安排变更，暂不支持播放该时段内容');"
        "openChannel('tv','x','演示频道');"
        "setTimeout(function(){"
        "  setStatus(null);"
        "  showErrCard({title:'该频道暂时无法播放',"
        "    reason:'由于播出安排变更，暂不支持播放该时段内容',"
        "    desc:'已尝试全部 3 条线路',"
        "    btns:[{label:'重试',primary:true,fn:function(){}},"
        "          {label:'手动选线路',fn:function(){}},"
        "          {label:'返回列表',fn:function(){}}]});"
        "},60);"
        "return 'scheduled';})()", 10)
    time.sleep(1.6)
    c = cdp.json(
        "(function(){var c=document.getElementById('errCard');"
        "return JSON.stringify({shown:c.classList.contains('show'),"
        "title:document.getElementById('ecTitle').textContent,"
        "desc:document.getElementById('ecDesc').textContent,"
        "ic:document.getElementById('ecIc').textContent,"
        "btns:document.querySelectorAll('#ecBtns button').length,"
        "reasonMap:window.__reasonSeen});})()") or {}
    print("errCard:", c, flush=True)
    v.check(c.get("shown") is True, "失败卡已显示")
    v.check("无法播放" in (c.get("title") or ""), "卡片标题 = %s" % c.get("title"))
    v.check((c.get("btns") or 0) == 3, "三个操作按钮 (btns=%s)" % c.get("btns"))
    v.check(bool(c.get("reasonMap")), "站点原话能映射成人话 (reasonMap=%s)" % c.get("reasonMap"))
    cap("v25_2_errcard.png")

    banner("3) 控制条（含「秒开/稳定」档位按钮）")
    cdp.eval_js("(function(){closePlayer();return 1;})()", 10)
    time.sleep(0.8)
    cdp.eval_js(
        "(function(){openChannel('tv','x','演示频道');"
        "setTimeout(function(){document.getElementById('ctrl').classList.remove('hide');"
        " setStatus('连接线路1…',true);},50);return 1;})()", 10)
    time.sleep(1.2)
    cap("v25_3_ctrlbar.png")
    b = cdp.json(
        "(function(){var b=document.getElementById('btnTune');"
        "return JSON.stringify({lb:b.querySelector('.lb').textContent,"
        "ic:b.querySelector('.ic').textContent,"
        "spinner:!!document.querySelector('#status .sp')});})()") or {}
    print("botbar:", b, flush=True)
    v.check(bool(b.get("lb")), "档位按钮有文案 (lb=%s)" % b.get("lb"))
    v.check(b.get("spinner") is True, "连接中显示转圈")

    banner("4) 档位切换：网页状态 + 原生桥 + 落盘")
    r = cdp.json(
        "(function(){var before=TUNE;setTune('stable');"
        "var nb=(window.NativeTV&&NativeTV.getTuneProfile)?NativeTV.getTuneProfile():'n/a';"
        "var lb=document.getElementById('btnTune').querySelector('.lb').textContent;"
        "return JSON.stringify({before:before,after:TUNE,native:nb,btn:lb,"
        "ls:localStorage.getItem('tv_tune')});})()", 12) or {}
    print("tune->stable:", r, flush=True)
    v.check(r.get("after") == "stable", "网页档位 → stable (after=%s)" % r.get("after"))
    v.check(r.get("native") == "stable", "原生桥同步到 stable (native=%s)" % r.get("native"))
    v.check(r.get("ls") == "stable", "localStorage 落盘 (ls=%s)" % r.get("ls"))
    time.sleep(0.5)
    cap("v25_4_tune_stable.png")

    r = cdp.json(
        "(function(){setTune('fast');"
        "var nb=(window.NativeTV&&NativeTV.getTuneProfile)?NativeTV.getTuneProfile():'n/a';"
        "return JSON.stringify({after:TUNE,native:nb,ls:localStorage.getItem('tv_tune')});})()",
        12) or {}
    print("tune->fast:", r, flush=True)
    v.check(r.get("after") == "fast" and r.get("native") == "fast",
            "切回 fast 两端一致 (web=%s native=%s)" % (r.get("after"), r.get("native")))

    banner("5) 收尾：回列表，确认无残留")
    cdp.eval_js("(function(){closePlayer();return 1;})()", 10)
    time.sleep(2.0)
    st = page_state(cdp)
    print("after close:", st, flush=True)
    v.check("show" not in (st.get("pcls") or ""), "播放层已关闭 (pcls=%s)" % st.get("pcls"))
    v.check(st.get("card", "").find("show") < 0, "失败卡已收 (card=%s)" % st.get("card"))
    v.check((st.get("listN") or 0) > 0, "列表已恢复 (listN=%s)" % st.get("listN"))

    # ★ Toast 判据要「等它自然消失」，不能一锤定音。
    #   本脚本第 3 步用假频道 openChannel 会触发真实的兜底失败链，它的提示是
    #   异步落地的 —— 可能在 closePlayer() 之后才弹出来（实测踩到 toasts=1）。
    #   但真实路径（`_probe_exit_toast.py` 实测：起播转发 BACK / 全线路失败后 BACK）
    #   残留都是 0。所以判据应该是「提示会自行到期」，而不是「此刻必须为 0」：
    #   瞬态提示 2.2~5.2s 内自己走掉 → PASS；真泄漏则永远不消失 → FAIL。
    last, txt = -1, []
    for _ in range(16):                      # 最多等 8s
        d = cdp.json("(function(){var e=document.querySelectorAll('#toasts .tst');"
                     "var o=[];for(var i=0;i<e.length;i++)o.push(e[i].textContent);"
                     "return JSON.stringify({n:e.length,t:o});})()") or {}
        last, txt = d.get("n", 0), d.get("t") or []
        if last == 0:
            break
        time.sleep(0.5)
    v.check(last == 0, "Toast 自行到期无泄漏（等待后 n=%s%s）"
            % (last, " " + repr(txt) if txt else ""))
    cap("v25_5_after_close.png")

    rc = v.done()
    sh(["shell", "am", "force-stop", PKG])
    print("=== DONE ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
