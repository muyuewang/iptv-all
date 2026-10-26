"""
探针：解 iptv345.com 播放页的加密线路（与本机源同格式、变量名随机）。

★ 关键差异（实测 2026-10-26）：
  本机源 ent 块（单函数）：
      function bhrpv(jji) { jji=jji.split("").reverse().join(""); jji=decodeb(jji,bzhhh);
                            jji=jji.replace("token="+uayqg,"token="+nnuht); jji=jji.replace(bzhhh,""); }
  iptv345 ent 块（三函数）：
      function hjyzz(data){...} function yrixv(str,key){...} function kmatd(hsj){
          hsj=hsj.split("").reverse().join(""); hsj=yrixv(hsj,bkvgh);
          hsj=hsj.replace("token="+pypbr,"token="+mfghg); hsj=hsj.replace(bkvgh,""); }
  且**所有变量名每次请求都随机**（skzwn/egaht/psbte…）。

  → 原来按「形参名/固定名」取变量的做法在 iptv345 上一律失效。
  → 鲁棒取法：**从 `.replace(<Z>, "")` 里取 key 变量名 Z**（两个源都有这句，
    且 Z 一定是真 key 变量）；token 对从 `"token="+X,"token="+Y` 取 X/Y。
  → 再用「已知明文攻击」反推 XOR key：明文模板 = `{api}/eplay2.php?token={old}&tid={tid}&id={cid}&p=0[&type=.flv]`
"""
import base64
import re
import ssl
import urllib.request

UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120 Mobile Safari/537.36 diashizhb LT-APP/48/517")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def fetch(url, ua=UA):
    """抓页面（源站间歇超时/断流，重试 4 次）。"""
    import time
    last = None
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': ua})
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


def b64e(b):
    return base64.b64encode(b).decode()


def parse_env(assign):
    """解析 assign 块：字面量 / 拼接表达式 / 二级引用（X = Y），可递归。"""
    raw = {}
    for m in re.finditer(r'(\w+)\s*=\s*([^;]+);', assign):
        name, expr = m.group(1), m.group(2).strip()
        if expr.startswith('"') and expr.endswith('"') and expr.count('"') == 2:
            raw[name] = expr[1:-1]
            continue
        parts = re.findall(r'"([^"]*)"(\.split\(""\)\.reverse\(\)\.join\(""\))?', expr)
        if parts:
            buf = ''.join(lit[::-1] if rev else lit for lit, rev in parts)
            raw[name] = buf
    # 二级引用（可多级）：X = Y
    refs = {}
    for m in re.finditer(r'(\w+)\s*=\s*(\w+)\s*;', assign):
        refs[m.group(1)] = m.group(2)

    def resolve(name, depth=0):
        if depth > 8:
            return ''
        # ★ 注意：初始声明 `var X = "";` 会留下一条**空值** raw 记录，
        #   它必须让位给后面的 `X = Y`（真实赋值）。所以先看引用。
        src = refs.get(name)
        if src:
            v = resolve(src, depth + 1)
            if v:
                return v
        return raw.get(name, '')

    out = {}
    for name in set(list(raw.keys()) + list(refs.keys())):
        out[name] = resolve(name)
    return out


def find_ent(html):
    """ent 块 = 含 `.reverse().join("")` 且含 `"token="+X` 的 script。"""
    for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S):
        if 'split("").reverse().join("")' in s and '"token="' in s:
            return s
    return None


def try_decrypt(html, api, tid, cid):
    ent = find_ent(html)
    if not ent:
        return [], 'no ent'
    # token 对
    m_tok = re.search(r'"token="\s*\+\s*(\w+)\s*,\s*"token="\s*\+\s*(\w+)', ent)
    if not m_tok:
        return [], 'no tok'
    tv_old, tv_new = m_tok.group(1), m_tok.group(2)
    # key 变量名：`.replace(<Z>, "")`
    m_key = re.search(r'\.replace\(\s*(\w+)\s*,\s*""\s*\)', ent)
    if not m_key:
        return [], 'no keyvar'
    keyvar = m_key.group(1)
    # assign 块
    assign = ''
    for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S):
        if re.search(r'var\s+\w+\s*=\s*""\s*;', s) and 'split("").reverse()' in s and 'function' not in s:
            assign = s
            break
    env = parse_env(assign)
    old = env.get(tv_old, '')
    new = env.get(tv_new, '')
    key_seed = env.get(keyvar, '')
    if not old or not new:
        return [], 'tok empty %r/%r' % (old, new)
    opts = re.findall(r'<option value="([^"]+)"', html)
    if not opts:
        return [], 'no opts'
    s0 = b64d(opts[0][::-1])
    if not s0:
        return [], 'b64 fail'
    key = None
    for tmpl in (
        "%s/eplay2.php?token=%s&tid=%s&id=%s&p=0&type=.flv" % (api.rstrip('/'), old, tid, cid),
        "%s/eplay2.php?token=%s&tid=%s&id=%s&p=0" % (api.rstrip('/'), old, tid, cid),
    ):
        out0 = b64e(tmpl.encode()).encode()
        if len(s0) == len(out0):
            key = bytes(s0[j] ^ out0[j] for j in range(len(s0)))
            break
    if key is None:
        return [], 'key len mismatch s0=%d' % len(s0)
    urls = []
    for opt in opts:
        st = b64d(opt[::-1])
        if not st:
            continue
        o2 = bytes(st[t] ^ key[t % len(key)] for t in range(len(st)))
        raw2 = b64d(o2.decode('latin1'))
        if not raw2:
            continue
        fin = raw2.decode('utf-8', 'replace')
        fin = fin.replace("token=" + old, "token=" + new)
        if key_seed:
            fin = fin.replace(key_seed, "")
        if not re.match(r'^https?://', fin):
            continue
        if 'type=' not in fin:
            fin += '&type=.flv'
        if fin not in urls:
            urls.append(fin)
    return urls, 'ok'


if __name__ == '__main__':
    print("### 对照：本机源 ys:1（app UA） ###")
    h = fetch("https://api1.2026016.xyz/iptve.php?act=play&tid=ys&id=1",
              UA + "/YM-RT/")
    u, w = try_decrypt(h, 'https://api1.2026016.xyz', 'ys', '1')
    print(w, len(u))
    for x in u[:2]:
        print('   ', x[:120])

    print("\n### iptv345 各频道 ###")
    CASES = [
        ("翡翠台", "https://iptv345.com/?act=play&token=968d1884a50db95762d655b51ed11206&tid=gt&id=5", "gt", "5"),
        ("TVB Plus", "https://iptv345.com/?act=play&token=f47d1bc8615b63ba6b5d0dc6622715da&tid=gt&id=8", "gt", "8"),
        ("ViuTV", "https://iptv345.com/?act=play&token=6e02777912b07710689aed87e748d963&tid=gt&id=19", "gt", "19"),
        ("ViuTV6", "https://iptv345.com/?act=play&token=5684baa22e5d77d14c51aa32f41a71f3&tid=gt&id=20", "gt", "20"),
        ("Now 財經", "https://iptv345.com/?act=play&token=5ff6467993be64c7c6fec03f1d04f840&tid=gt&id=18", "gt", "18"),
    ]
    for name, url, tid, cid in CASES:
        html = fetch(url)
        urls, why = try_decrypt(html, 'https://iptv345.com', tid, cid)
        print('\n===== %s ===== %s (%d 条)' % (name, why, len(urls)))
        for x in urls[:4]:
            print('   ', x[:130])
