# -*- coding: utf-8 -*-
"""
纯净电视 · WebViewTV 源适配层
====================================================
把 WEBVIEW 电视（github.com/hxh19950701/WebViewTvLive）的频道源接入本工程。

数据来源（该 App 的频道表托管在 GitHub，App 内为加密 .dat）
------------------------------------------------------------
  server_config.dat   全局配置：官方频道表地址、GitHub 代理列表、PC/移动 UA、公告
  full.dat            频道表：904 频道 / 1114 线路，含 group / name / urls[url, extra]
  webpage_adapter.dat 网页适配规则：按域名给出「找 video / 找播放键 / 找全屏键」的 JS 条件

解密算法（逆向自 App，见 core 层的 C1029 静态块）
-------------------------------------------------
  name  = "com.vasthread.webviewtv.app.LiveApplication"
  key   = reverse(name[:32]).encode()      # AES-128
  iv    = name[:16].encode()
  plain = AES-CBC-PKCS5 decrypt(data) -> GZIP 解包 -> JSON

本模块**纯标准库**（自带 AES-128-CBC 解密实现），不引入任何第三方依赖，
因此桌面端打包不需要新增 hidden-import，也不会撑大 exe。

同步说明
--------
JS 侧（shared/web/tv_core.js）不重写 AES，而是直接消费本模块产出的
`shared/web/wvt_channels.json` 预解密快照，见 shared/README.md。
"""

import os
import re
import ssl
import json
import time
import gzip
import urllib.request

# ------------------------------------------------------------------ 常量
# 解密用的种子就是 App 的 Application 类名，作者把它反过来当 key
_APP_CLASS = "com.vasthread.webviewtv.app.LiveApplication"

GH_OWNER = "hxh19950701"
GH_REPO = "WebViewTvLive"
GH_BRANCH = "main"
GH_RAW = "https://raw.githubusercontent.com/%s/%s/%s" % (GH_OWNER, GH_REPO, GH_BRANCH)
# 打不开 raw.githubusercontent 时的直连地址（内置一份保底，运行时再被 server_config 里的列表覆盖）
GH_RAW_HOSTS = ["https://raw.githubusercontent.com", "https://raw.gitmirror.com"]

# 数据文件版本目录：App 3.0.1 用 data/4
DATA_DIR = "data/4"
DATA_FILES = {
    "config": DATA_DIR + "/server_config.dat",
    "channels": DATA_DIR + "/full.dat",
    "adapters": DATA_DIR + "/webpage_adapter.dat",
}

# 保底 GitHub 代理（实测可用；运行时优先用 server_config 里下发的更全列表）
# ("prefix" 代理：直接拼在完整 raw 地址前面)
DEFAULT_PROXIES = [
    "https://ghproxy.net",
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://gitdl.cn",
    "https://ghproxy.cc",
    "https://dgithub.xyz",
]
# ("host" 代理：整体替换 raw.githubusercontent.com 主机名)
HOST_PROXIES = ["https://raw.gitmirror.com", "https://kkgithub.com"]
# jsDelivr CDN（走 gh 通道，与 raw 通道二选一）
JSDELIVR_CDNS = ["https://fastly.jsdelivr.net", "https://cdn.jsdelivr.net"]

RAW_FILE_TTL = 24 * 3600      # 频道表本地缓存有效期（秒）
HTTP_TIMEOUT = 12             # 单个镜像的超时
TOTAL_BUDGET = 45             # 整次下载的总时间预算（秒）


def _ssl_ctx():
    try:
        ctx = ssl.create_default_context()
    except Exception:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_CTX = _ssl_ctx()


def _ua():
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


# ================================================================== AES-128-CBC
# 只实现解密方向，够用即止；纯 Python（解密 20KB 只需毫秒级，性能无关）
_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11b
    return a & 0xff


def _mul(a, b):
    """GF(2^8) 乘法。"""
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


