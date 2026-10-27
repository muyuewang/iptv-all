//! 真实网络抓取层，对应 `shared/core.py` 的网络部分（`api_get` / `fetch_channels` /
//! `fetch_play` / `headers` / `_ssl_ctx`）。
//!
//! 设计要点（与 Python 版逐条对齐）：
//! - **同源解密**：`api_get` 命中哪个镜像，就把全局 `API` 切到该镜像，
//!   保证「抓取页面」与「解密时自造的 url0」用同一个域名。
//! - **多镜像回退**：主站最多 8s、后续镜像各 5s，健康镜像实测 <1s。
//! - **放宽证书校验**：对齐 `_ssl_ctx`（`verify_mode=CERT_NONE`），源站证书链
//!   不完整也能读公开直播页。
//! - **App UA 是命门**：UA 里 `LT-APP/48/517` 是接口校验点，必须原样带。
//!
//! 本模块仍是**纯逻辑 + 网络**（不依赖 GUI / Windows），可在 core crate 里单元测试
//! （镜像回退 / 同源切换 / 解析组合），真实外网抓取用 `#[ignore]` 标记手动跑。

use crate::core::{decrypt_page, parse_channels, parse_epg, Channel, EpgItem};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::RwLock;

/// App 请求的固定 UA（对应 Python `APP_UA`）。
/// ★ 命门：`LT-APP/48/517` 是接口校验点，改了就抓不到数据。
pub const APP_UA: &str = "Mozilla/5.0 (Linux; Android 9; SM-N9700 Build/PQ3B.190801.01311438; wv) \
AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 \
Mobile Safari/537.36 diashizhb LT-APP/48/517/YM-RT/";

/// 默认生效接口域名。
pub const API_DEFAULT: &str = "https://api1.2026016.xyz";

/// 同源镜像（实测均可返回同一套数据）；主站不通时按顺序回退。
pub const API_MIRRORS: [&str; 3] = [
    "https://api1.2026016.xyz",
    "https://api.2026016.xyz",
    "https://api2.2026016.xyz",
];

/// 频道分类（tid, 显示名），对应 `CATS`。
pub const CATS: [(&str, &str); 6] = [
    ("tv", "综合"),
    ("ty", "体育"),
    ("ys", "央视"),
    ("ws", "卫视"),
    ("wintv123", "极速港澳台"),
    ("gt", "港澳台"),
];

/// 请求头（对应 Python `headers()`）。
pub fn headers() -> reqwest::header::HeaderMap {
    let mut h = reqwest::header::HeaderMap::new();
    h.insert(reqwest::header::USER_AGENT, APP_UA.parse().unwrap());
    h.insert(
        reqwest::header::HeaderName::from_static("x-requested-with"),
        "com.sldv.okstuffx".parse().unwrap(),
    );
    // 命门 cookie：appVer=517 与 UA 里的 517 呼应。
    h.insert(
        reqwest::header::COOKIE,
        "adReward=1; iptvad=1; appVer=517".parse().unwrap(),
    );
    h.insert(reqwest::header::ACCEPT, "*/*".parse().unwrap());
    h.insert(
        reqwest::header::ACCEPT_LANGUAGE,
        "zh-CN,zh;q=0.9".parse().unwrap(),
    );
    h
}

/// 构建一个「放宽证书校验」的 HTTP 客户端（对应 `_ssl_ctx`）。
pub fn client() -> reqwest::Client {
    reqwest::Client::builder()
        // ★ 关键：USE_NATIVE_TLS。rustls 的 ring provider 在 WebView2 进程里首次握手
        //   会死锁（见 app 排查结论），用 Windows 自带 Schannel 规避。
        .use_native_tls()
        .danger_accept_invalid_certs(true)
        .default_headers(headers())
        // ★ no_proxy()：**禁用环境变量代理**。reqwest 默认会读 http_proxy/https_proxy，
        //   一旦环境里有代理（本机沙箱就是 127.0.0.1:12244），线路探测会全部走代理
        //   → 探速只命中 1 条、mpv 拿到的是代理错误页 → 表现为「12s 未出画面」。
        //   直播流必须直连源站，不走任何中间代理。
        .no_proxy()
        .build()
        .expect("构建 HTTP 客户端失败")
}

