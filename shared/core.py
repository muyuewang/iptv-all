# -*- coding: utf-8 -*-
"""
纯净电视直播 · 共享核心（Python 版）
================================================
三端（Windows / Linux）桌面版共用的唯一业务逻辑层：
  · App 伪装 UA 抓取
  · 频道列表解析
  · 播放页线路解密（已知明文反推 XOR key，绕开动态盐）
  · EPG 节目单解析
  · 收藏读写

设计要点
--------
1. 这个模块不含任何 GUI / 平台代码，纯标准库，可直接被 tv_app.py 导入。
2. 安卓端用的是等价的 JS 版（shared/web/tv_core.js），逻辑逐行对齐，
   改动本文件时务必同步那份，见 shared/README.md 的同步对照表。
3. 解密不依赖动态盐：用第 1 条线路的「已知明文」反推 XOR 密钥，
   因此无论服务器 salt 怎么变（deviceId/appVersion/ips/日期）都能解出整页线路。
"""

import os
import re
import ssl
import json
import time
import base64
import urllib.request
import urllib.error

# ------------------------------------------------------------------ 常量
APP_UA = ("Mozilla/5.0 (Linux; Android 9; SM-N9700 Build/PQ3B.190801.01311438; wv) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 "
          "Mobile Safari/537.36 diashizhb LT-APP/48/517/YM-RT/")

API = "https://api1.2026016.xyz"

