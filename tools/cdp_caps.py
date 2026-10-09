# -*- coding: utf-8 -*-
"""探测真机 WebView 的 MSE / 解码能力，定位卡顿根因。"""
import json, time, urllib.request, ssl
from websocket import create_connection

def target():
    j = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5))
    for t in j:
        if t.get("type") == "page":
            return t["webSocketDebuggerUrl"]
    raise SystemExit("no page target")

def run(expr, ws, tid=[0]):
    tid[0] += 1
    mid = tid[0]
    ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == mid:
            r = msg.get("result", {})
            if "exceptionDetails" in r:
                return "ERR: " + str(r["exceptionDetails"].get("text"))
            return r.get("result", {}).get("value")

CAPS = r"""
(function(){
  function t(s){ try{ return MediaSource.isTypeSupported(s); }catch(e){ return 'ex:'+e; } }
  return JSON.stringify({
    UA: navigator.userAgent,
    cores: navigator.hardwareConcurrency,
    memGB: navigator.deviceMemory,
    screen: screen.width+'x'+screen.height,
    dpr: window.devicePixelRatio,
    h264_baseline: t('video/mp4;codecs="avc1.42E01E"'),
    h264_main:     t('video/mp4;codecs="avc1.4D401E"'),
    h264_high:     t('video/mp4;codecs="avc1.64001E"'),
    h264_hi10:     t('video/mp4;codecs="avc1.640028"'),
    h264_generic:  t('video/mp4;codecs="avc1"'),
    h265_hevc:     t('video/mp4;codecs="hvc1.1.6.L93.B0"'),
    av1:           t('video/mp4;codecs="av01.0.05M.08"'),
    vp9:           t('video/webm;codecs="vp9"'),
    flv_in_mse:    t('video/x-flv'),
    fmp4:          t('video/mp4'),
    mse_ok: !!(window.MediaSource && MediaSource.isTypeSupported('video/mp4;codecs="avc1.42E01E"')),
    webgl: (function(){try{var c=document.createElement('canvas');return !!(c.getContext('webgl')||c.getContext('experimental-webgl'));}catch(e){return false}})()
  }, null, 1);
})()
"""

if __name__ == "__main__":
    ws = create_connection(target(), timeout=30, suppress_origin=True)
    print(run(CAPS, ws))
    ws.close()