/// 抓取状态：当前生效的接口域名 + 用户偏好镜像。
///
/// `api` 用 `RwLock` 保存「当前生效镜像」，`_pref_mirror` 对应 Python 的
/// `_pref_mirror`（设置页可改，这里先由 `set_pref_mirror` 提供入口）。
#[derive(Default)]
pub struct ApiState {
    pub api: RwLock<String>,
    pub pref: RwLock<Option<String>>,
    /// 回退序号（测试用：可注入「先失败前 N 个镜像」）
    fail_first: AtomicUsize,
}

impl Clone for ApiState {
    /// 克隆出一个**独立**的镜像状态（`fail_first` 测试位不复刻，保持 0）。
    /// 用于把「网络抓取」搬到独立线程运行时（避免 reqwest 复用 Tauri runtime）。
    fn clone(&self) -> Self {
        Self {
            api: RwLock::new(self.api.read().unwrap().clone()),
            pref: RwLock::new(self.pref.read().unwrap().clone()),
            fail_first: AtomicUsize::new(0),
        }
    }
}

impl ApiState {
    pub fn new() -> Self {
        Self {
            api: RwLock::new(API_DEFAULT.to_string()),
            pref: RwLock::new(None),
            fail_first: AtomicUsize::new(0),
        }
    }

    /// 当前生效镜像（克隆出来，避免跨线程持锁）。
    pub fn current_api(&self) -> String {
        self.api.read().unwrap().clone()
    }

    /// 设置用户偏好镜像（`None` = 自动回退）。
    pub fn set_pref_mirror(&self, host: Option<String>) {
        let mut g = self.pref.write().unwrap();
        *g = host.map(|h| {
            let h = h.trim().trim_end_matches('/').to_string();
            if h.starts_with("http") {
                h
            } else {
                format!("https://{h}")
            }
        });
    }
}

/// 决定「本轮要尝试的镜像顺序」（对应 Python `api_get` 里的 hosts 组装）。
fn mirror_order(state: &ApiState) -> Vec<String> {
    let mut hosts: Vec<String> = Vec::new();
    if let Some(p) = state.pref.read().unwrap().clone() {
        hosts.push(p);
    }
    let cur = state.api.read().unwrap().clone();
    if !hosts.contains(&cur) {
        hosts.push(cur);
    }
    for m in API_MIRRORS {
        if !hosts.contains(&m.to_string()) {
            hosts.push(m.to_string());
        }
    }
    hosts
}

/// 带 App UA 的 GET，返回 UTF-8 文本（对应 `http_get`）。
pub async fn http_get(
    url: &str,
    timeout_secs: u64,
    cli: &reqwest::Client,
) -> Result<String, String> {
    let resp = cli
        .get(url)
        .timeout(std::time::Duration::from_secs(timeout_secs))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status();
    let body = resp.text().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}"));
    }
    Ok(body)
}

/// 请求接口，自动镜像回退；命中后切全局 API（对应 `api_get`）。
///
/// * `path` —— 以 `/` 开头的接口路径。
/// * `timeout_secs` —— 主站封顶时长（后续镜像用 min(5, 该值)）。
pub async fn api_get(
    state: &ApiState,
    path: &str,
    timeout_secs: u64,
    cli: &reqwest::Client,
) -> Result<String, String> {
    let hosts = mirror_order(state);
    let mut last_err: Option<String> = None;
    let fail_n = state.fail_first.load(Ordering::SeqCst);

    for (i, m) in hosts.iter().enumerate() {
        // 测试注入：前 fail_n 个镜像直接判失败。
        if i < fail_n {
            last_err = Some(format!("[注入失败] {m}"));
            continue;
        }
        // ★ 超时封顶：主站最多 8s、后续镜像各 5s。
        let t = if i == 0 {
            timeout_secs.min(8)
        } else {
            timeout_secs.min(5)
        };
        match http_get(&format!("{m}{path}"), t, cli).await {
            Ok(txt) if txt.len() > 200 => {
                // 命中：切全局 API 到该镜像（同源解密的关键）。
                *state.api.write().unwrap() = m.clone();
                return Ok(txt);
            }
            Ok(txt) => last_err = Some(format!("响应过短({}字节)", txt.len())),
            Err(e) => last_err = Some(format!("{m}: {e}")),
        }
    }
    Err(last_err.unwrap_or_else(|| "所有接口镜像均不可用".to_string()))
}

