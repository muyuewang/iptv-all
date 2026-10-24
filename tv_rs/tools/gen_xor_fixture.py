#!/usr/bin/env python3
"""
生成 `decrypt_page` 的**合成加密页面**测试向量。

用途：`shared/core.py::decrypt_page` 是整条官网源链路的命门（XOR 已知明文
攻击），但仓库里没有真实播放页样本（页面动态下发、且带时效 token）。
这个脚本按 decrypt_page 的**逆过程**构造同构页面，算法与真实页面一致。

────────────────────────────────────────────────────────────────────
关键理解（不看懂这几个约束就造不出能解的页面）：

1) **第 1 条线路不能带 salt 前缀**。
   decrypt_page 用第 1 条线路做已知明文攻击：
       s0  = b64d(opt0[::-1])          # 密文字节
       key = s0 XOR b64e(url0)         # url0 由 api/tid/cid/old_token 拼出
   同时加密侧 st0 = b64e(raw0) XOR key。
   两式自洽的唯一条件是 raw0 == url0，即第 1 条线路**没有 salt 前缀**。
   其余线路才带 key_seed 前缀（解密时被 replace(key_seed,"") 去掉）。

2) **key 可以自由选定**。
   由 1) 可推 key ≡ 任意字节串，长度 = len(b64e(url0))。
   这里刻意让 key 含高位字节 → XOR 中间产物 st 出现非 UTF-8 字节，
   正是 Rust 侧 `String::from_utf8_lossy` 会静默解坏、Python latin1
   不会丢信息的场景，专门用来锁死该差异。

3) **assign 块必须先 `var x = "";` 再赋值**。
   decrypt_page 用 `var\\s+\\w+\\s*=\\s*""\\s*;` 挑选 assign 块，
   且该块内不能出现 "function"（含 function 的块会被当成 ent 块）。
   变量取值走 `_parse_env` + `_concat_val`，故
       x = "字面量".split("").reverse().join("");   =>  x 取该字面量的反序

输出：
  tv_rs/tests/fixtures/page_xor.html         合成页面（UTF-8）
  tv_rs/tests/fixtures/page_xor.expect.json  Python 侧解密期望结果
"""
import base64
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "..", "tests", "fixtures")

# ★ 必须用 core.py 里的真实 API 常量。
# decrypt_page 构造第 1 条线路的「已知明文」用的是模块级 API，而不是入参；
# 若这里换别的域名，key 推导就与 Python 不同源，除第 1 条外全部解坏
# （已知明文攻击会把所选 key 完全抵消，所以第 1 条永远「看起来对」，
#   这正是最容易被误判成"通过"的陷阱）。
sys.path.insert(0, os.path.join(HERE, "..", ".."))
from shared.core import API, decrypt_page, parse_epg  # noqa: E402

TID = "tv"
CID = "1"
OLD_TOKEN = "OLDTOK1234567"
NEW_TOKEN = "NEWTOK7654321"
# salt 前缀：解密时会被 replace(key_seed, "") 去掉，故不能在 URL 中自然出现
KEY_SEED = "Zx9Qm2Lp7Rt4Wv1B"


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def eplay_url(api: str, token: str, tid: str, cid: str) -> str:
    return "%s/eplay2.php?token=%s&tid=%s&id=%s&p=0&type=.flv" % (
        api, token, tid, cid)


def make_key(n: int) -> bytes:
    """确定性伪随机 key，刻意含高位字节（>=0x80）以触发非 UTF-8 中间产物。"""
    out = bytearray()
    x = 0x5A
    for i in range(n):
        x = (x * 37 + 11 + i) & 0xFF
        # 每 3 个字节强制一个高位字节
        out.append(x | 0x80 if i % 3 == 0 else x)
    return bytes(out)


def build_options(final_urls: list, key: bytes) -> list:
    """按解密逆过程生成每条 option 的 value。"""
    opts = []
    for i, u in enumerate(final_urls):
        # 还原 raw：先把新 token 换回旧 token
        raw = u.replace("token=" + NEW_TOKEN, "token=" + OLD_TOKEN)
        # 第 1 条不带 salt（见文件头说明 1），其余带
        if i > 0:
            raw = KEY_SEED + raw
        o2 = b64e(raw.encode("latin1"))
        # st = o2 XOR key（逐字节，key 循环）
        st = bytes(ord(c) ^ key[j % len(key)] for j, c in enumerate(o2))
        opts.append(b64e(st)[::-1])
    return opts


