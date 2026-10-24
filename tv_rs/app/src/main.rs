// 纯净电视 · Rust 重写（里程碑 1：主窗口骨架）
//
// 相较于里程碑 0（验证壳），本版升级为真实主窗口：
//   1. 用 WebviewWindowBuilder 显式建窗，挂 on_new_window 拦弹窗（命门②）
//   2. 挂 net_probe 网络旁听模块（命门①，官网源抓 m3u8 用的）
//   3. 前端升级为「频道列表 + 播放占位」主界面，经 IPC 拉取真实频道
//
// 后续里程碑：播放层、两源合流、EPG 等。本版刻意保持主窗口骨架的克制。

// Windows 发布构建不弹控制台窗口（debug 下保留，方便看自检日志）
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod net_probe;

use serde::Serialize;
use tauri::{WebviewUrl, WebviewWindowBuilder};

/// 运行环境信息。
#[derive(Serialize)]
struct AppInfo {
    app: &'static str,
    app_version: &'static str,
    core_version: &'static str,
    target: &'static str,
    profile: &'static str,
}

/// 频道分类（央视 / 卫视 / 地方台…）。里程碑 1 先给固定分类，里程碑 2 接真实抓取。
#[derive(Serialize)]
struct Category {
    tid: String,
    name: String,
}

#[tauri::command]
fn app_info() -> AppInfo {
    AppInfo {
        app: "clean-tv-app",
        app_version: env!("CARGO_PKG_VERSION"),
        core_version: clean_tv_core::VERSION,
        target: std::env::consts::ARCH,
        profile: if cfg!(debug_assertions) { "debug" } else { "release" },
    }
}

/// 返回分类列表。
#[tauri::command]
fn list_categories() -> Vec<Category> {
    vec![
        Category { tid: "tv".into(), name: "央视".into() },
        Category { tid: "ws".into(), name: "卫视".into() },
        Category { tid: "df".into(), name: "地方".into() },
    ]
}

/// 返回某分类下的频道列表。
///
/// 里程碑 1 用内置的固定分类页 HTML 走数据层 `parse_channels`（真实解析逻辑），
/// 验证「数据层 -> IPC -> 前端」链路；里程碑 2 换成真实抓取 + 解密。
#[tauri::command]
fn list_channels(tid: String) -> Vec<clean_tv_core::core::Channel> {
    let html = match tid.as_str() {
        "tv" => r#"
            <a href="iptve.php?act=play&tid=tv&id=1">CCTV-1 综合</a>
            <a href="iptve.php?act=play&tid=tv&id=2">CCTV-2 财经</a>
            <a href="iptve.php?act=play&tid=tv&id=3">CCTV-3 综艺</a>
            <a href="iptve.php?act=play&tid=tv&id=4">CCTV-4 中文国际</a>
            <a href="iptve.php?act=play&tid=tv&id=5">CCTV-5 体育</a>
            <a href="iptve.php?act=play&tid=tv&id=13">CCTV-13 新闻</a>
        "#,
        "ws" => r#"
            <a href="iptve.php?act=play&tid=ws&id=1">湖南卫视</a>
            <a href="iptve.php?act=play&tid=ws&id=2">江苏卫视</a>
            <a href="iptve.php?act=play&tid=ws&id=3">东方卫视</a>
            <a href="iptve.php?act=play&tid=ws&id=4">浙江卫视</a>
        "#,
        _ => r#"
            <a href="iptve.php?act=play&tid=df&id=1">北京台</a>
            <a href="iptve.php?act=play&tid=df&id=2">上海台</a>
            <a href="iptve.php?act=play&tid=df&id=3">广东台</a>
        "#,
    };
    clean_tv_core::core::parse_channels(html, &tid)
}

/// 启动自检：前端把真实渲染状态回传，用于区分「真白屏」与「抓图失败」。
///
/// 必须用 stderr（stdout 重定向到文件是块缓冲，进程 terminate 时整段丢失）。
#[tauri::command]
fn self_check(report: String) {
    eprintln!("[selfcheck] {report}");
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            // 显式建主窗口（conf 的 windows 数组已清空），这样才能挂 on_new_window。
            let win = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("纯净电视 · Rust")
                .inner_size(1100.0, 700.0)
                .min_inner_size(720.0, 480.0)
                .center()
                // 命门②：拦死所有 window.open（广告 / 官方播放页弹窗）。
                // 里程碑 2 再按需改为「拦下但在本容器跟进导航」。
                .on_new_window(|url, _features| {
                    eprintln!("[win] 拦截新窗口请求: {url}");
                    tauri::webview::NewWindowResponse::Deny
                })
                .build()?;

            // 命门①：挂网络旁听。里程碑 1 先旁听全量（空白名单），只打印命中日志；
            // 里程碑 2 接官方源时按域收敛白名单。
            let probe = net_probe::NetProbe::new(
                vec![],
                Some(Box::new(|hit| {
                    eprintln!("[net] 媒体流路由待接: {} ({})", hit.kind, hit.url);
                })),
            );
            let _ = win.with_webview(move |platform| probe.attach(platform));

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            app_info,
            list_categories,
            list_channels,
            self_check,
        ])
        .run(tauri::generate_context!())
        .expect("Tauri 启动失败");
}