/// 拉取某分类频道列表（对应 `fetch_channels`）。
pub async fn fetch_channels(
    state: &ApiState,
    tid: &str,
    cli: &reqwest::Client,
) -> Result<Vec<Channel>, String> {
    let html = api_get(state, &format!("/iptve.php?tid={tid}&app=517"), 15, cli).await?;
    Ok(parse_channels(&html, tid))
}

/// 抓播放页 + 解密，返回 (urls, epg, msg)（对应 `fetch_play`）。
pub async fn fetch_play(
    state: &ApiState,
    tid: &str,
    cid: &str,
    cli: &reqwest::Client,
) -> Result<(Vec<String>, Vec<EpgItem>, String), String> {
    let html = api_get(
        state,
        &format!("/iptve.php?act=play&tid={tid}&id={cid}"),
        15,
        cli,
    )
    .await?;
    // ★ 同源解密：decrypt_page 自造的 url0 用「当前生效镜像」做域名。
    let api = state.current_api();
    let res = decrypt_page(&html, tid, cid, &api);
    Ok((res.urls, parse_epg(&html), res.msg))
}

// ==================================================================
// 同步阻塞(blocking)网络抓取层
// ==================================================================
// ★ 根因背景：reqwest 的 **async 客户端**（无论 rustls 还是 native-tls/Schannel）在
//   Tauri 的 tokio runtime 里发 HTTPS 请求后，命令返回值无法经 IPC 回传前端
//   （前端 `await invoke` 永久挂起；纯计算命令正常）。这是 webview 进程里的
//   async TLS 与 tokio 调度相互干扰所致。
//
//   解法：改用 reqwest **blocking 同步客户端**（内部用系统线程 + Schannel 阻塞 API，
//   与 tokio 零交互），在 app 层用 `spawn_blocking`/独立线程调用。同步客户端在
//   Windows 上走的 Schannel 阻塞式握手不依赖 tokio 调度，稳定可靠。

/// 构建同步阻塞客户端（与异步版同一套 header + 放宽证书）。
pub fn blocking_client() -> reqwest::blocking::Client {
    reqwest::blocking::Client::builder()
        .use_native_tls()
        .danger_accept_invalid_certs(true)
        .default_headers(headers())
        // ★ 同上：禁用环境变量代理，直播线路直连源站。
        .no_proxy()
        .build()
        .expect("构建同步 HTTP 客户端失败")
}

/// 不跟随重定向的同步客户端（探活用）。
///
/// 用途：直播线路探活时**只跟到 302 就停**。跟到底会真的读 1KB FLV 正文，
/// 等于替 mpv 提前消耗一次源站的流配额 —— 实测探速「用掉」最好的线路后，
/// mpv 再播同一条要等 9 秒才出画。只看 302 存在与否就能判断节点是否活着。
pub fn blocking_client_no_redirect() -> reqwest::blocking::Client {
    reqwest::blocking::Client::builder()
        .use_native_tls()
        .danger_accept_invalid_certs(true)
        .default_headers(headers())
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .expect("构建不跟随重定向的同步 HTTP 客户端失败")
}

