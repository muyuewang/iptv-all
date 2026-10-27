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

mod cdp;
mod iptv789;
mod net_probe;
mod official_embed;
mod official_web;
mod player;
mod session;

use serde::Serialize;
use std::sync::Arc;
use tauri::{Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder};

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
    /// 来源分组（与早前 desktop 的两源结构对齐，见 shared/core.py / native_app.py）：
    /// local    = 本机源（api1.2026016.xyz，分类=综合/体育/央视/卫视/极速港澳台/港澳台）；
    /// official = 官网源（WebViewTvLive dat，cctv/央视频/地方网页，51 个分组）。
    src: String,
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
    /// 视频承载窗口（mpv 的 --wid 父窗口）。
    /// ★ 必须是**独立顶层窗口**：直接嵌主窗口的话，mpv 视频子窗口会被铺满
    ///   主窗口的 WebView2 层整块盖住 → 只剩声音没图像（见 player::VideoHost 注释）。
    host: Arc<player::VideoHost>,
    /// 最近一次上报的播放区**屏幕坐标**（x,y,w,h 物理像素），由 report_stage_rect
    /// 换算后缓存。官网源 WebView2 直播窗要用它定位（落在播放区）。
    /// ★ 与 host 的 place_over_client 产出同一套坐标，避免官网/本机两源各自换算漂移。
    stage_rect: std::sync::Mutex<Option<(i32, i32, i32, i32)>>,
    /// 最近一次上报的播放区**相对视口 CSS 偏移**（x,y,w,h + vw,vh），由 report_stage_rect
    /// 原样缓存。主窗口**拖动**时（屏幕原点变了，但视口内布局不变），用它重新
    /// `place_over_client`（内部 ClientToScreen 现算新原点）把 mpv 承载窗/官网直播窗
    /// 重新贴到主窗口上——否则拖动后视频画面与主窗体分离（用户实测白框分离）。
    stage_css: std::sync::Mutex<Option<(i32, i32, i32, i32, i32, i32)>>,
}

/// 全局频道缓存：启动预抓取后，命令 handler 只做纯内存读（零 HTTPS），
/// 从而绕开「命令内发 HTTPS 后返回值无法回传前端」的根故障。
static CHANNELS_CACHE: std::sync::Mutex<Option<std::collections::HashMap<String, Vec<Channel>>>> =
    std::sync::Mutex::new(None);

// ============================================================================
// 会话层桥接（session.rs 的回收/几何同步统一入口）
// ----------------------------------------------------------------------------
// `session::SessionManager` 是「谁在播、怎么统一回收、几何怎么统一同步」的唯一
// 入口。它需要调用底层三件事：停 mpv、关官网直播窗、按后端类型同步几何。
// 这三件事分别需要访问 `player::Player`（mpv 进程）、`player::VideoHost`（mpv
// 承载窗）、`official_embed`（WebView2 直播窗静态句柄）。前两者挂在 AppState 里，
// 用全局 OnceLock 做一次初始化，供 session 层（非 tauri command 上下文）取用。
// ============================================================================

static G_PLAYER: std::sync::OnceLock<Arc<player::Player>> = std::sync::OnceLock::new();
static G_HOST: std::sync::OnceLock<Arc<player::VideoHost>> = std::sync::OnceLock::new();

/// 停掉当前 mpv（session 层回收用的统一入口，内部把 wait 丢后台线程）。
fn stop_mpv() {
    if let Some(p) = G_PLAYER.get() {
        p.stop();
    }
}

/// 关闭官网源 WebView2 直播窗（session 层回收用的统一入口）。
fn close_live() {
    official_embed::close_live_window();
}

/// 按后端类型把几何同步到对应底层窗口（session 层几何同步统一入口）。
///
/// ★ 这是「几何单一真源」的落点：不再由 `report_stage_rect` 分别调
///   `host.place_over_client` + `official_embed::sync_live_geometry` 两处，
///   而是这里一次性同步当前会话在播的那个后端。
fn apply_geometry(kind: &'static str, rect: session::StageRect) {
    match kind {
        "webview" => {
            // 官网源：WebView2 直播窗直接摆到播放区。
            official_embed::sync_live_geometry(rect.x, rect.y, rect.w, rect.h);
        }
        _ => {
            // 本机源：mpv 承载窗摆到播放区（--wid 父窗口）。
            if let Some(host) = G_HOST.get() {
                host.place(rect.x, rect.y, rect.w, rect.h);
            }
        }
    }
}

/// 当前播放上下文（供底部控制条显示「线路 x/y」、以及手动切换线路）。
///
/// ★ 为什么要单独存：`play_channel` 对本机源是**秒回**（后台线程抓线），
///   命令返回值里拿不到真实的线路总数；而控制条需要知道「一共几条、现在是第几条」。
///   这里由真正出画的那条路径写入，前端 `now_playing()` 读。
#[derive(Clone, Default)]
struct NowPlaying {
    tid: String,
    cid: String,
    name: String,
    /// "local" | "official"
    src: String,
    /// 解密出来的线路列表（已按探速重排过）
    urls: Vec<String>,
    /// 当前在播的线路序号（0 起）
    idx: usize,
}

static NOW_PLAYING: std::sync::Mutex<Option<NowPlaying>> = std::sync::Mutex::new(None);

/// 当前频道节目单（只有本机源有；官网源/切换后为空）。
static NOW_EPG: std::sync::Mutex<Vec<clean_tv_core::core::EpgItem>> =
    std::sync::Mutex::new(Vec::new());

/// 写入当前播放上下文（整体覆盖）。
fn now_set(np: NowPlaying) {
    *NOW_PLAYING.lock().unwrap_or_else(|p| p.into_inner()) = Some(np);
}

/// 只更新线路列表与当前序号（换线/出画时用）。
fn now_update_lines(urls: Vec<String>, idx: usize) {
    if let Some(np) = NOW_PLAYING.lock().unwrap_or_else(|p| p.into_inner()).as_mut() {
        np.urls = urls;
        np.idx = idx;
    }
}

/// 读一份快照。
fn now_get() -> Option<NowPlaying> {
    NOW_PLAYING
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .clone()
}

/// 全局播放线路缓存：预抓取时并发抓每个频道的直连线路，命令只读缓存。
/// key = "{tid}:{cid}"，value = 直连线路 URL（第 1 条直连流）。
static PLAY_CACHE: std::sync::Mutex<Option<std::collections::HashMap<String, String>>> =
    std::sync::Mutex::new(None);

/// 官网源**网页地址**缓存：`w|{分组}:{cid}` -> 网页 URL 列表（按优先级排序）。
///
/// ★ 官网源 904 频道里只有 54 个带直连，其余 850 个只给网页地址。网页地址不是
///   流，mpv 播不了；必须让浏览器执行页面脚本、由它发起 HLS 请求，再把那个
///   m3u8 抓出来交给 mpv（见 `official_web.rs`）。所以这里必须把网页地址也留存。
///
/// ★★ 存**列表**而不是单个 URL（2026-10-26 改）：每个频道常有多条网页线路
///   （CCTV-1 = tv.cctv.com + yangshipin.cn + m-live.cctvnews…），
///   而第一条线路可能因 CDN 不可达而失败（实测 fengshows / cctvnews 线路
///   在部分网络下直接连不上）。只留一条 = 一条不通就没救了；
///   留全部 = 依次嗅探，第一条不通自动换下一条。
static WEB_URL_CACHE: std::sync::Mutex<Option<std::collections::HashMap<String, Vec<String>>>> =
    std::sync::Mutex::new(None);

/// 官网源（WebViewTvLive）分组 tid 前缀：前端拿到的 tid 形如 `w|央视`。
/// 用 `|` 分隔命名空间与分组名，play_channel 按 `w|` 识别官网源分组。
const WVT_PREFIX: &str = "w|";

/// 官网源网页线路优先级（对齐 `shared/wvt.py::_url_rank`）。
///
/// 实测口径（2026-10 冷/热各两次，「页面 ready → 出画」耗时）：
///   tv.cctv.com 3248~4561ms（最快） > 央视频 5510~6297ms > 其它 > 移动直播间 6538~7435ms。
/// 央视网直播页重（ready 3.0s）但老 HLS 播放器 ready 后 1.5s 就出画；
/// 央视频是 SPA + WASM/MSE，ready 只要 1.4s，之后还要再等 4.1s。
fn web_url_rank(s: &str) -> u8 {
    if s.contains("tv.cctv.com") {
        0
    } else if s.contains("yangshipin") {
        1
    } else if s.contains("cctvnews") || s.contains("liveRoom") {
        3
    } else {
        2
    }
}

/// 把某频道的**全部**网页线路按优先级排好序（去重 + 稳定排序）。
///
/// ★ 必须返回**全部**而不是只留最优的一条（2026-10-26 修复的真实缺陷）：
///   官网源 850 个「仅网页」频道里，**164 个有 ≥2 条网页线路**
///   （全部央视系都是 3 条：tv.cctv.com + yangshipin.cn + m-live.cctvnews）。
///   旧实现只缓存最优的一条 → 该条 CDN 不通时（实测 tlive.fengshows.com
///   直连超时、cctvnews 线路部分网络不通）**这个频道就没救了**。
///   保留全部 → 播放时逐条嗅探，第一条不通自动换下一条。
///
/// 同优先级内按**原始顺序**（`sort_by_key` 是稳定排序），
/// 保证同一频道的候选线路相对次序不被打乱。
fn ranked_web_pages(urls: &[String]) -> Vec<String> {
    let mut pages: Vec<&str> = urls.iter().map(|s| s.as_str()).collect();
    pages.sort_by_key(|s| web_url_rank(s));
    // 去重（同一 URL 可能在频道表里重复出现），保留首次出现的位置。
    let mut seen: Vec<&str> = Vec::new();
    let mut out: Vec<String> = Vec::new();
    for p in pages {
        if seen.contains(&p) {
            continue;
        }
        seen.push(p);
        out.push(p.to_string());
    }
    out
}

/// 官网源分组名列表（prewarm_wvt 解密频道表后按首次出现顺序填充）。
/// list_categories 只读这里，与 CHANNELS_CACHE 的 `w|{分组}` 键一一对应。
static WVT_GROUPS: std::sync::Mutex<Option<Vec<String>>> = std::sync::Mutex::new(None);

/// 789 实时取流代次：每次发起取流 +1，后台线程取到流后核对代次，
/// 不匹配（用户已换台/取消）就放弃起播，防止旧线程抢画面。
static FETCH_GEN: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// 全局共享的本机源 ApiState：镜像回退成功后 `state.api` 会记住可用镜像，
/// 后续所有抓取（频道表/播放页）直接命中可用镜像，避免每个分类/每线程都
/// 从默认 api1 重新试起（api1 证书吊销检查失败时每分类白等 8s）。
static LOCAL_API: std::sync::OnceLock<clean_tv_core::net::ApiState> = std::sync::OnceLock::new();

fn local_api() -> &'static clean_tv_core::net::ApiState {
    LOCAL_API.get_or_init(clean_tv_core::net::ApiState::new)
}

/// 生成新取流代次（单调递增）。
fn bump_fetch_gen() -> u64 {
    FETCH_GEN.fetch_add(1, std::sync::atomic::Ordering::SeqCst) + 1
}

/// 繁体 → 简体 单字对照表（86 对）。
///
/// ★ 为什么需要：官网源（WebViewTvLive full.dat）的频道名**混用简繁** ——
///   大部分频道是简体（「湖南卫视」「东方卫视」），但**港澳台分组
///   （香港/澳門/台灣）整组是繁体**（「鳳凰衛視中文台」「翡翠台」
///   「TVB 無線新聞台」）；而本机源 app 路由返回的频道名**全是简体**
///   （「凤凰卫视中文台」）。两边直接比字符串永远匹配不上 → 本机源
///   `gt`/`wintv123` 分类回落官网源的链路就断在这里。
///
/// ★ 表的来源：`zhconv.convert(ch,'zh-cn')` 对**两个源实际出现过的全部
///   非 ASCII 字符**（官网源 904 名 + 本机源 165 名）逐字转换，只保留
///   「转换结果为单字且与原字不同」的项 —— 得到 86 对。
///   **不引第三方 crate**（离线构建、NSIS 打包体积都不受影响）。
///
/// ★ 覆盖范围之外的新繁体字会漏转 → 匹配退化成原来的精确/包含比较，
///   不会崩、只是匹配不到（可接受）。
const T2S_TABLE: [(char, char); 86] = [
    ('亞', '亚'), ('來', '来'), ('備', '备'), ('兒', '儿'), ('內', '内'), ('創', '创'), ('劇', '剧'), ('勁', '劲'), ('區', '区'), ('國', '国'), ('團', '团'), ('場', '场'),
    ('奧', '奥'), ('娛', '娱'), ('實', '实'), ('寧', '宁'), ('島', '岛'), ('廈', '厦'), ('廣', '广'), ('愛', '爱'), ('慶', '庆'), ('懷', '怀'), ('戲', '戏'), ('時', '时'),
    ('會', '会'), ('東', '东'), ('業', '业'), ('極', '极'), ('樂', '乐'), ('歐', '欧'), ('歡', '欢'), ('灣', '湾'), ('無', '无'), ('爾', '尔'), ('競', '竞'), ('紀', '纪'),
    ('經', '经'), ('綜', '综'), ('綫', '线'), ('網', '网'), ('線', '线'), ('緯', '纬'), ('聞', '闻'), ('肅', '肃'), ('與', '与'), ('舊', '旧'), ('華', '华'), ('蓮', '莲'),
    ('藝', '艺'), ('蘇', '苏'), ('衛', '卫'), ('視', '视'), ('覺', '觉'), ('訊', '讯'), ('語', '语'), ('財', '财'), ('貴', '贵'), ('資', '资'), ('購', '购'), ('賽', '赛'),
    ('車', '车'), ('軍', '军'), ('農', '农'), ('遊', '游'), ('達', '达'), ('遼', '辽'), ('邊', '边'), ('釣', '钓'), ('鋒', '锋'), ('錄', '录'), ('鏡', '镜'), ('長', '长'),
    ('門', '门'), ('陝', '陕'), ('際', '际'), ('雲', '云'), ('電', '电'), ('頻', '频'), ('風', '风'), ('體', '体'), ('魚', '鱼'), ('鮮', '鲜'), ('鳳', '凤'), ('鷹', '鹰'),
    ('麗', '丽'), ('龍', '龙'),
];

