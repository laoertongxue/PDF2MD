import json
import re
import threading
import time
from pathlib import Path

from parsing_core.workbench.executors import IntensiveReadingExecutor
from parsing_core.workbench.markdown_sync import sync_chapter_markdown
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.task_package import (
    build_review_package,
    build_task_package,
    write_task_package,
)
from parsing_core.workbench.topic_state import (
    mark_topics_stale_for_chapter,
    refresh_topic_status,
)

ROUNDS = ["structure", "concepts", "plain_explain", "application", "mermaid", "cards", "review"]
CODEX_ROUNDS = {"mermaid", "review"}
MERMAID_FENCE_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
FIXED_CHAPTER_KINDS = {
    "summary",
    "concepts",
    "plain_explain",
    "application",
    "knowledge_mermaid",
    "application_mermaid",
    "reflection",
}
BLOCK_TITLES = {
    "summary": "本章概要",
    "concepts": "核心概念",
    "plain_explain": "通俗解释",
    "application": "应用场景",
    "knowledge_mermaid": "知识结构图",
    "application_mermaid": "应用流程图",
    "reflection": "复盘反思",
}


def _refresh_topics_for_chapter(repo: WorkbenchRepository, chapter_id: str) -> None:
    for topic in repo.list_topics_for_chapter(chapter_id):
        refresh_topic_status(repo, topic.id)


class ChapterMarkdownSyncError(Exception):
    pass


def _sync_chapter_markdown(repo: WorkbenchRepository, chapter_id: str) -> None:
    try:
        sync_chapter_markdown(repo, chapter_id)
    except OSError as exc:
        raise ChapterMarkdownSyncError from exc


