# 共享业务核心 · 维护说明

本目录是**唯一真源**。三个端不各写一份业务逻辑，全部从这里取：

| 文件 | 谁在用 | 说明 |
|---|---|---|
| `core.py` | Windows / Linux 桌面端（`desktop/tv_app.py` 载入） | 纯标准库，无 GUI、无平台代码 |
| `web/tv_core.js` | 安卓 WebView 端（`android/app/assets/player.html`） | 与 `core.py` 逐行对齐 |

`android/app/assets/tv_core.js` 是 **build.sh 自动拷贝** 的副本，**不要直接改它** ——
改了下次构建就被覆盖。

---

## 改了 Python 版？照着这张表同步 JS 版

| 功能 | `core.py` | `web/tv_core.js` |
|---|---|---|
| 伪装 UA | `APP_UA` | `TVCore.APP_UA` |
| 接口地址 | `API` | `TVCore.API` |
| 分类表 | `CATS` | `TVCore.CATS` |
| 频道列表解析 | `parse_channels()` | `parseChannels()` |
| 频道列表抓取+缓存 | `fetch_channels()` | `fetchChannels()` |
| **线路解密** | `decrypt_page()` | `decryptPage()` |
| EPG 解析 | `parse_epg()` | `parseEpg()` |
| 播放页抓取 | `fetch_play()` | `fetchPlay()` |
| 台标匹配 | （无，见下） | `logoSrc()`（台标只在客户端用） |
| 收藏 | `load_fav()/save_fav()` | `favArr()/favSave()/isFav()/toggleFav()` |
| 缓存 TTL | `CACHE_TTL` | `CACHE_TTL` |

> **UI / 播放层逻辑不在这份核心内**。全屏、横屏、手势、选路、缓冲策略属于「呈现层」，
> 各端按平台能力自行实现：
>
> | 能力 | 安卓（`player.html` + `MainActivity.java`） | 桌面（`index.html`） |
> |---|---|---|
> | 全屏 | `setFsMode()` 走原生 `NativeBridge.setLandscape()` 真转屏 | `toggleFs()` 走 Web Fullscreen API |
> | 双击画面切全屏 | `bindTouchGestures()` | `bindDblClickFs()` |
> | 数字键直选频道 | `numBuf` + 1.2s 缓冲 | 同逻辑（键盘数字键） |
> | 线路测速选优 | `probeLine()` / `pickBestLine()` | 同名函数，档位权重不同 |
> | 缓冲策略 | `mpegtsCfg()` 按 `TIER` 分三档 | 同结构，档位阈值更宽松 |

### 为什么台标匹配只在 JS 版
台标（`logos.js`）是纯前端资源，桌面端由浏览器直接引用、安卓端由 WebView 直接引用，
Python 侧不需要这份逻辑。

### 为什么收藏实现不同
- 桌面端：Python 读写 `fav.json`（前端调 `/api/fav` 存取）
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

改完 Python 版，跑一下：
```bash
python3 shared/core.py
```
会打印 UA、频道数、第一条线路、线路数、EPG 条数。全都有值即正常。

改完 JS 版，安卓端可这样验：
```bash
cd android && bash build.sh && adb install -r 纯净电视.apk
```
或直接用 Chrome 打开 `android/app/assets/player.html`（需允许 file 协议 fetch，仅供看 UI）。
