// 纯净电视 · WRY/Tauri 网络旁听 spike（里程碑 2 前的命门验证）
//
// 目标：回答「Tauri/WRY 能否旁听到 WebView2 里网页发出的 http(s) 请求」——
//   这是官网源（占 94.5% 频道）在 Rust 重写里能否抓到 m3u8 的前提。
//
// 结论背景（见 WRY_NETWORK_SPIKE.md）：官方 `on_web_resource_request` 只对
//   tauri://localhost 内部协议生效，旁听不到外部网页请求。本 demo 走**裸 COM 直调**：
//   拿 PlatformWebview 的 ICoreWebView2Controller -> CoreWebView2()，
//   然后 AddWebResourceRequestedFilter("*", ALL) + add_WebResourceRequested，
//   把每一个网页发出的请求 URI 打到 stderr。
//
// 用法（Windows only）：
//   cargo run -p clean-tv-app --bin spike_net -- <目标URL>
//   不传 URL 则默认加载一段会发多个请求的真实页面（baidu）。
//
// 验证判据：
//   ① stderr 是否出现目标网页域名下的 http(s) 请求（如 www.baidu.com 的资源）
//   ② 若出现 .m3u8/.flv 请求，则官网源命门彻底打通

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;
// webview2-com 高层回调封装（在 crate 顶层，见 lib.rs `pub use callback::*`）
use webview2_com::WebResourceRequestedEventHandler;
// 底层 COM 接口 + 常量（在 Microsoft 命名空间下）
use webview2_com::Microsoft::Web::WebView2::Win32::{
    COREWEBVIEW2_WEB_RESOURCE_CONTEXT_ALL, ICoreWebView2, ICoreWebView2WebResourceRequestedEventArgs,
};

/// 从底层 controller 拿到 ICoreWebView2，注册全量请求旁听。
fn attach_net_probe(platform: tauri::webview::PlatformWebview) {
    use windows::core::HSTRING;

    // 1. controller -> webview
    let webview: ICoreWebView2 = match unsafe { platform.controller().CoreWebView2() } {
        Ok(w) => w,
        Err(e) => {
            eprintln!("[spike] 拿 ICoreWebView2 失败: {e}");
            return;
        }
    };

    // 2. 全量过滤器：URI 用 "*" 匹配一切（HSTRING 实现 Param<PCWSTR>）
    let filter = HSTRING::from("*");
    if let Err(e) = unsafe {
        webview.AddWebResourceRequestedFilter(&filter, COREWEBVIEW2_WEB_RESOURCE_CONTEXT_ALL)
    } {
        eprintln!("[spike] AddWebResourceRequestedFilter 失败: {e}");
        return;
    }

    // 3. 注册回调：每个请求打印 URI
    let handler = WebResourceRequestedEventHandler::create(Box::new(
        move |_sender: Option<ICoreWebView2>,
              args: Option<ICoreWebView2WebResourceRequestedEventArgs>| {
            let Some(args) = args else {
                return Ok(());
            };
            let request: webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2WebResourceRequest =
                match unsafe { args.Request() } {
                    Ok(r) => r,
                    Err(e) => {
                        eprintln!("[spike] args.Request() 失败: {e}");
                        return Ok(());
                    }
                };
            let mut uri = windows::core::PWSTR::null();
            if let Err(e) = unsafe { request.Uri(&mut uri) } {
                eprintln!("[spike] request.Uri() 失败: {e}");
                return Ok(());
            }
            let uri_str = unsafe { uri.to_string().unwrap_or_else(|_| "<bad utf16>".into()) };
            eprintln!("[spike][req] {uri_str}");

            let lower = uri_str.to_lowercase();
            if lower.contains(".m3u8") || lower.contains(".flv") || lower.contains(".ts") {
                eprintln!("[spike][HIT] >>> 捕获疑似媒体流: {uri_str}");
            }

            Ok(())
        },
    ));

    let mut token: i64 = 0;
    if let Err(e) = unsafe { webview.add_WebResourceRequested(&handler, &mut token) } {
        eprintln!("[spike] add_WebResourceRequested 失败: {e}");
        return;
    }
    eprintln!("[spike] 网络旁听已挂载，token={token}");
    // handler 由 WebView2 的 COM 引用计数兜底，泄漏掉避免提前 drop 导致回调失效。
    std::mem::forget(handler);
}

fn main() {
    let url = std::env::args().nth(1);

    tauri::Builder::default()
        .setup(move |app| {
            let win = app.get_webview_window("main").unwrap();

            // 在导航之前挂载探针，才能抓到首页首个请求
            if let Err(e) = win.with_webview(attach_net_probe) {
                eprintln!("[spike] with_webview 失败: {e}");
            }

            let target = match &url {
                Some(u) => u.clone(),
                None => "https://www.baidu.com/".to_string(),
            };
            eprintln!("[spike] 导航到 {target}");
            if let Err(e) = win.navigate(target.parse().unwrap()) {
                eprintln!("[spike] navigate 失败: {e}");
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("Tauri 启动失败");
}