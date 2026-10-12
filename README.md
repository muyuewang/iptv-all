# 纯净电视直播 · 三端合一工程

无广告、无人机验证、无激励视频门的电视直播客户端。直接调用原 App 的后端接口，**自主解密**线路播放。
**v2.0 起同时兼容 WebViewTV 官网源**（904 频道 / 51 分组），自建源不稳定时可一键切换（v2.2 起安卓端也是同样的「本机源 / 官网源」分流）。
**v2.3 / v2.3.2 起桌面端把官方直播页的浏览器窗口直接「嵌」进播放区**（不再多出一个浏览器窗口），并修好全屏、统一了源切换与分类标签的样式。
**v2.4 起桌面端网页线路改用 WebView2 运行时 COM 直调内嵌**（不再外拉 Edge/Chrome 进程再用 Win32 `SetParent` 搬窗口），网页线路全败时自动走 CDP 嗅探 → mpv 兜底。

> ★ **开发者请先读 [开发手册.md](开发手册.md)** —— 技术选型 / 架构 / 数据源解密原理 / **踩坑实录**（WebView 媒体引擎、构建链、Tk/线程模型…）/ 迭代指引与回归清单。改代码前花 10 分钟通读，能避开已知的所有大坑。
>
> ★ **要做优化请看 [优化建议.md](优化建议.md)** —— 仓库瘦身（工具链误入库 14,418 个文件）/ 起播加速 / 无缝换台 / 过渡动效 / 错误提示组件化，含三阶段路线图与 Top 3 优先项。
>
> ★ **想抄点别人家的好东西请看 [借鉴方案-TVBoxOS.md](借鉴方案-TVBoxOS.md)** —— 精读 TVBoxOS 源码后的对照评估：起播看门狗三级退让 / 全源失败自动跳台 / HLS 跳过坏切片 / 换内核重播同流等 5 项值得抄，另有 5 项明确列了「不要抄」及原因，附可复查的证据索引（文件+行号）。

一份业务核心（`shared/`），三个端复用：

| 端                     | 目录         | 产物                                  | 状态                       |
| --------------------- | ---------- | ----------------------------------- | ------------------------ |
| **安卓手机 + Android TV** | `android/` | `android/纯净电视.apk`（约 5 MB，含双后端硬解） | ✅ 实测构建通过，已装真机验证（含 TV 播放 + 官网源） |
| **Windows 原生版**       | `desktop/` | `desktop/dist/电视直播.exe`（约 64 MB，含 mpv） | ✅ 原生一体化窗口 + mpv 内嵌硬解 + 官网源（**v2.4：网页线路改 WebView2 COM 直调内嵌，不再外拉浏览器进程**） |
| **Linux**             | `linux/`   | `linux/dist/纯净电视`（单文件）+ 可选 AppImage | ✅ 原生一体化窗口，系统 mpv 硬解（v1.8） |

> 手机与 TV 用**同一个 APK**，自动识别大屏进入 10-foot 遥控布局。  
> 三端共用同一套设计令牌（Design Token），观感统一、符合当下主流审美，详见下方「视觉设计」。

---

## 目录结构

```
tv_all/
├── README.md                    本文件（三端总览）
├── 架构与流程图-两源两端.md      ★ 两源 × 两端的架构图 / 流程图 / 文字说明（Mermaid）
├── 开发手册.md                  开发知识总集（数据源 / 选型 / 踩坑 / 迭代指引）
├── 官网源m3u8提取实测.md        15 站提取命中率实测（v2.9 判据依据）
├── shared/                      ★ 共享业务核心 —— 唯一真源
│   ├── core.py                  Python 版（Windows / Linux 桌面端用）—— 本机源
│   ├── wvt.py                   Python 版官网源适配层（AES-256 + 镜像回退 + 缓存）
│   ├── wvt_builtin.json         官网源内置快照（904 频道，127 KB，断网兜底）
│   ├── README.md                两张核心的同步对照表
│   └── web/
│       ├── tv_core.js           JS 版（安卓 WebView 端用）—— 本机源
│       ├── wvt.js               JS 版官网源适配层（与 wvt.py 逐条对齐）
│       └── wvt_inject.js        ★ 官网直播页注入脚本 —— **安卓与桌面端共用同一份**
│                                （安卓由 build.sh 拷进 assets，桌面端 embed.py 直接读）
│
├── tools/                       一次性脚本 + 自检工具
│   ├── gen_wvt_snapshot.py      刷新 shared/wvt_builtin.json（联网）
│   ├── gen_wvt_js.py            由 wvt_builtin.json 生成安卓快照 JS
│   ├── gen_wvt_aes_fixture.py   抓真实 .dat + Python 解密，产出交叉验证样本
│   ├── check_wvt_aes.js         Node 端跑 JS 解密并与 Python 期望值逐字节比对
│   ├── check_wvt_ui.py          桌面端官网源 UI 冒烟（打桩，事件驱动，9 步，约 13s）
│   ├── check_wvt_embed.py       ★ 桌面端真机端到端：真开官网页 + WebView2 内嵌起播判定
│                                （约 30s，需联网；v2.4 起断言语义已按新 embed 模型复核）
│   ├── check_wvt_real.py        桌面端真嗅探端到端（CDP → mpv）
│   ├── check_wvt_android.sh     安卓端官网源网页线路批量冒烟（跑在真机/模拟器上）
│   ├── shots_desktop.py         ★ 桌面端 UI 截图取证（起真 app，本机源/官网源各抓一张）
│   ├── adev.sh                  安卓 WebView CDP 求值（adb 转发 → /json/list → 求值，一次调用完成）
│   └── android_eval.py          adev.sh 的实际求值器
│
├── android/                     安卓端（手机 + TV）
│   ├── build.sh                 一键构建（无需 Gradle/Android Studio）
│   ├── ks.jks                   签名密钥（自签名）
│   ├── tools/                   构建工具链（aapt2/d8/android.jar，体积大，见下）
│   └── app/
│       ├── AndroidManifest.xml  含 LEANBACK_LAUNCHER + TV Banner
│       ├── src/com/clean/tv/MainActivity.java   WebView 壳（UA / 全屏 / 返回键 / 官网页面覆盖层）
│       ├── assets/
│       │   ├── player.html      界面 + 遥控导航（业务逻辑全部来自 tv_core.js / wvt.js）
│       │   ├── tv_core.js       ← 由 build.sh 从 ../shared/web/ 自动同步
│       │   ├── wvt.js           ← 由 build.sh 从 ../shared/web/ 自动同步
│       │   ├── wvt_channels.js  官网源内置快照（gen_wvt_js.py 生成）
│       │   ├── wvt_inject.js    官方直播页注入脚本（找 video / 钉全屏 / 去浮层 / 起播超时上报）
│       │   ├── logos.js         1800+ 台标映射
│       │   └── mpegts.js        FLV 直播播放库
│       └── res/                 图标与 TV Banner
│
├── desktop/                     Windows / Linux 桌面端（原生一体化窗口）
│   ├── native_app.py            ★ 主入口：tkinter 窗口 + mpv/WebView2 内嵌（硬解）＋ 官网源
│   ├── embed.py                 ★ 官网源网页线路的「内嵌浏览器」（WebView2 运行时 COM 直调，
│   │                               纯标准库 ctypes，无 pythonnet/无 win32com；附 CDP 嗅探兜底）
│   ├── WebView2Loader.dll       微软官方可再分发加载器（约 190KB，随 exe 打包进 _MEIPASS 根）
│   ├── cdp.py                   官网源嗅探器（真浏览器 + CDP，纯标准库）—— 没浏览器时的退路
│   ├── 电视直播原生.spec          PyInstaller 打包配置（含 mpv，别人电脑免安装）
│   ├── build_windows.bat        一键打包 Windows 单文件 exe
│   ├── mpv/                     内置 mpv（mpv.exe + d3dcompiler_43.dll），随 exe 打包
│   ├── assets/                  台标映射 / 播放库（供备用浏览器形态使用）
│   │   ├── index.html           备用浏览器形态界面（软解兜底）
│   │   ├── logos.js             1800+ 台标映射
│   │   └── mpegts.js
│   ├── tv_app.py                备用：本地服务 + 浏览器 --app 壳（无 mpv 时软解）
│   ├── app.ico                  应用图标（电视造型，gen_icon.py 生成，含 7 个尺寸）
│   ├── refresh_icon_cache.bat   换图标后刷新 Windows 图标缓存（管理员运行）
│   ├── gen/gen_icon.py          生成图标（相对路径，任意目录可跑）
│   ├── settings.json            〔运行时生成〕设置持久化（域名/UA/缓冲…）
│   ├── fav.json                 〔运行时生成〕收藏（绿色便携，换目录即重来）
│   ├── logocache/               〔运行时生成〕台标缩略图缓存
│   ├── wvt_channels.json        〔运行时生成〕官网源频道表缓存（24h TTL）
│   └── .sniff/                  〔运行时生成〕嗅探用浏览器 profile（复用可把 40s 降到 2s）
│
└── linux/                       Linux 专属入口
    ├── run.sh                   一键运行（源码方式，零编译）
    ├── build_linux.sh           打包单文件 / AppImage
    └── install.sh               装进应用菜单（可选）
```

---

## 快速开始

### 直接下载（开箱即用）

成品在 GitHub **Releases** 页下载：

| 文件 | 适用 | 说明 |
| --- | --- | --- |
| `IPTV-v2.10.7.apk` | 安卓手机 / TV / 盒子 | 下载后直接安装 |
| `IPTV-v2.10.7-Windows.exe` | Windows 10/11 x64 | 单文件双击即用，无需安装 |

> Windows 端官网网页线路需 WebView2 运行时（Win11 自带）。

### 安卓（手机 / TV / 盒子）

```bash
cd android
bash build.sh          # 产出 纯净电视.apk
```

安装后：TV 上出现在 Android TV 主屏；遥控器 **方向键**移动焦点、**OK** 播放、**返回**退出播放。

> 签名密钥 `ks.jks` 不入库：首次 `build.sh` 检测到缺失会用 keytool 自动生成自签名密钥。

真机 / 模拟器上调试官网源网页线路时还要用到 `adb`（不入库，按需自备）：

```bash
cd android/tools
curl -LO https://dl.google.com/android/repository/platform-tools-latest-windows.zip
unzip -oq platform-tools-latest-windows.zip     # → tools/platform-tools/adb.exe
```

### Windows

```bat
cd desktop
build_windows.bat      :: 产出 dist\电视直播.exe
```

> 桌面端运行依赖 `desktop/mpv/mpv.exe`（约 120 MB，超 GitHub 单文件上限不入库）：
> 从 https://mpv.io/installation/ 下载 Windows 版，把 `mpv.exe`、`d3dcompiler_43.dll`
> 放进 `desktop/mpv/` 即可（`build_windows.bat` 与 `native_app.py` 均按此路径查找）。

双击 `电视直播.exe` 即可，无地址栏独立窗口，关窗后自动退出。

### Linux

```bash
cd linux
./run.sh                       # 直接运行（推荐先试这个）
./build_linux.sh               # 打包单文件 → linux/dist/纯净电视
./build_linux.sh --appimage    # 额外做成 AppImage
./install.sh                   # 装进应用菜单（可选）
```

---

## 各端依赖

| 端          | 依赖                                                      |
| ---------- | ------------------------------------------------------- |
| 安卓构建       | JDK（javac/keytool）+ `android/tools/` 下的 build-tools（见下） |
| Windows 运行 | WebView2 运行时（Win10/11 自带；官网源网页线路用。缺失时自动退化为 CDP 嗅探 → mpv） |
| Windows 打包 | Python 3.8+ ，`pip install pyinstaller pillow`           |
| Linux 运行   | python3（系统自带）+ Chrome/Chromium/Edge 任一                  |
| Linux 打包   | python3-venv、python3-pip                                |

### 安卓构建工具链从哪来

`android/tools/` 约 200 MB，未随工程提交。两种获取方式：

**方式 A（推荐）** 从已导出的旧工程复制：

```bash
cp -r /path/to/apk_clean/build/tools android/tools
```

其中需含：

- `tools/android-14/`（aapt2、zipalign、apksigner）
- `tools/android-34/android.jar`
- `tools/r8.jar`

