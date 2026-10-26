//! CDP（Chrome DevTools Protocol）最小客户端 —— Edge headless 旁听媒体流。
//!
//! 两处共用：
//!   * `iptv789.rs` —— 789iptv 播放页（页面自己解密混淆 JS，CDP 抓真实 flv）；
//!   * `official_web.rs` —— 官网源网页直播（央视 tv.cctv.com / 央视频等），
//!     CDP 抓 HLS 主索引 + 收集 Referer/UA/Origin 等必需请求头。
//!
//! 实现要点（都是踩过的坑）：
//!   * Edge 的 `/json` 响应未必主动关连接 → 按 Content-Length 读，别用 read_to_end。
//!   * headless 首启会多出 edge://sync-confirmation-dialog 等 page target，
//!     **不能取第一个 type=page**，必须按 URL 特征挑目标页。
//!   * WebSocket 客户端帧**必须掩码**；服务端帧不掩码。ping 直接忽略。

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::{Duration, Instant};

/// 定位 headless Edge 可执行文件。
pub fn edge_path() -> Option<std::path::PathBuf> {
    for p in [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ] {
        let pb = std::path::PathBuf::from(p);
        if pb.is_file() {
            return Some(pb);
        }
    }
    None
}

/// 通过 CDP HTTP 端点查 `/json` 列出的 target 里找 page 类型 target。
pub fn cdp_json_targets(port: u16) -> Result<Vec<serde_json::Value>, String> {
    let addr = format!("127.0.0.1:{port}");
    let mut s = TcpStream::connect_timeout(
        &addr.parse::<std::net::SocketAddr>().map_err(|e| e.to_string())?,
        Duration::from_secs(2),
    )
    .map_err(|e| format!("连接 CDP 失败: {e}"))?;
    s.set_read_timeout(Some(Duration::from_secs(3))).ok();
    let req = format!("GET /json HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n\r\n");
    s.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
    let mut buf = Vec::new();
    // ★ 不依赖 read_to_end（Edge 的 /json 响应未必主动关连接，会用 Content-Length），
    //   改为读满 Content-Length 个字节。
    let mut tmp = [0u8; 8192];
    let header_end;
    loop {
        let n = s.read(&mut tmp).map_err(|e| e.to_string())?;
        if n == 0 {
            return Err("CDP /json 连接被关闭".into());
        }
        buf.extend_from_slice(&tmp[..n]);
        if let Some(pos) = find_subsequence(&buf, b"\r\n\r\n") {
            header_end = pos + 4;
            break;
        }
        if buf.len() > 64 * 1024 {
            return Err("CDP /json 响应头过长".into());
        }
    }
    let header = String::from_utf8_lossy(&buf[..header_end]).to_string();
    let content_len = header
        .lines()
        .find_map(|l| {
            let l = l.trim();
            l.to_ascii_lowercase()
                .strip_prefix("content-length:")
                .map(|v| v.trim().parse::<usize>().ok())
                .flatten()
        })
        .unwrap_or(0);
    let mut body = buf[header_end..].to_vec();
    while body.len() < content_len {
        let n = s.read(&mut tmp).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        body.extend_from_slice(&tmp[..n]);
    }
    let body_str = String::from_utf8_lossy(&body);
    serde_json::from_str(body_str.trim()).map_err(|e| format!("解析 /json 失败: {e}"))
}

/// 在字节切片中查找子序列，返回起始偏移。
pub fn find_subsequence(hay: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || hay.len() < needle.len() {
        return None;
    }
    (0..=hay.len() - needle.len()).find(|&i| &hay[i..i + needle.len()] == needle)
}


