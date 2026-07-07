# Tauri 外壳 + Sidecar 生命周期 (#1) 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 用 Tauri v2 把 Python `parsing-core serve` 包装为桌面应用，sidecar 自动拉起/心跳守护/僵尸清理，窗口显示状态面板。

**架构：** Rust 主进程通过 `tauri::process::Command::new_sidecar` 拉起 `.venv/bin/python` 运行 `parsing-core serve`；双向 PID 心跳（Rust→Python HTTP /health，Python→Rust os.kill 检测父进程）；前端纯 HTML 面板。

**技术栈：** Tauri v2 (Rust) + tokio + reqwest + 原生 HTML/CSS/JS（无 React/Vite）

**配套规格：** `docs/superpowers/specs/2026-07-07-tauri-shell-design.md`

---

## 文件结构

| 路径 | 职责 |
|---|---|
| `parsing-core-app/package.json` | Tauri CLI 占位 |
| `parsing-core-app/src/index.html` | 状态面板 UI |
| `parsing-core-app/src-tauri/Cargo.toml` | Rust 依赖 |
| `parsing-core-app/src-tauri/tauri.conf.json` | Tauri 配置 + sidecar 声明 |
| `parsing-core-app/src-tauri/build.rs` | tauri::build |
| `parsing-core-app/src-tauri/capabilities/main.json` | 权限声明 |
| `parsing-core-app/src-tauri/src/main.rs` | 入口：窗口/托盘/生命周期 |
| `parsing-core-app/src-tauri/src/sidecar.rs` | Sidecar 管理模块 |
| `parsing-core-app/src-tauri/src/state.rs` | AppState 定义 |
| `parsing-core-app/src-tauri/binaries/python3` | 构建时 symlink → venv python |
| `src/parsing_core/serving/serve.py` | 加 `--parent-pid` watchdog（修改） |

---

## 任务 0：Tauri v2 脚手架

**文件：**
- 创建：`parsing-core-app/package.json`、`parsing-core-app/src-tauri/Cargo.toml`、`parsing-core-app/src-tauri/tauri.conf.json`、`parsing-core-app/src-tauri/build.rs`、`parsing-core-app/src-tauri/capabilities/main.json`

- [ ] **步骤 1：创建 Tauri 项目骨架 + 安装 CLI**

```bash
cd /Users/laoer/Documents/PDF2MD
mkdir -p parsing-core-app/src parsing-core-app/src-tauri/src parsing-core-app/src-tauri/binaries parsing-core-app/src-tauri/icons parsing-core-app/src-tauri/capabilities
npm init -y --prefix parsing-core-app
# 安装 Tauri CLI（全局或 local）
cd parsing-core-app && npm install @tauri-apps/cli@latest
```

在 `parsing-core-app/package.json` 中加 scripts：
```json
{
  "name": "parsing-core-app",
  "private": true,
  "scripts": {
    "tauri": "tauri"
  }
}
```

- [ ] **步骤 2：创建 Cargo.toml**

```toml
[package]
name = "parsing-core-app"
version = "0.1.0"
edition = "2021"

[dependencies]
tauri = { version = "2", features = ["tray-icon"] }
tauri-plugin-shell = "2"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
tokio = { version = "1", features = ["full"] }
reqwest = { version = "0.12", features = ["json"] }

[build-dependencies]
tauri-build = { version = "2", features = [] }
```

- [ ] **步骤 3：创建 tauri.conf.json**

```json
{
  "productName": "parsing-core",
  "version": "0.1.0",
  "identifier": "com.parsingcore.app",
  "build": {
    "frontendDist": "../src",
    "devUrl": "http://localhost:1420",
    "beforeDevCommand": "",
    "beforeBuildCommand": ""
  },
  "app": {
    "windows": [
      {
        "title": "parsing-core",
        "width": 520,
        "height": 480,
        "resizable": true
      }
    ],
    "trayIcon": {
      "iconPath": "icons/icon.png",
      "iconAsTemplate": true
    }
  },
  "bundle": {
    "active": true,
    "icon": ["icons/icon.png"],
    "externalBin": ["binaries/python3"]
  },
  "plugins": {
    "shell": {
      "open": true,
      "scope": [
        { "name": "python3", "cmd": "binaries/python3",
          "args": [{"validator": ".*"}]
        }
      ]
    }
  }
}
```

- [ ] **步骤 4：创建 build.rs**

```rust
fn main() {
    tauri_build::build()
}
```

- [ ] **步骤 5：创建 capabilities/main.json**

