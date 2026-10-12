# 用 Flutter 改造纯净电视 · 可行性与方案评估

> 评估日期：2026-10-11 · 评估对象：`tv_all`（安卓 / Windows / Linux 三端）
> 评估方式：读实际代码量 + 核对 Flutter 侧对应包的**当前能力**（非道听途说）

---

## 0. 结论先说

| 维度 | 结论 |
|---|---|
| **技术可行性** | **可行，无死结。** 所有关键能力（自定义 UA 的 WebView、libmpv 硬解、官网页注入、Win32 子窗口问题）在 Flutter 侧都有成熟对应物 |
| **工程建议** | **不建议「全量重写」**。建议走「**PoC 门禁 + 桌面端先行**」增量路线，或评估 **Compose Multiplatform** 这条更贴合本项目特性的替代路 |
| **最大收益** | UI 从「写两遍」（tkinter + HTML）变成「写一遍」；核心从「Python + JS 双份手工同步」变成 Dart 一份 |
| **最大代价** | 动到**最值钱、最难调、刚验证过**的部分：播放内核切换会**作废 v2.6/v2.7 的稳定性成果**；WebView 的控制力度从「裸 WebView」降到「封装过的插件」 |
| **一句话** | Flutter 擅长「把界面统一」，但这个项目的护城河恰恰在**平台原生的脏活**里 —— 那正是 Flutter 要抽象掉、且抽象得**有损**的地方 |

---

## 1. 现状盘点（实测数字，非估算）

### 1.1 代码体量

| 模块 | 文件数 | 行数 | 说明 |
|---|---|---|---|
| `shared/`（真源） | 5 | **2494** | `core.py` 397 + `wvt.py` 485 = **Python 一套**；`web/tv_core.js` 267 + `web/wvt.js` 471 + `web/wvt_inject.js` 674 = **JS 一套** |
| `android/app/src`（Java） | 7 | **1963** | `MainActivity` 864 / `Media3PlayerBackend` 380 / `Exo2PlayerBackend` 337 / `FrozenFrame` 135 |
| `android/app/assets`（前端） | — | **player.html 2330 行** | + `mpegts.js` 273KB + `logos.js` 153KB + `wvt_channels.js` 105KB（数据） |
| `desktop/*.py` | 4 | **5055** | `native_app.py` 2662 / `embed.py` 1058 / `cdp.py` 839 / `tv_app.py` 496 |
| `linux/` | 3 | 255 | 壳脚本，包 desktop 的 Python |
| `tools/`（测试资产） | 77 | **10839** | **50 个** `_test_* / _probe_* / _smoke_* / check_*` 脚本 |

**产物体积**：`纯净电视.apk` **5.0 MB**（手写 aapt2+d8，无 Gradle 无冗余）· `电视直播.exe` **67.6 MB**（PyInstaller onefile，内含 mpv）。

### 1.2 构建链现状（这点很关键）

- 安卓：**没有 Gradle、没有完整 Android SDK** —— `android/build.sh` 手写 `aapt2 + d8 + apksigner`，工具链在 `android/tools/`（自带 jar）。
- 桌面：`PyInstaller onefile` → **单个绿色 exe**，放哪跑哪。
- Linux：直接跑 Python 源码 / 可选 AppImage。

本机实测：**未安装 Flutter/Dart**；Android 只有手工拉的 `platform-34` jar（无 SDK/NDK）；**VS 2022 已装**（Flutter Windows 的 MSVC 需求可满足）；`pub.dev` 直连在本沙箱被拦（用户实机通常无此问题）。

---

## 2. 能力映射表：现在的每一块脏活，Flutter 侧能不能接