def build_page(opts: list) -> str:
    # assign 块：var x = ""; 后赋值，且不含 function
    # 注意 concat_val 会把 "X".split("").reverse().join("") 求成 X 的反序，
    # 所以要得到目标值 V，字面量要写 V 的反序。
    assign = (
        "<script>\n"
        'var keySeed = "";\n'
        'keySeed = "%s".split("").reverse().join("");\n'
        'var tk = "";\n'
        'tk = "%s".split("").reverse().join("");\n'
        'var tkNew = "";\n'
        'tkNew = "%s".split("").reverse().join("");\n'
        "</script>\n"
    ) % (KEY_SEED[::-1], OLD_TOKEN[::-1], NEW_TOKEN[::-1])

    # ent 块：含 .reverse() 的 function；提供 (x, keySeed) 与两处 "token="+var
    ent = (
        "<script>\n"
        "function deco(s, keySeed) {\n"
        '  var k = keySeed.split("").reverse().join("");\n'
        '  return s.replace("token="+tk, "token="+tkNew);\n'
        "}\n"
        "</script>\n"
    )

    option_html = "\n".join(
        '<option value="%s">线路%d</option>' % (o, i + 1)
        for i, o in enumerate(opts)
    )

    epg = (
        '<ul id="myEpg">\n'
        "  <li><span> 20:00 新闻联播</span></li>\n"
        "  <li><span> 21:30 焦点访谈</span></li>\n"
        "</ul>\n"
    )
    # 故意留一个非 myEpg 的 li，验证 parse_epg 的段落切分
    extra = "<ul><li><span> 09:00 不应被解析</span></li></ul>\n"

    return (
        "<html><head>\n%s%s</head><body>\n"
        "<select>\n%s\n</select>\n%s%s</body></html>\n"
    ) % (assign, ent, option_html, extra, epg)


def main() -> None:
    # 第 1 条线路的明文固定为 eplay2 URL（已知明文攻击的前提）
    first_plain = eplay_url(API, OLD_TOKEN, TID, CID)
    first_final = eplay_url(API, NEW_TOKEN, TID, CID)

    final_urls = [
        first_final,
        # 其余线路可以任意，带 salt 前缀
        "http://cdn2.example.com/live/cctv1.flv?token=%s&type=.flv" % NEW_TOKEN,
        "http://cdn3.example.com/live/cctv1.m3u8?token=%s&a=1" % NEW_TOKEN,
        # 少见的：不带 type=，验证解密侧补 &type=.flv
        "http://cdn4.example.com/live/cctv1.m3u8?token=%s" % NEW_TOKEN,
    ]

    # key 长度必须等于 len(b64e(url0))
    n = len(b64e(first_plain.encode("latin1")))
    key = make_key(n)

    opts = build_options(final_urls, key)
    page = build_page(opts)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "page_xor.html"), "w", encoding="utf-8") as f:
        f.write(page)

    # 用 Python 原版跑，落期望值
    got_urls, msg = decrypt_page(page, TID, CID)
    epg = parse_epg(page)
    expect = {
        "note": "由 tv_rs/tools/gen_xor_fixture.py 生成，勿手改",
        "api": API,
        "tid": TID,
        "cid": CID,
        "msg": msg,
        "urls": got_urls,
        "epg": epg,
    }
    with open(os.path.join(OUT_DIR, "page_xor.expect.json"), "w",
              encoding="utf-8") as f:
        json.dump(expect, f, ensure_ascii=False, indent=2)

    print("page bytes :", len(page))
    print("key len    :", n)
    print("msg        :", msg)
    print("线路 输入/解出 : %d / %d" % (len(final_urls), len(got_urls)))
    for i, u in enumerate(got_urls):
        print("  [%d] %s" % (i, u))
    print("EPG 条数   :", len(epg), "->", [e["time"] + " " + e["name"] for e in epg])

    # 自检：解出的 URL 是否覆盖全部输入线路。
    # 注意 decrypt_page 会对不含 "type=" 的线路**自动补** &type=.flv，
    # 所以对比时要容忍这个尾部追加（这是 Python 的既定行为，不是偏差）。
    def _norm(u: str) -> str:
        return u.replace("&type=.flv", "")

    if [_norm(u) for u in got_urls] != [_norm(u) for u in final_urls]:
        print("!! 解出结果与输入不一致")
        for a, b in zip(got_urls, final_urls):
            if _norm(a) != _norm(b):
                print("   got :", a)
                print("   want:", b)
    else:
        print("OK 解出 %d/%d 条线路（顺序一致，type= 自动补齐符合预期）"
              % (len(got_urls), len(final_urls)))

    # 自检 2：XOR 中间产物含非 UTF-8 字节的线路数（决定向量是否有价值）
    bad = 0
    for o in opts:
        st = base64.b64decode(o[::-1] + "=" * ((-len(o)) % 4))
        try:
            st.decode("utf-8")
        except UnicodeDecodeError:
            bad += 1
    print("中间产物含非 UTF-8 字节的线路数:", bad, "/", len(opts))


if __name__ == "__main__":
    main()