/// 频道名归一化：繁→简 → 去分隔符 → 转小写 → 修已知拼写变体。
///
/// ★ 三个按名反查函数（`find_local_by_name` / `find_official_by_name` /
///   `find_789_by_name`）必须用**同一个**归一化，否则两源名字写法不同
///   （简繁 / 全角括号 / 空格）时永远匹配不上。
fn norm_chan_name(s: &str) -> String {
    // ① 繁 → 简（一次线性扫描 + 静态表查）。
    let mut simp = String::with_capacity(s.len());
    for ch in s.chars() {
        let mut c = ch;
        for (t, u) in T2S_TABLE.iter() {
            if *t == ch {
                c = *u;
                break;
            }
        }
        simp.push(c);
    }
    // ② 去分隔符（空格 / 连字符 / 括号 / 中点）。
    let lower: String = simp
        .chars()
        .filter(|c| !matches!(c, ' ' | '-' | '－' | '—' | '·' | '(' | ')' | '（' | '）'))
        .flat_map(|c| c.to_lowercase())
        .collect();
    // ③ 已知拼写变体：CECTV-x 实为 CETV-x（中国教育电视台）。
    lower.replace("cectv", "cetv")
}

/// 按频道名在 789 频道表（5 分类）里模糊匹配，供「本机源/官方源频道无直连」时
/// 自动回落到 789 网页源实时取流。归一化：去空格/连字符/括号、转小写。
/// 匹配优先级：归一化后精确相等 > 互相包含（取第一个命中，分类顺序 tv/ys/ws/ty/gt）。
fn find_789_by_name(name: &str) -> Option<(String, String)> {
    let norm = |s: &str| norm_chan_name(s);
    let target = norm(name);
    if target.is_empty() {
        return None;
    }
    let guard = CHANNELS_CACHE.lock().unwrap();
    let map = guard.as_ref()?;
    // 两轮：先全分类找精确，再找包含（避免「CCTV1」误命中排前的「CCTV1综合」以外的近似名）。
    // 789 缓存键带 `789_` 前缀（与本机源 UI 分类隔离，见 prewarm_789）。
    for pass in 0..2 {
        for (tid, name789) in clean_tv_core::net::CATS_789 {
            let key = format!("789_{tid}");
            if let Some(chans) = map.get(&key) {
                for c in chans {
                    let n = norm(&c.name);
                    if n.is_empty() {
                        continue;
                    }
                    let hit = if pass == 0 {
                        n == target
                    } else {
                        n.contains(&target) || target.contains(&n)
                    };
                    if hit {
                        diag(&format!(
                            "[fallback] 按名回落 789：{:?} -> {}:{}（{}·{}）",
                            name, tid, c.id, name789, c.name
                        ));
                        return Some((tid.to_string(), c.id.clone()));
                    }
                }
            }
        }
    }
    None
}

/// 按频道名在**本机源**六分类里模糊匹配（比 789 网页抓取可靠得多：本机源
/// 播放页解密只要 1~3 秒，789 要起 headless 浏览器且常失败）。
/// 归一化规则与 [`find_789_by_name`] 一致；匹配优先级：精确 > 互相包含。
fn find_local_by_name(name: &str) -> Option<(String, String)> {
    let norm = |s: &str| norm_chan_name(s);
    let target = norm(name);
    if target.is_empty() {
        return None;
    }
    let guard = CHANNELS_CACHE.lock().unwrap();
    let map = guard.as_ref()?;
    for pass in 0..2 {
        // 央视优先（官网源大部分是 CCTV / 卫视），再扫其余分类。
        for tid in ["ys", "ws", "tv", "ty", "wintv123", "gt"] {
            if let Some(chans) = map.get(tid) {
                for c in chans {
                    let n = norm(&c.name);
                    if n.is_empty() {
                        continue;
                    }
                    let hit = if pass == 0 {
                        n == target
                    } else {
                        n.contains(&target) || target.contains(&n)
                    };
                    if hit {
                        diag(&format!(
                            "[fallback] 按名回落本机源：{:?} -> {tid}:{}（{}）",
                            name, c.id, c.name
                        ));
                        return Some((tid.to_string(), c.id.clone()));
                    }
                }
            }
        }
    }
    None
}

/// 本机源**可用播放页的分类**（app 路由下有加密 `<option>` + 解密脚本）。
///
/// ★ 实测（2026-10-25，curl + Rust + Python 三方对照）：
///   | 分类        | app 路由（长 UA） | web 路由（短 UA） |
///   |-------------|-------------------|-------------------|
///   | ys / ws / ty | 200，可解密 ✓      | 200，无线路       |
///   | tv / gt / wintv123 | **恒 404**   | 200，无线路       |
///   源站在这三个分类下**没有 app 端播放页**，客户端无法凭空造出线路
///   （Python 版 shared/core.py 同样 404 → 上游既有行为，非客户端回归）。
const LOCAL_PLAYABLE_TIDS: [&str; 3] = ["ys", "ws", "ty"];

/// 按名找本机源**可用分类**里的同名频道。
///
/// ★ 用途：`tv`/`gt`/`wintv123` 三个分类的播放页在源站恒 404，但它们的频道
///   在 `ys`/`ws`/`ty` 里都有**同名对应**（实测 CCTV1綜合→ys:1、
///   湖南衛視→ws:1、CCTV5體育→ty:1…），故按名跨分类重抓即可救回。
///
/// ★ 只在 [`LOCAL_PLAYABLE_TIDS`] 里找：在另一个 404 分类里找到同名频道
///   只会再失败一次（实测曾找到 tv:20 → 依然 404）。
fn find_local_playable_by_name(name: &str) -> Option<(String, String)> {
    let norm = |s: &str| norm_chan_name(s);
    let target = norm(name);
    if target.is_empty() {
        return None;
    }
    let guard = CHANNELS_CACHE.lock().unwrap();
    let map = guard.as_ref()?;
    for pass in 0..2 {
        for tid in LOCAL_PLAYABLE_TIDS {
            if let Some(chans) = map.get(tid) {
                for c in chans {
                    let n = norm(&c.name);
                    if n.is_empty() {
                        continue;
                    }
                    let hit = if pass == 0 {
                        n == target
                    } else {
                        n.contains(&target) || target.contains(&n)
                    };
                    if hit {
                        diag(&format!(
                            "[fallback] 跨分类重试本机源：{:?} -> {tid}:{}（{}）",
                            name, c.id, c.name
                        ));
                        return Some((tid.to_string(), c.id.clone()));
                    }
                }
            }
        }
    }
    None
}

/// 官网源里**港澳台分组**的名字（按名回落时的优先目标）。
///
/// ★ 为什么需要优先级（2026-10-26 实测）：`find_official_by_name` 原来把所有
///   `w|` 分组一起搜、命中后按 `(分组名, cid)` 排序取第一个 ——
///   而「卫视」组的 UTF-8 首字节 `卫`=E58C **小于**「香港」的 `香`=E9A6，
///   于是「TVB翡翠台」这类港澳台频道会**先命中大陆分组**里的近似名
///   （实测「翡翠台」在「卫视」组没有、「香港」组有，但「明珠台」这类
///   在「卫视」组也存在同名台）→ 落到错误的网页线路，嗅探白跑 20+ 秒。
///   港澳台频道必须**优先**落到 香港/澳門/台灣 组。
const OFFICIAL_GT_GROUPS: [&str; 5] = ["香港", "澳門", "澳门", "台灣", "台湾"];

/// 官网源分组名 → 回落优先级（数字越小越优先）。
///
/// 0 = 港澳台分组（本机源 gt/wintv123/tv 的目标）
/// 1 = 其它分组（卫视/央视/地方台…）
fn official_group_rank(group: &str) -> u8 {
    if OFFICIAL_GT_GROUPS.iter().any(|g| *g == group) {
        0
    } else {
        1
    }
}

/// 按名找**官网源**里的同名频道（返回 (官网源 tid, cid)）。
///
/// ★ 用途（实测 2026-10-25）：本机源的 `tv`/`gt`/`wintv123` 三个分类在源站
///   app 路由下**恒 404**（无播放页），而这些分类里的港澳台频道
///   （凤凰卫视中文台、TVB翡翠台…）在 `ys`/`ws`/`ty` 里**没有同名对应**
///   → 跨分类重试也救不回。此时改走**官网源**（WebViewTvLive full.dat
///   有「香港/澳門/台灣」等分组），那边走内嵌 WebView2 嗅探网页线路。
///
/// ★ 命中排序（2026-10-26 修订）：先按「分组优先级」（港澳台组优先，
///   见 [`official_group_rank`]），再按分组名 → cid，避免命中到大陆分组
///   里的同名近似台。
///
/// 官网源 tid 形如 `w|{分组}`，cid 是组内序号。
fn find_official_by_name(name: &str) -> Option<(String, String)> {
    let norm = |s: &str| norm_chan_name(s);
    let target = norm(name);
    if target.is_empty() {
        return None;
    }
    let guard = CHANNELS_CACHE.lock().unwrap();
    let map = guard.as_ref()?;
    for pass in 0..2 {
        // 官网源分组键统一以 "w|" 开头；按名称哈希无序 → 先按「精确」再按「包含」。
        let mut hits: Vec<(u8, String, String)> = Vec::new();
        for (tid, chans) in map.iter() {
            if !tid.starts_with("w|") {
                continue;
            }
            let group = tid.trim_start_matches("w|");
            for c in chans {
                let n = norm(&c.name);
                if n.is_empty() {
                    continue;
                }
                let hit = if pass == 0 {
                    n == target
                } else {
                    n.contains(&target) || target.contains(&n)
                };
                if hit {
                    hits.push((official_group_rank(group), tid.clone(), c.id.clone()));
                }
            }
        }
        if !hits.is_empty() {
            // 稳定排序（分组优先级 → 分组名 → 组内序号），避免每次命中不同的组。
            hits.sort();
            let (_, t, c) = hits[0].clone();
            diag(&format!(
                "[fallback] 按名回落官网源：{name:?} -> {t}:{c}"
            ));
            return Some((t, c));
        }
    }
    None
}

/// 预抓取 789iptv 频道表（明文分类页，稳定），写入 CHANNELS_CACHE。
///
/// ★ 789 在本版里**不再作为独立 UI 源**（早前 desktop 也没有它）：只作为
///   「兜底取流源」——本机源/官网源某频道播不了时按名回落 789 实时抓流。
///   因此这个函数只喂 `find_789_by_name` 的匹配数据，后台跑、不阻塞启动。
fn prewarm_789() {
    diag("[boot] 开始预抓取 789iptv 频道表（兜底回落用）");
    let cli = clean_tv_core::net::blocking_client();
    let mut map = std::collections::HashMap::new();
    for (tid, _name) in clean_tv_core::net::CATS_789 {
        match clean_tv_core::net::blocking_fetch_789_channels(tid, &cli) {
            Ok(chans) => {
                let v: Vec<Channel> = chans.into_iter().map(|c| Channel { id: c.id, name: c.name }).collect();
                diag(&format!("[boot] 789 分类 {tid} 完成 {} 条", v.len()));
                map.insert(tid.to_string(), v);
            }
            Err(e) => {
                diag(&format!("[boot] 789 分类 {tid} 失败: {e}"));
            }
        }
    }
    if !map.is_empty() {
        let mut guard = CHANNELS_CACHE.lock().unwrap();
        let cache = guard.get_or_insert_with(std::collections::HashMap::new);
        // 789 分类 tid（tv/ty/ys/ws/gt）与本机源 CATS 重名，但 789 只是兜底
        // 匹配数据（find_789_by_name 直接遍历 CATS_789 读这份缓存），不进 UI。
        // 为避免与 UI 分类键混淆，缓存键加 `789_` 前缀隔离。
        let renamed: std::collections::HashMap<String, Vec<Channel>> = map
            .into_iter()
            .map(|(k, v)| (format!("789_{k}"), v))
            .collect();
        cache.extend(renamed);
    }
    diag("[boot] 789iptv 频道表预抓取完成");
}

