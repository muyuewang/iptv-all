//! 官网源网页直播取流模块（B 步核心）。
//!
//! ## 为什么需要它
//!
//! 官网源（WebViewTvLive `data/4/full.dat`）的 904 个频道里 **只有 54 个带直连
//! m3u8/flv**，其余 **850 个只给网页地址**（央视 `tv.cctv.com/live/cctv1/`、
//! 央视频 `yangshipin.cn/tv/home?pid=...`、移动直播间 `m-live.cctvnews...`）。
//! 网页地址本身不是流，必须让浏览器执行页面脚本、由它自己发起 HLS 请求，
//! 把那个 m3u8 抓出来交给 mpv 硬解。
//!
//! ## 做法（沿用旧 desktop 的 cdp.py，已实测验证）
//!
//! 1. 起一个 **headless Edge** 打开官网页面；
//! 2. CDP `Network.enable` 旁听 `Network.responseReceived`；
//! 3. 命中 `.m3u8` / `.flv` / `.ts` 的响应即拿到真实流地址；
//! 4. 同时记录**页面 UA / Referer / Origin** —— 央视的 HLS CDN 会校验 Referer，
//!    缺头就 403，所以这些头必须一起交给 mpv（`--http-header-fields`）。
//!
//! ## 实测（2026-10-25）
//!
//! ```text
//! 页面 https://tv.cctv.com/live/cctv1/
//! 抓到 https://ldncctvwbcdtxy.liveplay.myqcloud.com/ldncctvwbcd/cdrmldcctv1_1/index.m3u8?b=200-2100
//! headers: user-agent=Mozilla/5.0 ... Edg/157.0.0.0, referer=https://tv.cctv.com/, origin=https://tv.cctv.com
//! mpv 实测: h264 [High] 1280x720 25fps + aac [LC] 2ch 48000Hz → 出画
//! ```

use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

/// 嗅探结果：真实流地址 + 播放必需的请求头。
#[derive(Debug, Clone, Default)]
pub struct WebStream {
    /// mpv 可直接播放的 m3u8 / flv 地址。
    pub url: String,
    /// 附加请求头（`Referer` / `User-Agent` / `Origin`），键为小写。
    pub headers: Vec<(String, String)>,
}

