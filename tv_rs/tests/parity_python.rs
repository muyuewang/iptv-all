//! 与 Python 原版（`shared/wvt.py`、`shared/core.py`）的**真实数据对拍**测试。
//!
//! 用仓库里 `_aes_test/*.dat` 三份真实抓包样本跑两边，比对解密输出的
//! **字节数 + FNV-1a 64 哈希**。哈希一致即代表逐字节一致，能锁死
//! UTF-8 边界、gzip 解包、PKCS7 去填充、key/iv 派生等全部细节。
//!
//! 基准值由 Python 侧离线算出（见文件末尾注释），Rust 侧不得为了「跑绿」
//! 而修改断言 —— 一旦不一致就说明移植有 bug。

use clean_tv_core::wvt;
use std::path::PathBuf;

/// FNV-1a 64 位，与 Python 侧实现逐位等价（mod 2^64 用 wrapping_mul 表达）。
fn fnv1a64(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}

fn sample(name: &str) -> Option<Vec<u8>> {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("_aes_test")
        .join(name);
    std::fs::read(&p).ok()
}

/// 三份真实样本的 (文件名, Python 解出字节数, Python FNV-1a64)
const SAMPLES: &[(&str, usize, u64)] = &[
    // full.dat            -> 频道表，904 个频道
    ("full.dat", 172_614, 0x0a10_404e_af7d_9ec6),
    // server_config.dat   -> 服务端配置，含 base64 图标
    ("server_config.dat", 548_286, 0xdf5f_7174_ad26_311b),
    // webpage_adapter.dat -> 网页适配规则，32 条
    ("webpage_adapter.dat", 13_871, 0x5d02_4927_1428_b394),
];

#[test]
fn decrypt_dat_byte_identical_to_python() {
    let mut checked = 0;
    for (name, want_len, want_hash) in SAMPLES {
        let Some(raw) = sample(name) else {
            // 样本不在（例如浅克隆）时跳过，不算失败
            eprintln!("skip {name}: 样本不存在");
            continue;
        };
        let text = wvt::decrypt_dat(&raw).unwrap_or_else(|| panic!("{name} 解密返回 None"));
        let bytes = text.as_bytes();
        assert_eq!(bytes.len(), *want_len, "{name} 解出长度不一致");
        assert_eq!(
            fnv1a64(bytes),
            *want_hash,
            "{name} 内容哈希不一致（移植与 Python 不等价）"
        );
        checked += 1;
    }
    assert!(checked > 0, "一份样本都没跑，对拍无效");
}

#[test]
fn decrypt_full_dat_yields_904_channels() {
    let Some(raw) = sample("full.dat") else {
        eprintln!("skip: 样本不存在");
        return;
    };
    let json = wvt::decrypt_dat_json(&raw).expect("full.dat 应解出合法 JSON");
    let arr = json.as_array().expect("full.dat 顶层应为数组");
    assert_eq!(arr.len(), 904, "频道条数应与 Python 一致");

    // 首条结构：{name, group, urls:[{url, extra}]}
    let first = &arr[0];
    assert!(first.get("name").is_some(), "缺少 name");
    assert!(first.get("group").is_some(), "缺少 group");
    let urls = first.get("urls").and_then(|v| v.as_array()).expect("缺少 urls");
    assert!(!urls.is_empty(), "首条 url 列表不应为空");
    assert!(
        urls[0].get("url").and_then(|v| v.as_str()).is_some(),
        "url 条目缺少 url 字段"
    );
}

#[test]
fn decrypt_adapter_dat_yields_32_adapters() {
    let Some(raw) = sample("webpage_adapter.dat") else {
        eprintln!("skip: 样本不存在");
        return;
    };
    let json = wvt::decrypt_dat_json(&raw).expect("webpage_adapter.dat 应解出合法 JSON");
    let arr = json.as_array().expect("适配器顶层应为数组");
    assert_eq!(arr.len(), 32, "适配器条数应与 Python 一致");

    // 反序列化进 WebAdapter，验证 #[serde(flatten)] rest 能吃下未知字段
    let adapter: wvt::WebAdapter = serde_json::from_value(arr[0].clone())
        .expect("首条适配器应能反序列化为 WebAdapter");
    assert!(!adapter.url.is_empty(), "适配器 url 不应为空");
}

#[test]
fn decrypt_server_config_is_object() {
    let Some(raw) = sample("server_config.dat") else {
        eprintln!("skip: 样本不存在");
        return;
    };
    let json = wvt::decrypt_dat_json(&raw).expect("server_config.dat 应解出合法 JSON");
    let obj = json.as_object().expect("server_config 顶层应为对象");
    assert!(obj.contains_key("image"), "缺少 image（图标 base64）");
    assert!(obj.contains_key("pcUserAgent"), "缺少 pcUserAgent");
}

/// 用真实频道表校验 `is_direct_stream` 的分类比例与 Python 一致。
/// 记忆里的实测口径：直链约 5.5%、央视系网页约 12.6%、其余地方台网页。
#[test]
fn split_sources_on_real_data_matches_known_ratio() {
    let Some(raw) = sample("full.dat") else {
        eprintln!("skip: 样本不存在");
        return;
    };
    let json = wvt::decrypt_dat_json(&raw).expect("full.dat 应解出合法 JSON");
    let channels: Vec<wvt::WvtChannel> = serde_json::from_value(json).expect("应能反序列化频道表");

    let (direct, web) = wvt::split_sources(&channels);
    let total = direct.len() + web.len();
    assert!(total > 0, "线路总数不应为 0");

    let pct = direct.len() as f64 * 100.0 / total as f64;
    eprintln!("直连 {} / 总计 {} = {:.2}%", direct.len(), total, pct);
    // 直链占比很低（历史实测 5.5% 量级），用宽区间兜住数据版本漂移
    assert!(
        (1.0..20.0).contains(&pct),
        "直链占比 {pct:.2}% 超出合理区间，分类逻辑可能出错"
    );
}

