import pytest

from parsing_core.workbench.hybrid import HybridIntensiveReadingExecutor


class RecordingExecutor:
    def __init__(self, output: str):
        self.output = output
        self.calls: list[tuple[str, str]] = []

    def run(self, round_key: str, task_package: str) -> str:
        self.calls.append((round_key, task_package))
        return self.output


@pytest.mark.parametrize(
    "round_key",
    ["structure", "concepts", "plain_explain", "application", "cards"],
)
def test_hybrid_routes_deepseek_rounds(round_key):
    deepseek = RecordingExecutor("deepseek")
    codex = RecordingExecutor("codex")

    output = HybridIntensiveReadingExecutor(deepseek, codex).run(round_key, "# task")

    assert output == "deepseek"
    assert deepseek.calls == [(round_key, "# task")]
    assert codex.calls == []


@pytest.mark.parametrize("round_key", ["mermaid", "review"])
def test_hybrid_routes_codex_rounds(round_key):
    deepseek = RecordingExecutor("deepseek")
    codex = RecordingExecutor("codex")

    output = HybridIntensiveReadingExecutor(deepseek, codex).run(round_key, "# task")

    assert output == "codex"
    assert codex.calls == [(round_key, "# task")]
    assert deepseek.calls == []


def test_hybrid_rejects_unknown_round():
    executor = HybridIntensiveReadingExecutor(
        RecordingExecutor("deepseek"), RecordingExecutor("codex")
    )

    with pytest.raises(ValueError, match="unsupported round: unknown"):
        executor.run("unknown", "# task")
