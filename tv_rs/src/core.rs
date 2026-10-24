//! 核心业务逻辑，对应 `shared/core.py`。
//!
//! 本模块只移植**纯逻辑**（可离线、可单元测试），不含网络抓取：
//! - [`parse_channels`]  频道列表解析
//! - [`decrypt_page`]    XOR 线路解密（已知明文反推密钥）
//! - [`match_logo`]      台标匹配（与 JS 版 logoSrc 对齐）
//! - [`parse_epg`]       节目单解析

use regex::Regex;

use crate::base64x::{b64d, b64d_bytes, b64e};

/// 频道条目：`(id, name)`
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Channel {
    pub id: String,
    pub name: String,
}

/// 节目单条目
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct EpgItem {
    pub time: String,
    pub name: String,
}

/// 从分类页 HTML 解析频道列表 `[(id, name), ...]`。
///
/// 对应 Python `parse_channels`，正则：
/// `act=play[^'"]*&tid=([a-z0-9]+)&id=(\d+)['"]?[^>]*>([^<]+)<`
pub fn parse_channels(html: &str, tid: &str) -> Vec<Channel> {
    let re = Regex::new(
        r#"act=play[^'"]*&tid=([a-z0-9]+)&id=(\d+)['"]?[^>]*>([^<]+)<"#,
    )
    .unwrap();
    let mut out: Vec<Channel> = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for cap in re.captures_iter(html) {
        let g_tid = &cap[1];
        let g_id = &cap[2];
        let g_name = &cap[3];
        if g_tid != tid || seen.iter().any(|x| x == g_id) {
            continue;
        }
        seen.push(g_id.to_string());
        out.push(Channel {
            id: g_id.to_string(),
            name: g_name.trim().to_string(),
        });
    }
    out
}

