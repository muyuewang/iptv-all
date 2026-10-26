"""
探针：暴力定位 iptv345 播放页的真明明文模板。

★ 为什么需要猜：已知明文攻击里，模板的 **base64 长度** 唯一锁定域名
  （iptv345.com = 11 字符 → 模板 96 字节 → base64 128 字符 = s0 长度），
  但模板里的 **old token 值**还有歧义：env 里有两个 32 位 hex
  （`bxiqh` 与 key seed `gpqwh`）。取错 → XOR key 全错 → 解出乱码 → 404。

★ 判据（不靠猜）：解出的 raw 必须 `^https?://` 且含 `eplay2.php?token=`。
  一旦某组 (域名, old, 参数模板) 让**所有** option 都解出合法 URL，即为正解。
"""
import base64
import importlib.util
import itertools
import re

spec = importlib.util.spec_from_file_location('p', 'tools/probe_ip345_dec.py')
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

URL = "https://iptv345.com/?act=play&token=968d1884a50db95762d655b51ed11206&tid=gt&id=5"
TID, CID = 'gt', '5'


def collect_vars(html):
    """收集所有 32 位 hex 值的变量名（old token 候选）。"""
    ent = P.find_ent(html)
    m_tok = re.search(r'"token="\s*\+\s*(\w+)\s*,\s*"token="\s*\+\s*(\w+)', ent)
    m_key = re.search(r'\.replace\(\s*(\w+)\s*,\s*""\s*\)', ent)
    assign = ''
    for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S):
        if re.search(r'var\s+\w+\s*=\s*""\s*;', s) and 'split("").reverse()' in s and 'function' not in s:
            assign = s
            break
    env = P.parse_env(assign)
    cands = set()
    for k, v in env.items():
        if re.fullmatch(r'[0-9a-f]{32}', v or ''):
            cands.add((k, v))
    return ent, env, m_tok, m_key, sorted(cands)


def attempt(html, env, old, new, seed, api, p0, with_type):
    opts = re.findall(r'<option value="([^"]+)"', html)
    if not opts:
        return None, 'no opts'
    s0 = P.b64d(opts[0][::-1])
    if not s0:
        return None, 'b64 fail'
    suf = '&type=.flv' if with_type else ''
    tmpl = "%s/eplay2.php?token=%s&tid=%s&id=%s&p=%d%s" % (api.rstrip('/'), old, TID, CID, p0, suf)
    out0 = P.b64e(tmpl.encode()).encode()
    if len(s0) != len(out0):
        return None, 'len %d vs %d' % (len(s0), len(out0))
    key = bytes(s0[j] ^ out0[j] for j in range(len(s0)))
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
        if not re.match(r'^https?://', u):
            continue
        if 'eplay2.php?token=' not in u:
            continue
        urls.append(u)
    if not urls:
        return None, 'decoded but no valid url'
    # 换 token + 去 seed
    fin = []
    for u in urls:
        x = u.replace('token=' + old, 'token=' + new)
        if seed:
            x = x.replace(seed, '')
        fin.append(x)
    return fin, 'OK'


def main():
    html = P.fetch(URL)
    ent, env, m_tok, m_key, hexes = collect_vars(html)
    print('token 对:', m_tok.group(1), '->', m_tok.group(2))
    print('seed 变量:', m_key.group(1) if m_key else None)
    print('32hex 候选:', [(k, v) for k, v in hexes])
    tv_old, tv_new = m_tok.group(1), m_tok.group(2)
    new = env.get(tv_new, '')
    seed_var = m_key.group(1) if m_key else None
    seed = env.get(seed_var, '') if seed_var else ''
    print('new =', new)
    print('seed=', seed)
    print()
    apis = ['https://iptv345.com', 'https://d.2026016.xyz', 'https://api1.2026016.xyz',
            'https://api2.2026016.xyz', 'https://api.2026016.xyz']
    for (vn, vv), api, p0, wt in itertools.product(hexes, apis, [0, 1], [True, False]):
        fin, why = attempt(html, env, vv, new, seed, api, p0, wt)
        tag = '★' if fin else ' '
        print('%s %-10s %-26s p=%d type=%-5s %-24s %s' % (
            tag, vn, api, p0, wt, why, len(fin) if fin else ''))
        if fin:
            for u in fin[:3]:
                print('      ', u[:135])
            print()


if __name__ == '__main__':
    main()
