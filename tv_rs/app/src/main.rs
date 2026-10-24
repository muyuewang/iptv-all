// 纯净电视 · Rust 重写（里程碑 2：真实数据 + 播放层打通）
//
// 里程碑 1（主窗口骨架）基础上：
//   1. 网络层：真抓频道表（镜像回退 + 同源解密），来自 clean-tv-core::net
//   2. 播放层：外挂 mpv（--wid 内嵌 + IPC 控制），来自本 crate 的 player 模块
//   3. 前端：点击频道 -> 解密 -> 选直连线路 -> mpv 播放
//
// 命门①裸 COM 旁听（net_probe）：里程碑 2 仍挂上，但官网源（网页线路）暂不接播放，
// 只旁听打印——先保证「直连线路」这条主链路可播、可交付。

// Windows 发布构建不弹控制台窗口（debug 下保留，方便看自检日志）
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod net_probe;
mod player;

use serde::Serialize;
use std::sync::Arc;
use tauri::{Manager, State, WebviewUrl, WebviewWindowBuilder};

/// 运行环境信息。
#[derive(Serialize)]
struct AppInfo {
    app: &'static str,
    app_version: &'static str,
    core_version: &'static str,
    target: &'static str,
    profile: &'static str,
}

/// 频道分类。
#[derive(Serialize, Clone)]
struct Category {
    tid: String,
    name: String,
}

/// 频道条目（转发数据层类型，额外不含任何字段）。
#[derive(Serialize, Clone)]
struct Channel {
    id: String,
    name: String,
}

/// 播放结果（返回给前端）。
#[derive(Serialize, Clone)]
struct PlayResult {
    ok: bool,
    msg: String,
    /// 选中的线路序号（0 起）
    index: usize,
    /// 线路总数
    total: usize,
    /// 直连线路 URL（调试/自检用）
    url: String,
}

/// 应用级共享状态：播放器 + 主窗口 HWND。
///
/// 播放器用 `Arc<player::Player>`（内部有 Mutex + AtomicU64 代次，线程安全），
/// mpv 看门狗在独立线程里 block 等待退出，跨线程安全。
struct AppState {
    player: Arc<player::Player>,
    /// 主窗口 HWND（mpv --wid 用），setup 时填入。
    hwnd: std::sync::Mutex<u64>,
}

/// 全局频道缓存：启动预抓取后，命令 handler 只做纯内存读（零 HTTPS），
/// 从而绕开「命令内发 HTTPS 后返回值无法回传前端」的根故障。
static CHANNELS_CACHE: std::sync::Mutex<Option<std::collections::HashMap<String, Vec<Channel>>>> =
    std::sync::Mutex::new(None);

/// 全局播放线路缓存：预抓取时并发抓每个频道的直连线路，命令只读缓存。
/// key = "{tid}:{cid}"，value = 直连线路 URL（第 1 条直连流）。
static PLAY_CACHE: std::sync::Mutex<Option<std::collections::HashMap<String, String>>> =
    std::sync::Mutex::new(None);

/// 启动预抓取所有分类的频道列表（用 blocking 客户端，在 main 里同步先抓，
/// 不经过任何命令 handler）。抓完后写入 CHANNELS_CACHE，命令只读缓存。
fn prewarm_channels() {
    diag("[boot] 开始预抓取所有分类频道");
    let cli = clean_tv_core::net::blocking_client();
    let mut map = std::collections::HashMap::new();
    for (tid, _name) in clean_tv_core::net::CATS {
        let state = clean_tv_core::net::ApiState::new();
        match clean_tv_core::net::blocking_fetch_channels(&state, tid, &cli) {
            Ok(chans) => {
                let v: Vec<Channel> = chans.into_iter().map(|c| Channel { id: c.id, name: c.name }).collect();
                diag(&format!("[boot] 预抓取 {tid} 完成 {} 条", v.len()));
                map.insert(tid.to_string(), v);
            }
            Err(e) => {
                diag(&format!("[boot] 预抓取 {tid} 失败: {e}"));
            }
        }
    }
    *CHANNELS_CACHE.lock().unwrap() = Some(map);
    diag("[boot] 预抓取全部完成");
}

