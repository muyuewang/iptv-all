"""
探针：系统检查官网源港澳台/台灣分组各频道的网页线路，看流域名是否可达。

★ 背景（实测 2026-10-26）：本机源 `gt`/`wintv123` 分类在源站 app 路由下恒 404，
   回落官网源后嗅探到真实流，但 fengshows 的 CDN 域名 `tlive.fengshows.com`
   **被 DNS 污染**（解析到 Facebook IP 157.240.12.50 / 199.59.148.106，
   IPv6 也是 2a03:2880:...:face:b00c）→ 直连必然失败。
   需要系统摸清：哪些站点的流域名是干净的。

做法：加载每个网页 → 抓页面里的 flv/m3u8 直链 → 对流域名做 DNS 解析 +
      HTTP HEAD 实测可达性，输出表格。
"""
import os
import re
import socket
import ssl
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TV_RS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(TV_RS, '..', 'shared'))
import wvt  # noqa: E402

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def get(u, timeout=20):
    r = urllib.request.Request(u, headers={'User-Agent': UA})
    return urllib.request.urlopen(r, timeout=timeout, context=CTX).read().decode('utf-8', 'replace')


def resolve(host):
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
        return sorted({i[4][0] for i in infos})
    except Exception:
        return []


# 明显是被污染的 IP 段（Facebook / Twitter 的脸书段）
def looks_poisoned(ips):
    bad = []
    for ip in ips:
        p = ip.split('.')
        if len(p) != 4:
            continue
        a, b = int(p[0]), int(p[1])
        if a == 157 and b in (240, 254):
            bad.append(ip)
        if a == 199 and b in (59, 232):
            bad.append(ip)
        if a == 104 and b == 244:
            bad.append(ip)
    return bad


def probe_stream(u):
    """对流地址做 HEAD 探测，返回 (可达, 原因)。"""
    try:
        r = urllib.request.Request(u, headers={'User-Agent': UA}, method='HEAD')
        with urllib.request.urlopen(r, timeout=15, context=CTX) as resp:
            return True, '%d %s' % (resp.status, resp.headers.get('Content-Type', ''))
    except urllib.error.HTTPError as e:
        return (e.code < 400), 'HTTP %d' % e.code
    except Exception as e:
        return False, type(e).__name__ + ': ' + str(e)[:60]


def main():
    groups = sys.argv[1:] or ['香港', '澳門', '台灣']
    chans = wvt.load_channels()
    rows = []
    for c in chans:
        g = c.get('group', '')
        if g not in groups:
            continue
        urls = c.get('urls') or []
        page = urls[0].get('url', '') if urls else ''
        if not page:
            rows.append((g, c.get('name', ''), '(无网页)', '', ''))
            continue
        try:
            html = get(page)
        except Exception as e:
            rows.append((g, c.get('name', ''), page[:50], 'PAGE-FAIL', type(e).__name__))
            continue
        streams = re.findall(r'https?:\\?/\\?/[^"\'\s,]+?\.(?:flv|m3u8)(?:\?[^"\'\s,]*)?', html)
        streams = [s.replace('\\u002F', '/').replace('\\/', '/') for s in streams]
        streams = list(dict.fromkeys(streams))
        if not streams:
            rows.append((g, c.get('name', ''), page[:50], 'NO-STREAM', ''))
            continue
        s = streams[0]
        host = s.split('/')[2]
        ips = resolve(host)
        poisoned = looks_poisoned(ips)
        ok, why = probe_stream(s)
        rows.append((g, c.get('name', ''), host,
                     ('POISON ' + ','.join(poisoned)) if poisoned else ('OK ' + ','.join(ips[:2]) if ok else 'FAIL'),
                     why[:40]))
        print('  probe %-14s %-18s %-28s %s %s' % (g, c.get('name', '')[:18], host[:28],
                                                  rows[-1][3][:24], rows[-1][4]), flush=True)
    print('\n===== 汇总 =====')
    for g, n, h, st, why in rows:
        print('%-8s %-20s %-30s %-26s %s' % (g, n[:20], h[:30], st[:26], why))


if __name__ == '__main__':
    main()
