# src/parsing_core/parser/base.py
from abc import ABC, abstractmethod


class Parser(ABC):
    @abstractmethod
    def parse(self, file_path: str) -> str: ...

    def parse_text(self, text: str) -> str:
        return text
