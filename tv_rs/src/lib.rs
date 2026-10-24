//! clean-tv-core — 纯净电视 Rust 数据层
//!
//! 从 `shared/core.py` 与 `shared/wvt.py` 移植的**纯逻辑**（无 GUI / 无网络 / 无 Windows 依赖），
//! 目标是用同一组测试向量与 Python 版逐字节对齐，作为 Rust 重写的第 1 个里程碑。
//!
//! 模块对照：
//! - [`core`]     对应 `shared/core.py`（XOR 线路解密、频道解析、台标匹配、EPG）
//! - [`wvt`]      对应 `shared/wvt.py`（AES-CBC .dat 解密、线路分类、适配匹配）
//! - [`base64x`]  宽容 base64（latin1 语义），对应 `b64d` / `b64e`

pub mod base64x;
pub mod core;
pub mod wvt;