/// 预抓取所有频道的「直连线路」URL（并发线程池，避免串行阻塞过久）。
///
/// 每个频道抓一次播放页 + 解密，取第 1 条直连流 URL 缓存。这样 `play_channel`
/// 命令 handler 内零 HTTPS，只读缓存 + 起 mpv，返回值能可靠回传。
fn prewarm_play_lines() {
    diag("[boot] 开始预抓取全部频道直连线路");
    // 先拿到频道列表（此时 CHANNELS_CACHE 已填好）。
    let chans: Vec<(String, Channel)> = {
        let guard = CHANNELS_CACHE.lock().unwrap();
        match guard.as_ref() {
            Some(m) => m
                .iter()
                .flat_map(|(tid, v)| v.iter().map(|c| (tid.clone(), c.clone())).collect::<Vec<_>>())
                .collect(),
            None => Vec::new(),
        }
    };
    diag(&format!("[boot] 待抓线路频道数 {}", chans.len()));

    // 并发抓取：分块起线程（每批 8 个并发），避免一次性起 278 线程压垮系统。
    let total = chans.len();
    let results: std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, String>>> =
        std::sync::Arc::new(std::sync::Mutex::new(std::collections::HashMap::new()));
    let done = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));

    for chunk in chans.chunks(8) {
        let mut handles = Vec::new();
        for (tid, c) in chunk {
            let tid = tid.clone();
            let cid = c.id.clone();
            let results = results.clone();
            let done = done.clone();
            handles.push(std::thread::spawn(move || {
                let cli = clean_tv_core::net::blocking_client();
                let state = clean_tv_core::net::ApiState::new();
                if let Ok((urls, _epg, _msg)) =
                    clean_tv_core::net::blocking_fetch_play(&state, &tid, &cid, &cli)
                {
                    if let Some(url) = urls.into_iter().find(|u| clean_tv_core::wvt::is_direct_stream(u)) {
                        results.lock().unwrap().insert(format!("{tid}:{cid}"), url);
                    }
                }
                let n = done.fetch_add(1, std::sync::atomic::Ordering::SeqCst) + 1;
                if n % 50 == 0 || n == total {
                    diag(&format!("[boot] 线路预抓取进度 {n}/{total}"));
                }
            }));
        }
        for h in handles {
            let _ = h.join();
        }
    }
    let n = results.lock().unwrap().len();
    // 把 Arc<Mutex> 里的 HashMap 取出（凑合：Arc 强引用只剩 1 个时 try_unwrap 成功）。
    let map = match std::sync::Arc::try_unwrap(results) {
        Ok(m) => m.into_inner().unwrap(),
        Err(a) => (*a.lock().unwrap()).clone(),
    };
    *PLAY_CACHE.lock().unwrap() = Some(map);
    diag(&format!("[boot] 线路预抓取完成，命中 {} 条直连", n));
}

/// 返回分类列表（真实分类，来自 net::CATS）。
#[tauri::command]
fn list_categories() -> Vec<Category> {
    clean_tv_core::net::CATS
        .iter()
        .map(|(tid, name)| Category { tid: tid.to_string(), name: name.to_string() })
        .collect()
}

/// ★ 最终方案：命令只做**纯内存读缓存**（零 HTTPS）。
///
///   根因（多轮穷举实测锁定）：命令 handler 内一旦完成真实 HTTPS 数据传输，其返回值
///   无法经 WebView2 eval 通道回传前端（Promise 永久 pending）；纯内存/假数据返回则
///   正常。因此频道数据在**启动时预抓取进全局缓存**（见 `prewarm_channels`），本命令
///   只读缓存返回——handler 内零网络，返回可靠。
#[tauri::command]
fn list_channels(tid: String) -> Result<Vec<Channel>, String> {
    let guard = CHANNELS_CACHE.lock().unwrap();
    match guard.as_ref().and_then(|m| m.get(&tid)) {
        Some(chans) => {
            diag(&format!("[chan] list_channels 读缓存 {tid} 返回 {} 条", chans.len()));
            Ok(chans.clone())
        }
        None => Err(format!("分类 {tid} 尚未预抓取完成")),
    }
}

