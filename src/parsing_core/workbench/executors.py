from typing import Protocol


class IntensiveReadingExecutor(Protocol):
    def run(self, round_key: str, task_package: str) -> str: ...


class StubIntensiveReadingExecutor:
    def run(self, round_key: str, task_package: str) -> str:
        if round_key == "mermaid":
            return """\
## 知识结构图

```mermaid
graph TD
  CoreConcept[核心概念] --> ChapterStructure[章节结构]
```

## 应用流程图

```mermaid
flowchart LR
  ScenarioCase[应用场景] --> ActionStep[行动步骤]
```
"""
        if round_key == "cards":
            return "# 选题卡\n\n- 战略选择：从生活场景解释核心概念。"
        return f"# {round_key} 精读输出\n\n这是确定性占位输出。"


class ManualTaskPackageExecutor:
    def run(self, round_key: str, task_package: str) -> str:
        return task_package
