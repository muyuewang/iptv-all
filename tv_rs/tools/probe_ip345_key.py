"""
探针：直接用页面脚本里的 XOR key 解密 iptv345（不经「已知明文攻击」猜域名）。

★ 关键洞察（实测 2026-10-26）：`yrixv` 函数体里明写着
      function yrixv(str,key) { string = hjyzz(str);
                                key = key + "0289eaca54f41a63";   ← ★ key 后缀写死在脚本里
                                ... code += String.fromCharCode(string.charCodeAt(i) ^ key.charCodeAt(k)); ... }
  而 `key` 的实参就是 assign 块里的 seed 变量（`bkvgh`/`skzwn`…，名字随机但**值**可解析）。

  → XOR key = seed_value + "0289eaca54f41a63"（循环取模）。
  → 完整链路：opt 反转 → base64 解 → XOR(key) → base64 解 → URL 明文。

  这样**完全不需要猜 API 域名**，也不用「已知明文攻击」的长度对齐 ——
  域名直接由明文给出（iptv345 的页面里明文就是它自己的域名）。
"""
import importlib.util
import re

spec = importlib.util.spec_from_file_location('p', 'tools/probe_ip345_dec.py')
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

KEY_SUFFIX = "0289eaca54f41a63"   # ★ 源站脚本里写死的 key 后缀


def parse_all(html):
    ent = P.find_ent(html)
    m_tok = re.search(r'"token="\s*\+\s*(\w+)\s*,\s*"token="\s*\+\s*(\w+)', ent)
    mk = re.search(r'\.replace\(\s*(\w+)\s*,\s*""\s*\)', ent)
    assign = ''
    for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S):
        if re.search(r'var\s+\w+\s*=\s*""\s*;', s) and 'split("").reverse()' in s and 'function' not in s:
            assign = s
            break
    env = P.parse_env(assign)
    old = env.get(m_tok.group(1), '')
    new = env.get(m_tok.group(2), '')
    seed = env.get(mk.group(1), '') if mk else ''
    return ent, env, old, new, seed


def decrypt_with_key(html, old, new, seed):
    opts = re.findall(r'<option value="([^"]+)"', html)
    key = (seed + KEY_SUFFIX)
    kb = key.encode('latin1', 'replace')
    urls = []
    for opt in opts:
        st = P.b64d(opt[::-1])
        if not st:
            continue
        o2 = bytes(st[t] ^ kb[t % len(kb)] for t in range(len(st)))
        raw = P.b64d(o2.decode('latin1'))
        if not raw:
            continue
        u = raw.decode('utf-8', 'replace')
        if not u.startswith('http'):
            continue
        u = u.replace('token=' + old, 'token=' + new)
        if seed:
            u = u.replace(seed, '')
        if 'type=' not in u:
            u += '&type=.flv'
        if u not in urls:
            urls.append(u)
    return urls


if __name__ == '__main__':
    import ssl
    import urllib.request
    import time
    CTX = ssl.create_default_context()
    CTX.check_hostname = False
    CTX.verify_mode = ssl.CERT_NONE

    def head(u, timeout=15):
        req = urllib.request.Request(u, headers={'User-Agent': P.UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                d = r.read(2048)
                return r.status, r.headers.get('Content-Type', ''), len(d)
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get('Content-Type', ''), 0
        except Exception as e:
            return -1, type(e).__name__ + ':' + str(e)[:40], 0

    print("### 对照组：本机源 ys:1 ###")
    h = P.fetch("https://api1.2026016.xyz/iptve.php?act=play&tid=ys&id=1", P.UA + "/YM-RT/")
    ent, env, old, new, seed = parse_all(h)
    print('old =', old)
    print('new =', new)
    print('seed=', seed)
    us = decrypt_with_key(h, old, new, seed)
    print('解出 %d 条：' % len(us))
    for u in us[:3]:
        print('   ', u[:130])
    for i, u in enumerate(us[:2]):
        print('   实测 p=%d ->' % i, head(u))

    print("\n### iptv345 翡翠台 ###")
    t0 = time.time()
    h = P.fetch("https://iptv345.com/?act=play&token=968d1884a50db95762d655b51ed11206&tid=gt&id=5")
    ent, env, old, new, seed = parse_all(h)
    print('抓页 %.1fs' % (time.time() - t0))
    print('old =', old)
    print('new =', new)
    print('seed=', seed)
    us = decrypt_with_key(h, old, new, seed)
    print('解出 %d 条：' % len(us))
    for u in us[:3]:
        print('   ', u[:130])
    for i, u in enumerate(us[:5]):
        print('   实测 p=%d ->' % i, head(u))
