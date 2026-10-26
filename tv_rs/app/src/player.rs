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
///
/// ★ 2026-10-25 下调：实测 `--cache-secs=4` 会把起播硬推后 4 秒（mpv 要等缓存
///   填满才开始播 → 才有第一帧 → 才建视频窗口）。直播频道把这个值压到 2s，
///   UI 侧「无画面就换线」的看门狗负责兜住抖动，比一开始干等 4 秒强。
pub const BUFFER_TIERS: [(&str, u32, &str, &str); 3] = [
    ("low", 1, "8M", "4M"),
    ("balanced", 2, "32M", "16M"),
    ("smooth", 6, "96M", "48M"),
];

/// 一次播放会话的状态（供「是否真出画面」的看门狗轮询）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PollState {
    /// mpv 还在跑。
    Running,
    /// mpv 自己退出了（带退出码）。
    Exited(i32),
    /// child 已被 `stop()` 取走（会话被接管/主动停止）。
    Taken,
}

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
    /// 最近一次 `play()` 传入的父窗口 HWND，用于检测 mpv 的视频子窗口是否已创建。
    parent_hwnd: std::sync::atomic::AtomicU64,
}

impl Player {
    pub fn new(mpv: std::path::PathBuf) -> Self {
        Self {
            mpv,
            proc: Mutex::new(None),
            pipe: Mutex::new(None),
            ipc: Mutex::new(None),
            gen: std::sync::atomic::AtomicU64::new(0),
            parent_hwnd: std::sync::atomic::AtomicU64::new(0),
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
                // ★★ 不在这里 `wait()` —— 本函数会被 `play_channel` / `stop_channel`
                //   这类**同步命令**调用，而同步命令跑在主线程上。等 mpv 彻底退出
                //   通常几十毫秒，但 mpv 正在释放 GPU/解码器时可达数百毫秒到数秒，
                //   期间主线程收不了窗口消息 → Windows 直接把窗口标成「无响应」。
                //   进程回收放到后台线程，句柄由 Child 自己管理。
                std::thread::spawn(move || {
                    let _ = p.wait();
                });
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

        // ★ 诊断钩子：CLEAN_TV_MPV_LOG=<路径> 时把 mpv stderr 落盘（release GUI
        //   下默认 null 句柄，mpv 死因无从排查；验收时设此变量抓真实错误）。
        //   ★ 提前取：args 构造时要用它决定是否加 `--really-quiet`
        //     （加了就没有任何 stderr 输出，日志文件会是 0 字节）。
        let diag_mpv_log = std::env::var("CLEAN_TV_MPV_LOG").unwrap_or_default();

        let mut args: Vec<String> = vec![
            url.to_string(),
            format!("--wid={hwnd}"),
            // ★ 硬件解码器：默认走 `d3d11va`（零拷贝，GPU 直通）。
            //   ⚠️ 不要用 `auto-safe` —— 它在本机会选到 `d3d11va-copy`
            //   （拷贝回 CPU 的 nv12），经 libplacebo 上传/转换后**色度反了**
            //   （蓝↔粉对调，画面像坏卡）。实测同源对比：
            //     auto-safe     → 蓝粉对调（R-B=+13，G-R=-23，明显偏色）
            //     d3d11va       → 与软解逐像素一致
            //     d3d11va-copy  → 偏色
            //     no（软解）    → 基准
            //   可用环境变量 `CLEAN_TV_HWDEC` 覆盖（排查用）。
            format!("--hwdec={}", std::env::var("CLEAN_TV_HWDEC").unwrap_or_else(|_| "d3d11va".into())),
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
            // ★ 去掉 `--profile=fast`：它只把缩放器降成 bilinear，收益极小；
            //   实测它与 `--no-border` 同时出现时，多次出现「mpv 不建视频窗口」。
            //   判断是否真有画面已交给 `video_ready()` + 看门狗，不再靠猜。
        ];

        // ★★ `--really-quiet` 会把 mpv 的日志全部吞掉 → 设了
        //   `CLEAN_TV_MPV_LOG` 诊断钩子时日志文件是 0 字节，死因无从查。
        //   所以：诊断模式（设了 MPV_LOG）改用 `--msg-level=v=info`，
        //   保留 stdout/stderr 输出；正常运行仍用 `--really-quiet` 降开销。
        //   ★ 实际日志靠 `--log-file` 落盘（见下），msg-level 只决定写多少。
        if diag_mpv_log.is_empty() {
            args.push("--really-quiet".to_string());
        } else {
            args.push("--msg-level=v=info".to_string());
            args.push("--no-terminal".to_string());
        }

        // 嗅探流通常校验 Referer/Cookie，必须带上（对应 `_header_fields`）。
        if let Some(hf) = header_fields(headers) {
            args.push(format!("--http-header-fields={hf}"));
        }
        // 放宽 TLS 证书校验。
        //  ★ 2026-10-26 扩大范围：原判断只认 `iptv200.com`（789 的 CDN 证书无效），
        //    但实测**境外直播 CDN** 的证书链同样不完整，且 TLS1.3 会被源站拒绝
        //    （TVB 的 Akamai 边缘只接受 TLSv1.2 → `TLSV1_ALERT_PROTOCOL_VERSION`），
        //    证书链不完整 → 校验失败 → mpv 起播即退。
        //    判据改为「**嗅探来的流一律放宽**」：嗅探流来自第三方网页播放器，
        //    证书状况不可控；而本机源/官网源的**直连**线路证书正常，不受影响。
        //    （`--tls-verify=no` 只跳过证书链校验，不影响加密本身。）
        let is_sniffed = !headers.is_empty();
        if is_sniffed || url.contains("iptv200.com") {
            args.push("--tls-verify=no".to_string());
        }

        // ★ 诊断钩子：CLEAN_TV_MPV_LOG=<路径> 时把 mpv 日志落盘（release GUI
        //   下默认 null 句柄，mpv 死因无从排查；验收时设此变量抓真实错误）。
        //   ★★ 坑：mpv.exe 是 **GUI 子系统**程序，它的日志**不写 stderr**
        //   （实测 stderr 重定向得到 0 字节，而 `--log-file` 正常写出 15KB）。
        //   所以诊断模式必须用 `--log-file=<路径>`，不能靠 Stdio 重定向。
        //   ★ diag_mpv_log 已在上面（args 构造前）取过，这里直接复用。
        let stderr_target = Stdio::null();
        if !diag_mpv_log.is_empty() {
            // 路径里的 '=' 会被 mpv 当成参数分隔，用 `--log-file` 独立传参即可。
            args.push(format!("--log-file={}", diag_mpv_log));
            // GUI 子系统下 stdout 也是空句柄，但 stderr 重定向无害（保底）。
        }

        // ★ 剥离代理环境变量：mpv 会读 http_proxy/https_proxy，一旦环境里有代理
        //   （本机沙箱就是 127.0.0.1:12244），直播流请求全走代理 → 拿到错误页 →
        //   表现为「有声音没图像」或 12s 未出画面。直播必须直连源站。
        let mut mpv_env = std::env::vars_os()
            .filter(|(k, _)| {
                let key = k.to_string_lossy().to_ascii_lowercase();
                !matches!(
                    key.as_str(),
                    "http_proxy" | "https_proxy" | "all_proxy" | "ftp_proxy" | "no_proxy"
                )
            })
            .collect::<Vec<_>>();

        let child = Command::new(&self.mpv)
            .args(&args)
            .env_clear()
            .envs(mpv_env.drain(..))
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(stderr_target)
            .spawn()
            .map_err(|e| format!("mpv 启动失败: {e}"))?;

        let pid = child.id();
        *self.proc.lock().unwrap() = Some(child);
        *self.pipe.lock().unwrap() = Some(pipe.clone());
        self.parent_hwnd
            .store(hwnd, std::sync::atomic::Ordering::SeqCst);
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

    /// 一次播放会话的状态（非阻塞），供「是否真出画面」的看门狗轮询。
    pub fn poll(&self) -> PollState {
        let mut g = match self.proc.lock() {
            Ok(g) => g,
            Err(_) => return PollState::Taken,
        };
        match g.as_mut() {
            None => PollState::Taken,
            Some(p) => match p.try_wait() {
                Ok(Some(st)) => {
                    let code = st.code().unwrap_or(-1);
                    g.take();
                    PollState::Exited(code)
                }
                Ok(None) => PollState::Running,
                Err(_) => {
                    g.take();
                    PollState::Exited(-1)
                }
            },
        }
    }

    /// 修正 mpv 视频子窗口的尺寸。
    ///
    /// ★ 实测坑：主窗口处于最小化/未激活时，mpv 会把视频子窗口建成
    ///   `0×0 @ (-32000,-32000)`；之后窗口恢复它**不会自动变回来**，
    ///   于是「有声音没图像」。这里发现尺寸为 0 就按父窗口客户区强制拉齐
    ///   （mpv 收到 WM_SIZE 会重新配置 VO）。
    pub fn fix_video_geometry(&self) -> bool {
        #[cfg(windows)]
        {
            use std::sync::atomic::Ordering;
            use windows::core::BOOL;
            use windows::Win32::Foundation::{HWND, LPARAM, RECT};
            use windows::Win32::UI::WindowsAndMessaging::{
                EnumChildWindows, GetClassNameW, GetClientRect, GetWindowRect, SetWindowPos,
                SWP_NOACTIVATE, SWP_NOZORDER, SWP_SHOWWINDOW,
            };

            let parent = self.parent_hwnd.load(Ordering::SeqCst);
            if parent == 0 {
                return false;
            }
            struct Ctx {
                target: isize,
                pw: i32,
                ph: i32,
            }
            unsafe extern "system" fn cb(h: HWND, l: LPARAM) -> BOOL {
                let ctx = &mut *(l.0 as *mut Ctx);
                let mut buf = [0u16; 64];
                let n = GetClassNameW(h, &mut buf);
                if n > 0 && String::from_utf16_lossy(&buf[..n as usize]).eq_ignore_ascii_case("mpv") {
                    let mut r = RECT::default();
                    let empty = GetWindowRect(h, &mut r).is_err()
                        || r.right - r.left <= 0
                        || r.bottom - r.top <= 0;
                    if empty && ctx.pw > 0 && ctx.ph > 0 {
                        let _ = SetWindowPos(
                            h,
                            None,
                            0,
                            0,
                            ctx.pw,
                            ctx.ph,
                            SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW,
                        );
                        ctx.target = h.0 as isize;
                    }
                    return BOOL(0);
                }
                BOOL(1)
            }
            let mut ctx = Ctx { target: 0, pw: 0, ph: 0 };
            unsafe {
                let mut cr = RECT::default();
                let ph = HWND(parent as *mut std::ffi::c_void);
                if GetClientRect(ph, &mut cr).is_ok() {
                    ctx.pw = cr.right - cr.left;
                    ctx.ph = cr.bottom - cr.top;
                }
                let _ = EnumChildWindows(
                    Some(ph),
                    Some(cb),
                    LPARAM(&mut ctx as *mut Ctx as isize),
                );
            }
            ctx.target != 0
        }
        #[cfg(not(windows))]
        {
            false
        }
    }

    /// mpv 是否**已经真的出画面**。
    ///
    /// ★ 判据原理（2026-10-25 实测确认）：mpv 用 `--wid` 内嵌时，**只有在解出
    ///   第一帧视频之后**才会创建那个 `class="mpv"` 的视频子窗口。反过来，
    ///   子窗口存在 ⇔ 已经有画面了。这比"进程还活着"准确得多——实测出现过
    ///   mpv 存活 20 秒、只有声音、始终没有视频窗口的情况（用户看到的
    ///   「有声音没图像」）。音频是 AAC 独立帧，早就出声了。
    pub fn video_ready(&self) -> bool {
        #[cfg(windows)]
        {
            use std::sync::atomic::Ordering;
            use windows::core::BOOL;
            use windows::Win32::Foundation::{HWND, LPARAM};
            use windows::Win32::UI::WindowsAndMessaging::{
                EnumChildWindows, GetClassNameW, GetWindowRect, IsWindowVisible,
            };

            let parent = self.parent_hwnd.load(Ordering::SeqCst);
            if parent == 0 {
                return false;
            }
            struct Ctx {
                found: bool,
            }
            unsafe extern "system" fn cb(h: HWND, l: LPARAM) -> BOOL {
                let ctx = &mut *(l.0 as *mut Ctx);
                let mut buf = [0u16; 64];
                let n = GetClassNameW(h, &mut buf);
                if n > 0 {
                    let name = String::from_utf16_lossy(&buf[..n as usize]);
                    if name.eq_ignore_ascii_case("mpv") {
                        // ★ 必须要求有真实尺寸：实测主窗口最小化时 mpv 会创建一个
                        //   `0x0 @ (-32000,-32000)` 的窗口——窗口存在但没有画面，
                        //   只看"存在"会误判为已出画。
                        let mut r = windows::Win32::Foundation::RECT::default();
                        if GetWindowRect(h, &mut r).is_ok()
                            && r.right - r.left > 0
                            && r.bottom - r.top > 0
                            && IsWindowVisible(h).as_bool()
                        {
                            ctx.found = true;
                            return BOOL(0); // 找到即停止枚举
                        }
                    }
                }
                BOOL(1)
            }
            let mut ctx = Ctx { found: false };
            unsafe {
                let _ = EnumChildWindows(
                    Some(HWND(parent as *mut std::ffi::c_void)),
                    Some(cb),
                    LPARAM(&mut ctx as *mut Ctx as isize),
                );
            }
            ctx.found
        }
        #[cfg(not(windows))]
        {
            false
        }
    }

    /// 当前 mpv 是否仍在运行（不阻塞，tyr_wait 语义）。
    ///
    /// ★ 为什么需要它：验收/诊断时从外部数 mpv 进程并不可靠（沙箱里 Python 的
    ///   subprocess/Toolhelp 枚举都拿不到），由 app 自己用 child.try_wait 判断
    ///   最准：返回 None 即仍存活。
    pub fn is_alive(&self) -> bool {
        let mut g = match self.proc.lock() {
            Ok(g) => g,
            Err(_) => return false,
        };
        match g.as_mut() {
            Some(p) => matches!(p.try_wait(), Ok(None)),
            None => false,
        }
    }

    /// 阻塞等待 mpv 退出，返回 (gen, exit_code)；用于看门狗线程。
    ///
    /// ★ v2 重写（原实现持锁阻塞，两个致命后果）：
    ///   1) `proc` 锁被 `p.wait()` 长期占住 → 同进程内 `stop()`（换台）拿不到锁，
    ///      要等 mpv 自己退出才生效，表现为「换台没反应」；
    ///   2) `is_alive()` 也拿不到锁 → 起播自证心跳线程永远不出结果（实测日志缺失）。
    ///   改为 **300ms 轮询 `try_wait`**，每次只短暂持锁；被 `stop()` 取走 child
    ///   时返回 None（调用方据此判断「会话已被接管，不要重试」）。
    ///
    /// 返回的 gen 若与当前 gen 不一致，说明是被主动切换/停止，调用方应忽略。
    pub fn wait_exit(&self) -> Option<(u64, i32)> {
        let gen = self.gen();
        loop {
            {
                let mut g = self.proc.lock().ok()?;
                let p = g.as_mut()?; // 已被 stop() 取走 → 会话结束
                match p.try_wait() {
                    Ok(Some(st)) => {
                        let code = st.code().unwrap_or(-1);
                        g.take();
                        return Some((gen, code));
                    }
                    Ok(None) => {} // 仍在跑
                    Err(_) => {
                        g.take();
                        return Some((gen, -1));
                    }
                }
            }
            std::thread::sleep(std::time::Duration::from_millis(300));
        }
    }
}

/// 视频承载窗口：mpv 的 `--wid` 父窗口。
///
/// ## 为什么需要独立窗口（关键坑）
///
/// 早前版本把 mpv 直接嵌进 **Tauri 主窗口**（`--wid=<主窗口HWND>`）。但 Tauri
/// 主窗口里铺满了一层 WebView2（`WRY_WEBVIEW` → `Chrome_RenderWidgetHostHWND`，
/// 全尺寸 1100x700），而 mpv 的视频子窗口是**同级兄弟**，Z 序在 WebView2
/// **下面** → 画面被整块盖住，只剩音频（用户实测「有声音没图像」）。
///
/// 老 desktop 版（tkinter）没这问题：它把 mpv 嵌在 `video_host`（一个 Tk Frame）
/// 这个**独立容器**里，容器不被任何网页层覆盖。
///
/// 本模块的解法与之一致：**自己建一个独立顶层窗口**（`WS_POPUP`，无边框黑色，
/// `WS_EX_TOOLWINDOW` 不上任务栏、`WS_EX_NOACTIVATE` 不抢焦点）当 mpv 的父窗口，
/// 再按前端上报的播放区屏幕坐标把它摆到对应位置并置顶（`HWND_TOP`）。
pub struct VideoHost {
    /// 承载窗口 HWND（0 = 未创建）。
    hwnd: std::sync::atomic::AtomicU64,
    /// 主窗口 HWND（跟随其移动/显示状态）。
    owner: std::sync::atomic::AtomicU64,
}

impl VideoHost {
    /// 新建（此时还没建窗，等 `ensure` 时按需创建）。
    pub fn new() -> Self {
        Self {
            hwnd: std::sync::atomic::AtomicU64::new(0),
            owner: std::sync::atomic::AtomicU64::new(0),
        }
    }

    /// 承载窗口 HWND（0 = 尚未创建）。
    pub fn hwnd(&self) -> u64 {
        self.hwnd.load(std::sync::atomic::Ordering::SeqCst)
    }

    /// 确保承载窗口存在；返回其 HWND（0 = 创建失败）。
    ///
    /// `owner_hwnd` 是主窗口 HWND（承载窗成为它的 owned window + 跟随重定位）。
    pub fn ensure(&self, owner_hwnd: u64) -> u64 {
        use std::sync::atomic::Ordering;
        if owner_hwnd != 0 {
            self.owner.store(owner_hwnd, Ordering::SeqCst);
        }
        let cur = self.hwnd.load(Ordering::SeqCst);
        if cur != 0 {
            // 已存在：补设一次 owner（主窗口 HWND 可能晚于建窗才拿到）。
            if owner_hwnd != 0 {
                self.apply_owner(owner_hwnd);
            }
            return cur;
        }
        #[cfg(windows)]
        {
            use windows::core::PCWSTR;
            use windows::Win32::Foundation::HINSTANCE;
            use windows::Win32::UI::WindowsAndMessaging::{
                CreateWindowExW, RegisterClassW, CS_HREDRAW, CS_VREDRAW,
                WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_POPUP, WS_VISIBLE,
            };

            const CLASS_NAME: &str = "CleanTVVideoHost";
            unsafe {
                // 注册窗口类（已注册则忽略错误）。
                let class_name: Vec<u16> = CLASS_NAME.encode_utf16().chain(Some(0)).collect();
                let wndclass = windows::Win32::UI::WindowsAndMessaging::WNDCLASSW {
                    style: CS_HREDRAW | CS_VREDRAW,
                    // windows crate 要求 `unsafe extern "system" fn`，DefWindowProcW
                    // 是 `unsafe fn`（Rust ABI），要包一层转换 calling convention
                    //（见 video_host_wnd_proc）。
                    lpfnWndProc: Some(video_host_wnd_proc),
                    hInstance: HINSTANCE(std::process::id() as *mut std::ffi::c_void),
                    lpszClassName: PCWSTR(class_name.as_ptr()),
                    ..Default::default()
                };
                let _ = RegisterClassW(&wndclass);

                let h = CreateWindowExW(
                    WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                    PCWSTR(class_name.as_ptr()),
                    PCWSTR::null(),
                    WS_POPUP | WS_VISIBLE,
                    // 先放到屏幕外，收到前端上报的播放区坐标后再摆正。
                    -32000,
                    -32000,
                    640,
                    360,
                    // ★ hWndParent 传主窗口 HWND → 承载窗成为主窗口的
                    //   **owned window**（不是 child，见 WS_POPUP）。
                    //   Windows 保证 owned 窗口**永远压在 owner 之上**，
                    //   且与前台线程组无关 —— 这是让 mpv 视频不被
                    //   WebView2 渲染子窗盖住的关键（详见 place() 注释）。
                    if owner_hwnd != 0 {
                        Some(windows::Win32::Foundation::HWND(
                            owner_hwnd as *mut std::ffi::c_void,
                        ))
                    } else {
                        None
                    },
                    None,
                    Some(HINSTANCE(std::process::id() as *mut std::ffi::c_void)),
                    None,
                );
                match h {
                    Ok(h) => {
                        let v = h.0 as u64;
                        self.hwnd.store(v, Ordering::SeqCst);
                        eprintln!("[player] 视频承载窗口已创建 hwnd={v}");
                        v
                    }
                    Err(e) => {
                        eprintln!("[player] 视频承载窗口创建失败: {e}");
                        0
                    }
                }
            }
        }
        #[cfg(not(windows))]
        {
            let _ = owner_hwnd;
            0
        }
    }

    /// 把承载窗口摆到指定屏幕坐标并置顶。
    ///
    /// * `x, y` —— 屏幕坐标（物理像素）。
    /// * `w, h` —— 尺寸。
    ///
    /// ★ Z 序要点（2026-10-26 实测）：
    ///   承载窗是**独立顶层窗口**（`WS_POPUP`），与 Tauri 主窗口平级。
    ///   Windows 有一条硬规则：**前台线程组的窗口永远压过后台线程组的非 topmost 窗口**，
    ///   所以只靠 `SetWindowPos(HWND_TOP)` 并不足以让承载窗盖住主窗口里的
    ///   WebView2 渲染子窗（`Chrome_RenderWidgetHostHWND`）—— 实测承载窗被压到
    ///   主窗口之下 → 视频整块被盖 → 「有声音没图像」。
    ///   真正的解法是让承载窗成为主窗口的 **owned window**
    ///   （`GWLP_HWNDPARENT` = 主窗口 HWND，见 `ensure`），
    ///   Windows 保证 owned 窗口恒在 owner 之上，与前台状态无关。
    ///   这里再补一次 `HWND_TOP` 是为了让 owned 窗口在同级里排最前。
    pub fn place(&self, x: i32, y: i32, w: i32, h: i32) {
        use std::sync::atomic::Ordering;
        let hwnd = self.hwnd.load(Ordering::SeqCst);
        if hwnd == 0 {
            return;
        }
        #[cfg(windows)]
        {
            use windows::Win32::Foundation::HWND;
            use windows::Win32::UI::WindowsAndMessaging::{
                SetWindowPos, HWND_TOP, SWP_NOACTIVATE, SWP_SHOWWINDOW,
            };
            unsafe {
                let _ = SetWindowPos(
                    HWND(hwnd as *mut std::ffi::c_void),
                    Some(HWND_TOP),
                    x,
                    y,
                    w.max(16),
                    h.max(16),
                    SWP_NOACTIVATE | SWP_SHOWWINDOW,
                );
            }
        }
        #[cfg(not(windows))]
        {
            let _ = (x, y, w, h);
        }
    }

    /// 按「视口 CSS 坐标」把承载窗口摆到主窗口客户区内的指定位置。
    ///
    /// * `owner_hwnd` —— 主窗口 HWND（客户区原点用）。
    /// * `x, y` —— 目标区域相对**视口左上角**的 CSS 偏移。
    /// * `w, h` —— 目标区域的 CSS 尺寸。
    /// * `vw, vh` —— 视口的 CSS 尺寸（`window.innerWidth/innerHeight`）。
    ///
    /// 返回换算后的屏幕坐标 `(x, y, w, h)`；失败返回 None。
    ///
    /// ★ 为什么要在这里换算（而不是让前端乘 devicePixelRatio）：
    ///   JS 的 CSS 像素与 Win32 窗口坐标是两套空间（125% 缩放下差一个 dpr，
    ///   记忆里的 DPI 探针陷阱：跨进程读矩形会被虚拟化）。这里用
    ///   **客户区尺寸 ÷ 视口 CSS 尺寸** 求出真实缩放比，再叠加客户区屏幕原点，
    ///   单一真源，不受前端缩放假设影响。
    pub fn place_over_client(
        &self,
        owner_hwnd: u64,
        x: i32,
        y: i32,
        w: i32,
        h: i32,
        vw: i32,
        vh: i32,
    ) -> Option<(i32, i32, i32, i32)> {
        if owner_hwnd == 0 || vw <= 0 || vh <= 0 {
            return None;
        }
        #[cfg(windows)]
        {
            use windows::Win32::Foundation::{HWND, POINT};
            // ★ ClientToScreen 在 windows 0.62 里位于 Graphics::Gdi（不在 UI::WindowsAndMessaging），
            //   GetClientRect 才是 UI::WindowsAndMessaging。别记混。
            use windows::Win32::Graphics::Gdi::ClientToScreen;
            use windows::Win32::UI::WindowsAndMessaging::GetClientRect;
            let owner = HWND(owner_hwnd as *mut std::ffi::c_void);
            unsafe {
                let mut cr = windows::Win32::Foundation::RECT::default();
                if GetClientRect(owner, &mut cr).is_err() {
                    return None;
                }
                let cw = cr.right - cr.left;
                let ch = cr.bottom - cr.top;
                if cw <= 0 || ch <= 0 {
                    return None;
                }
                // 客户区左上角的屏幕坐标。
                let mut pt = POINT { x: 0, y: 0 };
                // ★ ClientToScreen 返回 BOOL（不是 Result），要用 as_bool() 判成败。
                if !ClientToScreen(owner, &mut pt).as_bool() {
                    return None;
                }
                // 缩放比：客户区实际像素 ÷ 视口 CSS 像素。
                let sx = cw as f64 / vw as f64;
                let sy = ch as f64 / vh as f64;
                let px = pt.x + (x as f64 * sx).round() as i32;
                let py = pt.y + (y as f64 * sy).round() as i32;
                let pw = (w as f64 * sx).round() as i32;
                let ph = (h as f64 * sy).round() as i32;
                self.place(px, py, pw, ph);
                Some((px, py, pw, ph))
            }
        }
        #[cfg(not(windows))]
        {
            let _ = (x, y, w, h, vw, vh);
            None
        }
    }

    /// 把承载窗口设为主窗口的 **owned window**（`GWLP_HWNDPARENT`）。
    ///
    /// ★ 这是「视频不被 WebView2 盖住」的**根因修复**（2026-10-26 实测）：
    ///   承载窗与 Tauri 主窗口平级（都是顶层），而 Windows 规定
    ///   **前台线程组的窗口压过后台线程组的非 topmost 窗口** ——
    ///   主窗口是本组的 active window，所以承载窗无论怎么 `HWND_TOP`
    ///   都会被 WebView2 渲染子窗（`Chrome_RenderWidgetHostHWND`）盖住
    ///   → 「有声音没图像」。
    ///   改成 owned window 后，Windows 保证 owned 恒在 owner 之上。
    fn apply_owner(&self, owner_hwnd: u64) {
        use std::sync::atomic::Ordering;
        let hwnd = self.hwnd.load(Ordering::SeqCst);
        if hwnd == 0 || owner_hwnd == 0 {
            return;
        }
        #[cfg(windows)]
        {
            use windows::Win32::Foundation::HWND;
            use windows::Win32::UI::WindowsAndMessaging::{
                SetWindowLongPtrW, GWLP_HWNDPARENT,
            };
            unsafe {
                let _ = SetWindowLongPtrW(
                    HWND(hwnd as *mut std::ffi::c_void),
                    GWLP_HWNDPARENT,
                    owner_hwnd as isize,
                );
            }
        }
    }

    /// 把承载窗口重新提到最前（不改位置/尺寸）。
    ///
    /// ★ 为什么需要（2026-10-26 实测）：
    ///   承载窗是主窗口的 **owned window**，Windows 保证它在 owner 之上；
    ///   但 owned 窗口在 **owner 被最小化/恢复、owner 失去焦点再获得焦点**
    ///   时可能被系统重新排到 owner 之下 → WebView2 渲染子窗整块盖住视频
    ///   → 「有声音没图像」复发。
    ///   所以主窗口的 `Focused` / `Moved` / `Resized` 事件都要调一次本方法，
    ///   把视频层重新顶上来。
    pub fn raise(&self) {
        use std::sync::atomic::Ordering;
        let hwnd = self.hwnd.load(Ordering::SeqCst);
        if hwnd == 0 {
            return;
        }
        #[cfg(windows)]
        {
            use windows::Win32::Foundation::HWND;
            use windows::Win32::UI::WindowsAndMessaging::{
                SetWindowPos, HWND_TOP, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW,
            };
            unsafe {
                let _ = SetWindowPos(
                    HWND(hwnd as *mut std::ffi::c_void),
                    Some(HWND_TOP),
                    0,
                    0,
                    0,
                    0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
                );
            }
        }
    }

    /// 隐藏承载窗口（停止播放时用，避免残留黑框）。
    pub fn hide(&self) {
        use std::sync::atomic::Ordering;
        let hwnd = self.hwnd.load(Ordering::SeqCst);
        if hwnd == 0 {
            return;
        }
        #[cfg(windows)]
        {
            use windows::Win32::Foundation::HWND;
            use windows::Win32::UI::WindowsAndMessaging::{SetWindowPos, SWP_HIDEWINDOW, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE};
            unsafe {
                let _ = SetWindowPos(
                    HWND(hwnd as *mut std::ffi::c_void),
                    None,
                    0,
                    0,
                    0,
                    0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_HIDEWINDOW,
                );
            }
        }
    }

    /// 显示承载窗口（起播前调用，避免闪烁黑框在错误位置）。
    pub fn show(&self) {
        use std::sync::atomic::Ordering;
        let hwnd = self.hwnd.load(Ordering::SeqCst);
        if hwnd == 0 {
            return;
        }
        #[cfg(windows)]
        {
            use windows::Win32::Foundation::HWND;
            use windows::Win32::UI::WindowsAndMessaging::{SetWindowPos, SWP_NOACTIVATE, SWP_SHOWWINDOW, SWP_NOMOVE, SWP_NOSIZE};
            unsafe {
                let _ = SetWindowPos(
                    HWND(hwnd as *mut std::ffi::c_void),
                    None,
                    0,
                    0,
                    0,
                    0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
                );
            }
        }
    }
}

/// 视频承载窗口的窗口过程：转发给默认窗口过程。
///
/// windows crate 要求 `unsafe extern "system" fn`（C ABI），而
/// `DefWindowProcW` 是 `unsafe fn`（Rust ABI），二者不兼容 —— 必须用
/// 本函数做一层 calling convention 转换。
unsafe extern "system" fn video_host_wnd_proc(
    hwnd: windows::Win32::Foundation::HWND,
    msg: u32,
    wparam: windows::Win32::Foundation::WPARAM,
    lparam: windows::Win32::Foundation::LPARAM,
) -> windows::Win32::Foundation::LRESULT {
    unsafe { windows::Win32::UI::WindowsAndMessaging::DefWindowProcW(hwnd, msg, wparam, lparam) }
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
        // ★ 2026-10-25 实测把 balanced 的 cache-secs 从 4 下调到 2
        //   （cache-secs=4 会把起播硬推后 4 秒：mpv 要等缓存填满才开始播）。
        assert_eq!(balanced.1, 2);
        assert_eq!(balanced.2, "32M");
        // low 档 cache 1s
        let low = BUFFER_TIERS.iter().find(|(t, ..)| *t == "low").unwrap();
        assert_eq!(low.1, 1);
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
        // ★ 头名保留**原始大小写**（mpv 的 --http-header-fields 大小写不敏感，
        //   保持原样便于日志核对）—— 所以断言用 "Referer:" 而不是 "referer:"。
        assert!(h.contains("Referer:"), "got: {h}");
        assert!(h.contains("Cookie:"), "got: {h}");
        assert!(h.contains("Origin:"), "got: {h}");
        assert!(!h.contains("drop"));
        // 白名单外的头被过滤
        assert!(!h.contains("X-Other"));
    }
}