```json
{
  "identifier": "default",
  "description": "default capability",
  "windows": ["main"],
  "permissions": [
    "core:event:default",
    "core:window:default",
    "core:tray:default",
    "shell:allow-open"
  ]
}
```

- [ ] **步骤 6：创建 sidecar symlink**

```bash
ln -sf /Users/laoer/Documents/PDF2MD/.venv/bin/python3 parsing-core-app/src-tauri/binaries/python3
```
（构建时 Tauri 会自动解析 symlink 并 bundle 真实文件）

- [ ] **步骤 7：冒烟编译**

```bash
cd parsing-core-app && npx tauri build --debug 2>&1 | tail -5
```
预期：编译通过（即使无 main.rs 业务逻辑，骨架应可 compile）

- [ ] **步骤 8：Commit**

```bash
git add parsing-core-app
git commit -m "chore(tauri): scaffold Tauri v2 project with sidecar config"
```

---

## 任务 1：Python 侧 --parent-pid watchdog

**文件：**
- 修改：`src/parsing_core/serving/serve.py`

- [ ] **步骤 1：在 serve.py 添加 watchdog 逻辑**

在 `main()` 函数中，argparse 加 `--parent-pid` 参数，存在则启动守护线程：

```python
    parser.add_argument("--parent-pid", type=int, default=None)

    # 在 uvicorn.run 之前（但 args 解析之后）：
    if args.parent_pid is not None:
        import threading
        def _watchdog():
            import os, signal, time as _t
            while True:
                try:
                    os.kill(args.parent_pid, 0)
                except OSError:
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                _t.sleep(3)
        threading.Thread(target=_watchdog, daemon=True, name="parent-watchdog").start()
```

- [ ] **步骤 2：验证**

```bash
.venv/bin/python -m parsing_core.serve --port 8001 --parent-pid 99999 &
sleep 1
# 父进程不存在（99999），服务应 3 秒内自我退出
curl -s http://127.0.0.1:8001/health || echo "服务已退出"  # 预期无法连接
```

- [ ] **步骤 3：回归测试**

```bash
.venv/bin/python -m pytest -v 2>&1 | tail -3
```
预期：137 passed 不变

- [ ] **步骤 4：Commit**

```bash
git add src/parsing_core/serving/serve.py
git commit -m "feat(serve): add --parent-pid watchdog for parent process monitoring"
```

---

## 任务 2：Rust 侧 state.rs

**文件：**
- 创建：`parsing-core-app/src-tauri/src/state.rs`

```rust
use serde::Serialize;
use std::sync::Mutex;

#[derive(Debug, Default)]
pub struct AppState {
    pub port: u16,
    pub running: bool,
    pub logs: Vec<String>,
    pub health_failures: u8,
    pub sidecar_child: Option<tauri::process::CommandChild>,
}

#[derive(Serialize)]
pub struct StatusPayload {
    pub port: u16,
    pub running: bool,
    pub logs: Vec<String>,
}
```

- [ ] **步骤：创建并 commit**

```bash
cd /Users/laoer/Documents/PDF2MD/parsing-core-app
mkdir -p src-tauri/src
# 写入 state.rs
git add src-tauri/src/state.rs
git commit -m "feat(tauri): add AppState with serializable status"
```

---

## 任务 3：Rust 侧 sidecar.rs（核心）

**文件：**
- 创建：`parsing-core-app/src-tauri/src/sidecar.rs`

