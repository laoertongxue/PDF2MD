# PDF2MD —— 智能多模态文档解析桌面应用

将非结构化商业报表（Excel / PDF / Word / 图片）批量转换为**穿插式 Markdown**：原文还原 + AI 解读（含 Mermaid 图表），本地方案全程数据不出网。

## 架构总览

```
┌──────────────────────────────────────────────────┐
│  Tauri v2 桌面壳 (Rust)                          │
│  • Sidecar 拉起 Python 服务                       │
│  • 双向 PID 心跳守护                              │
│  • WebView 渲染 React 前端                        │
├──────────────────────────────────────────────────┤
│  FastAPI 服务 (Python)                            │
│  • REST API 批量提交                              │
│  • WebSocket 实时状态推送                         │
│  • asyncio 并发池调度                             │
├──────────────────────────────────────────────────┤
│  解析内核 (MarkItDown + LLM)                      │
│  • 多格式文件 → Markdown                          │
│  • 按节切分 + 节级 LLM 穿插解读                   │
│  • Mermaid 图表即时生成                           │
│  • 三档算力路由: 本地 Ollama / 私有 vLLM / 公有云 │
├──────────────────────────────────────────────────┤
│  SQLite (WAL 模式)                                │
│  • 文件级 sha256 缓存                              │
│  • 节级 sha256 缓存                                │
│  • 崩溃可恢复 (resume 续跑)                       │
└──────────────────────────────────────────────────┘
```

## 快速开始

### 前置条件

- Python 3.11+
- Node.js 20+
- Rust (用于 Tauri 编译)
- [Ollama](https://ollama.com) (可选，本地 LLM)

### 1. 安装 Python 依赖

```bash
git clone https://github.com/your-username/PDF2MD.git
cd PDF2MD
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,serve,llm]"
```

### 2. 启动后端服务

```bash
# 默认 stub 模式（无 LLM，用占位输出快速验证）
python -m parsing_core.serve --port 8000

# 使用本地 Ollama
export PARSING_CORE_LOCAL_MODEL=ollama/llama3.2-vision:latest
python -m parsing_core.serve --port 8000

# 使用 OpenAI
export OPENAI_API_KEY=sk-xxx
export PARSING_CORE_PUBLIC_MODEL=openai/gpt-4o
python -m parsing_core.serve --port 8000
```

### 3. 启动桌面应用

```bash
cd parsing-core-app
npm install
npx tauri dev
```

或仅启动 Web 前端：

```bash
cd parsing-core-app
npm install
npm run dev  # → http://localhost:1420
```

### 4. 命令行单文件解析

```bash
python -m parsing_core.cli parse /path/to/report.xlsx
# 产物: ~/.local/share/parsing-core/{task_id}/merged.md
```

## 项目结构

```
PDF2MD/
├── src/parsing_core/          # Python 后端
│   ├── cli.py                 # CLI 入口 (5 子命令)
│   ├── orchestrator.py        # 解析编排器
│   ├── models/                # 数据模型 (Task/Section/AIArtifact)
│   ├── utils/                 # 工具 (sha256/副本/重试)
│   ├── storage/               # SQLite 持久化
│   ├── parser/                # MarkItDown + 分节器 + 图片抽取
│   ├── llm/                   # LLM 客户端 (Stub/LiteLLM)
│   └── serving/               # FastAPI 服务 (REST + WS + Scheduler)
├── parsing-core-app/          # Tauri 桌面应用
│   ├── src/                   # React + TypeScript 前端
│   │   ├── components/        # UI 组件
│   │   ├── api/               # REST/WS 客户端
│   │   └── store/             # Zustand 状态管理
│   ├── src-tauri/             # Rust 后端
│   │   ├── src/
│   │   │   ├── main.rs        # 入口 (窗口/托盘/生命周期)
│   │   │   ├── sidecar.rs     # Sidecar 管理
│   │   │   └── state.rs       # 状态定义
│   │   └── tauri.conf.json    # Tauri 配置
│   └── package.json
├── tests/                     # Python 测试 (154 passed)
└── docs/superpowers/          # 设计文档
    ├── specs/                 # 规格说明 (5 个子系统)
    └── plans/                 # 实现计划
```

## API 端点

| 路由 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 健康检查 |
| `/api/batches` | POST | 创建批次 `{files, concurrency}` |
| `/api/batches` | GET | 列出批次 `?status=RUNNING` |
| `/api/batches/{id}` | GET | 批次详情 |
| `/api/batches/{id}` | DELETE | 取消批次 |
| `/api/tasks` | POST | 单文件入口 |
| `/api/tasks/{id}` | GET | 任务状态 |
| `/api/tasks/{id}` | DELETE | 清理任务 |
| `/api/tasks/{id}/merged` | GET | 下载 merged.md |
| `/ws/batch/{id}?since=N` | WS | 实时事件流 |

## 算力路由

通过环境变量配置三档路由：

| Tier | 默认模型 | 配置变量 |
|---|---|---|
| `local` | ollama/llama3.2-vision | `PARSING_CORE_LOCAL_MODEL`, `OLLAMA_HOST` |
| `private` | openai/gpt-4o-mini | `PARSING_CORE_PRIVATE_MODEL`, `PARSING_CORE_PRIVATE_BASE_URL`, `PARSING_CORE_PRIVATE_API_KEY` |
| `public` | openai/gpt-4o | `PARSING_CORE_PUBLIC_MODEL`, `OPENAI_API_KEY` |
| `stub` | — (占位) | 无需配置 |

## 技术栈

| 层 | 技术 |
|---|---|
| 桌面壳 | Tauri v2 (Rust) + WebView |
| 前端 | Vite 6 + React 19 + TypeScript + Tailwind CSS + Zustand |
| 后端 | Python 3.11 + FastAPI + uvicorn |
| 解析 | MarkItDown + 自研分节器 |
| LLM | LiteLLM (OpenAI / Anthropic / Ollama / vLLM) |
| 存储 | SQLite (WAL) |
| 测试 | pytest (154) + tsc + ruff |

## 开发

```bash
# 运行全部测试
pytest -v

# 代码检查
ruff check src tests

# 前端编译检查
cd parsing-core-app && npx tsc --noEmit

# 构建桌面应用
cd parsing-core-app && npx tauri build
```

## License

MIT
