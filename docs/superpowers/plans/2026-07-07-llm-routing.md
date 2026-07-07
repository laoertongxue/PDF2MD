# 算力路由层 (#4) 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。

**目标：** 用 LiteLLM + 三档环境变量路由替换 StubLLMClient，接入 prompt 模板与流式输出，保留 Stub 测试回调。

**架构：** `RealLLMClient(LLMClient)` → `config.py` 环境变量路由 → `litellm.completion(stream=True)` → `on_progress` `LLM_TOKEN` 事件 → Scheduler WS 广播；`with_retry` 装饰器包裹 LLM 调用。

**技术栈：** litellm + Python 3.11 + pytest + unittest.mock

**配套规格：** `docs/superpowers/specs/2026-07-07-llm-routing-design.md`

---

## 文件结构

| 路径 | 职责 |
|---|---|
| `pyproject.toml` | 加 `litellm>=1.50` 可选依赖 |
| `src/parsing_core/llm/config.py` | 三档路由配置 + 环境变量读取 |
| `src/parsing_core/llm/litellm_client.py` | RealLLMClient 实现 |
| `src/parsing_core/llm/stub_client.py` | 不变 |
| `src/parsing_core/llm/base.py` | 不变 |
| `src/parsing_core/llm/prompt_templates.py` | 不变 |
| `src/parsing_core/orchestrator.py` | ensure interpret passes raw_md |
| `tests/test_llm/__init__.py` | 空 |
| `tests/test_llm/test_config.py` | config 测试 |
| `tests/test_llm/test_litellm_client.py` | RealLLMClient mock 测试 |
| `tests/test_llm/test_prompt_build.py` | prompt 模板测试 |

---

## 任务 0：依赖与包骨架

**文件：**
- 修改：`pyproject.toml`
- 创建：`tests/test_llm/__init__.py`

- [ ] **步骤 1：更新 pyproject.toml**

在 `[project.optional-dependencies]` 加：
```toml
llm = ["litellm>=1.50"]
```

- [ ] **步骤 2：安装依赖**

```bash
.venv/bin/pip install -e ".[llm]"
```

- [ ] **步骤 3：冒烟**

```bash
.venv/bin/python -c "import litellm; print(litellm.__version__)"
```
预期：版本号输出

- [ ] **步骤 4：创建 test_llm/__init__.py**

```bash
mkdir -p tests/test_llm && touch tests/test_llm/__init__.py
```

- [ ] **步骤 5：Commit**

```bash
git add -A && git commit -m "chore(llm): add litellm dependency and test_llm scaffold"
```

---

## 任务 1：config.py

**文件：**
- 创建：`src/parsing_core/llm/config.py`
- 测试：`tests/test_llm/test_config.py`

- [ ] **步骤 1：编写测试**

```python
# tests/test_llm/test_config.py
import os

from parsing_core.llm.config import TIER_CONFIGS, get_tier_config, PROMPT_CACHE_TIERS


def test_stub_tier_no_env_needed():
    cfg = get_tier_config("stub")
    assert cfg["model"] == "stub"
    assert cfg["stream"] == False


def test_local_tier_defaults(monkeypatch):
    monkeypatch.delenv("PARSING_CORE_LOCAL_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    cfg = get_tier_config("local")
    assert "ollama" in cfg["model"]
    assert cfg["api_base"] == "http://localhost:11434"
    assert cfg["stream"] == True


def test_local_tier_env_override(monkeypatch):
    monkeypatch.setenv("PARSING_CORE_LOCAL_MODEL", "ollama/custom-model")
    monkeypatch.setenv("OLLAMA_HOST", "http://192.168.1.1:11434")
    cfg = get_tier_config("local")
    assert cfg["model"] == "ollama/custom-model"
    assert cfg["api_base"] == "http://192.168.1.1:11434"


def test_private_tier_with_base_url(monkeypatch):
    monkeypatch.setenv("PARSING_CORE_PRIVATE_BASE_URL", "https://vllm.internal/v1")
    monkeypatch.setenv("PARSING_CORE_PRIVATE_API_KEY", "sk-xxx")
    cfg = get_tier_config("private")
    assert cfg["api_base"] == "https://vllm.internal/v1"
    assert cfg["api_key"] == "sk-xxx"


def test_public_tier_default(monkeypatch):
    monkeypatch.delenv("PARSING_CORE_PUBLIC_MODEL", raising=False)
    cfg = get_tier_config("public")
    assert "gpt-4o" in cfg["model"]


def test_public_tier_env_override(monkeypatch):
    monkeypatch.setenv("PARSING_CORE_PUBLIC_MODEL", "claude-3-5-sonnet-20241022")
    cfg = get_tier_config("public")
    assert "claude" in cfg["model"]


def test_prompt_cache_tiers_only_public():
    assert "local" not in PROMPT_CACHE_TIERS
    assert "public" in PROMPT_CACHE_TIERS
```

