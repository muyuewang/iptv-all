//! 官网源链路探针：api2 频道表 + 播放页解密出直连 URL。
use clean_tv_core::net::{self, ApiState};

fn main() {
    let cli = net::blocking_client();
    let state = ApiState::new();
    // 频道表
    match net::blocking_fetch_channels(&state, "ys", &cli) {
        Ok(chans) => {
            println!("[1] ys 频道表 {} 条，生效镜像 {}", chans.len(), state.current_api());
            for c in chans.iter().take(5) { println!("    {} {}", c.id, c.name); }
            // 播放页解密
            if let Some(c) = chans.first() {
                match net::blocking_fetch_play(&state, "ys", &c.id, &cli) {
                    Ok((urls, epg, msg)) => {
                        println!("[2] play ys:{} urls={} epg={} msg={}", c.id, urls.len(), epg.len(), msg);
                        for u in urls.iter().take(3) {
                            println!("    direct={} {}", clean_tv_core::wvt::is_direct_stream(u), u);
                        }
                    }
                    Err(e) => println!("[2] play 失败: {e}"),
                }
            }
        }
        Err(e) => println!("[1] ys 频道表失败: {e}"),
    }
}
