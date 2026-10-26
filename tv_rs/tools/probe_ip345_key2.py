"""
探针：精确定位 iptv345 的 XOR key（从脚本结构里抽，不猜变量名）。

★ 脚本结构（变量/函数名每次请求随机）：
    function <b64>(data) { ... }                                   ← base64 解码
    function <xor>(str,key) { string = <b64>(str);
                               key = key + "<16位hex>";              ← ★ key 后缀写死
                               len = key.length;
                               code += String.fromCharCode(string.charCodeAt(i) ^ key.charCodeAt(k));
                               stra = <b64>(code); return stra; }
    function <main>(hsj) { hsj = hsj.split("").reverse().join("");
                            hsj = <xor>(hsj, <KEYVAR>);             ← 调用点给出真 key 变量
                            hsj = hsj.replace("token="+<OLD>, "token="+<NEW>);
                            hsj = hsj.replace(<KEYVAR>, ""); return hsj; }

  → XOR key = env[<KEYVAR>] + "<16位hex后缀>"（后缀从 xor 函数体里正则抽）。

  完整链路：opt 反转 → base64 解 → XOR(key) → base64 解 → URL 明文
  → 换 token → 去 seed。
"""
import importlib.util
import re

spec = importlib.util.spec_from_file_location('p', 'tools/probe_ip345_dec.py')
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)


def parse(html):
    ent = P.find_ent(html)
    # ① xor 函数体：含 `charCodeAt(i) ^` 且 `key.charCodeAt(k)`
    xor_body = ''
    for m in re.finditer(r'function\s+(\w+)\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)\s*\{', ent):
        start = m.start()
        # 取该函数体（到下一个 function 或块尾）
        seg = ent[start:start + 900]
        if 'charCodeAt' in seg and '^' in seg:
            xor_body = seg
            xor_fn = m.group(1)
            break
    if not xor_body:
        return None
    # ② key 后缀：key = key + "xxxx"  或 key+="xxxx"
    ms = re.search(r'\w+\s*=\s*\w+\s*\+\s*"([0-9a-fA-F]{8,32})"', xor_body)
    suffix = ms.group(1) if ms else ''
    # ③ 调用点：<main> 里 <xor>(hsj, KEYVAR)
    mk = re.search(r'\w+\s*\(\s*\w+\s*,\s*(\w+)\s*\)', ent)
    keyvar = mk.group(1) if mk else None
    # ④ token 对
    mt = re.search(r'"token="\s*\+\s*(\w+)\s*,\s*"token="\s*\+\s*(\w+)', ent)
    # ⑤ assign 块 → env
    assign = ''
    for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S):
        if re.search(r'var\s+\w+\s*=\s*""\s*;', s) and 'split("").reverse()' in s and 'function' not in s:
            assign = s
            break
    env = P.parse_env(assign)
    return {
        'suffix': suffix,
        'keyvar': keyvar,
        'old_var': mt.group(1) if mt else None,
        'new_var': mt.group(2) if mt else None,
        'env': env,
    }


def decrypt(html, info):
    env = info['env']
    seed = env.get(info['keyvar'], '') if info['keyvar'] else ''
    old = env.get(info['old_var'], '') if info['old_var'] else ''
    new = env.get(info['new_var'], '') if info['new_var'] else ''
    kb = (seed + info['suffix']).encode('latin1', 'replace')
    if not kb:
        return [], 'no key'
    opts = re.findall(r'<option value="([^"]+)"', html)
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
    return urls, 'seed=%r suffix=%r' % (seed, info['suffix'])


if __name__ == '__main__':
    import ssl
    import urllib.request
    CTX = ssl.create_default_context()
    CTX.check_hostname = False
    CTX.verify_mode = ssl.CERT_NONE

    def head(u, timeout=15):
        req = urllib.request.Request(u, headers={'User-Agent': P.UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                d = r.read(1024)
                return r.status, r.headers.get('Content-Type', ''), len(d)
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get('Content-Type', ''), 0
        except Exception as e:
            return -1, type(e).__name__ + ':' + str(e)[:40], 0

    for label, url, ua in (
        ("本机源 ys:1", "https://api1.2026016.xyz/iptve.php?act=play&tid=ys&id=1", P.UA + "/YM-RT/"),
        ("iptv345 翡翠台", "https://iptv345.com/?act=play&token=968d1884a50db95762d655b51ed11206&tid=gt&id=5", P.UA),
    ):
        print("\n########## %s ##########" % label)
        h = P.fetch(url, ua)
        info = parse(h)
        if not info:
            print("  parse 失败")
            continue
        print("  suffix=%r keyvar=%r old_var=%r new_var=%r" % (
            info['suffix'], info['keyvar'], info['old_var'], info['new_var']))
        us, why = decrypt(h, info)
        print("  ", why)
        print("  解出 %d 条：" % len(us))
        for u in us[:3]:
            print("     ", u[:135])
        for i, u in enumerate(us[:3]):
            print("     实测 p=%d ->" % i, head(u))
