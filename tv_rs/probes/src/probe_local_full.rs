//! 全量本机源实测探针：对每个分类的每个频道，
//! ① 用 core.rs 的 `decrypt_page` 解出线路；
//! ② 对每条线路做「真实流可达性」探测（只取首字节，不消耗源站配额）。
//!
//! 用法：
//!   cargo run --release -p clean-tv-probes --bin probe_local_full -- [分类...]
//!   cargo run --release -p clean-tv-probes --bin probe_local_full -- tv ys
//!
//! 输出：每个分类的「可解密数 / 线路可达数」，以及每条线路的首字节特征
//! （FLV = 46 4C 56，TS = 47，HLS = #EXTM3U，HTTP 状态码）。

use clean_tv_core::core::{decrypt_page, parse_channels};
use clean_tv_core::net::blocking_client_no_redirect;
use std::io::Read;
use std::time::Duration;

/// 从线路 URL 里抠出 `/v/xxx.php?token=..&id=<台标缩写>` 的 id。
fn station_id(url: &str) -> Option<String> {
    let q = url.split('?').nth(1)?;
    for kv in q.split('&') {
        if let Some(v) = kv.strip_prefix("id=") {
            return Some(v.to_string());
        }
    }
    None
}

/// 从线路 URL 里抠出 token。
fn token_of(url: &str) -> Option<String> {
    let q = url.split('?').nth(1)?;
    for kv in q.split('&') {
        if let Some(v) = kv.strip_prefix("token=") {
            return Some(v.to_string());
        }
    }
    None
}

const CATS: [&str; 6] = ["tv", "ty", "ys", "ws", "wintv123", "gt"];

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let cats: Vec<&str> = if args.is_empty() {
        CATS.to_vec()
    } else {
        args.iter().map(|s| s.as_str()).collect()
    };

    let cli = blocking_client_no_redirect();
    let mut grand_ok = 0usize;
    let mut grand_tot = 0usize;
    // 频道名 → 台标缩写（用于给 404 分类反查线路）
    let mut id_map: std::collections::BTreeMap<String, String> = std::collections::BTreeMap::new();
    // 收集到的 token（/v/ 中转接口的 token 与频道无关，可复用）
    let mut tokens: Vec<String> = Vec::new();
    // 输出 TSV：频道名<TAB>台标缩写
    let mut id_rows: Vec<String> = Vec::new();

    for tid in cats {
        println!("\n========== 分类 {} ==========", tid);
        // 1) 频道列表
        let list_url = format!("https://api1.2026016.xyz/iptve.php?tid={tid}&app=517");
        let html = match cli
            .get(&list_url)
            .timeout(Duration::from_secs(15))
            .send()
        {
            Ok(r) => r.text().unwrap_or_default(),
            Err(e) => {
                println!("  列表失败: {e}");
                continue;
            }
        };
        let chans = parse_channels(&html, tid);
        println!("  频道数: {}", chans.len());

        let mut ok_dec = 0usize;
        let mut ok_stream = 0usize;
        let mut errs: Vec<(String, String, String)> = Vec::new();

        for c in &chans {
            let play_url = format!(
                "https://api1.2026016.xyz/iptve.php?act=play&tid={tid}&id={}",
                c.id
            );
            let resp = match cli
                .get(&play_url)
                .timeout(Duration::from_secs(15))
                .send()
            {
                Ok(r) => r,
                Err(e) => {
                    errs.push((c.id.clone(), c.name.clone(), format!("请求失败 {e}")));
                    continue;
                }
            };
            let st = resp.status();
            if !st.is_success() {
                errs.push((c.id.clone(), c.name.clone(), format!("HTTP {st}")));
                continue;
            }
            let body = resp.text().unwrap_or_default();
            if !body.contains("<option") {
                errs.push((c.id.clone(), c.name.clone(), "无线路数据".into()));
                continue;
            }
            // 2) 解密（用当前生效镜像做同源 url0）
            let res = decrypt_page(&body, tid, &c.id, "https://api1.2026016.xyz");
            if res.urls.is_empty() {
                errs.push((c.id.clone(), c.name.clone(), format!("解密失败: {}", res.msg)));
                continue;
            }
            ok_dec += 1;

            // 记录「频道名 → 台标缩写」与 token（供 404 分类反查）
            for u in &res.urls {
                if let Some(sid) = station_id(u) {
                    id_map.entry(c.name.clone()).or_insert(sid.clone());
                    id_rows.push(format!("{}\t{}", c.name, sid));
                }
                if let Some(tk) = token_of(u) {
                    if !tokens.contains(&tk) {
                        tokens.push(tk);
                    }
                }
            }

            // 3) 逐条线路探活（只读首字节）
            let mut alive = 0usize;
            let mut detail = String::new();
            for u in &res.urls {
                match probe(&cli, u) {
                    Ok(desc) => {
                        alive += 1;
                        detail.push_str(&format!("\n      ✓ {desc}"));
                        if alive >= 3 {
                            break;
                        }
                    }
                    Err(e) => {
                        if detail.len() < 200 {
                            detail.push_str(&format!("\n      ✗ {e}"));
                        }
                    }
                }
            }
            if alive > 0 {
                ok_stream += 1;
                println!(
                    "  ✓ {:>4} {:<18} 线路 {}{}",
                    c.id,
                    c.name,
                    res.urls.len(),
                    detail
                );
            } else {
                println!(
                    "  · {:>4} {:<18} 线路 {} 但全不可达{}",
                    c.id,
                    c.name,
                    res.urls.len(),
                    detail
                );
            }
        }
        grand_ok += ok_stream;
        grand_tot += chans.len();
        println!(
            "  ---- 分类 {} 汇总: 频道 {} / 可解密 {} / 有活流 {}",
            tid,
            chans.len(),
            ok_dec,
            ok_stream
        );
        if !errs.is_empty() {
            println!("  失败明细(前 10):");
            for (id, name, why) in errs.iter().take(10) {
                println!("    - {id} {name}: {why}");
            }
        }
    }
    println!("\n========== 总计: 有活流 {grand_ok} / {grand_tot} ==========");

    // ---- 导出「频道名 → 台标缩写」映射，供 404 分类反查 ----
    if let Ok(p) = std::env::var("CLEAN_TV_IDMAP_OUT") {
        let mut s = String::new();
        for (name, sid) in &id_map {
            s.push_str(&format!("{name}\t{sid}\n"));
        }
        let _ = std::fs::write(&p, s);
        println!("→ 台标缩写映射已写入 {p}（{} 条）", id_map.len());
    }
    if let Ok(p) = std::env::var("CLEAN_TV_TOKEN_OUT") {
        let _ = std::fs::write(&p, tokens.join("\n"));
        println!("→ token 已写入 {p}（{} 个）", tokens.len());
    }

    // ---- 反查：对没有线路的分类，用「同名单频道」+ 已收集 token 直接打 /v/ 中转 ----
    //   原理：/v/cqyx.php?token=T&id=<台标缩写> 的 **token 与频道无关**，
    //   且台标缩写来自线路本身 → 同一个台在 tv 分类和 ws 分类共用同一个缩写。
    //   于是 404 分类的频道可以借「别的分类里同名频道的缩写 + 任一 token」拿到流。
    if std::env::var("CLEAN_TV_CROSS").is_ok() {
        println!("\n========== 跨分类反查（404 分类用同名缩写 + 共享 token）==========");
        for tid in CATS {
            // 该分类频道列表
            let list_url = format!("https://api1.2026016.xyz/iptve.php?tid={tid}&app=517");
            let html = match cli
                .get(&list_url)
                .timeout(Duration::from_secs(15))
                .send()
            {
                Ok(r) => r.text().unwrap_or_default(),
                Err(_) => continue,
            };
            let chans = parse_channels(&html, tid);
            let mut hit = 0usize;
            for c in &chans {
                // 找同名频道的台标缩写
                let sid = match id_map.get(&c.name) {
                    Some(s) => s.clone(),
                    None => continue,
                };
                // 用收集到的 token 打 /v/ 中转
                let mut ok = false;
                for tk in tokens.iter().take(6) {
                    for path in ["cqyx", "ysp6", "migu8"] {
                        let u = format!(
                            "https://api1.2026016.xyz/v/{path}.php?token={tk}&id={sid}"
                        );
                        if let Ok(desc) = probe(&cli, &u) {
                            if desc.contains("302") {
                                println!(
                                    "  ✓ {:>4} {:<18} sid={:<10} {}",
                                    c.id, c.name, sid, desc
                                );
                                ok = true;
                                hit += 1;
                                break;
                            }
                        }
                    }
                    if ok {
                        break;
                    }
                }
            }
            println!("  ---- 分类 {} 跨分类反查命中 {}/{}", tid, hit, chans.len());
        }
    }
}

