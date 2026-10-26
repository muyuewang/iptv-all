//! 789iptv 实时取流 smoke 验证（独立 bin，不经过 Tauri）。
//!
//! 用法：`cargo run --bin probe_789 -- <tid> <cid>`
//! 例：`cargo run --bin probe_789 -- tv 1` （抓 CCTV1 综合）
//! 抓成功后打印真实 flv URL，并用 mpv 实测能否识别解码（--no-video --length=...）。

use std::process::{Command, Stdio};
use std::time::Duration;

// 复刻 app 里的 iptv789 模块逻辑（这里直接内联最小实现，避免依赖 app crate 的 tauri 依赖）。
// 说明：iptv789.rs 目前放在 app crate 里（依赖 std 即可），但 app crate 拉 tauri 太重，
// 故 smoke 单独内联同款逻辑，验证「起 headless Edge + CDP 抓流」链路本身是否通。

mod mini789 {
    use std::io::{Read, Write};
    use std::net::TcpStream;
    use std::process::{Child, Command, Stdio};
    use std::time::{Duration, Instant};

    pub const HOME: &str = "https://789iptv.com";

    fn edge_path() -> Option<std::path::PathBuf> {
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

    fn cdp_json_targets(port: u16) -> Result<Vec<serde_json::Value>, String> {
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
        let mut tmp = [0u8; 8192];
        let header_end;
        loop {
            let n = s.read(&mut tmp).map_err(|e| e.to_string())?;
            if n == 0 {
                return Err("CDP /json 连接被关闭".into());
            }
            buf.extend_from_slice(&tmp[..n]);
            if let Some(pos) = (0..buf.len().saturating_sub(3))
                .find(|&i| &buf[i..i + 4] == b"\r\n\r\n")
            {
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

    pub fn fetch_stream(tid: &str, cid: &str, timeout: Duration) -> Result<String, String> {
        let edge = edge_path().ok_or("未找到 MS Edge")?;
        let port: u16 = 9300 + (std::process::id() % 500) as u16;
        let prof = std::env::temp_dir().join(format!("clean-tv-789-smoke-{}-{}", std::process::id(), port));
        let url = format!("{HOME}/?act=play&tid={tid}&id={cid}");

        let mut child: Child = Command::new(&edge)
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

        let ws_url: String = loop {
            if Instant::now() > deadline {
                let _ = child.kill();
                let _ = child.wait();
                return Err("等待 CDP target 超时".into());
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

        match ws_capture(&ws_url, &deadline) {
            Ok(Some(hit)) => {
                let _ = child.kill();
                let _ = child.wait();
                Ok(hit)
            }
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                Err("未抓取到真实流".into())
            }
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                Err(e)
            }
        }
    }

    fn ws_capture(ws_url: &str, deadline: &Instant) -> Result<Option<String>, String> {
        let rest = ws_url.trim_start_matches("ws://");
        let (hostport, path) = match rest.find('/') {
            Some(i) => (&rest[..i], &rest[i..]),
            None => (rest, "/"),
        };
        let mut s = TcpStream::connect(hostport).map_err(|e| format!("WS 连接失败: {e}"))?;
        s.set_read_timeout(Some(Duration::from_millis(500))).ok();
        s.set_write_timeout(Some(Duration::from_secs(2))).ok();
        let key = "dGhlIHNhbXBsZSBub25jZQ==";
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
        send_ws(&mut s, r#"{"id":1,"method":"Network.enable","params":{}}"#)?;
        send_ws(&mut s, r#"{"id":2,"method":"Page.enable","params":{}}"#)?;
        send_ws(&mut s, r#"{"id":3,"method":"Page.reload","params":{"ignoreCache":true}}"#)?;

        loop {
            if Instant::now() > *deadline {
                return Ok(None);
            }
            match read_ws_frame(&mut s) {
                Ok(Some(Ok(text))) => {
                    if let Some(hit) = parse_cdp_msg(&text) {
                        return Ok(Some(hit));
                    }
                }
                Ok(Some(Err(_))) => continue,
                Ok(None) => continue,
                Err(e) => return Err(e),
            }
        }
    }

    fn parse_cdp_msg(msg: &str) -> Option<String> {
        let v: serde_json::Value = serde_json::from_str(msg).ok()?;
        if v["method"] != "Network.responseReceived" {
            return None;
        }
        let url = v["params"]["response"]["url"].as_str()?;
        if url.contains("/live/") && url.contains(".flv") && url.contains("sign=") {
            return Some(url.to_string());
        }
        None
    }

    fn send_ws(s: &mut TcpStream, text: &str) -> Result<(), String> {
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

    fn read_ws_frame(s: &mut TcpStream) -> Result<Option<Result<String, Vec<u8>>>, String> {
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
            _ => Ok(Some(Err(payload))),
        }
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let (tid, cid) = match (args.get(1), args.get(2)) {
        (Some(t), Some(c)) => (t.clone(), c.clone()),
        _ => {
            eprintln!("用法: probe_789 <tid> <cid>  例: probe_789 tv 1");
            std::process::exit(2);
        }
    };
    eprintln!("[probe] 取流 {tid}:{cid} ...");
    match mini789::fetch_stream(&tid, &cid, Duration::from_secs(20)) {
        Ok(u) => {
            println!("REAL_STREAM: {u}");
            // 再用 mpv 实测（若传了第 3 个参数 mpv 路径）
            if let Some(mpv) = args.get(3) {
                eprintln!("[probe] mpv 实测识别 ...");
                let status = Command::new(mpv)
                    .args(["--no-video", "--tls-verify=no", "--length=6", &u])
                    .stdout(Stdio::null())
                    .stderr(Stdio::piped())
                    .spawn()
                    .and_then(|mut c: std::process::Child| Ok(c.wait().map(|s: std::process::ExitStatus| s.code())));
                eprintln!("[probe] mpv exit = {:?}", status);
            }
        }
        Err(e) => {
            eprintln!("[probe] 取流失败: {e}");
            std::process::exit(1);
        }
    }
}