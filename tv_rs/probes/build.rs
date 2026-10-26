fn main() {
    // ★ 必须有 build.rs：`tauri::generate_context!()` 需要 tauri-build 生成的
    //   OUT_DIR 资源清单（图标/前端资产），缺了会报
    //   "OUT_DIR env var is not set"。
    tauri_build::build()
}
