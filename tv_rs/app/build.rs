fn main() {
    // tauri-build 会读 tauri.conf.json、生成窗口资源（Windows 上是 .rc/图标），
    // 并注入 Tauri 需要的 cfg。缺了它在 Windows 上会链接不到资源。
    tauri_build::build();
    // ★ 显式跟踪前端资源：tauri-build 没把 frontendDist（app/ui）纳入
    //   rerun-if-changed，导致改 index.html 后 cargo 不重编译、旧 UI 被内嵌进
    //   exe。这里手动钉上，改前端即触发 generate_context! 重新读取最新 HTML。
    println!("cargo:rerun-if-changed=ui");
}