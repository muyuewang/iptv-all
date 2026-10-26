//! WebViewTV 源适配层，对应 `shared/wvt.py`。
//!
//! 本模块移植**纯逻辑**（不含网络下载）：
//! - [`decrypt_dat`] / [`decrypt_dat_json`]  `.dat` 解密（AES-CBC → gzip → JSON）
//! - [`is_direct_stream`] / [`split_sources`] 线路分类
//! - [`url_rank`] / [`normalize_channels`]    线路优先级排序
//! - [`match_adapter`]                        网页适配规则匹配
//!
//! 解密算法（逆向自 App）：
//! ```text
//! name  = "com.vasthread.webviewtv.app.LiveApplication"
//! key   = reverse(name[:32]).encode()   # AES-128
//! iv    = name[:16].encode()
//! plain = AES-CBC-PKCS5 decrypt(data) -> GZIP 解包 -> JSON
//! ```

use aes::cipher::{block_padding::Pkcs7, BlockDecryptMut, KeyIvInit};
use flate2::read::GzDecoder;
use std::io::Read;

use regex::Regex;

/// 解密种子：App 的 Application 类名（作者把它反过来当 key）
const APP_CLASS: &str = "com.vasthread.webviewtv.app.LiveApplication";

type Aes128CbcDec = cbc::Decryptor<aes::Aes128>;
type Aes192CbcDec = cbc::Decryptor<aes::Aes192>;
type Aes256CbcDec = cbc::Decryptor<aes::Aes256>;

/// AES-CBC 解密（128/192/256 位密钥，含 PKCS7 去填充）。
///
/// 对应 Python `aes_cbc_decrypt`。按 key 长度自动选择 AES 变体。
pub fn aes_cbc_decrypt(data: &[u8], key: &[u8], iv: &[u8]) -> Option<Vec<u8>> {
    if iv.len() != 16 || !matches!(key.len(), 16 | 24 | 32) {
        return None;
    }
    // 非 16 字节整数倍时截断（对齐 Python 行为）
    let n = data.len() - data.len() % 16;
    if n == 0 {
        return Some(Vec::new());
    }
    let body = &data[..n];

    let out = match key.len() {
        16 => Aes128CbcDec::new_from_slices(key, iv)
            .ok()?
            .decrypt_padded_vec_mut::<Pkcs7>(body)
            .ok()?,
        24 => Aes192CbcDec::new_from_slices(key, iv)
            .ok()?
            .decrypt_padded_vec_mut::<Pkcs7>(body)
            .ok()?,
        32 => Aes256CbcDec::new_from_slices(key, iv)
            .ok()?
            .decrypt_padded_vec_mut::<Pkcs7>(body)
            .ok()?,
        _ => return None,
    };
    Some(out)
}

/// 把 WebViewTvLive 的 `.dat` 解成 JSON 文本。
///
/// 对应 Python `decrypt_dat`：key = APP_CLASS 前 32 字符反序，iv = 前 16 字符。
pub fn decrypt_dat(data: &[u8]) -> Option<String> {
    let key: Vec<u8> = APP_CLASS
        .chars()
        .take(32)
        .collect::<String>()
        .chars()
        .rev()
        .collect::<String>()
        .into_bytes();
    let iv: Vec<u8> = APP_CLASS.chars().take(16).collect::<String>().into_bytes();

    let plain = aes_cbc_decrypt(data, &key, &iv)?;

    // 尝试 gzip 解包；失败则原文（对应 Python 的 try/except pass）
    let buf = match gunzip(&plain) {
        Some(v) => v,
        None => plain,
    };
    Some(String::from_utf8_lossy(&buf).to_string())
}

/// gzip 解包，失败返回 None。
fn gunzip(data: &[u8]) -> Option<Vec<u8>> {
    let mut d = GzDecoder::new(data);
    let mut out = Vec::new();
    match d.read_to_end(&mut out) {
        Ok(_) if !out.is_empty() => Some(out),
        _ => None,
    }
}

