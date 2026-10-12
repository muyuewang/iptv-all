"""诊断：直接连到「官网源网页线路」那个覆盖层 WebView，抓它自己发的网络请求。

回答三个问题（一次定性 v2.9 提取链 403 的原因）：
  1) 页面自己请求了哪些 m3u8？响应码是多少？
  2) 它对 m3u8 / ts 分片的响应是不是 200（= 页面真的在拿到数据）？
  3) 它请求 m3u8 时**实际带了哪些请求头**（Network.requestWillBeSent 的 headers，
     这是权威版本 —— shouldInterceptRequest.getRequestHeaders() 只给一部分）。

用法： python tools/_diag_wvt_m3u8.py [频道关键字]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tvlab import CDP, boot_app, find_ws_url, open_wvt, sh  # noqa: E402

KW = sys.argv[1] if len(sys.argv) > 1 else "广东"
# 官方页 WebView 的调试端口（不能和 player.html 的 9334 撞）
PV_PORT = int(os.environ.get("TV_PV_PORT", "9336"))


def main():
    sh(["start-server"])
    sh(["wait-for-device"])
    cdp = boot_app()
    open_wvt(cdp, delay=5.0)
    found = cdp.eval_js(
        "(function(){var L=(window.WVT&&WVT.list)||[];"
        "for(var i=0;i<L.length;i++){if(L[i].name.indexOf(%s)>=0){"
        "var c=L[i];window.openChannel('wvt',c.id,c.name);return c.name;}}"
        "return 'none';})()" % json.dumps(KW), 12)
    print("openChannel ->", found, flush=True)
    time.sleep(6.0)

    url = find_ws_url(port=PV_PORT, match="gdtv")
    if not url:
        url = find_ws_url(port=PV_PORT, match="http")
    print("pageView ws ->", url, flush=True)
    if not url:
        print("!! 没找到官方页 WebView 的调试 target（是不是没 setWebContentsDebuggingEnabled？）")
        return 1
    pv = CDP(url)

    # 页面基本信息 + 它请求过的资源（不需要 Network 域，历史就在）
    info = pv.eval_js(
        "(function(){var r=performance.getEntriesByType('resource');var o=[];"
        "for(var i=0;i<r.length;i++){var n=r[i].name;"
        "if(n.indexOf('m3u8')>=0||n.indexOf('.ts')>=0){"
        "o.push({n:n.slice(0,120),size:r[i].transferSize||r[i].encodedBodySize||0,"
        "dur:Math.round(r[i].duration)});}}"
        "var v=document.querySelector('video');"
        "return JSON.stringify({url:location.href,cookie:document.cookie.slice(0,160),"
        "vSrc:v?String(v.currentSrc||v.src).slice(0,80):'-',"
        "vRS:v?v.readyState:-1,vCT:v?Math.round(v.currentTime*10)/10:-1,"
        "n:r.length,media:o.slice(0,14)});})()", 20)
    print("\n--- 页面资源历史 ---")
    print(info, flush=True)

    # 打开网络域，观察接下来的请求（HLS 会周期刷新 playlist + 持续拉分片）
    pv.ws.send({"id": 9001, "method": "Network.enable", "params": {}})
    print("\n--- Network 事件（22s）---", flush=True)
    t0 = time.time()
    seen = []
    while time.time() - t0 < 22:
        try:
            msg = json.loads(pv.ws.recv(timeout=2))
        except Exception:
            continue
        m = msg.get("method")
        if m == "Network.requestWillBeSent":
            r = msg["params"]["request"]
            u = r.get("url", "")
            if "m3u8" in u or "/live/" in u or ".ts" in u.split("?")[0]:
                hd = r.get("headers") or {}
                seen.append(("REQ ", u[:110], dict((k, str(v)[:48]) for k, v in hd.items())))
                print("REQ  %s" % u[:130], flush=True)
                for k, v in hd.items():
                    print("       %-22s %s" % (k, str(v)[:90]), flush=True)
        elif m == "Network.responseReceived":
            r = msg["params"]["response"]
            u = r.get("url", "")
            if "m3u8" in u or "/live/" in u or ".ts" in u.split("?")[0]:
                print("RESP %s -> %s  (%s)" % (u[:110], r.get("status"),
                                               r.get("mimeType", "")), flush=True)
        if len([x for x in seen if x[0] == "REQ "]) >= 6:
            break

    if not seen:
        print("(22s 内没有新的 m3u8/ts 请求 —— 页面可能没在播)", flush=True)
    pv.close()
    sh(["shell", "am", "force-stop", "com.clean.tv"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
