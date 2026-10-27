//! 播放会话统一抽象层（Session Manager）。
//!
//! 根治「两源两套架构」的病根：本机源走 mpv 独立进程、官网源走 WebView2 顶层
//! 直播窗，两条路径各自持有「起播 + 回收」逻辑，切源时手动「关 A 再开 B」，
//! 任何一步漏掉就残留窗口 / 双声道 / 几何错位。
//!
//! 本模块把这两种后端收敛成**同一个可替换的 `Backend` 枚举**，一次播放 =
//! 一个 `PlaySession`，切源 = `SessionManager::replace(new)` **原子替换**。
//! 旧会话的回收（同步隐藏 → 异步销毁）由会话管理器**统一**执行，任何模块
//! 都不再私自关窗 / 停进程。
//!
//! ## 三条核心不变式（inherent invariants）
//!
//! 1. **唯一所有权**（ownership）：mpv 进程句柄、WebView2 直播窗、嗅探窗、
//!    几何信息，全部收进 `PlaySession`。切换时只有一个入口能回收旧会话。
//! 2. **原子替换**（atomic replace）：`replace()` 先做「同步隐藏」微秒级切断
//!    画面 + 音频，再把真正的销毁丢后台线程，最后才登记新会话——中间不存在
//!    「旧窗还没关、新源已经开始」的窗口期。
//! 3. **代次闸门**（generation gate）：每次 `replace`/`clear` 代次 +1，所有
//!    异步回调（mpv 退出监控、出画判定、换线回填）只认自己的代次，不匹配即
//!    丢弃，杜绝「旧回调冲掉新会话」。
//!
//! ## 与底层模块的分工
//!
//! - `player::Player` 仍是 mpv 的进程管理 + IPC + 出画判定（不变）。
//! - `official_embed` 仍负责 WebView2 直播窗的**建窗/净化注入/出画判定**（不变）。
//! - 本模块**只做「谁在播、怎么统一回收、几何怎么统一同步」**这一层收敛，
//!   不改变任何已验证的起播/出画细节。

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

/// 播放器后端：把「mpv 独立进程」和「WebView2 直播窗」统一成一种可替换实现。
///
/// ★ 这是统一架构的核心：前端和 `play_channel` 都不再关心「这到底是个进程
///   还是个窗口」，只认 `Backend` 这一个抽象。响应更快 / 操作更流畅的关键
///   就在于「切源 = 换一个 Backend」，而不是「关一套 + 开另一套」。
#[derive(Debug)]
pub enum Backend {
    /// 本机源 / 官网源直连：mpv 独立进程（`.flv`/`.m3u8` 直接硬解）。
    Mpv { parent_hwnd: u64 },
    /// 官网源网页线路：WebView2 顶层直播窗（页面自带 `<video>` 直接播）。
    Webview { hwnd: u64 },
}

impl Backend {
    /// 后端类型名（诊断日志用）。
    pub fn kind(&self) -> &'static str {
        match self {
            Backend::Mpv { .. } => "mpv",
            Backend::Webview { .. } => "webview",
        }
    }

    /// 后端当前可见的底层窗口 HWND（用于几何同步）；mpv 的 parent 是承载窗、
    /// webview 是直播窗自身。0 表示尚未创建。
    pub fn hwnd(&self) -> u64 {
        match self {
            Backend::Mpv { parent_hwnd } => *parent_hwnd,
            Backend::Webview { hwnd } => *hwnd,
        }
    }
}

/// 播放区几何（屏幕物理像素坐标）——**单一真源**。
///
/// ★ 几何不再由前端分别上报给 mpv 和直播窗两处，而是收敛为 `SessionManager`
///   持有的唯一一份。全屏 / 切源 / 窗口缩放时，任何「会动几何」的操作都走
///   同一个入口，一次 `sync_geometry` 同步所有属于当前会话的底层窗口。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StageRect {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

impl StageRect {
    pub fn new(x: i32, y: i32, w: i32, h: i32) -> Self {
        Self { x, y, w, h }
    }
}

