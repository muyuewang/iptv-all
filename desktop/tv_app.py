# -*- coding: utf-8 -*-
"""
纯净电视直播 · 桌面版（Windows / Linux 通用）
================================================================
一个文件，三平台通用：Windows 打包成 exe，Linux 打包成 AppImage / 直接跑。

原理
----
本进程在 127.0.0.1 起本地服务，负责浏览器做不到的两件事：
  1) 用「App 伪装 UA」抓频道列表页 / 播放页（浏览器被禁止伪造 UA）
  2) 在本地完成线路解密，前端只拿现成的播放地址
然后用系统浏览器以「应用窗口」模式打开，观感等同原生应用。
页面关闭后（心跳停止）进程自动退出，不留后台残留。

业务逻辑全部在 shared/core.py，本文件只管：HTTP 服务 + 平台适配 + 启动窗口。
"""
import os
import sys
import time
import json
import socket
import shutil
import threading
import subprocess
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_core():
    """
    载入共享业务核心 shared/core.py（源码运行与打包运行行为一致）。

    打包注意：core.py 是「数据文件」而非被静态分析的模块，因此它 import 的
    stdlib 子模块（urllib.request 等）不会被 PyInstaller 自动收集。构建脚本里
    必须显式 --hidden-import 这些模块，见 build_windows.bat / build_linux.sh。
    """
    import importlib.util as _ilu
    cands = [
        os.path.join(getattr(sys, "_MEIPASS", _HERE), "shared", "core.py"),
        os.path.join(_HERE, "shared", "core.py"),
        os.path.join(_HERE, "..", "shared", "core.py"),
    ]
    for p in cands:
        if os.path.isfile(p):
            spec = _ilu.spec_from_file_location("cleantv_core", p)
            mod = _ilu.module_from_spec(spec)
            sys.modules["cleantv_core"] = mod
            spec.loader.exec_module(mod)
            return mod
    raise ImportError("找不到 shared/core.py，请确认 shared 目录与程序在一起")


core = _load_core()  # 共享业务核心

VERSION = "1.1"
CATS = core.CATS
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# ------------------------------------------------------------------ 路径
def res_path(*rel):
    """打包后资源解压目录 / 源码目录。"""
    base = getattr(sys, "_MEIPASS", _HERE)
    for b in (base, _HERE, os.path.join(_HERE, "..")):
        p = os.path.join(b, *rel)
        if os.path.exists(p):
            return p
    return os.path.join(base, *rel)