**方式 B** 用 Android SDK 里的对应文件，构建时用环境变量覆盖：

```bash
ANDROID_BUILD_TOOLS=/path/to/sdk/build-tools/34.0.0 \
ANDROID_JAR=/path/to/sdk/platforms/android-34/android.jar \
R8_JAR=/path/to/sdk/build-tools/34.0.0/lib/r8.jar \
bash android/build.sh
```

---

## 视觉设计

三端界面共用**同一套设计令牌**（Android `player.html` 与桌面 `index.html` 的 `:root` 完全一致），  
改一处观感即可整体统一。

**基础令牌**

```css
--bg:#0a0b0f;                                    /* 底色 */
--bg-grad:radial-gradient(120% 80% at 50% -10%, #1b1220, #0a0b0f 58%);  /* 顶部微光 */
--surface:rgba(255,255,255,.045);                /* 卡片底 */
--surface-2:rgba(255,255,255,.085);              /* 悬浮/次级 */
--line:rgba(255,255,255,.075);                   /* 描边 */
--fg:#f2f4f8; --fg-dim:#a2a9b8; --fg-mute:#6b7385; /* 三级文字 */
--brand:#ff2d55; --brand-2:#ff6b35;              /* 品牌渐变两端 */
--brand-grad:linear-gradient(135deg,#ff2d55,#ff6b35);
--gold:#ffc531;                                  /* 收藏 / 高亮 */
--r-sm:8px; --r-md:12px; --r-lg:16px; --r-xl:22px;
--ease:cubic-bezier(.32,.72,0,1);                /* 统一缓动 */
```

**设计要点**

- **分层暗色**：纯黑底 + 顶部径向微光 + 半透明卡片，靠透明度与描边叠出层次，不用纯灰块。
- **毛玻璃**：顶栏 / 播放控制条 / 提示用 `backdrop-filter: blur()` + 半透明底。
- **品牌渐变**：选中态、主按钮、EPG「正在播」左侧色条统一用 `#ff2d55→#ff6b35` 渐变。
- **微交互**：卡片 hover 上浮 `translateY(-2px)` + 渐变描边渐显；激活态带 `--glow` 外发光。
- **TV 焦点**：大屏模式焦点从刺眼的金色描边改为柔和红色辉光 `box-shadow:var(--glow)` + `scale(1.045)`，符合 10-foot 观看距离。

**图标**  
统一采用**电视/显示器造型**（品牌渐变机身 + 深色屏幕 + 白色播放三角 + 底座），贴合「电视直播」的产品定位，且在小尺寸下依然可辨：

- Android：`tools/gen_assets.py` 生成 `ic_launcher.png`（192×192）与 `tv_banner.png`（320×180）。
- 桌面：`gen/gen_icon.py` 生成 `app.ico`，**内含 7 个尺寸**（16/24/32/48/64/128/256）；  
  其中 ≤32px 单独手绘（三角更大、底座加宽、去掉细节），避免任务栏图标糊成一团。

> 两个生成脚本均使用**相对路径**（基于脚本自身位置），在任何目录下执行都能写对位置。  
> 换图标后，桌面端需重新打包 exe、安卓端需重新构建 APK 才会生效。

**任务栏图标不刷新？** Windows 会缓存 exe 图标（尤其替换同名 exe 后，任务栏常继续显示旧图标）。  
在 `desktop/` 目录下**以管理员身份运行** `refresh_icon_cache.bat` 一键刷新；或手动  
重启资源管理器 / 注销重登。换图标后须重新打包（桌面重打 exe、安卓重构建 APK）才生效。

---

## 工作原理

原 App 的服务器**靠 User-Agent 区分新旧版播放器页**：只有带  
`...diashizhb LT-APP/48/517/YM-RT/` 的 UA 才返回新版页面。  
浏览器被禁止伪造 UA，所以网页版行不通 —— 因此三端各用一个能设 UA 的「壳」：

```
                    ┌─ 安卓：原生 WebView（setUserAgentString + 本地 player.html）
伪装 UA 抓取 + 解密 ─┼─ Windows / Linux：原生一体化窗口(native_app.py，tkinter + mpv 内嵌)
                    └─ 备用：本地服务(tv_app.py) → 浏览器 --app 无边框窗口（软解兜底）
```

接口与解密（三端完全一致，代码在 `shared/`）：

```
频道列表  https://api1.2026016.xyz/iptve.php?tid=<tv|ty|ys|ws|wintv123|gt>&app=517
播放页    https://api1.2026016.xyz/iptve.php?act=play&tid=..&id=..
线路解密  option反转 → base64解 → XOR(key) → base64解 → 换token → 去盐前缀
播放      eplay2.php?token=..&tid=..&id=..&p=0..7&type=.flv → 302 → 字节 CDN 的 FLV
```

**关键技巧：解密密钥的盐是动态的（含 deviceId / appVersion / ips / 日期）。  
这里不去猜盐，而是用第 1 条线路的已知明文反推 XOR key —— 明文结构固定  
（`<API>/eplay2.php?token=<旧token>&tid=..&id=..&p=0&type=.flv`），与密文逐字节异或即得  
密钥，于是无论盐怎么变都能解出整页全部线路。**

视频流直连（接口已开 CORS），**不经过本地服务中转**，省带宽降延迟。

---

## 播放优化（v1.2）

原版 App 用原生播放器 `libtbMPlayer`（C++ + 硬件解码），而本项目的 WebView 方案用  
**mpegts.js 软解 FLV** —— 这是低端设备卡顿的根本来源。实测 4 核 / 4GB 的机器解  
<1080p@2.4Mbps>，20 秒只推进 4.6 秒（23% 实时速度），`readyState` 长期为 2（数据饥饿）。

v1.2 加入三层优化，同一台机器**20 秒推进 21.15 秒（106% 实时），完全不卡**：

**1. 码率感知的线路智能选路**（收益最大）

- 进入频道时并发探测全部线路的「首字节延迟 + 实际码率」（取 64KB 头部，读 FLV metadata）
- 按设备档次加权：**低端机码率惩罚 3.0**（宁可低清也不卡），中高端 0.6
- 评分 = 延迟(s) + 码率(Mbps) × 权重，取最低分
- 实测：8 条线路延迟 1.6s ~ 3.9s 差 2.4 倍；且各线路码率不同（1080p@2441kbps vs 720p@976kbps），  
  低端机自动避开重线路

**2. 设备分级缓冲策略**（`TIER` = low/mid/high，按 `deviceMemory`/`hardwareConcurrency`/分辨率判定）

| 档位   | Worker             | 缓冲                   | 追帧                  |
| ---- | ------------------ | -------------------- | ------------------- |
| low  | **关**（避免线程开销与内存翻倍） | stash 256KB，回退窗口 14s | 激进（maxLatency 1.2s） |
| mid  | 开                  | 384KB / 18s          | 1.8s                |
| high | 开                  | 512KB / 24s          | 2.5s                |

原配置的 `enableWorker:true` + `lazyLoad:false` + 无 `liveBufferLatencyMaxLatency`  
会让缓冲无限堆积、软解抢主线程 —— 已全部修正。

**3. WebView 层硬解优化**

- `AndroidManifest` 的 `targetSdkVersion` 由 28 提到 **34**（启用现代媒体管线，API 28 会走兼容模式）
- 加 `android:largeHeap="true"`、`FLAG_HARDWARE_ACCELERATED`、`LAYER_TYPE_HARDWARE`、  
  `RENDERER_PRIORITY_IMPORTANT`
- 低端机限制 `<video>` 解码输出宽度 = 屏幕宽度，降低 GPU 合成压力

**4. 卡顿自愈**：5 秒内 `waiting` ≥ 4 次判定线路不稳，自动切下一条。

> 注意：以上仍是 JS 软解，天花板低于原生硬解。若要彻底追平原版，需接原生播放器  
> （ExoPlayer / ijkplayer）。当前优化已让常见低端设备流畅播放。

---

## 常见问题

**Q：某频道打不开 / 全部线路失败？**  
令牌是会话级的，**返回列表重新进入**即可刷新。仍不行说明该源临时失效。

**Q：播放卡顿 / 一卡一卡的？**  
v1.2 已做码率感知选路 + 设备分级缓冲 + WebView 硬解优化（详见「播放优化」章节）。  
若仍卡：① 换线路（控制条内点其它线路）② 确认设备档次判定是否合理  
（低端机会主动选低码率线路，画质会略降但更流畅）③ 这是 JS 软解的天花板，  
要更流畅需换原生播放器。

**Q：改了 `shared/core.py`，三端都生效吗？**  
桌面端（Win/Linux）重新运行/打包即生效。安卓端 `build.sh` 会自动把  
`shared/web/tv_core.js` 同步进 APK —— 所以**改完 Python 记得同步改 JS 版**，  
对照表见 `shared/README.md`。

**Q：Windows 杀软报毒？**  
PyInstaller 单文件程序的启发式误报，加信任即可。程序只做两件事：访问直播接口、  
在本机 `127.0.0.1` 起临时服务，不写注册表、不自启。

**Q：打开 exe 时弹出「我们正在你的所有设备上同步你的浏览数据 / 已在此设备上登录 Microsoft Edge」？**  
**v2.4 起官网源网页线路走 WebView2 运行时内嵌（独立用户数据目录，不走你的 Edge 账户），正常不会再见到这个弹窗。**  
只有在网页线路全败、退化到 CDP 嗅探兜底时才会临时起一个 Edge/Chrome —— 程序在启动前已写入
`Preferences`（关闭 `sync`/`signin`/首次运行引导）并附 `--disable-sync` 等开关。若仍弹，
在弹出窗口里点「不是现在 / 不用了」即可，**不影响播放**；或删掉 exe 同目录的 `.sniff` 文件夹后重启程序。

**Q：收藏存在哪？**

- 安卓：WebView localStorage
- Windows：exe 同目录 `fav.json`（拷贝程序时一起带走）
- Linux：程序目录 `fav.json`

**Q：Linux 上没弹出独立窗口？**  
说明没装 Chromium 系浏览器，程序会退回默认浏览器打开标签页（功能正常，只是外观差些）。  
装一个即可：`sudo apt install chromium-browser`。

---

## 版本纪要

