"""
原子测试：抓 iptv345 播放页 → 解密 → **立刻**请求解出的流（token 可能短命）。

★ 要验证的点：
  ① 解出的 eplay2 URL 能不能返回流（200 + 流数据）？
  ② 参数变体：`type=.flv` 有无、`tid`/`id` 是否要换、`p` 索引。
  ③ token 是否短命（页面 token 与 eplay2 token 是否同一套）。
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
UA = P.UA

PAGE = "https://iptv345.com/?act=play&token=968d1884a50db95762d655b51ed11206&tid=gt&id=5"


def raw_get(u, timeout=15):
    """返回 (http_code, 前 64 字节 hex, 长度)。"""
    req = urllib.request.Request(u, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            data = r.read(4096)
            return r.status, r.headers.get('Content-Type', ''), len(data), data[:24].hex()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get('Content-Type', ''), 0, ''
    except Exception as e:
        return -1, type(e).__name__ + ':' + str(e)[:50], 0, ''


def main():
    t0 = time.time()
    html = P.fetch(PAGE)
    print('抓页耗时 %.1fs len=%d' % (time.time() - t0, len(html)))

    # 复用 probe_ip345_tmpl 的模板定位
    sys_mod = importlib.util.module_from_spec(spec)
    ent, env, m_tok, m_key, hexes = None, None, None, None, None
    m = re.search(r'"token="\s*\+\s*(\w+)\s*,\s*"token="\s*\+\s*(\w+)', P.find_ent(html))
    mk = re.search(r'\.replace\(\s*(\w+)\s*,\s*""\s*\)', P.find_ent(html))
    assign = ''
    for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S):
        if re.search(r'var\s+\w+\s*=\s*""\s*;', s) and 'split("").reverse()' in s and 'function' not in s:
            assign = s
            break
    env = P.parse_env(assign)
    old = env.get(m.group(1), '')
    new = env.get(m.group(2), '')
    seed = env.get(mk.group(1), '') if mk else ''
    opts = re.findall(r'<option value="([^"]+)"', html)
    print('old =', old)
    print('new =', new)
    print('seed=', seed)
    print('opts=', len(opts))
    if not old or not new or not opts:
        print('解析失败')
        return

    # 用 old 做已知明文（长度唯一锁定域名 iptv345.com）
    s0 = P.b64d(opts[0][::-1])
    tmpl = 'https://iptv345.com/eplay2.php?token=%s&tid=gt&id=5&p=0&type=.flv' % old
    out0 = P.b64e(tmpl.encode()).encode()
    if len(s0) != len(out0):
        print('长度不符 %d vs %d' % (len(s0), len(out0)))
        return
    key = bytes(s0[j] ^ out0[j] for j in range(len(s0)))

    def dec(opt):
        st = P.b64d(opt[::-1])
        o2 = bytes(st[t] ^ key[t % len(key)] for t in range(len(st)))
        raw = P.b64d(o2.decode('latin1'))
        u = raw.decode('utf-8', 'replace')
        u = u.replace('token=' + old, 'token=' + new)
        if seed:
            u = u.replace(seed, '')
        return u

    urls = [dec(o) for o in opts]
    print('\n解出 %d 条：' % len(urls))
    for u in urls[:4]:
        print('   ', u[:140])

    print('\n=== 立刻实测各线路 ===')
    for i, u in enumerate(urls[:6]):
        code, ct, n, hx = raw_get(u)
        print('  p=%d code=%-4s ct=%-24s n=%-6d %s' % (i, code, str(ct)[:24], n, hx))
        if code == 200 and n > 0:
            print('     ★ 可用:', u[:140])

    print('\n=== 参数变体（用 p=0） ===')
    base = urls[0]
    variants = [
        ('去 type', base.replace('&type=.flv', '')),
        ('换 tid=ws', base.replace('tid=gt', 'tid=ws')),
        ('换 id=1', base.replace('id=5', 'id=1')),
        ('无 tid/id', base.split('&tid=')[0]),
    ]
    for name, u in variants:
        code, ct, n, hx = raw_get(u)
        print('  %-12s code=%-4s ct=%-20s n=%-6d %s' % (name, code, str(ct)[:20], n, hx))


if __name__ == '__main__':
    main()
