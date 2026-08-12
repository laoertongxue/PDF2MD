import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_RELATIVE = Path("src")
WORKBENCH_RELATIVE = Path("src/parsing_core/workbench")
LAYERS = ("domain", "application", "ports")
WORKBENCH_MODULE = "parsing_core.workbench"
ALLOWED_WORKBENCH_PREFIXES = {
    "domain": {"parsing_core.workbench.domain"},
    "application": {
        "parsing_core.workbench.application",
        "parsing_core.workbench.domain",
        "parsing_core.workbench.ports",
    },
    "ports": {
        "parsing_core.workbench.ports",
        "parsing_core.workbench.domain",
    },
}
FORBIDDEN_PREFIXES = {
    "domain": {
        "fastapi",
        "sqlite3",
        "requests",
        "httpx",
        "pathlib",
    },
    "application": {
        "fastapi",
        "sqlite3",
        "requests",
        "httpx",
        "parsing_core.serving",
    },
    "ports": {
        "fastapi",
        "sqlite3",
        "requests",
        "httpx",
        "parsing_core.serving",
    },
}


def imported_modules(path: Path, src_root: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    current_package = path.relative_to(src_root).parent.parts
    modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parent_count = len(current_package) - node.level + 1
                module_parts = list(current_package[: max(parent_count, 0)])
            else:
                module_parts = []
            if node.module:
                module_parts.extend(node.module.split("."))

            base_module = ".".join(module_parts)
            for alias in node.names:
                if alias.name == "*":
                    if base_module:
                        modules.add(base_module)
                    continue
                modules.add(".".join((*module_parts, alias.name)))

    return modules


def _matches_prefix(module: str, prefixes: set[str]) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)


def _is_forbidden(layer: str, module: str) -> bool:
    if module == WORKBENCH_MODULE:
        return False
    if module.startswith(f"{WORKBENCH_MODULE}."):
        return not _matches_prefix(module, ALLOWED_WORKBENCH_PREFIXES[layer])
    return _matches_prefix(module, FORBIDDEN_PREFIXES[layer])


def find_violations(root: Path) -> list[str]:
    violations: list[str] = []
    src_root = root / SRC_RELATIVE
    workbench = root / WORKBENCH_RELATIVE

    for layer in LAYERS:
        layer_dir = workbench / layer
        boundary = layer_dir / "__init__.py"
        if not boundary.is_file():
            relative_boundary = boundary.relative_to(root)
            violations.append(f"{relative_boundary}: required package boundary is missing")
            continue

        for path in sorted(layer_dir.rglob("*.py")):
            for module in sorted(imported_modules(path, src_root)):
                if _is_forbidden(layer, module):
                    relative_path = path.relative_to(root)
                    violations.append(f"{relative_path}: forbidden import {module}")

    return violations


def _create_package_boundaries(root: Path) -> None:
    for layer in LAYERS:
        package = root / WORKBENCH_RELATIVE / layer
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8")


def _write_layer_module(root: Path, layer: str, source: str) -> None:
    path = root / WORKBENCH_RELATIVE / layer / "example.py"
    path.write_text(source, encoding="utf-8")


def _assert_forbidden_modules(root: Path, expected: set[str]) -> None:
    violations = find_violations(root)
    rendered = "\n".join(violations)

    assert len(violations) == len(expected)
    for module in expected:
        assert f"forbidden import {module}" in rendered


def test_repository_has_required_package_boundaries() -> None:
    assert find_violations(ROOT) == []


def test_find_violations_rejects_forbidden_domain_dependencies(tmp_path: Path) -> None:
    _create_package_boundaries(tmp_path)
    expected = {
        "fastapi",
        "sqlite3",
        "requests",
        "httpx",
        "pathlib",
        "parsing_core.workbench.application",
        "parsing_core.workbench.ocr",
        "parsing_core.workbench.ports",
        "parsing_core.workbench.infrastructure",
    }
    _write_layer_module(
        tmp_path,
        "domain",
        "\n".join(
            (
                "import fastapi",
                "import sqlite3",
                "import requests",
                "import httpx",
                "from pathlib import Path",
                "from parsing_core.workbench.application import UseCase",
                "from parsing_core.workbench import ocr",
                "from parsing_core.workbench.ports import Gateway",
                "from parsing_core.workbench.infrastructure import Repository",
            )
        ),
    )

    _assert_forbidden_modules(tmp_path, expected)


def test_find_violations_rejects_forbidden_application_dependencies(tmp_path: Path) -> None:
    _create_package_boundaries(tmp_path)
    expected = {
        "fastapi",
        "sqlite3",
        "requests",
        "httpx",
        "parsing_core.workbench.infrastructure",
        "parsing_core.workbench.ocr",
        "parsing_core.workbench.schema",
        "parsing_core.serving",
    }
    _write_layer_module(
        tmp_path,
        "application",
        "\n".join(
            (
                "import fastapi",
                "import sqlite3",
                "import requests",
                "import httpx",
                "from parsing_core.workbench.infrastructure import Repository",
                "from parsing_core.workbench.ocr import OcrEngine",
                "from parsing_core.workbench import schema",
                "from parsing_core.serving import Scheduler",
            )
        ),
    )

    _assert_forbidden_modules(tmp_path, expected)


def test_find_violations_rejects_forbidden_ports_dependencies(tmp_path: Path) -> None:
    _create_package_boundaries(tmp_path)
    expected = {
        "fastapi",
        "sqlite3",
        "requests",
        "httpx",
        "parsing_core.workbench.application",
        "parsing_core.workbench.infrastructure",
        "parsing_core.workbench.ocr",
        "parsing_core.workbench.schema",
        "parsing_core.serving",
    }
    _write_layer_module(
        tmp_path,
        "ports",
        "\n".join(
            (
                "import fastapi",
                "import sqlite3",
                "import requests",
                "import httpx",
                "from parsing_core.workbench.application import UseCase",
                "from parsing_core.workbench.infrastructure import Repository",
                "from parsing_core.workbench.ocr import OcrEngine",
                "from parsing_core.workbench import schema",
                "from parsing_core.serving import Scheduler",
            )
        ),
    )

    _assert_forbidden_modules(tmp_path, expected)


def test_find_violations_resolves_forbidden_relative_imports(tmp_path: Path) -> None:
    _create_package_boundaries(tmp_path)
    _write_layer_module(tmp_path, "domain", "from .. import ocr\n")
    _write_layer_module(tmp_path, "application", "from .. import schema\n")

    _assert_forbidden_modules(
        tmp_path,
        {"parsing_core.workbench.ocr", "parsing_core.workbench.schema"},
    )


def test_find_violations_allows_intended_layer_dependencies(tmp_path: Path) -> None:
    _create_package_boundaries(tmp_path)
    _write_layer_module(tmp_path, "domain", "from .entities import Entity\n")
    _write_layer_module(
        tmp_path,
        "application",
        "\n".join(
            (
                "from .use_cases import UseCase",
                "from .. import domain, ports",
                "from parsing_core.workbench.domain import Entity",
                "from parsing_core.workbench.ports import Repository",
            )
        ),
    )
    _write_layer_module(
        tmp_path,
        "ports",
        "\n".join(
            (
                "from .gateway import Gateway",
                "from .. import domain",
                "from parsing_core.workbench.domain import Entity",
            )
        ),
    )

    assert find_violations(tmp_path) == []
