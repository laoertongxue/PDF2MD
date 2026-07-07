# 算力路由层 (#4) 设计规格

**日期**: 2026-07-07
**子项目**: #4 算力路由层（LiteLLM 三档路由 + Prompt 缓存）
**路径**: 地基优先（方案 A）的第四步
**状态**: 已批准，待实现计划

---

## 1. 目标与非目标

### 1.1 目标

1. **LiteLLM 统一客户端**：`RealLLMClient(LLMClient)` 用 litellm 库统一调用 OpenAI/Anthropic/Ollama/自定义 vLLM，替换 StubLLMClient
2. **三档路由**：按 `model_tier` 字段值 + 环境变量自动选择模型：`local`（Ollama 本地）、`private`（自建 vLLM）、`public`（OpenAI/Anthropic）
3. **Prompt 模板接入**：`prompt_templates.SECTION_INTERPRET_PROMPT` 格式化 `{raw_md}` 后作为提示词发给 LLM
4. **Prompt 缓存**：Anthropic 用 `cache_control`，OpenAI 自动缓存，本地无缓存
5. **流式输出**：LLM token 流 → orchestrator `on_progress` → WS `LLM_TOKEN` 事件
6. **重试机制**：复用 `utils/retry.py` `with_retry` 装饰器，429/5xx 自动指数退避
7. **Stub 保留**：StubLLMClient 不删，用于测试与快速启动场景

### 1.2 非目标

- WebUI（#5）
- 本地模型部署（Ollama/Llama.cpp 安装由用户自理）
- 多个 LLM 厂商密钥管理面板
- `model_tier` 字段的路由配置 UI

---

## 2. 架构

### 2.1 路由决策树

```
model_tier="stub"  → StubLLMClient（原有）
model_tier="local" → litellm.completion(model=env("PARSING_CORE_LOCAL_MODEL", "ollama/llama3.2"))
                     base_url=env("OLLAMA_HOST", "http://localhost:11434")
model_tier="private"→ litellm.completion(model=env("PARSING_CORE_PRIVATE_MODEL"))
                     base_url=env("PARSING_CORE_PRIVATE_BASE_URL")
                     extra_headers={"Authorization": "Bearer ${PARSING_CORE_PRIVATE_API_KEY}"}
model_tier="public" → litellm.completion(model=env("PARSING_CORE_PUBLIC_MODEL", "openai/gpt-4o"))
                     自动读 OPENAI_API_KEY / ANTHROPIC_API_KEY
```

### 2.2 模块结构

```
src/parsing_core/llm/
  __init__.py            # (不变)
  base.py                # LLMClient ABC (不变)
  stub_client.py         # StubLLMClient (不变，仅用于测试)
  litellm_client.py      # RealLLMClient (新)
  config.py              # 三档路由配置常量 (新)
  prompt_templates.py    # (不变，接入使用)
tests/test_llm/
  __init__.py
  test_config.py          # 配置解析测试
  test_litellm_client.py  # 用 unittest.mock 测路由逻辑
  test_prompt_build.py    # prompt 模板格式化测试
```

### 2.3 调用关系

```
orchestrator._interpret_section
  └─ llm.interpret(section, raw_md)
       └─ RealLLMClient.interpret
            ├─ config.get_model_config(tier) → (model_name, kwargs)
            ├─ prompt = SECTION_INTERPRET_PROMPT.format(raw_md=raw_md)
            ├─ with_retry(max_attempts=3, base_delay=2):
            │    └─ litellm.acompletion(
            │         model=model_name,
            │         messages=[{"role":"user","content":prompt}],
            │         stream=True,
            │         **kwargs
            │       )
            ├─ 遍历 chunk:
            │    └─ token = chunk.choices[0].delta.content
            │         └─ on_progress(task_id, "LLM_TOKEN", {"token": token, "section_seq": section.seq})
            └─ 合并 tokens → ai_md → 返回 AIArtifact
```

---

## 3. 配置模型

### 3.1 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PARSING_CORE_LOCAL_MODEL` | `ollama/llama3.2-vision:latest` | 本地模型名 |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama 地址 |
| `PARSING_CORE_PRIVATE_MODEL` | `openai/gpt-4o-mini` | 私有云模型名（litellm 格式） |
| `PARSING_CORE_PRIVATE_BASE_URL` | — | vLLM/私有 API 地址 |
| `PARSING_CORE_PRIVATE_API_KEY` | — | 私有云 API Key |
| `PARSING_CORE_PUBLIC_MODEL` | `openai/gpt-4o` | 公有云模型名 |
| `OPENAI_API_KEY` | — | OpenAI Key（litellm 原生） |
| `ANTHROPIC_API_KEY` | — | Anthropic Key（litellm 原生） |

### 3.2 config.py

