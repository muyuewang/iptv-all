# -*- coding: utf-8 -*-
"""通过 WebView 远程调试，在指定页面里求值（安卓端诊断用）。

步骤：
    adb forward tcp:9222 localabstract:webview_devtools_remote_<pid>
    curl -s http://127.0.0.1:9222/json/list            # 拿 webSocketDebuggerUrl
    python tools/android_eval.py <ws_url> "<js expression>"
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "desktop"))

import cdp  # noqa: E402


def main():
    ws_url = sys.argv[1]
    expr = sys.argv[2] if len(sys.argv) > 2 else "1"
    ws = cdp.MiniWS(ws_url, timeout=12)
    try:
        cid = ws.send_call("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": True,
        })
        r = ws.wait_result(cid, timeout=12)
        if "exceptionDetails" in r:
            print("异常:", json.dumps(r["exceptionDetails"], ensure_ascii=False)[:800])
        val = r.get("result", {}).get("value")
        print(json.dumps(val, ensure_ascii=False, indent=2)[:4000])
    finally:
        try:
            ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
