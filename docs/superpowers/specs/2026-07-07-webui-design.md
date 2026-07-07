# WebUI (#5) 设计规格

**日期**: 2026-07-07
**子项目**: #5 WebUI
**路径**: 地基优先（方案 A）的第五步
**状态**: 已批准，待实现计划

---

## 1. 目标与非目标

### 1.1 目标

替换 Tauri 状态面板的纯 HTML 为完整 React/TS 前端，提供批量文档解析的操作界面和结果查看。

1. **批量提交 UI**：拖拽/文件选择 → 一键提交 batch → 实时 WS 状态推送
2. **任务仪表盘**：历史 batch 列表 + 每 batch 内 task 状态卡片 + 进度条
3. **文档查看器**：穿插原文/AI 解读渲染 + Mermaid 图实时预览 + 虚拟滚动（>5 万字不卡）
4. **流式体验**：WS `LLM_TOKEN` 事件打字机效果渲染 AI 解读
5. **Tauri 集成**：`npx tauri build` 产出完整 .app，前端替换原 `index.html`

### 1.2 非目标

- 算力路由配置面板（#4 用环境变量，不改 UI）
- 用户认证/登录
- 多语言 i18n
- 响应式移动端布局（桌面端优先）

---

## 2. 架构

### 2.1 前端进程拓扑

```
┌─────────────────────────────────────────────────────┐
│  Tauri WebView                                       │
│                                                      │
│  ┌─────────────────────────────────────────────┐    │
│  │  React App (Vite + TypeScript)              │    │
│  │                                              │    │
│  │  ┌──────────┐  ┌──────────┐  ┌───────────┐ │    │
│  │  │ 批量提交  │  │ 仪表盘   │  │ 文档查看器 │ │    │
│  │  │ BatchSubmit│  │ Dashboard│  │ DocViewer │ │    │
│  │  └────┬─────┘  └────┬─────┘  └─────┬─────┘ │    │
│  │       │             │              │       │    │
│  │       └──────┬──────┴──────────────┘       │    │
│  │             │                              │    │
│  │      ┌──────▼──────┐                       │    │
│  │      │ Zustand Store│                      │    │
│  │      │ (全局状态)   │                      │    │
│  │      └──────┬──────┘                       │    │
│  │             │                              │    │
│  │      ┌──────▼──────┐                       │    │
│  │      │  API Client  │──────────────────────│─── │
│  │      │  REST + WS   │                      │    │
│  │      └──────────────┘                      │    │
│  └─────────────────────────────────────────────┘    │
│                     │ REST/WS                        │
│                     ▼ 127.0.0.1:8000                │
│              ┌──────────────┐                       │
│              │ parsing-core │                       │
│              │ serve         │                       │
│              └──────────────┘                       │
└─────────────────────────────────────────────────────┘
```

### 2.2 项目结构

```
parsing-core-app/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── tailwind.config.ts
├── index.html                    # Vite entry (替换原 src/index.html)
├── src/
│   ├── main.tsx                  # React 入口
│   ├── App.tsx                   # 路由 + 布局
│   ├── index.css                 # Tailwind + 全局样式
│   ├── api/
│   │   ├── client.ts             # REST fetch 封装
│   │   ├── ws.ts                 # WebSocket 封装（自动重连）
│   │   └── types.ts              # API 响应类型
│   ├── store/
│   │   └── useStore.ts           # Zustand store（batches/tasks/ws）
│   ├── components/
│   │   ├── Layout.tsx            # 顶部导航 + 侧边栏
│   │   ├── BatchSubmit.tsx       # 拖拽提交区域
│   │   ├── Dashboard.tsx         # batch 列表卡片
│   │   ├── TaskCard.tsx          # 单个 task 状态卡片
│   │   ├── DocViewer.tsx         # 文档查看器（穿插视图）
│   │   ├── MermaidBlock.tsx      # Mermaid 渲染块
│   │   └── VirtualDoc.tsx        # 虚拟滚动容器
│   └── lib/
│       └── utils.ts              # 格式化/debounce 工具
└── dist/                         # Vite build 输出
```

### 2.3 路由

| 路由 | 页面 | 说明 |
|---|---|---|
| `/` | Dashboard | 默认首页，历史 batch 列表 |
| `/submit` | BatchSubmit | 新建 batch |
| `/batch/:id` | BatchDetail | 某 batch 下所有 task |
| `/doc/:taskId` | DocViewer | 查看某 task 的 merged.md |

---

## 3. API 客户端

### 3.1 REST (`api/client.ts`)

```typescript
const BASE = "http://127.0.0.1:8000";

export async function createBatch(files: string[], concurrency = 4) {
  const res = await fetch(`${BASE}/api/batches`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ files, concurrency }),
  });
  return res.json() as Promise<BatchResponse>;
}

export async function getBatch(batchId: string) {
  const res = await fetch(`${BASE}/api/batches/${batchId}`);
  return res.json() as Promise<BatchStatus>;
}

export async function getTask(taskId: string) {
  const res = await fetch(`${BASE}/api/tasks/${taskId}`);
  return res.json() as Promise<TaskStatus>;
}

export async function cancelBatch(batchId: string) {
  await fetch(`${BASE}/api/batches/${batchId}`, { method: "DELETE" });
}

export async function getMergedMd(taskId: string) {
  const res = await fetch(`${BASE}/api/tasks/${taskId}/merged`);
  return res.text();
}
```

