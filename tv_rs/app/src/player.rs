//! 播放层：外挂 mpv（`--wid` 内嵌 + `--input-ipc-server` 控制）。
//!
//! 对应桌面版 `native_app.py` 的 mpv 部分。设计：
//! - **外挂 mpv.exe**：直连流（.flv/.m3u8 等）交给 mpv 硬解，最省资源、原生支持
//!   HTTP-FLV。WebView2 的 `<video>` 播不了 .flv（H.264 需转封装），所以必须外挂。
//! - **`--wid` 内嵌**：把 mpv 画面嵌进 Tauri 窗口（tauri 暴露 `hwnd()` 拿 HWND）。
//! - **`--input-ipc-server`**：命名管道控 mpv（暂停/音量/画面比例/下一线路）。
//! - **异常退出切线**：mpv 进程退出 → 通知上层切下一条线路（对应 `_watch_mpv`）。
//!
//! IPC 协议（Windows 命名管道）：payload = `{"command":[...]}\n`，`open(path, "r+b")`。
//! 注意 mpv 列表选项里逗号要转义成 `\\,`（见 desktop `_header_fields`，UA 里的
//! "KHTML, like Gecko" 若不转义会导致整个选项解析失败、mpv 退出码 2 秒退）。

use std::io::Write;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

/// 缓冲档位 → (cache 秒, demuxer-max-bytes, demuxer-max-back-bytes)，对应
/// `shared/core.py::BUFFER_TIERS`。
pub const BUFFER_TIERS: [(&str, u32, &str, &str); 3] = [
    ("low", 2, "8M", "4M"),
    ("balanced", 4, "32M", "16M"),
    ("smooth", 8, "96M", "48M"),
];

/// mpv 可执行文件路径由 main.rs 的 `resolve_exe_mpv` 负责定位（release 资源布局 +
/// 开发目录兜底），这里不再重复。

/// 一个正在运行的 mpv 播放会话。
pub struct Player {
    mpv: std::path::PathBuf,
    proc: Mutex<Option<Child>>,
    /// 命名管道路径（IPC 用）。
    pipe: Mutex<Option<String>>,
    /// 当前 ipc 连接句柄（Windows 命名管道以文件句柄读写）。
    ipc: Mutex<Option<std::fs::File>>,
    /// 会话代次（对外停止/切换后作废旧的 mpv 退出回调）。
    gen: std::sync::atomic::AtomicU64,
}

impl Player {
    pub fn new(mpv: std::path::PathBuf) -> Self {
        Self {
            mpv,
            proc: Mutex::new(None),
            pipe: Mutex::new(None),
            ipc: Mutex::new(None),
            gen: std::sync::atomic::AtomicU64::new(0),
        }
    }

    /// 当前 mpv 可执行文件路径（诊断用）。
    pub fn mpv_path(&self) -> &std::path::Path {
        &self.mpv
    }

    /// 当前会话代次。
    pub fn gen(&self) -> u64 {
        self.gen.load(std::sync::atomic::Ordering::SeqCst)
    }

    /// 停止当前播放（终止 mpv 进程、关 IPC）。
    pub fn stop(&self) {
        self.gen.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        if let Ok(mut g) = self.ipc.lock() {
            *g = None;
        }
        if let Ok(mut g) = self.pipe.lock() {
            *g = None;
        }
        if let Ok(mut g) = self.proc.lock() {
            if let Some(mut p) = g.take() {
                let _ = p.kill();
                let _ = p.wait();
            }
        }
    }

    /// 启动 mpv 播放 `url` 到给定窗口句柄 `hwnd`。
    ///
    /// * `hwnd` —— 父窗口 HWND（数值，来自 `WebviewWindow::hwnd()`）。
    /// * `tier` —— 缓冲档位（low/balanced/smooth）。
    /// * `headers` —— 嗅探流需要的额外请求头（referer/ua/cookie/origin）。
    ///
    /// 返回 (pid, ipc 管道路径)。失败返 Err。
    pub fn play(
        &self,
        url: &str,
        hwnd: u64,
        tier: &str,
        headers: &[(&str, &str)],
    ) -> Result<(u32, String), String> {
        // 先停掉旧的
        self.stop();

        let (cache_secs, maxb, maxbb) = BUFFER_TIERS
            .iter()
            .find(|(t, ..)| *t == tier)
            .map(|(_, c, b, bb)| (*c, *b, *bb))
            .unwrap_or((4, "32M", "16M"));

        // 命名管道：放到系统临时目录，进程间唯一。
        let pipe = std::env::temp_dir()
            .join(format!("clean-tv-mpv-{}.ipc", std::process::id()))
            .to_string_lossy()
            .to_string();

        let mut args: Vec<String> = vec![
            url.to_string(),
            format!("--wid={hwnd}"),
            "--hwdec=auto-safe".to_string(),
            "--vo=gpu".to_string(),
            "--cache=yes".to_string(),
            format!("--cache-secs={cache_secs}"),
            format!("--demuxer-max-bytes={maxb}"),
            format!("--demuxer-max-back-bytes={maxbb}"),
            "--network-timeout=12".to_string(),
            format!("--input-ipc-server={pipe}"),
            "--no-border".to_string(),
            "--no-osc".to_string(),
            "--cursor-autohide=no".to_string(),
            "--profile=fast".to_string(),
            "--really-quiet".to_string(),
        ];

        // 嗅探流通常校验 Referer/Cookie，必须带上（对应 `_header_fields`）。
        if let Some(hf) = header_fields(headers) {
            args.push(format!("--http-header-fields={hf}"));
        }

        let child = Command::new(&self.mpv)
            .args(&args)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("mpv 启动失败: {e}"))?;