def data_dir():
    """绿色版：配置写在可执行文件旁边；源码运行写在脚本目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return _HERE


FAV_FILE = os.path.join(data_dir(), "fav.json")
LOG_PATH = None
_mem_cache = {}
_lock = threading.Lock()
_last_ping = [time.time()]
_first_seen = [False]


def log(*a):
    if not LOG_PATH:
        return
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), " ".join(str(x) for x in a)))
    except Exception:
        pass


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CleanTV/" + VERSION

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _static(self, name, ctype):
        p = res_path("assets", name)
        try:
            with open(p, "rb") as f:
                return self._send(200, f.read(), ctype)
        except Exception as e:
            return self._send(404, "missing " + name, "text/plain; charset=utf-8")

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = {}
        for kv in query.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                params[k] = urllib.parse.unquote(v)
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if path == "/mpegts.js":
                return self._static("mpegts.js", "application/javascript; charset=utf-8")
            if path == "/logos.js":
                return self._static("logos.js", "application/javascript; charset=utf-8")
            if path == "/api/ping":
                _last_ping[0] = time.time()
                _first_seen[0] = True
                return self._json({"ok": True})
            if path == "/api/meta":
                return self._json({"cats": [{"tid": t, "name": n} for t, n in CATS],
                                   "version": VERSION, "os": sys.platform,
                                   "hw": detect_hw_backends()})
            if path == "/api/list":
                tid = params.get("tid", "tv")
                with _lock:
                    chans = core.fetch_channels(tid, cache=_mem_cache)
                return self._json({"ok": True, "channels": chans})
            if path == "/api/play":
                tid = params.get("tid", "tv")
                cid = params.get("id", "")
                urls, epg, msg = core.fetch_play(tid, cid)
                log("play", tid, cid, msg, len(urls), "lines")
                return self._json({"ok": bool(urls), "urls": urls, "epg": epg, "msg": msg})
            if path == "/api/fav":
                return self._json({"ok": True, "fav": core.load_fav(FAV_FILE)})
            return self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception as e:
            log("handler error", repr(e))
            return self._json({"ok": False, "msg": "服务异常：" + str(e)}, 200)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b"{}"
            data = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            data = {}
        if path == "/api/fav":
            return self._json({"ok": core.save_fav(FAV_FILE, data.get("fav", []))})
        return self._send(404, "not found", "text/plain; charset=utf-8")


# ------------------------------------------------------------------ 平台适配
def free_port(start=18899, tries=40):
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def detect_hw_backends():
    """探测系统是否有可用的原生硬解播放器（供前端显示升级提示）。

    桌面端当前用浏览器 + mpegts.js 软解，CPU 强时够用；但若装了 mpv/ffplay，
    可走「原生窗口嵌入」实现真正的 GPU 硬解。这里只探测，不自动切换。
    """
    found = {}
    for name, hint in (("mpv", "libmpv / mpv 播放器"),
                       ("ffplay", "ffmpeg 附带的 ffplay"),
                       ("vlc", "VLC 播放器")):
        p = shutil.which(name)
        if p:
            found[name] = p
    return found


def find_browser():
    """返回支持 --app 应用窗口的浏览器路径（Chromium 系）。"""
    if IS_WIN:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        lad = os.environ.get("LocalAppData", "")
        cands = [
            os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(lad, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(lad, r"Microsoft\Edge\Application\msedge.exe"),
        ]
    elif IS_MAC:
        cands = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:  # Linux
        cands = []
        for name in ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser", "microsoft-edge", "microsoft-edge-stable",
                     "brave-browser", "vivaldi", "opera"):
            p = shutil.which(name)
            if p:
                cands.append(p)
        cands += ["/usr/bin/google-chrome", "/usr/bin/chromium",
                  "/usr/bin/chromium-browser", "/snap/bin/chromium",
                  "/opt/google/chrome/chrome"]
    for c in cands:
        if c and os.path.isfile(c) and (IS_WIN or os.access(c, os.X_OK)):
            return c
    return None


def _write_browser_prefs(udd):
    """预写浏览器首选项，彻底关掉「同步浏览数据 / 自动登录」引导弹窗。

    仅靠命令行开关不足以抑制 Edge 因独立 profile 触发的首次运行引导，
    因此这里在启动前直接写入一份 Preferences。
    """
    try:
        prof = os.path.join(udd, "Default")
        os.makedirs(prof, exist_ok=True)
        pf = os.path.join(prof, "Preferences")
        prefs = {
            "profile": {"default_content_setting_values": {}},
            # 关闭账号同步 / 登录引导
            "sync": {"requested": False, "has_setup_completed": True},
            "signin": {"allowed": False, "allowed_on_next_startup": False},
            "browser": {"has_seen_welcome_page": True,
                        "check_default_browser": False,
                        "show_app_launcher_promo": False},
            "edge": {"first_run_completed": True,
                     "show_first_run_experience": False},
            # 关闭「正在同步」提示与账号栏
            "profile_avatar_icon_index": 26,
            "toolbar": {"show_avatar": False},
            "credentials_enable_service": False,
            "credentials_enable_autosignin": False,
        }
        # 若已存在则合并，避免覆盖用户其它设置
        if os.path.isfile(pf):
            try:
                with open(pf, "r", encoding="utf-8") as f:
                    old = json.load(f)
                if isinstance(old, dict):
                    for k, v in prefs.items():
                        if isinstance(v, dict) and isinstance(old.get(k), dict):
                            old[k].update(v)
                        else:
                            old[k] = v
                    prefs = old
            except Exception:
                pass
        with open(pf, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
        return True
    except Exception as e:
        log("write browser prefs fail", e)
        return False


def open_window(url):
    """用系统浏览器以独立应用窗口打开；失败则退回默认浏览器打开标签页。"""
    br = find_browser()
    if br:
        args = [br, "--app=" + url, "--window-size=1360,880",
                "--no-first-run", "--no-default-browser-check",
                # 关闭同步 / 登录 / 首次运行引导（配合 _write_browser_prefs）
                "--disable-sync", "--disable-features=SigninInterception",
                "--no-service-autorun", "--password-store=basic",
                "--disable-breakpad", "--disable-component-update",
                "--noerrdialogs", "--disable-infobars",
                "--disable-session-crashed-bubble"]
        if not IS_MAC:                      # Linux 下 Chrome 需要显式指定
            udd = os.path.join(data_dir(), ".browser")
            _write_browser_prefs(udd)
            args.append("--user-data-dir=" + udd)
        try:
            subprocess.Popen(args, close_fds=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "app"
        except Exception as e:
            log("launch browser fail", e)
    try:
        webbrowser.open(url)
        return "tab"
    except Exception as e:
        log("webbrowser fail", e)
        return "none"


def watchdog():
    """窗口关闭后自动收工：收到过页面心跳则 25 秒无心跳退出；
    始终没人打开页面则给 120 秒宽限，避免误退。"""
    while True:
        time.sleep(4)
        idle = time.time() - _last_ping[0]
        if _first_seen[0]:
            if idle > 25:
                log("心跳停止，退出")
                os._exit(0)
        elif idle > 120:
            log("长时间未打开页面，退出")
            os._exit(0)


# ------------------------------------------------------------------ 入口
def main():
    global LOG_PATH
    argv = sys.argv[1:]
    LOG_PATH = os.path.join(data_dir(), "tvlive.log") if "--log" in argv else None

    port = free_port()
    url = "http://127.0.0.1:%d/" % port
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    noopen = "--noopen" in argv or "--server" in argv
    if not noopen:
        threading.Thread(target=watchdog, daemon=True).start()

    log("server on", url)
    print("LISTEN %s" % url, flush=True)

    if noopen:
        print("仅服务模式：浏览器访问 %s ；Ctrl+C 退出" % url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    time.sleep(0.4)
    open_window(url)
    # 主线程驻留，等心跳超时由 watchdog 结束进程
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
