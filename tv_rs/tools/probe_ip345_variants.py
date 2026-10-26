"""
探针：对 iptv345 解出的 eplay2 URL 做**变体穷举 + 真实取流判定**。

★ 已知：iptv345 与本机源同格式，XOR 已知明文攻击解出的 10 条 URL 形如
      https://iptv345.com/eplay2.php?token=<T>&tid=gt&id=5&p=<0..9>&type=.flv
  但一律 404。可能原因逐个试：
   ① token 该用「替换后的 new」还是「密文里的 old 32hex」还是「页面 URL 里的 token」
   ② `type=.flv` 该不该带
   ③ API 域名该是 iptv345.com 还是同源的 d.2026016.xyz / api1 / api2
   ④ tid/id 是否要换
  判据：HTTP 200 且 Content-Type 是流（video/x-flv / application/octet-stream / mpegurl）。
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
PAGE = "https://iptv345.com/?act=play&token=968d1884a50db95762d655b51ed11206&tid=gt&id=5"
PAGE_TOKEN = "968d1884a50db95762d655b51ed11206"


def parse_vars(html):
    ent = P.find_ent(html)
    mt = re.search(r'"token="\s*\+\s*(\w+)\s*,\s*"token="\s*\+\s*(\w+)', ent)
    mk = re.search(r'\.replace\(\s*(\w+)\s*,\s*""\s*\)', ent)
    assign = ''
    for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S):
        if re.search(r'var\s+\w+\s*=\s*""\s*;', s) and 'split("").reverse()' in s and 'function' not in s:
            assign = s
            break
    env = P.parse_env(assign)
    old = env.get(mt.group(1), '')
    new = env.get(mt.group(2), '')
    seed = env.get(mk.group(1), '') if mk else ''
    return old, new, seed


def decrypt_known(html, api, tid, cid):
    """已知明文攻击：返回 (raw_urls, old)。"""
    old, new, seed = parse_vars(html)
    opts = re.findall(r'<option value="([^"]+)"', html)
    s0 = P.b64d(opts[0][::-1])
    out0 = P.b64e(("%s/eplay2.php?token=%s&tid=%s&id=%s&p=0&type=.flv"
                    % (api.rstrip('/'), old, tid, cid)).encode()).encode()
    if len(s0) != len(out0):
        return [], old
    key = bytes(s0[j] ^ out0[j] for j in range(len(s0)))
    raws = []
    for opt in opts:
        st = P.b64d(opt[::-1])
        o2 = bytes(st[t] ^ key[t % len(key)] for t in range(len(st)))
        raw = P.b64d(o2.decode('latin1'))
        if raw:
            raws.append(raw.decode('utf-8', 'replace'))
    return raws, old


def fetch_head(u, timeout=12):
    req = urllib.request.Request(u, headers={'User-Agent': P.UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            d = r.read(512)
            return r.status, r.headers.get('Content-Type', ''), len(d), d[:16].hex()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get('Content-Type', ''), 0, ''
    except Exception as e:
        return -1, type(e).__name__ + ':' + str(e)[:40], 0, ''


def main():
    html = P.fetch(PAGE)
    old, new, seed = parse_vars(html)
    print('old  =', old)
    print('new  =', new)
    print('seed =', seed)
    print()
    raws, _ = decrypt_known(html, 'https://iptv345.com', 'gt', '5')
    print('解出 %d 条 raw（未替换 token）：' % len(raws))
    for r in raws[:3]:
        print('   ', r[:140])
    print()

    # token 候选
    tok_new_stripped = new.replace(seed, '')
    tok_cands = {
        'new去seed': tok_new_stripped,
        'new原样': new,
        'old32hex': old,
        'pageToken': PAGE_TOKEN,
    }
    apis = ['https://iptv345.com', 'https://d.2026016.xyz',
            'https://api1.2026016.xyz', 'https://api2.2026016.xyz']
    hits = []
    for tname, tv in tok_cands.items():
        for api in apis:
            for suf in ('&type=.flv', ''):
                for p in (0, 1, 2):
                    u = "%s/eplay2.php?token=%s&tid=gt&id=5&p=%d%s" % (api, tv, p, suf)
                    code, ct, n, hx = fetch_head(u)
                    flag = ''
                    if code == 200:
                        flag = '  ★★★ 200'
                        hits.append((tname, api, suf, p, u, ct, n))
                    print('  %-10s %-26s p=%d %-10s -> %-4s %-22s n=%-5d %s' % (
                        tname, api, p, suf or '(无type)', code, str(ct)[:22], n, flag))
    print('\n===== 命中 =====')
    for h in hits:
        print(' ', h)


if __name__ == '__main__':
    main()
