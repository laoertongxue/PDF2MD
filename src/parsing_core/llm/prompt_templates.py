# src/parsing_core/llm/prompt_templates.py
SECTION_INTERPRET_PROMPT = """你是工业报表分析助手。给定以下原文节，请输出：
1. 关键指标（如有）
2. 风险提示（如有）
3. 一段 mermaid 流程图代码块，可视化该节描述的过程或结构

原文节：
<<<
{raw_md}
>>>

输出格式：Markdown，必须包含 `### ▸ AI 解读` 标题和至少一个 ```mermaid 代码块。
"""