```python
import os

TIER_CONFIGS = {
    "stub": {"model": "stub", "stream": False},
    "local": {
        "model": os.getenv("PARSING_CORE_LOCAL_MODEL", "ollama/llama3.2-vision:latest"),
        "api_base": os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        "stream": True,
    },
    "private": {
        "model": os.getenv("PARSING_CORE_PRIVATE_MODEL", "openai/gpt-4o-mini"),
        "api_base": os.getenv("PARSING_CORE_PRIVATE_BASE_URL"),
        "api_key": os.getenv("PARSING_CORE_PRIVATE_API_KEY"),
        "stream": True,
    },
    "public": {
        "model": os.getenv("PARSING_CORE_PUBLIC_MODEL", "openai/gpt-4o"),
        "stream": True,
    },
}

PROMPT_CACHE_TIERS = {"public"}  # 仅公有云开 prompt cache
```

---

## 4. RealLLMClient 实现

```python
class RealLLMClient(LLMClient):
    def __init__(self, tier: str, on_progress=None):
        self.tier = tier
        self._on_progress = on_progress
        self._cfg = TIER_CONFIGS[tier]

    def interpret(self, section: Section, raw_md: str) -> AIArtifact:
        prompt = SECTION_INTERPRET_PROMPT.format(raw_md=raw_md)
        kwargs = {"model": self._cfg["model"], "messages": [{"role":"user","content":prompt}]}
        if api_base := self._cfg.get("api_base"):
            kwargs["api_base"] = api_base
        if api_key := self._cfg.get("api_key"):
            kwargs["api_key"] = api_key
        if self._cfg.get("stream"):
            kwargs["stream"] = True

        # prompt caching for public
        if self.tier in PROMPT_CACHE_TIERS:
            kwargs["messages"] = self._apply_cache_control(kwargs["messages"])

        full_response = ""

        @with_retry(max_attempts=3, base_delay=2)
        def _call():
            nonlocal full_response
            response = litellm.completion(**kwargs)
            if self._cfg.get("stream"):
                for chunk in response:
                    token = chunk.choices[0].delta.content or ""
                    full_response += token
                    if self._on_progress:
                        self._on_progress(task_id, "LLM_TOKEN", {"token": token})
            else:
                full_response = response.choices[0].message.content
            return full_response

        _call()
        # ... 返回 AIArtifact
```

---

## 5. Prompt 缓存

### 5.1 Anthropic

对 messages[0] 加 `cache_control: {"type": "ephemeral"}`，后续调用命中：

```python
def _apply_cache_control(self, messages):
    if "claude" in self._cfg["model"]:
        messages[-1]["content"] = [
            {"type": "text", "text": messages[-1]["content"],
             "cache_control": {"type": "ephemeral"}}
        ]
    return messages
```

OpenAI 自动缓存无需显式配置。

---

## 6. 流式输出与 on_progress 集成

orchestrator 的 `on_progress` 回调已支持 `LLM_TOKEN` 事件类型（#3 预留）。RealLLMClient 通过 `_on_progress` 调用发送 token。Scheduler 的 `sync_progress` 收到 `LLM_TOKEN` 事件后通过 `asyncio.run_coroutine_threadsafe` → `_emit` → WS 广播。

事件格式：
```json
{"seq": 42, "batch_id": "b1", "task_id": "t1", "event": "LLM_TOKEN",
 "payload": {"token": "产量", "section_seq": 0}, "ts": 1783331520}
```

---

## 7. 测试策略

### 7.1 单元测试（pytest + unittest.mock）

- `test_config`：读环境变量、默认值回退
- `test_litellm_client`：mock `litellm.completion` 验证 routing 正确（tier="local" 时 model="ollama/...", api_base="http://..."）
- `test_prompt_build`：格式化 `SECTION_INTERPRET_PROMPT` 含 raw_md

### 7.2 集成测试

- Stub 仍可用：`python -m parsing_core.cli parse tests/fixtures/sample.md` 仍走 Stub，前 137 测试不变
- 需 Ollama 才能跑真 local 测试——本子项目不强制真模型集成测试

### 7.3 回归

- 全量测试 ≥ 137 passed（CLI + 服务 + 所有存在测试）

---

## 8. 依赖

```toml
[project.optional-dependencies]
llm = ["litellm>=1.50"]
```

---

## 9. 验收标准

1. ✅ `RealLLMClient("local").interpret(...)` 调 litellm.completion(model="ollama/...")
2. ✅ `RealLLMClient("public").interpret(...)` 调 litellm.completion(model="openai/gpt-4o")
3. ✅ Prompt template 格式化正确含 raw_md 内容
4. ✅ 流式输出 token 通过 on_progress 回调
5. ✅ 429/5xx 自动重试（with_retry）
6. ✅ Stub 不破坏，前 137 测试全绿
7. ✅ ruff check 无 warning

---

## 10. 时间估算

| 阶段 | 工作量 |
|---|---|
| config.py + litellm 依赖 | 0.5 天 |
| RealLLMClient 核心 | 1 天 |
| prompt template 接入 + 缓存 | 0.5 天 |
| 流式 + on_progress 集成 | 0.5 天 |
| 测试 | 1 天 |
| **合计** | **3.5 天** |
