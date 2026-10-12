# -*- coding: utf-8 -*-
"""一次性：用真浏览器打开央视频 TV 页，抓侧栏「限免/VIP」标记 → pid→tag 映射。
产物：/tmp/ysp_tags.json  {"600099655": "mianfei", ...}（mianfei=限免 / vip=VIP）
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "desktop"))
import cdp  # noqa: E402

log = lambda *a: print(*a)

sess = cdp.BrowserSession(profile_dir=os.path.join(HERE, "..", "desktop", ".sniff"),
                          offscreen=True, headless=False, log=log)
try:
    sess.ensure()
    sess.navigate("https://www.yangshipin.cn/tv/home?pid=600001859")
    time.sleep(14)                      # SPA + 频道接口，留足渲染时间
    js = r"""
(() => {
  const out = [];
  const seen = new Set();
  // 找所有包含 限免/VIP 文本的小标签，向上找条目根，收集名称与 pid
  const els = document.querySelectorAll("body *");
  for (const el of els) {
    const t = (el.textContent || "").trim();
    if (t !== "\u9650\u514D" && t !== "VIP" && t !== "vip") continue;
    // el 是标签本身，向上找包含频道名的条目
    let node = el;
    for (let i = 0; i < 8 && node; i++) {
      node = node.parentElement;
      if (!node) break;
      const txt = (node.textContent || "").trim();
      const m = txt.match(/([A-Za-z0-9\u4e00-\u9fa5·\-\+ ]{2,30}?)[\s\u3000]*(?:\u9650\u514D|VIP|vip)/);
      if (m && txt.length < 120) {
        const key = m[1].trim() + "|" + t;
        if (!seen.has(key)) { seen.add(key); out.push({name: m[1].trim(), tag: t}); }
        break;
      }
    }
  }
  // 顺带抓侧栏所有条目的 pid（Vue 组件 or DOM 里带 pid 的）
  const pids = {};
  const html = document.body.innerHTML;
  const re = /pid[=:]["']?(\d{9,15})/g; let mm;
  while ((mm = re.exec(html))) pids[mm[1]] = 1;
  return {tags: out, pidCount: Object.keys(pids).length,
          sample: document.body.innerText.slice(0, 600)};
})()
"""
    r = sess.ws.call("Runtime.evaluate",
                     {"expression": js, "returnByValue": True,
                      "awaitPromise": False})
    val = (r.get("result") or {}).get("value") or {}
    tags = val.get("tags") or []
    print("抓到标记条目:", len(tags))
    for t in tags[:30]:
        print("  ", t)
    print("页面 pid 数:", val.get("pidCount"))
    with open("/tmp/ysp_tags_raw.json", "w", encoding="utf-8") as f:
        json.dump(val, f, ensure_ascii=False, indent=1)
    # 侧栏条目要逐个点出 pid 不现实；改为从 Vue devtools hook 抓组件数据
    js2 = r"""
(() => {
  // Vue3: __vue_app__；Vue2: __vue__
  const app = document.getElementById("app");
  const keys = Object.keys(app || {});
  return {vueKeys: keys, hasVue3: !!(app && app.__vue_app__),
          hasVue2: !!(app && app.__vue__)};
})()
"""
    r2 = sess.ws.call("Runtime.evaluate",
                      {"expression": js2, "returnByValue": True})
    print("vue:", (r2.get("result") or {}).get("value"))
finally:
    sess.close()
