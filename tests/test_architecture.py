import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKBENCH = ROOT / "src" / "parsing_core" / "workbench"
FORBIDDEN = {
    "domain": {"fastapi", "sqlite3", "requests", "httpx", "pathlib"},
    "application": {"fastapi", "sqlite3", "requests"},
}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    return modules


def test_workbench_layer_import_boundaries() -> None:
    violations: list[str] = []

    for layer, forbidden in FORBIDDEN.items():
        layer_dir = WORKBENCH / layer
        paths = sorted(layer_dir.rglob("*.py")) if layer_dir.is_dir() else []
        for path in paths:
            for module in sorted(imported_modules(path)):
                if module.partition(".")[0] in forbidden:
                    relative_path = path.relative_to(ROOT)
                    violations.append(f"{relative_path}: forbidden import {module}")

    assert violations == []