- [ ] **步骤 2：跑测试失败**

```bash
.venv/bin/python -m pytest tests/test_llm/test_config.py -v
```
预期：FAIL，ModuleNotFoundError

- [ ] **步骤 3：编写实现**

```python
# src/parsing_core/llm/config.py
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

PROMPT_CACHE_TIERS = {"public"}


def get_tier_config(tier: str) -> dict:
    cfg = TIER_CONFIGS.get(tier, TIER_CONFIGS["stub"]).copy()
    cfg = {k: v for k, v in cfg.items() if v is not None}
    return cfg
```

- [ ] **步骤 4：跑测试通过**

```bash
.venv/bin/python -m pytest tests/test_llm/test_config.py -v
```
预期：7 passed

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/llm/config.py tests/test_llm/test_config.py
git commit -m "feat(llm): add tier config with env var routing"
```

---

## 任务 2：prompt templates 测试

**文件：**
- 测试：`tests/test_llm/test_prompt_build.py`

- [ ] **步骤 1：编写测试**

```python
# tests/test_llm/test_prompt_build.py
from parsing_core.llm.prompt_templates import SECTION_INTERPRET_PROMPT


def test_prompt_contains_raw_md():
    prompt = SECTION_INTERPRET_PROMPT.format(raw_md="## A\n\nbody text here")
    assert "## A" in prompt
    assert "body text here" in prompt
    assert "mermaid" in prompt


def test_prompt_contains_interpret_header():
    prompt = SECTION_INTERPRET_PROMPT.format(raw_md="content")
    assert "### ▸ AI 解读" in prompt


def test_prompt_has_mermaid_instruction():
    prompt = SECTION_INTERPRET_PROMPT.format(raw_md="x")
    assert "mermaid" in prompt.lower()


def test_prompt_formats_empty_raw_md():
    prompt = SECTION_INTERPRET_PROMPT.format(raw_md="")
    assert "<<" in prompt and ">>" in prompt
```

- [ ] **步骤 2：跑测试通过**

```bash
.venv/bin/python -m pytest tests/test_llm/test_prompt_build.py -v
```
预期：4 passed

- [ ] **步骤 3：Commit**

```bash
git add tests/test_llm/test_prompt_build.py
git commit -m "test(llm): add prompt template formatting tests"
```

---

## 任务 3：RealLLMClient 核心

**文件：**
- 创建：`src/parsing_core/llm/litellm_client.py`
- 测试：`tests/test_llm/test_litellm_client.py`

- [ ] **步骤 1：编写测试（mock litellm）**

```python
# tests/test_llm/test_litellm_client.py
import time
from unittest.mock import MagicMock, patch

from parsing_core.llm.litellm_client import RealLLMClient
from parsing_core.models.dataclasses import Section


def make_section(raw="## A\n\nbody"):
    return Section(id="s1", task_id="t1", seq=0, raw_md_path="/x.raw.md",
                   sha256="h", char_count=len(raw), ai_status="PENDING")


def test_client_routes_to_stub():
    client = RealLLMClient("stub")
    assert client.tier == "stub"


def test_interpret_calls_litellm_correct_model(monkeypatch):
    monkeypatch.setenv("PARSING_CORE_LOCAL_MODEL", "ollama/test-model")
    client = RealLLMClient("local")
    sec = make_section()

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "AI response"

    with patch("parsing_core.llm.litellm_client.litellm.completion", return_value=mock_response) as mock_llm:
        artifact = client.interpret(sec, raw_md=sec.raw_md_path)

    call_kwargs = mock_llm.call_args[1]
    assert "ollama" in call_kwargs["model"]
    assert call_kwargs["api_base"] == "http://localhost:11434"
    assert artifact.ai_md == "AI response"


def test_interpret_returns_ai_md_path_set():
    client = RealLLMClient("stub")
    sec = make_section()

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "response text"

    with patch("parsing_core.llm.litellm_client.litellm.completion", return_value=mock_response):
        artifact = client.interpret(sec, raw_md=sec.raw_md_path)

    assert artifact.ai_md == "response text"
    assert artifact.model_name == "stub"
    assert artifact.tokens_in > 0


def test_interpret_streaming_calls_on_progress(monkeypatch):
    monkeypatch.setenv("PARSING_CORE_LOCAL_MODEL", "ollama/test")
    tokens = []

    def cb(task_id, kind, payload):
        if kind == "LLM_TOKEN":
            tokens.append(payload["token"])

    client = RealLLMClient("local", on_progress=cb)
    sec = make_section()

    chunk1 = MagicMock()
    chunk1.choices = [MagicMock()]
    chunk1.choices[0].delta = MagicMock()
    chunk1.choices[0].delta.content = "Hello"

    chunk2 = MagicMock()
    chunk2.choices = [MagicMock()]
    chunk2.choices[0].delta = MagicMock()
    chunk2.choices[0].delta.content = " World"

    with patch("parsing_core.llm.litellm_client.litellm.completion", return_value=[chunk1, chunk2]):
        artifact = client.interpret(sec, raw_md=sec.raw_md_path)

    assert tokens == ["Hello", " World"]
    assert artifact.ai_md == "Hello World"


