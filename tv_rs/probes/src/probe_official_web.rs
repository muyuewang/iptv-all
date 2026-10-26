// 官网源网页嗅探探针：验证 Rust 版 CDP 抓流 + 请求头。
//
// 用法：cargo run -p clean-tv-app --bin probe_official_web -- <页面URL>

use std::time::Duration;

#[path = "../../app/src/cdp.rs"]
mod cdp;
#[path = "../../app/src/official_web.rs"]
mod official_web;

fn main() {
    let url = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "https://tv.cctv.com/live/cctv1/".to_string());
    eprintln!("[probe] 开始嗅探 {url}");
    let t0 = std::time::Instant::now();
    match official_web::sniff_stream(&url, Duration::from_secs(40)) {
        Ok(s) => {
            eprintln!("[probe] ✔ 用时 {:.1}s", t0.elapsed().as_secs_f64());
            eprintln!("[probe] url = {}", s.url);
            for (k, v) in &s.headers {
                eprintln!("[probe] hdr {k}: {v}");
            }
        }
        Err(e) => {
            eprintln!("[probe] ✘ 用时 {:.1}s  失败: {e}", t0.elapsed().as_secs_f64());
            std::process::exit(1);
        }
    }
}
