//! WebView2 网络旁听模块（官网源的命门①）。
//!
//! 背景见 `../../WRY_NETWORK_SPIKE.md`：官方 `on_web_resource_request` 只对
//! `tauri://localhost` 内部协议生效，旁听不到外部网页请求。这里走**裸 COM 直调**，
//! 拿 `PlatformWebview::controller().CoreWebView2()` 挂 `WebResourceRequested`。
//!
//! 语义关键点：WebView2 的 `WebResourceRequested` 若**不 SetResponse 就是放行**，
//! 天然符合「旁听不拦截」——与安卓 `shouldInterceptRequest 旁听 m3u8 必须 return null`
//! 同义。所以本模块只读请求 URI、绝不 SetResponse。

use webview2_com::Microsoft::Web::WebView2::Win32::{
    COREWEBVIEW2_WEB_RESOURCE_CONTEXT_ALL, ICoreWebView2, ICoreWebView2WebResourceRequestedEventArgs,
};
use webview2_com::WebResourceRequestedEventHandler;

/// 媒体流命中（用于把抓到 m3u8/flv/ts 的路由给播放层）。
#[derive(Debug, Clone)]
pub struct MediaHit {
    /// 命中的完整 URL
    pub url: String,
    /// 命中的流类型（按 URL 后缀粗判）
    pub kind: &'static str,
}

/// 旁听命中时的回调：`Result<()>`。返回 Err 不影响旁听，仅打印日志。
pub type MediaSink = Box<dyn Fn(MediaHit) + Send + 'static>;

/// 从一段 URL 判定是否媒体流，并返回类型标签。
///
/// 判据刻意保持与 `shared/wvt.py` 的 `is_direct_stream` 一致的口径（去掉 query
/// 后缀看扩展名），但这里只做「旁听命中」粗判，不追求精确。
pub fn classify_media(url: &str) -> Option<&'static str> {
    let lower = url.to_ascii_lowercase();
    // 去 query / fragment
    let path = lower.split(['?', '#']).next().unwrap_or("");
    if path.ends_with(".m3u8") {
        Some("m3u8")
    } else if path.ends_with(".flv") {
        Some("flv")
    } else if path.ends_with(".ts") {
        Some("ts")
    } else {
        None
    }
}

/// 旁听配置。
pub struct NetProbe {
    /// 域名白名单（小写）。空 = 旁听一切（调试用）。
    /// 正式使用必须设白名单，避免和 `tauri://`、`asset://` 内部协议抢请求。
    domains: Vec<String>,
    /// 媒体流命中回调（可选）。不设则只打印日志。
    sink: Option<MediaSink>,
}

impl NetProbe {
    /// 新建旁听器。
    ///
    /// * `domains` —— 要旁听的域名白名单（如 `["tv.cctv.com"]`）；空数组 = 旁听全部。
    pub fn new(domains: Vec<String>, sink: Option<MediaSink>) -> Self {
        Self {
            domains: domains.into_iter().map(|d| d.to_ascii_lowercase()).collect(),
            sink,
        }
    }

