# Tauri 外壳 + Sidecar 生命周期 (#1) 设计规格

**日期**: 2026-07-07
**子项目**: #1 Tauri 外壳 + Sidecar 生命周期
**路径**: 地基优先（方案 A）的第三步
**状态**: 已批准，待实现计划

---

## 1. 目标与非目标

### 1.1 目标

1. **桌面应用壳**：Tauri v2 项目，双击即启动 `parsing-core serve` sidecar，退出一键终止
2. **Sidecar 嵌入**：打包 `.venv/bin/python` + `parsing-core` 依赖进 `.app` bundle，用户零 Python 环境依赖
3. **进程硬隔离**：Rust 主进程与 Python sidecar 互不阻塞，UI 永不假死
4. **PID 心跳守护**：
   - Rust → Python：每 3 秒 HTTP GET `/health`，连续 3 次失败判定 sidecar 死亡，自动重启
   - Python → Rust：收到启动时传入的父进程 PID，守护线程每 3 秒 `os.kill(parent_pid, 0)` 检测父进程心跳，父进程丢失立即退出
5. **僵尸进程防范**：Rust 退出时 SIGTERM → Python，再等 3s 超时 SIGKILL
6. **状态面板**：小窗口显示"服务运行中 · http://127.0.0.1:8000" + 手动启动/停止按钮 + 日志区域
7. **端口自适应**：默认 8000，占用则试 8001
8. **系统托盘**：最小化到托盘，右键菜单"显示""退出"

### 1.2 非目标（本子项目不做）

- WebUI 渲染（#5 做完整前端替换状态面板）
- LiteLLM 三档算力路由（#4）
- 原生文件对话框 / 拖拽（#5）
- Windows 安装包 / macOS 签名公证（CI/CD 阶段）
- macOS App Sandbox / Hardened Runtime（发布阶段）

---

## 2. 架构

### 2.1 进程拓扑

```
┌──────────────────────────────────────────────────────┐
│  macOS 桌面                                          │
│                                                      │
│  ┌──────────────────────────────────────────┐       │
│  │ parsing-core.app                         │       │
│  │                                          │       │
│  │  ┌──────────────┐  ┌──────────────────┐ │       │
│  │  │ Tauri Rust    │  │  WebView (HTML)  │ │       │
│  │  │ 主进程        │  │  - 状态面板      │ │       │
│  │  │ - 窗口管理    │  │  - 启停按钮      │ │       │
│  │  │ - 托盘        │  │  - 日志区域      │ │       │
│  │  │ - 心跳守护    │  │                  │ │       │
│  │  └──────┬───────┘  └──────────────────┘ │       │
│  │         │ Sidecar                        │       │
│  │         ▼                                │       │
│  │  ┌──────────────────────────────────┐   │       │
│  │  │ Python parsing-core serve        │   │       │
│  │  │ - uvicorn 127.0.0.1:8000        │   │       │
│  │  │ - /health                        │   │       │
│  │  │ - PID 守护线程                    │   │       │
│  │  └──────────────────────────────────┘   │       │
│  └──────────────────────────────────────────┘       │
└──────────────────────────────────────────────────────┘
```

### 2.2 模块结构

```
parsing-core-app/
├── src-tauri/
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   ├── build.rs
│   ├── capabilities/main.json
│   ├── icons/
│   ├── src/
│   │   ├── main.rs          # 窗口创建、托盘、生命周期入口
│   │   ├── sidecar.rs       # Sidecar 管理：启动/停止/心跳/重启
│   │   └── state.rs         # 全局状态：port/running/logs
│   └── binaries/            # Tauri sidecar 放置目录
│       └── (build 时软链 .venv/bin/python3)
├── src/
│   └── index.html           # 状态面板 UI
└── package.json             # (Tauri CLI + 空脚本，无 React 依赖)
```

### 2.3 调用关系

