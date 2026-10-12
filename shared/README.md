# 共享业务核心 · 维护说明

本目录是**唯一真源**。三个端不各写一份业务逻辑，全部从这里取：

| 文件 | 谁在用 | 说明 |
|---|---|---|
| `core.py` | 桌面端（`desktop/native_app.py` 原生窗口 / `desktop/tv_app.py` 浏览器壳） | 纯标准库，无 GUI、无平台代码 —— **本机源** |
| `wvt.py` | 同上 | 同样纯标准库 —— **官网源**（WebViewTV）：AES-256 解密 / 镜像回退 / 缓存 / 频道表 |
| `wvt_builtin.json` | 桌面端随包发布 | 官网源内置快照（904 频道），断网兜底 |
| `web/tv_core.js` | 安卓 WebView 端（`android/app/assets/player.html`） | 与 `core.py` 逐行对齐 —— **本机源** |
| `web/wvt.js` | 同上 | 与 `wvt.py` 逐条对齐 —— **官网源** |
| `web/wvt_inject.js` | **安卓 + 桌面端两端共用** | 官方直播页注入脚本：找 `<video>` / 原地钉全屏 / 去浮层 / 起播与超时上报。安卓由 `build.sh` 拷到 `assets/`，桌面端由 `desktop/embed.py` 直接读 |

`android/app/assets/tv_core.js`、`android/app/assets/wvt.js`、`android/app/assets/wvt_inject.js`
都是 **build.sh 自动拷贝** 的副本，**不要直接改它们** —— 改了下次构建就被覆盖。

官网源的内置快照还有一份给安卓用的 JS 形式（`android/app/assets/wvt_channels.js`），
由 `tools/gen_wvt_js.py` 从 `wvt_builtin.json` 生成，**也不用手改**。

---

## 改了 Python 版？照着这张表同步 JS 版

| 功能 | `core.py` | `web/tv_core.js` |
|---|---|---|
| 伪装 UA | `APP_UA` / `UA` / `headers()` | `TVCore.APP_UA`（WebView 的 UA 由原生 `setUserAgentString` 设置） |
| 接口地址 | `API` | `TVCore.API` / `TVCore.getApi()` |
| **接口镜像回退** | `api_get()` + `API_MIRRORS` | `apiGet()` + `TVCore.API_MIRRORS` |
| 指定镜像 | `apply_settings()` 写入 `_pref_mirror` | `TVCore.setApi()` |
| 分类表 | `CATS` | `TVCore.CATS` |
| 频道列表解析 | `parse_channels()` | `parseChannels()` |
| 频道列表抓取+缓存 | `fetch_channels()` | `fetchChannels()` |
| **线路解密** | `decrypt_page()` | `decryptPage()` |
| EPG 解析 | `parse_epg()` | `parseEpg()` |
| 播放页抓取 | `fetch_play()` | `fetchPlay()` |
| 台标解析（JS→映射） | `parse_logos()` | （`logos.js` 直接就是 `window.LOGOS`） |
| 台标匹配 | `match_logo()` | `logoSrc()` |
| 收藏 | `load_fav()/save_fav()` | `favArr()/favSave()/isFav()/toggleFav()` |
| 设置读写 | `load_settings()/save_settings()` | （安卓端用 `localStorage`，字段同名） |
| 缓存 TTL | `CACHE_TTL` | `CACHE_TTL` |

> `API_MIRRORS` 三个域名（`api1` / `api` / `api2.2026016.xyz`）实测同源，
> `api_get()` / `apiGet()` 依次尝试，命中即把 `API` 切过去，**保证抓页与解密同域**。
> 两边的回退超时都做了**封顶**（主域名 8s、后续镜像 5s）——健康镜像实测 <1s，
> 不封顶的话一个挂死的主站就串行吃满 15s×N 个域名（「点开频道半天没反应」）。

> **UI / 播放层逻辑不在这份核心内**。全屏、横屏、手势、选路、缓冲策略属于「呈现层」，
> 各端按平台能力自行实现：
>
> | 能力 | 安卓（`player.html` + `MainActivity.java`） | 桌面（`native_app.py`） |
> |---|---|---|
> | 全屏 | `setFsMode()` 走原生 `NativeBridge.setLandscape()` 真转屏 | `_set_fullscreen()` 走 Tk 窗口全屏（收起顶栏/控制条/列表） |
> | 双击画面切全屏 | `bindTouchGestures()` | 同逻辑（鼠标双击 `video_host`，**只绑双击**，别同时绑单击） |
> | 数字键直选频道 | `numBuf` + 1.2s 缓冲 | 同逻辑（键盘数字键） |
> | 线路测速选优 | `probeLine()` / `pickBestLine()` | 桌面端不做测速，直接顺序切线 |
> | 缓冲策略 | `mpegtsCfg()` 按 `TIER` 分三档 | `BUFFER_TIERS` 映射到 mpv `--cache-secs` |
> | 子窗口跟随 resize | 不涉及（WebView 是 View 树的） | `_fit_children()` → `embed.cover_child_windows()`：mpv / Chromium 都是 **Win32 子窗口，不跟随父窗口 resize**，必须手动 `MoveWindow` 铺满 |
> | 内嵌窗口抢焦点后的快捷键 | 不涉及 | `_poll_hotkeys()` 用 `GetAsyncKeyState` 轮询 F11 / Esc（Tk 的 `<Key>` 绑定收不到） |

