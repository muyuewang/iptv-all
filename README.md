# 纯净电视直播 · 三端合一工程

无广告、无人机验证、无激励视频门的电视直播客户端。直接调用原 App 的后端接口，**自主解密**线路播放。

一份业务核心（`shared/`），三个端复用：

| 端 | 目录 | 产物 | 状态 |
|---|---|---|---|
| **安卓手机 + Android TV** | `android/` | `android/纯净电视.apk`（约 120 KB） | ✅ 实测构建通过，已装真机验证（含 TV 播放） |
| **Windows 绿色版** | `desktop/` | `desktop/dist/电视直播.exe`（约 8.8 MB） | ✅ 实测端到端通过（新图标已嵌入） |
| **Linux** | `linux/` | `linux/dist/纯净电视`（单文件）+ 可选 AppImage | ✅ 脚本就绪，`run.sh` 免编译直跑 |

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
│   ├── app.ico                  应用图标（品牌渐变，gen_icon.py 生成）
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

| 端 | 依赖 |
|---|---|
| 安卓构建 | JDK（javac/keytool）+ `android/tools/` 下的 build-tools（见下） |
| Windows 运行 | 系统自带 Microsoft Edge（Win10/11）或 Chrome |
| Windows 打包 | Python 3.8+ ，`pip install pyinstaller pillow` |
| Linux 运行 | python3（系统自带）+ Chrome/Chromium/Edge 任一 |
| Linux 打包 | python3-venv、python3-pip |

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
- Android：`tools/gen_assets.py` 生成 `ic_launcher.png`（192×192）与 `tv_banner.png`（320×180），品牌渐变圆角方块 + 白色播放三角。
- 桌面：`gen/gen_icon.py` 生成 `app.ico`（品牌渐变 + 白三角），已嵌入 exe。

> 两个生成脚本均使用**相对路径**（基于脚本自身位置），在任何目录下执行都能写对位置。

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

**关键技巧**：解密密钥的盐是动态的（含 deviceId / appVersion / ips / 日期）。
这里不去猜盐，而是**用第 1 条线路的已知明文反推 XOR key** —— 明文结构固定
（`<API>/eplay2.php?token=<旧token>&tid=..&id=..&p=0&type=.flv`），与密文逐字节异或即得
密钥，于是无论盐怎么变都能解出整页全部线路。

视频流直连（接口已开 CORS），**不经过本地服务中转**，省带宽降延迟。

---

## 常见问题

**Q：某频道打不开 / 全部线路失败？**
令牌是会话级的，**返回列表重新进入**即可刷新。仍不行说明该源临时失效。

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

## 待扩展（源码已预留）

- 回看播放（回看页是另一套密文，需另写解密分支）
- 台标补全、开机自启
- 遥控器数字键直选频道
- Linux 端 TV 大屏模式（界面已支持宽度自适应，可加 `?tv=1` 强制）

---

## 免责声明

仅用于个人学习与自有设备上的技术研究。接口与内容版权归原服务方所有，
请勿用于商业用途或传播。