/// ★★ 把「源站中转 URL」解析成「最终真实流 URL」（2026-10-26 新增）。
///
/// ★ 为什么需要：app 版本机源的线路形态是
///   `https://api1.2026016.xyz/eplay2.php?token=..&tid=ty&id=28&p=2&type=.flv`
///   —— **必须先 302 跳到 CDN 才是真流**：
///   ```
///   eplay2.php ──302──> https://t41.iptv200.com:8443/live/eltasport1.flv?sign=..
///   ```
///   实测（同一时刻、同一线路）：
///     · **只取 302 响应头（不跟随）→ 0.20 ~ 0.25 秒**
///     · CDN 直连 `t41.iptv200.com:8443` → **0.6 秒**
///     · 而 mpv 自己跟 302 并读流 → 首跳拖到 **11.8 ~ 20.9 秒**
///       （源站 TLS 握手常超时，实测 6 次里 4 次超时）
///   mpv 的出画窗口是 12s，被源站首跳吃光 → **明明活着的线路被判死**
///   （实测 gt:50 愛爾達體育1台：mpv 日志 11.1s 才连上源站，看门狗已耗尽）。
///
/// ★★ 为什么必须**递归**（多跳）：源站的 302 目标**有两类**，实测（ys:1，13 条线路）：
///   ```
///   p=0 → https://t40.iptv200.com:8443/live/cctv1.flv?sign=1792964300-1a2f…   ← 直连真流
///   p=4 → https://api1.2026016.xyz/v/migu8.php?token=PFZuM7UB%2F…&id=cctv1   ← 二级中转
///   p=5 → https://api1.2026016.xyz/v/cctv.php?token=OQJrN7cK%2B…&id=cctv1    ← 二级中转
///   ```
///   `/v/*.php` 还要再 302 一次才是真流（实测 `/v/migu8.php` 单独请求返 **HTTP 500**，
///   必须带 Location 里给的 token&id 参数才行）。
///   所以：跟到 Location，若**仍是中转口**（路径以 `.php` 结尾）就带着它的参数继续跟，
///   直到拿到真正的媒体地址（最多 4 跳）。
///
/// ★ 只取响应头、**不跟随、不读正文**：跟随重定向后 reqwest 会去读 FLV 正文，
///   而 FLV 是**永不结束的流** → 读操作被挂住直到超时（实测 11.8~20.9s）。
///
/// 返回 `Ok(真流URL)` = 解析成功；`Err` = 不可解析（调用方应回退到原 URL）。
pub fn resolve_stream_url(url: &str, timeout_ms: u64) -> Result<String, String> {
    let cli = blocking_client_no_redirect();
    let mut cur = url.to_string();
    // ★ 最多 4 跳：eplay2 →(/v/*.php) →(媒体地址)。超过就认为解析失败。
    for _ in 0..4 {
        let resp = cli
            .get(&cur)
            .timeout(std::time::Duration::from_millis(timeout_ms))
            .send()
            .map_err(|e| e.to_string())?;
        let status = resp.status();
        if status.is_redirection() {
            let loc = resp
                .headers()
                .get(reqwest::header::LOCATION)
                .and_then(|v| v.to_str().ok())
                .filter(|s| !s.is_empty())
                .ok_or_else(|| format!("HTTP {} 但无 Location 头", status.as_u16()))?;
            // 相对 Location 补全成绝对 URL。
            let next = if loc.starts_with("http://") || loc.starts_with("https://") {
                loc.to_string()
            } else if let Ok(base) = reqwest::Url::parse(&cur) {
                base.join(loc)
                    .map(|u| u.to_string())
                    .unwrap_or_else(|_| loc.to_string())
            } else {
                loc.to_string()
            };
            // ★ 还 是中转口（.php）→ 继续跟；否则就是真流地址了。
            let path = reqwest::Url::parse(&next)
                .map(|u| u.path().to_ascii_lowercase())
                .unwrap_or_default();
            if path.ends_with(".php") {
                cur = next;
                continue;
            }
            return Ok(next);
        }
        if status.is_success() {
            // 直接就是流（没有中转）→ 原样返回，让 mpv 自己处理。
            return Ok(cur);
        }
        return Err(format!("HTTP {}", status.as_u16()));
    }
    Err("重定向跳数过多（>4）".to_string())
}