| 版本   | 要点                                                                                |
| ---- | --------------------------------------------------------------------------------- |
| v1.0 | 三端骨架：伪装 UA + 线路解密 + WebView 壳                                                     |
| v1.1 | 设计令牌统一、图标体系、TV 10-foot 遥控布局                                                       |
| v1.2 | **播放优化**：码率感知选路、设备分级缓冲、WebView 硬解、卡顿自愈                                            |
| v1.3 | **手机全屏自动横屏**、双击手势、遥控数字键直选、频道加减台、桌面双击全屏（详见下节）                                      |
| v1.4 | **原生硬解播放（ExoPlayer）**：MediaCodec 硬解根治卡顿，JS 软解保留兜底（详见下节）                           |
| v1.5 | **兼容 Android 4.4 + 双后端硬解**：老 ExoPlayer2 后端（API 16+）、minSdk 降到 19、硬解失败防死循环、桌面端断流重连 |
| v1.6 | **电视家式播放交互重构**：修复全屏点不出菜单、后台仍有声；上下双栏控制条、线路弹层、亮度/音量手势、画面比例                          |
| v1.7 | **桌面端 mpv 硬解**：装 mpv 后自动切 GPU 硬解（D3D11VA），彻底根治桌面端卡顿                               |
| v1.8 | **桌面端原生一体化窗口**：tkinter 主窗口 + mpv `--wid` 内嵌（不再弹独立窗口）、mpv 随 exe 捆绑免安装（详见下节）              |
| v1.9 | **桌面端界面与稳定性增强**：台标 / EPG 面板 / 画面比例 / 音量 / 搜索、接口镜像回退、设置页、mpv 异常自动切线 + token 刷新（详见下节）           |
| v2.0 | **接入 WebViewTV 官网源**：904 频道 / 1114 线路 / 51 分组；桌面端直连走 mpv、网页线路走 CDP 嗅探，安卓端用覆盖层 WebView + JS 注入自己播（详见下节）           |
| v2.1 | **官网源网页线路修实**：放弃搬 DOM（会打断 MSE 播放）改成原地钉全屏；修掉「自建容器被自己的清理逻辑打回原形」的 bug；通用去浮层；起播判定改用 `currentTime`；**线路起不来自动换下一条官网线路** |
| v2.2 | **安卓端数据源分流**：顶栏加「本机源 / 官网源」分段切换（与桌面端对齐），两套频道表各自记住浏览位置；官网源搜索改为跨全部分组；布局改纵向 flex，顶栏加减内容不再需要改高度魔法数字 |
| v2.3 | **桌面端官网源「内嵌浏览器」**：Edge 窗口用 Win32 `SetParent` 嵌进播放区（不再多出一个浏览器窗口/进程）；`Browser.close` + `exit_type=Normal` 消灭「还原页面」弹窗；**修好全屏「缺画面」**（Win32 子窗口不跟随 resize → `cover_child_windows` + `GetAsyncKeyState` 兜 F11/Esc）；「本机源/官网源」与分类标签统一为手绘胶囊条（`PillBar`）；安卓端源码条同款样式 + 注入脚本改用 `visualViewport` 并压掉页面 `zoom`，修掉官网源全屏「很大、显示不全」 |
| **v2.3.2** | **修「exe 播放视频又打开一个应用」**：删掉 `--kiosk`（实测它启动时+播放中途会两次抢全屏，第二次把已 `SetParent` 的窗口从宿主里摘出来变回顶层窗口）；标题栏改用「剥 `WS_CAPTION` + `client_insets/fit_child_to` 客户区对齐」消掉；`find_hwnd` 加 `_score_window` 打分挑真窗口；`GetParent` 复查 `SetParent`；`_size_watch` 盯客户区尺寸；`_hide_guard` 降级为兜底 |
| **v2.3.3** | **收尾内嵌 + 统一下拉**：① Chromium 现在把标题栏**画在客户区里**（Windows 报的非客户区顶边恒 0，剥不剥 `WS_CAPTION` 都一样）→ 改为「按 DPI 估算自绘栏高度、把窗口整体往上顶出播放区」；② `find_hwnd` 淘汰 `(-32768,-32768)` 的未定位占位窗口 + 按 `--window-size` 匹配真窗口；③ 并发换台串行化 + 代次作废（修 `port=None` 的 `%d` TypeError 与漏进程）；④ **「分类 / 分组」统一为手绘美化下拉 `DropSelect`**（本机源与官网源同一个控件：圆角 + 品牌渐变 + 悬停高亮 + 滚动，取代原生 ttk.Combobox 与平铺胶囊两种形态） |
| **v2.3.4** | **修下拉首点竖排字 + 位置统一**：① **「第一次点下拉弹出竖排字」** —— 旧包 `_draw_pop` 在浮层布局完成前取 `winfo_width()=1`，整条列表被挤成一竖条（一个字一行）；`_pop_w/_pop_h` 记尺寸之外再加硬兜底（宽度永不小于 `MIN_W`），且真实点击路径加了「点在按钮上不算点别处」守卫（防主窗口 `<Button-1>` 冒泡把刚展开的浮层秒收）；② **分类 / 分组占顶部同一槽位** —— 以前分组行排在频道列表后面被 pack 挤到左栏最底下（分类在顶、分组在底），现在两个选择器互斥地占 `filter_slot` 同一格，切源位置不动；③ 真实点击开/关、槽位 Δy、竖排字像素检测全部进了 `shots_dropdown.py`（24 项回归） |
| **v2.3.5** | **修「官网源换台后浏览器跑出去自己在桌面播」**：用户实测日志（cleantv.log）抓到**互杀风暴** —— `_connect` 把「被新请求取代」和「真失败」混为一谈，被取代的旧线程走 `close()` 杀掉新请求正要复用的浏览器 → 新线程再起一只 → 再被更新的请求杀……实测 19500/19501 两端口轮流起浏览器、几秒一杀，最后漏出去一只没内嵌的 `--app` 窗口在桌面上自己播（任务栏多出「浏览器 + 央视频」）。修：① play() 在 close 前先查代次，被取代时安静退出；② `attach()` 等窗口从「一把 25s」改成「2s 一段、段间查代次/查进程」，不再拿死 pid 傻等（修「找不到浏览器窗口放弃内嵌」）；③ 启动前清掉 Chromium 按网址记的 `app_window_placement`（它跟我们 `--window-size` 打架）；④ 尺寸看门狗日志节流 10s/条（央视频页面会一直 resizeTo，之前 1 分钟刷 200 行）；⑤ 新增 `check_switch_channels.py`（4~5 个不同站点连切压力测试）与 `check_ui_ops.py`（本机源全操作 19 项点测）两道回归 |
| **v2.4** | **桌面端网页线路重写为 WebView2 COM 直调内嵌（详见下节）**：不再外拉 Edge/Chrome 进程再 `SetParent` 搬窗口 —— 直接加载 WebView2 运行时（Win10/11 自带），`ctypes` 手搓 COM vtable（纯标准库，无 pythonnet），页面就是子 HWND、全屏/缩放天然跟随宿主；修掉「Tk 回调里嵌套泵派发 Tcl 事件 → GIL 段错误」（`_ui` 改线程安全队列 + 主线程排水，阻塞等待全部挪出 Tk 回调）；**排查到底：WebView2 运行时不捆绑 Widevine CDM → CCTV 等 Widevine DRM 官网页线路平台性无解**（PlayReady 在但站点不用；广东卫视移动版 m.gdtv.cn 实测正常起播）；新增两级韧性兜底——单条线路起播失败自动换下一条官网线路，**全部线路失败自动 CDP 嗅探真实流地址交给 mpv**；`WebView2Loader.dll`（微软官方可再分发，约 190KB）随 exe 打包 |
| **v2.4.1** | **点击反馈 + 本机源提速**：① 点击频道后播放区立即铺**等待动画层**（频道名 + 旋转弧 + 步进提示 + 流动细条），mpv 视频子窗口出现（≈第一帧上屏）才收掉，「点了有没有反应」从此一目了然；② 频道列表**选中行高亮**（品牌暗红底，换源/换分组/搜索后都不丢）；③ **接口镜像回退超时封顶**（主域名 8s、后续镜像各 5s，两端 core 同步改）——以前一个挂死的主站会串行吃满 15s×4 个域名，表现为「点开频道半天没反应」 |
| **v2.4.5 ~ v2.4.8** | 付费标记（VIP/限免角标）→ **播放失败秒级反馈** → **修后台声音残留**（双根因：`__appPause` 跨作用域写闸门 + `visibilitychange` 晚到）→ 修「返回后画面还在 / 官网源返回要按两次」 |
| **v2.5** | **清爽与秒开**：仓库瘦身（工具链误入库 14,418 个文件移出版本控制）· 缓冲分档「秒开/稳定」（`PlayTuning.java`，起播门槛 700ms/1500ms）· 错误提示组件化（4 类 Toast + `humanReason()` 原因映射 + 失败卡）· 播放层进出动效（≤300ms，只动 `transform`/`opacity`） |
| **v2.5.1** | **无缝换台**：原方案「保留 surface」被实测否决（`ExoPlayer.release()` 会清空 TextureView 纹理，揭帧后亮度 0.0）→ 新增 `FrozenFrame.java` 在 release 前 `getBitmap()` 抓帧钉成 ImageView，新流 `onRenderedFirstFrame` 才撤。**BACK→点台最长暗屏 3.36s → 0.00s** |
| **v2.5.2** | **修「请求在途时退出 → 异步回调复活播放」**：`openChannel()` 的取流/测速 Promise 链三处漏查会话闸门，用户快速按 BACK 后声音继续响 20s+。新增回归 `_test_exit_inflight.py` |
| **v2.6** | **借鉴 TVBoxOS 的播放失败处理**（详见 [借鉴方案-TVBoxOS.md](借鉴方案-TVBoxOS.md)）：① **起播看门狗** —— 挂起型线路不再逐条等满 `readTimeout 15s`，10s/15s（随「秒开/稳定」档）主动换下一条；② **全源失败自动跳下一个频道**（失败卡 8s 倒计时 + 防雪崩上限）；③ 缓冲底仓 6s→3s。**过程中实测揪出 3 个静默失效型真 bug**：media3 切源时的 `VideoSize.UNKNOWN(0x0)` 假事件把看门狗瞬间撤掉、`nativeStats()` 因 media3 线程校验恒返回 `{}`（8s 卡顿看护一直是死代码）、`minBufferMs` 撞上 `>= bufferForPlaybackAfterRebufferMs` 不变量直接崩主线程 |
| **v2.7** | **HLS 坏片自救 + 内核粒度收窄**：① 实测**推翻了** TVBoxOS 的 `HlsErrorHandlingPolicy`（挂上后坏片重试从 6 次放大到 **22 次**且仍 fatal —— 返回 `null` 是**禁用** fallback，注释写反了），**已回退**；改为在 `onPlayerError` 里自救：当前是 m3u8 + IO 类错误码时 `seekToDefaultPosition()` **跳到直播边缘重播同一条流**（最多 2 次），失败才换线路。造**真直播**流实测（`_probe_hls_live.py`）：坏片连拒 8 次后，**6.6s 恢复**、后续分片一路正常。② `NATIVE.dead` 从**频道级**收窄到**线路级** `deadIdx[i]` —— 以前一条坏线路会让本频道后续所有线路都被迫走软解。③ 顺带修「起播成功复位卡顿计时」写错变量（一直没生效）+ 卡顿判据只认前进（直播 playlist 滑动时 pos 会回跳，画面在动却被判卡死） |
| **v2.8** | **底栏播放信息角标**（借鉴 TVBoxOS 的 `LIVE_SHOW_NET_SPEED/_RESOLUTION`）：`#statBadge` 三态循环 —— 关 `ⓘ` / 简 `1080P · 软解` / 详 `1920×1080 · 软解 · ↓2.4M · 缓冲1.0s`，点一下切换、存 `localStorage`。两后端 `stats()` 补 `br`（码率）+ `bw`（带宽估计，走 `AnalyticsListener.onBandwidthEstimate` —— `ExoPlayer` 接口在 media3 1.4 起**没有** `getBandwidthMeter()`，javac 直接报找不到符号）。**两个实测坑**：① 国内 IPTV 的 HLS 不声明 `FRAME-RATE`，`fps` 是 `Format.NO_VALUE(-1)`，直接格式化会显示「-1fps」→ 过滤掉并改显示「硬解 / 软解」；② 角标**只读** `bindNativeWatch` 已有的 stats 缓存，绝不自己再调一次 `nativeStats()`（那是 `CountDownLatch` 同步等主线程最多 300ms，会白卡 WebView）。验收 `_probe_stats_badge.py`（三态 + 退出后定时器必须停） |
| **v2.9** | **安卓端官网页提取 → 原生硬解**：官网源 94.5% 是「官方直播页」（网页 WASM+MSE **软解**，盒子/低端机 CPU 吃满）。安卓 WebView 本来就加载着那个页面 → 用 `WebViewClient.shouldInterceptRequest` **旁听**它自己的 `*.m3u8` 请求（返回 `null` 不干预网络栈，零额外开销；不需要外部浏览器/CDP，比桌面端便宜一个数量级）。新增 `SniffProbe` 判可播性（递归下钻 master→media playlist + 验第一个分片容器：`0x47` 或 fMP4 `ftyp/styp/moof`）+ `nativePlayExt(url, headersJson)`（提取到的流要带 Referer/Cookie → 请求头在构建 DataSource 时设死，**必须全量重建播放器**，不能接 `switchTo`）。**★★ 最大的坑**：绝不能「只认第一条 m3u8」—— 实测 m.gdtv.cn **第一次请求的 m3u8 是废的**（`transferSize=0`），页面随即用新 token 请求第二条才成功，真正在播的是第二条；只认第一条 ⇒ 探测恒 403（`tools/_diag_wvt_m3u8.py` 用 CDP 直连官方页 WebView 抓 `performance.getEntriesByType('resource')` + `<video>.src` 才查出来）。改为逐条探测（上限 5 条、谁通用谁、拿到 ok 即收工）。策略：**白名单域（15 站实测干净）网页稳定起播 3s 后优选切换** + **任何线路起播失败时用提取地址兜底**；**央视系（cctv/央视频/cdrm）按域名直接拦掉不提取** —— 它是 TS 载荷私有加扰，探测会「通过」但播出来全花屏，且**花屏是静默的**（不报错）没法自动回退。验收 `_probe_sniff_ext.py`：拦截生效 / 探测可播 / 切原生出画（`1280x720`）三条全 PASS，两轮复现；回归 SMOKE 16/16 · WATCHDOG 8/8 · EXIT-INFLIGHT 5/5 |