def _expand_key(key):
    """密钥扩展，支持 AES-128/192/256 -> [(轮密钥, 轮数), ...]。

    本工程实际用到的是 AES-256：App 把 32 字符类名前缀反序后直接当 32 字节密钥。
    """
    nk = len(key) // 4
    assert nk in (4, 6, 8), "仅支持 128/192/256 位密钥"
    nr = nk + 6                                    # 轮数
    w = [list(key[i * 4:i * 4 + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]                      # RotWord
            t = [_SBOX[b] for b in t]              # SubWord
            t[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[b] for b in t]              # AES-256 额外的 SubWord
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    rks = [bytes(sum(w[r * 4:r * 4 + 4], [])) for r in range(nr + 1)]
    return rks, nr


def _add_round_key(s, rk):
    for i in range(16):
        s[i] ^= rk[i]


def _inv_shift_rows(s):
    # state 按列优先排布：s[col*4 + row]
    for r in range(1, 4):
        row = [s[c * 4 + r] for c in range(4)]
        row = row[-r:] + row[:-r]                  # 循环右移 r
        for c in range(4):
            s[c * 4 + r] = row[c]


def _inv_sub_bytes(s):
    for i in range(16):
        s[i] = _INV_SBOX[s[i]]


def _inv_mix_columns(s):
    for c in range(4):
        a = s[c * 4:c * 4 + 4]
        s[c * 4 + 0] = _mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9)
        s[c * 4 + 1] = _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13)
        s[c * 4 + 2] = _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11)
        s[c * 4 + 3] = _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)


def _aes_decrypt_block(block, rks, nr):
    s = list(block)
    _add_round_key(s, rks[nr])
    for rnd in range(nr - 1, 0, -1):
        _inv_shift_rows(s)
        _inv_sub_bytes(s)
        _add_round_key(s, rks[rnd])
        _inv_mix_columns(s)
    _inv_shift_rows(s)
    _inv_sub_bytes(s)
    _add_round_key(s, rks[0])
    return bytes(s)


def aes_cbc_decrypt(data, key, iv):
    """AES-CBC 解密（128/192/256 位密钥，含 PKCS7 去填充），返回 bytes。"""
    rks, nr = _expand_key(key)
    if len(data) % 16:
        data = data[: len(data) - len(data) % 16]
    out = bytearray()
    prev = iv
    for off in range(0, len(data), 16):
        blk = data[off:off + 16]
        dec = _aes_decrypt_block(blk, rks, nr)
        out += bytes(a ^ b for a, b in zip(dec, prev))
        prev = blk
    if out:
        pad = out[-1]
        if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
            del out[-pad:]
    return bytes(out)


# ================================================================== .dat 解密
def decrypt_dat(data):
    """把 WebViewTvLive 的 .dat 解成 JSON 文本。"""
    key = _APP_CLASS[:32][::-1].encode("utf-8")
    iv = _APP_CLASS[:16].encode("utf-8")
    plain = aes_cbc_decrypt(data, key, iv)
    try:
        plain = gzip.decompress(plain)
    except Exception:
        pass                                        # 少数情况下未压缩
    return plain.decode("utf-8", "ignore")


def decrypt_dat_json(data):
    return json.loads(decrypt_dat(data))


# ================================================================== 下载
def _candidate_urls(rel, proxies=None):
    """
    生成候选下载地址，顺序 = 「直连 -> jsDelivr -> 保底代理 -> 服务端下发的代理」。

    代理有两种形态：
      · prefix：把完整 raw 地址拼在后头，如 https://ghproxy.net/https://raw.githubusercontent.com/...
      · host  ：整体替换 raw.githubusercontent.com 主机名，如 https://raw.gitmirror.com/hxh19950701/...
    """
    raw_url = "%s/%s" % (GH_RAW, rel)
    out, seen = [], set()

    def add(u):
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    # 1) 直连（非墙内网络最快）
    add(raw_url)
    # 2) jsDelivr（国内可达性最好）
    for cdn in JSDELIVR_CDNS:
        add("%s/gh/%s/%s@%s/%s" % (cdn, GH_OWNER, GH_REPO, GH_BRANCH, rel))
    # 3) host 型代理
    for h in HOST_PROXIES:
        add(h.rstrip("/") + "/" + "%s/%s/%s/%s" % (GH_OWNER, GH_REPO, GH_BRANCH, rel))
    # 4) prefix 型代理（保底 + 服务端下发），保底排在前面保证速度
    plist = list(DEFAULT_PROXIES) + list(proxies or [])
    for p in plist:
        p = (p or "").strip().rstrip("/")
        if not p or "jsdelivr" in p or "jsd" in p:
            continue
        add(p + "/" + raw_url)
    return out


