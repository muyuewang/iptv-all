# WRY/Tauri 网络旁听 + window.open 拦截 能力验证（spike）

> 日期：2026-10-24 · 版本：tauri 2.12.5 / wry 0.57.1
> 目的：判断官网源（占 94.5% 频道）的两条命门能否在 Rust/Tauri 重写中复现。

## ✅ 结论（已实测验证，非「应该可以」）

| 命门 | 需求 | Tauri/WRY 能否做到 | 判定 |
|---|---|---|---|
| ①旁听网络请求抓 m3u8 | 网页播放时拦截 `shouldInterceptRequest`，抓到 m3u8 真流 URL | ✅ **裸 COM 直调可行，已实测捕获** | 可行（走方案①，非官方 API） |
| ②拦 `window.open` | 把新开窗口（广告/官方播放页）拦死在当前容器 | ✅ **原生支持**，钩子齐全 | 可行 |

**官网源命门①已打通**：写了个最小 demo（`app/src/bin/spike_net.rs`），用
`PlatformWebview::controller().CoreWebView2()` 拿到底层 `ICoreWebView2`，裸调
`AddWebResourceRequestedFilter("*", ALL)` + `add_WebResourceRequested`，**实测抓到**：

```
[spike] 网络旁听已挂载，token=21
[spike][req] https://www.baidu.com/                              ← 主文档
[spike][req] https://pss.bdstatic.com/.../cos-icon.css          ← 子资源
[spike][req] https://example.com/live/chunk.flv                  ← 媒体流
[spike][HIT] >>> 捕获疑似媒体流: .../chunk.flv
[spike][HIT] >>> 捕获疑似媒体流: .../seg001.ts
```

→ **关键结论**：官方 `on_web_resource_request` 只能旁听 `tauri://localhost` 内部协议
（wry 把 WebResourceRequested 阉割成「只服务自定义协议」，证据见下文），但**裸 COM
直调能完整旁听到外部网页的任意 http(s) 请求，含媒体流**。官网源抓 m3u8 的路是通的。

## ②拦 window.open —— 可行（证据）

WRY 的 `WebViewAttributes` 暴露了 `new_window_req_handler`（`wry-0.57.1/src/lib.rs:705`）：

```rust
pub new_window_req_handler:
  Option<Box<dyn Fn(String, NewWindowFeatures) -> NewWindowResponse>>,
```

Tauri 侧 `WebviewWindowBuilder::on_new_window`（`webview_window.rs`）再包一层。
返回 `NewWindowResponse::Deny` 即可拦住 `window.open`；若想「拦下但在我容器内跟进导航」，
可返回 `Allow` 到同一 WebView（对应安卓现有 `shouldOverrideUrlLoading` + pageView 那套逻辑）。

**结论**：这条命门无风险。桌面端 WebView2 弹窗铁律（历史记忆：NewWindowRequested
必须注册否则开出归 exe 所有的独立顶层窗口）在 Tauri 里由 wry 内部处理，我们只需
在 builder 上挂 `on_new_window` 即可。

## ①旁听外部 http 请求 —— 官方 API 做不到（证据链）

这是 spike 的核心发现，**官网源这条命门在 Tauri 稳定 API 下是断的**。

1. WRY 内部**已经**实现了 WebView2 的 `WebResourceRequested`（`webview2/mod.rs:1050
   `webview.add_WebResourceRequested(...)`），但**只服务于自定义协议**：
   - `attach_custom_protocol_handler`（`webview2/mod.rs:1010`）里，事件回调
     `custom_protocols.iter().find(...)` 只匹配 workaround URI；匹配不到就 `Ok(())` 直接放行。
   - `AddWebResourceRequestedFilter` 也只对 `custom_protocol_workaround` 前缀作过滤。

2. 这意味着：**网页发出的任意 http(s) 请求（含 m3u8）根本不会进 wry 的 Rust 回调**，
   只对 `tauri://`、自定义协议这类内部协议生效。

3. Tauri 的 `on_web_resource_request`（`webview_window.rs:235`、`mod.rs:494`）是
   包在 `protocol/tauri.rs` 上的——`get_response`:188-190 里 handler 只在
   **tauri://localhost 资源的响应阶段**被调用，`strip_prefix("tauri://localhost")`
   （`tauri.rs:159-164`）进一步锁定它只处理本地协议资源，与外部网页请求无关。

4. 结论：想要旁听到官网源网页播放 m3u8 的请求，**走官方 `on_web_resource_request`
   这条路行不通**。

## 可行的绕路方案（按成本排序）

1. **裸 COM 直调 ✅ 已实测验证通过（推荐）**：Tauri 提供 `PlatformWebview::controller()`，
   返回的正是 `webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2Controller`，
   再 `.CoreWebView2()` 拿到 `ICoreWebView2`，手动
   `AddWebResourceRequestedFilter("*", ALL)` + `add_WebResourceRequested`。
   这条路的**可运行 demo 就是 `app/src/bin/spike_net.rs`**，实测抓到外部网页请求和
   媒体流（.flv/.ts）。关键点：
   - `webview2-com` 是 Tauri 已拉进来的依赖（0.39.1），显式声明同版本即可复用，
     **不新增依赖、不多编译 bindings**；用它的 `WebResourceRequestedEventHandler::create()`
     （顶层导出，见 `lib.rs` 的 `pub use callback::*`）免手写 vtable。
   - `windows` crate 版本必须是 **0.62**（与 webview2-com-sys 0.39.1 一致），
     否则两套不兼容的 `windows` 同时编译，COM 类型对不上。
   - 过滤器用 `windows::core::HSTRING::from("*")`（HSTRING 实现 `Param<PCWSTR>`），
     不能传裸 `*const u16`。
   - handler 要 `std::mem::forget`（COM 引用计数兜底），并 `let mut token: i64`。
   - 必须在 `win.navigate()` **之前**挂探针，才能抓到首页首个请求。

2. **CDP 调试端口嗅探**（备选）：复刻安卓 `cdp.py` 思路。优点是零 COM 负担；缺点是
   依赖调试端口、有生命周期开销，属「旁路工具」而非第一等架构。官方源若后续觉得
   裸 COM 维护成本高，可用此方案兜底。

3. **保持现有桌面端架构不动**：已无必要——命门①已打通，官网源可以放心往 Rust 搬。

## 给后续里程碑的建议（已更新）

- **里程碑 2（官方源）可以正常推进**：命门①（旁听抓 m3u8）已用 `spike_net.rs` 实测
  打通，无需再重复 spike。下一批工作是把 `attach_net_probe` 从 spike 收编成正式模块：
  ①做「过滤器白名单」（只拦目标域，别拦全 `*`，避免和 Tauri 自己的 `tauri://` IPC、
  `asset://` 资源抢请求）；②在回调里判断 `.m3u8`/`.flv` 并转发给播放层（对应安卓
  `shouldInterceptRequest 旁听 m3u8 必须 return null` 那套，但 WebView2 的
  `WebResourceRequested` 若**不 SetResponse 就是放行**，天然符合「旁听不拦截」语义）。
- 拦 `window.open` 无风险，里程碑 1 的窗口骨架即可带上 `on_new_window`。