---

## 原生硬解播放（v1.4 ~ v1.5）

### 为什么要上硬解

JS 软解（mpegts.js）的极限已在 v1.2 拉满：<1080p@2.4Mbps> 在 4 核低端机上  
20 秒只能推进 4.6 秒 → 调参后勉强 106%，但那已是软解天花板，源一重就卡。  
根治方案只有一条路：**把解码交给 MediaCodec 硬解（DSP），CPU 几乎不参与**。

### 架构

```
网页 player.html                       原生层（MainActivity + NativePlayer）
──────────────                        ─────────────────────────────
点频道 → 测速选路 → playIdx(i)
    ↓ 有原生桥？
NativeTV.nativePlay(url)  ──────────→ NativePlayer.play(url)
    │                                   ExoPlayer：FLV 解封装 → MediaCodec 硬解
    │                                   画面 → TextureView（全屏，WebView 之下）
    │ ← ─ ─ window.__onNativeState ─ ─  状态：buffering/ready/size/decoder/ended/error
控制条浮层（网页画，原生画面之上）
    │
    └─ 原生不可用 / 播放报错 → 自动回落 mpegts.js 软解（兜底保留）
```


### 关键改动

| 文件                      | 改动                                                                                                                                                                         |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tools/fetch_media3.py` | 新增：从 maven 拉 media3 1.4.1 + androidx + kotlin + guava 共 19 个 jar（无 gradle 构建链的依赖收集器）                                                                                       |
| `NativePlayer.java`     | 新增：ExoPlayer 硬解播放层。TextureView 输出、直播低延迟（LiveConfiguration targetOffset 1.5s）、异步解码队列、软解自动回退、MediaCodecList 硬解探测                                                             |
| `MainActivity.java`     | JS 桥新增 `hasNativePlay/nativePlay/nativeSwitch/nativePause/nativeResume/nativeStop/nativeStats/nativeHwDecode`；状态经 `window.__onNativeState(state,detail)` 透传网页；WebView 背景透明 |
| `player.html`           | `NATIVE` 通道：原生优先 → 软解兜底；`native-on` 模式（页面透明化只留控制条浮层）；断流自动重连同线路 ×2 再切线；stats 看护卡顿切线                                                                                         |
| `build.sh`              | 多 jar classpath（**javac 的 `-classpath` 必须用 cygpath 转 Windows 路径**，MSYS 不转换 `;` 串内的 POSIX 路径）；d8 multi-dex；javac 失败立即终止（防止静默打出缺类 APK）                                       |

### 踩坑实录（无 gradle 手工构建 media3 的完整代价）

| 坑                         | 现象                                                                              | 解法                                                             |
| ------------------------- | ------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `DefaultLoadControl` 约束   | `minBufferMs cannot be less than bufferForPlaybackAfterRebufferMs` 崩溃           | `setBufferDurationsMs(6000, 30000, 1500, 3000)`，min ≥ rebuffer |
| collection 1.4.0 是 KMP 包  | jar 里没有 `.class`，运行期 `CircularIntArray` NoClassDefFoundError                    | 改用 `collection-jvm` 1.4.0                                      |
| Kotlin 运行时缺失              | d8 脱糖报 `kotlin.jvm.functions.*` not found                                       | 补 `kotlin-stdlib` + `kotlinx-coroutines-core-jvm`              |
| kotlin-stdlib 超长路径        | 解包超 Windows MAX_PATH                                                            | 纯 jar 依赖不解包，直接复制                                               |
| media3-ui 连坐 recyclerview | d8 报 RecyclerView not found                                                     | 不引入 media3-ui（用自己的画面层，用不到 PlayerView）                          |
| **Player 线程检查**           | JS 桥线程调 `player.getPlaybackState()` 抛 `wrong thread` 被 catch 吞掉 → stats 永远 `{}` | `statsSync()`：post 到主线程 + CountDownLatch 等待                    |
| **视频输出没绑定**               | `attach()` 的绑定发生在 player 创建前 → 有声无画黑屏                                           | `play()` 里 build 后立刻 `setVideoTextureView(surface)`            |
| SurfaceView Z 序黑屏         | SurfaceView 独立图层 + WebView 透明的组合在部分环境整屏黑                                        | 改用 **TextureView**（画进应用窗口，合成必对；解码仍是硬解）                         |
| MSYS 路径转换                 | javac `-classpath "a;b"` 内的 `/c/...` 不被转换 → 「程序包不存在」                            | `cygpath -w` 显式转换后拼接                                           |

### 实测（LDPlayer 模拟器 / SM-N9700 伪装）

- 1080p H.264 FLV 直播：`playing=true`，播放位置实时推进，无卡顿
- MediaCodec 异步模式生效（`DMCodecAdapterFactory: asynchronous MediaCodec adapter`）
- 断流自动重连同线路，不烧线路
- 模拟器上 `hw=0/1` 取决于模拟器暴露的解码器；**真机上会命中 `OMX.qcom.* / c2.qti.*` 等硬解器**（探测逻辑已在位）
- 模拟器 `screencap` 截不到媒体层（黑图）属已知模拟器缺陷，`screenrecord` 可见真实画面

---

## Android 4.4 兼容 + 双后端硬解（v1.5）

### 问题：media3 不支持 4.4

media3 1.4.x 要求 **minSdk 21（Android 5.0）**，而很多老 TV 盒子还是 Android 4.4（API 19）。  
老版 ExoPlayer 2.19.1 支持到 **API 16（Android 4.1）**，同样走 MediaCodec 硬解。

### 方案：双后端，运行时按系统版本选择

```
NativePlayer（门面，不 import 任何播放器具体类）
    │  Build.VERSION.SDK_INT >= 21 ?
    ├── 是 → 反射加载 Media3PlayerBackend   (androidx.media3 1.4.1)
    └── 否 → Exo2PlayerBackend              (com.google.android.exoplayer2 2.19.1)

★ 关键：media3 类只在 API 21+ 反射加载，4.4 设备绝不会触碰 androidx.media3.*，
  否则 ClassNotFoundException。门面本身零 media3 import。
```

### 关键改动

| 文件                         | 改动                                                                                          |
| -------------------------- | ------------------------------------------------------------------------------------------- |
| `NativeBackend.java`       | 新增：硬解后端统一接口（屏蔽 media3 与 exoplayer2 差异）                                                      |
| `Media3PlayerBackend.java` | 新增：media3 后端（原 NativePlayer 逻辑迁移，Android 5.0+）                                              |
| `Exo2PlayerBackend.java`   | 新增：老 ExoPlayer2 后端（Android 4.4 兼容，API 几乎一致仅包名不同）                                            |
| `NativePlayer.java`        | 重构为门面：按 SDK_INT 反射选后端，加 `available()` 探测                                                    |
| `fetch_exoplayer2.py`      | 新增：拉老 ExoPlayer 2.19.1 八个模块 + androidx + guava + multidex 共 14 jar                          |
| `AndroidManifest.xml`      | `minSdkVersion` 21 → **19**；`application` 加 `androidx.multidex.MultiDexApplication`         |
| `build.sh`                 | d8 `--min-api 19` + `--main-dex-list`（legacy multidex）；媒体库去重（androidx/guava 统一用 media3 新版本） |
| `player.html`              | 硬解失败置 `NATIVE.dead` 防「硬解→软解→切线→又硬解」死循环；新频道重置                                                |

### 踩坑实录（4.4 兼容追加）

| 坑              | 现象                                                                       | 解法                                                                    |
| -------------- | ------------------------------------------------------------------------ | --------------------------------------------------------------------- |
| **方法数超限**      | 双后端 + guava 共 79496 方法 > 65536，min-api 19 时 d8 不自动 multidex              | `--main-dex-list` 显式指定启动类 + `androidx.multidex`                       |
| **依赖冲突**       | media3 与 exoplayer2 都带 androidx.core/guava，d8 报 `defined multiple times` | d8 喂料去重：androidx/guava 用 media3 的新版，exoplayer2 只取独有 `exoplayer-*.jar` |
| **老 API 签名差异** | 2.19.1 的 `onVideoSizeChanged` 是 `VideoSize` 对象而非 `int w,int h`           | 按各自 Listener 签名适配                                                     |
| **硬解失败死循环**    | `NATIVE.ok` 恒 true，硬解失败回软解后再切线又走原生                                       | `NATIVE.dead` 标记：本频道硬解失败后直走软解，新频道重置                                   |

> 验证：Android 9 模拟器走 media3 后端（`硬解后端：media3`），解码器正常启动，multidex 生效（`VM has multidex support`）。4.4 真机走 Exo2 后端（逻辑同构，API 16 兼容）。

---

## 桌面端硬解（v1.7）

### 为什么浏览器软解会卡

桌面端用 Chromium 窗口 + mpegts.js 软解。即使加了 `--enable-accelerated-video-decode`  
等硬解开关，浏览器对 **FLV/MSE 流的硬件解码支持有限**，部分环境仍走 CPU 软解，  
<1080p@2.5Mbps> 直播就会卡。

### 根治方案：mpv 原生硬解（已落地）

装 mpv 后，桌面端播放自动切换到 mpv 独立窗口硬解：

```
点频道 → 测速选路 → 检测到 mpv
    ↓
/api/native_play → Python 启动 mpv --hwdec=auto（D3D11VA/VAAPI GPU 硬解）
    ↓
