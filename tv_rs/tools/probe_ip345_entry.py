"""
探针：iptv345 是不是本机源的「另一个域名入口」？

★ 重大发现（实测 2026-10-26）：
  `https://iptv345.com/?tid=gt` 返回**完整频道列表**（act=play&tid=gt&id=N），
  tid/id 与本机源（api1.2026016.xyz/iptve.php?tid=gt）**完全一致**！
  → iptv345 = 本机源同源站点的另一个域名。

  如果 `https://iptv345.com/?act=play&tid=gt&id=N` 的播放页与本机源同格式
  （加密 option + 解密脚本），那本机源 app 路由 404 的分类（tv/gt/wintv123）
  就能**换域名重试**拿到线路 —— 不必再走网页嗅探（也就绕开了 fengshows 的
  DNS 污染问题）。

做法：对每个分类/频道，抓 iptv345 播放页 → 用探针里的鲁棒解密
  （变量名随机 → 从 `.replace(Z,"")` 取 key 变量 + 已知明文攻击）解线路
  → 立刻实测 eplay2 URL 是否 200。
"""
import importlib.util
import re
import ssl
import time
import urllib.request

spec = importlib.util.spec_from_file_location('p', 'tools/probe_ip345_dec.py')
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def head(u, timeout=12):
    req = urllib.request.Request(u, headers={'User-Agent': P.UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            d = r.read(1024)
            return r.status, r.headers.get('Content-Type', ''), len(d)
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get('Content-Type', ''), 0
    except Exception as e:
        return -1, type(e).__name__ + ':' + str(e)[:40], 0


def parse_vars(html):
    ent = P.find_ent(html)
    if not ent:
        return None
    mt = re.search(r'"token="\s*\+\s*(\w+)\s*,\s*"token="\s*\+\s*(\w+)', ent)
    mk = re.search(r'\.replace\(\s*(\w+)\s*,\s*""\s*\)', ent)
    assign = ''
    for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S):
        if re.search(r'var\s+\w+\s*=\s*""\s*;', s) and 'split("").reverse()' in s and 'function' not in s:
            assign = s
            break
    env = P.parse_env(assign)
    if not mt:
        return None
    return (env.get(mt.group(1), ''), env.get(mt.group(2), ''),
            env.get(mk.group(1), '') if mk else '')


def solve(html, api, tid, cid):
    """已知明文攻击（域名靠长度锁定）。返回解出的 URL 列表。"""
    v = parse_vars(html)
    if not v:
        return [], 'no vars'
    old, new, seed = v
    if not old:
        return [], 'no old'
    opts = re.findall(r'<option value="([^"]+)"', html)
    if not opts:
        return [], 'no opts'
    s0 = P.b64d(opts[0][::-1])
    key = None
    for suf in ('&type=.flv', ''):
        tmpl = "%s/eplay2.php?token=%s&tid=%s&id=%s&p=0%s" % (api.rstrip('/'), old, tid, cid, suf)
        out0 = P.b64e(tmpl.encode()).encode()
        if len(s0) == len(out0):
            key = bytes(s0[j] ^ out0[j] for j in range(len(s0)))
            break
    if key is None:
        return [], 'len mismatch s0=%d' % len(s0)
    urls = []
    for opt in opts:
        st = P.b64d(opt[::-1])
        if not st:
            continue
        o2 = bytes(st[t] ^ key[t % len(key)] for t in range(len(st)))
        raw = P.b64d(o2.decode('latin1'))
        if not raw:
            continue
        u = raw.decode('utf-8', 'replace')
        u = u.replace('token=' + old, 'token=' + new)
        if seed:
            u = u.replace(seed, '')
        if 'type=' not in u:
            u += '&type=.flv'
        if u not in urls:
            urls.append(u)
    return urls, 'ok'


def main():
    # ① iptv345 频道列表
    print("=== iptv345 频道列表 (?tid=gt) ===")
    h = P.fetch('https://iptv345.com/?tid=gt')
    items = re.findall(r'act=play&tid=(\w+)&id=(\d+)[^>]*>([^<]+)<', h)
    print("  解析到 %d 条：" % len(items))
    for t, i, n in items[:8]:
        print("    %s:%s  %s" % (t, i, n.strip()))
    print("  token 形式:", re.findall(r'token=([0-9a-f]{8})', h)[:3])

    # ② 播放页
    print("\n=== iptv345 播放页 (?act=play&tid=gt&id=1) ===")
    p = P.fetch('https://iptv345.com/?act=play&tid=gt&id=1')
    print("  页面长度:", len(p))
    v = parse_vars(p)
    print("  old/new/seed:", v)
    for api in ('https://iptv345.com', 'https://d.2026016.xyz', 'https://api1.2026016.xyz'):
        urls, why = solve(p, api, 'gt', '1')
        print("  %-26s %-18s %d" % (api, why, len(urls)))
        if urls:
            for u in urls[:2]:
                print("      ", u[:130])
            print("      实测:", head(urls[0]))

    # ③ 官网源给的 token URL（已知 tid/id）
    print("\n=== 官网源 token URL（gt id=5）===")
    p2 = P.fetch('https://iptv345.com/?act=play&token=968d1884a50db95762d655b51ed11206&tid=gt&id=5')
    v2 = parse_vars(p2)
    print("  old/new/seed:", v2)
    urls, why = solve(p2, 'https://iptv345.com', 'gt', '5')
    print("  solve:", why, len(urls))
    if urls:
        print("   ", urls[0][:130])
        print("   实测:", head(urls[0]))


if __name__ == '__main__':
    main()
