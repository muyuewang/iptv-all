use clean_tv_core::net::{self, ApiState};
fn main() {
    let cli = net::blocking_client();
    let state = ApiState::new();
    for round in 1..=2 {
        match net::blocking_fetch_play(&state, "ys", "1", &cli) {
            Ok((urls, _, msg)) => println!("[round {round}] msg={msg} 第一条={}", urls.first().cloned().unwrap_or_default()),
            Err(e) => println!("[round {round}] 失败: {e}"),
        }
    }
}
