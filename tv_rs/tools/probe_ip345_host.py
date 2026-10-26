"""
探针：iptv345 解出的 `p.iptv200.com/play.php` 是**被混淆过的域名**。

★ 事实（实测 2026-10-26）：
  - iptv345 播放页的 option 用真 keystream 解出明文 = `https://p.iptv200.com/play.php?token=...`
  - 但页面 HTML 里 `iptv200` / `play.php` / `eplay` **出现 0 次** → 说明这个
    域名是**故意伪装**的（源站拿本机源的 eplay2 换个域名/路径混淆）。
  - 本机源真实可播的形态是 `https://api1.2026016.xyz/eplay2.php?token=...` → 302 → 真实 FLV。

  → 猜测：iptv345 的明文需要**再做一次替换**（域名 p.iptv200.com → 真 API），
    或者真 API 也在 iptv345 自己的域名下但路径不同（如 /play.php）。

做法：把解出的明文做**变体替换**后逐个实测：
    域名候选：iptv345.com / p.iptv200.com / api1.2026016.xyz / api2.2026016.xyz
    路径候选：/play.php / /eplay2.php
    参数：token 用 old(32hex) / new；p=0..2；带不带 type=.flv
判据：HTTP 302（源站行为）或 200 + 流 content-type。
"""
import importlib.util
import re
import ssl
import urllib.request

spec = importlib.util.spec_from_file_location('p', 'tools/probe_ip345_true.py')
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=CTX))


def probe(u, timeout=15):
    req = urllib.request.Request(u, headers={'User-Agent': M.UA})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, r.headers.get('Content-Type', ''), r.headers.get('Location', '')
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get('Content-Type', ''), e.headers.get('Location', '')
    except Exception as e:
        return -1, type(e).__name__ + ':' + str(e)[:40], ''


def main():
    h = M.fetch('https://iptv345.com/?act=play&tid=gt&id=1')
    a = M.analyze(h)
    opts = re.findall(r'<option value="([^"]+)"', h)
    kb = a['keystream']
    st = M.b64d(opts[0][::-1])
    o2 = bytes(st[i] ^ kb[i % len(kb)] for i in range(len(st)))
    raw = M.b64d(o2.decode('latin1')).decode('utf-8', 'replace')
    print('RAW 明文:', raw)
    old, new, seed = a['old'], a['new'], a['seed']
    print('old =', old)
    print('new =', new)
    print('seed=', seed)

    # 变体表
    hosts = ['p.iptv200.com', 'iptv345.com', 'api1.2026016.xyz', 'api2.2026016.xyz',
             'd.2026016.xyz', 'api.2026016.xyz']
    paths = ['/play.php', '/eplay2.php']
    tokens = {'old': old, 'new去seed': new.replace(seed, ''), 'new原样': new}
    hits = []
    for hn, pa, (tn, tv), p in [(a, b, c, d) for a in hosts for b in paths
                                for c in tokens.items() for d in (0, 1)]:
        u = "https://%s%s?token=%s&tid=gt&id=1&p=%d&type=.flv" % (hn, pa, tv, p)
        code, ct, loc = probe(u)
        ok = (code == 302) or (code == 200 and 'html' not in ct.lower())
        flag = ' ★★★' if ok else ''
        if ok:
            hits.append((hn, pa, tn, p, code, ct, loc, u))
            print('  %-22s %-12s %-10s p=%d -> %s %s %s%s' % (hn, pa, tn, p, code, ct[:18], loc[:70], flag))
        else:
            print('  %-22s %-12s %-10s p=%d -> %s' % (hn, pa, tn, p, code))
    print('\n===== 命中 =====')
    for x in hits:
        print(' ', x)


if __name__ == '__main__':
    main()
