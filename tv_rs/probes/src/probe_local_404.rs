// 验证：短版 UA 下 fetch_play 能否正常解密出线路。
// 用法：cargo run -p clean-tv-probes --bin probe_local_404
use clean_tv_core::net;
use reqwest::header::{HeaderMap, HeaderName, USER_AGENT};

const LONG_UA: &str = "Mozilla/5.0 (Linux; Android 9; SM-N9700 Build/PQ3B.190801.01311438; wv) \
AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 \
Mobile Safari/537.36 diashizhb LT-APP/48/517/YM-RT/";
const SHORT_UA: &str = "LT-APP/48/517";

fn client_with(ua: &str) -> reqwest::blocking::Client {
    let mut h = HeaderMap::new();
    h.insert(USER_AGENT, ua.parse().unwrap());
    h.insert(
        HeaderName::from_static("x-requested-with"),
        "com.sldv.okstuffx".parse().unwrap(),
    );
    h.insert(
        reqwest::header::COOKIE,
        "adReward=1; iptvad=1; appVer=517".parse().unwrap(),
    );
    h.insert(reqwest::header::ACCEPT, "*/*".parse().unwrap());
    h.insert(
        reqwest::header::ACCEPT_LANGUAGE,
        "zh-CN,zh;q=0.9".parse().unwrap(),
    );
    reqwest::blocking::Client::builder()
        .use_native_tls()
        .danger_accept_invalid_certs(true)
        .default_headers(h)
        .no_proxy()
        .build()
        .expect("client")
}

fn main() {
    let state = net::ApiState::new();
    for (tag, ua) in [("长版UA", LONG_UA), ("短版UA", SHORT_UA)] {
        let cli = client_with(ua);
        println!("=== {} ===", tag);
        for tid in ["tv", "ys", "ws", "ty", "gt", "wintv123"] {
            // 直接看播放页标题，确认返回的是不是**该频道的**页面（而非通用页）
            let url = format!("https://api1.2026016.xyz/iptve.php?act=play&tid={tid}&id=1");
            let title = match cli.get(&url).send() {
                Ok(r) if r.status().is_success() => {
                    let b = r.text().unwrap_or_default();
                    let t = b.split("<title>").nth(1).unwrap_or("?").split("</title>").next().unwrap_or("?");
                    format!("{} [{}]", t, b.len())
                }
                Ok(r) => format!("HTTP {}", r.status()),
                Err(e) => format!("ERR {e}"),
            };
            // 再跑一遍真正的 fetch_play（走项目解密逻辑）
            let res = net::blocking_fetch_play(&state, tid, "1", &cli);
            let fp = match res {
                Ok((urls, epg, msg)) => format!(
                    "msg={msg} urls={} direct={} epg={} 首条={}",
                    urls.len(),
                    urls.iter().filter(|u| clean_tv_core::wvt::is_direct_stream(u)).count(),
                    epg.len(),
                    urls.first().map(|u| u[..u.len().min(95)].to_string()).unwrap_or_default()
                ),
                Err(e) => format!("ERR {e}"),
            };
            println!("  {tid:<9} {title:<40} {fp}");
        }
        println!();
    }
}