```
main.rs
  ├─ 创建窗口（加载 src/index.html）
  ├─ 创建系统托盘
  ├─ sidecar.start()
  │    └─ tauri::process::Command::new_sidecar("python3")
  │         .args(["-m", "parsing_core.serve", "--port", port, "--parent-pid", std::process::id()])
  │         .spawn()
  └─ health_loop:
       └─ 每 3s: reqwest::get("http://127.0.0.1:{port}/health")
            ├─ 200 → healthy_count=0, 更新 UI "运行中"
            └─ 失败 → healthy_count++, 若 ≥3 则重启 sidecar

  └─ 退出时: sidecar.kill() → SIGTERM → sleep(3s) → SIGKILL
```

### 2.4 Python 侧修订

`serve.py` 新增 `--parent-pid` 参数：

```python
def _parent_watchdog(parent_pid: int):
    import os
    import signal
    while True:
        try:
            os.kill(parent_pid, 0)
        except OSError:
            os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(3)
```

在 `main()` 中若 `--parent-pid` 传入则启动 `threading.Thread(target=_parent_watchdog, daemon=True).start()`

---

## 3. 数据模型

### 3.1 Rust 侧状态

```rust
use std::sync::Mutex;

struct AppState {
    port: u16,              // 当前监听端口
    sidecar_pid: Option<u32>,
    running: bool,
    logs: Vec<String>,      // 最近 100 条日志
    health_failures: u8,
}
```

通过 `tauri::State<Mutex<AppState>>` 注入命令。

### 3.2 Tauri Commands（Rust → 前端 IPC）

| 命令 | 方向 | 说明 |
|---|---|---|
| `get_status` | Rust → JS | 返回 `{port, running, logs}` |
| `start_service` | JS → Rust | 手动启动 sidecar |
| `stop_service` | JS → Rust | 手动停止 sidecar |
| `subscribe_logs` | Rust → JS | 用 `tauri::Event` 推送日志行 |

---

## 4. 核心算法

### 4.1 Sidecar 启动

```rust
fn start_sidecar(state: &AppState, port: u16, parent_pid: u32) -> Result<()> {
    let (mut rx, child) = Command::new_sidecar("python3")?
        .args(["-m", "parsing_core.serve", "--port", &port.to_string(),
               "--parent-pid", &parent_pid.to_string()])
        .spawn()?;
    // 异步读 stdout → push 到 state.logs → emit 事件给前端
    tokio::spawn(async move {
        let mut reader = BufReader::new(child.stdout);
        let mut line = String::new();
        while reader.read_line(&mut line).await? > 0 {
            // push to logs, emit event
        }
    });
    Ok(())
}
```

### 4.2 健康检查

```rust
async fn health_loop(state: Arc<Mutex<AppState>>) {
    loop {
        tokio::time::sleep(Duration::from_secs(3)).await;
        let port = state.lock().port;
        match reqwest::get(format!("http://127.0.0.1:{}/health", port)).await {
            Ok(r) if r.status() == 200 => state.lock().health_failures = 0,
            _ => {
                state.lock().health_failures += 1;
                if state.lock().health_failures >= 3 {
                    // 重启 sidecar
                }
            }
        }
    }
}
```

### 4.3 端口自适应

```rust
fn find_free_port(start: u16) -> u16 {
    for port in start..start + 100 {
        if TcpListener::bind(("127.0.0.1", port)).is_ok() {
            return port;
        }
    }
    start
}
```

---

## 5. UI（src/index.html）

