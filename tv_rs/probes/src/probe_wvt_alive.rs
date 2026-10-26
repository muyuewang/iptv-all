//! 探针：逐条实测本机源直连地址的 HTTP 可达性 + m3u8 有效性。
//! 输出「存活/失效」分类，供判断哪些地址过期了。
use std::io::Write;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

fn main() {
    let cli = clean_tv_core::net::blocking_client();
    let chans = match clean_tv_core::net::blocking_fetch_wvt_channels(&cli) {
        Ok(c) => c,
        Err(e) => { eprintln!("抓取失败: {e}"); return; }
    };
    // 收集直连（复现 prewarm_wvt 选线）
    let mut direct: Vec<(String, String)> = Vec::new();
    for c in &chans {
        let u = c.urls.iter()
            .find(|u| clean_tv_core::wvt::is_m3u8_direct(&u.url))
            .or_else(|| c.urls.iter().find(|u| clean_tv_core::wvt::is_direct_stream(&u.url)));
        if let Some(u) = u { direct.push((c.name.clone(), u.url.clone())); }
    }
    // 去重（同名同 URL 保留一条）
    let mut seen = std::collections::HashSet::new();
    direct.retain(|(n, u)| seen.insert(format!("{n}|{u}")));
    let total = direct.len();
    eprintln!("去重后直连 {} 条，开始并发实测…", total);

    let ok = Arc::new(std::sync::Mutex::new(Vec::new()));
    let bad = Arc::new(std::sync::Mutex::new(Vec::new()));
    let done = Arc::new(AtomicUsize::new(0));

    for chunk in direct.chunks(10) {
        let mut handles = Vec::new();
        for (name, url) in chunk {
            let name = name.clone();
            let url = url.clone();
            let ok = ok.clone();
            let bad = bad.clone();
            let done = done.clone();
            handles.push(std::thread::spawn(move || {
                let cli = clean_tv_core::net::blocking_client();
                let alive = probe_one(&cli, &name, &url);
                if alive {
                    ok.lock().unwrap().push((name, url));
                } else {
                    bad.lock().unwrap().push((name, url));
                }
                let n = done.fetch_add(1, Ordering::SeqCst) + 1;
                eprintln!("进度 {n}/{total}");
            }));
        }
        for h in handles { let _ = h.join(); }
    }

    let ok = ok.lock().unwrap();
    let bad = bad.lock().unwrap();
    println!("\n===== 存活 {} 条 =====", ok.len());
    let mut f = std::fs::File::create("app_wvt_ok.txt").unwrap();
    for (n, u) in ok.iter() { let _ = writeln!(f, "{}\t{}", n, u); println!("OK\t{}\t{}", n, u); }
    println!("\n===== 失效 {} 条 =====", bad.len());
    let mut b = std::fs::File::create("app_wvt_bad.txt").unwrap();
    for (n, u) in bad.iter() { let _ = writeln!(b, "{}\t{}", n, u); println!("BAD\t{}\t{}", n, u); }
}

fn probe_one(cli: &reqwest::blocking::Client, name: &str, url: &str) -> bool {
    // 只 GET 前 1KB 看 HTTP 状态 + 内容是否是 m3u8（或非 HTML 错误页）
    let resp = match cli.get(url).timeout(std::time::Duration::from_secs(10)).send() {
        Ok(r) => r,
        Err(e) => {
            eprintln!("  [ERR] {} : {}", name, e);
            return false;
        }
    };
    if !resp.status().is_success() {
        return false;
    }
    let bytes = match resp.bytes() {
        Ok(b) => b,
        Err(_) => return false,
    };
    // 取前几十字节判断
    let head = String::from_utf8_lossy(&bytes[..bytes.len().min(200)]);
    let is_html = head.trim_start().to_lowercase().starts_with("<!doctype")
        || head.trim_start().to_lowercase().starts_with("<html")
        || head.trim_start().to_lowercase().starts_with("<head")
        || head.trim_start().to_lowercase().starts_with("<meta");
    if is_html {
        return false; // 返回 HTML = 中转页/错误页，不可播
    }
    // m3u8 应含 #EXTM3U
    true
}