/// 把 `.dat` 解成 JSON 值。
pub fn decrypt_dat_json(data: &[u8]) -> Option<serde_json::Value> {
    let txt = decrypt_dat(data)?;
    serde_json::from_str(&txt).ok()
}

// ================================================================== 线路分类

/// 可直接播放的媒体扩展名
pub const MEDIA_EXT: [&str; 6] = [".m3u8", ".flv", ".ts", ".mp4", ".mkv", ".mpd"];

/// 判断某条线路是否「可直接播放」（不需要浏览器嗅探）。
///
/// 对应 Python `is_direct_stream`，并额外覆盖官方源的 `eplay2.php` 转发口：
/// - 去掉 query 后以媒体扩展名结尾
/// - 或 URL 里含 `.m3u8`（形如转发口 `?url=...m3u8`）
/// - 或含 `type=.flv`（官方源 HTTP-FLV 转发口，mpv 可直接硬解）
///
/// ★★ 2026-10-26 补充：本机源（app 版后端 `api*.2026016.xyz`）除 `eplay2.php`
///   转发口外，还有**中转口**形态
///   `https://api1.2026016.xyz/v/{cqyx,ysp6,migu8,bstv,bstv1,hunantv,bfiptv}.php
///    ?token=<T>&id=<台标缩写>[&xra=<档位>]`
///   —— 它 **302 跳到真流**（`t*.iptv200.com:8443/live/*.flv` 或
///   `dbiptv.sn.chinamobile.com/PLTV/.../index.m3u8`），mpv 跟随 302 即可播，
///   **不需要嗅探**。实测（2026-10-26）本机源当前解密出的线路**全部**是
///   `eplay2.php` 形态（`/v/` 是它 302 的**跳转目标**，不是解密产物），
///   但源站随时可能改版，故一并纳入判定，避免整批线路被误判成「网页线路」。
///
/// ⚠ 判据只认 **path 里含 `/v/` 且以 `.php` 结尾** —— 不能用 `contains(".php")`
///   （会把 `iptve.php?act=play` 播放页、`p.iptv200.com/play.php` 这类
///   HTML/已下线接口误判成可播）。
pub fn is_direct_stream(url: &str) -> bool {
    if url.is_empty() {
        return false;
    }
    let lower = url.to_lowercase();
    let base = url.split('?').next().unwrap_or("").to_lowercase();
    if MEDIA_EXT.iter().any(|ext| base.ends_with(ext)) {
        return true;
    }
    // ★ 真 m3u8 直链：URL 本体（去掉 query）以 `.m3u8` 结尾。
    //   注意这里要排除「中转网页」——如 `http://m3u8.cec-tv.com.cn/?url=...m3u8`
    //   它的 path 是 `/`，query 里才含 `.m3u8`，实际返回 HTML 播放页、mpv 无法
    //   直接播（"Failed to recognize file format"）。所以不能用 `lower.contains(".m3u8")`。
    if base.ends_with(".m3u8") || base.ends_with(".m3u8/") {
        return true;
    }
    // ★ 官方源 eplay2.php 转发口：`.../eplay2.php?token=...&p=0&type=.flv`
    //   是 HTTP-FLV 流，mpv 可直接播。
    if lower.contains("eplay2.php") && lower.contains("type=.flv") {
        return true;
    }
    // ★ 本机源中转口：`.../v/<name>.php?token=..&id=..` → 302 到真流。
    if is_relay_stream(&base) {
        return true;
    }
    false
}

/// 本机源中转口判定：`path` 里含 `/v/` 段且以 `.php` 结尾。
///
/// 与 [`is_direct_stream`] 分开是因为「官网源网页线路」也要走同一判定，
/// 而官网源的中转口没有 `/v/` 段（那是本机源后端独有的形态）。
pub fn is_relay_stream(base_path: &str) -> bool {
    if !base_path.ends_with(".php") {
        return false;
    }
    // path 形如 `/v/cqyx.php` → 段级判断，避免 `/somev/cqyx.php` 误判
    base_path
        .split('/')
        .any(|seg| seg.eq_ignore_ascii_case("v"))
}

