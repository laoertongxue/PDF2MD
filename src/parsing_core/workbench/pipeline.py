import re
from pathlib import Path

from parsing_core.workbench.executors import IntensiveReadingExecutor
from parsing_core.workbench.markdown_sync import sync_chapter_markdown
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.task_package import build_task_package, write_task_package

ROUNDS = ["structure", "concepts", "plain_explain", "application", "mermaid", "cards", "review"]
MERMAID_FENCE_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


class IntensiveReadingPipeline:
    def __init__(
        self,
        repo: WorkbenchRepository,
        executor: IntensiveReadingExecutor,
        run_dir: str | Path,
    ):
        self.repo = repo
        self.executor = executor
        self.run_dir = Path(run_dir)

    def run_all(self, chapter_id: str) -> None:
        chapter = self.repo.get_chapter(chapter_id)
        if chapter is None:
            raise ValueError("chapter not found")
        if chapter.status != "CONFIRMED":
            raise ValueError("chapter must be CONFIRMED before intensive reading")

        for round_key in ROUNDS:
            self._run_round(chapter_id, round_key)
        sync_chapter_markdown(self.repo, chapter_id)

    def rerun(self, chapter_id: str, round_key: str) -> None:
        if round_key not in ROUNDS:
            raise ValueError("unknown round")

        self._run_round(chapter_id, round_key)
        self.repo.mark_runs_stale(chapter_id, ROUNDS[ROUNDS.index(round_key) + 1 :])
        sync_chapter_markdown(self.repo, chapter_id)

    def _run_round(self, chapter_id: str, round_key: str) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        package = build_task_package(self.repo, chapter_id, round_key)
        input_path = write_task_package(package, self.run_dir)
        output_path = self.run_dir / f"{chapter_id}-{round_key}-output.md"
        output = ""
        try:
            output = self.executor.run(round_key, package.content)
            output_path.write_text(output, encoding="utf-8")
            self._materialize_round(chapter_id, round_key, output)
            self.repo.upsert_run(
                chapter_id=chapter_id,
                round_key=round_key,
                executor=type(self.executor).__name__,
                status="DONE",
                input_path=input_path,
                output_path=str(output_path),
                output=output,
                stale=False,
            )
        except Exception as exc:
            self.repo.upsert_run(
                chapter_id=chapter_id,
                round_key=round_key,
                executor=type(self.executor).__name__,
                status="FAILED",
                input_path=input_path,
                output_path=str(output_path),
                output=output or str(exc),
                stale=False,
            )
            raise

    def _materialize_round(self, chapter_id: str, round_key: str, output: str) -> None:
        chapter = self.repo.get_chapter(chapter_id)
        if chapter is None:
            raise ValueError("chapter not found")

        if round_key == "structure":
            self.repo.upsert_note_block(chapter_id, "summary", "本章概要", output, 0)
        elif round_key == "concepts":
            self.repo.upsert_note_block(chapter_id, "concepts", "核心概念", output, 1)
        elif round_key == "plain_explain":
            self.repo.upsert_note_block(chapter_id, "plain_explain", "通俗解释", output, 2)
        elif round_key == "application":
            self.repo.upsert_note_block(chapter_id, "application", "应用场景", output, 3)
        elif round_key == "mermaid":
            diagrams = _extract_mermaid_diagrams(output)
            self.repo.upsert_note_block(
                chapter_id,
                "knowledge_mermaid",
                "知识结构图",
                diagrams[0],
                4,
            )
            self.repo.upsert_note_block(
                chapter_id,
                "application_mermaid",
                "应用流程图",
                diagrams[1],
                5,
            )
        elif round_key == "cards":
            self.repo.delete_cards_by_chapter_and_kind(chapter_id, "topic")
            self.repo.create_card(
                chapter.course_id,
                chapter_id,
                "topic",
                f"{chapter.title} 写作选题",
                output,
            )
        elif round_key == "review":
            self.repo.upsert_note_block(chapter_id, "reflection", "复盘反思", output, 6)


def _extract_mermaid_diagrams(output: str) -> list[str]:
    diagrams = [match.strip() for match in MERMAID_FENCE_RE.findall(output) if match.strip()]
    if len(diagrams) < 2:
        raise ValueError("mermaid round must output knowledge and application diagrams")
    return diagrams[:2]