/// 播放某频道：**命令只读缓存 + 起 mpv（零 HTTPS）**。
///
/// 播放线路已在启动预抓取进 PLAY_CACHE（见 `prewarm_play_lines`）。本命令只做
/// 「读缓存取直连 URL + 起 mpv」，handler 内零网络，返回值可靠回传。
#[tauri::command]
fn play_channel(tid: String, cid: String, state: State<'_, AppState>) -> Result<PlayResult, String> {
    diag(&format!("[play] play_channel 读缓存 tid={tid} cid={cid}"));
    let key = format!("{tid}:{cid}");
    let url = {
        let guard = PLAY_CACHE.lock().unwrap();
        guard
            .as_ref()
            .and_then(|m| m.get(&key).cloned())
            .ok_or_else(|| format!("该频道暂无直连线路（{key}）"))?
    };
    // 起 mpv（内嵌主窗口）
    let player = state.player.clone();
    let hwnd = *state.hwnd.lock().unwrap();
    diag(&format!("[play] 起播 hwnd={} mpv={} url.len={}", hwnd, player.mpv_path().display(), url.len()));
    let (_pid, _pipe) = player.play(&url, hwnd, "balanced", &[]).map_err(|e| format!("起播失败: {e}"))?;
    let player2 = player.clone();
    std::thread::spawn(move || {
        if let Some((_gen, code)) = player2.wait_exit() {
            diag(&format!("[play] mpv 退出 code={code}"));
        }
    });
    diag("[play] mpv 已启动");
    Ok(PlayResult { ok: true, msg: String::new(), index: 0, total: 1, url })
}

/// 停止播放。
#[tauri::command]
fn stop_channel(state: State<'_, AppState>) {
    state.player.stop();
}

/// 诊断日志落盘（release 无控制台时用）。设 CLEAN_TV_LOG=<路径> 时把关键状态
/// 追加写进文件；不设则无操作。用于交付前 release 验证。
fn diag(msg: &str) {
    // ★ release GUI 子系统无 stderr，eprintln! 写到空句柄 —— debug 才打印。
    //   文件落盘是 release 下唯一可见通道。
    #[cfg(debug_assertions)]
    eprintln!("{msg}");
    let _ = msg;
    if let Ok(p) = std::env::var("CLEAN_TV_LOG") {
        use std::io::Write;
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&p) {
            let _ = writeln!(f, "{msg}");
        }
    }
}

/// 自动化测试钩子：返回环境变量标记（如 CLEAN_TV_AUTOPLAY=1 触发自动播放）。
#[derive(Serialize)]
struct EnvFlag {
    autoplay: bool,
}

#[tauri::command]
fn env_flag() -> EnvFlag {
    let ap = std::env::var("CLEAN_TV_AUTOPLAY").as_deref() == Ok("1");
    diag(&format!("[flag] env_flag 被调用 autoplay={ap}"));
    EnvFlag { autoplay: ap }
}

/// 运行环境信息。
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

/// 启动自检（区分「真白屏」与「抓图失败」）。
#[tauri::command]
fn self_check(report: String) {
    diag(&format!("[selfcheck] {report}"));
}