### 3.2 WebSocket (`api/ws.ts`)

```typescript
export function connectBatchWs(batchId: string, onEvent: (e: WsEvent) => void) {
  const ws = new WebSocket(`ws://127.0.0.1:8000/ws/batch/${batchId}`);
  ws.onmessage = (msg) => {
    onEvent(JSON.parse(msg.data) as WsEvent);
  };
  ws.onclose = () => {
    // 自动重连 with ?since= 逻辑
  };
  return () => ws.close();
}
```

### 3.3 类型 (`api/types.ts`)

从 #3 的 Pydantic 模型派生：

```typescript
export interface BatchResponse { batch_id: string; task_ids: string[]; accepted: number; rejected: number; }
export interface BatchStatus { batch_id: string; status: string; total_tasks: number; completed_tasks: number; tasks: TaskItem[]; }
export interface TaskItem { task_id: string; status: string; file_path: string; }
export interface TaskStatus { task_id: string; batch_id: string; status: string; sections: number; completed: number; error_msg?: string; }
export interface WsEvent { seq: number; batch_id: string; task_id?: string; event: string; payload: Record<string, unknown>; ts: number; }
```

---

## 4. 状态管理 (Zustand)

```typescript
interface AppState {
  // 数据
  batches: BatchStatus[];
  tasks: Record<string, TaskStatus>;
  mergedDocs: Record<string, string>;
  // WS 连接
  activeWs: Map<string, WebSocket>;
  // UI
  selectedBatch: string | null;
  polling: boolean;
  // 操作
  submitBatch: (files: string[], concurrency: number) => Promise<void>;
  loadBatch: (id: string) => Promise<void>;
  loadMerged: (taskId: string) => Promise<void>;
  connectWs: (batchId: string) => void;
  disconnectWs: (batchId: string) => void;
}
```

---

## 5. 文档查看器（DocViewer 核心）

### 5.1 渲染管线

```
merged.md 文本
  └─ 按 "---" 分隔节
       └─ 每节解析：
            ├─ ## 第 N 节：{title} → 节标题
            ├─ 原文段 → react-markdown 渲染
            └─ ### ▸ AI 解读 → 
                 ├─ 解读文本 → react-markdown
                 └─ ```mermaid ... ``` → MermaidBlock 即时渲染
```

### 5.2 虚拟滚动

`@tanstack/react-virtual` + `useVirtualizer`，每节一个 DOM row。

### 5.3 打字机效果

WS `LLM_TOKEN` 事件累积 buffer → 50ms 去抖动 → 累积更新 AI 解读文本。

---

## 6. 依赖

```json
{
  "dependencies": {
    "react": "^19",
    "react-dom": "^19",
    "react-router-dom": "^7",
    "zustand": "^5",
    "react-markdown": "^9",
    "mermaid": "^11",
    "@tanstack/react-virtual": "^3",
    "tailwindcss": "^4",
    "lucide-react": "^0.400"
  },
  "devDependencies": {
    "@tauri-apps/cli": "^2",
    "typescript": "^5.7",
    "vite": "^6",
    "@vitejs/plugin-react": "^4"
  }
}
```

---

## 7. Tauri 集成

### 7.1 tauri.conf.json 变更

```json
{
  "build": {
    "frontendDist": "../dist",
    "devUrl": "http://localhost:1420",
    "beforeDevCommand": "npm run dev",
    "beforeBuildCommand": "npm run build"
  }
}
```

### 7.2 开发流程

```bash
# 终端 1：起后端
python -m parsing_core.serve --port 8000

# 终端 2：起前端
cd parsing-core-app && npm run dev  # Vite dev server → :1420

# 终端 3：起 Tauri（连接 :1420）
cd parsing-core-app && npx tauri dev
```

---

## 8. 测试策略

- **组件测试**（vitest + @testing-library/react）：DocViewer 渲染 5+ 万元虚拟滚动
- **API mock**（msw）：WS 事件流模拟
- **手动冒烟**：`npx tauri dev` 完整流程

---

## 9. 验收标准

1. ✅ `npx tauri dev` 启动后窗口显示 React Dashboard
2. ✅ 提交 5 个文件 batch → 卡片显示每个 task 进度条
3. ✅ 点击 "查看" → DocViewer 渲染原文/AI 解读/Mermaid 图
4. ✅ 5 万字 MD 虚拟滚动 60fps
5. ✅ `npx tauri build` 产出完整 .app
6. ✅ 154 Python 测试不破坏
7. ✅ TS 编译无错误

---

## 10. 时间估算

| 阶段 | 工作量 |
|---|---|
| Vite + React 脚手架 + 类型定义 | 0.5 天 |
| API 客户端 + Zustand store | 1 天 |
| Dashboard + BatchSubmit 页面 | 1 天 |
| DocViewer + Mermaid + 虚拟滚动 | 1.5 天 |
| Tauri 集成 + build 验证 | 0.5 天 |
| 测试 + 冒烟 | 0.5 天 |
| **合计** | **5 天** |
