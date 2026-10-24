fn main() {
    // tauri-build 会读 tauri.conf.json、生成窗口资源（Windows 上是 .rc/图标），
    // 并注入 Tauri 需要的 cfg。缺了它在 Windows 上会链接不到资源。
    tauri_build::build()
}