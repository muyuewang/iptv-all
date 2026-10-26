"""
探针：iptv345 的正确解密链（与本机源**不同**）。

★ iptv345 的 option 格式（实测 2026-10-26）：
    <option value="=QQY9Z1XMomN...">線路1</option>
  → 值是 **`=` 前缀 + base64**，**不反转**（本机源是反转的）。
  → base64 解出的是**密文**（不是明文）→ 还需 XOR → 再 base64。
  → 页面 JS：`startPlayer($("#playURL")[0].value, ...)` **直接把 option 值
    当流地址用**（不经过 kmatd）→ 说明浏览器端另有一次解码。

  从本机源同源逻辑推断完整链：
    opt(`=B64`) → 去掉 `=` → base64 解 → XOR(seed) → base64 解 → 流 URL
  （本机源是 opt → 反转 → base64 解 → XOR → base64 解）

  seed 从 assign 块解析（seed 变量 = XOR key 本体；`yrixv` 里 key = seed + 固定后缀）。
"""
import base64
import re
import ssl
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120 Mobile Safari/537.36 diashizhb LT-APP/48/517")


def fetch(url):
    import time
    last = None
    for _ in range(4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            return urllib.request.urlopen(req, timeout=25, context=CTX).read().decode('utf-8', 'replace')
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise last


def b64d(s):
    s = s.strip()
    try:
        return base64.b64decode(s + '=' * (-len(s) % 4))
    except Exception:
        return None


def parse_env(assign):
    raw = {}
    for m in re.finditer(r'(\w+)\s*=\s*([^;]+);', assign):
        name, expr = m.group(1), m.group(2).strip()
        if expr.startswith('"') and expr.endswith('"') and expr.count('"') == 2:
            raw[name] = expr[1:-1]
            continue
        parts = re.findall(r'"([^"]*)"(\.split\(""\)\.reverse\(\)\.join\(""\))?', expr)
        if parts:
            raw[name] = ''.join(lit[::-1] if rev else lit for lit, rev in parts)
    refs = {}
    for m in re.finditer(r'(\w+)\s*=\s*(\w+)\s*;', assign):
        refs[m.group(1)] = m.group(2)

    def resolve(name, depth=0):
        if depth > 8:
            return ''
        src = refs.get(name)
        if src:
            v = resolve(src, depth + 1)
            if v:
                return v
        return raw.get(name, '')
    return {n: resolve(n) for n in set(list(raw) + list(refs))}


def get_seed_and_suffix(html):
    ent = None
    for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S):
        if 'split("").reverse().join("")' in s and '"token="' in s:
            ent = s
            break
    if not ent:
        return None, None, None
    mk = re.search(r'\.replace\(\s*(\w+)\s*,\s*""\s*\)', ent)
    ms = re.search(r'\w+\s*=\s*\w+\s*\+\s*"([0-9a-fA-F]{6,32})"', ent)
    assign = ''
    for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S):
        if re.search(r'var\s+\w+\s*=\s*""\s*;', s) and 'split("").reverse()' in s and 'function' not in s:
            assign = s
            break
    env = parse_env(assign)
    seed = env.get(mk.group(1), '') if mk else ''
    suffix = ms.group(1) if ms else ''
    return seed, suffix, ent


def try_chain(opt, seed, suffix, order='b64_xor_b64', xor_first=False):
    v = opt[1:] if opt.startswith('=') else opt
    st = b64d(v)
    if not st:
        return None
    keys = []
    for k in ([seed + suffix] if suffix else []) + [seed]:
        if k:
            keys.append(k.encode('latin1', 'replace'))
    for kb in keys:
        if not kb:
            continue
        o2 = bytes(st[i] ^ kb[i % len(kb)] for i in range(len(st)))
        raw = b64d(o2.decode('latin1'))
        if raw and (b'http' in raw[:40]):
            return raw.decode('utf-8', 'replace')
    return None


if __name__ == '__main__':
    PAGE = "https://iptv345.com/?act=play&token=968d1884a50db95762d655b51ed11206&tid=gt&id=5"
    h = fetch(PAGE)
    open('_ip345b.html', 'w', encoding='utf-8').write(h)
    seed, suffix, ent = get_seed_and_suffix(h)
    print('seed  =', seed)
    print('suffix=', suffix)
    opts = re.findall(r'<option value="([^"]+)"', h)
    print('opts =', len(opts))
    for i, o in enumerate(opts[:5]):
        u = try_chain(o, seed, suffix)
        print('  [%d] %s' % (i, (u[:150] if u else 'FAIL')))