/// 探测一条线路：跟到 302 拿 Location，或读首字节判断流格式。
fn probe(cli: &reqwest::blocking::Client, url: &str) -> Result<String, String> {
    let resp = cli
        .get(url)
        .timeout(Duration::from_secs(8))
        .send()
        .map_err(|e| format!("连接失败: {}", first_line(&e.to_string())))?;
    let st = resp.status();
    // 302 → Location 是真流
    if st.is_redirection() {
        let loc = resp
            .headers()
            .get(reqwest::header::LOCATION)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("");
        return Ok(format!("HTTP {st} → {}", truncate(loc, 110)));
    }
    if !st.is_success() {
        return Err(format!("HTTP {st}"));
    }
    // 直接给流：读首字节
    let ct = resp
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    let mut resp = resp;
    let mut buf = [0u8; 16];
    let n = resp
        .read(&mut buf)
        .map_err(|e| format!("读失败 {e}"))?;
    let head = &buf[..n.min(16)];
    let magic = if head.starts_with(b"FLV") {
        "FLV"
    } else if head.starts_with(&[0x47]) {
        "TS"
    } else if head.starts_with(b"#EXTM3U") {
        "HLS"
    } else if head.starts_with(b"\x1aE\xdf\xa3") {
        "MATROSKA"
    } else {
        "?"
    };
    Ok(format!(
        "HTTP {st} {ct} magic={magic} bytes={} head={:02x?}",
        n,
        &head[..head.len().min(8)]
    ))
}

fn truncate(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        s.chars().take(n).collect::<String>() + "…"
    }
}

fn first_line(s: &str) -> String {
    s.lines().next().unwrap_or("").to_string()
}