/// 同步 GET，返回 UTF-8 文本。
fn blocking_http_get(url: &str, timeout_secs: u64, cli: &reqwest::blocking::Client) -> Result<String, String> {
    let resp = cli
        .get(url)
        .timeout(std::time::Duration::from_secs(timeout_secs))
        .send()
        .map_err(|e| e.to_string())?;
    let status = resp.status();
    let body = resp.text().map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}"));
    }
    Ok(body)
}

/// 同步版 api_get（镜像回退 + 同源切换）。
pub fn blocking_api_get(
    state: &ApiState,
    path: &str,
    timeout_secs: u64,
    cli: &reqwest::blocking::Client,
) -> Result<String, String> {
    let hosts = mirror_order(state);
    let mut last_err: Option<String> = None;
    let fail_n = state.fail_first.load(Ordering::SeqCst);

    for (i, m) in hosts.iter().enumerate() {
        if i < fail_n {
            last_err = Some(format!("[注入失败] {m}"));
            continue;
        }
        let t = if i == 0 { timeout_secs.min(8) } else { timeout_secs.min(5) };
        match blocking_http_get(&format!("{m}{path}"), t, cli) {
            Ok(txt) if txt.len() > 200 => {
                *state.api.write().unwrap() = m.clone();
                return Ok(txt);
            }
            Ok(txt) => last_err = Some(format!("响应过短({}字节)", txt.len())),
            Err(e) => last_err = Some(format!("{m}: {e}")),
        }
    }
    Err(last_err.unwrap_or_else(|| "所有接口镜像均不可用".to_string()))
}

/// 同步版 fetch_channels。
///
/// ★ 双路由回退（港澳台实时化，2026-10-26）：源站按分类对媒体路由支持不均，
///   历史上 `tv`/`gt`/`wintv123` 在 app 路由（`&app=517`）下**恒 404**，而
///   普通路由（不带 `app`）正常。为让港澳台等分类实时可用、且不再依赖
///   full.dat 快照，这里先走 app 路由，抓不到或解析为空就退回普通路由重抓。
pub fn blocking_fetch_channels(
    state: &ApiState,
    tid: &str,
    cli: &reqwest::blocking::Client,
) -> Result<Vec<Channel>, String> {
    let html = blocking_api_get(state, &format!("/iptve.php?tid={tid}&app=517"), 15, cli)?;
    let chans = parse_channels(&html, tid);
    if !chans.is_empty() {
        return Ok(chans);
    }
    // app 路由 404/空 → 退普通路由重抓一次（源站偶发只对部分分类开普通路由）。
    let html2 = blocking_api_get(state, &format!("/iptve.php?tid={tid}"), 15, cli)?;
    Ok(parse_channels(&html2, tid))
}

/// 同步版 fetch_play。
pub fn blocking_fetch_play(
    state: &ApiState,
    tid: &str,
    cid: &str,
    cli: &reqwest::blocking::Client,
) -> Result<(Vec<String>, Vec<EpgItem>, String), String> {
    let html = blocking_api_get(
        state,
        &format!("/iptve.php?act=play&tid={tid}&id={cid}"),
        15,
        cli,
    )?;
    let api = state.current_api();
    let res = decrypt_page(&html, tid, cid, &api);
    Ok((res.urls, parse_epg(&html), res.msg))
}

// ==================================================================
// 本机源（WebViewTvLive）数据下载层
// ==================================================================
//
// 本机源频道表托管在 GitHub（`hxh19950701/WebViewTvLive` 的 `data/4/full.dat`），
// 是 AES-128-CBC 加密 + gzip 的 JSON（904 频道 / 1114 线路）。走 raw + 多代理
// 镜像回退，**与官方源 `api2.2026016.xyz` 完全独立**——官方源限流不影响本机源。
//
// 本机源线路构成：1114 条里仅 61 条直连（m3u8/flv，mpv 可直接播），其余 1053 条
// 是网页线路（需 WebView2 嗅探，暂不在本模块处理）。这里只接「直连」增量。

