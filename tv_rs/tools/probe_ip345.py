"""
探针：验证 iptv345.com 的 token URL 能否像本机源一样 XOR 解密出 eplay2 直链。

★ 背景（实测 2026-10-26）：官网源香港分组里 5 个频道（翡翠台 / TVB Plus /
  Now 財經 / ViuTV / ViuTV6）的网页地址是
  `https://iptv345.com/?act=play&token=<32位hex>&tid=gt&id=<N>`
  —— **与本机源同源同格式**！直连访问 200 OK，页面里同样有加密 `<option value="=kFD...">`
  和解密脚本。如果这套 XOR 解密能出 eplay2 直链 → 港澳台频道就有了一条
  **不依赖网页嗅探、不依赖 DNS 干净**的可靠出路（且本机源那三个 404 分类
  也能借 iptv345 补齐）。

做法：复用 shared/core.py 的解密逻辑（decrypt_page），只换域名。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TV_RS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(TV_RS, '..', 'shared'))

import core  # noqa: E402

CASES = [
    ("翡翠台", "https://iptv345.com/?act=play&token=968d1884a50db95762d655b51ed11206&tid=gt&id=5"),
    ("TVB Plus", "https://iptv345.com/?act=play&token=f47d1bc8615b63ba6b5d0dc6622715da&tid=gt&id=8"),
    ("Now 財經", "https://iptv345.com/?act=play&token=5ff6467993be64c7c6fec03f1d04f840&tid=gt&id=18"),
    ("ViuTV", "https://iptv345.com/?act=play&token=6e02777912b07710689aed87e748d963&tid=gt&id=19"),
    ("ViuTV6", "https://iptv345.com/?act=play&token=5684baa22e5d77d14c51aa32f41a71f3&tid=gt&id=20"),
]


def main():
    import urllib.request
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/120 Mobile Safari/537.36 diashizhb LT-APP/48/517")
    for name, url in CASES:
        print("\n===== %s =====" % name)
        print("URL:", url)
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            html = urllib.request.urlopen(req, timeout=25, context=ctx).read().decode('utf-8', 'replace')
        except Exception as e:
            print("  抓页失败:", type(e).__name__, e)
            continue
        print("  页面长度:", len(html))
        # 找解密脚本里的 key（与本机源同格式）
        keys = re.findall(r'"([0-9a-f]{32})"', html)
        print("  页面里的 hex key 候选:", keys[:3])
        opts = re.findall(r'<option value="([^"]+)"', html)
        print("  option 数量:", len(opts))
        if not opts:
            print("  ✗ 没有加密 option")
            continue
        # 复用 core 的解密：它按页面里的 key 反推 XOR
        try:
            res = core.decrypt_page(html, "gt", "5")
            urls = res[0] if isinstance(res, tuple) else res.get("urls", [])
            msg = res[1] if isinstance(res, tuple) else res.get("msg", "")
            print("  decrypt_page ->", msg, "urls:", len(urls))
            for u in urls[:5]:
                print("     ", u[:110])
            res = {"urls": urls}
            print("  decrypt_page ->", res.get('msg'), "urls:", len(res.get('urls', [])))
            for u in res.get('urls', [])[:5]:
                print("     ", u[:110])
        except Exception as e:
            print("  decrypt_page 异常:", type(e).__name__, e)
            # 手工试：把 option 值当 base64 + XOR
            import base64
            v = opts[0]
            try:
                raw = base64.b64decode(v[1:] + '=' * (-len(v[1:]) % 4))
                print("  base64 解出:", raw[:80])
            except Exception as e2:
                print("  base64 失败:", e2)


if __name__ == '__main__':
    main()