def download_dat(rel, proxies=None, timeout=HTTP_TIMEOUT, budget=TOTAL_BUDGET):
    """下载某个数据文件（带多镜像回退），返回 (bytes, 生效地址)。"""
    import time as _t
    t0 = _t.time()
    last = None
    for url in _candidate_urls(rel, proxies):
        if _t.time() - t0 > budget:
            break
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _ua(), "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                data = r.read()
            if data and len(data) > 64:
                return data, url
            last = RuntimeError("响应过小")
        except Exception as e:                      # noqa: BLE001 —— 换下一个镜像
            last = e
    raise last or RuntimeError("全部镜像均不可用")


# ================================================================== 缓存 + 解析
def _cache_path(cache_dir, name):
    return os.path.join(cache_dir or ".", "wvt_" + name + ".json")


def _read_cache(cache_dir, name, ttl):
    p = _cache_path(cache_dir, name)
    try:
        if ttl and time.time() - os.path.getmtime(p) > ttl:
            return None
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(cache_dir, name, obj):
    p = _cache_path(cache_dir, name)
    try:
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        return True
    except Exception:
        return False


# 内置保底 UA（server_config 拉不到时用）
FALLBACK_CONFIG = {
    "pcUserAgent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"),
    "mobileUserAgent": ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36"),
    "githubProxies": DEFAULT_PROXIES,
}


# 内置兜底快照：随包发布的一份「已解密的频道表」。
# 作用：镜像全挂 / 首次启动离线时仍能出频道，而不是空白。
BUILTIN_NAME = "wvt_builtin.json"


def builtin_snapshot():
    """读取随包发布的内置快照（含 channels / adapters）。"""
    import sys
    base = getattr(sys, "_MEIPASS", None)
    cands = []
    if base:
        cands.append(os.path.join(base, "shared", BUILTIN_NAME))
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.join(here, BUILTIN_NAME))
    cands.append(os.path.join(here, "..", "shared", BUILTIN_NAME))
    for p in cands:
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def load_config(cache_dir=None, ttl=RAW_FILE_TTL, force=False):
    """取 server_config（含官方频道表地址 / 代理列表 / UA）。"""
    if not force:
        c = _read_cache(cache_dir, "config", ttl)
        if c:
            return c
    try:
        data, _ = download_dat(DATA_FILES["config"])
        cfg = decrypt_dat_json(data)
        _write_cache(cache_dir, "config", cfg)
        return cfg
    except Exception:
        return dict(FALLBACK_CONFIG)


def load_channels(cache_dir=None, ttl=RAW_FILE_TTL, force=False, proxies=None):
    """
    取频道表，返回 [{group, name, urls:[{url, extra}]}, ...]。
    顺序：本地缓存 -> 联网下载 -> 内置快照（保证任何情况下都有数据）。
    """
    if not force:
        c = _read_cache(cache_dir, "channels", ttl)
        if c:
            return c
    try:
        proxies = proxies or (load_config(cache_dir).get("githubProxies") or DEFAULT_PROXIES)
        data, _ = download_dat(DATA_FILES["channels"], proxies=proxies)
        chans = decrypt_dat_json(data)
        if chans:
            _write_cache(cache_dir, "channels", chans)
            return chans
    except Exception:
        pass
    c = _read_cache(cache_dir, "channels", 0)      # 过期缓存也好过没有
    if c:
        return c
    return builtin_snapshot().get("channels", [])


def load_adapters(cache_dir=None, ttl=RAW_FILE_TTL, force=False, proxies=None):
    """取网页适配规则，返回 [{url:域名, ua, videoElementCondition, ...}, ...]。"""
    if not force:
        c = _read_cache(cache_dir, "adapters", ttl)
        if c:
            return c
    try:
        proxies = proxies or (load_config(cache_dir).get("githubProxies") or DEFAULT_PROXIES)
        data, _ = download_dat(DATA_FILES["adapters"], proxies=proxies)
        ads = decrypt_dat_json(data)
        if ads:
            _write_cache(cache_dir, "adapters", ads)
            return ads
    except Exception:
        pass
    c = _read_cache(cache_dir, "adapters", 0)
    if c:
        return c
    return builtin_snapshot().get("adapters", [])