// ==================================================================
// XOR 线路解密（decrypt_page）对拍
// ==================================================================
//
// 向量由 `tools/gen_xor_fixture.py` 按 decrypt_page 的**逆过程**合成，
// 期望值由 Python 原版算出。这个向量的价值在于：
// 4 条线路的 XOR 中间产物**全部含非 UTF-8 字节**（生成脚本会打印 "4 / 4"）。
// 若 Rust 侧走 `String::from_utf8_lossy` 再用 `b64d(&str)`，非法字节会被
// 替换成 U+FFFD（3 字节），长度与内容双错 → 线路全部解坏。
// 必须走 `b64d_bytes(&[u8])` 才与 Python 的 latin1 语义等价。

fn fixture(name: &str) -> Option<String> {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join(name);
    std::fs::read_to_string(&p).ok()
}

#[test]
fn decrypt_page_matches_python_on_xor_fixture() {
    let Some(html) = fixture("page_xor.html") else {
        eprintln!("skip: page_xor.html 不存在，先跑 tools/gen_xor_fixture.py");
        return;
    };
    let Some(expect_txt) = fixture("page_xor.expect.json") else {
        eprintln!("skip: page_xor.expect.json 不存在");
        return;
    };
    let expect: serde_json::Value = serde_json::from_str(&expect_txt).expect("期望文件应为合法 JSON");

    let api = expect["api"].as_str().expect("缺少 api");
    let tid = expect["tid"].as_str().expect("缺少 tid");
    let cid = expect["cid"].as_str().expect("缺少 cid");
    let want_msg = expect["msg"].as_str().expect("缺少 msg");
    let want_urls: Vec<String> = expect["urls"]
        .as_array()
        .expect("缺少 urls")
        .iter()
        .map(|v| v.as_str().unwrap().to_string())
        .collect();

    let got = clean_tv_core::core::decrypt_page(&html, tid, cid, api);

    assert_eq!(got.msg, want_msg, "msg 不一致");
    assert_eq!(
        got.urls, want_urls,
        "解出的线路与 Python 不一致\n  rust = {:?}\n  py   = {:?}",
        got.urls, want_urls
    );
    // 这个向量必须真的覆盖 4 条线路，否则对拍没有说服力
    assert_eq!(got.urls.len(), 4, "应解出 4 条线路");
}

#[test]
fn decrypt_page_epg_matches_python_on_xor_fixture() {
    let Some(html) = fixture("page_xor.html") else {
        eprintln!("skip: page_xor.html 不存在");
        return;
    };
    let Some(expect_txt) = fixture("page_xor.expect.json") else {
        eprintln!("skip: 期望文件不存在");
        return;
    };
    let expect: serde_json::Value = serde_json::from_str(&expect_txt).unwrap();

    let got = clean_tv_core::core::parse_epg(&html);
    let want: Vec<(String, String)> = expect["epg"]
        .as_array()
        .expect("缺少 epg")
        .iter()
        .map(|v| {
            (
                v["time"].as_str().unwrap().to_string(),
                v["name"].as_str().unwrap().to_string(),
            )
        })
        .collect();

    assert_eq!(got.len(), want.len(), "EPG 条数不一致");
    for (i, (gt, (wt, wn))) in got.iter().zip(want.iter()).enumerate() {
        assert_eq!((&gt.time, &gt.name), (wt, wn), "EPG 第 {i} 条不一致");
    }
    // myEpg 段落外的那条 li 不应被解析
    assert!(
        !got.iter().any(|e| e.name.contains("不应被解析")),
        "解析越界：抓到了 myEpg 段外的条目"
    );
}

/// 直接锁死 `b64d_bytes` 的必要性：非 UTF-8 字节经 latin1 往返必须无损。
///
/// 若有人把 `b64d_bytes` 换回 `b64d(&String::from_utf8_lossy(..))`，
/// 这个测试会立刻失败。
#[test]
fn b64d_bytes_preserves_non_utf8_roundtrip() {
    // 0x80..=0xFF 的孤立字节在 UTF-8 里非法
    let raw: Vec<u8> = vec![0x00, 0x7f, 0x80, 0x81, 0xfe, 0xff, 0xc0, 0xc1];
    let encoded = clean_tv_core::base64x::b64e(&raw);
    // 字节级解码：必须逐字节还原
    let back = clean_tv_core::base64x::b64d_bytes(encoded.as_bytes())
        .expect("字节级解码应成功");
    assert_eq!(back, raw, "b64d_bytes 未能逐字节还原非 UTF-8 数据");

    // 反证：走 from_utf8_lossy 会丢信息（长度/内容都会变）
    let lossy = String::from_utf8_lossy(&raw).to_string();
    assert_ne!(
        lossy.as_bytes(),
        raw.as_slice(),
        "from_utf8_lossy 本该是有损的，这个前提变了说明测试假设需要复核"
    );
}