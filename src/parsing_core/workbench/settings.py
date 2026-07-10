import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkbenchSettings:
    deepseek_model: str = "deepseek-chat"


def load_settings(path: str | Path) -> WorkbenchSettings:
    settings_path = Path(path)
    if not settings_path.exists():
        return WorkbenchSettings()
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    return WorkbenchSettings(deepseek_model=data.get("deepseek_model") or "deepseek-chat")


def save_settings(path: str | Path, settings: WorkbenchSettings) -> None:
    settings_path = Path(path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
