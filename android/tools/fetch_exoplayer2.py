#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载并解包老版 ExoPlayer 2.19.1（com.google.android.exoplayer2）依赖，
供 Android 4.4（API 16+）设备的原生硬解后端使用。

为什么需要它：
  media3 1.4.x 要求 minSdk 21（Android 5.0），Android 4.4 的 TV 盒子用不了。
  老版 ExoPlayer 2.19.1 支持到 API 16（Android 4.1），同样走 MediaCodec 硬解，
  是 4.4 设备唯一能用的原生硬解库。包名是 com.google.android.exoplayer2，
  与 media3 的 androidx.media3.* 不冲突，可共存双轨。

产物：
  tools/exoplayer2/aar/<name>.aar        原始 aar
  tools/exoplayer2/libs/<name>.jar       classes.jar（供 javac/d8 用）
  tools/exoplayer2/REQUIRED_JARS.txt     清单

用法： python fetch_exoplayer2.py
"""
import os, sys, io, zipfile, urllib.request, shutil

NO_PROXY = True
if NO_PROXY:
    os.environ['no_proxy'] = '*'
    for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        os.environ.pop(k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'exoplayer2')
AAR_DIR = os.path.join(OUT, 'aar')
LIB_DIR = os.path.join(OUT, 'libs')

REPOS = [
    "https://dl.google.com/dl/android/maven2",
    "https://maven.aliyun.com/repository/google",
    "https://repo1.maven.org/maven2",
    "https://maven.aliyun.com/repository/central",
]

V = "2.19.1"
# (group_path, artifact, version, 是否 aar)
DEPS = [
    # ---- exoplayer2 模块（FLV 解封装在 extractor 里）----
    ("com/google/android/exoplayer", "exoplayer-core",        V, True),
    ("com/google/android/exoplayer", "exoplayer-hls",         V, True),
    ("com/google/android/exoplayer", "exoplayer-common",      V, True),
    ("com/google/android/exoplayer", "exoplayer-container",   V, True),
    ("com/google/android/exoplayer", "exoplayer-datasource",  V, True),
    ("com/google/android/exoplayer", "exoplayer-decoder",     V, True),
    ("com/google/android/exoplayer", "exoplayer-extractor",   V, True),
    ("com/google/android/exoplayer", "exoplayer-database",    V, True),
    # ---- androidx 传递依赖 ----
    ("androidx/annotation",          "annotation",            "1.3.0", False),  # jar
    ("androidx/core",                "core",                  "1.8.0", True),
    # multidex：minSdk<21 时方法数超 65K 必须（legacy multidex）
    ("androidx/multidex",            "multidex",              "2.0.1", True),
    # ---- guava（老 ExoPlayer 内部用 ImmutableList 等）----
    ("com/google/guava",             "guava",                 "31.1-android", False),
    ("com/google/guava",             "failureaccess",         "1.0.1", False),
    ("com/google/guava",             "listenablefuture",      "9999.0-empty-to-avoid-conflict-with-guava", False),
]


def fetch(path_rel):
    last = None
    for repo in REPOS:
        url = repo + "/" + path_rel
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fetch/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
            print("    OK  %s  (%d KB)  <- %s" % (os.path.basename(path_rel), len(data)//1024, repo.split('/')[2]))
            return data
        except Exception as e:
            last = e
            continue
    print("    FAIL %s  (%s)" % (path_rel, last))
    return None


def main():
    os.makedirs(AAR_DIR, exist_ok=True)
    os.makedirs(LIB_DIR, exist_ok=True)
    jars = []
    for gp, art, ver, is_aar in DEPS:
        base = "%s/%s/%s" % (gp, art, ver)
        fname = "%s-%s.%s" % (art, ver, "aar" if is_aar else "jar")
        print("== %s ==" % art)
        cache = os.path.join(AAR_DIR, fname)
        if os.path.isfile(cache) and os.path.getsize(cache) > 0:
            data = open(cache, 'rb').read()
            print("    cached (%d KB)" % (len(data)//1024))
        else:
            data = fetch(base + "/" + fname)
            if data is None:
                print("    !! 致命：依赖 %s 下载失败，终止" % art)
                sys.exit(1)
            open(cache, 'wb').write(data)

        if is_aar:
            # aar：取 classes.jar 复制进 libs
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
            cj = None
            tmp = os.path.join(AAR_DIR, art + ".classes.jar")
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "classes.jar" in z.namelist():
                    cj = z.read("classes.jar")
            if cj is None:
                print("      !! aar 内无 classes.jar: %s" % names[:5])
                sys.exit(1)
            dst = os.path.join(LIB_DIR, art + ".jar")
            open(dst, 'wb').write(cj)
            jars.append(dst)
            print("      -> classes.jar (%d KB)" % (len(cj)//1024))
        else:
            dst = os.path.join(LIB_DIR, art + ".jar")
            shutil.copyfile(cache, dst)
            jars.append(dst)
            print("      -> jar (%d KB)" % (os.path.getsize(dst)//1024))

    man = os.path.join(OUT, "REQUIRED_JARS.txt")
    with open(man, "w", encoding="utf-8") as f:
        for j in jars:
            f.write(os.path.basename(j) + "\n")
    print("\n== 完成，共 %d 个 jar ==" % len(jars))
    for j in jars:
        print("  %-30s %8d KB" % (os.path.basename(j), os.path.getsize(j)//1024))


if __name__ == "__main__":
    main()
