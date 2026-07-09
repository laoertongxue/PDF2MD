import os
import shutil
import subprocess
from pathlib import Path


class CodexCliError(RuntimeError):
    pass


def resolve_codex_path() -> str:
    env_path = os.environ.get("CODEX_CLI_PATH")
    if env_path:
        return env_path
    found = shutil.which("codex")
    if not found:
        raise CodexCliError("codex cli not found")
    return found


class CodexCliExecutor:
    def __init__(self, codex_path: str, run_dir: str | Path, timeout: int = 300):
        self.codex_path = codex_path
        self.run_dir = Path(run_dir)
        self.timeout = timeout

    def run(self, round_key: str, task_package: str) -> str:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.run_dir / f"codex-{round_key}-output.md"
        if output_path.exists():
            output_path.unlink()
        cmd = [
            self.codex_path,
            "exec",
            "--sandbox",
            "read-only",
            "--cd",
            str(self.run_dir),
            "--output-last-message",
            str(output_path),
            "-",
        ]
        result = subprocess.run(
            cmd,
            input=task_package,
            text=True,
            capture_output=True,
            timeout=self.timeout,
            check=False,
        )
        if result.returncode != 0:
            raise CodexCliError(result.stderr.strip() or "codex cli failed")
        if not output_path.exists():
            raise CodexCliError("codex output file missing")
        output = output_path.read_text(encoding="utf-8")
        if not output.strip():
            raise CodexCliError("codex output file empty")
        return output