def peek(cache_dir=None):
    """
    **不联网**能立刻拿到的频道数据：本地缓存（含过期）-> 内置快照。

    与 shared/web/wvt.js 的 `load()` 里「本地缓存 -> 内置快照（立刻可用）」
    这一段同义，供桌面端实现「先上屏、后台再联网刷新」。

    为什么必须有它：冷启动（用户第一次双击 exe、无缓存、网络又不佳）时
    `load_channels` 要等 `load_config`（多镜像串行，单镜像 12s / 总预算 45s）
    再等 channels（又是 45s），`load_adapters` 又一遍 —— 实测本机 26s，
    网络差时三段各吃满预算可达 2 分钟以上。而内置快照 0.01s 就有全量频道，
    没有任何理由让用户对着空列表等。

    返回 (channels, adapters, origin)，origin ∈ {"cache", "builtin", "none"}。
    """
    ch = _read_cache(cache_dir, "channels", 0)
    if ch:
        ad = _read_cache(cache_dir, "adapters", 0)
        if not ad:
            ad = builtin_snapshot().get("adapters", [])
        return ch, ad, "cache"
    snap = builtin_snapshot()
    if snap.get("channels"):
        return (snap.get("channels") or [], snap.get("adapters") or [], "builtin")
    return [], [], "none"


# ================================================================== 线路分类
MEDIA_EXT = (".m3u8", ".flv", ".ts", ".mp4", ".mkv", ".mpd")


def is_direct_stream(url):
    """判断某条线路是否「可直接播放」（不需要浏览器嗅探）。"""
    if not url:
        return False
    u = url.split("?")[0].lower()
    if u.endswith(MEDIA_EXT):
        return True
    # 形如 http://m3u8.cec-tv.com.cn/?url=...m3u8 的转发口
    if ".m3u8" in url.lower():
        return True
    return False


def split_sources(channels):
    """把线路分成 (直连, 网页) 两组，网页的带上频道与线路信息。"""
    direct, web = [], []
    for c in channels:
        for idx, u in enumerate(c.get("urls", [])):
            item = {"group": c.get("group", ""), "name": c.get("name", ""),
                    "url": u.get("url", ""), "extra": u.get("extra", ""),
                    "index": idx, "total": len(c.get("urls", []))}
            (direct if is_direct_stream(item["url"]) else web).append(item)
    return direct, web


def match_adapter(url, adapters):
    """按域名匹配网页适配规则；没匹配到返回 None（走通用规则）。"""
    if not adapters:
        return None
    host = re.sub(r"^https?://", "", (url or ""), flags=re.I).split("/")[0].lower()
    best = None
    for a in adapters:
        key = (a.get("url") or "").lower()
        if not key:
            continue
        if key in host:
            if best is None or len(key) > len(best.get("url", "")):
                best = a
    return best


# ================================================================== 自检
if __name__ == "__main__":
    import sys
    cd = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_wvt_cache")
    print("== 解密自检 ==")
    try:
        data, via = download_dat(DATA_FILES["channels"])
        chans = json.loads(decrypt_dat(data))
        print("频道数:", len(chans), "| 来源:", via)
        d, w = split_sources(chans)
        print("直连线路:", len(d), "| 网页线路:", len(w))
        groups = {}
        for c in chans:
            groups[c.get("group", "?")] = groups.get(c.get("group", "?"), 0) + 1
        print("分组数:", len(groups), "| 前 8:", list(groups.items())[:8])
        c0 = chans[0]
        print("示例:", c0["name"], c0["group"], json.dumps(c0["urls"][:1], ensure_ascii=False))
    except Exception as e:
        print("失败:", repr(e))
        sys.exit(1)
    print("== 适配规则自检 ==")
    ads = load_adapters(cd, force=True)
    print("适配规则:", len(ads), "条 | 域名:", [a.get("url") for a in ads][:10])