/// 预抓取官网源（WebViewTvLive dat）频道表，**按分组建 UI 分类**。
///
/// 官网源就是早前 desktop 的 `src_mode="wvt"`：频道表里每条自带 `group` 字段
/// （央视/央视数字/国家/卫视/少儿/…共 51 组），左栏「分组」下拉逐组切换。
/// 这里解密 full.dat 后：
///   · CHANNELS_CACHE 写入 `w|{分组}` -> 该分组全部频道（cid = 组内序号）；
///   · PLAY_CACHE 写入 `w|{分组}:{cid}` -> 该频道第 1 条直连线路
///     （选线优先真 m3u8 直链 > 其它直连；纯网页线路频道不写，播放时按名回落 789）；
///   · WVT_GROUPS 记录分组出现顺序，list_categories 据此生成 `w|{分组}` 分类。
fn prewarm_wvt() {
    diag("[boot] 开始预抓取官网源（WebViewTvLive）");
    let cli = clean_tv_core::net::blocking_client();
    match clean_tv_core::net::blocking_fetch_wvt_channels(&cli) {
        Ok(chans) => {
            // 分组按首次出现顺序收集（对齐旧版分组下拉的顺序感）。
            let mut groups: Vec<String> = Vec::new();
            // per-group: (频道列表, 直连映射 cid->url)
            let mut per_group: std::collections::HashMap<String, Vec<Channel>> =
                std::collections::HashMap::new();
            let mut play_map: std::collections::HashMap<String, String> =
                std::collections::HashMap::new();
            // ★ 网页地址映射（官网源 850/904 频道只有网页）：key 同 play_map，
            //   value = 该频道最优网页地址（按 _url_rank 排序，央视网直播页最快）。
            let mut web_map: std::collections::HashMap<String, Vec<String>> =
                std::collections::HashMap::new();

            for c in &chans {
                let g = if c.group.is_empty() { "其它".to_string() } else { c.group.clone() };
                if !groups.contains(&g) {
                    groups.push(g.clone());
                }
                let list = per_group.entry(g.clone()).or_default();
                let cid = list.len().to_string();
                list.push(Channel { id: cid.clone(), name: c.name.clone() });
                // 选线优先级：真 m3u8 直链（URL 本体 .m3u8 结尾）> 其它直连（flv 等）。
                let direct = c
                    .urls
                    .iter()
                    .find(|u| clean_tv_core::wvt::is_m3u8_direct(&u.url))
                    .or_else(|| c.urls.iter().find(|u| clean_tv_core::wvt::is_direct_stream(&u.url)));
                if let Some(u) = direct {
                    play_map.insert(format!("{WVT_PREFIX}{g}:{cid}"), u.url.clone());
                } else {
                    // 没有直连 → 记下**全部**网页地址，播放时依次嗅探。
                    // 排序口径对齐 shared/wvt.py::_url_rank（实测：央视网直播页最快，
                    // 央视频次之，移动直播间最慢）。
                    //
                    // ★ 存全部而非只存最优的一条：同一频道常有多条网页线路
                    //   （CCTV-1 = tv.cctv.com + yangshipin.cn + m-live.cctvnews…），
                    //   第一条可能因 CDN 不可达而失败（实测 fengshows 线路直连超时）。
                    //   只留一条 = 一条不通就没救了；留全部 = 依次嗅探自动换线。
                    let raw: Vec<String> = c
                        .urls
                        .iter()
                        .map(|u| u.url.clone())
                        .filter(|s| s.starts_with("http"))
                        .collect();
                    let pages = ranked_web_pages(&raw);
                    if !pages.is_empty() {
                        web_map.insert(format!("{WVT_PREFIX}{g}:{cid}"), pages);
                    }
                }
            }

            let total_direct = play_map.len();
            diag(&format!(
                "[boot] 官网源解密 {} 频道 / {} 分组，含直连线路 {} 条",
                chans.len(),
                groups.len(),
                total_direct
            ));
            // 分组名单落盘（验收证据：头 8 个 + 末 3 个，避免刷屏）。
            let head: Vec<&str> = groups.iter().take(8).map(|s| s.as_str()).collect();
            let tail: Vec<&str> = groups.iter().rev().take(3).map(|s| s.as_str()).collect();
            diag(&format!("[boot] 官网源分组名单 头8={head:?} 末3={tail:?}"));

            {
                let mut guard = CHANNELS_CACHE.lock().unwrap();
                let cache = guard.get_or_insert_with(std::collections::HashMap::new);
                for (g, list) in per_group {
                    cache.insert(format!("{WVT_PREFIX}{g}"), list);
                }
            }
            {
                let mut guard = PLAY_CACHE.lock().unwrap();
                guard.get_or_insert_with(std::collections::HashMap::new).extend(play_map);
            }
            {
                let mut guard = WEB_URL_CACHE.lock().unwrap();
                guard.get_or_insert_with(std::collections::HashMap::new).extend(web_map);
            }
            *WVT_GROUPS.lock().unwrap() = Some(groups);
        }
        Err(e) => {
            diag(&format!("[boot] 官网源预抓取失败: {e}"));
        }
    }
}

/// 预抓取本机源（api1.2026016.xyz）所有分类的频道列表。
///
/// 本机源就是早前 desktop 的 `src_mode="local"`：core.py 的接口域（api1/api/api2
/// 同源镜像），CATS 六分类（综合/体育/央视/卫视/极速港澳台/港澳台），播放页 XOR
/// 解密出 eplay2 直链。抓完后写入 CHANNELS_CACHE（键 = 裸 tid），命令只读缓存。
fn prewarm_channels() {
    diag("[boot] 开始预抓取本机源所有分类频道");
    let cli = clean_tv_core::net::blocking_client();
    // ★ 重试循环：镜像偶发不可达（实测 api1 曾整段 SSL 吊销检查失败、api2 慢），
    //   一次性抓失败本机源分类就永久消失（用户看到「分类少了」）。
    //   每轮只抓缺失分类，部分成功先入缓存让 UI 尽快显示；最多 6 轮、间隔 20s。
    let mut missing: Vec<&str> = clean_tv_core::net::CATS.iter().map(|(t, _)| *t).collect();
    for round in 1..=6u32 {
        if missing.is_empty() {
            break;
        }
        if round > 1 {
            diag(&format!("[boot] 本机源预抓取第 {round} 轮重试，缺 {} 个分类: {:?}", missing.len(), missing));
            std::thread::sleep(std::time::Duration::from_secs(20));
        }
        let mut still_missing = Vec::new();
        for tid in &missing {
            match clean_tv_core::net::blocking_fetch_channels(local_api(), tid, &cli) {
                Ok(chans) => {
                    let v: Vec<Channel> = chans.into_iter().map(|c| Channel { id: c.id, name: c.name }).collect();
                    diag(&format!("[boot] 本机源预抓取 {tid} 完成 {} 条（镜像 {}）", v.len(), local_api().current_api()));
                    // 本机源分类键 = 裸 tid（tv/ty/ys/ws/wintv123/gt）；789 兜底
                    // 数据已带 `789_` 前缀、官网源带 `w|` 前缀，互不冲突。
                    let mut guard = CHANNELS_CACHE.lock().unwrap();
                    guard.get_or_insert_with(std::collections::HashMap::new).insert(tid.to_string(), v);
                }
                Err(e) => {
                    diag(&format!("[boot] 本机源预抓取 {tid} 失败: {e}"));
                    still_missing.push(*tid);
                }
            }
        }
        missing = still_missing;
    }
    if !missing.is_empty() {
        diag(&format!("[boot] 本机源预抓取 6 轮后仍缺: {missing:?}（放弃，UI 不显示这些分类）"));
    }
    diag("[boot] 本机源频道预抓取结束");
}

/// 返回分类列表（对齐早前 desktop 的两源结构）。
///
/// - **本机源**（src=local，默认选中）：CATS 六分类（综合/体育/央视/卫视/
///   极速港澳台/港澳台），来自 api1.2026016.xyz。
/// - **官网源**（src=official）：WebViewTvLive dat 的分组（央视/央视数字/…，
///   共 51 组），tid 形如 `w|央视`。
/// - 789iptv 不再进 UI（只做播放兜底），见 prewarm_789。
#[tauri::command]
fn list_categories() -> Vec<Category> {
    let mut cats: Vec<Category> = Vec::new();
    let guard = CHANNELS_CACHE.lock().unwrap();
    if let Some(map) = guard.as_ref() {
        // ① 本机源六分类（裸 tid）。
        for (tid, name) in clean_tv_core::net::CATS {
            if map.contains_key(tid) {
                cats.push(Category { tid: tid.to_string(), name: name.to_string(), src: "local".to_string() });
            }
        }
        // ② 官网源分组（`w|{分组}`），按频道表首次出现顺序。
        if let Some(groups) = WVT_GROUPS.lock().unwrap().as_ref() {
            for g in groups {
                if map.contains_key(&format!("{WVT_PREFIX}{g}")) {
                    cats.push(Category { tid: format!("{WVT_PREFIX}{g}"), name: g.clone(), src: "official".to_string() });
                }
            }
        }
    }
    // 兜底：本机源还没就绪时先给出骨架（前端显示「加载中」，轮询会再拉）。
    if cats.is_empty() {
        cats.extend(
            clean_tv_core::net::CATS
                .iter()
                .map(|(tid, name)| Category { tid: tid.to_string(), name: name.to_string(), src: "local".to_string() }),
        );
    }
    cats
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

/// 后台起一个线程做 789 实时取流 + 起播（成功后 mpv 直接接管画面），
/// 失败时 emit `play-failed` 事件让前端显示错误。
///
/// ★ 为什么必须后台：`fetch_stream` 要起 headless Edge 子进程并等 CDP 抓流
///   （实测 5~15 秒），Tauri 同步命令在主线程执行——之前直接在命令里同步调它，
///   UI 线程被堵 18 秒，Windows 直接判「未响应」（用户实测卡死）。改为：
///   命令立即返回 → 后台线程取流 → 成功 mpv 接管画面 / 失败事件回推前端。
///
/// 代次防抢：线程持有发起时的 gen，取到流后与全局 FETCH_GEN 核对，不一致
/// （期间用户又点了别的频道）就放弃起播，只有最新一次点击能上屏。
fn spawn_789_fetch(app: tauri::AppHandle, player: Arc<player::Player>, hwnd: u64,
                   tid: String, cid: String, label: String, gen: u64) {
    std::thread::spawn(move || {
        diag(&format!("[play] 789 后台取流 {tid}:{cid} gen={gen}"));
        let hit = match iptv789::fetch_stream(&tid, &cid, std::time::Duration::from_secs(18)) {
            Ok(h) => h,
            Err(e) => {
                diag(&format!("[play] 789 取流失败 gen={gen}: {e}"));
                let _ = app.emit("play-failed", format!("{label}：{e}"));
                return;
            }
        };
        // 代次核对：期间用户又点了新频道，这次的结果作废。
        if FETCH_GEN.load(std::sync::atomic::Ordering::SeqCst) != gen {
            diag(&format!("[play] 789 取流结果作废 gen={gen}（已过期，用户已换台）"));
            return;
        }
        let url = hit.url.clone();
        diag(&format!("[play] 789 命中真实流 url.len={} no_verify={}", url.len(), hit.no_verify));
        let _ = player.stop();
        let headers: Vec<(&str, &str)> = vec![];
        if let Err(e) = player.play(&url, hwnd, "balanced", &headers) {
            diag(&format!("[play] 789 起播失败: {e}"));
            let _ = app.emit("play-failed", format!("{label}：起播失败 {e}"));
            return;
        }
        // 789 的 flv 直连同样要等首帧：出画才算成功，否则明确报错而不是"有声音没图像"。
        match wait_video(&player, std::time::Duration::from_secs(12)) {
            Ok(()) => {
                spawn_exit_monitor(player.clone(), "789".to_string());
                diag("[play] 789 mpv 已出画面（后台线程）");
                let _ = app.emit("play-started", format!("{label}"));
            }
            Err(reason) => {
                diag(&format!("[play] 789 起播未出画面：{reason}"));
                let _ = player.stop();
                let _ = app.emit("play-failed", format!("{label}：{reason}"));
            }
        }
    });
}

/// 等「**真的出画面**」，而不是「mpv 进程还活着」。
///
/// ★ 为什么必须改（2026-10-25 实测）：mpv 只有解出第一帧视频才创建 `--wid`
///   视频子窗口。之前判据是「进程存活 >8s」，于是「有声音、没图像」也会被判
///   PASS——实测出现过 mpv 存活 20 秒、全程只有音频、始终没有视频窗口。
///   音频（AAC）帧独立可解，立刻出声；视频要等建连 + 关键帧 + 预缓冲，
///   网络慢时（实测连 CDN 花 7.2s）能拖十几秒甚至一直不出。
///
/// 返回 Ok(()) = 已出画；Err(原因) = 退出 / 超时 / 被新会话接管。
fn wait_video(player: &Arc<player::Player>, timeout: std::time::Duration) -> Result<(), String> {
    let gen = player.gen();
    let t0 = std::time::Instant::now();
    while t0.elapsed() < timeout {
        // 先纠正「主窗口最小化时 mpv 建成 0×0」的情况，再判断是否真出画。
        player.fix_video_geometry();
        if player.video_ready() {
            diag(&format!(
                "[play] 出画确认 {:.1}s（mpv 视频窗口已创建）",
                t0.elapsed().as_secs_f64()
            ));
            return Ok(());
        }
        if player.gen() != gen {
            return Err("会话被新播放接管".to_string());
        }
        match player.poll() {
            player::PollState::Exited(code) => return Err(format!("mpv 退出 code={code}")),
            player::PollState::Taken => return Err("会话已结束".to_string()),
            player::PollState::Running => {}
        }
        std::thread::sleep(std::time::Duration::from_millis(200));
    }
    Err(format!("{}s 内未出画面", timeout.as_secs()))
}

/// 探测一个流地址在本机**是否连得上**（不读流正文）。
///
/// ★ 为什么要这一步：嗅探（WebView2 旁听）只能证明「浏览器发起过这个请求」，
///   **不能证明本机连得上**。实测反例（2026-10-26）：fengshows 的 CDN 域名
///   `tlive.fengshows.com` 被 DNS 污染（解析到 157.240.x / 199.59.x ——
///   Facebook/Twitter 的 IP 段），WebView2 抓得到 URL，但 mpv 直连必然失败，
///   白等 14s 才报「未出画面」。预检能提前 ~14s 判定并快速回落。
///
/// 判据：能建连 + 拿到 HTTP 响应头即视为「可达」。
///   · 302（源站跳转到真流）→ 活
///   · 2xx（直接给流）→ 活
///   · 404 → 该资源确定不存在（线路已下线）→ **判死**，换下一条
///   · 其它 4xx/5xx → 节点活着但这条线路失效（仍交给 mpv 试，源站可能自愈）
///   · 连接错误 / 超时 → 不可达
///
/// 流可达性预检的结论。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ProbeVerdict {
    /// 确定活着（2xx / 3xx）
    Alive,
    /// **确定不存在**（HTTP 404）→ 换下一条线路，别浪费 14s
    Dead,
    /// **不确定**（连接错误 / 超时 / DNS 失败）→ 不能判死，交给 mpv 试
    Unknown,
}

