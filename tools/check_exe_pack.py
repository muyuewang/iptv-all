# -*- coding: utf-8 -*-
"""校验打包后的 exe：数据文件是否齐全 + 冻结后能否 import 到关键模块。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "desktop"))

EXE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "desktop", "dist", "电视直播.exe")

from PyInstaller.archive.readers import CArchiveReader  # noqa: E402

FAIL = []


def check(cond, msg):
    print(("  [OK] " if cond else "  [!!] ") + msg)
    if not cond:
        FAIL.append(msg)


print("exe:", EXE)
print("size: %.1f MB" % (os.path.getsize(EXE) / 1048576.0))

r = CArchiveReader(EXE)
keys = [k.replace("\\", "/") for k in r.toc.keys()]
print("归档条目数:", len(keys))

print("== 1) 数据文件 ==")
for w in ("shared/core.py", "shared/wvt.py", "shared/wvt_builtin.json",
          "shared/web/wvt_inject.js", "shared/web/tv_core.js", "shared/web/wvt.js",
          "assets/logos.js", "mpv/mpv.exe",
          # v2.4：WebView2 COM 直调封装（embed.py 运行时从 sys._MEIPASS 根找这个 DLL）
          "WebView2Loader.dll"):
    check(any(k.endswith(w) for k in keys), w)

print("== 1b) shared/wvt.py 是不是**带 v2.9.2 冷启动修复**的那一份 ==")
# wvt.py 以 datas（源码形态）进包，不走 PYZ，所以直接读文本查特征。
# v2.9.2 修：桌面端官网源曾是「联网 -> 内置快照」，冷启动要等三段下载跑完
# （实测 26s，网络差时更久）才上屏 —— 用户看到「打开后官网源没有节目列表」。
# 现在必须是「peek() 先上屏 -> 后台静默刷新」。
try:
    _wk = None
    for k in r.toc.keys():
        if k.replace("\\", "/") == "shared/wvt.py":
            _wk = k
            break
    if _wk is None:
        check(False, "归档里找不到 shared/wvt.py")
    else:
        _blob = r.extract(_wk)
        if isinstance(_blob, bytes):
            _blob = _blob.decode("utf-8", "ignore")
        for token in ("def peek", "def builtin_snapshot", "_MEIPASS",
                      "return builtin_snapshot().get(\"channels\", [])"):
            check(token in (_blob or ""), "wvt.py 含 %s" % token)
except Exception as e:
    check(False, "读 shared/wvt.py 失败：%r" % (e,))

print("== 2) PYZ 里的关键模块 ==")
pyz_key = None
for k in r.toc.keys():
    if k.replace("\\", "/").endswith(".pyz"):
        pyz_key = k
        break
if pyz_key is None:
    check(False, "未找到 PYZ")
else:
    from PyInstaller.archive.readers import ZlibArchiveReader
    import tempfile
    try:
        blob = r.extract(pyz_key)
        if isinstance(blob, str):
            blob = blob.encode("utf-8", "ignore")
        tmp = os.path.join(tempfile.gettempdir(), "_pypyz.pyz")
        with open(tmp, "wb") as f:
            f.write(blob)
        z = ZlibArchiveReader(tmp)
        names = set(z.toc.keys())
        # native_app 是入口脚本，PyInstaller 把它单独放进 PKG 的 script 条目，
        # 不进 PYZ —— 不用在这里找它（能找到才奇怪）。
        for m in ("embed", "cdp", "ctypes", "ctypes.wintypes",
                  "tkinter", "tkinter.ttk", "PIL.Image"):
            check(m in names, m)

        print("== 3) exe 里的 embed.py 是不是**带修复**的那一份 ==")
        # ★ 光看「embed 在不在」不够 —— 上一版 exe 里也有 embed。
        #   必须确认这次的修复真的进去了，否则用户拿到手的还是老行为。
        #   PYZ 里存的是 marshal 过的 code 对象，不是源码，所以得递归掏字符串常量
        #   （函数名在 co_names、文档字符串/字面量在 co_consts）。
        import marshal

        def collect(code, strs, names, depth=0):
            if depth > 6:
                return
            for k in code.co_consts:
                if isinstance(k, str):
                    strs.append(k)
                elif isinstance(k, tuple):
                    # 关键字调用的参数名会打包成 tuple 常量（如 last_resort=True）
                    strs.extend(s for s in k if isinstance(s, str))
                elif hasattr(k, "co_consts"):
                    collect(k, strs, names, depth + 1)
            names.extend(code.co_names)
            names.extend(code.co_varnames)  # 参数名/局部变量名（如 def f(last_resort=False)）

        try:
            cobj = z.extract("embed")
            if isinstance(cobj, bytes):
                cobj = marshal.loads(cobj[8:])
            strs, names = [], []
            collect(cobj, strs, names)
            hay = "\n".join(strs) + "\n" + "\n".join(names)
            # v2.4：embed.py 已整体重写为 WebView2 COM 直调（不再外拉 Edge 进程），
            # 旧版 Edge SetParent 时代的特征（--app= / --kiosk / client_insets）全部作废。
            for token in (
                    "WebView2Loader.dll",
                    "CreateCoreWebView2EnvironmentWithOptions",
                    "ICoreWebView2Settings2",
                    "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                    "WVT_EMBED_ARGS",            # EXTRA_ARGS 可被环境变量整体覆盖
                    "register_extra",            # 文档级附加注入
                    "_pending_extra",            # core 未就绪时挂起的附加脚本队列
                    "_eval_sync", "_eval_fire",  # 同步/异步 JS 执行
                    "ComObject", "_vtbl_call",   # COM 直调基础设施
                    "_find_loader"):             # DLL 搜索路径（含 sys._MEIPASS）
                check(token in hay, "embed.py 含 %s" % token)
            # 负向检查：确保 exe 里不再是「外拉 Edge + SetParent 内嵌」的旧实现。
            # co_consts 里恰好等于这些启动参数字面量的才算命中（文档字符串提一句不算）。
            stale_args = [s for s in strs if s.strip() in ("--app=", "--kiosk",
                                                           "--window-position=-32000,-32000")]
            check(not stale_args,
                  "embed.py 已**不带** Edge 命令行启动参数（v2.4 WebView2 COM 重写）")
        except Exception as e:
            check(False, "取 PYZ 里的 embed 失败：%r" % (e,))

        print("== 4) 入口脚本 native_app 是不是**带修复**的那一份 ==")
        # ★ native_app 是 PKG 里的 script 条目（不在 PYZ），单独掏。
        #   v2.3.4 教训：下拉控件的新代码进了包、但 _pop_w 修复没进去，
        #   用户跑出来「第一次点下拉弹竖排字」，之前没有任何检查能发现。
        try:
            script_key = None
            for k in r.toc.keys():
                if k.replace("\\", "/").endswith("native_app"):
                    script_key = k
                    break
            if script_key is None:
                check(False, "PKG 里找不到 native_app 脚本条目")
            else:
                raw = r.extract(script_key)
                try:
                    cobj = marshal.loads(raw)
                except Exception:
                    cobj = marshal.loads(raw[16:])   # PyInstaller 6 script 条目前有 cookie
                strs, names = [], []
                collect(cobj, strs, names)
                hay = "\n".join(strs) + "\n" + "\n".join(names)
                for token in ("DropSelect", "_pop_w", "_pop_h", "MIN_W",
                              "filter_slot", "_outside_click",
                              "group_frame", "_render_cat_bar_off",
                              # v2.4：_ui 线程安全队列 + 主线程排水（修后台线程跨线程
                              # root.after 的段错误）；网页线路全败 → CDP 嗅探 → mpv 兜底
                              "_ui_q", "_drain_ui", "_closing",
                              "_sniff_fallback", "last_resort", "_header_fields",
                              # v2.9.2：官网源冷启动改成「内置快照/缓存先上屏 →
                              # 后台静默刷新」（与 JS 侧 WVTCore.load 的顺序对齐），
                              # 状态栏标「正在联网更新…」；silent/pending 是新增参数名
                              "_set_wvt_status", "silent", "pending"):
                    check(token in hay, "native_app 含 %s" % token)
        except Exception as e:
            check(False, "取 native_app 脚本失败：%r" % (e,))
    except Exception as e:
        check(False, "读 PYZ 失败：%r" % (e,))

print()
if FAIL:
    print("失败 %d 项：" % len(FAIL))
    for f in FAIL:
        print("  - " + f)
    sys.exit(2)
print("全部通过 ✅")
