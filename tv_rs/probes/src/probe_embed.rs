// 纯净电视 · 内嵌 WebView2 旁听探针（验证治本路线能否抓到真实 m3u8）
//
// 目标：验证「内嵌 WebView2 + 裸 COM WebResourceRequested」这条治本路线，
//   能不能在**本进程内**抓到官网页面发出的真实媒体流 URL **和真实请求头**。
//
// 与 spike_net.rs 的区别：
//   spike_net 只打印 URI（且跑在独立进程里）；
//   本探针打印**命中的媒体流 + 浏览器实际发出的请求头**（referer/origin/cookie/ua），
//   因为这些头必须原样交给 mpv（央视 CDN 缺 Referer 会 403）。
//
// 用法（Windows only）：
//   cargo run -p clean-tv-app --bin probe_embed -- <目标URL>
//   不传 URL 则默认 https://tv.cctv.com/live/cctv1/
//
// 判据：
//   ① stderr 出现 [embed][HIT] 且 URL 以 .m3u8 结尾 → 治本路线成立
//   ② 头里能看到 referer → 说明真实请求头可取，mpv 能带上它

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::{Duration, Instant};

use webview2_com::Microsoft::Web::WebView2::Win32::{
    ICoreWebView2, ICoreWebView2WebResourceRequestedEventArgs,
};
use webview2_com::WebResourceRequestedEventHandler;