use crate::wvt::{decrypt_dat_json, is_direct_stream, WvtChannel};

/// 本机源 GitHub 仓库信息（对应 Python `wvt.py`）。
const WVT_OWNER: &str = "hxh19950701";
const WVT_REPO: &str = "WebViewTvLive";
const WVT_BRANCH: &str = "main";
const WVT_DATA: &str = "data/4/full.dat";

/// raw 直连地址 + 保底镜像（prefix 型 GitHub 代理）。
const WVT_RAW_URL: &str = "https://raw.githubusercontent.com/hxh19950701/WebViewTvLive/main";
const WVT_PREFIX_PROXIES: [&str; 4] = [
    "https://ghproxy.net",
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://ghproxy.cc",
];
const WVT_HOST_PROXIES: [&str; 2] = ["https://raw.gitmirror.com", "https://kkgithub.com"];
const WVT_JSDELIVR: [&str; 2] = ["https://fastly.jsdelivr.net", "https://cdn.jsdelivr.net"];

/// 生成本机源 `full.dat` 的候选下载地址（多镜像回退顺序）。
///
/// 对应 Python `_candidate_urls`：直连 → jsDelivr → host 代理 → prefix 代理。
fn wvt_candidate_urls() -> Vec<String> {
    let raw = format!("{WVT_RAW_URL}/{WVT_DATA}");
    let mut out: Vec<String> = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    let mut add = |u: String| {
        if !u.is_empty() && !seen.contains(&u) {
            seen.push(u.clone());
            out.push(u);
        }
    };

    add(raw.clone());
    for cdn in WVT_JSDELIVR {
        add(format!("{cdn}/gh/{WVT_OWNER}/{WVT_REPO}@{WVT_BRANCH}/{WVT_DATA}"));
    }
    for h in WVT_HOST_PROXIES {
        add(format!(
            "{}/{}/{}/{}/{}",
            h.trim_end_matches('/'),
            WVT_OWNER,
            WVT_REPO,
            WVT_BRANCH,
            WVT_DATA
        ));
    }
    for p in WVT_PREFIX_PROXIES {
        add(format!("{}/{}", p.trim_end_matches('/'), raw));
    }
    out
}

/// 下载本机源 `full.dat` 并解密成频道表（多镜像回退）。
///
/// 对应 Python `load_channels` 的联网分支（`download_dat` + `decrypt_dat_json`）。
/// 用 blocking 客户端（复用同一套放宽证书 + 零环境代理的 reqwest 栈）。
pub fn blocking_fetch_wvt_channels(
    cli: &reqwest::blocking::Client,
) -> Result<Vec<WvtChannel>, String> {
    let mut last_err = String::from("全部镜像均不可用");
    for url in wvt_candidate_urls() {
        let bytes: Vec<u8> = match cli
            .get(&url)
            .timeout(std::time::Duration::from_secs(12))
            .send()
            .and_then(|r| r.bytes().map(|b| b.to_vec()))
        {
            Ok(b) if b.len() > 64 => b,
            Ok(_) => {
                last_err = "响应过小".into();
                continue;
            }
            Err(e) => {
                last_err = e.to_string();
                continue;
            }
        };
        match decrypt_dat_json(&bytes) {
            Some(v) => {
                if let Ok(chans) = serde_json::from_value::<Vec<WvtChannel>>(v) {
                    return Ok(chans);
                }
                last_err = "频道表 JSON 结构不符".into();
            }
            None => last_err = "解密失败".into(),
        }
    }
    Err(last_err)
}