| # | 现在怎么做的 | 为什么难 | Flutter 侧对应 | 成熟度 | 风险 |
|---|---|---|---|---|---|
| 1 | 安卓 `WebView.setUserAgentString(LT-APP/48/517…)` —— **UA 是命门**，服务器靠它区分新旧播放器页 | 浏览器改不了 UA，是整套方案的地基 | `webview_flutter` 的 `setUserAgent()`；Windows 侧 `webview_flutter_windows.setUserAgent()` | ✅ 官方/成熟 | 中（要重测整条 UA 链） |
| 2 | 官网页注入 `wvt_inject.js`（找 `<video>`、原地钉全屏、去浮层、失败快报） | 674 行踩坑史，跑在**目标页上下文**里 | Windows：`addScriptToExecuteOnDocumentCreated`（**文档创建前注入**，正是需要的语义）；安卓：`runJavaScript` + `addJavaScriptChannel` | ✅ 覆盖良好 | 低（**JS 本身可原样复用**） |
| 3 | 桌面端 `embed.py` —— **WebView2 COM vtable 直调** + 专职 STA 线程 + MsgWait 泵（1058 行） | 为了拿到 `--app` 式内嵌和精确生命周期 | `webview_flutter_windows` 内部就是 WebView2 + 纹理桥（`TextureBridgeGpu`→D3D）；`initializeEnvironment(additionalArguments:)` 可传 Chromium 开关 | ✅ 相当关键的是它还**原生支持 headless**（`setSize` 后不渲染也能跑脚本）—— 正好对应「CDP 嗅探兜底」 | **低**（Windows 端反而是**最合适**的一块） |
| 4 | 桌面端 mpv `--wid` 内嵌 + `--input-ipc-server` JSON 控制 + `--http-header-fields`（逗号转义坑） | Win32 子窗口不跟随父窗口 resize，一堆补丁 | `media_kit`（**就是 libmpv**）+ `media_kit_video`，纹理渲染进 Widget 树 | ✅ Windows/Android/Linux 全支持，硬解走 `mediacodec_embed`/ANGLE | **中**（要重迁 header/缓存档位/选路） |
| 5 | 安卓 **ExoPlayer(media3) 硬解** —— v2.6 起播看门狗、v2.7 坏片自救全建在它的 `onPlayerError` / `onRenderedFirstFrame` 上 | 刚验证过的核心稳定性资产 | **换引擎 = 换 API**：libmpv 的 error/property 事件模型完全不同 | ⚠️ 概念可迁移，**实现要重写** | **高 ★** |
| 6 | 安卓**软解 `mpegts.js`**（WebView 内 MSE 播放）—— 与硬解双路 | 兜底通道 | libmpv **本身就能软解** → 有机会**干掉 mpegts.js 双路**，全平台统一到一个引擎 | ✅ 这是 Flutter 路线**最大的真收益** | 中 |
| 7 | `FrozenFrame` 抓 Bitmap 钉屏做无缝换台（`release()` 会清纹理） | 实测把暗屏从 3.36s 压到 0.00s | media_kit `screenshot()` / Flutter `RepaintBoundary.toImage()` | ✅ 可复现，甚至更简单 | 低 |
| 8 | Win32 子窗口不跟随 resize 的一堆补丁（`put_Bounds`/`_fit_now`） | 本项目全屏缺画面的**唯一根因** | Flutter 里 WebView/video 都是**纹理**，在 Widget 树里天然跟随布局 | ✅ **整类 bug 消失** | 低（净收益） |
| 9 | 安卓 TV 遥控器四行导航（`listOf()` + `getClientRects` 过滤） | HTML 里手搓焦点 | `dpad` 包（Flutter 3.24+，D-pad 导航 / 焦点记忆 / 区域规则）或原生 `Focus`+`FocusTraversalGroup` | ✅ 生态可用 | 中（要重做一整套焦点模型） |
| 10 | 官网源 Widevine DRM 播不了（WebView2 不捆绑 CDM） | 已是已知限制 | media_kit 官方注明 **no DRM** —— **与现状持平，无回退** | — | 无变化 |

**读表结论**：Windows 端（第 3、4、8 项）Flutter **明显更优**；安卓端（第 5、9 项）Flutter **明显更贵**。

---

## 3. 六个真实风险（按严重度排序）

### 🔴 R1｜播放内核切换会**作废刚验证的稳定性成果**
v2.6（起播看门狗 + 自动跳台）、v2.7（HLS 坏片自救 + 线路级判死）全部长在 ExoPlayer 的 `onPlayerError` / `PlaybackException.errorCode` / `onRenderedFirstFrame` 上。
改 Flutter 若用 media_kit，等于**换引擎**：HLS 栈从 ExoPlayer 换成 ffmpeg 的 hls demuxer，错误码语义、首帧回调、缓冲模型全不一样 —— `Media3PlayerBackend.java`(380) + `Exo2PlayerBackend.java`(337) + `player.html` 里整个 `bindNativeWatch`/`armStartWatch` 体系**重写**。
**缓解**：若坚持 Flutter，可在安卓侧保留 ExoPlayer（走 platform channel 桥一个薄 Java 层）—— 但那样 Dart + Java 两头写，收益又被吃掉一半。

### 🔴 R2｜安卓 WebView 的**控制力度会下降**
现在用的是**裸 `android.webkit.WebView`**，能干这些事：`setWebContentsDebuggingEnabled`、`pauseWebMedia`（掐掉 OpenSL ES 出声）、**切 WebView alpha 做换台过渡**、`pageView` 覆盖层代次管理 + 延后 150ms 拆 WebView、原生 `requestFullscreen` 接管。
`webview_flutter` 的 API **明显更窄**，这些大多要落到 platform channel 或自己 fork —— **等于把已经写好的 Java 再写一遍**。

