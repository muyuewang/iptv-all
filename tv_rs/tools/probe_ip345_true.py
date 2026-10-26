"""
探针：用**真 keystream**（脚本里写死的 seed+后缀）直接解 iptv345，
不再用「已知明文攻击」猜域名。

★ iptv345 页面脚本结构（函数名/变量名/后缀每次请求都随机）：
    function <b64>(data) { ...base64 解码... }
    function <xor>(str,key) { string = <b64>(str);
                               key = key + "<16位hex>";       ← ★ 后缀
                               len = key.length;
                               code += String.fromCharCode(string.charCodeAt(i) ^ key.charCodeAt(k));
                               stra = <b64>(code); return stra; }
    function <main>(hsj) { hsj = hsj.split("").reverse().join("");
                            hsj = <xor>(hsj, <KEYVAR>);       ← ★ KEYVAR 是真 seed 变量
                            hsj = hsj.replace("token="+<OLD>, "token="+<NEW>);
                            hsj = hsj.replace(<KEYVAR>, ""); return hsj; }

  完整链：opt 反转 → base64 解 → XOR(seed+后缀) → base64 解 → URL
          → 换 token(old→new) → 去 seed。
  ★ 关键：这样**不需要知道 API 域名** —— 明文里自带。
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


def analyze(html):
    """抽出 (keystream, old, new, seed)。"""
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
    ent = ''
    for s in scripts:
        if 'split("").reverse().join("")' in s and '"token="' in s:
            ent = s
            break
    if not ent:
        return None
    # ① xor 函数：签名 function f(a, key) 且体内有 charCodeAt 与 ^
    xor_fn = None
    for m in re.finditer(r'function\s+(\w+)\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)\s*\{', ent):
        seg = ent[m.start():m.start() + 1200]
        if 'charCodeAt' in seg and '^' in seg:
            xor_fn = (m.group(1), m.group(3), seg)
            break
    if not xor_fn:
        return None
    fname, keyparam, body = xor_fn
    # ② 后缀：key = key + "xxxx"  或  key+="xxxx"
    ms = re.search(re.escape(keyparam) + r'\s*=\s*' + re.escape(keyparam) + r'\s*\+\s*"([0-9a-fA-F]{4,40})"', body)
    if not ms:
        ms = re.search(r'\+\s*"([0-9a-fA-F]{4,40})"', body)
    suffix = ms.group(1) if ms else ''
    # ③ 调用点：<fname>(x, KEYVAR)
    #    ★ 必须排除 xor 函数**自身**的定义处 `function <fname>(str, key)` ——
    #    那里的第二个参数是形参名（字面 "key"），不是真 seed 变量。
    mc = None
    for m in re.finditer(re.escape(fname) + r'\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)', ent):
        # 定义处形如 `function <fname>(a, b)` —— 前面紧跟 function 关键字
        prefix = ent[max(0, m.start() - 12):m.start()]
        if 'function' in prefix:
            continue
        mc = m
        break
    keyvar = mc.group(2) if mc else None
    # ④ token 对
    mt = re.search(r'"token="\s*\+\s*(\w+)\s*,\s*"token="\s*\+\s*(\w+)', ent)
    if not keyvar or not mt:
        return None
    # ⑤ assign 块
    assign = ''
    for s in scripts:
        if re.search(r'var\s+\w+\s*=\s*""\s*;', s) and 'split("").reverse()' in s and 'function' not in s:
            assign = s
            break
    env = parse_env(assign)
    seed = env.get(keyvar, '')
    old = env.get(mt.group(1), '')
    new = env.get(mt.group(2), '')
    return {'keystream': (seed + suffix).encode('latin1', 'replace'),
            'seed': seed, 'old': old, 'new': new,
            'suffix': suffix, 'keyvar': keyvar,
            'oldvar': mt.group(1), 'newvar': mt.group(2), 'xorfn': fname}


def decrypt(html, a):
    kb = a['keystream']
    if not kb:
        return [], 'no keystream'
    opts = re.findall(r'<option value="([^"]+)"', html)
    urls = []
    for opt in opts:
        st = b64d(opt[::-1])
        if not st:
            continue
        o2 = bytes(st[i] ^ kb[i % len(kb)] for i in range(len(st)))
        raw = b64d(o2.decode('latin1'))
        if not raw:
            continue
        u = raw.decode('utf-8', 'replace')
        if not u.startswith('http'):
            continue
        u = u.replace('token=' + a['old'], 'token=' + a['new'])
        if a['seed']:
            u = u.replace(a['seed'], '')
        if 'type=' not in u:
            u += '&type=.flv'
        if u not in urls:
            urls.append(u)
    return urls, 'ok'


if __name__ == '__main__':
    import time
    for label, page in (
        ("iptv345 gt:5", "https://iptv345.com/?act=play&token=968d1884a50db95762d655b51ed11206&tid=gt&id=5"),
        ("iptv345 gt:1", "https://iptv345.com/?act=play&tid=gt&id=1"),
    ):
        print("\n########## %s ##########" % label)
        h = fetch(page)
        a = analyze(h)
        if not a:
            print("  analyze 失败")
            continue
        print("  xorfn=%s keyvar=%s suffix=%r" % (a['xorfn'], a['keyvar'], a['suffix']))
        print("  seed=%r" % a['seed'])
        print("  old =%r" % a['old'])
        print("  new =%r" % a['new'])
        us, why = decrypt(h, a)
        print("  解出 %d 条 (%s)" % (len(us), why))
        for u in us[:3]:
            print("     ", u[:150])
        if us:
            def head(u, timeout=12):
                req = urllib.request.Request(u, headers={'User-Agent': UA})
                try:
                    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                        d = r.read(512)
                        return r.status, r.headers.get('Content-Type', ''), len(d)
                except urllib.error.HTTPError as e:
                    return e.code, e.headers.get('Content-Type', ''), 0
                except Exception as e:
                    return -1, type(e).__name__ + ':' + str(e)[:40], 0
            for i, u in enumerate(us[:3]):
                print("     实测 p=%d ->" % i, head(u))