```html
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>parsing-core</title>
<style>
  body { font-family: -apple-system, sans-serif; padding: 24px; }
  .status { font-size: 18px; }
  .running { color: #22c55e; }
  .stopped { color: #ef4444; }
  #logs { font-family: monospace; font-size: 12px; max-height: 300px;
          overflow-y: auto; background: #f6f6f6; padding: 12px; border-radius: 6px; }
  button { margin: 8px 8px 8px 0; padding: 6px 16px; }
</style></head>
<body>
  <h2>parsing-core</h2>
  <p class="status" id="status">启动中...</p>
  <button id="btn-start" onclick="start()">启动服务</button>
  <button id="btn-stop" onclick="stop()">停止服务</button>
  <h3>日志</h3>
  <div id="logs"></div>
  <script>
    const { invoke } = window.__TAURI__.core;
    async function refresh() {
      const s = await invoke("get_status");
      document.getElementById("status").textContent =
        s.running ? `✅ 运行中 · http://127.0.0.1:${s.port}` : "⏸ 已停止";
      document.getElementById("status").className = "status " + (s.running ? "running" : "stopped");
      document.getElementById("logs").textContent = s.logs.join("\n");
    }
    function start() { invoke("start_service"); refresh(); }
    function stop() { invoke("stop_service"); refresh(); }
    setInterval(refresh, 1000);
    window.__TAURI__.event.listen("log", (e) => {
      document.getElementById("logs").textContent += "\n" + e.payload;
    });
  </script>
</body></html>
```

---

## 6. Tauri Conf 关键配置

```json
{
  "productName": "parsing-core",
  "version": "0.1.0",
  "identifier": "com.parsingcore.app",
  "build": {
    "frontendDist": "../src",
    "devUrl": "http://localhost:1420"
  },
  "app": {
    "windows": [{"title": "parsing-core", "width": 520, "height": 480}],
    "trayIcon": {"iconPath": "icons/icon.png"}
  },
  "bundle": {
    "active": true,
    "icon": ["icons/icon.png"],
    "externalBin": ["binaries/python3"]
  }
}
```

---

## 7. 测试策略

### 7.1 Rust 单元测试
- `sidecar::find_free_port(8000)` 返回可用端口
- `sidecar::start` + `sidecar::kill` 拉起/终止子进程

### 7.2 集成测试
- `tauri::test::assert_webview_contains` 验证状态面板显示"运行中"
- Mock /health 返回 200/500 验证重启逻辑

### 7.3 手动冒烟
```bash
cd parsing-core-app
npx tauri dev          # 窗口弹出，状态面板显示"运行中"
curl http://127.0.0.1:8000/health  # {"status":"ok"}
killall parsing-core   # 关闭应用后 curl 失败（僵尸进程清理验证）
```

---

## 8. 依赖

```toml
# Cargo.toml
[dependencies]
tauri = { version = "2", features = ["tray-icon"] }
tauri-plugin-shell = "2"
reqwest = { version = "0.12", features = ["json"] }
tokio = { version = "1", features = ["full"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
```

---

## 9. 验收标准

1. ✅ `npx tauri dev` 启动桌面窗口，状态面板显示服务状态
2. ✅ Sidecar 自动拉起 `parsing-core serve`，`/health` 返回 200
3. ✅ 关闭窗口或退出应用，Python 进程终止（无僵尸进程残留）
4. ✅ 手动停止按钮 kill Python，再点启动重新拉起
5. ✅ 连续 3 次 /health 失败后自动重启 sidecar
6. ✅ Python 检测父进程死亡后自我退出
7. ✅ 托盘图标显示，右键菜单有"显示""退出"
8. ✅ `cargo test` 通过
9. ✅ `npx tauri build` 生成 .app bundle

---

## 10. 时间估算

| 阶段 | 工作量 |
|---|---|
| Tauri v2 脚手架 + Cargo | 0.5 天 |
| Sidecar 管理模块（start/stop/health/restart） | 1 天 |
| PID 心跳守护（Rust + Python 两侧） | 0.5 天 |
| 状态面板 UI（HTML + Tauri commands） | 0.5 天 |
| 系统托盘 + 生命周期 | 0.5 天 |
| 测试 + 冒烟 | 0.5 天 |
| **合计** | **3.5 天** |

---

**下一步**：调用 `writing-plans` 技能产出实现计划。