import os
from pathlib import Path


class FsLayout:
    def __init__(self, base_dir: str | None = None) -> None:
        if base_dir is None:
            base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
            base_dir = str(Path(base) / "parsing-core")
        self.base_dir = base_dir
        Path(self.base_dir).mkdir(parents=True, exist_ok=True)

    def task_dir(self, task_id: str) -> str:
        d = Path(self.base_dir) / task_id
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def section_raw_path(self, task_id: str, seq: int) -> str:
        return str(Path(self.task_dir(task_id)) / f"{seq}.raw.md")

    def section_ai_path(self, task_id: str, seq: int) -> str:
        return str(Path(self.task_dir(task_id)) / f"{seq}.ai.md")

    def merged_path(self, task_id: str) -> str:
        return str(Path(self.task_dir(task_id)) / "merged.md")

    def images_dir(self, task_id: str) -> str:
        d = Path(self.task_dir(task_id)) / "images"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