mpv 独立窗口播放，浏览器只做频道列表 + 线路切换控制
```

实测（Windows + mpv 0.41.0）：

```
Using hardware decoding (d3d11va)      ← GPU 硬解，非 CPU 软解
VO: [gpu] 1920x1080 d3d11[nv12]        ← GPU 渲染 1080p
```

### 安装 mpv（一次性）

```bash
winget install --id shinchiro.mpv -e     # 官方推荐 Windows 构建
# Linux: sudo apt install mpv / sudo dnf install mpv
```

装完重新打开桌面版即可，`find_mpv()` 会自动探测 `C:\Program Files\MPV Player\mpv.exe`  
等常见路径（含 mpv.net、winget 安装目录）。

### 关键改动

| 文件           | 改动                                                                                                                       |
| ------------ | ------------------------------------------------------------------------------------------------------------------------ |
| `tv_app.py`  | `find_mpv()` 精确探测；`native_play()` 启动 mpv 硬解窗口；`/api/native_play`、`/api/native_stop` 接口；watchdog 退出时 `_stop_mpv()` 关掉播放窗口 |
| `index.html` | `MPV_READY` 标志：装了 mpv 走硬解，没装回退软解；`closePlayer` 停 mpv                                                                     |

> 未装 mpv 时仍走软解（含 v1.6 的浏览器硬解开关 + 双 Worker 解封装优化）。  
> 装了 mpv 才是彻底根治——GPU 硬解，CPU 几乎不参与。

---

## 桌面端原生一体化窗口（v1.8）

### 为什么要从「浏览器壳」换成原生窗口

v1.7 的桌面端是「本地服务 + 浏览器 `--app` 窗口 + mpegts.js 软解 + mpv 独立窗口硬解」，
问题有二：

1. **画面与操作分离**：点频道后 mpv **另开一个窗口**，频道列表/控制条还在浏览器窗口里，
   两个窗口来回切，体验割裂；
2. **浏览器软解是瓶颈**：一旦没装 mpv（或探测不到），就退回浏览器 <1080p@2.5Mbps> 软解，卡。

v1.8 改为**一个原生窗口装下全部**：

```
主窗口（tkinter）
├── 顶栏：标题 + 状态
├── 左侧：分类标签 + 频道列表（可滚动 / 可搜索）
├── 右侧：播放区 —— mpv 画面「内嵌」其中（--wid 嵌入到 Frame 的 HWND）
└── 底栏：频道名 + 线路切换 + 播放/暂停 + 收藏
```

### mpv 是如何「内嵌」的

tkinter 的 `Frame.winfo_id()` 就是该控件的**原生窗口句柄（HWND）**。把它交给 mpv 的
`--wid=` 参数，mpv 就会把视频输出**渲染进这个 HWND**，而不是自建窗口：

```python
b = self.video_host.winfo_id()          # 拿到 Frame 的 HWND
mpv  --wid=<b>  <url>  --hwdec=auto-safe  --vo=gpu
```

实测日志：`Using hardware decoding (d3d11va)` + `VO: [gpu] 1920x1080 d3d11[nv12]`
→ 画面出现在原生窗口的播放区里，**不再弹第二个窗口**。

> **Linux 同理**：`winfo_id()` 返回 X11 Window ID，`mpv --wid=` 一样能嵌。
> 只是 Linux 用系统 mpv（`apt install mpv`），不随包捆绑。

### mpv 免安装（随 exe 捆绑）

把 `mpv.exe` 与它唯一依赖的 `d3dcompiler_43.dll` 放进 `desktop/mpv/`，打包时一并塞进 exe：

| 探测优先级       | 位置                                              |
| ------------ | ----------------------------------------------- |
| ① 内置捆绑       | `_MEIPASS/mpv/mpv.exe`（PyInstaller 解压目录）        |
| ② 系统 PATH    | `shutil.which("mpv")`                           |
| ③ 常见安装路径     | `C:\Program Files\MPV Player\mpv.exe` 等 / `/usr/bin/mpv` |

打包后别人电脑**双击即硬解，无需安装任何环境**。代价是 exe 体积从 9 MB 涨到约 60 MB。

### 打包踩坑（v1.8 实录）

| 现象                              | 根因                                                        | 解法                                                    |
| ------------------------------- | --------------------------------------------------------- | ----------------------------------------------------- |
| venv 打包后运行崩溃（缺 tcl/tk）          | venv 借用系统 tkinter 但缺 tcl/tk 数据目录                          | 用**系统官方 Python** 打包（自带 tkinter/tcl/tk）                 |
| `tcl8.6/tk8.6` 数据目录 MISSING     | tcl 装在非标准路径，PyInstaller 的 hook 找不到                         | spec 里手动 `add-data` 收 `sys.base_prefix/tcl` 与 `tk`      |
| bash 跑 windowed exe 报 SIGTERM    | 无控制台的 GUI exe 在后台 shell 里被误判为崩溃                            | 用 Windows 原生方式启动验证进程存活                                 |
| mpv 有声音没画面                     | `--wid=` 指向的 HWND 在窗口完全创建前无效                             | 先 `update_idletasks()` 再取 `winfo_id()`，且等主窗口就绪           |

### 相关文件

| 文件                | 作用                                                    |
| ----------------- | ----------------------------------------------------- |
| `native_app.py`   | 主程序：窗口构建 + 频道/解密/播放 + mpv 生命周期管理                      |
| `电视直播原生.spec`     | 打包配置（含 mpv、tcl/tk 收集、hidden-imports）                  |
| `build_windows.bat` | 一键打包（检查 tkinter/mpv → 生成图标 → PyInstaller）             |
| `linux/build_linux.sh` | 同源打包（Linux 用系统 mpv，条件捆绑）                             |
| `tv_app.py`       | **备用**：浏览器壳形态，无 tkinter/mpv 的环境可用（软解）                |

---

## 桌面端界面与稳定性增强（v1.9）

v1.8 把窗口做成了原生一体化，但功能还比较素（只有频道列表 + 播放器）。v1.9 补齐体验与稳定性。

### 界面

| 能力         | 说明                                                                        |
| ---------- | ------------------------------------------------------------------------- |
| **台标**     | 解析 `assets/logos.js`（1800+ 条），与 JS 端同一套匹配规则（精确 → 去后缀别名 → 包含）；未命中显彩色首字徽标 |
| **EPG 面板** | 播放区下方节目单条，横向滚动，**当前节目高亮**并显示「正在播：xxx」                                     |
| **画面比例**   | 适应 / 铺满 / 16:9 / 4:3 四档，走 mpv `video-aspect-override` + `keepaspect`     |
| **音量**     | 滑条 + 一键静音，走 mpv IPC `set_property volume`                                |
| **搜索**     | 顶栏关键字实时过滤当前分类（当前分类 76 个频道，输入 `CCTV` 秒筛到 4 个）                              |
| **快捷键**    | `空格` 暂停 / `←→` 切线路 / `↑↓` 切频道 / `F11` 全屏 / `Esc` 退出 / `回车` 收起节目单 / `Ctrl+F` 搜索  |
| **鼠标**     | 双击 / 单击画面切全屏；频道行悬停高亮；行尾 ☆ 直接收藏                                            |

> 台标缩略图在后台线程下载并落盘缓存（`logocache/`），主线程只负责创建 `PhotoImage`（Tk 不允许跨线程调图形）。
> 装了 **Pillow** 走完整解码（PNG/JPG 都行）；没装则只解原生 PNG，其余回落徽标。

### 稳定性

| 问题              | 解法                                                                          |
| --------------- | --------------------------------------------------------------------------- |
| **单条线路挂了就黑屏**   | mpv 加 `--keep-open=no`，进程异常退出即视为该线路失败 → 自动切下一条                                   |
| **所有线路都挂了**     | 全部试完仍失败 → **重新抓播放页刷新 token** 再重试一轮（token 会过期，刷新即可续命）                        |
| **暂停 / 音量不生效**  | v1.8 用 `stdin` 写命令，但 mpv 在 `--input-terminal=no` 下不读 stdin → 改为 `--input-ipc-server` 命名管道 + JSON IPC |
| **接口域名被墙 / 挂掉** | `api_get()` 在 `api1 / api / api2.2026016.xyz` 三个同源镜像间自动回退，命中即切，保证抓页与解密同域               |

### 设置页

点顶栏「⚙ 设置」，可改并持久化到 exe 同目录 `settings.json`：

| 项           | 作用                                                     |
| ----------- | ------------------------------------------------------ |
| 接口域名        | 下拉选镜像或自定义域名，附「测试」按钮实时验证可达性                             |
| User-Agent  | 自定义 UA（默认内置 App UA）                                     |
| 播放缓冲档位      | 低延迟 / 均衡 / 抗卡顿 → 映射到 mpv `--cache-secs` 与 `--demuxer-max-bytes` |
| 频道列表缓存      | 秒数（默认 600）                                             |
| mpv 附加参数    | 高级项，原样追加给 mpv                                          |

---

## 官网源：接入 WebViewTV 直播源（v2.0）

### 背景

自建源（`api*.2026016.xyz`）能解出线路，但**可用率不稳定** —— 很多频道点进去解不出流。
「网页电视 / WebViewTV」（`WebViewTV_3.0.1.apk`）的源实测**长期稳定**。

于是把它的源**一步到位兼容进本工程**：桌面端 exe 与安卓 app 都能直接切到「官网源」。

> 上游开源仓库：`github.com/hxh19950701/WebViewTvLive`（v1.6 之后闭源，数据文件仍在更新）。

### 它是怎么工作的（以及为什么不能照搬）

WebViewTV **不是直连直播源**，而是：

```
频道表(904 条)  ──选频道──▶  官方直播网页（tv.cctv.com / yangshipin.cn / …）
                                   │
                              真浏览器打开 + JS 注入找 <video> + 强制全屏
                                   │
                              浏览器自己播（WASM + MSE，blob: 源）
