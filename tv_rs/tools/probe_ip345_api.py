"""
探针：用「解出的 token 必须逐字等于脚本里的 old token」这一强判据，
反推 iptv345 播放页的**真实 eplay2 API 域名**。

★ 为什么需要：已知明文攻击里，模板明文与真明文**长度相同**时 XOR key 反推
  必然「成功」（数学恒等），但解出的 raw 其实还是我们自己造的模板 —— 是假阳性。
  真正的判据是：raw 里 `token=<old>` 必须与脚本解析出的 old **逐字相同**
  （域名猜错 → raw 的 token 段是乱码）。用它逐个试候选域名。
"""
import base64
import importlib.util
import re
import ssl
import urllib.request

spec = importlib.util.spec_from_file_location('p', 'tools/probe_ip345_dec.py')
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120 Mobile Safari/537.36 diashizhb LT-APP/48/517")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def fetch(url, ua=UA):
    req = urllib.request.Request(url, headers={'User-Agent': ua})
    return urllib.request.urlopen(req, timeout=25, context=CTX).read().decode('utf-8', 'replace')


def parse_vars(html):
    """返回 (old, new, key_seed, opts)。"""
    ent = P.find_ent(html)
    m_tok = re.search(r'"token="\s*\+\s*(\w+)\s*,\s*"token="\s*\+\s*(\w+)', ent)
    m_key = re.search(r'\.replace\(\s*(\w+)\s*,\s*""\s*\)', ent)
    assign = ''
    for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S):
        if re.search(r'var\s+\w+\s*=\s*""\s*;', s) and 'split("").reverse()' in s and 'function' not in s:
            assign = s
            break
    env = P.parse_env(assign)
    old = env.get(m_tok.group(1), '')
    new = env.get(m_tok.group(2), '')
    key_seed = env.get(m_key.group(1), '') if m_key else ''
    opts = re.findall(r'<option value="([^"]+)"', html)
    return old, new, key_seed, opts


def solve(html, api, tid, cid):
    """返回 (urls, status)。status 说明为什么。"""
    old, new, key_seed, opts = parse_vars(html)
    if not old or not new:
        return [], 'old/new empty'
    if not opts:
        return [], 'no opts'
    s0 = P.b64d(opts[0][::-1])
    if not s0:
        return [], 'b64 fail'
    # 候选明文：域名 × 是否有 type=.flv
    cands = []
    for dom in api:
        for suf in ('&type=.flv', ''):
            cands.append("%s/eplay2.php?token=%s&tid=%s&id=%s&p=0%s" % (dom.rstrip('/'), old, tid, cid, suf))
    key = None
    for tmpl in cands:
        out0 = P.b64e(tmpl.encode()).encode()
        if len(s0) == len(out0):
            key = bytes(s0[j] ^ out0[j] for j in range(len(s0)))
            break
    if key is None:
        return [], 'len mismatch s0=%d' % len(s0)
    # ★ 强判据：解第 1 条，raw 里必须含 `token=<old>` 逐字相同。
    st = P.b64d(opts[0][::-1])
    o2 = bytes(st[t] ^ key[t % len(key)] for t in range(len(st)))
    raw = P.b64d(o2.decode('latin1'))
    if not raw:
        return [], 'raw b64 fail'
    fin = raw.decode('utf-8', 'replace')
    if ('token=' + old) not in fin:
        return [], 'token mismatch (域名错)'
    urls = []
    for opt in opts:
        s = P.b64d(opt[::-1])
        if not s:
            continue
        o = bytes(s[t] ^ key[t % len(key)] for t in range(len(s)))
        r = P.b64d(o.decode('latin1'))
        if not r:
            continue
        u = r.decode('utf-8', 'replace').replace("token=" + old, "token=" + new)
        if key_seed:
            u = u.replace(key_seed, "")
        if not re.match(r'^https?://', u):
            continue
        if 'type=' not in u:
            u += '&type=.flv'
        if u not in urls:
            urls.append(u)
    return urls, 'OK'


CANDIDATES = [
    'https://iptv345.com',
    'https://api1.2026016.xyz',
    'https://api2.2026016.xyz',
    'https://api.2026016.xyz',
    'https://d.2026016.xyz',
    'https://n6wxm.com',
    'https://www.bauval.org',
    'https://www.czernik.org',
]

if __name__ == '__main__':
    url = ("https://iptv345.com/?act=play&token=968d1884a50db95762d655b51ed11206&tid=gt&id=5")
    html = fetch(url)
    old, new, key_seed, opts = parse_vars(html)
    print('old  =', old)
    print('new  =', new)
    print('seed =', key_seed)
    print('opts =', len(opts))
    print()
    for api in CANDIDATES:
        urls, st = solve(html, [api], 'gt', '5')
        print('%-28s %-22s %d' % (api, st, len(urls)))
        if urls:
            for u in urls[:2]:
                print('      ', u[:130])