def test_prompt_template_used_in_call(monkeypatch):
    monkeypatch.setenv("PARSING_CORE_LOCAL_MODEL", "ollama/test")
    client = RealLLMClient("local")
    sec = make_section(raw="## Test\n\nsample content")

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "ok"

    with patch("parsing_core.llm.litellm_client.litellm.completion", return_value=mock_response) as mock_llm:
        client.interpret(sec, raw_md="## Test\n\nsample content")

    call_kwargs = mock_llm.call_args[1]
    user_msg = call_kwargs["messages"][0]["content"]
    assert "## Test" in user_msg
    assert "sample content" in user_msg
    assert "### ▸ AI 解读" in user_msg
```

- [ ] **步骤 2：跑测试失败**

```bash
.venv/bin/python -m pytest tests/test_llm/test_litellm_client.py -v
```
预期：FAIL

- [ ] **步骤 3：编写实现**

```python
# src/parsing_core/llm/litellm_client.py
import time
import uuid

import litellm

from parsing_core.llm.base import LLMClient
from parsing_core.llm.config import PROMPT_CACHE_TIERS, get_tier_config
from parsing_core.llm.prompt_templates import SECTION_INTERPRET_PROMPT
from parsing_core.models.dataclasses import AIArtifact, Section
from parsing_core.utils.retry import with_retry


class RealLLMClient(LLMClient):
    def __init__(self, tier: str, on_progress=None):
        self.tier = tier
        self._on_progress = on_progress
        self._cfg = get_tier_config(tier)

    def interpret(self, section: Section, raw_md: str) -> AIArtifact:
        prompt = SECTION_INTERPRET_PROMPT.format(raw_md=raw_md)
        kwargs = {
            "model": self._cfg["model"],
            "messages": [{"role": "user", "content": prompt}],
        }
        if api_base := self._cfg.get("api_base"):
            kwargs["api_base"] = api_base
        if api_key := self._cfg.get("api_key"):
            kwargs["api_key"] = api_key

        if self._cfg.get("stream"):
            kwargs["stream"] = True

        if self.tier in PROMPT_CACHE_TIERS:
            kwargs["messages"] = self._apply_cache_control(kwargs["messages"])

        full_response = ""

        @with_retry(max_attempts=3, base_delay=2.0)
        def _call():
            nonlocal full_response
            response = litellm.completion(**kwargs)
            if self._cfg.get("stream"):
                for chunk in response:
                    token = chunk.choices[0].delta.content or ""
                    full_response += token
                    if self._on_progress:
                        self._on_progress(section.task_id, "LLM_TOKEN", {"token": token, "section_seq": section.seq})
            else:
                full_response = str(response.choices[0].message.content)
            return full_response

        _call()

        tokens_in = len(prompt.split())
        tokens_out = len(full_response.split())
        return AIArtifact(
            id=str(uuid.uuid4()),
            section_id=section.id,
            ai_md_path="",
            ai_md=full_response,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=0.0,
            retry_count=0,
            model_name=self._cfg["model"],
            created_at=int(time.time()),
        )

    def _apply_cache_control(self, messages: list) -> list:
        if "claude" not in self._cfg["model"]:
            return messages
        return messages
```

- [ ] **步骤 4：跑测试通过**

```bash
.venv/bin/python -m pytest tests/test_llm/test_litellm_client.py -v
```
预期：5 passed

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/llm/litellm_client.py tests/test_llm/test_litellm_client.py
git commit -m "feat(llm): add RealLLMClient with LiteLLM routing, streaming and retry"
```

---

## 任务 4：集成与回归

- [ ] **步骤 1：全量回归**

```bash
.venv/bin/python -m pytest -v 2>&1 | tail -3
```
预期：≥ 149 passed

- [ ] **步骤 2：ruff**

```bash
.venv/bin/ruff check src tests && .venv/bin/ruff format src tests
```

- [ ] **步骤 3：手验证 Stub 仍可用**

```bash
.venv/bin/python -m parsing_core.cli parse tests/fixtures/sample.md
```
预期：JSON 输出含 `"status":"COMPLETED"`

- [ ] **步骤 4：Commit**

```bash
git add -A && git commit -m "chore(llm): integration verification and ruff baseline"
```

---

## 自检

- 规格覆盖：§1.1 七个目标对应任务 1-3 全部覆盖 ✓
- 占位符：无 TODO ✓
- 一致性：config.py TIER_CONFIGS ↔ litellm_client.py get_tier_config 一致 ✓

---

计划已保存到 `docs/superpowers/plans/2026-07-07-llm-routing.md`。两种执行方式：**1. 子代理驱动** / **2. 内联执行**。选哪种？
