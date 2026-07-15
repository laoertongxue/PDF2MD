import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts/check-release-sidecar.sh"


def _fake_bundle(tmp_path: Path, source: str) -> tuple[Path, Path]:
    app = tmp_path / "PDF2MD.app"
    contents = app / "Contents"
    launcher = contents / "MacOS/python3"
    runtime_python = contents / "Resources/python-runtime/bin/python3"
    launcher.parent.mkdir(parents=True)
    runtime_python.parent.mkdir(parents=True)
    (contents / "Resources/src/parsing_core").mkdir(parents=True)
    launcher.write_text("#!/bin/bash\n", encoding="utf-8")
    runtime_python.write_text("#!/bin/bash\n", encoding="utf-8")
    (contents / "Resources/src/parsing_core/module.py").write_text(source, encoding="utf-8")
    launcher.chmod(0o755)
    runtime_python.chmod(0o755)

    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "codesign").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (commands / "otool").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (commands / "file").write_text("#!/bin/bash\necho 'ASCII text'\n", encoding="utf-8")
    for command in commands.iterdir():
        command.chmod(0o755)
    return app, commands


def _run(app: Path, commands: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{commands}:{env['PATH']}"
    return subprocess.run(["bash", str(CHECK), str(app)], text=True, capture_output=True, env=env)


def test_release_scan_ignores_business_regex_but_rejects_real_user_path(tmp_path):
    regex_app, commands = _fake_bundle(tmp_path / "regex", 'PATTERN = r"/Users/[^\\s]+"\n')
    assert _run(regex_app, commands).returncode == 0

    leak_app, commands = _fake_bundle(
        tmp_path / "leak", 'SOURCE = "/Users/laoer/Documents/PDF2MD/book.pdf"\n'
    )
    result = _run(leak_app, commands)
    assert result.returncode != 0
    assert "development-machine" in result.stderr
