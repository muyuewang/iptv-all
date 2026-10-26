//! 探针：查「失效频道」在 full.dat 里的完整线路（不只第一条直连），
//! 看是否有可用替代 URL。用法：cargo run --probe_wvt_detail
fn main() {
    let cli = clean_tv_core::net::blocking_client();
    let chans = match clean_tv_core::net::blocking_fetch_wvt_channels(&cli) {
        Ok(c) => c,
        Err(e) => { eprintln!("抓取失败: {e}"); return; }
    };
    // 失效频道名（实测 BAD 名单）
    let bad_names = [
        "安多卫视", "邓州综合", "洛阳科教", "洛阳新闻综合", "唐河一套", "淅川综合",
        "镇平综合", "方城综合", "济源一套", "包头新闻综合", "北海经济科教", "百色综合",
        "哈密汉语综合", "哈密维语综合", "哈密影视文艺", "澳視澳門", "澳視葡文",
        "澳門資訊", "澳門綜藝", "澳門體育", "澳門 Macau", "立法會直播",
    ];
    println!("===== 失效频道完整线路 =====");
    for c in &chans {
        if bad_names.contains(&c.name.as_str()) {
            println!("\n【{}】共 {} 条线路", c.name, c.urls.len());
            for (i, u) in c.urls.iter().enumerate() {
                let kind = if clean_tv_core::wvt::is_m3u8_direct(&u.url) { "m3u8直连" }
                    else if clean_tv_core::wvt::is_direct_stream(&u.url) { "直连(flv/etc)" }
                    else { "网页" };
                println!("  [{i}] {kind}\t{}", u.url);
            }
        }
    }
}