/// 是否「URL 本体以 .m3u8 结尾」的真 m3u8 直链（区别于中转网页把 .m3u8 藏在 query）。
/// 供本机源选线时优先挑选真正可直接播的流。
pub fn is_m3u8_direct(url: &str) -> bool {
    if url.is_empty() {
        return false;
    }
    let base = url.split('?').next().unwrap_or("").to_lowercase();
    base.ends_with(".m3u8") || base.ends_with(".m3u8/")
}

/// 一条线路（展平后的条目）
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct SourceItem {
    pub group: String,
    pub name: String,
    pub url: String,
    pub extra: String,
    pub index: usize,
    pub total: usize,
}

/// 频道表条目：`{group, name, urls:[{url, extra}]}`
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct WvtChannel {
    #[serde(default)]
    pub group: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub urls: Vec<WvtUrl>,
}

/// 频道内的一条线路
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct WvtUrl {
    #[serde(default)]
    pub url: String,
    #[serde(default)]
    pub extra: String,
}

/// 把线路分成 (直连, 网页) 两组。
///
/// 对应 Python `split_sources`。
pub fn split_sources(channels: &[WvtChannel]) -> (Vec<SourceItem>, Vec<SourceItem>) {
    let mut direct = Vec::new();
    let mut web = Vec::new();
    for c in channels {
        let total = c.urls.len();
        for (idx, u) in c.urls.iter().enumerate() {
            let item = SourceItem {
                group: c.group.clone(),
                name: c.name.clone(),
                url: u.url.clone(),
                extra: u.extra.clone(),
                index: idx,
                total,
            };
            if is_direct_stream(&item.url) {
                direct.push(item);
            } else {
                web.push(item);
            }
        }
    }
    (direct, web)
}

/// 线路优先级（v2.10.24 修正后的实测结论）。
///
/// 只排序不删除：`tv.cctv.com`(0) > 央视频(1) > 其它(2) > 移动直播间(3)。
pub fn url_rank(u: Option<&str>) -> u8 {
    let s = u.unwrap_or("");
    if s.contains("tv.cctv.com") {
        return 0;
    }
    if s.contains("yangshipin") {
        return 1;
    }
    if s.contains("cctvnews") || s.contains("liveRoom") {
        return 3;
    }
    2
}

/// 对频道表按 [`url_rank`] 做稳定排序（每频道内线路顺序）。
///
/// 对应 Python `normalize_channels`：`sorted` 是稳定的，同分保持原顺序，
/// 这里用 `sort_by_key`（Rust 的 slice sort 同样是稳定排序）。
pub fn normalize_channels(channels: &mut [WvtChannel]) {
    for c in channels.iter_mut() {
        if c.urls.len() >= 2 {
            c.urls
                .sort_by_key(|x| url_rank(Some(x.url.as_str())));
        }
    }
}

// ================================================================== 适配规则

/// 网页适配规则（字段较多且各源不同，保留宽松结构）
#[derive(Debug, Clone, serde::Deserialize)]
pub struct WebAdapter {
    #[serde(default)]
    pub url: String,
    #[serde(default)]
    pub ua: String,
    #[serde(default, rename = "videoElementCondition")]
    pub video_element_condition: String,
    #[serde(flatten)]
    pub rest: std::collections::HashMap<String, serde_json::Value>,
}

