//! 探针：用 reqwest（与 app 完全相同的客户端构造）打印 eplay2.php 的 Location。
//! 用途：排查「curl 能拿到带 sign 的 Location，app 却拿不到」——
//! 怀疑 reqwest 的某个默认行为（自动加头 / HTTP 版本 / Accept-Encoding）导致
//! 源站走了不同的分支。

fn main() {
    let url = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "https://api1.2026016.xyz/eplay2.php?token=AF1V76S9JQVhZ97TDEHRF%2BYV&tid=ys&id=1&p=0&type=.flv".into());
    let cli = reqwest::blocking::Client::builder()
        .use_native_tls()
        .danger_accept_invalid_certs(true)
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .unwrap();
    for i in 0..4 {
        match cli.get(&url).timeout(std::time::Duration::from_secs(12)).send() {
            Ok(r) => {
                let st = r.status();
                let loc = r
                    .headers()
                    .get(reqwest::header::LOCATION)
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or("(无 Location)");
                println!("#{i} status={st} loc={loc}");
                println!("   全部响应头: {:#?}", r.headers());
            }
            Err(e) => println!("#{i} ERR {e}"),
        }
        std::thread::sleep(std::time::Duration::from_millis(300));
    }
}