/// 把 JS 里的 `"a"."b".split("").reverse().join("")` 常量拼接式求值。
///
/// 对应 Python `_concat_val`：
/// 1. 先正则把 `"xxx".split("").reverse().join("")` 替换成反序字符串字面量
/// 2. 再拼接所有 `"..."` 字面量
pub fn concat_val(expr: &str) -> String {
    // 匹配 "xxx".split("").reverse().join("") 形式，替换为反序
    let rev_re = Regex::new(r#""([^"]*)"\s*\.split\(""\)\s*\.reverse\(\)\s*\.join\(""\)"#).unwrap();
    let replaced = rev_re.replace_all(expr, |cap: &regex::Captures| {
        let s: String = cap[1].chars().rev().collect();
        format!("\"{}\"", s)
    });
    // 提取所有 "..." 字面量并拼接
    let str_re = Regex::new(r#""([^"]*)""#).unwrap();
    let mut out = String::new();
    for cap in str_re.captures_iter(&replaced) {
        out.push_str(&cap[1]);
    }
    out
}

/// 解析一小段 JS 变量赋值块 -> `HashMap<变量名, 字符串值>`。
///
/// 对应 Python `_parse_env`。
pub fn parse_env(block: &str) -> std::collections::HashMap<String, String> {
    let re = Regex::new(r#"(?:var\s+)?([A-Za-z_$][\w$]*)\s*=\s*([^;]+);"#).unwrap();
    let ident = Regex::new(r#"^[A-Za-z_$][\w$]*$"#).unwrap();
    let mut env = std::collections::HashMap::new();
    for cap in re.captures_iter(block) {
        let name = cap[1].to_string();
        let expr = cap[2].trim().to_string();
        if ident.is_match(&expr) {
            let v = env.get(&expr).cloned().unwrap_or_default();
            env.insert(name, v);
        } else {
            env.insert(name, concat_val(&expr));
        }
    }
    env
}

/// 解密结果：`(urls, msg)`
#[derive(Debug, Clone)]
pub struct DecryptResult {
    pub urls: Vec<String>,
    pub msg: String,
}

/// 还原播放页里的全部线路地址（XOR 解密）。
///
/// 对应 Python `decrypt_page`。算法链：
/// `option 值反转 -> base64 解 -> XOR(key) -> base64 解 -> 换 token -> 去盐前缀`
///
/// key 由「第 1 条线路的已知明文」与自造的同构密文逐字节异或反推。
///
/// 参数 `api` 是当前生效的接口域名（解密与抓取必须同源）。
pub fn decrypt_page(html: &str, tid: &str, cid: &str, api: &str) -> DecryptResult {
    let fail = |msg: &str| DecryptResult { urls: vec![], msg: msg.to_string() };

    // 1) 找 assign 块（变量赋值）与 ent 块（含解密函数的 script）
    let script_re = Regex::new(r"(?s)<script[^>]*>(.*?)</script>").unwrap();
    let assign_re = Regex::new(r#"var\s+\w+\s*=\s*""\s*;"#).unwrap();
    let reverse_marker = "split(\"\").reverse()";
    let func_marker = Regex::new(
        r"function\s+\w+\s*\([^)]*\)\s*\{[^}]*\.reverse\(\)[^}]*\}",
    )
    .unwrap();

    let mut assign: Option<&str> = None;
    let mut ent: Option<&str> = None;
    for s in script_re.captures_iter(html) {
        let body = s.get(1).unwrap().as_str();
        if assign.is_none()
            && assign_re.is_match(body)
            && body.contains(reverse_marker)
            && !body.contains("function")
        {
            assign = Some(body);
        }
        if ent.is_none() && func_marker.is_match(body) {
            ent = Some(body);
        }
    }
    let (assign, ent) = match (assign, ent) {
        (Some(a), Some(e)) => (a, e),
        _ => return fail("页面结构变化"),
    };

    // 2) 提取 key 变量与 token 变量
    let m_key = Regex::new(r"\(\s*\w+\s*,\s*(\w+)\s*\)").unwrap();
    let m_tok = Regex::new(r#""token="\+(\w+)\s*,\s*"token="\+(\w+)"#).unwrap();
    let cap_key = match m_key.captures(ent) {
        Some(c) => c[1].to_string(),
        None => return fail("参数缺失"),
    };
    let cap_tok = match m_tok.captures(ent) {
        Some(c) => (c[1].to_string(), c[2].to_string()),
        None => return fail("参数缺失"),
    };

    let env = parse_env(assign);
    let key_seed = env.get(&cap_key).cloned().unwrap_or_default();
    let old = env.get(&cap_tok.0).cloned().unwrap_or_default();
    let new = env.get(&cap_tok.1).cloned().unwrap_or_default();
    if key_seed.is_empty() || old.is_empty() || new.is_empty() {
        return fail("变量为空");
    }

    // 3) 收集所有 option 值
    let opt_re = Regex::new(r#"<option value="([^"]+)""#).unwrap();
    let mut opts: Vec<String> = Vec::new();
    for cap in opt_re.captures_iter(html) {
        opts.push(cap[1].to_string());
    }
    if opts.is_empty() {
        return fail("无线路数据");
    }

    // 4) 用第 1 条线路反推 XOR key（已知明文攻击）
    //    s0 = b64d(opts[0][::-1])
    let s0_rev: String = opts[0].chars().rev().collect();
    let s0 = match b64d(&s0_rev) {
        Some(v) => v,
        None => return fail("解码失败"),
    };
    //    url0 = f"{api}/eplay2.php?token={old}&tid={tid}&id={cid}&p=0&type=.flv"
    let url0 = format!(
        "{}/eplay2.php?token={}&tid={}&id={}&p=0&type=.flv",
        api.trim_end_matches('/'),
        old,
        tid,
        cid
    );
    let out0 = b64e(url0.as_bytes());
    if s0.len() != out0.len() {
        return fail("密钥长度不符");
    }
    //    key[j] = ord(s0[j]) ^ ord(out0[j])
    let key: Vec<u8> = s0
        .iter()
        .zip(out0.as_bytes().iter())
        .map(|(a, b)| a ^ b)
        .collect();

    // 5) 逐条解密
    let mut urls: Vec<String> = Vec::new();
    for opt in &opts {
        let st_rev: String = opt.chars().rev().collect();
        let st = match b64d(&st_rev) {
            Some(v) => v,
            None => continue,
        };
        // o2 = ''.join(chr(ord(st[t]) ^ key[t % len(key)]))
        let o2: Vec<u8> = st
            .iter()
            .enumerate()
            .map(|(i, &b)| b ^ key[i % key.len()])
            .collect();
        // raw = b64d(o2) —— o2 是**任意字节**，必须走字节级解码。
        // Python 侧 o2 是 latin1 字符串，每个码点就是该字节值，绝不丢信息；
        // 这里若先 from_utf8_lossy 再解码，非 UTF-8 字节会被换成 U+FFFD，
        // 长度和内容双错，整条线路会静默解坏。
        let raw = match b64d_bytes(&o2) {
            Some(v) => v,
            None => continue,
        };
        // fin = raw.replace("token="+old, "token="+new).replace(key_seed, "")
        // 同样保持字节操作，避免中间引入编码转换
        let mut fin = replace_bytes(&raw, format!("token={}", old).as_bytes(), format!("token={}", new).as_bytes());
        fin = replace_bytes(&fin, key_seed.as_bytes(), b"");
        // Python: re.match(r'^https?://', fin) —— 只认开头
        if !fin.starts_with(b"http://") && !fin.starts_with(b"https://") {
            continue;
        }
        let mut fin = String::from_utf8_lossy(&fin).to_string();
        if !fin.contains("type=") {
            fin.push_str("&type=.flv");
        }
        if !urls.contains(&fin) {
            urls.push(fin);
        }
    }
    DecryptResult { urls, msg: "ok".to_string() }
}

/// 字节级 `str.replace` —— 对应 Python 的 `bytes.replace` / latin1 字符串 replace。
///
/// 不用 `String::replace` 是因为输入可能不是合法 UTF-8（XOR 产物）。
fn replace_bytes(hay: &[u8], from: &[u8], to: &[u8]) -> Vec<u8> {
    if from.is_empty() || hay.len() < from.len() {
        return hay.to_vec();
    }
    let mut out = Vec::with_capacity(hay.len());
    let mut i = 0usize;
    while i < hay.len() {
        if i + from.len() <= hay.len() && &hay[i..i + from.len()] == from {
            out.extend_from_slice(to);
            i += from.len();
        } else {
            out.push(hay[i]);
            i += 1;
        }
    }
    out
}

/// 解析播放页里的当日节目单。
///
/// 对应 Python `parse_epg`。
pub fn parse_epg(html: &str) -> Vec<EpgItem> {
    if !html.contains("id=\"myEpg\"") {
        return vec![];
    }
    let seg = match html.split("id=\"myEpg\"").nth(1) {
        Some(s) => match s.split("</ul>").next() {
            Some(x) => x,
            None => return vec![],
        },
        None => return vec![],
    };
    let re = Regex::new(
        r#"(?s)<li[^>]*>.*?<span>\s*([0-9]{1,2}:[0-9]{2})\s*([^<]*)</span>"#,
    )
    .unwrap();
    let mut out = Vec::new();
    for cap in re.captures_iter(seg) {
        out.push(EpgItem {
            time: cap[1].to_string(),
            name: cap[2].trim().to_string(),
        });
    }
    out
}

/// 台标名规范化：去空白（含全角空格）
fn norm(s: &str) -> String {
    s.chars()
        .filter(|c| !c.is_whitespace() && *c != '\u{3000}')
        .collect()
}

/// 台标匹配（与 JS 版 `logoSrc` 对齐）：精确 -> 去后缀别名 -> 包含匹配。
///
/// `logos` 是 `频道名 -> 图片URL` 的映射。
pub fn match_logo(name: &str, logos: &std::collections::HashMap<String, String>) -> String {
    if logos.is_empty() {
        return String::new();
    }
    let n = norm(name);
    if let Some(v) = logos.get(&n) {
        return v.clone();
    }
    // 去后缀别名（HD 大小写不敏感，对齐 Python 的 (?i)HD）
    let mut alias = n.clone();
    for pat in ["綜合", "综合", "頻道", "频道"] {
        alias = alias.replace(pat, "");
    }
    let hd_re = Regex::new(r"(?i)HD").unwrap();
    let alias = hd_re.replace_all(&alias, "").to_string();
    if let Some(v) = logos.get(&alias) {
        return v.clone();
    }
    // 包含匹配（len(k)>=3 且 len(n)>=2 且 (k in n or n in k)）
    for (k, v) in logos {
        if k.chars().count() >= 3 && n.chars().count() >= 2 && (k.contains(&n) || n.contains(k)) {
            return v.clone();
        }
    }
    String::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_channels_basic() {
        let html = r#"<a href="iptve.php?act=play&tid=tv&id=1">CCTV1综合</a><a href="iptve.php?act=play&tid=tv&id=2">CCTV2</a><a href="iptve.php?act=play&tid=ys&id=9">其他</a>"#;
        let ch = parse_channels(html, "tv");
        assert_eq!(ch.len(), 2);
        assert_eq!(ch[0].id, "1");
        assert_eq!(ch[0].name, "CCTV1综合");
        assert_eq!(ch[1].id, "2");
    }

    #[test]
    fn parse_channels_dedupe() {
        let html = r#"<a href="iptve.php?act=play&tid=tv&id=1">A</a><a href="iptve.php?act=play&tid=tv&id=1">A重复</a>"#;
        let ch = parse_channels(html, "tv");
        assert_eq!(ch.len(), 1);
    }

    #[test]
    fn concat_val_reverse() {
        // "abc".split("").reverse().join("") -> "cba"
        assert_eq!(concat_val(r#""abc".split("").reverse().join("")"#), "cba");
        // 拼接两个字符串
        assert_eq!(concat_val(r#""ab" + "cd""#), "abcd");
    }

    #[test]
    fn norm_removes_fullwidth_space() {
        assert_eq!(norm("CCTV1 综合\u{3000}频道"), "CCTV1综合频道");
    }

    #[test]
    fn match_logo_exact() {
        let mut logos = std::collections::HashMap::new();
        logos.insert("CCTV1".to_string(), "http://x/1.png".to_string());
        assert_eq!(match_logo("CCTV1", &logos), "http://x/1.png");
        assert_eq!(match_logo("不存在的台", &logos), "");
    }

    #[test]
    fn match_logo_alias() {
        let mut logos = std::collections::HashMap::new();
        logos.insert("CCTV1".to_string(), "http://x/1.png".to_string());
        // 去「综合」「频道」「HD」后缀后匹配
        assert_eq!(match_logo("CCTV1综合", &logos), "http://x/1.png");
    }

    #[test]
    fn parse_epg_extracts_items() {
        let html = r#"<div id="myEpg"><li><span>20:00 新闻联播</span></li><li><span>21:00 焦点访谈</span></li></ul>"#;
        let epg = parse_epg(html);
        assert_eq!(epg.len(), 2);
        assert_eq!(epg[0].time, "20:00");
        assert_eq!(epg[0].name, "新闻联播");
    }
}