/// 一次播放会话：mpv 进程 + WebView2 直播窗 + 几何，归一个主人管。
///
/// 目前底层资源仍由 `player::Player`（mpv 进程）和 `official_embed`（直播窗
/// 静态句柄）持有；`PlaySession` 记录的是「会话级」的元信息：代次、后端类型、
/// 最新几何。真正的回收动作由 `SessionManager::replace` 驱动，调用底层模块的
/// `stop()` / `close_live_window()` 完成——避免重复造一套句柄管理，而是把
/// **回收的调用点**从散落的 3 处收敛到 1 处。
#[derive(Debug)]
pub struct PlaySession {
    /// 会话代次（会话管理器的全局单调递增，非每会话独立）。
    pub gen: u64,
    /// 后端类型与底层窗口（用于几何同步 + 诊断）。
    pub backend: Backend,
    /// 最近一次几何（屏幕物理像素）。
    pub stage: Option<StageRect>,
}

/// 会话管理器：唯一有权「换源 / 停播 / 同步几何」的地方。
///
/// 全局单例通过 `SessionManager::global()` 获取（内部 `Mutex`），供各命令 handler
/// 与后台回调共享。
#[derive(Default)]
pub struct SessionManager {
    /// 当前会话（None = 什么都没在播）。
    cur: Mutex<Option<PlaySession>>,
    /// 全局代次（跨会话单调递增）。
    gen: AtomicU64,
}

impl SessionManager {
    /// 全局单例。
    pub fn global() -> &'static SessionManager {
        static S: SessionManager = SessionManager {
            cur: Mutex::new(None),
            gen: AtomicU64::new(0),
        };
        &S
    }

    /// 当前会话代次（0 = 尚无会话）。异步回调用它判断「我是不是已经被换掉了」。
    #[allow(dead_code)]
    pub fn gen(&self) -> u64 {
        self.gen.load(Ordering::SeqCst)
    }

    /// 当前会话（只读快照，克隆出 `Option<PlaySession>`）。
    #[allow(dead_code)]
    pub fn current(&self) -> Option<PlaySession> {
        self.cur
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .as_ref()
            .map(|s| PlaySession {
                gen: s.gen,
                backend: Backend::from_parts(s.backend.kind(), s.backend.hwnd()),
                stage: s.stage,
            })
    }

    /// 当前后端类型（无会话返回 None）。
    #[allow(dead_code)]
    pub fn backend_kind(&self) -> Option<&'static str> {
        self.cur
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .as_ref()
            .map(|s| s.backend.kind())
    }

    /// **原子替换会话**（切源 / 起播的唯一入口）。
    ///
    /// 流程严格按「同步隐藏旧会话 → 异步销毁 → 登记新会话」：
    /// 1. 代次 +1（作废旧会话的一切异步回调）。
    /// 2. 同步回收旧会话：mpv `stop()` 立即 `kill`，直播窗先 `SW_HIDE` 立即
    ///    切断画面 + 音频；`child.wait()` / `win.close()` 已由底层模块丢后台线程。
    /// 3. 登记新会话（携带新代次）。
    ///
    /// ★ 关键：先回收、后登记，中间不存在「新源已开始但旧窗还占着屏幕」的窗口。
    pub fn replace(&self, backend: Backend, stage: Option<StageRect>) -> u64 {
        // 1. 代次 +1：这一刻起，旧会话的所有异步回调被判「作废」。
        let gen = self.gen.fetch_add(1, Ordering::SeqCst) + 1;

        // 2. 同步回收旧会话（调用底层模块的统一回收入口，不再散落各处）。
        self.recycle_previous();

        // 3. 登记新会话。
        let session = PlaySession {
            gen,
            backend,
            stage,
        };
        *self.cur.lock().unwrap_or_else(|p| p.into_inner()) = Some(session);
        gen
    }

    /// 清空会话（停止播放），无新会话接入。
    pub fn clear(&self) {
        self.gen.fetch_add(1, Ordering::SeqCst);
        self.recycle_previous();
        *self.cur.lock().unwrap_or_else(|p| p.into_inner()) = None;
    }

    /// 更新当前会话的后端（后端类型在「解析 url → 选 Backend」后才确定，
    /// 或换线时 parent_hwnd 变化）。不影响代次。
    #[allow(dead_code)]
    pub fn update_backend(&self, backend: Backend) {
        if let Some(s) = self.cur.lock().unwrap_or_else(|p| p.into_inner()).as_mut() {
            s.backend = backend;
        }
    }

    /// 更新当前会话的几何（并**立即**同步到底层窗口）。
    ///
    /// ★ 这是几何收敛的关键：不再由 `report_stage_rect` 分别调
    ///   `host.place_over_client` + `official_embed::sync_live_geometry` 两处，
    ///   而是这里一次性同步「当前会话在播的那个后端」对应的窗口。
    ///   本机源 → 移动 mpv 承载窗；官网源 → 移动 WebView2 直播窗。
    pub fn sync_geometry(&self, rect: StageRect) {
        let (gen, kind) = {
            let mut g = self.cur.lock().unwrap_or_else(|p| p.into_inner());
            match g.as_mut() {
                Some(s) => {
                    s.stage = Some(rect);
                    (s.gen, s.backend.kind())
                }
                None => return,
            }
        };
        // 几何同步不能持有会话锁去调底层（底层可能再回调进来），先释放。
        let _ = gen;
        super::apply_geometry(kind, rect);
    }

    /// 会话是否仍在播放（是否有当前会话）。
    #[allow(dead_code)]
    pub fn is_active(&self) -> bool {
        self.cur
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .is_some()
    }

    /// 给「真正出画」的回填接口：确认某条线路出画后，把后端类型/窗口登记进来，
    /// 并记录几何（若有）。返回该会话代次，供异步回填方核对。
    ///
    /// ★ 因为本机源是后台抓线、官网源查缓存后建窗，`replace` 在起播那一刻
    ///   可能还不知道最终的 parent_hwnd / 直播窗 hwnd；这里让真正出画的
    ///   路径把最终句柄补进来，几何立即应用到它。
    pub fn settle_backend(&self, backend: Backend, stage: Option<StageRect>, settle_gen: u64) -> bool {
        let mut g = self.cur.lock().unwrap_or_else(|p| p.into_inner());
        let cur = match g.as_mut() {
            Some(s) => s,
            None => return false,
        };
        // 代次不匹配 = 会话已被换掉，本次回填作废（旧回调别冲新会话）。
        if cur.gen != settle_gen {
            return false;
        }
        cur.backend = backend;
        if stage.is_some() {
            cur.stage = stage;
        }
        let kind = cur.backend.kind();
        let rect = cur.stage;
        drop(g);
        if let Some(r) = rect {
            super::apply_geometry(kind, r);
        }
        true
    }

    /// 统一回收「上一个会话」占用的底层资源。
    ///
    /// ★ 这是「残留 / 双声」根治点：以前 `play_channel` 里 `state.player.stop()`
    ///   和 `official_embed::close_live_window()` 各调各的，漏一个就残留；
    ///   现在无论上个会话是本机源还是官网源，都在**这一处**把两样都回收干净。
    fn recycle_previous(&self) {
        // mpv：kill 进程（wait 已由 player.rs 丢后台线程，不卡主线程）。
        super::stop_mpv();
        // 官网源直播窗：先同步 SW_HIDE 切断画面+音频，再后台 close（微秒级）。
        super::close_live();
    }
}

