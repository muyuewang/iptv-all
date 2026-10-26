//! 789iptv 实时取流模块（替代过时的 WebViewTvLive data/4 本机源）。
//!
//! 背景：本机源 data/4 直连大面积失效（51 中 23 失效），作者迁移到 data/5 且
//! 密钥藏闭源 APK 无从反推；而 789iptv.com（原 678iptv.com）网页版直播稳定可播。
//! 因此本模块把「789iptv 网页直播」接进 Rust 端作为新的可播源。
//!
//! 已破解的播放链路（实测验证，见 `decode_678.js` / `cdp_signs.py`）：
//!   1. 频道表：`https://789iptv.com/?act=<tid>` 分类页，结构同官方源
//!     （`?act=play&tid=x&id=y`），可直接用 `clean_tv_core::core::parse_channels` 解析。
//!     6 分类：tv(综合) / ty(体育) / ys(央视) / ws(卫视) / gt(港澳台) + wintv123。
//!   2. 播放页 `?act=play&tid=x&id=y` 内含混淆 JS（变量名随机），页面运行时自行
//!     解密出中转 URL：`https://p.iptv200.com/play.php?token=...&tid=x&id=y&p=0&type=.flv`。
//!   3. `play.php` 中转**必须浏览器环境**（curl / mpv 直连均 404），302 到真实流：
//!     `https://t{编号}.iptv200.com:8443/live/{缩写}.flv?sign={unix时间戳}-{md5}`。
//!   4. 真实 flv 的 sign 时效**分钟级**，必须实时抓取；CDN 证书无效需 `--tls-verify=no`。
//!
//! 架构决策：因为「解密 JS 混淆变量名随机」 + 「play.php 必须浏览器」 + 「sign 必须
//! 实时」，最可靠的取流路径是——**起一个 headless Edge 打开播放页，让页面自己解密
//! 并自动起播，再用 CDP (Chrome DevTools Protocol) 旁听抓真实 flv URL**。这完全绕开
//! 解密混淆 JS（页面自己解），也天然满足 play.php 的浏览器环境要求。
//!
//! 本模块只做「起 Edge + CDP 抓流」，网络旁听用 WebSocket 客户端与 headless Edge
//! 的 `--remote-debugging-port` 通信。抓到的真实 flv URL 交给 `player::Player` 播。

use crate::cdp::{cdp_json_targets, edge_path};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

/// 789iptv 首页域名（原 678iptv.com 已迁移到 789iptv.com）。
pub const HOME: &str = "https://789iptv.com";

/// 取流结果：真实的、mpv 可直接播放的 flv/m3u8 URL。
#[derive(Debug, Clone)]
pub struct StreamHit {
    pub url: String,
    /// 供播放层判断需不需要 `--tls-verify=no`（789 真实 CDN 证书无效，恒为 true）。
    pub no_verify: bool,
}

/// 起一个 headless Edge，打开 789iptv 播放页，旁听抓真实流。
///
/// * `tid` / `cid` —— 频道分类与 id。
/// * `timeout` —— 最长等待时长。
///
/// 流程：起 Edge（`--headless=new --remote-debugging-port=<随机端口>`）→ 轮询 `/json`
/// 拿到 page target 的 `webSocketDebuggerUrl` → 连 WS → `Network.enable` →
/// 收 `Network.responseReceived`，命中 `/live/*.flv?sign=` 即返回。
pub fn fetch_stream(tid: &str, cid: &str, timeout: Duration) -> Result<StreamHit, String> {
    let edge = edge_path().ok_or("未找到 MS Edge（headless 抓流依赖它）")?;
    // 随机端口：进程内唯一，避免多并发取流互相打架。
    let port: u16 = 9300 + (std::process::id() % 500) as u16;
    // 独立 user-data-dir：避免影响用户正常 Edge，也避免锁冲突。
    let prof = std::env::temp_dir().join(format!("clean-tv-789-{}-{}", std::process::id(), port));
    let url = format!("{HOME}/?act=play&tid={tid}&id={cid}");

    // ★ 剥离代理环境变量（直播必须直连源站，不能走中间代理）。
    let mut edge_env = std::env::vars_os()
        .filter(|(k, _)| {
            let key = k.to_string_lossy().to_ascii_lowercase();
            !matches!(
                key.as_str(),
                "http_proxy" | "https_proxy" | "all_proxy" | "ftp_proxy" | "no_proxy"
            )
        })
        .collect::<Vec<_>>();

    let mut child: Child = Command::new(&edge)
.env_clear()
.envs(edge_env.drain(..))
.args([
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-extensions",
                "--mute-audio",
                "--no-first-run",
                "--no-default-browser-check",
                &format!("--remote-debugging-port={port}"),
                &format!("--user-data-dir={}", prof.display()),
                &url,
            ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("启动 headless Edge 失败: {e}"))?;

    let deadline = Instant::now() + timeout;

    // 1) 轮询 /json 直到出现「789iptv 播放页」的 page target。
    //    ★ 关键：headless 首次启动会弹出 edge://sync-confirmation-dialog/ 等额外
    //    page target，不能随便取第一个 type=page。必须找 url 含 789iptv 的 target，
    //    否则连错 WS 永远抓不到媒体流。
    let ws_url: String = loop {
        if Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err("等待 Edge CDP target 超时".into());
        }
        if let Ok(ts) = cdp_json_targets(port) {
            if let Some(t) = ts
                .iter()
                .find(|t| t["type"] == "page" && t["url"].as_str().map(|u| u.contains("789iptv")).unwrap_or(false))
            {
                if let Some(u) = t["webSocketDebuggerUrl"].as_str() {
                    break u.to_string();
                }
            }
        }
        std::thread::sleep(Duration::from_millis(250));
    };

    // 2) 连 WS（CDP 公共模块），只认「/live/*.flv?sign=」这种真流。
    let mut hit: Option<StreamHit> = None;
    let r = crate::cdp::ws_capture_any(&ws_url, &deadline, |url| {
        if hit.is_none() && url.contains("/live/") && url.contains(".flv") && url.contains("sign=") {
            hit = Some(StreamHit { url: url.to_string(), no_verify: true });
            return true;
        }
        false
    });
    let _ = child.kill();
    let _ = child.wait();
    match r {
        Ok(true) => hit.ok_or_else(|| "未抓取到真实流（页面可能未自动起播或线路失效）".to_string()),
        Ok(false) => Err("未抓取到真实流（页面可能未自动起播或线路失效）".into()),
        Err(e) => Err(e),
    }
}

