# -*- coding: utf-8 -*-
"""为「两版解密逐字节一致」的交叉验证准备样本。

抓三份真实 .dat（走镜像回退），再用 Python 侧解密写成期望明文；
随后 `node tools/check_wvt_aes.js` 会用 JS 侧解同一份密文做对比。

产出（默认写到 _aes_test/）：
    full.dat / webpage_adapter.dat / server_config.dat
    以及各自的 .expect.json

用法：python tools/gen_wvt_aes_fixture.py [输出目录]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "shared"))

import wvt  # noqa: E402

FILES = ("data/4/full.dat", "data/4/webpage_adapter.dat", "data/4/server_config.dat")


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "_aes_test")
    os.makedirs(out, exist_ok=True)
    print("样本目录：%s" % out)
    ok = 0
    for rel in FILES:
        name = os.path.basename(rel)
        try:
            data, url = wvt.download_dat(rel)
        except Exception as e:            # noqa: BLE001
            print("  [!!] %-22s 下载失败：%s" % (name, e))
            continue
        try:
            obj = wvt.decrypt_dat_json(data)
        except Exception as e:            # noqa: BLE001
            print("  [!!] %-22s 解密失败：%s" % (name, e))
            continue
        with open(os.path.join(out, name), "wb") as f:
            f.write(data)
        with open(os.path.join(out, name + ".expect.json"), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        n = len(obj) if isinstance(obj, list) else len(obj.keys())
        print("  [OK] %-22s %7d bytes  %4d 条  <- %s" % (name, len(data), n, url[:60]))
        ok += 1

    print()
    if ok == len(FILES):
        print("样本就绪。接着跑：  node tools/check_wvt_aes.js")
        return 0
    print("有 %d 个样本未就绪（网络问题可重试）" % (len(FILES) - ok))
    return 1


if __name__ == "__main__":
    sys.exit(main())