---

## 改了官网源 Python 版？照着这张表同步 JS 版

| 功能 | `wvt.py` | `web/wvt.js` |
|---|---|---|
| 密钥派生 | `_APP_CLASS` 派生的 `key` / `iv` | `KEY_STR` / `IV_STR`（`keyInfo()` 可自检） |
| **AES-256-CBC 解密** | `_expand_key()` / `aes_cbc_decrypt()` | `expandKey()` / `aesCbcDecrypt()` |
| gzip 解压 | 标准库 `gzip` | `DecompressionStream("gzip")`（浏览器原生，缺失则只用快照） |
| `.dat` → JSON | `decrypt_dat()` / `decrypt_dat_json()` | `decryptDatToJson()` / `decryptDatJson()` |
| 数据文件路径 | `DATA_FILES` | `DATA_FILES`（同名） |
| 镜像候选 | `_candidate_urls()` | `candidates()` |
| 多镜像下载 + 总预算 | `download_dat()`（`TOTAL_BUDGET`） | `download()`（`TOTAL_BUDGET`） |
| 本地缓存（24h） | `_read_cache()` / `_write_cache()` | `readCache()` / `writeCache()`（走 `localStorage`） |
| 频道表 | `load_channels()` | `WVTCore.load()` / `WVTCore.refresh()` |
| **不联网能拿到的数据** | `peek()`（缓存含过期 → 内置快照） | `WVTCore.load()` 里「`readCache()` → `snapshot()`」这一段 |
| 适配规则 | `load_adapters()` / `match_adapter()` | `WVTCore.load()` / `WVTCore.matchAdapter()` |
| 兜底适配规则 | —（桌面端不用） | `WVTCore.defaultAdapter()` |
| 直链判定 | `is_direct_stream()` | `WVTCore.isDirect()` |
| 内置快照 | `builtin_snapshot()` | `WVTCore.snapshot()`（读 `window.WVT_SNAPSHOT`） |

**两版的解密必须逐字节一致**，改完跑一次交叉验证（见下「自检」）。

> ★ **「立刻出数据」这件事 JS 版天生是对的，Python 版踩过坑（v2.9.2 修）**
>
> `WVTCore.load()` 的顺序是「本地缓存 → 内置快照（**立刻返回**）→ `opts.refresh` 时后台联网刷新」，
> 安卓端调的就是 `WVTCore.load({refresh:true})`，所以从来没空过。
>
> 桌面端 `native_app._load_wvt()` 一度写成「联网 → 内置快照」：冷启动（别人电脑第一次双击 exe、
> 无缓存、网络又不佳）要等 `load_config` + `load_channels` + `load_adapters` 三段全跑完才上屏 ——
> 实测本机 26s，网络差时每段各吃满 45s 预算（`TOTAL_BUDGET`），用户看到的就是
> **「打开后官网源没有节目列表」**。内置快照 0.01s 就有 904 频道，没有任何理由让它排在最后。
>
> 现在 Python 侧对齐：`wvt.peek()` 先出数据 → 后台 `load_channels` / `load_adapters` 静默刷新
> （频道表先落地，适配规则不拖着它）。**以后动这块，记住顺序永远是「先上屏、后刷新」。**

### 桌面端独有的三件事（JS 版不需要对齐）

| 能力 | 位置 | 说明 |
|---|---|---|
| **把浏览器窗口嵌进播放区** | `desktop/embed.py` | Win32 `SetParent`（**不要用 `--kiosk`**，它会抢全屏把窗口从宿主里摘出去）+ `client_insets/fit_child_to` 按客户区对齐，官网页面就在我们自己的窗口里播（v2.3.2 起稳定） |
| CDP 嗅探网页线路的真实流地址 | `desktop/cdp.py` | 真浏览器 + Network 监听，纯标准库自带最小 WebSocket（**退路**：机器上没有 Edge/Chrome 时用） |
| 直接交给 mpv（带 referer/UA/cookie） | `desktop/native_app.py` 的 `_start_mpv(headers=…)` | 走 `--http-header-fields` |

安卓端不需要嗅探也不需要内嵌 —— 它直接开一个覆盖层 WebView 加载官方页面，
注入 `wvt_inject.js` 让页面**自己播**（与 WebViewTV 同策略）。
`shared/web/wvt_inject.js` 就是两端共用的那一份，**改注入逻辑要同时想到两端**：
安卓是 WebView（`visualViewport` + `setUseWideViewPort`），桌面是同版本 Chromium 内核的
Edge/Chrome（`--app=` 窗口 + `SetParent` 内嵌，**注意别加 `--kiosk`**），尺寸基准都是「视觉视口」。