/// 按域名匹配网页适配规则；没匹配到返回 None（走通用规则）。
///
/// 对应 Python `match_adapter`：取「key 是 host 子串」中最长的一条。
pub fn match_adapter<'a>(url: &str, adapters: &'a [WebAdapter]) -> Option<&'a WebAdapter> {
    if adapters.is_empty() {
        return None;
    }
    let host_re = Regex::new(r"(?i)^https?://").unwrap();
    let host = host_re.replace(url, "").to_string();
    let host = host.split('/').next().unwrap_or("").to_lowercase();

    let mut best: Option<&WebAdapter> = None;
    for a in adapters {
        let key = a.url.to_lowercase();
        if key.is_empty() {
            continue;
        }
        if host.contains(&key) {
            match best {
                None => best = Some(a),
                Some(b) if key.len() > b.url.len() => best = Some(a),
                _ => {}
            }
        }
    }
    best
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn direct_stream_detection() {
        // 路径以媒体扩展名结尾
        assert!(is_direct_stream("http://a.com/live.m3u8"));
        assert!(is_direct_stream("http://a.com/x.flv?a=1")); // query 被剥掉后仍是 .flv
        assert!(is_direct_stream("http://a.com/x.M3U8")); // 大小写不敏感
        // ★ 转发口（query 里带 .m3u8）**不算**直连：实测 `http://m3u8.cec-tv.com.cn/?url=...`
        //   返回的是 HTML 播放页，mpv 报 "Failed to recognize file format"。
        assert!(!is_direct_stream("http://a.com/play?url=http://b.com/live.m3u8"));
        // 域名里含 m3u8 但路径无媒体后缀 —— 必须判否，否则会把网页线路误判成直连
        assert!(!is_direct_stream("http://m3u8.cec-tv.com.cn/?url=abc"));
        assert!(!is_direct_stream("http://tv.cctv.com/live/cctv1/"));
        assert!(!is_direct_stream(""));
    }

    /// 与 Python `shared/wvt.py::is_direct_stream` 的逐例对拍结果锁死，
    /// 防止后续「优化」改变判定语义导致线路分类漂移。
    #[test]
    fn direct_stream_matches_python_reference() {
        // (url, python 实测返回值)
        let cases: &[(&str, bool)] = &[
            ("http://a.com/live.m3u8", true),
            ("http://a.com/x.flv?a=1", true),
            ("http://m3u8.cec-tv.com.cn/?url=abc", false),
            ("http://a.com/x.M3U8", true),
            ("http://tv.cctv.com/live/cctv1/", false),
            ("", false),
            // 转发口返回 HTML，判否（与 is_m3u8_direct 的语义一致）
            ("http://a.com/play?url=http://b.com/live.m3u8", false),
        ];
        for (url, want) in cases {
            assert_eq!(is_direct_stream(url), *want, "url={url}");
        }
    }

    /// 本机源中转口（`/v/*.php`）判为直连 —— mpv 跟随 302 即得真流。
    #[test]
    fn relay_stream_detection() {
        // ★ 本机源真实中转口（2026-10-26 实测形态）
        assert!(is_direct_stream(
            "https://api1.2026016.xyz/v/cqyx.php?token=ABC&id=gzws"
        ));
        assert!(is_direct_stream(
            "https://api1.2026016.xyz/v/ysp6.php?token=ABC&xra=4&id=fyzq"
        ));
        // 段级判断：`/somev/` 不是 `/v/`
        assert!(!is_direct_stream("https://x.com/somev/cqyx.php?token=A"));
        // ★ 不能把「播放页」和「已下线的 678 出口」误判成可播
        assert!(!is_direct_stream("https://api1.2026016.xyz/iptve.php?act=play&tid=gt&id=5"));
        assert!(!is_direct_stream(
            "https://p.iptv200.com/play.php?token=ABC&tid=gt&id=5&p=0&type=.flv"
        ));
        // is_relay_stream 直接判定
        assert!(is_relay_stream("/v/cqyx.php"));
        assert!(is_relay_stream("/v/bstv1.php"));
        assert!(!is_relay_stream("/iptve.php"));
        assert!(!is_relay_stream("/eplay2.php"));
    }

    #[test]
    fn url_rank_order() {
        assert_eq!(url_rank(Some("http://tv.cctv.com/live/cctv1/")), 0);
        assert_eq!(url_rank(Some("http://www.yangshipin.cn/tv/home")), 1);
        assert_eq!(url_rank(Some("http://other.com/x")), 2);
        assert_eq!(url_rank(Some("http://m-live.cctvnews.cctv.com/x")), 3);
        assert_eq!(url_rank(None), 2);
    }

    #[test]
    fn normalize_keeps_stable_within_same_rank() {
        let mut channels = vec![WvtChannel {
            group: "g".into(),
            name: "n".into(),
            urls: vec![
                WvtUrl { url: "http://other1.com".into(), extra: "".into() },
                WvtUrl { url: "http://tv.cctv.com/a".into(), extra: "".into() },
                WvtUrl { url: "http://other2.com".into(), extra: "".into() },
            ],
        }];
        normalize_channels(&mut channels);
        let urls: Vec<&str> = channels[0].urls.iter().map(|u| u.url.as_str()).collect();
        // tv.cctv.com 排到最前，两个 other 保持原相对顺序
        assert_eq!(
            urls,
            vec!["http://tv.cctv.com/a", "http://other1.com", "http://other2.com"]
        );
    }

    #[test]
    fn split_sources_groups_correctly() {
        let channels = vec![WvtChannel {
            group: "央视".into(),
            name: "CCTV1".into(),
            urls: vec![
                WvtUrl { url: "http://a.com/x.m3u8".into(), extra: "".into() },
                WvtUrl { url: "http://tv.cctv.com/live".into(), extra: "".into() },
            ],
        }];
        let (direct, web) = split_sources(&channels);
        assert_eq!(direct.len(), 1);
        assert_eq!(web.len(), 1);
        assert_eq!(direct[0].index, 0);
        assert_eq!(direct[0].total, 2);
        assert_eq!(web[0].url, "http://tv.cctv.com/live");
    }

    #[test]
    fn match_adapter_longest_domain_wins() {
        let ads = vec![
            WebAdapter {
                url: "cctv.com".into(),
                ua: "".into(),
                video_element_condition: "".into(),
                rest: Default::default(),
            },
            WebAdapter {
                url: "tv.cctv.com".into(),
                ua: "".into(),
                video_element_condition: "".into(),
                rest: Default::default(),
            },
        ];
        let m = match_adapter("https://tv.cctv.com/live/cctv1/", &ads).unwrap();
        assert_eq!(m.url, "tv.cctv.com");

        let none = match_adapter("https://unknown.com/x", &ads);
        assert!(none.is_none());
    }

    #[test]
    fn aes_key_iv_derivation_matches_python() {
        // key = reverse(APP_CLASS[:32])，iv = APP_CLASS[:16]
        let key: String = APP_CLASS
            .chars()
            .take(32)
            .collect::<String>()
            .chars()
            .rev()
            .collect();
        assert_eq!(key.len(), 32);
        // iv 就是类名前 16 字符
        let iv: String = APP_CLASS.chars().take(16).collect();
        assert_eq!(iv, "com.vasthread.we");
        // key 的第 1 个字符 == APP_CLASS 第 32 个字符（即反序）
        assert_eq!(
            key.chars().next().unwrap(),
            APP_CLASS.chars().nth(31).unwrap()
        );
    }

    #[test]
    fn aes_cbc_roundtrip_with_pkcs7() {
        // 用已知向量自检：key=16 个 0，iv=16 个 0，加密后应能解回原文
        use aes::cipher::{block_padding::Pkcs7, BlockEncryptMut, KeyIvInit};
        let key = [0u8; 16];
        let iv = [0u8; 16];
        let plain = b"hello aes cbc";
        let enc = cbc::Encryptor::<aes::Aes128>::new_from_slices(&key, &iv)
            .unwrap()
            .encrypt_padded_vec_mut::<Pkcs7>(plain);
        let dec = aes_cbc_decrypt(&enc, &key, &iv).unwrap();
        assert_eq!(dec, plain);
    }
}