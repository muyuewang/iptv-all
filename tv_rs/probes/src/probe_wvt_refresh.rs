//! 探针：重新抓取本机源 full.dat，打印直连地址 + 逐条实测 HTTP 可播性。
//! 用法：cargo run --release --bin probe_wvt_refresh -- [最多测 N 条]
fn main() {
    let n: usize = std::env::args().nth(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let cli = clean_tv_core::net::blocking_client();
    match clean_tv_core::net::blocking_fetch_wvt_channels(&cli) {
        Ok(chans) => {
            eprintln!("解密频道总数 {}", chans.len());
            let mut direct: Vec<(String, String)> = Vec::new();
            for c in &chans {
                // 复现 prewarm_wvt 的选线：优先真 m3u8，回退 is_direct_stream
                let u = c.urls.iter()
                    .find(|u| clean_tv_core::wvt::is_m3u8_direct(&u.url))
                    .or_else(|| c.urls.iter().find(|u| clean_tv_core::wvt::is_direct_stream(&u.url)));
                if let Some(u) = u {
                    direct.push((c.name.clone(), u.url.clone()));
                }
            }
            eprintln!("直连频道数 {}", direct.len());
            let take = if n == 0 { direct.len() } else { n.min(direct.len()) };
            for (name, url) in direct.iter().take(take) {
                println!("{}\t{}", name, url);
            }
        }
        Err(e) => eprintln!("失败: {e}"),
    }
}