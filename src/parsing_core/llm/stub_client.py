# src/parsing_core/llm/stub_client.py
import time
import uuid

from parsing_core.llm.base import LLMClient
from parsing_core.models.dataclasses import AIArtifact, Section


class StubLLMClient(LLMClient):
    """确定性 stub LLM 客户端。不调用任何真实模型，输出固定结构的占位 MD。"""

    def interpret(self, section: Section, raw_md: str) -> AIArtifact:
        tokens_in = len(raw_md.split())
        ai_md = self._render(section.seq, tokens_in)
        tokens_out = len(ai_md.split())
        return AIArtifact(
            id=str(uuid.uuid4()),
            section_id=section.id,
            ai_md_path="",
            ai_md=ai_md,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=0.0,
            retry_count=0,
            model_name="stub",
            created_at=int(time.time()),
        )

    @staticmethod
    def _render(seq: int, tokens_in: int) -> str:
        return (
            "### ▸ AI 解读\n\n"
            f"- **关键指标**: <stub 占位>（节 {seq}，输入约 {tokens_in} tokens）\n"
            "- **风险提示**: <stub 占位>\n\n"
            "```mermaid\n"
            "flowchart LR\n"
            f"  A[Stub 节 {seq}] --> B[占位节点]\n"
            "  B --> C[Mermaid 已就绪]\n"
            "```\n"
        )