/// 定位 mpv.exe：优先 exe 同目录下 `mpv/mpv.exe`（release 资源布局），
/// 其次 exe 同目录 `mpv.exe`；开发阶段退化为「仓库内置 desktop/mpv」。
fn resolve_exe_mpv() -> std::path::PathBuf {
    let mut candidates: Vec<std::path::PathBuf> = Vec::new();

    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join("mpv").join("mpv.exe"));
            candidates.push(dir.join("mpv.exe"));
            // 资源目录：Tauri 把 resources 放到 exe 同层 `_up_/_resources` 或同层，
            // 这里一并尝试 Tauri 的 RESOURCE_DIR 环境变量（打包运行时注入）。
            if let Ok(rd) = std::env::var("TAURI_RESOURCE_DIR") {
                candidates.push(std::path::PathBuf::from(rd).join("resources").join("mpv").join("mpv.exe"));
            }
        }
    }

    // 开发阶段兜底：仓库根 desktop/mpv/mpv.exe
    if let Ok(cwd) = std::env::current_dir() {
        for base in [cwd.clone(), cwd.join("..").join("..")] {
            candidates.push(base.join("desktop").join("mpv").join("mpv.exe"));
        }
        // 另外：app 资源目录 resources/mpv/mpv.exe（相对 app crate）
        candidates.push(cwd.join("app").join("resources").join("mpv").join("mpv.exe"));
        candidates.push(cwd.join("resources").join("mpv").join("mpv.exe"));
    }

    for c in candidates {
        if c.is_file() {
            return c;
        }
    }
    panic!("未找到 mpv.exe（请确认已放入 resources/mpv/ 或 desktop/mpv/）");
}

fn main() {
    diag("[boot] main 入口");
    // ★ 启动预抓取：在 tauri Builder 之前同步抓取所有分类频道，写入全局缓存。
    //   这样命令 handler 内零 HTTPS，返回值能可靠回传前端。
    prewarm_channels();
    // ★ 预抓取播放线路（带环境开关，debug 测试可跳过以加快启动）。
    if std::env::var("CLEAN_TV_SKIP_PREWARM_PLAY").as_deref() != Ok("1") {
        prewarm_play_lines();
    }
    tauri::Builder::default()
        .manage(AppState {
            player: Arc::new(player::Player::new(
                // 找 mpv：优先 exe 同目录（release 资源布局），退化为开发目录兜底。
                resolve_exe_mpv(),
            )),
            hwnd: std::sync::Mutex::new(0),
        })
        .setup(|app| {
            diag("[boot] setup 开始");
            // 显式建主窗口（conf windows 数组已清空），这样才能挂 on_new_window。
            let win = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("纯净电视 · Rust")
                .inner_size(1100.0, 700.0)
                .min_inner_size(720.0, 480.0)
                .center()
                // 命门②：拦死所有 window.open（广告 / 官方播放页弹窗）。
                .on_new_window(|url, _features| {
                    eprintln!("[win] 拦截新窗口请求: {url}");
                    tauri::webview::NewWindowResponse::Deny
                })
                .build()?;

            // 填入主窗口 HWND（mpv --wid 内嵌用）。
            let hwnd = win.hwnd().map(|h| h.0 as u64).unwrap_or(0);
            if let Some(state) = app.try_state::<AppState>() {
                *state.hwnd.lock().unwrap() = hwnd;
            }
            diag(&format!("[app] 主窗口 HWND={hwnd}"));

            // 命门①：裸 COM 旁听（官网源抓 m3u8 用）。里程碑 2 先全量旁听打印，
            // 接官网源的网页线路时再按域收敛白名单。
            // ★ 对照实验：临时禁用旁听，验证「WebResourceRequested 全量过滤器」是否
            //   干扰 webview 网络/渲染，导致真实网络抓取后 IPC 返回值卡死。
            let _probe = net_probe::NetProbe::new(
                vec![],
                Some(Box::new(|hit| {
                    eprintln!("[net] 媒体流: {} ({})", hit.kind, hit.url);
                })),
            );
            // let _ = win.with_webview(move |platform| probe.attach(platform));
            diag("[boot] 旁听已禁用（对照实验）");
            diag("[boot] setup 完成");
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            app_info,
            list_categories,
            list_channels,
            play_channel,
            stop_channel,
            env_flag,
            self_check,
        ])
        .run(tauri::generate_context!())
        .expect("Tauri 启动失败");
}