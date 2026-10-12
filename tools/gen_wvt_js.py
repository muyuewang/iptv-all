# -*- coding: utf-8 -*-
"""把 shared/wvt_builtin.json 转成安卓端可直接 <script> 引入的快照 JS。

产出：android/app/assets/wvt_channels.js
内容：window.WVT_SNAPSHOT = {...}
      channels 用紧凑数组 [group, name, urls[], extras[]]
      extras 全为空时写 null，省体积

用法：python tools/gen_wvt_js.py
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "shared", "wvt_builtin.json")
DST = os.path.join(ROOT, "android", "app", "assets", "wvt_channels.js")


def main():
    if not os.path.isfile(SRC):
        print("找不到 %s，请先跑 tools/gen_wvt_snapshot.py" % SRC, file=sys.stderr)
        return 1
    with open(SRC, "r", encoding="utf-8") as f:
        snap = json.load(f)

    rows = []
    n_url = 0
    for c in snap.get("channels") or []:
        name = (c.get("name") or "").strip()
        group = c.get("group") or "其他"
        urls, extras = [], []
        for u in c.get("urls") or []:
            if isinstance(u, dict):
                uu = u.get("url")
                if not uu:
                    continue
                urls.append(uu)
                extras.append(u.get("extra") or "")
            elif isinstance(u, str) and u:
                urls.append(u)
                extras.append("")
        if not name or not urls:
            continue
        n_url += len(urls)
        rows.append([group, name, urls, extras if any(extras) else None])

    data = {
        "generated": snap.get("generated"),
        "generated_text": snap.get("generated_text"),
        "source": snap.get("source"),
        "adapters": snap.get("adapters") or [],
        "channels": rows,
        "tags": snap.get("tags") or {},     # 央视频付费频道标记（限免/VIP，pid -> tag）
    }

    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    out = (
        "/* 自动生成，请勿直接编辑 —— 来源 shared/wvt_builtin.json\n"
        " * 重新生成：python tools/gen_wvt_js.py\n"
        " * 生成时间：%s\n"
        " */\n"
        "window.WVT_SNAPSHOT=%s;\n"
        % (time.strftime("%Y-%m-%d %H:%M:%S"), body)
    )

    os.makedirs(os.path.dirname(DST), exist_ok=True)
    with open(DST, "w", encoding="utf-8") as f:
        f.write(out)

    print("已写 %s" % DST)
    print("  频道 %d 个 / 线路 %d 条 / 适配规则 %d 条 / %.1f KB"
          % (len(rows), n_url, len(data["adapters"]), len(out.encode("utf-8")) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
