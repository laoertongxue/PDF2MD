import time
import uuid

import litellm

from parsing_core.llm.base import LLMClient
from parsing_core.llm.config import get_tier_config
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
                        self._on_progress(
                            section.task_id,
                            "LLM_TOKEN",
                            {"token": token, "section_seq": section.seq},
                        )
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