### 🟠 R3｜PlatformView 的透明叠加（安卓特有）
Flutter 安卓上 WebView 是 **PlatformView（混合合成）**，`<video>`/MSE 画面与 Flutter 纹理的**透明叠加与 z-order 历史上有坑**。现在的方案恰恰是「靠切 alpha 让下层画面透出来」。这条要在真机上重验，**不能想当然**。

### 🟠 R4｜构建链从「极简」变「重型」
| | 现在 | Flutter |
|---|---|---|
| 安卓 | 手写 aapt2+d8，**5 MB APK**，不依赖 Gradle/SDK | 需要 Flutter SDK + 完整 Android SDK + **AGP 9 / Gradle 9 / JDK 17+** + NDK 供给 + 联网拉 pub/maven |
| Windows | `PyInstaller onefile` → **单个绿色 exe**，零外部依赖 | 需要 **MSVC（本机有 VS2022 ✅）**；产物是**文件夹**（exe + DLL + data/），要恢复「一个文件」得再套自解压/Inno |
| Linux | 直接跑 Python / AppImage | 需要 **GTK + clang + 系统 `libmpv-dev`** —— 用户机器上多一层系统依赖 |
体积：Flutter 引擎 +4~8MB，media_kit 每 ABI 再带 libmpv（约 10~20MB/ABI）→ 安卓 APK 从 **5MB 涨到 20~40MB**（`--split-per-abi` 后约 15~25MB）。**「绿色便携单文件」这条产品特性会退化。**

### 🟡 R5｜UA / HTTP header 这条命门要**全链重测**
`LT-APP/48/517` 的 UA 注入、`--http-header-fields` 的逗号转义、镜像域名回退 + 抓页解密**必须同域** —— 这些是 v2.x 一路踩出来的。换引擎/换 WebView 宿主后，**必须按老规矩在真机重新实测**，不接受「应该一样」。

### 🟡 R6｜`tv_core.js` 未必能消掉
「核心写两遍」的痛点是真的，但注意：**官网页注入脚本必须留在 JS**（跑在目标页上下文）；若软解路径继续用 WebView，`tv_core.js` 也还在。**只有把软解也交给 libmpv、彻底弃用 mpegts.js，JS 核心才可能消失** —— 这正好是 R1 的另一面。

---

## 4. 四条路线对比

| | **A 全量 Flutter** | **B 增量：桌面端先行** | **C Compose Multiplatform** | **D 维持现状 + 局部优化**（当前路线） |
|---|---|---|---|---|
| 做法 | 三端 UI+核心全部 Dart 化，播放统一 media_kit | 先只把 `native_app.py` 换成 Flutter，安卓不动 | 核心 Kotlin，UI 用 Compose（Android + Desktop 共享），安卓保留裸 WebView/ExoPlayer | 继续 Python/JS 双份同步，按 `优化建议.md` 逐项打磨 |
| UI 写一遍 | ✅ | 部分（安卓仍 HTML） | ✅ | ❌ |
| 核心写一遍 | 部分（注入 JS 仍在） | ❌ | ✅（Kotlin 一份） | ❌ |
| 安卓原生控制力 | ⚠️ 下降 | ✅ 不动 | ✅ **保留**（Compose 与 Android View 互操作天然好） | ✅ |
| 保住 v2.6/v2.7 | ❌ 重写 | ✅ | ✅ | ✅ |
| 单文件绿色便携 | ❌ | ❌（桌面变文件夹） | ❌ | ✅ |
| 主要代价 | 最高，全端回归重做 | 只换桌面壳，收益最小 | 引入 Kotlin 生态 + 学 Compose Desktop | 维护双份同步的长期成本 |
| 适合 | 团队重做、且愿意接受体积/构建变重 | 只想摆脱 tkinter 的丑陋与难维护 | **最贴合本项目特性**（原生脏活多 + 要共享 UI） | 单人维护、追求稳定与体积极致 |

> **C 路线值得单列**：本项目的护城河是「平台原生脏活」（UA WebView、ExoPlayer 调优、Win32 子窗口）。Compose Multiplatform 允许 **安卓端继续用裸 WebView 与 ExoPlayer**（Compose 里塞 `AndroidView` 是原生能力），同时把 UI 与核心在 Kotlin 里共享到桌面 —— 它**不逼你抽象掉那些脏活**，这是它相对 Flutter 的结构性优势。代价是 Kotlin 生态与 Compose Desktop 的成熟度要自己再核一遍。

---

## 5. 若决定走 Flutter：分阶段方案（带门禁）