```

关键结论：**官网流地址抓出来也没用**。
CCTV 这类官网用 WASM 软解 + MSE 喂 `blob:`，真实地址是一串随时失效的分片请求；
实测 20 个官网页面里只有 2 个在 HTML 里直接带 `.m3u8`。
所以「让浏览器自己播」是唯一稳的路 —— 本工程在两端各用不同手段落地了这件事。

### 逆向：数据文件的加密方案

`data/4/*.dat` 是加密的。从 APK 反编译（jadx）拿到算法：

```python
# com.vasthread.webviewtv.app.LiveApplication
key = 类名前 32 字符 倒序  →  b"eviL.ppa.vtweivbew.daerhtsav.moc"   # 32 字节 → AES-256
iv  = 类名前 16 字符       →  b"com.vasthread.we"
明文 = AES-256-CBC 解密 → gzip 解压 → JSON
```

| 文件                    | 内容                                            |
| --------------------- | --------------------------------------------- |
| `data/4/server_config.dat` | UA、镜像代理列表（688 KB，含 base64 图片）                   |
| `data/4/full.dat`          | **频道表**：904 个频道 / 1114 条线路 / 51 个分组（21 KB）      |
| `data/4/webpage_adapter.dat` | 官网适配规则 32 条：怎么在页面里找 `video` / 播放按钮 / 前置点击元素  |

Python 侧**纯标准库**自写了 AES（`_expand_key` 支持 128/192/256、`aes_cbc_decrypt`），
不引入 `pycryptodome`，避免给 exe 增重。

JS 侧（安卓）用**同一份算法**重写了一遍 —— 两版用真实 `.dat` 文件做过交叉验证：
解密结果与 Python **逐字节一致**（904 频道 / 32 规则 / config 全对）。

### 三端复用：一份数据，两个实现

| 实现 | 位置 | 谁在用 |
| --- | --- | --- |
| Python | `shared/wvt.py` | Windows / Linux 桌面端 |
| JS | `shared/web/wvt.js` | 安卓 WebView 端（由 `build.sh` 同步到 assets） |
| 内置快照 | `shared/wvt_builtin.json` → `tools/gen_wvt_js.py` → `android/app/assets/wvt_channels.js` | 两端离线兜底 |

下载与兜底是**三重**的：

```
① 本地缓存（24h TTL，wvt_channels.json）
② 联网下载 ← 依次尝试 10+ 个镜像通道，谁先通用谁
③ 内置快照（127 KB，随包发布）—— 断网也能出 904 个频道
```

可用镜像通道（实测排序）：
`ghproxy.net` / `gh-proxy.com` / `ghfast.top` / `gitdl.cn` / `ghproxy.cc` / `dgithub.xyz`
+ `raw.gitmirror.com` / `kkgithub.com`（换 host）
+ `fastly.jsdelivr.net` / `cdn.jsdelivr.net`（jsDelivr gh 通道）。

> `raw.githubusercontent.com` 在境内**直连不通**，所以它排在最后仅作兜底。

### 桌面端：直连走 mpv，网页线路走「WebView2 内嵌」（v2.4）

顶栏新增 **「本机源 / 官网源」** 切换；切到官网源后，左侧分类标签换成**分组下拉**（51 个分组）。

```
选中频道
  ├─ 直连线路（含 .m3u8/.flv/.ts/.mp4，44 条）
  │     → 直接丢给 mpv，带 Referer/UA 请求头
  └─ 网页线路（860 条）
        → embed.py 在播放区里建一个 WebView2 控件（系统组件，Win10/11 自带运行时）
        → AddScriptToExecuteOnDocumentCreated 官方预注入 shared/web/wvt_inject.js
          （与安卓端同一份脚本，导航前就挂好，不怕换页）
        → Navigate 到官方直播页，页面自己播（WASM + MSE）
        → 起播判定 currentTime > 0.2（play 事件 ≠ 起播）
        → 单条线路超时/报错 → 自动换下一条官网线路（多数频道有 3 条）
        → ★ 全部线路失败 → cdp.py 嗅探真实流地址 → 交给 mpv（最后兜底）
```

**v2.3.x 的旧做法**是「启动完整 Edge 进程 `--app=…` → 全屏幕搜它的窗口 → `SetParent`
强改成子窗口」。这是在 hack 一个**没打算被嵌入**的独立程序，于是一步一个坎：
标题栏画在客户区里只能按 DPI 瞎猜、Chromium 按网址记窗口位置跟 `--window-size` 打架、
页面 `resizeTo` 跟尺寸看门狗对抗、换台并发时进程互杀漏出游离浏览器（v2.3.5 根因，详见下节）。

v2.4 换成 **WebView2**：微软官方「嵌进你自己程序」的浏览器控件（Windows 系统组件，
Office / 微信小程序 PC 版都在用）。控件生来就是主窗口的子窗口：
「找窗口 / SetParent / 抢边框」这一类 bug 整体消失；尺寸 `put_Bounds` 精确控制没有对抗；
UA 用 `Settings2.put_UserAgent` 直接换；脚本官方预注入比 CDP 抢时机更稳；
生命周期由主程序管理，没有游离进程可言。

`desktop/embed.py` 是**纯标准库**实现（只用 SDK 的 `WebView2Loader.dll`，微软官方可再
分发、约 190KB、随 exe 打包进 `_MEIPASS` 根；接口序号/GUID 从官方 WebView2.h
（SDK 1.0.4258.31）逐一核对，全部走 COM vtable，无 pythonnet / 无 win32com）：

| 关键点 | 做法 |
| --- | --- |
| **线程模型** | 专职 STA 线程 `CoInitializeEx(APARTMENTTHREADED)` + 消息泵，env/controller/core 全在这条线程创建和调用（别的线程碰 vtable 报 0x802A000C 或段错误）；**泵必须先转起来再 create**，否则环境挂到 loader 内部线程上满屏诡异错误；回调里只做 AddRef + 存指针 + set Event；主线程等结果时边泵消息边等（WebView2 向 parent 线程发同步消息，不泵就死锁）—— 所以后台线程等 Event、主线程跑 Tk mainloop 最稳 |
| **作业与换台** | play/eval 是生成器作业，每步 yield 等待秒数；代次变了立刻作废 —— 控件是大家的，根本没有「进程互杀」这回事 |
| **自动起播** | `--autoplay-policy=no-user-gesture-required` 等参数走官方环境变量通道 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`（与外部传入**合并**而非覆盖）；可用 `WVT_EMBED_ARGS` 环境变量整体覆盖（设为空串 = 不带任何参数，用于诊断） |
| **GPU 沙箱坑** | 部分机器（云桌面/虚拟 GPU）GPU 进程沙箱起不来（exit_code=7 连崩），Chromium「GPU process isn't usable. Goodbye」整个自杀 → 表现为控件全黑。实测 `--disable-gpu-sandbox` 能救（保留硬件加速）；`--disable-gpu` / `--use-angle=swiftshader` 都救不了 |
| **★ Widevine 平台限制** | WebView2 运行时**不捆绑 Widevine CDM**：HTTPS 页面上 EME 探测 clearkey/playready OK、widevine `NotSupportedError`（EME API 只在 HTTPS 安全上下文暴露，about:blank 上测是误导）；外挂 Edge 的 widevinecdm.dll 无效 → CCTV（HTML5_CDN_LIVE_DRM_PLAYER）/央视频等 Widevine DRM 网页线路**平台性无解**，只能靠嗅探兜底走 mpv。广东卫视移动版 m.gdtv.cn 等非 DRM 页面实测正常起播 |
| **DRM 线路兜底** | 网页线路全败 → CDP 嗅探真实流地址（cdp.py）→ 交给 mpv 播；嗅探也失败才报「均不可用」。mpv 请求头注意：`--http-header-fields` 以**逗号**分隔条目，UA 里的 `KHTML, like Gecko` 必须把值里逗号转义成 `\,`，否则 mpv 退出码 2 秒退且零输出 |
| **Tk 段错误根因** | 「Tk 回调里做阻塞等待」会让等待泵的 `DispatchMessage` 派发 Tk 定时器消息 → 嵌套进 Tcl 事件处理 → Tcl 从嵌套上下文调 Python 回调 → `PyEval_RestoreThread: GIL released` 段错误。修法三处：`_ui` 改线程安全队列 + 主线程 `after` 排水（后台线程绝不跨线程 `root.after`）；`_on_close` 只设标志 + destroy，同步 close 挪到 mainloop 退出后的 finally；探针/观测轮询全部挪后台线程 |

**实测**：广东卫视（m.gdtv.cn 移动版）在 WebView2 内嵌 4~6 秒真实起播（截图取证
`state=playing`）；CCTV-2 等非 DRM 线路同理。换台并发无游离窗口（没有进程可游离）。

> 以下是 v2.3.1 → v2.3.5 时代（Edge `SetParent` 方案）的排查记录，v2.4 已整体换掉该实现，
> 但这些坑对任何「强嵌第三方浏览器窗口」的尝试仍有参考价值。

### 桌面端「浏览器怎么又变成一个独立应用」（v2.3.1 → v2.3.2）

用户反馈：**「exe 版本播放视频怎么又打开一个应用，没法像本地源一样在自己这个页面内观看」**。

v2.3 那一轮已经做了 `SetParent` 内嵌，源码自检也 PASS，但**真 exe 上会复发**。
查下来是**三个独立坑**叠在一起，而 v2.3 的 e2e 测试恰好都漏掉了。

#### 坑 1（万恶之源）：`--kiosk` 会**两次**抢全屏，第二次直接把窗口从宿主里摘出去

`play()` 的时序是 `ensure()`（起进程）→ `_connect()`（等 CDP，1~5s）→ `attach()`（这才 SetParent）。

**第一次** —— 启动时无视 `--window-position`（`tools/_probe_kiosk.py` 逐秒抓全屏图）：

```
t=0.5s  rect=(-26214, -26214, -24934, -25494)   离屏，正常
t=1.0s  rect=(0,    0,  1536,  864)              直接变成主屏全屏！
```

**第二次** —— 播放中途（`tools/check_exe_embed.py` 跑真 exe，20 次/秒盯游离窗口）：

```
t=20.21s  hwnd=6c0974  rect=(0, 0, 1920, 1080)
t=20.21s  hwnd=650cbc  rect=(0, 0, 1920, 1079)
```

此刻窗口**早就 SetParent 成功了**（`IsChild` 为真、客户区正常铺在播放区里），
却被 Windows 从宿主中摘出来、重新变回顶层窗口满屏盖住桌面 —— 这就是用户的原话。

**→ 修法：永远不要加 `--kiosk`。** 它唯一的好处（藏掉标题栏）用「剥 `WS_CAPTION` +
客户区对齐」同样拿得到（见坑 2），而代价是窗口随时可能脱离宿主。

#### 坑 2：`--app=` 的标题栏是**真·非客户区**，根本不需要 kiosk

v2.3.1 曾误判成「那条栏是 Chromium 自绘、`WS_CAPTION` 摘不掉、只能靠 kiosk」——
`tools/_probe_appframe.py` 直接推翻：

```
window=1440x740   client=1422x693   非客户区=(左9, 上38, 右9, 下9)
```

上边那 38px 就是标题栏，`WS_CAPTION` 一剥就没。之前"看起来还在"，是因为
`MoveWindow(hwnd, 0, 0, w, h)` 铺的是**窗口**尺寸，那 38px 实打实压在画面顶上。

#### 坑 3：`find_hwnd()` 取 `found[0]` 会挑中 0×0 的隐藏辅助窗口

Chromium 一个进程下同时挂着好几个 `Chrome_WidgetWin_*`：

```
hwnd=c01032  可见=False  rect=(0, 0, 0, 0)          ← 隐藏辅助窗口
hwnd=770b18  可见=True   rect=(0, 0, 1536, 864)     ← 真正的主窗口
hwnd=500198  可见=False  rect=(77, 77, 1229, 669)   ← 离屏占位
```

`EnumWindows` 按 Z 序返回，`found[0]` 完全可能命中那个 0×0 的假窗口。
`SetParent` 到假窗口上的表现就是**播放区一片黑，真窗口还盖在桌面上**。

#### 修法

| 问题 | 修法 |
| --- | --- |
| kiosk 抢全屏 / 把窗口摘出宿主 | **启动参数删掉 `--kiosk`**（根治）；`_hide_guard` 只做兜底 |
| 窗口在被嵌入前露脸 | `_hide_guard` 线程：起进程后每 **20ms** 扫一次，见到可见的顶层窗口就 `SW_HIDE`。去掉 kiosk 后窗口启动就在 `-32000,-32000` **且不可见**，守卫正常一次都不出手 |
| 挑错窗口 | `_score_window()` 打分（面积截断 + 可见 +5000 + 有标题 +3000 + 类名 `Chrome_WidgetWin_1` +1000，面积 ≤1 直接淘汰），取最高分；**并且等到有实际尺寸的窗口出现才返回** |
| `SetParent` 静默失败 | 它的返回值是「原父窗口」不是成功标志 → 必须用 `GetParent()` 复查一遍 |
| 尺寸被改回去 | `_size_watch` 线程每 350ms 复查**客户区**尺寸，不对就 `resize()` 纠正（Chromium 自己会按 Preferences 里的 `window_placement` 重算边界，它并不知道自己已经是子窗口了） |
| 标题栏占画面 | `client_insets()` + `fit_child_to()` —— 不跟边框较劲，把窗口左上角挪到 `(-9,-38)`、尺寸加到 `(w+18, h+47)`，**让「客户区」去对齐宿主区**，超出部分靠父窗口的 `WS_CLIPCHILDREN` 裁掉（`cover_child_windows` 也走这条） |

#### 自检（`tools/check_wvt_embed.py` 第 9 步 + `tools/check_exe_embed.py`）

- **游离窗口监视**：从开播前起，每 50ms 采一次所有 `Chrome_WidgetWin_*`，
  只要「可见 + 与虚拟屏幕的交集 ≥ 100×100 + 不是播放区的子窗口」就记账 → 必须为空。
- **暴露时长量化**：守卫每次拦截都记 `poll`（该窗口连续第几轮被看到可见）。
  因为我们看到就立刻隐藏，正常永远是 1 ⟺ **暴露 ≤ 一个轮询周期（20ms，约一帧）**。
  `>1` 说明 `SW_HIDE` 没压住 Chromium，直接判失败。
- **几何比对改成比客户区**（`client_rect_on_screen()`）：决定画面大小的是客户区，
  窗口矩形因为「左上角挪出去」反而比播放区大，拿窗口矩形比会误判。
- 测量前先 `settle()`（轮询到矩形连续两次不变，最多 1.5s）——
  全屏切换后 `_fit_children` 是 `after(120ms)` 才跑的，直接量会抓到过渡值（实测抓到过 199×34）。
- `check_exe_embed.py` 跑**真 exe**、模拟点「官网源」+ 频道，全程 20 次/秒盯游离窗口
  （注意：本机 125% 缩放，测试进程必须 `SetProcessDPIAware()`，否则 `ImageGrab` 的物理像素
  与虚拟化坐标差 25%，点击会点歪）。

**最终实测**（`check_wvt_embed.py` → RESULT PASS）：

```
== 4) 内嵌窗口几何（窗口态）==   差 [0, 0, 0, 0]   [OK] 是 video_host 的子窗口
== 5) 切全屏 ==                 差 [0, 0, 0, 0]   [OK] 全屏后画面仍铺满
== 6) 退出全屏 ==               差 [0, 0, 0, 0]   [OK] 退出全屏后画面仍铺满
== 9) 浏览器有没有自己跑出来变成一个独立应用 ==
  全程没抓到（采样 12 次/秒，从开播前就开始盯）
  [OK] 全程浏览器没有游离在播放区之外
  隐身守卫拦截 1 次（轮询 20 ms）：t=4.47s 可见轮数=1  ← 最大暴露 20 ms，不到一帧
  [OK] 浏览器进程 13840 已随应用退出（不残留）
```

#### 退路：`cdp.py` 嗅探（没装 Edge/Chrome 的机器）

`desktop/cdp.py` 是**纯标准库**实现（自带最小 WebSocket 客户端，RFC6455），
在 `_show_web()` 发现 `find_browser()` 拿不到浏览器时启用：

| 关键点 | 做法 |
| --- | --- |
| 启动浏览器 | Edge / Chrome，`--remote-debugging-port` + 离屏窗口 |
| **离屏窗口不起播** | Chromium 遮挡检测在作祟 → 加 `--disable-backgrounding-occluded-windows` 等 4 个开关 + `--disable-features=…,CalculateNativeWinOcclusion` |
| 抓到埋点 URL | `p.data.cctv.com/play.1.3` 这类要排除 → `media_score()` 打分（m3u8=100 / mpegurl=95 / mp4=60 / ts=20）+ 埋点域名黑名单，取最高分而非第一个命中 |
| 冷启动慢 | 浏览器实例**复用**：首次 40~85s → 之后 **1~4s** |
| 进程残留 | Chromium 多进程，必须 `taskkill /F /T /PID` 杀整棵树 |

**实测**：CCTV-1 综合 → 7.9 秒解析出 `https://ldncctvwbcdbd.a.bdydns.com/ldncctvwbcd/cdrmldcctv1_1/index.m3u8?b=200-2100`，mpv 正常解码。

> 这条路只在「网页线路」上跑，且解析出来的多半是 `blob:` 或一次性分片地址，
> 可用率远不如内嵌浏览器 —— 所以它默认不启用，只做保底。

### 桌面端全屏修复（v2.3）

用户反馈「有时候不能全屏、缺画面」。根因是**两个 Win32 子窗口都不跟随父窗口 resize**：

| 现象 | 根因 | 修法 |
| --- | --- | --- |
| 全屏后画面只占原来那么大，右下角缺一块 | `mpv --wid` 窗口是 Win32 子窗口，父窗口变大它**不会**自动跟随 | `cover_child_windows(host)`：枚举宿主下所有**非 Tk** 子窗口 `MoveWindow` 铺满；绑到 `video_host` 的 `<Configure>` |
| 全屏按钮没反应 / 要按好几次 | 播放区上同时绑了单击和双击，点一下进全屏、双击连切三次，状态全乱 | 只保留 `<Double-Button-1>` |
| 内嵌浏览器抢焦点后 F11 / Esc 失灵 | Chromium 子窗口拿到键盘焦点，Tk 的 `<Key>` 绑定收不到 | `GetAsyncKeyState` 轮询（140ms）兜 F11(0x7A) / Esc(0x1B)，用 `GetWindowThreadProcessId` 判「前台窗口是不是本进程的」 |
| 全屏后控制条还在、不是真沉浸 | 只 `attributes("-fullscreen", True)`，没收起四周 UI | `_set_fullscreen()` 统一 `pack_forget()` 顶栏/控制条/左侧列表，并记住 EPG 原本是否展开，退出时原样恢复；全屏期间留一个置顶小浮层（频道名 + 暂停 + 退出全屏） |

### 桌面端样式统一：手绘胶囊条 `PillBar`（v2.3）

用户反馈「本机源/官网源 子标签切换风格不一致」。
原因是两处用的是**两套不同控件**：源切换是「深色 Frame 里包两个 ttk Button 的分段控件」，
分类标签是三列平铺的原生 `tk.Button` —— 圆角、内距、字号、描边、选中态全对不上，
而且原生 Button 的外观还会随 Windows 主题变。

现在两处**共用同一个 `PillBar(tk.Canvas)`**：

- 手绘 `_round()` 圆角矩形（`create_polygon(..., smooth=True)`）；
- 选中态用 `_grad_round()` 逐行画 `BRAND2 → BRAND` **垂直渐变**（原生按钮做不出渐变）；
- 未选中 `BG2`，hover `BG3` + 文字提亮；行数由 `cols` 自动算高度；
- 顶栏源切换 `cols=2`、左侧分类 `cols=3`，只是参数不同，视觉天然一致。

---

### 安卓端：直连走原生硬解，网页线路走「覆盖层 WebView + 注入」

顶栏与桌面端一样是 **「本机源 / 官网源」切换**；两套频道表结构完全不同，各自记住看到哪儿：

```
本机源 ── 分类标签：⭐收藏 / 综合 / 体育 / 央视 / 卫视 / 港澳台 …   → 走原接口
官网源 ── 分组 chip 条：央视 / 央视数字 / 国家 / 卫视 … (51 个)     → 走内置快照
           ↑ 切源时整行替换，频道网格跟着换
```

- 官网源下搜索框**跨全部分组**（904 个频道里按名字捞），不受当前分组限制；
  本机源的收藏夹是跨源的 —— 在官网源收藏的频道，切回本机源也能看到并点开。
- 布局用 `body{display:flex;flex-direction:column}` + `#scroller{flex:1}`，
  顶栏里再加减东西都不用调 `calc(100% - Npx)` 的魔法数字。
- 遥控器方向键按「源切换 → 分类/分组 → 频道网格」四行结构上下走；
  `listOf()` 会过滤掉不可见的元素，所以隐藏的那一行自动被跳过。
- **源切换条与分类胶囊用同一套外观（v2.3）**：`#srcbar` 从「底槽 + 内嵌小按钮」的
  分段控件退化成纯排列容器，正式样式全交给 `.srcbtn`，并与 `.tab` 逐条对齐
  （同样的 `border-radius:999px` / `padding:8px 16px` / `1px solid var(--line)` /
  选中 `--brand-grad` + 同款投影）。两处的 `:focus-visible` 焦点环也一起加上了。

安卓不方便起第二个浏览器进程，直接复用平台能力：

```
选中频道
  ├─ 直连线路 → 走原有链路（ExoPlayer 原生硬解 → mpegts.js 软解兜底）
  └─ 网页线路 → MainActivity 起一个全屏覆盖层 WebView
                  加载官方页 → 注入 assets/wvt_inject.js
                  → 按 webpage_adapter.dat 的规则找 <video>
                  → 解开祖先链上的裁切/隐藏/层叠上下文（unclip）
                  → 把 <video> 自己钉成 position:fixed 全屏 + z-index 最大（pinVideo）
                  → 通用去浮层：elementsFromPoint 探测，谁盖在 video 上就收谁
                  → 自动 play() + 点播放按钮兜底
                  → 真的出画面（currentTime > 0）才回调原生：切横屏 + 收加载遮罩
                  → 12s 空转 / 20s 没画面 → 判定本线路不可用 → 自动换下一条官网线路
                  → 设备返回键 / 页面左上角 ✕ → 销毁覆盖层，回到频道列表
```

分组切换用横向 chip 条（51 个分组）。

**全屏适配实机回归（v2.3，雷电 SM-N9700 / emulator-5554）** —— 三种完全不同的官网平台逐像素比对：

| 频道 | 官网平台 | `<video>` 实测矩形 | 视口 `visualViewport` | 结果 |
|------|----------|--------------------|------------------------|------|
| CCTV-13 新闻 | tv.cctv.com | `[0,0,640,360]` | `[640,360]` | 精确相等 |
| 广东卫视 | m.gdtv.cn | `[0,0,640,360]` | `[640,360]` | 精确相等 |
| 江苏卫视 | live.jstv.com | `[0,0,640,360]` | `[640,360]` | 精确相等 |
| 湖南卫视 | yangshipin.cn（央视频） | `[0,0,640,360]` | `[640,360]` | 精确相等 |

四个都 `readyState=4`、`currentTime` 正常递增、页面无 `#error_msg_player`。
（dpr=2 → 物理 1280×720，正好等于屏幕。）复现命令：

```bash
WAIT=24 bash tools/check_wvt_android.sh "广东卫视" "江苏卫视" "湖南卫视"
```

#### 四个踩过的坑（改这块代码前务必先读）

1. **不要把 `<video>` 搬出原来的 DOM 子树。**
   CCTV 这类站点用 MSE 挂 `blob:` 源，元素一搬家播放就断（`readyState` 掉回 0 且不再拉流），
   而站点的播放器逻辑还在原地等元素 —— 结果就是永远黑屏 / 停在站点的播放按钮浮层上。
   正确做法是**让元素留在原地**，把它自己钉成 `position:fixed` 全屏 + 最大 z-index。
   `wvt_inject.js` 里仍保留一个自建容器 `#__wvt_host`，但只在「原地固定确实没生效」
   （祖先里还有 transform/filter 之类别的东西把 fixed 拽回局部坐标系）时才动用。
2. **别用同一套「清祖先样式」的逻辑顺手清到自己的容器头上。**
   早期版本把 video 搬进 `#__wvt_host`，然后 `unclip()` 沿祖先链设 `position:static` /
   `z-index:auto` —— 宿主自己就是 video 的祖先，于是被自己清成了普通流内元素，
   全屏失效、站点的播放浮层照样压在上面。现在 `unclip()` 遇到 `#__wvt_host` 立即停止，
   并且 `hostBox()` 每次调用都重刷样式（不只在创建时设一次）。
3. **`play()` 事件 ≠ 起播。**
   `play` 事件只是「play() 被调用」，此时可能一个字节都还没拉到。
   必须用 `currentTime > 0.2` 或者 `playing` 事件才判定为真的在播，
   否则加载遮罩会提前收掉，用户看到的是站点自己的「大播放按钮」浮层。
   同理，**站点自己弹的错误提示也是判据**：CCTV-1 在部分时段会被
   「由于播出安排变更，暂不支持播放该时段内容」挡住，这不是我们的 bug，
   而是应该换下一条线路 —— 所以有了自动换线。
4. **尺寸别信任「布局视口」，也别信任 `100%` / `100vw`。**（v2.3）
   安卓 WebView 开着 `setUseWideViewPort(true)`，官方页又没有 meta viewport 时，
   布局视口是 **980px**、真实可视区域只有几百 px；页面若还在 `<html>` 上设了
   `zoom:2`（CCTV 系就有），`position:fixed` 的层也跟着被放大 2 倍 ——
   结果就是用户反馈的「**全屏很大、显示不全**」。现在：
   - `viewSize()` 优先用 **`window.visualViewport.width/height`**（`position:fixed`
     唯一正确的参照系），退化到 `innerWidth/Height`，最后才是 `clientWidth`；
   - `killZoom()` 每轮用 `getComputedStyle` 复查 `<html>` / `<body>` 的 `zoom`，
     非 1 就用行内 `!important` 压回去（**只在样式表里设的 zoom，行内样式看不出来**）；
   - `fitBox()` 每轮拿实测 `getBoundingClientRect()` 与视口比对，超 12% 按比例缩回、
     不足 88% 撑满 —— 兜住任何想不到的坐标系；
   - `window.resize` / `orientationchange` / `visualViewport.resize` 都重钉一次，
     否则旋屏后画面停在旧尺寸（看起来同样是「显示不全」）。
   桌面端同理：`MoveWindow` 用的是物理像素、而 Tk 的 `winfo_width()` 在 125% DPI 缩放下
   是逻辑像素，所以浏览器窗口尺寸一律取自 `GetClientRect(host)`，不要从 Tk 侧换算。

#### 自动换线与失败上报

```
wvt_inject.js                     MainActivity                     player.html
  判定本线路起不来  ──►  NativeTV.pageFailed()  ──►  window.__onWebPageFailed()
                                                          │
                                   有下一条线路 ──► playIdx(idx+1) → 同一个 WebView 换 URL
                                   没有下一条   ──► closePage() → 回频道列表 + toast 提示
```

网页侧用一个 `webState` 状态机串起这几条路径：`0` 空闲 / `1` 播放中 / `2` 正在自动切线路 /
`3` 官网线路全部失败。切线路过程中原生回的 `__onWebPageClosed` 会被 `2` 挡掉，不会误重置界面。

- `closePage()` 是**延后 150ms** 才真拆 WebView 的，期间若已经开了新的一页，
  靠 `pageSeq` 代次守卫放弃拆除 —— 换线路时不会误杀刚建好的页面。
- 用户按返回键 / 点左上角 ✕ 从官方页面退出时，**一并关掉播放层回到频道列表**。
  不能停在播放层：那会儿本机 `<video>` 是没有源的，留在那里只有一个「大播放按钮」占位画面。
- 实测：CCTV-1 第 1 条 `tv.cctv.com` 被站点限制 → 第 2 条 `yangshipin.cn` 正常起播（约 22s 内完成切换）。

#### 调试这套东西的手法

安卓 WebView 开着 `setWebContentsDebuggingEnabled(true)`，可以直接用 CDP 在页面里求值：

```bash
# tools/adev.sh 把「adb 转发 → 取 /json/list → CDP 求值」压在一次调用里完成
# （本机 bash 调用结束后 adb server 会被回收，forward 随之失效，必须挤在一次调用内）
bash tools/adev.sh "(function(){return location.href;})()"           # 默认取我们自己的 player.html
bash tools/adev.sh "(function(){var v=document.getElementsByTagName('video')[0];return {rs:v.readyState,ct:v.currentTime};})()" @overlay
bash tools/adev.sh "NativeTV.closePage()"

# 批量冒烟：逐个频道打开官网页并回报起播情况
bash tools/check_wvt_android.sh "广东卫视" "江苏卫视" "四川卫视"

# 注入脚本的日志会落到 logcat
android/tools/platform-tools/adb.exe logcat -s CleanTV
```

`tools/adev.sh` 会自动从 `/proc/net/unix` 里抓 `webview_devtools_remote_<pid>` 后缀 ——
**不要**用 `adb shell pidof <包名>` 的结果去猜这个后缀，那是 WebView 沙箱进程号，
和主进程号不是一回事，转发会建出一个空端口。


### 数据规模（2026-10 快照）

| 项 | 数值 |
| --- | --- |
| 频道 | 904 |
| 线路 | 1114（直连 44 / 网页 860，按频道首条线路统计） |
| 分组 | 51（央视 / 央视数字 / 国家 / 卫视 / 少儿 / 超清体验 / …） |
| 适配规则 | 32 |

### 维护：源变了怎么办

快照不是死的，两级都会自动更新：

- **运行时**：24h 缓存过期 → 自动联网拉最新 `full.dat` 解密；拉不到就用快照
- **换包时**：刷新打进安装包的快照

```bash
# 桌面 exe 的内置快照
python tools/gen_wvt_snapshot.py      # → shared/wvt_builtin.json

# 安卓的内置快照（从上面那份生成，不需要联网）
python tools/gen_wvt_js.py            # → android/app/assets/wvt_channels.js
```

### 局限

- 网页线路的**起播依赖真实浏览器**，比直连慢（桌面首次嗅探 8~30s，之后 1~4s；安卓是直接开页面，几秒）
- 官网页面改版 → 适配规则可能失效（上游 `webpage_adapter.dat` 会跟着更新）
- 部分官网对 UA / 地域有要求，境外 IP 可能打不开
- 嗅探到的流带时效签名，**不能分享链接**，只能本机自己播

---

## 手机全屏与手势（v1.3）

### 为什么必须原生接管

Android WebView 的 `requestFullscreen()` **只把网页铺满 View，不会改变屏幕朝向**。  
手机竖屏点「全屏」后，视频仍是一个 16:9 的横向条，上下大片留白 —— 这正是「点了全屏还是很小」的根因。

v1.3 改为**原生 + 网页双层协作**：

```
网页「全屏」按钮 / 双击画面
        ↓  window.NativeTV.setLandscape(true)
MainActivity.applyOrientation()  →  setRequestedOrientation(SENSOR_LANDSCAPE)
        ↓  同时给页面加 .fs 类（视频容器改为 absolute inset:0 铺满）
真正横屏 + 全屏铺满；退出时反向执行，回到竖屏频道列表
```

**关键改动**

| 位置                  | 改动                                                                                                                                                                                  |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MainActivity.java` | 新增 `NativeBridge`（`@JavascriptInterface`）：`setLandscape` / `setImmersive` / `isLandscape`；`applyOrientation()` 用 `SENSOR_LANDSCAPE` 支持左右翻转；沉浸式系统栏；`onConfigurationChanged` 回调网页触发重排 |

| `AndroidManifest.xml` | `screenOrientation`、`resizeableActivity`、`configChanges` 补 `navigation\|density`、主题改 `NoTitleBar.Fullscreen` |  
| `player.html` | `.fs` 全屏样式（视频 `position:absolute;inset:0`，控制条改浮层）；`setFsMode()` / `isFsMode()`；`bindTouchGestures()` |

### 交互清单

| 操作    | 安卓手机                | 安卓 TV / 盒子             | 桌面                |
| ----- | ------------------- | ---------------------- | ----------------- |
| 进入播放  | **自动转横屏全屏**         | 本身就是横屏                 | 铺满窗口              |
| 全屏开关  | 点「全屏」按钮 / **双击画面**  | 同左                     | 按钮 / `F` 键 / 双击画面 |
| 唤出控制条 | 单击画面（6 秒自动隐藏）       | 按方向键                   | 鼠标移动              |
| 换台    | —                   | **频道 +/- 键**           | 数字键 / 方向键         |
| 直选频道  | —                   | **数字键**（1.2 秒无后续数字即跳转） | 数字键               |
| 退出播放  | 返回键 / 「返回」按钮（自动回竖屏） | 返回键                    | `Esc`             |

> 关于双击：判定只依赖 `click` 事件。实测 Android WebView 在 `<video>` 上层派发的  
> `touch*` 事件 `clientX` 常为 `NaN`、`target` 还会串到控制条上，直接用 touch 会误判。

### 已知边界

- 部分定制 ROM 会忽略 `setRequestedOrientation`（多见于厂商锁定了系统旋转）。  
  此时全屏按钮仍有效，只是不转屏；可尝试在系统设置里允许「自动旋转」。
- 模拟器的 `adb shell input tap` 在横屏 WebView 内不派发 click，属**模拟器限制**，  
  真机正常。验证手势请在真机或改用 CDP 派发事件。
- 模拟器/部分设备 `navigator.deviceMemory` 不可用，档次判定会保守落到 `mid`。

---

## 待扩展

### 已完成（本版落地）

- ✅ **手机全屏自动横屏 + 沉浸式**（原生接管，见上节）
- ✅ **双击画面切全屏**（三端一致）
- ✅ **遥控器数字键直选频道**（1.2 秒缓冲，避免两位数频道误跳）
- ✅ **遥控器 / 键盘 频道+/- 切台**
- ✅ **单击画面唤出控制条**，6 秒无操作自动隐藏
- ✅ **原生硬解播放（ExoPlayer / media3）**——MediaCodec 硬解根治 JS 软解卡顿，JS 软解保留兜底（见上节「原生硬解播放」）
- ✅ **Android 4.4 兼容**——minSdk 降到 19，老 ExoPlayer2 后端（API 16+）双轨硬解（见上节「Android 4.4 兼容」）
- ✅ **桌面端断流重连 + 硬解探测**——软解兜底补齐「断流重连 ×2 再切线」，检测 mpv/ffplay/vlc 并提示升级路径
- ✅ **桌面端原生一体化窗口（Windows / Linux）**——tkinter 窗口 + mpv `--wid` 内嵌，mpv 随 exe 捆绑免安装（见上节 v1.8）
- ✅ **桌面端界面增强**——台标 / EPG 面板 / 画面比例 / 音量 / 搜索 / 快捷键（见上节 v1.9）
- ✅ **接口镜像回退**——`api1 / api / api2.2026016.xyz` 三域名自动切换，抓页与解密同域
- ✅ **设置页 + 持久化**——接口域名 / UA / 缓冲档位 / 缓存 TTL / mpv 附加参数，存 `settings.json`
- ✅ **桌面端稳定性**——mpv 异常退出自动切线；全部线路失败重新抓页刷新 token 重试；IPC 改用命名管道（修复暂停/音量失效）
- ✅ **WebViewTV 官网源接入（v2.0）**——904 频道 / 1114 线路 / 51 分组；AES-256-CBC 纯标准库解密（Python + JS 双实现，逐字节对齐）；镜像回退 + 24h 缓存 + 内置快照三重兜底；桌面端直连走 mpv、网页线路走 CDP 嗅探；安卓端覆盖层 WebView + JS 注入自己播
- ✅ **官网源网页线路修实（v2.1，安卓端）**——`<video>` 原地钉全屏（不搬 DOM，否则打断 MSE）、通用去浮层、起播改用 `currentTime` 判定、**线路起不来自动换下一条官网线路**、从官方页退出直接回频道列表。
- ✅ **安卓端数据源分流（v2.2）**——顶栏「本机源 / 官网源」分段切换（对齐桌面端），两套频道表各自记住浏览位置、收藏跨源可见；官网源搜索跨全部分组。
- ✅ **桌面端官网源「内嵌浏览器」（v2.3 → v2.3.2）**——`embed.py` 用 Win32 `SetParent` 把 Edge/Chrome 窗口嵌进播放区，不再多出一个浏览器窗口/进程；**启动参数不用 `--kiosk`**（它会启动时+播放中途两次抢全屏、把已嵌好的窗口从宿主里摘出去），标题栏改用「剥 `WS_CAPTION` + 客户区对齐」消掉；`_score_window` 挑对真窗口、`GetParent` 复查 `SetParent`、`_size_watch` 盯客户区尺寸、`_hide_guard` 兜底；`Browser.close` + `exit_type=Normal` + `atexit` 兜底，消灭「还原页面」弹窗与进程残留；`shared/web/wvt_inject.js` 提升为**两端共用同一份**；没装浏览器时退回 `cdp.py` 嗅探。
- ✅ **桌面端全屏修复（v2.3）**——`cover_child_windows()` 让 mpv/Chromium 这些 Win32 子窗口跟随父窗口 resize（「缺画面」的根因）；只保留双击进全屏（原来单击+双击同时绑，状态全乱）；`GetAsyncKeyState` 轮询兜住内嵌浏览器抢焦点后的 F11/Esc；全屏统一收起顶栏/控制条/列表 + 置顶小浮层。
- ✅ **「本机源/官网源」与分类标签样式统一（v2.3）**——两处共用同一个手绘 `PillBar(tk.Canvas)`：圆角 + `BRAND2→BRAND` 垂直渐变选中态 + hover 提亮，不再受 Windows 主题影响。
- ✅ **官网源全屏「很大、显示不全」修复（v2.3，两端）**——注入脚本改用 `visualViewport` 作尺寸基准、`killZoom()` 压掉 `<html>/<body>` 的 `zoom`（CCTV 系设了 `zoom:2`）、`fitBox()` 实测校正、resize/orientationchange 重钉；安卓 `pageView` 另加 `setTextZoom(100)` 防系统字体缩放撑大布局。
- ✅ **安卓端源切换条样式对齐分类胶囊（v2.3）**——`#srcbar` 退化为纯容器，`.srcbtn` 与 `.tab` 逐条对齐（圆角/内距/字号/描边/选中渐变/焦点环）。
  实测（模拟器 SM-N9700）：CCTV-13 / CCTV-1（自动切到央视频线路）/ 广东卫视 / 江苏卫视 / 四川·北京·湖南·湖北 卫视，`readyState` 均为 4、视频铺满整屏（`rect` = viewport）

### 近期可做（改造点已明确）

**1. 回看播放**  
回看页与直播页是**两套密文**，但明文结构同样固定，可复用「用第 1 条反推 XOR key」的思路。

- 改造点：`shared/core.py` 增加 `fetch_replay()`；`shared/web/tv_core.js` 同步 `fetchReplay()`
- 前端：EPG 每条目加「回看」按钮，点了进播放层并标注「回看模式」
- 风险：回看线路可能是 MP4/HLS；HLS 已随 media3-exoplayer-hls 编入原生层可直接用，软解兜底需按 `type=` 分支

**2. 手势增强（安卓端）**

- 上/下滑调亮度与音量（`NativeBridge` 已有 `setBrightness` / `setVolume`，接入即可）
- 横向滑动快进快退（直播场景可改为「切台」）
- 长按画面唤出「锁定 / 画面比例」菜单

**3. 画面比例切换（安卓端）**  
桌面端已四档（适应/铺满/16:9/4:3）；安卓侧可用 `TextureView` 矩阵变换实现，  
存 `localStorage`，与桌面端同名选项对齐。

**4. 开机自启与常驻**

- 安卓：`BOOT_COMPLETED` 广播 + `BOOT_COMPLETED` 权限；TV 盒子场景很实用
- Windows：可选「发送到启动目录」的开关（不建议默认开，以免用户反感）

### 中长期

- **台标补全**：`tools/gen_logos.py` 已能批量抓取，可扩到各地方台；当前 1800+ 条
- **Linux 端 TV 大屏模式**：界面已支持宽度自适应，加 `?tv=1` 即可强制；还缺遥控器按键映射
- **多源聚合**：同一频道合并多个上游，主源失效自动切备源（现在的「多线路」是同一源的多 CDN）
- **EPG 增强**：当前只解析「时间 + 节目名」，可补进度条、剩余时间、点进即回看
- **设置页（安卓端）**：桌面端已落地，安卓侧可把同组字段放进 `localStorage` 设置面板
- **精简打包**：安卓 APK 可加 `arm64-v8a` 白名单、去掉未用资源；桌面端 PyInstaller 可裁 stdlib

> 源码中暂无 `TODO` 占位；以上改造点均已在现有结构上预留了接入位（  
> 例如 `playIdx()` 已按 `TIER` 分支、`NativeBridge` 已可继续扩展方法）。

---

## 免责声明

仅用于个人学习与自有设备上的技术研究。接口与内容版权归原服务方所有，  
请勿用于商业用途或传播。