fn main() {
    let url = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "https://tv.cctv.com/live/cctv1/".to_string());

    let (tx, rx) = std::sync::mpsc::channel::<(String, Vec<(String, String)>)>();

    tauri::Builder::default()
        .setup(move |app| {
            eprintln!("[probe] setup 进入");
            eprintln!("[probe] 准备建窗");
            let builder = tauri::WebviewWindowBuilder::new(
                app,
                "probe-embed",
                tauri::WebviewUrl::External("https://example.invalid/".parse().unwrap()),
            )
            .title("embed probe")
            .inner_size(320.0, 180.0)
            .position(-32000.0, -32000.0)
            .skip_taskbar(true)
            .decorations(false)
            .focusable(false)
            // ★ UA 必须与 CDP 方案（official_web.rs 的 UA 常量）**完全一致**：
            //   WebView2 默认 UA 里含 `WebView2` 标识，央视页面会据此走不同分支
            //   （实测：默认 UA 时页面只发 2 个请求、播放器完全不启动）。
            .user_agent(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
                 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0",
            )
            // ★ 必须 visible(true)：WebView2 的 `SetIsVisible(false)` 会让 Chromium
            //   判定页面「不可见」→ 渲染挂起、requestAnimationFrame 停摆 →
            //   直播播放器的起播逻辑永不执行 → 永远抓不到 m3u8（实测：hidden 窗
            //   只发 14 个静态资源请求就停住，40s 无 m3u8）。
            //   用「显示但挪到屏幕外」代替隐藏：用户看不见，渲染却照常。
            .visible(true)
            // ★ 自动播放 + 后台节流三件套（缺一不可）：
            //   1) autoplay-policy=no-user-gesture-required —— WebView2 默认禁止
            //      有声自动播放，播放器因此不启动 → 不发 m3u8。
            //   2) disable-backgrounding-occluded-windows / disable-renderer-backgrounding
            //      —— 屏幕外窗口会被 Chromium 判为「被遮挡」，进而降频/挂起渲染。
            //   3) disable-background-timer-throttling —— 页面定时器不被降频。
            //   注：tauri 会覆盖 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS，必须用
            //   additional_browser_args 显式注入才生效。
            .additional_browser_args(
                // ★ 与 CDP 方案（official_web.rs 的 Edge 参数）**对齐**：
                //   --mute-audio / --disable-gpu / --no-sandbox 等。
                //   央视页面用 document.write 拉跨域脚本，缺了这些参数时
                //   Chromium 会因「解析阻塞型跨域脚本」策略把播放器脚本掐掉
                //   （实测：页面只发 2 个请求、播放器完全不启动、无任何报错）。
                "--mute-audio \
                 --disable-gpu \
                 --no-sandbox \
                 --disable-extensions \
                 --no-first-run \
                 --no-default-browser-check \
                 --autoplay-policy=no-user-gesture-required \
                 --disable-background-timer-throttling \
                 --disable-backgrounding-occluded-windows \
                 --disable-renderer-backgrounding \
                 --disable-features=msEdgeSidebarV2,msEdgeShoppingAssistant \
                 --remote-debugging-port=9339",
            );

            eprintln!("[probe] build() 开始");
            let win = builder.build()?;
            eprintln!("[probe] build() 完成");

            // ★ 探针必须先于导航挂上。
            let tx2 = tx.clone();
            win.with_webview(move |platform| {
                use windows::core::HSTRING;
                let webview: ICoreWebView2 = match unsafe { platform.controller().CoreWebView2() } {
                    Ok(w) => w,
                    Err(e) => {
                        eprintln!("[probe] CoreWebView2 失败: {e}");
                        return;
                    }
                };
                let filter = HSTRING::from("*");
                if let Err(e) = unsafe {
                    webview.AddWebResourceRequestedFilter(
                        &filter,
                        webview2_com::Microsoft::Web::WebView2::Win32::COREWEBVIEW2_WEB_RESOURCE_CONTEXT_ALL,
                    )
                } {
                    eprintln!("[probe] AddWebResourceRequestedFilter 失败: {e}");
                    return;
                }
                let handler = WebResourceRequestedEventHandler::create(Box::new(
                    move |_s: Option<ICoreWebView2>,
                          args: Option<ICoreWebView2WebResourceRequestedEventArgs>| {
                        let Some(args) = args else { return Ok(()) };
                        let request = match unsafe { args.Request() } {
                            Ok(r) => r,
                            Err(_) => return Ok(()),
                        };
                        let mut uri = windows::core::PWSTR::null();
                        if unsafe { request.Uri(&mut uri) }.is_err() {
                            return Ok(());
                        }
                        let uri_str = unsafe { uri.to_string().unwrap_or_default() };
                        // 打印前若干条请求，便于观察页面加载序列。
                        let lower = uri_str.to_ascii_lowercase();
                        let path = lower.split(['?', '#']).next().unwrap_or("");
                        let is_media =
                            path.ends_with(".m3u8") || path.ends_with(".flv") || path.ends_with(".ts");
                        if is_media {
                            let headers = read_headers(&request);
                            eprintln!("[probe][HIT] {}", uri_str);
                            for (k, v) in &headers {
                                eprintln!("[probe]   {}: {}", k, v);
                            }
                            let _ = tx2.send((uri_str, headers));
                        } else {
                            eprintln!("[probe][req] {}", uri_str);
                        }
                        Ok(())
                    },
                ));                let mut token: i64 = 0;
                if let Err(e) = unsafe { webview.add_WebResourceRequested(&handler, &mut token) } {
                    eprintln!("[probe] add_WebResourceRequested 失败: {e}");
                    return;
                }
                eprintln!("[probe] 旁听已挂载 token={token}");
                std::mem::forget(handler);
            })?;

            eprintln!("[probe] 导航 {url}");
            win.navigate(url.parse().unwrap())?;

            // ★ run() 会阻塞在事件循环里，所以命中等待必须另起线程。
            //   Receiver 不可克隆 → 整条接收端搬进线程（发送端在探针闭包里）。
            let rx2 = rx;
            std::thread::spawn(move || {
                let t0 = Instant::now();
                loop {
                    match rx2.recv_timeout(Duration::from_millis(500)) {
                        Ok((u, headers)) => {
                            eprintln!("[probe] ===== 命中 =====");
                            eprintln!("[probe] url={u}");
                            for (k, v) in &headers {
                                eprintln!("[probe] {k}: {v}");
                            }
                            eprintln!("[probe] 用时 {:.1}s", t0.elapsed().as_secs_f64());
                            std::process::exit(0);
                        }
                        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                            if t0.elapsed() > Duration::from_secs(40) {
                                eprintln!("[probe] 超时，未抓到媒体流");
                                std::process::exit(2);
                            }
                        }
                        Err(_) => std::process::exit(3),
                    }
                }
            });

            // ★ 诊断线程：用 CDP 连上**内嵌** WebView2，打印网络响应状态码与
            //   控制台错误，定位「播放器为何不发 m3u8 请求」。
            std::thread::spawn(move || {
                use std::io::{Read, Write};
                use std::net::TcpStream;
                let t0 = Instant::now();
                // 等调试端口起来
                let mut ws_url = String::new();
                while t0.elapsed() < Duration::from_secs(15) {
                    if let Ok(ts) = cdp_json_targets(9339) {
                        if let Some(t) = ts.iter().find(|t| {
                            t["type"] == "page"
                                && t["url"].as_str().map(|u| u.contains("cctv")).unwrap_or(false)
                        }) {
                            if let Some(u) = t["webSocketDebuggerUrl"].as_str() {
                                ws_url = u.to_string();
                                break;
                            }
                        }
                    }
                    std::thread::sleep(Duration::from_millis(300));
                }
                if ws_url.is_empty() {
                    eprintln!("[cdp] 没找到目标 target");
                    return;
                }
                eprintln!("[cdp] 连上 {ws_url}");
                let rest = ws_url.trim_start_matches("ws://");
                let (hostport, path) = match rest.find('/') {
                    Some(i) => (&rest[..i], &rest[i..]),
                    None => (rest, "/"),
                };
                let mut s = match TcpStream::connect(hostport) {
                    Ok(s) => s,
                    Err(e) => {
                        eprintln!("[cdp] 连接失败: {e}");
                        return;
                    }
                };
                s.set_read_timeout(Some(Duration::from_millis(500))).ok();
                let key = "dGhlIHNhbXBsZSBub25jZQ==";
                let req = format!("GET {path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n");
                if s.write_all(req.as_bytes()).is_err() {
                    eprintln!("[cdp] 握手失败");
                    return;
                }
                let mut hs = [0u8; 4096];
                let mut hl = 0usize;
                while hl < hs.len() {
                    match s.read(&mut hs[hl..]) {
                        Ok(0) => break,
                        Ok(n) => {
                            hl += n;
                            if hl >= 4 && &hs[hl - 4..hl] == b"\r\n\r\n" {
                                break;
                            }
                        }
                        Err(_) => break,
                    }
                }
                if !String::from_utf8_lossy(&hs[..hl]).starts_with("HTTP/1.1 101") {
                    eprintln!("[cdp] 握手失败: {}", String::from_utf8_lossy(&hs[..hl.min(120)]));
                    return;
                }
                let _ = cdp_send_ws(&mut s, r#"{"id":1,"method":"Network.enable","params":{}}"#);
                let _ = cdp_send_ws(&mut s, r#"{"id":2,"method":"Runtime.enable","params":{}}"#);
                let _ = cdp_send_ws(&mut s, r#"{"id":3,"method":"Log.enable","params":{}}"#);
                // ★ 必须 reload 一次：页面脚本可能在监听器就绪前就已执行，
                //   不 reload 就只能看到首屏的前几个请求（CDP 方案同样有这一步）。
                std::thread::sleep(Duration::from_millis(800));
                let _ = cdp_send_ws(&mut s, r#"{"id":4,"method":"Page.reload","params":{"ignoreCache":false}}"#);
                std::thread::sleep(Duration::from_millis(6000));
                // ★ 注入 JS 探针：看播放器 DOM/变量的实际状态。
                let probe_js = r#"JSON.stringify({
                  ua: navigator.userAgent.slice(0,60),
                  hidden: document.hidden,
                  vis: document.visibilityState,
                  vids: document.querySelectorAll('video').length,
                  vidSrc: (document.querySelector('video')||{}).currentSrc || (document.querySelector('video')||{}).src || '',
                  ready: document.readyState,
                  bodyLen: document.body ? document.body.innerHTML.length : 0,
                  keys: Object.keys(window).filter(k=>/player|video|hls|my|channel/i.test(k)).slice(0,25)
                })"#;
                let js_msg = serde_json::json!({
                    "id": 99,
                    "method": "Runtime.evaluate",
                    "params": {"expression": probe_js, "returnByValue": true}
                })
                .to_string();
                let _ = cdp_send_ws(&mut s, &js_msg);
                while t0.elapsed() < Duration::from_secs(40) {
                    match cdp_read_ws(&mut s) {
                        Some(text) => {
                            if let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) {
                                // JS 探针的返回（id=99）
                                if v["id"].as_i64() == Some(99) {
                                    let val = v["result"]["result"]["value"]
                                        .as_str()
                                        .unwrap_or("<none>");
                                    eprintln!("[cdp][JS] {val}");
                                    continue;
                                }
                                let m = v["method"].as_str().unwrap_or("");
                                if m == "Network.responseReceived" {
                                    let url = v["params"]["response"]["url"].as_str().unwrap_or("");
                                    let st = v["params"]["response"]["status"].as_i64().unwrap_or(0);
                                    let mime = v["params"]["response"]["mimeType"].as_str().unwrap_or("");
                                    // 只打印非静态资源，避免刷屏
                                    let low = url.to_ascii_lowercase();
                                    let path = low.split(['?', '#']).next().unwrap_or("");
                                    let static_res = path.ends_with(".css")
                                        || path.ends_with(".js")
                                        || path.ends_with(".png")
                                        || path.ends_with(".jpg")
                                        || path.ends_with(".jpeg")
                                        || path.ends_with(".gif")
                                        || path.ends_with(".ico")
                                        || path.ends_with(".svg")
                                        || path.ends_with(".woff")
                                        || path.ends_with(".woff2");
                                    if !static_res {
                                        eprintln!("[cdp][resp] {st} {mime} {url}");
                                    }
                                } else if m == "Log.entryAdded" {
                                    let lvl = v["params"]["entry"]["level"].as_str().unwrap_or("");
                                    let txt = v["params"]["entry"]["text"].as_str().unwrap_or("");
                                    eprintln!("[cdp][log:{lvl}] {txt}");
                                } else if m == "Runtime.consoleAPICalled" {
                                    let args = v["params"]["args"]
                                        .as_array()
                                        .map(|a| {
                                            a.iter()
                                                .map(|x| {
                                                    x["value"].as_str().unwrap_or("<obj>").to_string()
                                                })
                                                .collect::<Vec<_>>()
                                                .join(" ")
                                        })
                                        .unwrap_or_default();
                                    eprintln!("[cdp][console] {args}");
                                } else if m == "Runtime.exceptionThrown" {
                                    let txt = v["params"]["exceptionDetails"]["text"]
                                        .as_str()
                                        .unwrap_or("");
                                    eprintln!("[cdp][exception] {txt}");
                                }
                            }
                        }
                        None => {}
                    }
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("Tauri 启动失败");
}

/// CDP HTTP 端点：列 target。
fn cdp_json_targets(port: u16) -> Result<Vec<serde_json::Value>, String> {
    let addr = format!("127.0.0.1:{port}");
    let mut s = TcpStream::connect_timeout(
        &addr.parse::<std::net::SocketAddr>().map_err(|e| e.to_string())?,
        Duration::from_secs(2),
    )
    .map_err(|e| format!("连接失败: {e}"))?;
    s.set_read_timeout(Some(Duration::from_secs(3))).ok();
    let req = format!("GET /json HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n\r\n");
    s.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
    let mut buf = Vec::new();
    let mut tmp = [0u8; 8192];
    loop {
        let n = s.read(&mut tmp).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        buf.extend_from_slice(&tmp[..n]);
        if buf.windows(4).any(|w| w == b"\r\n\r\n") {
            break;
        }
        if buf.len() > 65536 {
            break;
        }
    }
    let body = String::from_utf8_lossy(&buf);
    let start = body.find("\r\n\r\n").map(|i| i + 4).unwrap_or(0);
    serde_json::from_str(body[start..].trim()).map_err(|e| format!("解析失败: {e}"))
}

/// CDP 发一条文本帧（客户端必须掩码）。
fn cdp_send_ws(s: &mut TcpStream, text: &str) -> Result<(), String> {
    let payload = text.as_bytes();
    let mut header = Vec::with_capacity(14);
    header.push(0x81u8);
    let mask = [0x12u8, 0x34, 0x56, 0x78];
    let len = payload.len();
    if len < 126 {
        header.push(0x80 | len as u8);
    } else if len <= 0xFFFF {
        header.push(0x80 | 126);
        header.extend_from_slice(&(len as u16).to_be_bytes());
    } else {
        header.push(0x80 | 127);
        header.extend_from_slice(&(len as u64).to_be_bytes());
    }
    header.extend_from_slice(&mask);
    let mut masked = Vec::with_capacity(len);
    for (i, b) in payload.iter().enumerate() {
        masked.push(b ^ mask[i % 4]);
    }
    s.write_all(&header).map_err(|e| e.to_string())?;
    s.write_all(&masked).map_err(|e| e.to_string())?;
    s.flush().map_err(|e| e.to_string())?;
    Ok(())
}

/// CDP 读一帧文本。
fn cdp_read_ws(s: &mut TcpStream) -> Option<String> {
    let mut h = [0u8; 2];
    if s.read_exact(&mut h).is_err() {
        return None;
    }
    let opcode = h[0] & 0x0F;
    let masked = h[1] & 0x80 != 0;
    let mut len = (h[1] & 0x7F) as u64;
    if len == 126 {
        let mut b = [0u8; 2];
        if s.read_exact(&mut b).is_err() {
            return None;
        }
        len = u16::from_be_bytes(b) as u64;
    } else if len == 127 {
        let mut b = [0u8; 8];
        if s.read_exact(&mut b).is_err() {
            return None;
        }
        len = u64::from_be_bytes(b) as u64;
    }
    let mut mask_key = [0u8; 4];
    if masked && s.read_exact(&mut mask_key).is_err() {
        return None;
    }
    let mut payload = vec![0u8; len as usize];
    if s.read_exact(&mut payload).is_err() {
        return None;
    }
    if masked {
        for (i, b) in payload.iter_mut().enumerate() {
            *b ^= mask_key[i % 4];
        }
    }
    match opcode {
        0x1 => Some(String::from_utf8_lossy(&payload).into_owned()),
        _ => None,
    }
}

fn read_headers(
    request: &webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2WebResourceRequest,
) -> Vec<(String, String)> {
    const KEEP: [&str; 4] = ["referer", "origin", "cookie", "user-agent"];
    let mut out = Vec::new();
    let headers = match unsafe { request.Headers() } {
        Ok(h) => h,
        Err(e) => {
            eprintln!("[probe] Headers() 失败: {e}");
            return out;
        }
    };
    let iter = match unsafe { headers.GetIterator() } {
        Ok(i) => i,
        Err(e) => {
            eprintln!("[probe] GetIterator() 失败: {e}");
            return out;
        }
    };
    loop {
        let mut has = windows::core::BOOL::default();
        if unsafe { iter.MoveNext(&mut has) }.is_err() || !has.as_bool() {
            break;
        }
        let mut name = windows::core::PWSTR::null();
        let mut value = windows::core::PWSTR::null();
        if unsafe { iter.GetCurrentHeader(&mut name, &mut value) }.is_err() {
            continue;
        }
        let n = unsafe { name.to_string().unwrap_or_default() };
        let v = unsafe { value.to_string().unwrap_or_default() };
        if KEEP.contains(&n.to_ascii_lowercase().as_str()) && !v.is_empty() {
            out.push((n.to_ascii_lowercase(), v));
        }
    }
    out
}