class IntensiveReadingPipeline:
    def __init__(
        self,
        repo: WorkbenchRepository,
        executor: IntensiveReadingExecutor,
        run_dir: str | Path,
        *,
        clock=None,
        lease_ttl: int = 7_200,
        heartbeat_interval: float | None = None,
    ):
        self.repo = repo
        self.executor = executor
        self.run_dir = Path(run_dir)
        self.clock = clock or (lambda: int(time.time()))
        self.lease_ttl = lease_ttl
        self.heartbeat_interval = (
            min(60.0, lease_ttl / 3) if heartbeat_interval is None else heartbeat_interval
        )
        self._candidate_paths: dict[str, tuple[str, str]] = {}

    def run_all(self, chapter_id: str) -> None:
        chapter = self.repo.get_chapter(chapter_id)
        if chapter is None:
            raise ValueError("chapter not found")
        start = self.repo.start_chapter_generation(
            chapter_id, now=self.clock(), lease_ttl=self.lease_ttl
        )
        mark_topics_stale_for_chapter(
            self.repo,
            chapter_id,
            f"chapter {chapter_id} changed",
            round_keys=ROUNDS,
        )
        try:
            for round_key in ROUNDS[:-1]:
                self._run_candidate(chapter_id, start.owner_id, round_key)
            self._run_review_and_publish(chapter_id, start.owner_id)
            _sync_chapter_markdown(self.repo, chapter_id)
        except Exception:
            try:
                self.repo.fail_chapter_generation(chapter_id, start.owner_id)
            except ValueError as exc:
                if "lease lost" not in str(exc):
                    raise
            raise
        finally:
            _refresh_topics_for_chapter(self.repo, chapter_id)

    def _run_with_heartbeat(
        self, chapter_id: str, owner_id: str, round_key: str, prompt: str
    ) -> str:
        stop = threading.Event()
        errors = []

        def renew():
            while not stop.wait(self.heartbeat_interval):
                try:
                    self.repo.heartbeat_chapter_generation(
                        chapter_id, owner_id, now=self.clock(), lease_ttl=self.lease_ttl
                    )
                except Exception as exc:
                    errors.append(exc)
                    return

        thread = threading.Thread(
            target=renew, name=f"chapter-lease-heartbeat-{chapter_id}", daemon=True
        )
        thread.start()
        try:
            output = self.executor.run(round_key, prompt)
        finally:
            stop.set()
            thread.join()
        if errors:
            raise ValueError("chapter generation lease lost") from errors[0]
        self.repo.heartbeat_chapter_generation(
            chapter_id, owner_id, now=self.clock(), lease_ttl=self.lease_ttl
        )
        return output

    def _run_candidate(self, chapter_id: str, owner_id: str, round_key: str) -> None:
        run = self.repo.create_chapter_generation_run(
            chapter_id, owner_id, round_key, now=self.clock()
        )
        package = build_task_package(self.repo, chapter_id, round_key)
        input_path = ""
        if round_key not in CODEX_ROUNDS:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            input_path = write_task_package(package, self.run_dir)
        try:
            output = self._run_with_heartbeat(chapter_id, owner_id, round_key, package.content)
            output_path = self.run_dir / f"{chapter_id}-{round_key}-output.md"
            self.run_dir.mkdir(parents=True, exist_ok=True)
            output_path.write_text(output, encoding="utf-8")
            self._candidate_paths[round_key] = (input_path, str(output_path))
            if round_key == "mermaid":
                _extract_mermaid_diagrams(output)
            if not output.strip():
                raise ValueError("chapter candidate must contain content")
            if round_key == "cards" and "选题卡" not in output:
                raise ValueError("chapter cards candidate is invalid")
            self.repo.finish_chapter_generation_run(
                run.id, owner_id, "COMPLETED", output=output, now=self.clock()
            )
        except Exception as exc:
            current = self.repo.get_chapter_generation_run(run.id)
            if current.status == "RUNNING":
                self.repo.finish_chapter_generation_run(
                    run.id, owner_id, "FAILED", error=_safe_chapter_error(exc), now=self.clock()
                )
                if not any(item.round_key == round_key for item in self.repo.list_runs(chapter_id)):
                    self.repo.upsert_run(
                        chapter_id,
                        round_key,
                        type(self.executor).__name__,
                        "FAILED",
                        input_path,
                        "",
                        f"{type(exc).__name__}: intensive reading round failed",
                        False,
                    )
            raise

    def _run_review_and_publish(self, chapter_id: str, owner_id: str) -> None:
        candidates = self.repo.chapter_generation_candidates(chapter_id, owner_id)
        prompt = build_review_package(self.repo, chapter_id, candidates)
        run = self.repo.create_chapter_generation_run(
            chapter_id, owner_id, "review", now=self.clock()
        )
        try:
            raw = self._run_with_heartbeat(chapter_id, owner_id, "review", prompt)
            review = _parse_review(raw)
            if not review["passed"] or review["issues"]:
                raise ValueError("chapter review rejected")
            revised = review["revised_blocks"]
            if set(revised) != FIXED_CHAPTER_KINDS:
                raise ValueError("review must return exact fixed chapter blocks")
            if any(not isinstance(value, str) or not value.strip() for value in revised.values()):
                raise ValueError("review fixed chapter blocks must be nonempty strings")
            for kind in ("knowledge_mermaid", "application_mermaid"):
                if not re.search(r"\b(graph|flowchart)\b", revised[kind]):
                    raise ValueError("review must contain two valid Mermaid blocks")
            cards = candidates["cards"]
            if "选题卡" not in cards:
                raise ValueError("review cards are invalid")
            blocks = {
                kind: (BLOCK_TITLES[kind], revised[kind], seq)
                for seq, kind in enumerate(BLOCK_TITLES)
            }
            chapter = self.repo.get_chapter(chapter_id)
            self.repo.publish_chapter_generation(
                chapter_id,
                owner_id,
                blocks,
                (f"{chapter.title} 写作选题", cards),
                run.id,
                raw,
                self._candidate_paths,
            )
        except Exception as exc:
            current = self.repo.get_chapter_generation_run(run.id)
            if current.status == "RUNNING":
                self.repo.finish_chapter_generation_run(
                    run.id, owner_id, "FAILED", error=_safe_chapter_error(exc), now=self.clock()
                )
            raise

    def rerun(self, chapter_id: str, round_key: str) -> None:
        if round_key not in ROUNDS:
            raise ValueError("unknown round")
        if self.repo.get_chapter(chapter_id) is None:
            raise ValueError("chapter not found")

        mark_topics_stale_for_chapter(
            self.repo,
            chapter_id,
            f"chapter {chapter_id} changed",
            round_keys=ROUNDS[ROUNDS.index(round_key) :],
        )
        try:
            self._run_round(chapter_id, round_key)
            _sync_chapter_markdown(self.repo, chapter_id)
        finally:
            _refresh_topics_for_chapter(self.repo, chapter_id)

    def _run_round(self, chapter_id: str, round_key: str) -> None:
        input_path = ""
        output_path = self.run_dir / f"{chapter_id}-{round_key}-output.md"
        output = ""
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            package = build_task_package(self.repo, chapter_id, round_key)
            if round_key not in CODEX_ROUNDS:
                input_path = write_task_package(package, self.run_dir)
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
                output=f"{type(exc).__name__}: intensive reading round failed",
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


def _safe_chapter_error(exc: Exception) -> str:
    value = str(exc)
    return (
        value[:300]
        if "/" not in value and "\\" not in value
        else f"{type(exc).__name__}: chapter round failed"
    )


def _parse_review(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("chapter review must return JSON") from exc
    if not isinstance(value, dict) or set(value) != {"passed", "issues", "revised_blocks"}:
        raise ValueError("chapter review contract is invalid")
    if not isinstance(value["passed"], bool) or not isinstance(value["issues"], list):
        raise ValueError("chapter review contract is invalid")
    if any(not isinstance(issue, str) for issue in value["issues"]):
        raise ValueError("chapter review contract is invalid")
    if not isinstance(value["revised_blocks"], dict):
        raise ValueError("chapter review contract is invalid")
    return value