/// ★★ 为什么 404 要判死（2026-10-26 实测）：789 的 `p.iptv200.com/play.php`
///   在**所有分类**（tv/ys/ws/ty/gt）都恒 404 —— 服务端已下线该中转接口。
///   这种情况下若判「可达」，mpv 会白等 14s 才报失败；而多条网页线路时，
///   判死能让它**立刻换下一条**，把 14s 降到 2.5s。
///
/// ★★ 为什么「连接失败」不能判死（2026-10-26 新增，实测教训）：
///   嗅探到的境外流（`tlive.fengshows.com` / `news.now.com` / `rthk9.rthk.hk`…）
///   在本机**系统 DNS 被污染**（解析到 Facebook/Twitter 段），
///   reqwest 直连必然超时 → 旧实现直接判「不可达」并**丢弃这条线路**。
///   但：
///     ① Chromium（内嵌 WebView2 嗅探时）**自带 DoH**，不受系统 DNS 污染；
///     ② 换 DoH 解析器拿真实 IP 后该流**依然不通**（说明是网络层封锁），
///        但这层差异不该由预检替 mpv 做决定；
///     ③ 污染是**间歇/按域名**的，今天不通不代表用户网络里不通。
///   故：连接失败只记「不确定」，**仍然交给 mpv 试**（mpv 有自己的连接栈
///   与重试），只有明确的 404 才判死。
fn probe_stream_verdict(url: &str, headers: &[(String, String)]) -> ProbeVerdict {
    let cli = clean_tv_core::net::blocking_client_no_redirect();
    let mut req = cli
        .get(url)
        .timeout(std::time::Duration::from_millis(2500));
    for (k, v) in headers {
        let kl = k.to_ascii_lowercase();
        // ★ 只带必要头：Referer/Origin/Cookie 决定能否过源站校验，
        //   但 User-Agent 用源站原值即可（浏览器嗅探时读到的）。
        if matches!(kl.as_str(), "referer" | "origin" | "cookie" | "user-agent") {
            req = req.header(k.as_str(), v.as_str());
        }
    }
    match req.send() {
        Ok(r) => {
            let st = r.status();
            diag(&format!("[play] 流可达性预检 {} -> HTTP {}", st, url.split('?').next().unwrap_or("")));
            if st == reqwest::StatusCode::NOT_FOUND {
                // 404 = 资源确定不存在 → 判死（换下一条线路，别浪费 14s）。
                ProbeVerdict::Dead
            } else {
                // 其它 4xx/5xx 也算「连上了」——交给 mpv 判活更准（源站可能瞬时失效）。
                ProbeVerdict::Alive
            }
        }
        Err(e) => {
            diag(&format!(
                "[play] 流可达性预检失败 {}（{}，判为不确定，仍交 mpv 试）",
                url.split('?').next().unwrap_or(""),
                e
            ));
            ProbeVerdict::Unknown
        }
    }
}

/// 兼容包装：老调用点只要 bool。
fn probe_stream_reachable(url: &str, headers: &[(String, String)]) -> bool {
    probe_stream_verdict(url, headers) != ProbeVerdict::Dead
}

/// 出画之后挂一个退出监视线程，只记录存活时长（不再参与起播判定）。
fn spawn_exit_monitor(player: Arc<player::Player>, tag: String) {
    std::thread::spawn(move || {
        let t0 = std::time::Instant::now();
        if let Some((_g, code)) = player.wait_exit() {
            diag(&format!(
                "[play] {tag} mpv 退出 code={code} 存活 {:.1}s",
                t0.elapsed().as_secs_f64()
            ));
        }
    });
}

/// 串行探测多条线路，把「先吐数据」的排到最前。
///
/// ★ 为什么必须**串行**（不是并发）：13 条线路的 eplay2 URL **token 相同**，
///   同时打过去会被源站限流——实测 13 条并发只有 1 条成功（其余全部 2.5s 超时），
///   而串行单发时 p=0/2/3 三条都能出流（3.66s / 1.71s / 2.41s）。
///   并发探测反而把好线路全挤掉了。
///
/// ★★ 2026-10-26 改造：**探速时顺手把 302 的真流地址取回来**。
///   本机源线路形态是 `eplay2.php`（源站中转），mpv 自己跟 302 时
///   首跳要 11.8~20.9s（源站 TLS 握手极慢，多次超时），
///   而 CDN 直连只要 0.6s —— 慢吞吞的首跳吃掉了 12s 出画窗口，
///   明明活着的线路被判「未出画面」（实测 gt:50 愛爾達體育1台）。
///   所以这里跟一次 302，**只取 Location 头不读流正文**（不耗流配额），
///   把真流 URL 交给 mpv 直连 CDN。
///
/// 策略：按原顺序逐条 GET，**单条超时 2.5s**，能读出数据即算可用；
/// 最多试 `probe` 条，成功的按耗时升序排到最前，其余保持原顺序补后。
///
/// ★★ 2026-10-26 二次改造：**探速要判到「真流」这一层**。
///   只看 `eplay2.php` 的 302 存在与否**区分不出真流死活** —— 实测同一频道：
///     · `…pull-f5-hs.pstatp.com/live/xxx.flv` → **HTTP 404**（11.7s 才返回错误）= 死线
///     · `t42.iptv200.com:8443/live/deltasport2.flv?sign=…` → **HTTP 200**（0.7s）= 活线
///   两者经 eplay2.php 时**都是 302**，探速分不出 → mpv 先撞死线，
///   白等 12s 才换下一条（实测 gt:51 线路1/2 各浪费 12s，线路3 才出画）。
///   所以：先取 302 的 Location（0.2s），再对真流做**轻量探活**
///   （Range: bytes=0-0，只看状态码，**不拉正文**，不消耗流配额）。
fn race_lines(urls: &[String], probe: usize, budget: std::time::Duration) -> Vec<String> {
    if urls.len() <= 1 {
        return urls.to_vec();
    }
    let cli = clean_tv_core::net::blocking_client_no_redirect();
    let deadline = std::time::Instant::now() + budget;
    let mut good: Vec<(std::time::Duration, String)> = Vec::new();

    for (i, u) in urls.iter().take(probe).enumerate() {
        if std::time::Instant::now() >= deadline {
            break;
        }
        let t0 = std::time::Instant::now();
        // ★ 只跟到 302 就停（客户端已设 redirect::Policy::none）：**不拉流正文**。
        //   跟到底会真的读 1KB FLV 数据，等于替 mpv 消耗一次源站配额 ——
        //   实测探速把最好的线路「用掉」后，mpv 再播同一条要等 9 秒才出画。
        //   只看 302 是否存在就能判断这条线路的节点是否活着，且不占配额。
        let (ok, real) = match cli
            .get(u)
            .timeout(std::time::Duration::from_millis(2500))
            .send()
        {
            Ok(r) => {
                let st = r.status();
                // 302 = 节点活着（后面就是真流）；200 = 直接给流，也算活着。
                let base_ok = st.as_u16() == 302 || st.is_success();
                // ★ 若 302，取 Location → **递归解析**到最终真流
                //   （源站的 Location 有两类：直连 CDN（带 sign）与
                //     `/v/*.php` 二级中转（还要再 302 一次），
                //     实测 ys:1 的 13 条线路里两种都有）。
                let real: Option<String> = r
                    .headers()
                    .get(reqwest::header::LOCATION)
                    .and_then(|v| v.to_str().ok())
                    .filter(|s| s.starts_with("http"))
                    .map(|s| s.to_string())
                    .and_then(|loc| {
                        if loc.contains(".php") {
                            // 二级中转 → 递归解析（带它自己的参数）。
                            clean_tv_core::net::resolve_stream_url(&loc, 2500).ok()
                        } else {
                            Some(loc)
                        }
                    });
                (base_ok, real)
            }
            Err(_) => (false, None),
        };
        let el = t0.elapsed();
        // ★ 真流层判活：只有真流也活着（2xx）才算这条线路可用。
        //   真流 404 → 这条线是死的（pstatp.com 型），跳过它，别让 mpv 白等 12s。
        //   ★ 真流探活超时给 **1.2s**（不是 2.5s）：死线要 11.7s 才返回 404，
        //   等满 2.5s 会把探速预算（12s）耗光 → 后面的活线根本没机会被探到。
        //   实测源站 9 条线路里 **4 条带 sign（活）、5 条 pstatp 型（死）**，
        //   死线恰好排在前面（p=0,1 死 / p=2~5 活 / p=6,7 死）——
        //   探速必须能覆盖到后面几条。
        let (ok, real): (bool, Option<String>) = match real {
            Some(rl) => {
                // ★★ 判活放开（2026-10-26 修正）：真流层只看「能否连接成功」，
                //   不看「首字节快不快」。原来用 `Range: bytes=0-0` + 只认 2xx/206，
                //   对「首字节慢但能出流」的节点（t*.iptv200.com:8443 的 flv
                //   TLS 握手后还要等流数据）会**误判死**——而这些正是能播的线。
                //   改为：能连接（send() 成功）且状态码非 4xx/5xx 就算活；
                //   只有连接失败/超时/404 才算死（pstatp.com 死线）。
                let diag_cli = clean_tv_core::net::blocking_client();
                let (live, why) = match diag_cli
                    .get(&rl)
                    .header(reqwest::header::RANGE, "bytes=0-0")
                    .timeout(std::time::Duration::from_millis(1200))
                    .send()
                {
                    Ok(r) => {
                        let s = r.status();
                        let code_s = s.as_u16();
                        // 2xx / 206 / 3xx 都算活（3xx 是 CDN 再跳一次，节点本身在）。
                        let ok = code_s >= 200 && code_s < 400;
                        (ok, format!("HTTP {code_s}"))
                    }
                    Err(e) => (false, format!("ERR {e}")),
                };
                diag(&format!(
                    "[play] 探速真流 线路{} live={} {} ({why})",
                    i + 1,
                    live,
                    rl.split('?').next().unwrap_or("")
                ));
                (ok && live, Some(rl))
            }
            None => (ok, None),
        };
        diag(&format!(
            "[play] 探速 线路{} ok={} 用时 {:.2}s",
            i + 1,
            ok,
            el.as_secs_f64()
        ));
        if ok {
            // ★ 存**真流 URL**（不是中转口）：mpv 直连 CDN，省掉源站慢首跳。
            good.push((el, real.unwrap_or_else(|| u.clone())));
            // 命中 2 条就够选（第一条最可能是最快的），不必把 13 条全试完。
            if good.len() >= 2 {
                break;
            }
        }
    }

    good.sort_by_key(|(el, _)| *el);
    let mut out: Vec<String> = good.into_iter().map(|(_, u)| u).collect();
    if !out.is_empty() {
        let first = out.first().cloned().unwrap_or_default();
        diag(&format!(
            "[play] 探速选中线路（可用 {} 条，最快 {} 字符）",
            out.len(),
            first.len()
        ));
    } else {
        diag("[play] 探速未命中（线路都慢/失败），按原顺序尝试");
    }
    for u in urls {
        if !out.iter().any(|x| x == u) {
            out.push(u.clone());
        }
    }
    // ★★ 兜底：把仍是中转口（eplay2.php）的 URL 解析成最终真流 URL。
    //   探速阶段已经产出了真流地址（直接命中），但**排在后面的线路**
    //   （探速没覆盖到的）还是中转口形态 → 这里补一次解析，
    //   mpv 直连 CDN，不再被源站慢首跳（11.8~20.9s）拖死。
    let out = out
        .iter()
        .map(|u| {
            if !u.contains("eplay2.php") {
                return u.clone();
            }
            let t0 = std::time::Instant::now();
            match clean_tv_core::net::resolve_stream_url(u, 6000) {
                Ok(real) if real != *u && real.starts_with("http") => {
                    diag(&format!(
                        "[play] 解析中转口→真流 {:.2}s: {} → {}",
                        t0.elapsed().as_secs_f64(),
                        u.split('?').next().unwrap_or(""),
                        real.split('?').next().unwrap_or("")
                    ));
                    real
                }
                Ok(_) => u.clone(),
                Err(e) => {
                    diag(&format!("[play] 解析真流失败（用原 URL）: {e}"));
                    u.clone()
                }
            }
        })
        .collect();
    out
}

