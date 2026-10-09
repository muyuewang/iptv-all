# 纯净电视直播 · 三端合一工程

无广告、无人机验证、无激励视频门的电视直播客户端。直接调用原 App 的后端接口，**自主解密**线路播放。

一份业务核心（`shared/`），三个端复用：

| 端                     | 目录         | 产物                                  | 状态                       |
| --------------------- | ---------- | ----------------------------------- | ------------------------ |
| **安卓手机 + Android TV** | `android/` | `android/纯净电视.apk`（约 120 KB）        | ✅ 实测构建通过，已装真机验证（含 TV 播放） |
| **Windows 绿色版**       | `desktop/` | `desktop/dist/电视直播.exe`（约 8.8 MB）   | ✅ 实测端到端通过（新图标已嵌入）        |
| **Linux**             | `linux/`   | `linux/dist/纯净电视`（单文件）+ 可选 AppImage | ✅ 脚本就绪，`run.sh` 免编译直跑    |

> 手机与 TV 用**同一个 APK**，自动识别大屏进入 10-foot 遥控布局。  
> 三端共用同一套设计令牌（Design Token），观感统一、符合当下主流审美，详见下方「视觉设计」。

---

## 目录结构

```
tv_all/
├── README.md                    本文件（三端总览）
├── shared/                      ★ 共享业务核心 —— 唯一真源
│   ├── core.py                  Python 版（Windows / Linux 桌面端用）
│   ├── README.md                两张核心的同步对照表
│   └── web/
│       └── tv_core.js           JS 版（安卓 WebView 端用）
│
├── android/                     安卓端（手机 + TV）
│   ├── build.sh                 一键构建（无需 Gradle/Android Studio）
│   ├── ks.jks                   签名密钥（自签名）
│   ├── tools/                   构建工具链（aapt2/d8/android.jar，体积大，见下）
│   └── app/
│       ├── AndroidManifest.xml  含 LEANBACK_LAUNCHER + TV Banner
│       ├── src/com/clean/tv/MainActivity.java   WebView 壳（设 UA / 全屏 / 返回键）
│       ├── assets/
│       │   ├── player.html      界面 + 遥控导航（业务逻辑全部来自 tv_core.js）
│       │   ├── tv_core.js       ← 由 build.sh 从 ../shared/web/ 自动同步
│       │   ├── logos.js         1800+ 台标映射
│       │   └── mpegts.js        FLV 直播播放库
│       └── res/                 图标与 TV Banner
│
├── desktop/                     Windows / Linux 共用桌面端
│   ├── tv_app.py                本地服务 + 平台适配（一个文件两端通用）
│   ├── build_windows.bat        Windows 打包（→ 单文件 exe）
│   ├── assets/
│   │   ├── index.html           界面（频道网格 / 播放器 / EPG / 收藏）
│   │   ├── logos.js
│   │   └── mpegts.js
│   ├── app.ico                  应用图标（电视造型，gen_icon.py 生成，含 7 个尺寸）
│   ├── refresh_icon_cache.bat   换图标后刷新 Windows 图标缓存（管理员运行）
│   └── gen/gen_icon.py          生成图标（相对路径，任意目录可跑）
│
└── linux/                       Linux 专属入口
    ├── run.sh                   一键运行（源码方式，零编译）
    ├── build_linux.sh           打包单文件 / AppImage
    └── install.sh               装进应用菜单（可选）
```

---

## 快速开始

### 安卓（手机 / TV / 盒子）

```bash
cd android
bash build.sh          # 产出 纯净电视.apk
```

安装后：TV 上出现在 Android TV 主屏；遥控器 **方向键**移动焦点、**OK** 播放、**返回**退出播放。

### Windows

```bat
cd desktop
build_windows.bat      :: 产出 dist\电视直播.exe
```

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
| Windows 运行 | 系统自带 Microsoft Edge（Win10/11）或 Chrome                   |
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
伪装 UA 抓取 + 解密 ─┼─ Windows：本地服务(tv_app.py) → Edge/Chrome --app 无边框窗口
                    └─ Linux：  本地服务(tv_app.py) → Chrome/Chromium 应用窗口
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
这是因为桌面端为了让窗口独立（不带地址栏、不污染你日常用的浏览器），给 Edge/Chrome  
指定了专属配置目录 `--user-data-dir`。浏览器把全新目录当成「新设备」，于是触发  
同步 / 自动登录引导。程序已在启动前写入一份 `Preferences`（关闭 `sync`、`signin`  
与首次运行引导）并附上 `--disable-sync` 等开关，正常不会再现。若某版本浏览器仍弹：  
在弹出窗口里点「不是现在 / 不用了」即可，**不影响播放**；或删掉 exe 同目录的  
`.browser` 文件夹后重启程序。