    /// 挂载旁听到指定窗口。
    ///
    /// 必须在窗口 `navigate()` 之前调用，才能抓到首页首个请求。
    pub fn attach(self, platform: tauri::webview::PlatformWebview) {
        use windows::core::HSTRING;

        // controller -> webview
        let webview: ICoreWebView2 = match unsafe { platform.controller().CoreWebView2() } {
            Ok(w) => w,
            Err(e) => {
                eprintln!("[net] 拿 ICoreWebView2 失败: {e}");
                return;
            }
        };

        // 过滤器：命门②旁听用全量 "*"，由回调内部白名单过滤。
        // （WebView2 的 AddWebResourceRequestedFilter 只支持前缀匹配，做不到
        //  任意域名联合白名单，所以过滤逻辑放在回调里，过滤器用 "*" 全量。）
        let filter = HSTRING::from("*");
        if let Err(e) = unsafe {
            webview.AddWebResourceRequestedFilter(&filter, COREWEBVIEW2_WEB_RESOURCE_CONTEXT_ALL)
        } {
            eprintln!("[net] AddWebResourceRequestedFilter 失败: {e}");
            return;
        }

        // 回调里需要把命中结果发给外部 sink。WebResourceRequested 在 UI 线程触发，
        // sink 可能要被播放层（另一线程）消费，用 mpsc + 事件通知是后续里程碑的事；
        // 这里先保持「同线程直接调用 sink」，里程碑 2 再接跨线程。
        let domains_in = self.domains.clone();
        let domains_log = self.domains.clone();
        let sink = self.sink;

        let handler = WebResourceRequestedEventHandler::create(Box::new(
            move |_sender: Option<ICoreWebView2>,
                  args: Option<ICoreWebView2WebResourceRequestedEventArgs>| {
                let Some(args) = args else {
                    return Ok(());
                };
                let request =
                    match unsafe { args.Request() } {
                        Ok(r) => r,
                        Err(e) => {
                            eprintln!("[net] args.Request() 失败: {e}");
                            return Ok(());
                        }
                    };
                let mut uri = windows::core::PWSTR::null();
                if let Err(e) = unsafe { request.Uri(&mut uri) } {
                    eprintln!("[net] request.Uri() 失败: {e}");
                    return Ok(());
                }
                let uri_str = unsafe { uri.to_string().unwrap_or_else(|_| "<bad utf16>".into()) };

                // 白名单过滤
                if !domains_in.is_empty() && !is_host_in(&domains_in, &uri_str) {
                    return Ok(());
                }

                // 媒体流命中
                if let Some(kind) = classify_media(&uri_str) {
                    eprintln!("[net][流] {kind} {uri_str}");
                    if let Some(sink) = &sink {
                        sink(MediaHit {
                            url: uri_str.clone(),
                            kind,
                        });
                    }
                    // 媒体流不做任何 SetResponse = 放行
                }
                Ok(())
            },
        ));

        let mut token: i64 = 0;
        if let Err(e) = unsafe { webview.add_WebResourceRequested(&handler, &mut token) } {
            eprintln!("[net] add_WebResourceRequested 失败: {e}");
            return;
        }
        eprintln!(
            "[net] 旁听挂载成功 token={token} domains={:?}",
            domains_log
        );
        // handler 由 WebView2 COM 引用计数兜底，泄漏避免提前 drop 致回调失效。
        std::mem::forget(handler);
    }
}

/// 从 URL 提取 host 并做白名单匹配（小写）。
fn is_host_in(domains: &[String], url: &str) -> bool {
    let host = url
        .split("://")
        .nth(1)
        .and_then(|rest| rest.split('/').next())
        .unwrap_or("")
        .to_ascii_lowercase();
    domains
        .iter()
        .any(|d| host == *d || host.ends_with(&format!(".{d}")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classify_media_detects_stream_suffix() {
        assert_eq!(classify_media("http://a.com/live.m3u8"), Some("m3u8"));
        assert_eq!(classify_media("http://a.com/x.flv?a=1"), Some("flv"));
        assert_eq!(classify_media("http://a.com/seg001.ts"), Some("ts"));
        // 大小写不敏感
        assert_eq!(classify_media("http://a.com/X.M3U8"), Some("m3u8"));
        // 域名含 m3u8 但路径无后缀 —— 必须判 None（与 wvt.py 口径一致）
        assert_eq!(classify_media("http://m3u8.cec-tv.com.cn/?url=abc"), None);
        assert_eq!(classify_media("http://tv.cctv.com/live/cctv1/"), None);
        assert_eq!(classify_media(""), None);
    }

    #[test]
    fn host_whitelist_matches() {
        let domains = vec!["tv.cctv.com".to_string()];
        // 精确匹配
        assert!(is_host_in(&domains, "http://tv.cctv.com/live/cctv1/"));
        // 子域匹配
        assert!(is_host_in(&domains, "https://news.tv.cctv.com/x.m3u8"));
        // 非白名单域不匹配
        assert!(!is_host_in(&domains, "https://www.baidu.com/"));
        // 大小写不敏感
        assert!(is_host_in(&domains, "http://TV.CCTV.COM/a"));
    }

    #[test]
    fn empty_whitelist_matches_nothing() {
        // is_host_in 是「严格匹配」：空名单不命中任何 URL。
        // 「空白名单 = 旁听一切」由 attach 里的 `!domains_in.is_empty() &&` 短路实现，
        // 不是本函数的职责。
        let domains: Vec<String> = vec![];
        assert!(!is_host_in(&domains, "https://anything.example.com/x"));
    }
}