---

## 解密算法（本机源，改的时候务必保持两版一致）

### 台标匹配两版都要有
台标资源 `logos.js` 只有一份（`window.LOGOS`）。JS 端（安卓 / 浏览器壳）直接用它；
**Python 端（`native_app.py` 原生窗口）** 用 `parse_logos()` 把同一份文件解析成 dict，
再 `match_logo()` 匹配 —— 匹配规则与 JS 的 `logoSrc()` 逐条对齐（精确 → 去后缀别名 → 包含）。

### 为什么收藏 / 设置实现不同
- 桌面原生端：Python 读写 `fav.json` / `settings.json`（绿色便携，放 exe 同目录）
- 浏览器壳端：Python 读 `fav.json`，前端调 `/api/fav` 存取
- 安卓端：直接读写浏览器 `localStorage`

---

## 解密算法（改的时候务必保持两版一致）

```
输入：播放页 HTML、tid、频道 id
① option 值取反序         opts[i][::-1]
② base64 解码             → s_i
③ 用第 1 条线路反推 key     key = s_0 ⊕ utf8(已知明文URL_0)
④ 逐字节 XOR（key 循环）    o_i = s_i ⊕ key
⑤ base64 解码             → 明文
⑥ token 替换              token=旧 → token=新
⑦ 去掉盐前缀              明文里剥掉 key_seed
⑧ 补 &type=.flv（若无 type）
输出：整页全部线路地址
```

**为什么不用算盐**：盐含 deviceId/appVersion/ips/日期，动态且无规律。
但第 1 条线路的明文结构是**固定的**（由服务器端 eplay2 拼接规则决定），
用它和密文异或就能还原 key，从而解出整页 —— 这是整个方案能稳定工作的关键。

注意 ④ 用的是 key 的**循环**（`key[t % len(key)]`），因为部分线路明文比 key 长。

---

## 自检

改完本机源 Python 版，跑一下：
```bash
python3 shared/core.py
```
会打印 UA、频道数、第一条线路、线路数、EPG 条数。全都有值即正常。

改完 JS 版，安卓端可这样验：
```bash
cd android && bash build.sh && adb install -r 纯净电视.apk
```
或直接用 Chrome 打开 `android/app/assets/player.html`（需允许 file 协议 fetch，仅供看 UI）。

### 官网源专用自检

**① Python 侧**（解密 + 镜像回退 + 降级链路）：
```bash
python - <<'PY'
import sys; sys.path.insert(0, "shared")
import wvt
print("AES key =", wvt._APP_CLASS[:32][::-1])
print("AES iv  =", wvt._APP_CLASS[:16])
ch = wvt.load_channels(cache_dir="_wvt_cache")
print("频道", len(ch), "分组", len({c["group"] for c in ch}))
PY
```

**② 两版解密结果必须逐字节一致**（改过任何一边都要跑）：
```bash
# 先抓一份真实 .dat 做样本，再用 Python 生成期望值，最后让 JS 解同一份密文对比
python -c "
import sys,os,json; sys.path.insert(0,'shared'); import wvt
os.makedirs('_aes_test',exist_ok=True)
for rel in ('data/4/full.dat','data/4/webpage_adapter.dat','data/4/server_config.dat'):
    d,u = wvt.download_dat(rel); f=os.path.join('_aes_test',os.path.basename(rel))
    open(f,'wb').write(d)
    json.dump(wvt.decrypt_dat_json(d), open(f+'.expect.json','w',encoding='utf-8'),
              ensure_ascii=False, separators=(',',':'))
"
node _aes_test/_check.js        # 期望输出：全部一致 ✅
```

**③ 刷新内置快照**（上游更新频道表后）：
```bash
python tools/gen_wvt_snapshot.py    # -> shared/wvt_builtin.json（联网）
python tools/gen_wvt_js.py          # -> android/app/assets/wvt_channels.js
```

**④ 桌面端 UI 冒烟**（打桩，快，不联网也能跑大部分）：
```bash
python tools/check_wvt_ui.py          # 9 步：切源 / 分组 / 直连 / 网页 / 退路嗅探 / 过期结果 / 全屏 / 搜索
```

**⑤ 桌面端真机端到端**（真开官网页 + 真内嵌 + 真全屏，需联网，约 30s）：
```bash
python tools/check_wvt_embed.py CCTV-13
```
会用 Win32 `GetWindowRect` 逐像素比对「浏览器窗口矩形 == 播放区矩形」，
覆盖 窗口态 / 全屏 / 退出全屏 / 复用窗口 四个场景，并截图到 `_shots/`。
期望输出最后一行 `RESULT PASS`。

**⑥ exe 打包完整性**（构建后跑）：
```bash
python tools/check_exe_pack.py        # 数据文件 + PYZ 里的关键模块（embed / cdp / ctypes / tkinter / PIL）
```