/// 从本机源频道表里提取「直连线路」频道，返回 (频道名, 直连 URL)。
///
/// 只取每个频道第 1 条直连线路（mpv 直接可播），忽略网页线路。
/// 对应 `split_sources` 里的 direct 分支 + 去重取首条。
pub fn wvt_direct_lines(channels: &[WvtChannel]) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for c in channels {
        if let Some(u) = c.urls.iter().find(|u| is_direct_stream(&u.url)) {
            out.push((c.name.clone(), u.url.clone()));
        }
    }
    out
}

// ==================================================================
// 789iptv 频道表抓取层（替代过时的 WebViewTvLive data/4 本机源）
// ==================================================================
//
// 789iptv.com（原 678iptv.com）是 jquery mobile 电视直播站，分类页 URL 形如
// `https://789iptv.com/?act=<tid>`，频道条目结构 `?act=play&tid=x&id=y`，与
// 官方源完全相同，可直接复用 `parse_channels`（见 core.rs）。789 分类页返回的是
// 明文 HTML（无加密），抓取稳定、无签名时效问题，适合启动预抓进缓存。
//
// 5 分类：tv(综合) / ty(体育) / ys(央视) / ws(卫视) / gt(港澳台)。

/// 789iptv 分类（tid -> 显示名）。与官方源 CATS 前 5 项对齐。
pub const CATS_789: [(&str, &str); 5] = [
    ("tv", "综合"),
    ("ty", "体育"),
    ("ys", "央视"),
    ("ws", "卫视"),
    ("gt", "港澳台"),
];

/// 789iptv 首页域名。
pub const HOME_789: &str = "https://789iptv.com";

/// 同步抓取 789iptv 某分类的频道列表（明文 HTML，直接 parse_channels）。
///
/// 用 blocking 客户端，独立于官方源的 `ApiState`（789 无需镜像回退/同源解密）。
pub fn blocking_fetch_789_channels(
    tid: &str,
    cli: &reqwest::blocking::Client,
) -> Result<Vec<Channel>, String> {
    // ★ 789 的分类页 URL 是 `?tid=<tid>`（无 act= 前缀），与播放页 `?act=play&...` 不同。
    //   实测 `?act=<tid>` 只有 tv 能解析（首页默认），ty/ys/ws/gt 全 0 频道。
    let url = format!("{HOME_789}/?tid={tid}");
    let html = blocking_http_get(&url, 15, cli)?;
    let chans = parse_channels(&html, tid);
    if chans.is_empty() {
        return Err(format!("分类 {tid} 解析出 0 频道（页面结构可能变化）"));
    }
    Ok(chans)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 镜像顺序：pref 优先，然后是当前 api，最后补全所有镜像且去重。
    #[test]
    fn mirror_order_pref_then_current_then_all() {
        let st = ApiState::new();
        st.set_pref_mirror(Some("https://my.mirror.example".into()));
        let hosts = mirror_order(&st);
        assert_eq!(hosts[0], "https://my.mirror.example");
        // 后面不重复 pref
        assert!(!hosts[1..].contains(&"https://my.mirror.example".to_string()));
        // 当前 api 一定在列
        assert!(hosts.contains(&API_DEFAULT.to_string()));
        // 所有镜像都在
        for m in API_MIRRORS {
            assert!(hosts.contains(&m.to_string()));
        }
    }

    /// pref 不设置时，当前 api 排最前。
    #[test]
    fn mirror_order_current_first_without_pref() {
        let st = ApiState::new();
        let hosts = mirror_order(&st);
        assert_eq!(hosts[0], API_DEFAULT);
    }

    /// 请求头包含命门 UA 与 appVer cookie。
    #[test]
    fn headers_contain_lifeline() {
        let h = headers();
        let ua = h.get(reqwest::header::USER_AGENT).unwrap().to_str().unwrap();
        assert!(ua.contains("LT-APP/48/517"));
        let cookie = h
            .get(reqwest::header::COOKIE)
            .unwrap()
            .to_str()
            .unwrap();
        assert!(cookie.contains("appVer=517"));
    }
}