/// 页面 UA。央视 CDN 会校验 UA/Referer，写死一个桌面 Chrome UA 最稳。
const UA: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
                  (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0";

/// 判断一个 URL 是否是「可交给 mpv 的媒体流」。
///
/// 口径与 `net_probe::classify_media` 一致，但更严格一点：**路径本体**要有
/// `.m3u8` / `.flv` / `.ts` 后缀。`m3u8.cec-tv.com.cn/?url=...m3u8` 这种中转口
/// 返回的是 HTML 播放页，不能当流。
pub fn is_media_stream(url: &str) -> bool {
    let lower = url.to_ascii_lowercase();
    let path = lower.split(['?', '#']).next().unwrap_or("");
    path.ends_with(".m3u8") || path.ends_with(".flv") || path.ends_with(".ts")
}

/// 从页面 URL 推出 Referer（取其 origin 部分）。
///
/// 央视 CDN 校验的是 **origin**（`https://tv.cctv.com/`），不是完整页面 URL，
/// 所以要只保留 scheme://host/。
fn origin_of(page_url: &str) -> Option<String> {
    let rest = page_url.split("://").nth(1)?;
    let host = rest.split('/').next()?;
    if host.is_empty() {
        return None;
    }
    let scheme = page_url.split("://").next().unwrap_or("https");
    Some(format!("{scheme}://{host}/"))
}

/// 嗅探一个官网页面，抓真实流地址 + 请求头。
///
/// * `page_url` —— 官网页面（如 `https://tv.cctv.com/live/cctv1/`）。
/// * `timeout`  —— 最长等待（含 Edge 冷启动）。
///
/// 失败返回 Err（调用方据此换下一条线路 / 回落本机源）。
pub fn sniff_stream(page_url: &str, timeout: Duration) -> Result<WebStream, String> {
    let edge = crate::cdp::edge_path().ok_or("未找到 MS Edge（官网源网页取流依赖它）")?;
    // ★ 端口/profile 必须**每次调用都不同**：
    //   同 app 内第二次嗅探若复用同一 user-data-dir，Edge 会把新进程「委托」给
    //   已存在的实例 → 调试端口属于旧实例、页面早已加载完 → Network.enable
    //   收不到任何新请求 → 立刻「未抓到媒体流」（实测 1.7 秒就失败）。
    //   用进程内原子计数器递增，保证端口与 profile 都唯一。
    static SEQ: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
    let seq = SEQ.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let port: u16 = 9600
        + ((std::process::id() as u32 + seq * 7) % 300) as u16;
    let prof = std::env::temp_dir().join(format!(
        "clean-tv-official-{}-{}-{}",
        std::process::id(),
        port,
        seq
    ));

    // ★ 剥离代理环境变量：Edge 会读 http_proxy/https_proxy 环境变量（有则走代理）。
    //   直播页面必须直连源站，否则页面/CDN 行为与真实用户环境不一致。
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
            // 隐藏首启的各种弹窗，否则会拖慢并抢走焦点。
            "--disable-features=msEdgeSidebarV2,msEdgeShoppingAssistant",
            &format!("--remote-debugging-port={port}"),
            &format!("--user-data-dir={}", prof.display()),
            &format!("--user-agent={UA}"),
            page_url,
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("启动 headless Edge 失败: {e}"))?;
    let deadline = Instant::now() + timeout;

    // 1) 轮询 /json 找到「目标页面」的 page target。
    //    ★ 不能取第一个 type=page：headless 首启会有 edge://sync-confirmation-dialog
    //    之类的额外 target，连错 WS 永远抓不到媒体流。
    //    ★ 更不能拿 about:blank 充数：Edge 起完进程到真正导航之间会短暂出现
    //    about:blank target，连上它就永远收不到目标页的请求（实测 5.5s 就判超时）。
    //    只认 URL 里含目标域名（或其子域）的 target。
    let want_host: String = page_url
        .split("://")
        .nth(1)
        .and_then(|r| r.split('/').next())
        .unwrap_or("")
        .to_ascii_lowercase();
    let ws_url: String = loop {
        if Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err("等待 Edge CDP target 超时".into());
        }
        if let Ok(ts) = crate::cdp::cdp_json_targets(port) {
            if let Some(t) = ts.iter().find(|t| {
                t["type"] == "page"
                    && t["url"]
                        .as_str()
                        .map(|u| {
                            !want_host.is_empty()
                                && u.to_ascii_lowercase().contains(&want_host)
                        })
                        .unwrap_or(false)
            }) {
                if let Some(u) = t["webSocketDebuggerUrl"].as_str() {
                    break u.to_string();
                }
            }
        }
        std::thread::sleep(Duration::from_millis(200));
    };

    // 2) CDP 旁听：命中媒体流即停。
    let mut hit: Option<String> = None;
    let mut seen = 0usize;
    let r = crate::cdp::ws_capture_any(&ws_url, &deadline, |url| {
        seen += 1;
        if seen <= 8 {
            eprintln!("[off][req] {}", url);
        }
        if hit.is_none() && is_media_stream(url) {
            hit = Some(url.to_string());
            return true;
        }
        false
    });
    eprintln!("[off] 旁听结束 收到响应 {seen} 条 命中={}", hit.is_some());
    let _ = child.kill();
    let _ = child.wait();

    match r {
        Ok(true) => {}
        Ok(false) => return Err("官网源网页未抓到媒体流（页面未起播或线路失效）".into()),
        Err(e) => return Err(e),
    }
    let url = hit.ok_or("官网源网页未抓到媒体流")?;

    // 3) 拼请求头：Referer/Origin 用页面 origin，UA 用桌面 Chrome。
    let mut headers: Vec<(String, String)> = Vec::new();
    if let Some(o) = origin_of(page_url) {
        headers.push(("referer".to_string(), o.clone()));
        headers.push(("origin".to_string(), o));
    }
    headers.push(("user-agent".to_string(), UA.to_string()));

    Ok(WebStream { url, headers })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn media_stream_detection() {
        assert!(is_media_stream("https://a.com/live/index.m3u8?b=1"));
        assert!(is_media_stream("https://a.com/live/x.flv"));
        assert!(is_media_stream("https://a.com/seg/1.ts"));
        // 中转口返回 HTML，不是流
        assert!(!is_media_stream("https://m3u8.cec-tv.com.cn/?url=http://b/live.m3u8"));
        assert!(!is_media_stream("https://tv.cctv.com/live/cctv1/"));
        assert!(!is_media_stream(""));
    }

    #[test]
    fn origin_extraction() {
        assert_eq!(
            origin_of("https://tv.cctv.com/live/cctv1/"),
            Some("https://tv.cctv.com/".to_string())
        );
        assert_eq!(
            origin_of("https://www.yangshipin.cn/tv/home?pid=600001859"),
            Some("https://www.yangshipin.cn/".to_string())
        );
        assert_eq!(origin_of("not-a-url"), None);
    }
}