**Q：收藏存在哪？**

- 安卓：WebView localStorage
- Windows：exe 同目录 `fav.json`（拷贝程序时一起带走）
- Linux：程序目录 `fav.json`

**Q：Linux 上没弹出独立窗口？**  
说明没装 Chromium 系浏览器，程序会退回默认浏览器打开标签页（功能正常，只是外观差些）。  
装一个即可：`sudo apt install chromium-browser`。

---

## 版本纪要

| 版本   | 要点                                           |
| ---- | -------------------------------------------- |
| v1.0 | 三端骨架：伪装 UA + 线路解密 + WebView 壳                |
| v1.1 | 设计令牌统一、图标体系、TV 10-foot 遥控布局                  |
| v1.2 | **播放优化**：码率感知选路、设备分级缓冲、WebView 硬解、卡顿自愈       |
| v1.3 | **手机全屏自动横屏**、双击手势、遥控数字键直选、频道加减台、桌面双击全屏（详见下节） |

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

### 近期可做（改造点已明确）

**1. 回看播放**  
回看页与直播页是**两套密文**，但明文结构同样固定，可复用「用第 1 条反推 XOR key」的思路。

- 改造点：`shared/core.py` 增加 `fetch_replay()`；`shared/web/tv_core.js` 同步 `fetchReplay()`
- 前端：EPG 每条目加「回看」按钮，点了进播放层并标注「回看模式」
- 风险：回看线路可能是 MP4/HLS，`mpegts.js` 不适用，需按 `type=` 分支播放器

**2. 硬解播放通道（彻底追平原版流畅度）**  
当前是 `mpegts.js` 软解，天花板受 CPU 限制。要根治需原生播放器：

- 方案 A：接入 **ExoPlayer**（`media3`），用 `addJavascriptInterface` 暴露 `play(url)`，  
  WebView 只做 UI，解码走 ExoPlayer 硬解。改动量中等，效果最好。
- 方案 B：`<video>` 直连（若源可直出 HLS/MP4）。需服务端配合，暂不可行。
- 方案 C：保持软解，但低端机强制走 720p 以下线路（已在 `pickBestLine` 中预留评分权重）。

**3. 手势增强**

- 上/下滑调亮度与音量（需 `NativeBridge` 增加 `setBrightness` / `setVolume`）
- 横向滑动快进快退（直播场景可改为「切台」）
- 长按画面唤出「锁定 / 画面比例」菜单

**4. 画面比例切换**  
`object-fit: contain` 目前写死。可加 `contain / cover / fill` 三态切换，  
存 `localStorage`，适配非 16:9 的老节目源。

**5. 开机自启与常驻**

- 安卓：`BOOT_COMPLETED` 广播 + `BOOT_COMPLETED` 权限；TV 盒子场景很实用
- Windows：可选「发送到启动目录」的开关（不建议默认开，以免用户反感）

### 中长期

- **台标补全**：`tools/gen_logos.py` 已能批量抓取，可扩到各地方台；当前 1800+ 条
- **Linux 端 TV 大屏模式**：界面已支持宽度自适应，加 `?tv=1` 即可强制；还缺遥控器按键映射
- **多源聚合**：同一频道合并多个上游，主源失效自动切备源（现在的「多线路」是同一源的多 CDN）
- **EPG 增强**：当前只解析「时间 + 节目名」，可补进度条、剩余时间、点进即回看
- **设置页**：缓冲档位手动指定、UA 自定义、接口域名自定义（应对换域名）
- **精简打包**：安卓 APK 可加 `arm64-v8a` 白名单、去掉未用资源；桌面端 PyInstaller 可裁 stdlib

> 源码中暂无 `TODO` 占位；以上改造点均已在现有结构上预留了接入位（  
> 例如 `playIdx()` 已按 `TIER` 分支、`NativeBridge` 已可继续扩展方法）。

---

## 免责声明

仅用于个人学习与自有设备上的技术研究。接口与内容版权归原服务方所有，  
请勿用于商业用途或传播。