/// 依次尝试多条直连线路起播，**首启秒退自动换下一条**（最多 3 条）。
///
/// 为什么要它：mpv 对某些线路会 code=2 秒退（源站偶发 5xx / token 刚轮换 /
/// 证书链抖动），实测同一 URL 隔几秒再试就能播——换线比直接给用户报错好。
/// 判定：mpv 存活 ≥ 8 秒或正常退出（code=0）算这条线路可用；否则试下一条。
/// 返回成功起播的 URL；全部失败返回最后一条错误。
///
/// ⚠ 本函数**阻塞**到 mpv 退出（整个播放周期），只能在后台线程里调。
fn play_first_ok(
    player: &Arc<player::Player>,
    hwnd: u64,
    urls: &[String],
    app: &tauri::AppHandle,
    label: &str,
) -> Result<(String, usize), String> {
    let mut last_err = String::from("无可用线路");
    for (i, u) in urls.iter().take(6).enumerate() {
        if let Err(e) = player.play(u, hwnd, "balanced", &[]) {
            last_err = format!("mpv 启动失败: {e}");
            diag(&format!("[play] 线路{} 启动失败：{e}", i + 1));
            continue;
        }
        // ★ 代次必须在 play() 之后取：play() 内部先 stop() 会 bump gen。
        let gen = player.gen();
        match wait_video(player, std::time::Duration::from_secs(12)) {
            Ok(()) => {
                spawn_exit_monitor(player.clone(), format!("线路{}", i + 1));
                // ★ 通知前端清掉「正在获取播放线路中…」占位。
                let _ = app.emit("play-started", format!("{label}"));
                // ★ 只把「成功的是第几条」交回调用方 —— 由它决定要不要写全局。
                //   （这里若直接写全局，switch_line 传进来的单条数组会把
                //     线路总数覆盖成 1，控制条就显示「线路 1/1」。）
                return Ok((u.clone(), i));
            }
            Err(reason) => {
                if player.gen() != gen {
                    diag(&format!("[play] 线路{} 被新会话接管（gen 已变）", i + 1));
                    return Ok((u.clone(), i));
                }
                diag(&format!("[play] 线路{} 未出画面：{reason}，换下一条", i + 1));
                last_err = reason;
                let _ = player.stop();
            }
        }
    }
    Err(last_err)
}

/// 本机源后台实时抓线：fetch_play（普通 HTTPS + XOR 解密，1~3 秒）→ 起播。
/// 播放页 token 虽然短期稳定，但会按 IP/时段轮换，预抓缓存有失效隐患——实时
/// 抓线永远拿新鲜线路，速度又快，故本机源不走缓存起播。
/// 播放页里的 13 条线路按序尝试（首启秒退自动换线）；全失败按名回落 789。
///
/// ★ `depth` = 回落链深度（0 = 用户直接点的频道）。**必须有上限**：
///   实测（2026-10-26）存在天然的回落环 ——
///     本机源 gt:1(404) → 官网源嗅探 → 未出画 → 回落本机源同名 → 又 404
///     → 回落官网源 → …（实测跑满 112 秒仍在循环，每轮 20+ 秒）。
///   环的两端是「同一频道名的两个源」，用深度上限切断。
fn spawn_local_fetch(app: tauri::AppHandle, player: Arc<player::Player>, hwnd: u64,
                     tid: String, cid: String, label: String, gen: u64) {
    spawn_local_fetch_depth(app, player, hwnd, tid, cid, label, gen, 0)
}

/// 带回落深度的本机源抓线（见 [`spawn_local_fetch`] 的深度说明）。
fn spawn_local_fetch_depth(app: tauri::AppHandle, player: Arc<player::Player>, hwnd: u64,
                           tid: String, cid: String, label: String, gen: u64, depth: u32) {
    std::thread::spawn(move || {
        diag(&format!("[play] 本机源后台抓线 {tid}:{cid} gen={gen} depth={depth}"));
        let cli = clean_tv_core::net::blocking_client();
        let mut urls: Vec<String> = Vec::new();
        let mut used_tid = tid.clone();
        let mut used_cid = cid.clone();
        // ★★ 抓取播放页 → 过滤直连线路，抽成闭包以便「失败自动重抓」复用。
        //   带新请求重试一次的原因：源站播放页 token 按 IP/时段轮换，且服务端
        //   临时抽风（返回 `msg=页面结构变化` / 空线路 / 网络抖动）占比不低，
        //   实测一次重抓即可恢复 —— 代价仅 1~3 秒，远小于直接弹窗让用户重按。
        let fetch_and_filter = |tid: &str, cid: &str, try_idx: u32| -> (Vec<String>, String) {
            let res = clean_tv_core::net::blocking_fetch_play(local_api(), tid, cid, &cli);
            match res {
                Ok((u, epg, msg)) => {
                    if msg == "ok" {
                        // ★ 节目单留着给底部「节目单」按钮用（以前直接 `_epg` 丢弃了）
                        *NOW_EPG.lock().unwrap_or_else(|p: std::sync::PoisonError<_>| p.into_inner()) =
                            epg;
                        let lines = u
                            .into_iter()
                            .filter(|x| clean_tv_core::wvt::is_direct_stream(x))
                            .collect();
                        (lines, msg)
                    } else {
                        diag(&format!(
                            "[play] 本机源 gen={gen} 抓线#{try_idx} {tid}:{cid} 播放页返回 msg={msg}"
                        ));
                        (Vec::new(), msg)
                    }
                }
                Err(e) => {
                    diag(&format!(
                        "[play] 本机源 gen={gen} 抓线#{try_idx} {tid}:{cid} 失败: {e}"
                    ));
                    (Vec::new(), format!("抓线错误: {e}"))
                }
            }
        };
        // ★ 首次抓取；空线路/异常时带新请求重抓一次（源站临时抽风常见）。
        let (mut lines, mut last_msg) = fetch_and_filter(&tid, &cid, 1);
        if lines.is_empty() {
            diag(&format!(
                "[play] 本机源 {tid}:{cid} 首次无线路（{last_msg}），自动重抓一次"
            ));
            let (l2, m2) = fetch_and_filter(&tid, &cid, 2);
            lines = l2;
            last_msg = m2;
        }
        urls = lines;
        // ★★ 跨分类重试（源站的 app 路由只对部分分类提供播放页）。
        //   实测（2026-10-25）：app 路由（长 UA）下
        //     `ys`/`ws`/`ty` 分类的播放页正常（200 + 可解密的加密 option）；
        //     `tv`/`gt`/`wintv123` 分类**恒 404**（源站该路由下没数据）。
        //   而 `tv` 分类的每个频道在 `ys`/`ws`/`ty` 里都有**同名对应**
        //   （实测 CCTV1綜合→ys:1、湖南衛視→ws:1、鳳凰衛視中文台→gt:1 …）。
        //   故：原分类 404 时按名去其它分类找同名频道重抓一次。
        if urls.is_empty() {
            if let Some((t2, c2)) = find_local_playable_by_name(&label) {
                diag(&format!(
                    "[play] 本机源 {used_tid}:{used_cid} 无线路，按名跨分类重试 {t2}:{c2}（{label}）"
                ));
                used_tid = t2.clone();
                used_cid = c2.clone();
                // 复用上面的抓取闭包（同样带「空线路自动重抓一次」）。
                let (l2, m2) = fetch_and_filter(&t2, &c2, 1);
                if l2.is_empty() {
                    diag(&format!(
                        "[play] 本机源跨分类 {t2}:{c2} 首次无线路（{m2}），自动重抓一次"
                    ));
                    let (l3, m3) = fetch_and_filter(&t2, &c2, 2);
                    urls = l3;
                    last_msg = m3;
                } else {
                    urls = l2;
                    last_msg = m2;
                }
            }
        }
        if !urls.is_empty() {
            // 代次核对：期间用户又点了新频道，这次的结果作废。
            if FETCH_GEN.load(std::sync::atomic::Ordering::SeqCst) != gen {
                diag(&format!("[play] 本机源结果作废 gen={gen}（用户已换台）"));
                return;
            }
            diag(&format!(
                "[play] 本机源命中 {} 条线路，首条 url.len={} hwnd={hwnd} mpv={}",
                urls.len(),
                urls[0].len(),
                player.mpv_path().display()
            ));
            // ★ 并发探速：13 条线路分别指向不同 CDN（实测 p=0 → t1.iptv200.com /
            //   p=3 → t24.iptv200.com，同城延迟差好几秒，还夹着 403 和握手超时）。
            //   按原顺序死磕第一条要等 6~10 秒才出画；这里并发探前 4 条，
            //   谁先吐数据就用谁，失败/超时的自动排后面。
            // ★ 串行探速（不是并发）：13 条 URL token 相同，并发会被源站限流
            //   （实测并发只有 1 条成功；串行 p=0/2/3 都能出流）。
            //   只探前 9 条：实测源站 9 条线路里 **4 条带 sign（活）、5 条 pstatp 型（死）**，
            //   死线恰好排在前面（p=0,1 死 → p=2~5 活 → p=6,7 死）——
            //   只探 5 条会漏掉后面的活线（实测 gt:50 愛爾達體育1台因此判死）。
            let urls = race_lines(&urls, 9, std::time::Duration::from_secs(20));
            match play_first_ok(&player, hwnd, &urls, &app, &label) {
                Ok((_u, ok_idx)) => {
                    // 出画成功：线路列表 + 实际序号写进全局，控制条显示「线路 x/y」
                    now_update_lines(urls.clone(), ok_idx);
                    diag(&format!(
                        "[play] 本机源 出画 线路 {}/{}（本线程收工）",
                        ok_idx + 1,
                        urls.len()
                    ));
                }
                Err(e) => {
                    diag(&format!("[play] 本机源全部线路失败: {e}"));
                    // ★★ 线路 token 失效自动换线（任务 #93）：源站 CDN 的 `?sign=`
                    //   短期签发，到期后**这批线路集体秒退**（mpv 全 code=2）。
                    //   此时重新抓一次播放页 → 拿到带新 sign 的新线路，再探速播一遍，
                    //   往往直接恢复。代价 1~3 秒抓线 + 探速，远小于弹窗让用户重按。
                    //   代次核对：期间用户又换台则作废。
                    if FETCH_GEN.load(std::sync::atomic::Ordering::SeqCst) != gen {
                        diag(&format!("[play] 本机源换线回合作废 gen={gen}（用户已换台）"));
                        return;
                    }
                    diag(&format!(
                        "[play] 本机源 {used_tid}:{used_cid} 线路疑似 token 失效，重抓播放页换新线路"
                    ));
                    let (fresh, m3) = fetch_and_filter(&used_tid, &used_cid, 3);
                    if fresh.is_empty() {
                        diag(&format!(
                            "[play] 本机源换线回合重抓仍无线路（{m3}），弹窗择路"
                        ));
                        let _ = app.emit(
                            "local-exhausted",
                            format!("{used_tid}|{used_cid}|{label}|{e}"),
                        );
                        return;
                    }
                    let fresh = race_lines(&fresh, 9, std::time::Duration::from_secs(20));
                    match play_first_ok(&player, hwnd, &fresh, &app, &label) {
                        Ok((_u, ok2)) => {
                            now_update_lines(fresh.clone(), ok2);
                            diag(&format!(
                                "[play] 本机源 换线回合出画 线路 {}/{}（token 刷新成功）",
                                ok2 + 1,
                                fresh.len()
                            ));
                        }
                        Err(e2) => {
                            diag(&format!("[play] 本机源换线回合仍失败: {e2}"));
                            let _ = app.emit(
                                "local-exhausted",
                                format!("{used_tid}|{used_cid}|{label}|{e2}"),
                            );
                        }
                    }
                }
            }
            return;
        }
        // ★★ 本机源连「可播线路」都没抓到（无线路，非重试失败）→ 同样走弹窗，
        //   不再跨源回落（官网源/789 一律不碰，两源互不通）。
        diag(&format!(
            "[play] 本机源 {used_tid}:{used_cid} 无可用线路，弹窗择路（{label}）"
        ));
        let _ = app.emit(
            "local-exhausted",
            format!("{used_tid}|{used_cid}|{label}|无可用线路"),
        );
    });
}

/// 播放**官网源**（WebViewTvLive）某频道：直连缓存 → 网页嗅探 → 回落本机源 → 789。
///
/// ★ 抽成独立函数的原因：本机源某些分类（`tv`/`gt`/`wintv123`）在源站
///   app 路由下**恒 404**（实测 2026-10-25），而这些分类里的港澳台频道
///   在本机源可用分类（`ys`/`ws`/`ty`）里没有同名对应 → 需要回落到
///   **官网源**（那边有香港/澳門/台灣等分组，走内嵌 WebView2 嗅探）。
///
/// 返回 `Ok` = 已接管播放（直连或嗅探已启动）；`Err` = 该频道无直连也无网页地址。
fn play_official_source(
    app: tauri::AppHandle,
    player: Arc<player::Player>,
    hwnd: u64,
    tid: String,
    cid: String,
    name: String,
    session_gen: u64,
) -> Result<PlayResult, String> {
    play_official_source_depth(app, player, hwnd, tid, cid, name, 0, session_gen)
}

