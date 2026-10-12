# -*- coding: utf-8 -*-
"""通过 CDP 在真机 WebView 里执行 JS，验证播放链路（解码/缓冲/卡顿）。"""
import json, sys, time, urllib.request, ssl
from websocket import create_connection  # websocket-client

CDP = "http://127.0.0.1:9222/json"

def target():
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    j = json.load(urllib.request.urlopen(CDP, timeout=5))
    for t in j:
        if t.get("type") == "page":
            return t["webSocketDebuggerUrl"]
    raise SystemExit("no page target")

def run(expr, ws):
    mid = int(time.time() * 1000) % 100000
    ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == mid:
            r = msg.get("result", {})
            if "exceptionDetails" in r:
                return {"__error__": r["exceptionDetails"].get("text"),
                        "detail": str(r["exceptionDetails"])[:400]}
            return r.get("result", {}).get("value")

if __name__ == "__main__":
    ws = create_connection(target(), timeout=30, suppress_origin=True)
    probes = {
        "device_tier": "TIER",
        "mpegts_ok":   "!!(window.mpegts && mpegts.getFeatureList().mseLivePlayback)",
        "mse_h264":    "(()=>{try{return MediaSource.isTypeSupported('video/mp4;codecs=\"avc1.42E01E\")')}catch(e){return 'err:'+e}})()",
        "tvc_cats":    "Object.keys(window.TVCore||{}).slice(0,10)",
        "chan_count":  "document.querySelectorAll('.ch').length",
        "has_probe":   "typeof pickFastest",
        "has_stall":   "typeof bindStallWatch",
        "cfg_low":     "JSON.stringify(mpegtsCfg())",
    }
    for k, v in probes.items():
        print(f"{k:14} = {run(v, ws)}")
    ws.close()