```rust
use crate::state::AppState;
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use tauri::Emitter;
use tauri::Manager;

const HEALTH_INTERVAL_SECS: u64 = 3;
const MAX_HEALTH_FAILURES: u8 = 3;

pub fn find_free_port(start: u16) -> u16 {
    for port in start..start + 100 {
        if TcpListener::bind(("127.0.0.1", port)).is_ok() {
            return port;
        }
    }
    start
}

pub async fn start_sidecar(app: &tauri::AppHandle, state: Arc<Mutex<AppState>>) -> Result<(), String> {
    let port = {
        let mut s = state.lock().map_err(|e| e.to_string())?;
        if s.running {
            return Err("already running".into());
        }
        s.running = true;
        s.port
    };

    let parent_pid = std::process::id();
    let sidecar_command = app.shell()
        .sidecar("python3")
        .map_err(|e| e.to_string())?
        .args([
            "-m", "parsing_core.serve",
            "--port", &port.to_string(),
            "--parent-pid", &parent_pid.to_string(),
            "--host", "127.0.0.1",
        ]);

    let (mut rx, child) = sidecar_command.spawn().map_err(|e| e.to_string())?;

    {
        let mut s = state.lock().map_err(|e| e.to_string())?;
        s.sidecar_child = Some(child);
        s.logs.push(format!("[sidecar] started on port {}", port));
    }
    let app_clone = app.clone();
    let state_clone = state.clone();

    tokio::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                tauri::process::CommandEvent::Stdout(line) => {
                    let line_str = String::from_utf8_lossy(&line);
                    let mut s = state_clone.lock().unwrap();
                    s.logs.push(line_str.to_string());
                    if s.logs.len() > 100 {
                        s.logs.remove(0);
                    }
                    drop(s);
                    app_clone.emit("log", line_str.to_string()).ok();
                }
                tauri::process::CommandEvent::Stderr(line) => {
                    let line_str = String::from_utf8_lossy(&line);
                    let mut s = state_clone.lock().unwrap();
                    s.logs.push(format!("[stderr] {}", line_str));
                    drop(s);
                }
                tauri::process::CommandEvent::Terminated(_) => {
                    let mut s = state_clone.lock().unwrap();
                    s.running = false;
                    s.sidecar_child = None;
                    s.logs.push("[sidecar] terminated".into());
                    drop(s);
                    break;
                }
                _ => {}
            }
        }
    });

    Ok(())
}

pub async fn stop_sidecar(state: Arc<Mutex<AppState>>) -> Result<(), String> {
    let mut s = state.lock().map_err(|e| e.to_string())?;
    if let Some(child) = s.sidecar_child.take() {
        child.kill().map_err(|e| e.to_string())?;
        s.logs.push("[sidecar] stopped".into());
    }
    s.running = false;
    Ok(())
}

pub async fn health_loop(app: tauri::AppHandle, state: Arc<Mutex<AppState>>) {
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(HEALTH_INTERVAL_SECS)).await;
        let port = state.lock().unwrap().port;
        let url = format!("http://127.0.0.1:{}/health", port);
        match reqwest::get(&url).await {
            Ok(r) if r.status().is_success() => {
                state.lock().unwrap().health_failures = 0;
            }
            _ => {
                let mut s = state.lock().unwrap();
                s.health_failures += 1;
                if s.health_failures >= MAX_HEALTH_FAILURES {
                    s.logs.push("[health] 3 failures, restarting sidecar...".into());
                    drop(s);
                    let _ = stop_sidecar(state.clone()).await;
                    tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                    let _ = start_sidecar(&app, state.clone()).await;
                }
            }
        }
    }
}
```

- [ ] **步骤：创建 rust 模块并验证编译**

```bash
cd /Users/laoer/Documents/PDF2MD/parsing-core-app
# 写入 sidecar.rs
echo 'pub mod sidecar; pub mod state;' > src-tauri/src/lib.rs
git add src-tauri/src/
git commit -m "feat(tauri): add sidecar manager with start/stop/health/restart"
```

---

## 任务 4：Rust 侧 main.rs + 前端 UI

**文件：**
- 创建：`parsing-core-app/src-tauri/src/main.rs`（含 Tauri commands）
- 创建：`parsing-core-app/src/index.html`（状态面板）

- [ ] **步骤 1：创建 main.rs**

```rust
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod sidecar;
mod state;

use state::{AppState, StatusPayload};
use std::sync::{Arc, Mutex};
use tauri::{Manager, State};

type SharedState = Arc<Mutex<AppState>>;

#[tauri::command]
fn get_status(state: State<SharedState>) -> StatusPayload {
    let s = state.lock().unwrap();
    StatusPayload {
        port: s.port,
        running: s.running,
        logs: s.logs.clone(),
    }
}

#[tauri::command]
async fn start_service(app: tauri::AppHandle, state: State<SharedState>) -> Result<String, String> {
    sidecar::start_sidecar(&app, state.inner().clone()).await?;
    Ok("started".into())
}

#[tauri::command]
async fn stop_service(state: State<SharedState>) -> Result<String, String> {
    sidecar::stop_sidecar(state.inner().clone()).await?;
    Ok("stopped".into())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let port = sidecar::find_free_port(8000);
            let s = SharedState::new(Mutex::new(AppState {
                port,
                running: false,
                logs: vec![format!("[init] port {} selected", port)],
                health_failures: 0,
                sidecar_child: None,
            }));
            app.manage(s.clone());

            let app_handle = app.handle().clone();
            let state_clone = s.clone();
            tokio::spawn(async move {
                let _ = sidecar::start_sidecar(&app_handle, state_clone).await;
            });

            let app_h2 = app.handle().clone();
            let state_h = s.clone();
            tokio::spawn(async move {
                sidecar::health_loop(app_h2, state_h).await;
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_status, start_service, stop_service])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
```

- [ ] **步骤 2：创建 index.html**

