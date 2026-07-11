DEEPSEEK_ROUNDS = {
    "structure",
    "concepts",
    "plain_explain",
    "application",
    "cards",
    "topic_outline",
}
CODEX_ROUNDS = {"mermaid", "review"}


class HybridIntensiveReadingExecutor:
    def __init__(self, deepseek_executor, codex_executor):
        self.deepseek_executor = deepseek_executor
        self.codex_executor = codex_executor

    def run(self, round_key: str, task_package: str) -> str:
        if round_key in DEEPSEEK_ROUNDS:
            return self.deepseek_executor.run(round_key, task_package)
        if round_key in CODEX_ROUNDS:
            return self.codex_executor.run(round_key, task_package)
        raise ValueError(f"unsupported round: {round_key}")