### Phase 0 · PoC 门禁（**先花 3~5 天，用最烂的路验证最难的点**）
**任何一行生产代码都先别动。** 建一个空 Flutter 工程，只验证四件事：

| # | 验证项 | 通过标准（硬性） |
|---|---|---|
| P1 | `media_kit` 在 **Windows** 播一条真 IPTV HLS，带自定义 UA + Referer | 出画 + 音画同步 + 硬解生效（GPU 占用有变化） |
| P2 | `media_kit` 在 **雷电 emulator-5554** 播同一条流 | 硬解起播 ≤ 3s，且**能拿到错误事件**（为 R1 铺路：先证明 libmpv 的错误模型够用） |
| P3 | `webview_flutter_windows` 注入 `wvt_inject.js` 到 m.gdtv.cn | 脚本在**文档创建前**注入成功，`<video>` 被正确钉满全屏 |
| P4 | `webview_flutter`(Android) `setUserAgent` 后抓页解密 | 拿到**新播放器页**（即 UA 生效），线路数 > 0 |

> **P1~P4 全绿** 才进入 Phase 1；**任一红** → 直接回到方案 D，损失仅几天。
> 特别看 P2：如果 libmpv 在安卓不吃某些 IPTV 流，R1 就从「贵」升级成「致命」。

### Phase 1 · 核心 Dart 化（不动 UI，纯逻辑）
把 `core.py` + `tv_core.js` 合成一份 Dart `core/`，用**同一批 fixtures**（channel 列表、加密页样本、EPG 样本）做**逐字段比对**，确保与 Python/JS 版输出一致。
**这条可以先做、且即使最终不上 Flutter 也不亏**（Dart 命令行工具也能验证算法）。

### Phase 2 · 桌面端替换
Flutter 桌面上线，**与 Python 版并存**（靠 `desktop/_prev/` 那套并存机制），用现成的 `check_exe_*` / `check_wvt_*` 回归对照，达标后切换默认。

### Phase 3 · 安卓端替换
最难的一步。建议**分两步**：
1. UI 先 Flutter 化，**播放继续走原生**（platform channel 桥 ExoPlayer + 裸 WebView）—— 保住 v2.6/v2.7；
2. 再评估是否把播放也切 media_kit（此时 R1 的代价才真正支付）。

### Phase 4 · TV / 收尾
`dpad` 焦点模型、遥控器四行走查、体积优化（`--split-per-abi`）、自解压单文件封装。

---

## 6. 投入产出粗算

- **可复用（不必重写）**：`wvt_inject.js`(674) 全部 · 三端业务算法（换一份语言写）· **50 个测试脚本的判据逻辑**（断言思路可移植，driver 要重写）· 台标/频道数据（纯数据）。
- **必须重写**：Java 播放后端(717) + `MainActivity` 的 WebView 掌控(864) · `player.html` 的 UI 部分(约 1900/2330) · `native_app.py` 全部 UI(约 2000/2662) · `embed.py`(1058，Windows 侧可大幅缩水) · 全部测试 driver。
- **粗判**：**全量 A 路线的净工作量 ≈ 现有三端代码的 60~70% 重写 + 全端回归重跑**；而收益主要是「省掉未来的双份同步」——**只有当后续还要持续大改 UI 时才划算**。以目前 v2.8 已相对稳定的状态，**收益期偏长、回本偏慢**。

---

## 7. 最终建议

1. **不要为了「统一技术栈」而全量重写。** 现在的双份同步（Python/JS）是有成本，但它是**局部成本**；Flutter 全量重写会把「局部成本」换成「全端回归风险 + 构建链变重 + 单文件便携退化」。
2. **如果目标是「安卓 TV 大屏体验 + 桌面端不再丑」** → 优先做 **Phase 0 PoC**，只花几天就能把 R1/R3 这两个真风险证伪或证实；证伪了再谈全量。
3. **认真评估 C 路线（Compose Multiplatform）**。它对本项目「原生脏活多 + 想共享 UI」的特性契合度高于 Flutter —— 它不要求你放弃裸 WebView 与 ExoPlayer。
4. **无论走哪条，先做 Phase 1（核心 Dart/Kotlin 化 + 逐字段比对）**：这一步**独立可验证、失败成本低、且不回退现有任何能力**，是性价比最高的起点。
5. **维持现状**（方案 D）在「单人维护 + 追求稳定 + 极致体积」前提下，**依然是完全合理的选择** —— 按 `优化建议.md` 逐项打磨即可。

---

*本文所有能力判断基于 2026-10 的 Flutter 3.47 稳定线与各包公开文档；Phase 0 的四个验收项必须在**本机真机**上实跑，不以文档描述为准。*
