# src/parsing_core/llm/base.py
from abc import ABC, abstractmethod

from parsing_core.models.dataclasses import AIArtifact, Section


class LLMClient(ABC):
    @abstractmethod
    def interpret(self, section: Section, raw_md: str) -> AIArtifact: ...