        let pid = child.id();
        *self.proc.lock().unwrap() = Some(child);
        *self.pipe.lock().unwrap() = Some(pipe.clone());
        // release GUI 子系统无 stderr，这里只留 debug 打印（避免写空句柄）。
        #[cfg(debug_assertions)]
        eprintln!("[play] mpv 启动 pid={pid} url={}", &url[..url.len().min(80)]);
        Ok((pid, pipe))
    }

    /// 连接 IPC 通道（阻塞重试，最多 `timeout_ms`）。成功后可用 [`Player::cmd`]。
    #[allow(dead_code)]
    pub fn connect_ipc(&self, timeout_ms: u64) -> bool {
        let pipe = match self.pipe.lock().unwrap().clone() {
            Some(p) => p,
            None => return false,
        };
        let t0 = std::time::Instant::now();
        while t0.elapsed().as_millis() < timeout_ms as u128 {
            match std::fs::OpenOptions::new().read(true).write(true).open(&pipe) {
                Ok(f) => {
                    *self.ipc.lock().unwrap() = Some(f);
                    return true;
                }
                Err(_) => std::thread::sleep(std::time::Duration::from_millis(80)),
            }
        }
        false
    }

    /// 发一条 mpv IPC 命令（如 `set_property` / `loadfile`）。
    #[allow(dead_code)]
    pub fn cmd(&self, command: &[&str]) -> bool {
        let payload = format!(
            "{}\n",
            serde_json::json!({ "command": command }).to_string()
        );
        let mut g = match self.ipc.lock() {
            Ok(g) => g,
            Err(_) => return false,
        };
        match g.as_mut() {
            Some(f) => {
                if f.write_all(payload.as_bytes()).is_ok() {
                    let _ = f.flush();
                    return true;
                }
                false
            }
            None => false,
        }
    }

    /// 阻塞等待 mpv 退出，返回 (gen, exit_code)；用于看门狗线程。
    ///
    /// 返回的 gen 若与当前 gen 不一致，说明是被主动切换/停止，调用方应忽略。
    pub fn wait_exit(&self) -> Option<(u64, i32)> {
        let gen = self.gen();
        let code = {
            let mut g = self.proc.lock().unwrap();
            let p = g.as_mut()?;
            p.wait().ok()?.code().unwrap_or(-1)
        };
        Some((gen, code))
    }
}

/// 把嗅探到的请求头转成 mpv `--http-header-fields` 值。
///
/// mpv 列表选项以逗号分隔条目，值里的逗号必须转义成 `\\,`（UA 里的
/// "KHTML, like Gecko" 是典型）。
pub fn header_fields(headers: &[(&str, &str)]) -> Option<String> {
    if headers.is_empty() {
        return None;
    }
    const KEEP: [&str; 4] = ["referer", "user-agent", "cookie", "origin"];
    let mut parts = Vec::new();
    for (k, v) in headers {
        if KEEP.contains(&k.to_ascii_lowercase().as_str()) && !v.is_empty() && !v.contains('\n') {
            parts.push(format!("{k}: {}", v.replace(',', "\\,")));
        }
    }
    if parts.is_empty() {
        None
    } else {
        Some(parts.join(","))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn buffer_tiers_lookup_by_name() {
        let balanced = BUFFER_TIERS.iter().find(|(t, ..)| *t == "balanced").unwrap();
        assert_eq!(balanced.1, 4);
        assert_eq!(balanced.2, "32M");
        // low 档 cache 2s
        let low = BUFFER_TIERS.iter().find(|(t, ..)| *t == "low").unwrap();
        assert_eq!(low.1, 2);
    }

    #[test]
    fn header_fields_escapes_commas() {
        let h = header_fields(&[("user-agent", "Mozilla/5.0 (KHTML, like Gecko)")]).unwrap();
        assert!(h.contains("KHTML\\, like Gecko"), "got: {h}");
        // 无关头被过滤
        let none = header_fields(&[("x-custom", "abc")]);
        assert!(none.is_none());
    }

    #[test]
    fn header_fields_keeps_only_whitelist() {
        let h = header_fields(&[
            ("Referer", "https://tv.cctv.com/"),
            ("Cookie", "a=1"),
            ("Origin", "https://tv.cctv.com"),
            ("User-Agent", "xx"),
            ("X-Other", "drop"),
        ])
        .unwrap();
        assert!(h.contains("referer:"));
        assert!(h.contains("cookie:"));
        assert!(!h.contains("drop"));
    }
}