/// 让 `Backend` 从 `(kind, hwnd)` 重建（`current()` 的只读快照用，避免暴露
/// 底层可变句柄）。
impl Backend {
    fn from_parts(kind: &'static str, hwnd: u64) -> Backend {
        if kind == "webview" {
            Backend::Webview { hwnd }
        } else {
            Backend::Mpv { parent_hwnd: hwnd }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stage_rect_basic() {
        let r = StageRect::new(1, 2, 3, 4);
        assert_eq!((r.x, r.y, r.w, r.h), (1, 2, 3, 4));
    }

    #[test]
    fn backend_kind() {
        assert_eq!(Backend::Mpv { parent_hwnd: 5 }.kind(), "mpv");
        assert_eq!(Backend::Webview { hwnd: 6 }.kind(), "webview");
        assert_eq!(Backend::Mpv { parent_hwnd: 5 }.hwnd(), 5);
        assert_eq!(Backend::Webview { hwnd: 6 }.hwnd(), 6);
    }

    #[test]
    fn backend_from_parts_roundtrip() {
        let b = Backend::from_parts("webview", 7);
        assert_eq!(b.kind(), "webview");
        assert_eq!(b.hwnd(), 7);
        let b = Backend::from_parts("mpv", 8);
        assert_eq!(b.kind(), "mpv");
        assert_eq!(b.hwnd(), 8);
    }
}