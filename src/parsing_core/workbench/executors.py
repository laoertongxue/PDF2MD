from typing import Protocol


class IntensiveReadingExecutor(Protocol):
    def run(self, round_key: str, task_package: str) -> str: ...


class TextExecutor(Protocol):
    def run(self, task_key: str, prompt: str) -> str: ...


class StubIntensiveReadingExecutor:
    def run(self, round_key: str, task_package: str) -> str:
        topic_package = None
        if round_key in {
            "alignment", "comparison", "plain_cases", "framework_application",
            "mermaid", "cards", "review",
        }:
            import json

            try:
                candidate = json.loads(task_package)
                if isinstance(candidate, dict):
                    if "topic_id" in candidate:
                        topic_package = candidate
                    elif isinstance(candidate.get("task_package"), dict):
                        topic_package = candidate["task_package"]
            except json.JSONDecodeError:
                pass
        if topic_package is not None:
            import json

            package = topic_package
            labels = [chapter["source_label"] for chapter in package["source_chapters"]]
            refs = " ".join(labels)
            if round_key == "alignment":
                value = {"overview": f"主题总览 {refs}", "linked_sources": refs,
                         "core_concepts": f"核心概念与取舍 {refs}"}
            elif round_key == "comparison":
                value = {"viewpoint_comparison": f"观点对照 {refs}",
                         "consensus_disagreements": f"共识与分歧 {refs}",
                         "complementary_views": f"互补视角 {refs}"}
            elif round_key == "plain_cases":
                value = {"plain_explanation": f"通俗解释 {refs}",
                         "textbook_cases": f"教材案例 {refs}",
                         "real_world_problem_solving": f"现实问题求解 {refs}"}
            elif round_key == "framework_application":
                value = {"integrated_framework": f"融合框架 {refs}",
                         "application_methods": f"应用方法 {refs}",
                         "further_thinking": f"延伸思考 {refs}"}
            elif round_key == "mermaid":
                value = {"knowledge_diagram": "graph TD\n  A[核心概念] --> B[融合框架]",
                         "application_diagram": "flowchart LR\n  A[识别问题] --> B[应用框架]"}
            elif round_key == "cards":
                value = {"cards": [{"card_type": "insight", "title": f"选题卡 {i + 1}",
                                     "content": f"围绕主题展开的写作角度 {i + 1}。",
                                     "source_refs": labels} for i in range(8)]}
            else:
                value = {"passed": True, "issues": []}
            return json.dumps(value, ensure_ascii=False)
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
