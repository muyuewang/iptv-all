#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载并解包 ExoPlayer(media3) 依赖 —— 供无 gradle 的 aapt2+d8 构建链使用。

产物：
  tools/media3/aar/<name>.aar          原始 aar
  tools/media3/classes/<name>/         解包后的 classes.jar / res / AndroidManifest.xml
  tools/media3/libs/                   汇总的全部 classes.jar（供 javac/d8 用）
  tools/media3/REQUIRED_JARS.txt       javac classpath 清单

用法： python fetch_media3.py
"""
import os, sys, io, json, zipfile, urllib.request, shutil

NO_PROXY = True
if NO_PROXY:
    os.environ['no_proxy'] = '*'
    for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        os.environ.pop(k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'media3')
AAR_DIR = os.path.join(OUT, 'aar')
CLS_DIR = os.path.join(OUT, 'classes')
LIB_DIR = os.path.join(OUT, 'libs')

REPOS = [
    "https://dl.google.com/dl/android/maven2",
    "https://maven.aliyun.com/repository/google",
    "https://repo1.maven.org/maven2",
    "https://maven.aliyun.com/repository/central",
]

# ExoPlayer v1.4.1 —— 兼容 minSdk21（media3 1.4.x 要求 minSdk 21）
V = "1.4.1"
# (group_path, artifact, version, 是否 aar)
DEPS = [
    # ---- media3 本体 ----
    ("androidx/media3", "media3-common",        V, True),
    ("androidx/media3", "media3-container",     V, True),
    ("androidx/media3", "media3-datasource",    V, True),
    ("androidx/media3", "media3-decoder",       V, True),
    ("androidx/media3", "media3-extractor",     V, True),   # ★ FLV 解封装在这里
    ("androidx/media3", "media3-exoplayer",     V, True),   # ★ 核心播放引擎
    ("androidx/media3", "media3-exoplayer-hls", V, True),   # 顺带支持 HLS（回看可能用）
    ("androidx/media3", "media3-ui",            V, True),   # ★ PlayerView
    # ---- media3 的 androidx 传递依赖（手工列全，避免漏包）----
    ("androidx/annotation",     "annotation",        "1.8.1", False),   # annotation 只有 jar
    ("androidx/core",           "core",              "1.12.0", True),
    ("androidx/collection",     "collection",        "1.4.0", True),
    ("androidx/lifecycle",      "lifecycle-common",  "2.6.2", False),
    ("androidx/lifecycle",      "lifecycle-runtime", "2.6.2", True),
    ("androidx/versionedparcelable", "versionedparcelable", "1.1.1", True),
    ("androidx/loader",         "loader",            "1.1.0", True),
    ("androidx/interpolator",   "interpolator",      "1.0.0", True),
    # ---- guava（media3-exoplayer 内部用 com.google.common.collect.ImmutableList，
    #      media3 用 android flavor，跟它的 -android 变体对齐）----
    ("com/google/guava",        "guava",             "33.3.1-android", False),
    # ---- guava 的运行期传递依赖：failureaccess + listenablefuture ----
    ("com/google/guava",        "failureaccess",     "1.0.2", False),
    ("com/google/guava",        "listenablefuture",  "9999.0-empty-to-avoid-conflict-with-guava", False),
]


def fetch(path_rel):
    """依次尝试各仓库下载，返回 bytes 或 None。"""
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


def unzip_into(data, dest):
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(dest)
    return os.listdir(dest)


def main():
    for d in (AAR_DIR, CLS_DIR, LIB_DIR):
        os.makedirs(d, exist_ok=True)

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
                continue
            open(cache, 'wb').write(data)

        dest = os.path.join(CLS_DIR, art)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        names = unzip_into(data, dest)

        if is_aar:
            cj = os.path.join(dest, "classes.jar")
            if os.path.isfile(cj):
                dst = os.path.join(LIB_DIR, art + ".jar")
                shutil.copyfile(cj, dst)
                jars.append(dst)
                print("      -> classes.jar (%d KB)" % (os.path.getsize(cj)//1024))
            else:
                print("      !! aar 内无 classes.jar: %s" % names)
        else:
            dst = os.path.join(LIB_DIR, art + ".jar")
            # 纯 jar 依赖：解包目录里可能有多余层级，直接找同名文件
            src = os.path.join(dest, fname)
            if not os.path.isfile(src):
                # 有些 jar 解包后名字不同，回退到刚下载的原始文件
                src = cache
            shutil.copyfile(src, dst)
            jars.append(dst)
            print("      -> jar (%d KB)" % (os.path.getsize(dst)//1024))

    # 写清单
    man = os.path.join(OUT, "REQUIRED_JARS.txt")
    with open(man, "w", encoding="utf-8") as f:
        for j in jars:
            f.write(os.path.basename(j) + "\n")
    print("\n== 完成，共 %d 个 jar ==" % len(jars))
    print("清单: %s" % man)
    for j in jars:
        print("  %-34s %8d KB" % (os.path.basename(j), os.path.getsize(j)//1024))


if __name__ == "__main__":
    main()
