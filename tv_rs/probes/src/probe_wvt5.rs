//! 探针：下载 data/5/full.dat 解密，打印直连线路，对比 data/4 是否已更新地址。
//! 用法：cargo run --probe_wvt5
fn main() {
    let cli = clean_tv_core::net::blocking_client();
    // 直接构造 data/5 的候选 URL（复用镜像回退思路）
    let raw5 = "https://raw.githubusercontent.com/hxh19950701/WebViewTvLive/main/data/5/full.dat";
    let mut urls = vec![raw5.to_string()];
    for cdn in ["https://fastly.jsdelivr.net", "https://cdn.jsdelivr.net"] {
        urls.push(format!("{cdn}/gh/hxh19950701/WebViewTvLive@main/data/5/full.dat"));
    }
    urls.push("https://raw.gitmirror.com/hxh19950701/WebViewTvLive/main/data/5/full.dat".to_string());
    urls.push("https://kkgithub.com/hxh19950701/WebViewTvLive/main/data/5/full.dat".to_string());

    let mut raw_bytes: Option<Vec<u8>> = None;
    for u in &urls {
        match cli.get(u).timeout(std::time::Duration::from_secs(15)).send() {
            Ok(r) if r.status().is_success() => {
                if let Ok(b) = r.bytes() {
                    if b.len() > 64 {
                        eprintln!("下载成功: {u} ({}) bytes", b.len());
                        raw_bytes = Some(b.to_vec());
                        break;
                    }
                }
            }
            Ok(r) => eprintln!("  HTTP {}: {}", r.status(), u),
            Err(e) => eprintln!("  ERR {}: {}", e, u),
        }
    }
    let bytes = match raw_bytes {
        Some(b) => b,
        None => { eprintln!("data/5 full.dat 全部下载失败"); return; }
    };
    match clean_tv_core::wvt::decrypt_dat_json(&bytes) {
        Some(v) => {
            use clean_tv_core::wvt::WvtChannel;
            if let Ok(chans) = serde_json::from_value::<Vec<WvtChannel>>(v) {
                eprintln!("data/5 解密频道总数 {}", chans.len());
                let mut direct = 0;
                for c in &chans {
                    let u = c.urls.iter()
                        .find(|u| clean_tv_core::wvt::is_m3u8_direct(&u.url))
                        .or_else(|| c.urls.iter().find(|u| clean_tv_core::wvt::is_direct_stream(&u.url)));
                    if let Some(u) = u {
                        direct += 1;
                        println!("{}\t{}", c.name, u.url);
                    }
                }
                eprintln!("data/5 直连 {} 条", direct);
            } else {
                eprintln!("JSON 结构不符");
            }
        }
        None => eprintln!("data/5 解密失败"),
    }
}