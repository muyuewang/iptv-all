//! 探针：对比「reqwest 探活」与「curl 探活」在同一真流 URL 上的行为差异。
//!
//! 背景（2026-10-26）：ys/tv 回归 —— app 的真流层探活把**活着**的线路判成
//! `live=false`（实测 0.06~0.20s 内秒败，不是超时），而同一条 URL 用 curl
//! `Range: bytes=0-0` 返 HTTP 200。本探针把可能造成差异的每一个变量拆开单独试：
//!   ① 是否带 app 默认头（UA/cookie）
//!   ② 是否带 Range 头
//!   ③ 是否跟随重定向
//!   ④ 客户端是否复用连接池
//! 并打印**错误原文**（不只打印 live=false），以便定位是「连不上」还是「状态码非 2xx」。
//!
//! 用法：probe_live <url> [url...]

use clean_tv_core::net::headers;
use std::io::Read;
use std::time::{Duration, Instant};

/// 造一个「裸」客户端：无默认头、允许重定向。
fn bare(redirect: bool) -> reqwest::blocking::Client {
    let mut b = reqwest::blocking::Client::builder()
        .use_native_tls()
        .danger_accept_invalid_certs(true)
        .no_proxy();
    if !redirect {
        b = b.redirect(reqwest::redirect::Policy::none());
    }
    b.build().unwrap()
}

/// 造一个「app 同款」客户端：带默认头、不跟随重定向。
fn app_like() -> reqwest::blocking::Client {
    reqwest::blocking::Client::builder()
        .use_native_tls()
        .danger_accept_invalid_certs(true)
        .default_headers(headers())
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .unwrap()
}

/// 跑一次探测，打印状态码 / 错误原文 / 耗时。
fn probe(tag: &str, cli: &reqwest::blocking::Client, url: &str, range: bool, follow: bool) {
    let mut rb = cli.get(url).timeout(Duration::from_millis(2500));
    if range {
        rb = rb.header(reqwest::header::RANGE, "bytes=0-0");
    }
    let t0 = Instant::now();
    match rb.send() {
        Ok(mut r) => {
            let st = r.status();
            let loc = r
                .headers()
                .get(reqwest::header::LOCATION)
                .and_then(|v| v.to_str().ok())
                .map(|s| s.to_string())
                .unwrap_or_else(|| "-".into());
            let ct = r
                .headers()
                .get(reqwest::header::CONTENT_TYPE)
                .and_then(|v| v.to_str().ok())
                .map(|s| s.to_string())
                .unwrap_or_else(|| "-".into());
            let cl = r
                .headers()
                .get(reqwest::header::CONTENT_LENGTH)
                .and_then(|v| v.to_str().ok())
                .map(|s| s.to_string())
                .unwrap_or_else(|| "-".into());
            // ★ 只读 1 字节：证明正文真的能开始流动（FLV 流会一直挂着）。
            let mut first = [0u8; 1];
            let body = r
                .read(&mut first)
                .map(|n| format!("body{}=0x{:02x}", n, first[0]))
                .unwrap_or_else(|e| format!("bodyERR({})", crate::short_err(&e)));
            println!(
                "  [{tag}] status={} el={:.2}s ct={} cl={} loc={} {}",
                st.as_u16(),
                t0.elapsed().as_secs_f64(),
                ct,
                cl,
                loc,
                body
            );
        }
        Err(e) => println!(
            "  [{tag}] ERR el={:.2}s {}",
            t0.elapsed().as_secs_f64(),
            crate::short_err(&e)
        ),
    }
    let _ = follow;
}

/// 把 reqwest 的长错误链压成一行（reqwest::Error 的 sources 里有根因）。
pub fn short_err(e: &dyn std::error::Error) -> String {
    let mut s = e.to_string();
    let mut src = e.source();
    let mut depth = 0;
    while let Some(c) = src {
        if depth > 3 {
            break;
        }
        s.push_str(" <- ");
        s.push_str(&c.to_string());
        src = c.source();
        depth += 1;
    }
    s.replace('\n', " ")
}

fn main() {
    let urls: Vec<String> = std::env::args().skip(1).collect();
    if urls.is_empty() {
        eprintln!("用法: probe_live <url> [url...]");
        std::process::exit(2);
    }

    // ★ rustls 客户端（对照 native-tls/Schannel）。
    let rustls_cli = reqwest::blocking::Client::builder()
        .use_rustls_tls()
        .danger_accept_invalid_certs(true)
        .no_proxy()
        .build()
        .unwrap();

    let app = app_like();
    let bare_noredir = bare(false);
    let bare_follow = bare(true);

    for url in &urls {
        println!("=== {} ===", url);
        // ① app 同款（Schannel + 默认头 + Range）
        probe("sch+range   ", &app, url, true, false);
        // ② rustls 裸客户端 + Range —— ★ 决定性的对照
        probe("rustls+range", &rustls_cli, url, true, false);
        // ③ rustls 裸客户端 无 Range
        probe("rustls norng", &rustls_cli, url, false, false);
        // ④ Schannel 裸 + Range
        probe("bare+range  ", &bare_noredir, url, true, false);
        // ⑤ Schannel 裸 无 Range
        probe("bare norange", &bare_noredir, url, false, false);
        // ⑥ Schannel 跟随重定向
        probe("bare follow ", &bare_follow, url, false, true);
    }
}
