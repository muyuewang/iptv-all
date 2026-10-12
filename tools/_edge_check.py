# -*- coding: utf-8 -*-
"""真机 Edge 对照实验：同一页面在真实 Edge 里能否起播？能否用 Widevine？

用法：python _edge_check.py <url> [等待秒]
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "desktop"))
os.chdir(os.path.join(ROOT, "desktop"))

import cdp  # noqa: E402

VIDEO_Q = ("(function(){var vs=document.getElementsByTagName('video');"
           "if(!vs.length)return 'novideo';"
           "var v=vs[0];"
           "return JSON.stringify({rs:v.readyState,t:Math.round(v.currentTime*10)/10,"
           "p:v.paused,w:v.videoWidth,h:v.videoHeight,"
           "buf:(function(){try{return Math.round((v.buffered.length?"
           "v.buffered.end(v.buffered.length-1):0)*10)/10;}catch(e){return -1}})()});})()")

EME_Q = ("(function(){var out={secure:isSecureContext,"
         "api:!!navigator.requestMediaKeySystemAccess};"
         "if(!out.api)return JSON.stringify(out);"
         "var cfg=[{initDataType:'cenc',videoCapabilities:["
         "{contentType:'video/mp4;codecs=\"avc1.64001f\"'}]}];"
         "return navigator.requestMediaKeySystemAccess('com.widevine.alpha',cfg)"
         ".then(function(){out.widevine='OK';return JSON.stringify(out);})"
         ".catch(function(e){out.widevine='NO:'+e.name;"
         "return JSON.stringify(out);});})()")


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "https://m.gdtv.cn/tvChannelDetail/43"
    wait = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    proc, port, udd = cdp.launch_browser(url, offscreen=True)
    print("Edge pid=%s port=%d" % (proc.pid, port))
    try:
        time.sleep(wait)
        pages = cdp.wait_targets(port, timeout=15)
        if not pages:
            print("没有可用 target"); return
        c = cdp.CDP(pages[0]["webSocketDebuggerUrl"])
        r = c.call("Runtime.evaluate", {"expression": VIDEO_Q,
                                        "returnByValue": True, "awaitPromise": True})
        print("video:", r.get("result", {}).get("result", {}).get("value"))
        r = c.call("Runtime.evaluate", {"expression": EME_Q,
                                        "returnByValue": True, "awaitPromise": True})
        print("EME:", r.get("result", {}).get("result", {}).get("value"))
        c.close()
    finally:
        cdp.kill_browser(proc, udd)


if __name__ == "__main__":
    main()
