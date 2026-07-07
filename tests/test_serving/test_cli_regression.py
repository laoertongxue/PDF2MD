import json
import subprocess
import sys
from pathlib import Path


def test_cli_parse_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r = subprocess.run(
        [sys.executable, "-m", "parsing_core.cli", "parse", sample],
        capture_output=True,
        text=True,
        cwd=".",
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["status"] == "COMPLETED"