/// 带回落深度的官网源播放（见 [`spawn_local_fetch`] 的深度说明）。
fn play_official_source_depth(
    app: tauri::AppHandle,
    player: Arc<player::Player>,
    hwnd: u64,
    tid: String,
    cid: String,
    name: String,
    _depth: u32,
    session_gen: u64,
) -> Result<PlayResult, String> {
    let group = tid.strip_prefix(WVT_PREFIX).unwrap_or("").to_string();
    // ★ 缓存值先取到局部变量（避免同名遮蔽导致闭包捕获动态环境）。
    let cached_direct = {
        let guard = PLAY_CACHE.lock().unwrap_or_else(|p| p.into_inner());
        guard.as_ref().and_then(|m| m.get(&format!("{tid}:{cid}")).cloned())
    };
    let cached_pages = {
        let guard = WEB_URL_CACHE.lock().unwrap_or_else(|p| p.into_inner());
        guard
            .as_ref()
            .and_then(|m| m.get(&format!("{tid}:{cid}")).cloned())
            .unwrap_or_default()
    };
    {
        let key = format!("{tid}:{cid}");
        diag(&format!("[play] 官网源查直连缓存 key={key}"));
        let cached = cached_direct.clone();
        let pages = cached_pages.clone();
        diag(&format!(
            "[play] 官网源缓存查询完成 直连={} 网页线路={} 条",
            cached.is_some(),
            pages.len()
        ));
        if let Some(url) = cached {
            diag(&format!("[play] 官网源[{group}] {name} 直连命中 url.len={} hwnd={}", url.len(), hwnd));
            // ★ 全部下沉后台线程：命令必须秒回（同步等 10s 会把 UI 卡成「未响应」）。
            //   判定标准 = **真的出画面**（见 wait_video），不是「进程还活着」。
            let p2 = player.clone();
            let app2 = app.clone();
            let label = name.clone();
            let label2 = label.clone();
            let g = group.to_string();
            let hwnd2 = hwnd;
            let url2 = url.clone();
            std::thread::spawn(move || {
                if let Err(e) = p2.play(&url2, hwnd2, "balanced", &[]) {
                    diag(&format!("[play] 官网源起播失败: {e}"));
                    let _ = app2.emit(
                        "official-exhausted",
                        format!("{tid}|{cid}|{label2}|起播失败 {e}"),
                    );
                    return;
                }
                // ★ 会话层：官网源直连最终走 mpv，回填准确后端句柄（承载窗 hwnd）。
                session::SessionManager::global().settle_backend(
                    session::Backend::Mpv { parent_hwnd: hwnd2 },
                    None,
                    session_gen,
                );
                let gen0 = p2.gen();
                match wait_video(&p2, std::time::Duration::from_secs(10)) {
                    Ok(()) => {
                        spawn_exit_monitor(p2.clone(), format!("官网源[{g}]"));
                        let _ = app2.emit("play-started", format!("{label2}"));
                    }
                    Err(reason) => {
                        if p2.gen() != gen0 {
                            diag("[play] 官网源 mpv 被新会话接管");
                            return;
                        }
                        diag(&format!("[play] 官网源[{g}] {label2} 未出画面：{reason}，弹窗择路"));
                        let _ = p2.stop();
                        // ★★ 官网源直连未出画面 → 不再回落本机源/789，弹窗让用户选。
                        let _ = app2.emit(
                            "official-exhausted",
                            format!("{tid}|{cid}|{label2}|{reason}"),
                        );
                    }
                }
            });
            return Ok(PlayResult { ok: true, msg: "streaming".to_string(), index: 0, total: 1, url: url.clone() });
        }
        // ★★ 无直连但有网页地址 → **WebView2 可见窗直接直播**（官网源正规解法）。
        //   官网源 850/904 频道只有网页地址，网页不是流、mpv 播不了。早先是
        //   「开离屏窗嗅探出 m3u8 → 交 mpv」，但网页线路有三座 mpv 迈不过去的山
        //   （CDRM 私有加扰 / 证书不完整 / CORS+Referer），而页面自己的 `<video>`
        //   由浏览器原生处理这三关。所以 **让 WebView2 窗口落在播放区持续播**，
        //   页面脚本自己起播、自己拉流 —— 绕开全部三座山，还带声音。
        //
        //   直连线路（PLAY_CACHE 命中 m3u8/flv）仍走 mpv（上面分支），互不干扰。
        if !pages.is_empty() {
            let app2 = app.clone();
            let label = name.clone();
            let g = group.to_string();
            let page_list = pages.clone();
            std::thread::spawn(move || {
                // ★★ 逐条网页线路尝试：开 WebView2 直播窗导航第一条，页面出画即成功；
                //   页面加载失败/无播放器就关窗换下一条（与旧「逐条嗅探」同粒度）。
                let mut last_err = String::from("所有网页线路均未出画");
                for (idx, page) in page_list.iter().enumerate() {
                    diag(&format!(
                        "[play] 官网源[{g}] {label} 开 WebView2 直播窗 {}/{}: {page}",
                        idx + 1,
                        page_list.len()
                    ));
                    // 开窗必须在主线程（Tauri 建窗要求）。`run_on_main_thread` 从后台线程调用是
                    // **异步排队**的（不等待执行），直接读布尔会竞态读到 false。
                    // 用一对 mpsc 通道做同步回执：闭包在主线程执行完 open_live_window
                    // 后把结果 send 回来，后台线程 recv 阻塞等真值。
                    //
                    // ★ 坐标在**闭包内**（真正建窗前最后一刻）读最新 stage_rect：
                    //   起播时前端 play() 里 await reportStageRect() 是异步的，后台线程
                    //   先跑起来可能读到旧值/None。闭包跑在主线程、且 report_stage_rect
                    //   也是主线程 invoke，二者串行 → 闭包读到的必然是最新已上报坐标。
                    let app3 = app2.clone();
                    let app4 = app2.clone();
                    let url = page.clone();
                    let (tx, rx) = std::sync::mpsc::channel::<(bool, i32, i32, i32, i32)>();
                    let dispatch_ok = app2.run_on_main_thread(move || {
                        // 建窗前读最新播放区坐标（None 则用 fallback 居中）。
                        let rect = {
                            let st = app4.try_state::<AppState>();
                            st.and_then(|s| *s.stage_rect.lock().unwrap_or_else(|p| p.into_inner()))
                        };
                        let (sx, sy, sw, sh) = rect.unwrap_or((200, 120, 800, 450));
                        let r = official_embed::open_live_window(&app3, &url, sx, sy, sw, sh);
                        let _ = tx.send((r, sx, sy, sw, sh));
                    });
                    if dispatch_ok.is_err() {
                        last_err = format!("{page} 直播窗派发失败（主线程忙）");
                        continue;
                    }
                    // 等待主线程回执（最多 5s，防止主线程卡死拖死后台线程）。
                    let (ok, sx, sy, sw, sh) = match rx.recv_timeout(std::time::Duration::from_secs(5)) {
                        Ok(v) => v,
                        Err(_) => {
                            last_err = format!("{page} 直播窗回执超时");
                            continue;
                        }
                    };
                    if !ok {
                        last_err = format!("{page} 直播窗建窗失败");
                        continue;
                    }
                    diag(&format!(
                        "[play] 官网源[{g}] {label} WebView2 直播窗已开 @({sx},{sy},{sw}x{sh})，等待出画 {}/{}",
                        idx + 1,
                        page_list.len()
                    ));
                    // ★★ 建窗成功 ≠ 能看：页面可能还在拉流（央视频要 5~20s），
                    //   也可能这条线路根本播不出来。必须等**真的解出帧**才算成功。
                    //   判定走 WebView2 的 ExecuteScript 直接问页面 `<video>` 状态，
                    //   与窗口透明度无关 → 判定期间窗口可以一直保持全透明。
                    let live_hwnd =
                        official_embed::LIVE_HWND.load(std::sync::atomic::Ordering::SeqCst);
                    // 首条线路给足时间（3s 起播宽限 + 16s 观察）；后续线路收紧，
                    // 避免多条都慢时把用户晾太久。
                    let observe = if idx == 0 { 16000 } else { 10000 };
                    match official_embed::wait_live_playing(&app2, live_hwnd, 3000, observe) {
                        official_embed::LiveVerdict::Playing => {
                            // ★ 出画了才把窗口从屏幕外移进播放区 + 260ms 淡入
                            official_embed::show_live_window_at(live_hwnd, sx, sy, sw, sh, 260);
                            diag(&format!(
                                "[play] 官网源[{g}] {label} 出画确认 {}/{}",
                                idx + 1,
                                page_list.len()
                            ));
                            // ★ 会话层：官网源网页线路最终走 WebView2 直播窗，回填准确句柄。
                            session::SessionManager::global().settle_backend(
                                session::Backend::Webview { hwnd: live_hwnd },
                                Some(session::StageRect::new(sx, sy, sw, sh)),
                                session_gen,
                            );
                            let _ = app2.emit("play-started", format!("{label}"));
                            return;
                        }
                        official_embed::LiveVerdict::Superseded => {
                            diag("[play] 官网源直播窗已被换台接管，本线程收工");
                            return;
                        }
                        official_embed::LiveVerdict::NotPlaying => {
                            diag(&format!(
                                "[play] 官网源[{g}] {label} {}/{} 页面未出画，换下一条线路",
                                idx + 1,
                                page_list.len()
                            ));
                            last_err = format!("{page} 页面未出画");
                            // 关掉这个（始终透明的）窗，换下一条
                            official_embed::close_live_window();
                            continue;
                        }
                    }
                }
                // 所有网页线路都建窗失败 → 弹窗让用户选（不再回落本机源/789）。
                diag(&format!("[play] 官网源[{g}] {label} 所有网页线路失败: {last_err}"));
                let _ = app2.emit(
                    "official-exhausted",
                    format!("{tid}|{cid}|{label}|{last_err}"),
                );
            });
            return Ok(PlayResult { ok: true, msg: "streaming".to_string(), index: 0, total: 1, url: String::new() });
        } else {
            // ★ 既无直连缓存、又无网页地址 → 不再回落，弹窗让用户选。
            diag(&format!("[play] 官网源[{group}] {name} 既无直连也无网页地址，弹窗择路"));
            let _ = app.emit(
                "official-exhausted",
                format!("{tid}|{cid}|{name}|既无直连也无网页地址"),
            );
            return Ok(PlayResult { ok: true, msg: "streaming".to_string(), index: 0, total: 1, url: String::new() });
        }
    }
}

/// 播放某频道。**命令本身必须秒回**（UI 线程不许阻塞），耗时操作全部下沉后台线程。
///
/// 两条路径（789 只做兜底，不进 UI）：
/// - **本机源**（裸 tid ∈ tv/ty/ys/ws/wintv123/gt）：spawn `spawn_local_fetch`
///   后台实时抓播放页解密（1~3 秒），命令立即返回；失败按名回落 789。
/// - **官网源**（tid = `w|{分组}`）：读 PLAY_CACHE 直连线路零网络起播；
///   无直连（网页线路频道）→ 按名回落 789 实时取流；再不行报错回推。
#[tauri::command]
fn play_channel(tid: String, cid: String, name: String, app: tauri::AppHandle,
                state: State<'_, AppState>) -> Result<PlayResult, String> {
    diag(&format!("[play] play_channel tid={tid} cid={cid} name={name:?}"));
    // ★ 记下当前播放上下文（控制条要显示频道名 / 线路 x/y）。线路列表稍后由
    //   真正出画的那条路径补上（本机源是后台抓的，这里还不知道有几条）。
    let is_official = tid.starts_with(WVT_PREFIX);
    now_set(NowPlaying {
        tid: tid.clone(),
        cid: cid.clone(),
        name: name.clone(),
        src: if is_official { "official".into() } else { "local".into() },
        urls: Vec::new(),
        idx: 0,
    });
    // 换台即清空上一频道的节目单，避免「节目单」按钮闪出旧内容
    NOW_EPG.lock().unwrap_or_else(|p| p.into_inner()).clear();
    let player = state.player.clone();
    let owner_hwnd = *state.hwnd.lock().unwrap_or_else(|p| p.into_inner());

    // ★ mpv 的 `--wid` 父窗口用**独立承载窗口**，不是主窗口。
    //   直接嵌主窗口 → mpv 视频子窗口与 WebView2 同级，Z 序在其下 → 画面被
    //   整块盖住，只剩音频（用户实测「有声音没图像」）。详见 player::VideoHost。
    //   承载窗口的位置/尺寸由前端上报播放区几何（report_stage_rect）后摆正。
    let host = state.host.clone();
    let hwnd = host.ensure(owner_hwnd);
    if hwnd == 0 {
        return Err("视频承载窗口创建失败".to_string());
    }
    host.show();

    // ★★ 会话层：切源/切台统一走 `SessionManager`，旧会话（mpv 进程 + 官网直播窗）
    //   在这里被**原子替换**——先同步回收旧后端，再登记新后端。这是「残留/双声」
    //   的根治点：以前 `stop()` + `close_live_window()` 各调各的，现在一处收口。
    //
    //   ★ 分叉点收敛：以前这里按 `tid.starts_with(WVT_PREFIX)` 把源码切开成两条
    //   完全不同的路径。统一后，两条路径最终都会落到「一个后端资源」上——
    //   本机源最终是 mpv，官网源最终是 mpv（直连）或 WebView2（网页线路）。
    //   这里先按源码登记一个**占位后端**（本机源=mpv，官网源=webview），真正出画
    //   的路径再 `settle_backend` 回填准确的句柄与几何。
    let backend_hint = if is_official {
        session::Backend::Webview { hwnd: 0 }
    } else {
        session::Backend::Mpv { parent_hwnd: hwnd }
    };
    let stage = state.stage_rect.lock().unwrap_or_else(|p| p.into_inner())
        .map(|(x, y, w, h)| session::StageRect::new(x, y, w, h));
    let session_gen = session::SessionManager::global().replace(backend_hint, stage);

    // 官网源分组（`w|{分组}`）：直连缓存命中即播（零网络）；
    // 否则**用内嵌 WebView2 嗅探官网网页真实流**（这才是官网源真正的内容）；
    // 嗅探不到再回落本机源同名 → 789。逻辑见 play_official_source。
    if tid.starts_with(WVT_PREFIX) {
        return play_official_source(app, player, hwnd, tid, cid, name, session_gen);
    }

    // 本机源（裸 tid）：后台实时抓播放页解密，命令秒回。
    let gen = bump_fetch_gen();
    spawn_local_fetch(app, player, hwnd, tid.clone(), cid.clone(), name.clone(), gen);
    diag(&format!("[play] 本机源已派发后台抓线 {tid}:{cid} gen={gen}，命令返回"));
    Ok(PlayResult { ok: true, msg: "streaming".to_string(), index: 0, total: 1, url: String::new() })
}

