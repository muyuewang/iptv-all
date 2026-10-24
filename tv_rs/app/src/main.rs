// 纯净电视 · Tauri 壳（里程碑 0）
//
// 这一版的目标**不是**做完整应用，而是用最小可运行的壳验证三件事：
//   1. MSVC + Windows SDK 能把 tauri（WebView2）依赖树链接通过
//   2. workspace 里 clean-tv-core 能被 GUI 侧正常引用
//   3. serde 类型能跨 Tauri IPC 边界传给前端（数据层将来要靠这个出口）
//
// 真正的主窗口、播放层、两源合流都在后续里程碑。这里刻意保持极薄。

// Windows 发布构建不弹控制台窗口（debug 下保留，方便看自检日志）
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Serialize;

/// 运行环境信息 —— 用来验证「core 版本 / 编译目标」能传到前端。
#[derive(Serialize)]
struct AppInfo {
    app: &'static str,
    app_version: &'static str,
    core_version: &'static str,
    target: &'static str,
    profile: &'static str,
}

#[tauri::command]
fn app_info() -> AppInfo {
    AppInfo {
        app: "clean-tv-app",
        app_version: env!("CARGO_PKG_VERSION"),
        // 取数据层 crate 自己编译出的常量，证明 workspace 依赖真的连上了
        core_version: clean_tv_core::VERSION,
        target: std::env::consts::ARCH,
        profile: if cfg!(debug_assertions) { "debug" } else { "release" },
    }
}

/// 用一段固定的分类页 HTML 跑一遍数据层的频道解析，把结果返回前端。
///
/// 这是「数据层 -> IPC -> 前端」的最小闭环：如果前端能列出这三条频道，
/// 说明 clean-tv-core 的 serde 序列化与 Tauri 命令注册都是通的。
#[tauri::command]
fn demo_channels() -> Vec<clean_tv_core::core::Channel> {
    let html = r#"
        <a href="iptve.php?act=play&tid=tv&id=1">CCTV-1 综合</a>
        <a href="iptve.php?act=play&tid=tv&id=2">CCTV-2 财经</a>
        <a href="iptve.php?act=play&tid=tv&id=3">CCTV-3 综艺</a>
        <a href="iptve.php?act=play&tid=ys&id=9">不该出现（tid 不匹配）</a>
    "#;
    clean_tv_core::core::parse_channels(html, "tv")
}

/// 启动自检：让页面把真实渲染状态回传到后端日志。
///
/// 为什么留这个（而不是排障完就删）：
/// WebView2 的内容走 DirectComposition 硬件合成层，**不进 GDI 屏幕表面**，
/// 所以 `PrintWindow(PW_RENDERFULLCONTENT)` 与屏幕 `BitBlt` 都只能抓到纯白
/// 窗口背景。这是引擎级限制，不是脚本 bug —— 意味着「截图看界面」这条路在
/// 本项目后续里程碑里一直不可靠。让页面自己报 computedStyle / 元素几何 /
/// 文本，是唯一能区分「真白屏」和「抓图失败」的手段。
///
/// 注意：不能用 `webview.eval()` 反向 invoke —— eval 跑在隔离世界，访问不到
/// 页面的 `window.__TAURI__`。必须由页面主世界自己调用本命令。
///
/// 用 `eprintln!`（stderr）而非 `println!`：stdout 在重定向到文件时是**块缓冲**，
/// 进程被 terminate 时缓冲区不会 flush，日志会整段丢失。stderr 无此问题。
#[tauri::command]
fn self_check(report: String) {
    eprintln!("[selfcheck] {report}");
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![app_info, demo_channels, self_check])
        .run(tauri::generate_context!())
        .expect("Tauri 启动失败");
}