# src/parsing_core/parser/markitdown_adapter.py
from pathlib import Path

from parsing_core.parser.base import Parser


class MarkItDownAdapter(Parser):
    def __init__(self) -> None:
        try:
            from markitdown import MarkItDown
        except ImportError as e:
            raise RuntimeError("markitdown not installed") from e
        self._md = MarkItDown()

    def parse(self, file_path: str) -> str:
        result = self._md.convert(file_path)
        return str(result)

    def parse_text(self, text: str) -> str:
        # 仅 Markdown 直接透传，避免无谓的文件 IO
        if Path(text).suffix.lower() in (".md", ".markdown", ".txt"):
            return Path(text).read_text(encoding="utf-8")
        return self.parse(text)