与规格 § 完全相同创建 `parsing-core-app/src/index.html`。

- [ ] **步骤 3：编译验证**

```bash
cd parsing-core-app && npx tauri build --debug 2>&1 | tail -10
```
预期：编译通过

- [ ] **步骤 4：Commit**

```bash
cd /Users/laoer/Documents/PDF2MD
git add parsing-core-app/src-tauri/src/main.rs parsing-core-app/src/index.html
git commit -m "feat(tauri): add main entry, commands, and status panel UI"
```

---

## 任务 5：托盘与窗口生命周期

**文件：**
- 修改：`parsing-core-app/src-tauri/src/main.rs`

在 `main.rs` 的 `setup` 闭包中添加托盘逻辑：

```rust
    let app_handle = app.handle().clone();
    let _tray = tauri::tray::TrayIconBuilder::new()
        .icon(app.default_window_icon().unwrap().clone())
        .menu(
            &tauri::menu::MenuBuilder::new(app)
                .item(&tauri::menu::MenuItemBuilder::with_id("show", "显示").build(app)?)
                .item(&tauri::menu::MenuItemBuilder::with_id("quit", "退出").build(app)?)
                .build()?,
        )
        .on_menu_event(move |app, event| {
            match event.id().as_ref() {
                "show" => {
                    if let Some(w) = app.get_webview_window("main") {
                        let _ = w.show();
                        let _ = w.set_focus();
                    }
                }
                "quit" => {
                    app.exit(0);
                }
                _ => {}
            }
        })
        .build(app)?;
```

窗口关闭事件（隐藏到托盘而非退出）：

在 `tauri::Builder` 链中添加：
```rust
        .on_window_event(|w, e| {
            if let tauri::WindowEvent::CloseRequested { .. } = e {
                let _ = w.hide();
            }
        })
```

- [ ] **步骤：commit**

```bash
git add -A
git commit -m "feat(tauri): add system tray and hide-on-close lifecycle"
```

---

## 任务 6：集成测试与最终收尾

- [ ] **步骤 1：build debug 版本**

```bash
cd parsing-core-app && npx tauri build --debug
```
预期：生成 `parsing-core-app/src-tauri/target/debug/bundle/macos/parsing-core.app`

- [ ] **步骤 2：手动冒烟**

```bash
# 启动 .app
open parsing-core-app/src-tauri/target/debug/bundle/macos/parsing-core.app
sleep 3
curl -s http://127.0.0.1:8000/health
# 关闭 .app
killall parsing-core 2>/dev/null
sleep 4
# 验证无僵尸 Python
ps aux | grep "parsing_core" | grep -v grep
```
预期：health 返回 `{"status":"ok"}`；kill 后无残留 `python -m parsing_core.serve` 进程

- [ ] **步骤 3：Py 回归测试**

```bash
.venv/bin/python -m pytest -v 2>&1 | tail -3
```
预期：137 passed

- [ ] **步骤 4：cargo test**

```bash
cd parsing-core-app && cargo test 2>&1 | tail -5
```

- [ ] **步骤 5：Commit**

```bash
git add -A
git commit -m "chore(tauri): final integration and smoke verification"
```

---

## 自检

**1. 规格覆盖度对照**
- §1.1 目标 1 桌面应用壳 → 任务 0 scaffold ✓
- §1.1 目标 2 Sidecar 嵌入 → 任务 0 binaries symlink + tauri.conf externalBin ✓
- §1.1 目标 3 进程硬隔离 → 任务 3 sidecar.rs spawn ✓
- §1.1 目标 4 PID 心跳守护 → 任务 1 Python watchdog + 任务 3 health_loop ✓
- §1.1 目标 5 僵尸进程防范 → 任务 3 stop_sidecar kill + 任务 1 Python 自我终止 ✓
- §1.1 目标 6 状态面板 → 任务 4 index.html + commands ✓
- §1.1 目标 7 端口自适应 → 任务 3 find_free_port ✓
- §1.1 目标 8 系统托盘 → 任务 5 tray ✓

**遗漏**：无。

**2. 占位符扫描**：无 TODO/待定 ✓

**3. 类型一致性**：AppState 在 state.rs ↔ sidecar.rs ↔ main.rs 三处一致 ✓

---

## 执行交接

计划已完成并保存到 `docs/superpowers/plans/2026-07-07-tauri-shell.md`。两种执行方式：

**1. 子代理驱动（推荐）** - 每个任务调度一个新的子代理，任务间进行审查，快速迭代

**2. 内联执行** - 在当前会话中使用 executing-plans 执行任务，批量执行并设有检查点

选哪种方式？