HDRS = {
    "User-Agent": APP_UA,
    "x-requested-with": "com.sldv.okstuffx",
    "Cookie": "adReward=1; iptvad=1; appVer=517",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# (tid, 显示名)
CATS = [("tv", "综合"), ("ty", "体育"), ("ys", "央视"), ("ws", "卫视"),
        ("wintv123", "极速港澳台"), ("gt", "港澳台")]

CACHE_TTL = 600          # 频道列表内存缓存秒数


def _ssl_ctx():
    """部分源站证书链不完整，这里放宽校验（仅用于读取公开直播页）。"""
    try:
        ctx = ssl.create_default_context()
    except Exception:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_CTX = _ssl_ctx()


# ------------------------------------------------------------------ 基础工具
def http_get(url, timeout=15, headers=None):
    """带 App UA 的 GET，返回 utf-8 文本。"""
    req = urllib.request.Request(url, headers=headers or HDRS)
    return urllib.request.urlopen(req, timeout=timeout, context=_CTX) \
        .read().decode("utf-8", "ignore")


def b64d(s):
    """宽容 base64 解码（自动补 padding），返回 latin1 字符串或 None。"""
    if s is None:
        return None
    s = s.strip()
    try:
        return base64.b64decode(s + "=" * ((-len(s)) % 4)).decode("latin1")
    except Exception:
        return None


def b64e(s):
    return base64.b64encode(s.encode("latin1")).decode("ascii")


def _concat_val(expr):
    """把 JS 里的 "a"."b".split("").reverse().join("") 常量拼接式求值。"""
    expr = re.sub(r'"([^"]*)"\s*\.split\(""\)\s*\.reverse\(\)\s*\.join\(""\)',
                  lambda m: '"' + m.group(1)[::-1] + '"', expr)
    return "".join(re.findall(r'"([^"]*)"', expr))


def _parse_env(block):
    """解析一小段 JS 变量赋值块 -> {变量名: 字符串值}。"""
    env = {}
    for m in re.finditer(r'(?:var\s+)?([A-Za-z_$][\w$]*)\s*=\s*([^;]+);', block):
        name, expr = m.group(1), m.group(2).strip()
        if re.fullmatch(r'[A-Za-z_$][\w$]*', expr):
            env[name] = env.get(expr, "")
        else:
            env[name] = _concat_val(expr)
    return env


# ------------------------------------------------------------------ 频道列表
def parse_channels(html, tid):
    """从分类页 HTML 解析 [(id, name), ...]。"""
    out, seen = [], set()
    for m in re.finditer(r"act=play[^'\"]*&tid=([a-z0-9]+)&id=(\d+)['\"]?[^>]*>([^<]+)<", html):
        if m.group(1) != tid or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        out.append({"id": m.group(2), "name": m.group(3).strip()})
    return out


def fetch_channels(tid, cache=None, use_cache=True):
    """拉取并缓存某分类频道列表。cache 传 dict 即可复用。"""
    now = time.time()
    if use_cache and cache is not None and tid in cache and now - cache[tid][0] < CACHE_TTL:
        return cache[tid][1]
    html = http_get("%s/iptve.php?tid=%s&app=517" % (API, tid))
    chans = parse_channels(html, tid)
    if cache is not None:
        cache[tid] = (now, chans)
    return chans


# ------------------------------------------------------------------ 解密
def decrypt_page(html, tid, cid):
    """
    还原播放页里的全部线路地址。
    返回 (urls:list[str], msg:str)

    算法：option 值反转 -> base64 解 -> XOR(key) -> base64 解 -> 换 token -> 去盐前缀。
    key 由「第 1 条线路的已知明文」与自造的同构密文逐字节异或反推而成。
    """
    try:
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
        assign = ent = None
        for s in scripts:
            if assign is None and re.search(r'var\s+\w+\s*=\s*""\s*;', s) \
                    and 'split("").reverse()' in s and 'function' not in s:
                assign = s
            if ent is None and re.search(r'function\s+\w+\s*\([^)]*\)\s*\{[^}]*\.reverse\(\)[^}]*\}', s):
                ent = s
        if not assign or not ent:
            return [], "页面结构变化"

        m_key = re.search(r'\(\s*\w+\s*,\s*(\w+)\s*\)', ent)
        m_tok = re.search(r'"token="\+(\w+)\s*,\s*"token="\+(\w+)', ent)
        if not m_key or not m_tok:
            return [], "参数缺失"

        env = _parse_env(assign)
        key_seed = env.get(m_key.group(1), "")
        old = env.get(m_tok.group(1), "")   # 密文里出现的旧 token（要替换掉）
        new = env.get(m_tok.group(2), "")   # 明文里应出现的新 token
        if not key_seed or not old or not new:
            return [], "变量为空"

        opts = re.findall(r'<option value="([^"]+)"', html)
        if not opts:
            return [], "无线路数据"

        # 用第 1 条线路构造已知明文 -> 反推 XOR key
        s0 = b64d(opts[0][::-1])
        if not s0:
            return [], "解码失败"
        url0 = "%s/eplay2.php?token=%s&tid=%s&id=%s&p=0&type=.flv" % (API, old, tid, cid)
        out0 = b64e(url0)
        if len(s0) != len(out0):
            return [], "密钥长度不符"
        key = [ord(s0[j]) ^ ord(out0[j]) for j in range(len(s0))]

        urls = []
        for opt in opts:
            st = b64d(opt[::-1])
            if not st:
                continue
            o2 = "".join(chr(ord(st[t]) ^ key[t % len(key)]) for t in range(len(st)))
            raw = b64d(o2)
            if not raw:
                continue
            fin = raw.replace("token=" + old, "token=" + new).replace(key_seed, "")
            if not re.match(r'^https?://', fin):
                continue
            if "type=" not in fin:          # 统一补 FLV，保证 8 条线路都能播
                fin += "&type=.flv"
            if fin not in urls:
                urls.append(fin)
        return urls, "ok"
    except Exception as e:
        return [], "异常:%r" % (e,)


# ------------------------------------------------------------------ EPG
def parse_epg(html):
    """解析播放页里的当日节目单 -> [{'time':'20:00','name':'...'}, ...]"""
    out = []
    if 'id="myEpg"' not in html:
        return out
    seg = html.split('id="myEpg"')[1].split("</ul>")[0]
    for m in re.finditer(r'<li[^>]*>.*?<span>\s*([0-9]{1,2}:[0-9]{2})\s*([^<]*)</span>', seg, re.S):
        out.append({"time": m.group(1), "name": m.group(2).strip()})
    return out


def fetch_play(tid, cid):
    """抓播放页 -> (urls, epg, msg)"""
    html = http_get("%s/iptve.php?act=play&tid=%s&id=%s" % (API, tid, cid))
    urls, msg = decrypt_page(html, tid, cid)
    return urls, parse_epg(html), msg


# ------------------------------------------------------------------ 收藏
def load_fav(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_fav(path, arr):
    try:
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(arr, f, ensure_ascii=False)
        return True
    except Exception:
        return False


def play_url(tid, cid, p=0):
    """直出播放地址（调试/外链用）。"""
    html = http_get("%s/iptve.php?act=play&tid=%s&id=%s" % (API, tid, cid))
    urls, _ = decrypt_page(html, tid, cid)
    if 0 <= p < len(urls):
        return urls[p]
    return urls[0] if urls else None


if __name__ == "__main__":
    # 自检：python core.py
    print("UA:", APP_UA[:60], "...")
    ch = fetch_channels("ys")
    print("央视频道数:", len(ch), ch[:3])
    if ch:
        u, e, msg = fetch_play("ys", ch[0]["id"])
        print("第一条线路:", (u[0] if u else None))
        print("线路数:", len(u), "EPG条目:", len(e), "msg:", msg)
