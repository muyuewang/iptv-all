//! 探针：下载 data/5 的 server_config.dat / webpage_adapter.dat，
//! 尝试用旧 key 解密，看明文结构（可能藏着新的解密 seed / 数据源入口）。
fn main() {
    let cli = clean_tv_core::net::blocking_client();
    for file in ["server_config.dat", "webpage_adapter.dat"] {
        let raw = format!("https://fastly.jsdelivr.net/gh/hxh19950701/WebViewTvLive@main/data/5/{file}");
        eprintln!("===== 下载 {file} =====");
        let bytes = match cli.get(&raw).timeout(std::time::Duration::from_secs(15)).send() {
            Ok(r) if r.status().is_success() => r.bytes().map(|b| b.to_vec()).unwrap_or_default(),
            Ok(r) => { eprintln!("HTTP {}", r.status()); continue; }
            Err(e) => { eprintln!("ERR {}", e); continue; }
        };
        if bytes.len() <= 64 {
            eprintln!("响应过小 {} bytes，可能是明文", bytes.len());
            let txt = String::from_utf8_lossy(&bytes);
            eprintln!("明文前 500 字：\n{}", &txt[..txt.len().min(500)]);
            continue;
        }
        eprintln!("下载 {} bytes", bytes.len());
        // 尝试旧 key 解密
        match clean_tv_core::wvt::decrypt_dat(&bytes) {
            Some(txt) => {
                let head = &txt[..txt.len().min(800)];
                eprintln!("解密成功，明文前 800 字：\n{}", head);
            }
            None => eprintln!("旧 key 解密失败（加密种子已变）"),
        }
    }
}