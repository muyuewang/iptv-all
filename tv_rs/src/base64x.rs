//! 宽容 base64 编解码（latin1 语义），与 Python 版 `b64d` / `b64e` 对齐。
//!
//! Python 版：
//! ```python
//! def b64d(s):   # 自动补 padding，返回 latin1 字符串或 None
//!     s = s.strip()
//!     return base64.b64decode(s + "=" * ((-len(s)) % 4)).decode("latin1")
//! def b64e(s):
//!     return base64.b64encode(s.encode("latin1")).decode("ascii")
//! ```
//!
//! latin1 = ISO-8859-1 = 每个字节直接映射 0..=255 的码点，因此：
//! - 解码 -> 字节串（Rust 里直接当 `Vec<u8>`，latin1 的每个码点 == 该字节值）
//! - 编码 -> 字节串每个字节当 latin1 码点重新编码

use base64::Engine;

/// 宽容 base64 解码（自动补 padding），返回字节串；失败返回 None。
///
/// 对应 `b64d`。注意 Python 的 `.decode("latin1")` 在 Rust 里等价于
/// **直接保留字节流**（latin1 码点 == 字节值），所以这里返回 `Vec<u8>` 即可，
/// 上层要当 latin1 字符串用时按字节逐一对映。
pub fn b64d(s: &str) -> Option<Vec<u8>> {
    b64d_bytes(s.as_bytes())
}

/// 字节级 base64 解码 —— XOR 解密链必须走这个入口。
///
/// `decrypt_page` 里 XOR 的产物是**任意字节**（0..=255），不保证是合法 UTF-8。
/// 若先 `String::from_utf8_lossy` 再交给 [`b64d`]，非法字节会被替换成
/// U+FFFD（3 字节），既改变长度也改变内容，与 Python 的 latin1 语义彻底偏离。
/// 必须始终以 `&[u8]` 传递。
pub fn b64d_bytes(raw: &[u8]) -> Option<Vec<u8>> {
    // 对应 Python `s.strip()`：去掉首尾 ascii 空白
    let mut start = 0usize;
    let mut end = raw.len();
    while start < end && raw[start].is_ascii_whitespace() {
        start += 1;
    }
    while end > start && raw[end - 1].is_ascii_whitespace() {
        end -= 1;
    }
    let s = &raw[start..end];
    if s.is_empty() {
        return None;
    }
    // 自动补 padding（len % 4 的余数补 '='）
    let pad = (4 - s.len() % 4) % 4;
    let mut buf = Vec::with_capacity(s.len() + pad);
    buf.extend_from_slice(s);
    buf.extend(std::iter::repeat(b'=').take(pad));
    base64::engine::general_purpose::STANDARD.decode(&buf).ok()
}

/// base64 编码（把字节流当 latin1 视为字节串），返回 ASCII 字符串。
///
/// 对应 `b64e`。原 Python 是 `s.encode("latin1")`——即输入 latin1 字符串的
/// 每个码点取低字节。Rust 侧直接接收 `&[u8]`（语义等价）。
pub fn b64e(data: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(data)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn b64d_pads_automatically() {
        // "YWJj" = "abc"；去掉 padding 的变体 "YWJj" 本身 len%4==0。
        // 用需要补 padding 的："YQ" -> "a"（len=2，补 2 个 '='）
        assert_eq!(b64d("YQ").unwrap(), b"a");
        // "YWI" -> "ab"（len=3，补 1 个 '='）
        assert_eq!(b64d("YWI").unwrap(), b"ab");
    }

    #[test]
    fn b64d_roundtrip() {
        let data = b"hello world \x00\xff binary";
        assert_eq!(b64d(&b64e(data)).unwrap(), data.as_slice());
    }

    #[test]
    fn b64d_invalid_returns_none() {
        assert!(b64d("!!!not base64!!!").is_none());
    }
}