/// 用纯标准库实现的最小 WebSocket 客户端，连 CDP 收媒体流事件。
///
/// 只做 CDP 需要的最小集：`Network.enable` 命令 + 解析 `Network.responseReceived`
/// 事件。CDP 的 WS 不压缩、无掩码到服务器（客户端帧必须掩码），文本帧收发。
/// 连上 CDP 的 WS，启用 Network 监听并触发一次 reload。
///
/// 返回 `Ok(true)` = 回调报告命中；`Ok(false)` = 超时未命中。
/// 媒体流判据交给调用方（`official_web` 要 m3u8/flv，`iptv789` 要 sign= 的 flv），
/// 故这里把「所有 responseReceived 的 url」都回传，由调用方筛。
pub fn ws_capture_any(
    ws_url: &str,
    deadline: &Instant,
    mut on_response: impl FnMut(&str) -> bool,
) -> Result<bool, String> {
    let rest = ws_url.trim_start_matches("ws://");
    let (hostport, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    let mut s = TcpStream::connect(hostport).map_err(|e| format!("WS 连接失败: {e}"))?;
    s.set_read_timeout(Some(Duration::from_millis(500))).ok();
    s.set_write_timeout(Some(Duration::from_secs(2))).ok();

    // WebSocket 握手
    let key = "dGhlIHNhbXBsZSBub25jZQ=="; // 固定 Sec-WebSocket-Key（握手回验用）
    let req = format!(
        "GET {path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    );
    s.write_all(req.as_bytes()).map_err(|e| format!("WS 握手发送失败: {e}"))?;
    let mut hs = [0u8; 4096];
    let mut hs_len = 0usize;
    while hs_len < hs.len() {
        let n = match s.read(&mut hs[hs_len..]) {
            Ok(0) => return Err("WS 握手被关闭".into()),
            Ok(n) => n,
            Err(_) => break,
        };
        hs_len += n;
        if hs_len >= 4 && &hs[hs_len - 4..hs_len] == b"\r\n\r\n" {
            break;
        }
    }
    if !String::from_utf8_lossy(&hs[..hs_len]).starts_with("HTTP/1.1 101") {
        return Err(format!("WS 握手失败: {}", String::from_utf8_lossy(&hs[..hs_len.min(120)])));
    }

    // Network.enable 是关键：不 enable 就收不到 responseReceived。
    send_ws(&mut s, r#"{"id":1,"method":"Network.enable","params":{}}"#)?;
    send_ws(&mut s, r#"{"id":2,"method":"Page.enable","params":{}}"#)?;
    // reload 一次，确保页面脚本执行并发出媒体请求。
    send_ws(&mut s, r#"{"id":3,"method":"Page.reload","params":{"ignoreCache":true}}"#)?;

    let mut partial = String::new();
    loop {
        if Instant::now() > *deadline {
            return Ok(false);
        }
        match read_ws_frame(&mut s) {
            Ok(Some(Ok(text))) => {
                partial.push_str(&text);
                if partial.is_empty() {
                    continue;
                }
                let msg = std::mem::take(&mut partial);
                if let Some(url) = response_url_of(&msg) {
                    // ★ 回调返回 true = 命中（由调用方决定），否则继续收下一条。
                    //   不能「见到第一个 responseReceived 就返回」——页面先加载 CSS/JS/
                    //   图片，媒体请求排在后面。
                    if on_response(&url) {
                        return Ok(true);
                    }
                }
            }
            Ok(Some(Err(_))) => continue,
            Ok(None) => continue, // timeout
            Err(e) => return Err(e),
        }
    }
}

/// 从一条 CDP 消息里取 `Network.responseReceived` 的 URL；不是则 None。
pub fn response_url_of(msg: &str) -> Option<String> {
    let v: serde_json::Value = serde_json::from_str(msg).ok()?;
    if v["method"] != "Network.responseReceived" {
        return None;
    }
    v["params"]["response"]["url"].as_str().map(|s| s.to_string())
}

// ===== 最小 WebSocket 帧收发 =====

/// 发送一条文本帧（客户端必须掩码）。
pub fn send_ws(s: &mut TcpStream, text: &str) -> Result<(), String> {
    let payload = text.as_bytes();
    let mut header = Vec::with_capacity(14);
    header.push(0x81u8); // FIN + text opcode
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

/// 读一帧。返回 `Ok(Some(Ok(text)))` 文本帧、`Ok(Some(Err(binary)))`、`Ok(None)` 超时。
pub fn read_ws_frame(s: &mut TcpStream) -> Result<Option<Result<String, Vec<u8>>>, String> {
    // 读 2 字节头
    let mut h = [0u8; 2];
    loop {
        match s.read_exact(&mut h) {
            Ok(_) => break,
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock || e.kind() == std::io::ErrorKind::TimedOut => return Ok(None),
            Err(e) => return Err(format!("WS 读头失败: {e}")),
        }
    }
    let opcode = h[0] & 0x0F;
    let masked = h[1] & 0x80 != 0;
    let mut len = (h[1] & 0x7F) as u64;
    if len == 126 {
        let mut b = [0u8; 2];
        s.read_exact(&mut b).map_err(|e| e.to_string())?;
        len = u16::from_be_bytes(b) as u64;
    } else if len == 127 {
        let mut b = [0u8; 8];
        s.read_exact(&mut b).map_err(|e| e.to_string())?;
        len = u64::from_be_bytes(b);
    }
    let mut mask_key = [0u8; 4];
    if masked {
        s.read_exact(&mut mask_key).map_err(|e| e.to_string())?;
    }
    let mut payload = vec![0u8; len as usize];
    s.read_exact(&mut payload).map_err(|e| e.to_string())?;
    if masked {
        for (i, b) in payload.iter_mut().enumerate() {
            *b ^= mask_key[i % 4];
        }
    }
    match opcode {
        0x1 => Ok(Some(Ok(String::from_utf8_lossy(&payload).into_owned()))),
        0x8 => Err("WS 被服务端关闭".into()),
        0x9 => {
            // ping -> pong（简化：忽略）
            Ok(None)
        }
        _ => Ok(Some(Err(payload))),
    }
}