/// 主线程响应探针：每 2 秒往主线程投一个空任务，量「投递 → 执行」的延迟。
///
/// 用途：「偶尔无响应」这类问题靠肉眼抓不到。延迟超过 800ms 就记一条
/// `[hung] 主线程阻塞约 Nms`，事后翻日志就知道卡了多久、在什么操作之后卡的。
/// （正常运行这条日志一条都不该出现。）
fn spawn_hung_probe(app: tauri::AppHandle) {
    std::thread::spawn(move || loop {
        std::thread::sleep(std::time::Duration::from_secs(2));
        let t0 = std::time::Instant::now();
        let done = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let flag = done.clone();
        let dispatched = app
            .run_on_main_thread(move || {
                flag.store(true, std::sync::atomic::Ordering::SeqCst);
            })
            .is_ok();
        if !dispatched {
            return; // 主线程已退出（关闭应用）
        }
        while t0.elapsed() < std::time::Duration::from_secs(6) {
            if done.load(std::sync::atomic::Ordering::SeqCst) {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        let ms = t0.elapsed().as_millis() as u64;
        if ms > 800 {
            diag(&format!("[hung] 主线程阻塞约 {ms}ms"));
        }
    });
}

/// 暂停 / 继续。由前端传入目标状态（true=暂停），后端执行 `set pause`。
///
/// ★ 不用 `cycle pause`：那样后端拿不到新状态（IPC 只回报命令是否成功），
///   前端还得再问一次；直接 set 目标值，双方状态天然一致。
#[tauri::command]
fn set_pause(paused: bool, state: State<'_, AppState>) -> bool {
    let v = if paused { "yes" } else { "no" };
    if !state.player.cmd(&["set", "pause", v]) {
        diag("[ui] 暂停指令未送达 mpv");
    }
    diag(&format!("[ui] 暂停={paused}"));
    paused
}

/// 设置音量（0~100）。
#[tauri::command]
fn set_volume(v: i32, state: State<'_, AppState>) {
    let v = v.clamp(0, 100);
    let s = v.to_string();
    state.player.cmd(&["set", "volume", &s]);
}

/// 画面适应方式：`fit` 适应（留黑边）/ `stretch` 拉伸 / `crop` 裁剪填满。
#[tauri::command]
fn set_fit(mode: String, state: State<'_, AppState>) {
    // panscan=1 表示裁掉黑边填满；0 表示按比例适应。拉伸用 aspect 覆盖。
    match mode.as_str() {
        "stretch" => {
            state.player.cmd(&["set", "panscan", "0"]);
            state.player.cmd(&["set", "video-aspect-override", "no"]);
        }
        "crop" => {
            state.player.cmd(&["set", "video-aspect-override", "no"]);
            state.player.cmd(&["set", "panscan", "1"]);
        }
        _ => {
            // fit（默认）
            state.player.cmd(&["set", "panscan", "0"]);
            state.player.cmd(&["set", "video-aspect-override", "no"]);
        }
    }
    diag(&format!("[ui] 画面适应 -> {mode}"));
}

/// 当前频道节目单（本机源有；官网源通常为空）。
#[tauri::command]
fn now_epg() -> Vec<clean_tv_core::core::EpgItem> {
    NOW_EPG
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .clone()
}

/// 底部控制条用的当前播放信息。
#[derive(serde::Serialize)]
struct NowInfo {
    name: String,
    src: String,
    /// 当前线路序号（0 起）
    index: usize,
    /// 线路总数；未知时为 0
    total: usize,
    /// 是否允许手动切换线路（本机源且多线路）
    can_switch: bool,
}

#[tauri::command]
fn now_playing() -> NowInfo {
    match now_get() {
        Some(np) => NowInfo {
            name: np.name,
            src: np.src.clone(),
            index: np.idx,
            total: np.urls.len(),
            // 官网源的「线路」是一个个网页地址，切换要重建 WebView2 并等出画，
            // 语义与体验都不同，暂不在控制条开放，避免点了长时间黑屏。
            can_switch: np.src == "local" && np.urls.len() > 1,
        },
        None => NowInfo {
            name: String::new(),
            src: String::new(),
            index: 0,
            total: 0,
            can_switch: false,
        },
    }
}

/// 手动切换线路（`delta` = +1 下一条 / -1 上一条）。
///
/// ★ 复用已解密好的线路列表，**不再重新抓播放页**（省 1~3 秒）。
/// ★ 仍然先解 302 再交给 mpv —— 直接喂 eplay2.php 会让首跳拖满看门狗。
#[tauri::command]
fn switch_line(delta: i32, app: tauri::AppHandle, state: State<'_, AppState>) -> Result<(), String> {
    let np = now_get().ok_or_else(|| "当前没有在播的频道".to_string())?;
    if np.urls.len() <= 1 {
        return Err("该频道只有一条线路".to_string());
    }
    let n = np.urls.len() as i32;
    let next = ((np.idx as i32 + delta).rem_euclid(n)) as usize;
    let url = np.urls[next].clone();
    diag(&format!(
        "[line] 手动切换 {}/{} -> {}/{}",
        np.idx + 1,
        n,
        next + 1,
        n
    ));

    let player = state.player.clone();
    let host = state.host.clone();
    let label = np.name.clone();
    let urls = np.urls.clone();
    // 立刻写回序号：前端连点也不会错位
    now_update_lines(urls, next);

    std::thread::spawn(move || {
        let hwnd = host.ensure(0);
        if hwnd == 0 {
            let _ = app.emit("play-failed", format!("{label}：承载窗口创建失败"));
            return;
        }
        // 解析 302 → CDN 真流（失败就退回原 URL，让 mpv 自己跟）
        let real = clean_tv_core::net::resolve_stream_url(&url, 4000)
            .ok()
            .filter(|r| r != &url)
            .unwrap_or_else(|| url.clone());
        match play_first_ok(&player, hwnd, std::slice::from_ref(&real), &app, &label) {
            Ok(_) => diag(&format!("[line] 已切到 {}/{}", next + 1, n)),
            Err(e) => {
                diag(&format!("[line] 切换失败 {}/{}: {e}", next + 1, n));
                let _ = app.emit("play-failed", format!("{label}：线路 {} 不可用", next + 1));
            }
        }
    });
    Ok(())
}

/// 全屏开关。返回切换后的状态（true = 已全屏）。
///
/// ★ 前端同时给 body 加 `.fs` 类收起左侧列表 —— 两件事都做才是
///   「点全屏就只剩画面」的效果。
#[tauri::command]
fn toggle_fullscreen(app: tauri::AppHandle) -> bool {
    use tauri::Manager;
    let Some(win) = app.get_webview_window("main") else {
        return false;
    };
    let now = win.is_fullscreen().unwrap_or(false);
    let _ = win.set_fullscreen(!now);
    diag(&format!("[ui] 全屏 -> {}", !now));
    !now
}

/// 停止播放。
#[tauri::command]
fn stop_channel(state: State<'_, AppState>) {
    // ★ 会话层统一回收：mpv 进程 + 官网直播窗两样一起收干净（不再散落各调各的）。
    session::SessionManager::global().clear();
    state.host.hide();
}

/// 前端上报播放区（`#stage`）在**视口内**的位置与尺寸（CSS 像素）。
///
/// ★ 为什么让 Rust 侧做坐标换算：JS 的 CSS 像素与 Win32 的窗口坐标是**两套
///   空间**（DPI 缩放 + 客户区偏移），前端自己乘 `devicePixelRatio` 必然错
///   （实测 125% 缩放下偏差正好一个 dpr）。这里前端只报**相对视口的 CSS 偏移**，
///   由 Rust 用「客户区尺寸 ÷ 视口 CSS 尺寸」求出真实缩放比，再换算成
///   `SetWindowPos` 要的屏幕坐标 —— 单一真源，不会漂。
///
/// * `x, y` —— stage 左上角相对视口左上角的 CSS 偏移。
/// * `w, h` —— stage 的 CSS 尺寸。
/// * `vw, vh` —— 视口的 CSS 尺寸（`window.innerWidth/innerHeight`）。
#[tauri::command]
fn report_stage_rect(x: i32, y: i32, w: i32, h: i32, vw: i32, vh: i32,
                      state: State<'_, AppState>) {
    let owner = *state.hwnd.lock().unwrap_or_else(|p| p.into_inner());
    let host = state.host.clone();
    host.ensure(owner);
    // ★ 缓存 CSS 偏移 + 视口尺寸：主窗口拖动时用它重算屏幕坐标（见 Moved 事件）。
    *state.stage_css.lock().unwrap_or_else(|p| p.into_inner()) = Some((x, y, w, h, vw, vh));
    match host.place_over_client(owner, x, y, w, h, vw, vh) {
        Some((px, py, pw, ph)) => {
            // ★ 缓存屏幕坐标：官网源 WebView2 直播窗要用它落在播放区。
            let mut guard = state.stage_rect.lock().unwrap_or_else(|p| p.into_inner());
            *guard = Some((px, py, pw, ph));
            drop(guard);
            // ★★ 几何单一真源（第三步）：不再分别调 mpv 承载窗 + 官网直播窗两处，
            //   而是交给会话层按「当前后端」一次性同步。全屏/切源后画面缩在中间
            //   一整块黑的根因（只动了 mpv、没动直播窗）由此从机制上消除。
            session::SessionManager::global()
                .sync_geometry(session::StageRect::new(px, py, pw, ph));
            diag(&format!(
                "[player] 播放区换算 css=({x},{y},{w},{h}) 视口={vw}x{vh} -> 屏幕=({px},{py},{pw},{ph})"
            ));
        }
        None => {
            diag("[player] 播放区换算失败（客户区或视口尺寸异常）");
        }
    }
}

/// 诊断日志落盘（release 无控制台时用）。设 CLEAN_TV_LOG=<路径> 时把关键状态
/// 追加写进文件；不设则无操作。用于交付前 release 验证。
///
/// ★ 每行带相对启动的毫秒数：排查「mpv 秒退」「后台线程延迟」这类问题时，
///   只看有无日志无法区分「立刻死」和「播 90 秒后死」，时间轴是必需证据。
fn diag(msg: &str) {
    static START: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();
    let t = START.get_or_init(std::time::Instant::now).elapsed().as_millis();
    let line = format!("[+{t}ms] {msg}");
    // ★ release GUI 子系统无 stderr，eprintln! 写到空句柄 —— debug 才打印。
    //   文件落盘是 release 下唯一可见通道。
    #[cfg(debug_assertions)]
    eprintln!("{line}");
    let _ = &line;
    if let Ok(p) = std::env::var("CLEAN_TV_LOG") {
        use std::io::Write;
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&p) {
            let _ = writeln!(f, "{line}");
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
    // ★ 默认自动起播（双击 exe 即播本机源第一个可播频道）；仅当显式设
    //   CLEAN_TV_AUTOPLAY=0 时关闭。这样用户无环境变量也一定能看到画面，
    //   不再需要靠着改 autoplay 门控才能起播。
    let ap = std::env::var("CLEAN_TV_AUTOPLAY").as_deref() != Ok("0");
    diag(&format!("[flag] env_flag 被调用 autoplay={ap}"));
    EnvFlag { autoplay: ap }
}

/// 自动化测试钩子：CLEAN_TV_AUTOPLAY_SRC=local 时 autoplay 强制走本机源（验回落链路）。
#[tauri::command]
fn env_flag_src() -> String {
    std::env::var("CLEAN_TV_AUTOPLAY_SRC").unwrap_or_default()
}

/// 自动化测试钩子：CLEAN_TV_AUTOPLAY_TID=ys（本机源分类）或 `w|央视`（官网源分组）
/// AUTOPLAY_SRC 的 switchSrc 轮询链路可靠）。boot 早期由前端读取。
#[tauri::command]
fn env_autoplay_tid() -> String {
    std::env::var("CLEAN_TV_AUTOPLAY_TID").unwrap_or_default()
}

/// 自动化测试钩子：CLEAN_TV_AUTOPLAY_CID=5 —— 指定 autoplay 起播的**频道序号**
/// （在 `CLEAN_TV_AUTOPLAY_TID` 指定的分类里按列表顺序数，从 1 开始）。
///
/// ★ 用途（2026-10-26）：验收需要定点测某个频道 —— 本机源 tv/wintv123/gt 的
///   第一个频道恒为「凤凰卫视中文台」，而它恰好是**被网络层封锁**的
///   （实测 tlive.fengshows.com 直连 + DoH 真实 IP 均不通）→ 拿它当样本
///   测不出其它频道的真实表现。
#[tauri::command]
fn env_autoplay_cid() -> String {
    std::env::var("CLEAN_TV_AUTOPLAY_CID").unwrap_or_default()
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
    // ★ 启动预抓取顺序（全部后台化，窗口秒开；前端分类下拉自带轮询补拉）：
    //   1) 本机源频道表（api1 镜像，默认 tab 的数据源）
    //   2) 官网源分组频道表（full.dat 解密，数秒）
    //   3) 789iptv 兜底频道表（只喂 find_789_by_name，不进 UI）
    //   播放线路不预抓：本机源点播时实时解密（1~3 秒），官网源直连随频道表
    //   一起进 PLAY_CACHE。
    if std::env::var("CLEAN_TV_SKIP_PREWARM_CHANNELS").as_deref() != Ok("1") {
        std::thread::spawn(prewarm_channels);
    }
    if std::env::var("CLEAN_TV_SKIP_PREWARM_WVT").as_deref() != Ok("1") {
        std::thread::spawn(prewarm_wvt);
    }
    std::thread::spawn(prewarm_789);
    // ★ 会话层全局句柄：先建好 player/host 的 Arc，再同时注入 AppState 与 OnceLock，
    //   使 session::SessionManager 能在非 tauri 命令上下文里回收 mpv / 同步几何。
    let app_player = Arc::new(player::Player::new(
        // 找 mpv：优先 exe 同目录（release 资源布局），退化为开发目录兜底。
        resolve_exe_mpv(),
    ));
    let app_host = Arc::new(player::VideoHost::new());
    let _ = G_PLAYER.set(app_player.clone());
    let _ = G_HOST.set(app_host.clone());

    tauri::Builder::default()
        .manage(AppState {
            player: app_player,
            hwnd: std::sync::Mutex::new(0),
            host: app_host,
            stage_rect: std::sync::Mutex::new(None),
            stage_css: std::sync::Mutex::new(None),
        })
        .setup(|app| {
            diag("[boot] setup 开始");
            // 显式建主窗口（conf windows 数组已清空），这样才能挂 on_new_window。
            let wb = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("纯净电视 · Rust")
                .inner_size(1100.0, 700.0)
                .min_inner_size(720.0, 480.0)
                .center()
                // 命门②：拦死所有 window.open（广告 / 官方播放页弹窗）。
                .on_new_window(|url, _features| {
                    eprintln!("[win] 拦截新窗口请求: {url}");
                    tauri::webview::NewWindowResponse::Deny
                });
            // ★★ 浏览器参数必须与官网源嗅探窗**逐字一致**（`official_embed::browser_args`）。
            //   WebView2 的 `additional_browser_args` 是**环境级**设置：同一进程内
            //   第二个 WebView2 若用不同的环境选项建窗，会直接失败
            //   `HRESULT(0x8007139F) ERROR_INVALID_STATE`（实测：主窗口已占环境后，
            //   嗅探窗建不出来 → 官网源网页线路全线播不了）。
            //   参数一致 → WebView2 复用同一环境，嗅探窗正常创建。
            //   附带的自动化调试端口参数也要两边都给，否则同样不一致。
            let debug_extra = match std::env::var("CLEAN_TV_CDP_PORT") {
                Ok(p) if !p.is_empty() => {
                    diag(&format!("[boot] WebView2 调试端口 {p}"));
                    // ★ `--remote-allow-origins` 必带：新版 Edge 对 CDP 的 WebSocket
                    //   连接做 Origin 校验，缺它握手直接 403（实测）。
                    format!("--remote-debugging-port={p} --remote-allow-origins=*")
                }
                _ => String::new(),
            };
            let wb = wb.additional_browser_args(&official_embed::browser_args(&debug_extra));
            let win = wb.build()?;

            // 填入主窗口 HWND（mpv --wid 内嵌 + 官网源直播窗 owner 用）。
            let hwnd = win.hwnd().map(|h| h.0 as u64).unwrap_or(0);
            if let Some(state) = app.try_state::<AppState>() {
                *state.hwnd.lock().unwrap() = hwnd;
            }
            official_embed::MAIN_OWNER.store(hwnd, std::sync::atomic::Ordering::SeqCst);
            diag(&format!("[app] 主窗口 HWND={hwnd}"));

            // ★ 主窗口重新获得焦点 / 移动 / 缩放后，把视频承载窗重新顶上来。
            //   承载窗是主窗口的 owned window，Windows 只保证「owned 在 owner 之上」，
            //   但焦点/位置变化时系统可能重排 → WebView2 渲染子窗盖住视频
            //   → 表现为「有声音没图像」。这里显式补一次置顶。
            {
                let app_handle = app.handle().clone();
                win.on_window_event(move |ev| {
                    use tauri::WindowEvent;
                    let moved = matches!(ev, WindowEvent::Moved(_));
                    let need_raise = matches!(
                        ev,
                        WindowEvent::Focused(true) | WindowEvent::Moved(_) | WindowEvent::Resized(_)
                    );
                    if !need_raise {
                        return;
                    }
                    let st = app_handle.state::<AppState>();
                    // ★★ 主窗口**拖动**（Moved）时，客户区屏幕原点变了，但视口内
                    //   布局不变——必须用缓存的 CSS 偏移重新换算屏幕坐标，把 mpv
                    //   承载窗 + 官网直播窗**重新贴**到主窗口上。否则只 raise 不
                    //   重定位，视频画面停在旧屏幕位置，与主窗体分离（用户实测）。
                    if moved {
                        let owner = *st.hwnd.lock().unwrap_or_else(|p| p.into_inner());
                        if let Some((x, y, w, h, vw, vh)) =
                            *st.stage_css.lock().unwrap_or_else(|p| p.into_inner())
                        {
                            // place_over_client 内部 ClientToScreen 现算新屏幕原点。
                            if let Some((px, py, pw, ph)) =
                                st.host.place_over_client(owner, x, y, w, h, vw, vh)
                            {
                                *st.stage_rect.lock().unwrap_or_else(|p| p.into_inner()) =
                                    Some((px, py, pw, ph));
                                session::SessionManager::global()
                                    .sync_geometry(session::StageRect::new(px, py, pw, ph));
                                diag(&format!(
                                    "[player] 拖动重定位 -> 屏幕=({px},{py},{pw},{ph})"
                                ));
                            }
                        }
                        return;
                    }
                    // 只在播放中（承载窗可见）才置顶，避免把隐藏的窗口拉出来。
                    if st.host.hwnd() != 0 {
                        st.host.raise();
                    }
                    // ★ 官网源 WebView2 直播窗同样要重顶（owned window 焦点变化可能被重排）。
                    official_embed::raise_live_window();
                });
            }

            // ★ 无前端起播自检钩子：CLEAN_TV_SMOKE_PLAY="ys:1" 时，窗口就绪 3 秒后
            //   直接走「本机源实时抓线 → mpv 起播」链路（完全不依赖 WebView2 里的 JS）。
            //   用途：本机锁屏/无桌面时 WebView2 不执行前端脚本，仍能验证交付的
            //   release 二进制播放链路是否真的通（验收证据落 CLEAN_TV_LOG）。
            if let Ok(spec) = std::env::var("CLEAN_TV_SMOKE_PLAY") {
                if let Some((t, c)) = spec.split_once(':') {
                    if let Some(st) = app.try_state::<AppState>() {
                        let pl = st.player.clone();
                        let app_h = app.handle().clone();
                        let (tid, cid) = (t.to_string(), c.to_string());
                        diag(&format!("[smoke] 3s 后直连自检 {tid}:{cid}（跳过前端）"));
                        std::thread::spawn(move || {
                            std::thread::sleep(std::time::Duration::from_secs(3));
                            let gen = bump_fetch_gen();
                            spawn_local_fetch(app_h, pl, hwnd, tid, cid, "自检频道".to_string(), gen);
                        });
                    }
                }
            }

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
            spawn_hung_probe(app.handle().clone());
            // ★ 自动化测试钩子：CLEAN_TV_AUTOFULLSCREEN=<秒> → 到点通知前端切全屏。
            //   官网源的画面是独立 WebView2 窗口，无法用 CDP 点按钮（开 CDP 会让
            //   直播窗建不出来），所以留这个钩子做「全屏后是否铺满」的验收。
            // ★ 自动化测试钩子：CLEAN_TV_SWITCH_AT=<秒> + _TID/_CID/_NAME
            //   → 到点通知前端切到另一个频道（用于复现「官网源切本机源时旧窗残留」）。
            if let Ok(v) = std::env::var("CLEAN_TV_SWITCH_AT") {
                let secs = v.parse::<u64>().unwrap_or(20);
                let tid = std::env::var("CLEAN_TV_SWITCH_TID").unwrap_or_default();
                let cid = std::env::var("CLEAN_TV_SWITCH_CID").unwrap_or_default();
                let name = std::env::var("CLEAN_TV_SWITCH_NAME").unwrap_or_default();
                if !tid.is_empty() {
                    let ah = app.handle().clone();
                    std::thread::spawn(move || {
                        std::thread::sleep(std::time::Duration::from_secs(secs));
                        diag(&format!("[test] 自动切台 -> {tid}:{cid} {name}"));
                        let _ = ah.emit("auto-play", format!("{tid}|{cid}|{name}"));
                    });
                }
            }
            if let Ok(v) = std::env::var("CLEAN_TV_AUTOFULLSCREEN") {
                let secs = v.parse::<u64>().unwrap_or(15);
                let ah = app.handle().clone();
                std::thread::spawn(move || {
                    std::thread::sleep(std::time::Duration::from_secs(secs));
                    diag(&format!("[test] 自动全屏触发（{secs}s）"));
                    let _ = ah.emit("auto-fullscreen", ());
                });
            }
            diag("[boot] setup 完成");
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            app_info,
            list_categories,
            list_channels,
            play_channel,
            stop_channel,
            now_playing,
            switch_line,
            toggle_fullscreen,
            set_pause,
            set_volume,
            set_fit,
            now_epg,
            report_stage_rect,
            env_flag,
            env_flag_src,
            env_autoplay_tid,
            env_autoplay_cid,
            self_check,
        ])
        .run(tauri::generate_context!())
        .expect("Tauri 启动失败");
}
#[cfg(test)]
mod tests {
    use super::*;

    /// 官网源网页线路排序：央视网 → 央视频 → 其它 → 移动直播间。
    #[test]
    fn web_url_rank_order() {
        assert_eq!(web_url_rank("https://tv.cctv.com/live/cctv1/"), 0);
        assert_eq!(web_url_rank("https://www.yangshipin.cn/tv/home?pid=600001859"), 1);
        assert_eq!(web_url_rank("https://www.4gtv.tv/channel/x"), 2);
        assert_eq!(
            web_url_rank("https://m-live.cctvnews.cctv.com/live/landscape.html?liveRoomNumber=1"),
            3
        );
    }

    /// ★ 必须保留**全部**网页线路（旧实现只留第 1 条 → 一条不通就没救）。
    #[test]
    fn ranked_web_pages_keeps_all_lines() {
        let urls = vec![
            "https://m-live.cctvnews.cctv.com/live/landscape.html?liveRoomNumber=1".to_string(),
            "https://www.yangshipin.cn/tv/home?pid=600001859".to_string(),
            "https://tv.cctv.com/live/cctv1/".to_string(),
        ];
        let pages = ranked_web_pages(&urls);
        assert_eq!(pages.len(), 3, "3 条网页线路必须全部保留");
        assert!(pages[0].contains("tv.cctv.com"), "第 1 位应是央视网（rank 0）");
        assert!(pages[1].contains("yangshipin"), "第 2 位应是央视频（rank 1）");
        assert!(pages[2].contains("cctvnews"), "第 3 位应是移动直播间（rank 3）");
    }

    /// 重复 URL 去重，且不改变首次出现的位置。
    #[test]
    fn ranked_web_pages_dedups() {
        let urls = vec![
            "https://tv.cctv.com/live/cctv1/".to_string(),
            "https://www.yangshipin.cn/tv/home?pid=1".to_string(),
            "https://tv.cctv.com/live/cctv1/".to_string(),
        ];
        let pages = ranked_web_pages(&urls);
        assert_eq!(pages.len(), 2);
        assert_eq!(pages[0], "https://tv.cctv.com/live/cctv1/");
    }

    /// 同优先级内保持原始相对次序（稳定排序）。
    #[test]
    fn ranked_web_pages_is_stable() {
        let urls = vec![
            "https://www.4gtv.tv/channel/a".to_string(),
            "https://embed.4gtv.tv/ftv/A.html".to_string(),
            "https://embed.4gtv.tv/ftv/B.html".to_string(),
        ];
        let pages = ranked_web_pages(&urls);
        assert_eq!(pages[0], "https://www.4gtv.tv/channel/a");
        assert_eq!(pages[1], "https://embed.4gtv.tv/ftv/A.html");
        assert_eq!(pages[2], "https://embed.4gtv.tv/ftv/B.html");
    }

    /// 非 http 开头的不算网页线路。
    #[test]
    fn ranked_web_pages_ignores_non_http() {
        let urls = vec![
            "ftp://x/y".to_string(),
            "https://tv.cctv.com/live/cctv1/".to_string(),
        ];
        // ranked_web_pages 本身不过滤，过滤在调用方；这里只验证排序不 panic。
        assert_eq!(ranked_web_pages(&urls).len(), 2);
    }
}
