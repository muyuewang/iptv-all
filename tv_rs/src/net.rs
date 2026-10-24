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
        .build()
        .expect("构建同步 HTTP 客户端失败")
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
pub fn blocking_fetch_channels(
    state: &ApiState,
    tid: &str,
    cli: &reqwest::blocking::Client,
) -> Result<Vec<Channel>, String> {
    let html = blocking_api_get(state, &format!("/iptve.php?tid={tid}&app=517"), 15, cli)?;
    Ok(parse_channels(&html, tid))
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