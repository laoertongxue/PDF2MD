from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict, cast
from uuid import uuid4

from parsing_core.workbench.executors import IntensiveReadingExecutor
from parsing_core.workbench.markdown_sync import (
    ChapterMarkdownSyncError,
    publish_chapter_markdown,
    recover_chapter_publication_journals,
    sync_chapter_markdown,
)
from parsing_core.workbench.models import ChapterGenerationStart
from parsing_core.workbench.repository import ChapterGenerationConflictError, WorkbenchRepository
from parsing_core.workbench.schema import CHAPTER_SYNC_PENDING
from parsing_core.workbench.task_package import (
    READING_RULES,
    TASK_PACKAGE_SCHEMA_VERSION,
    TaskPackage,
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
CITATION_RE = re.compile(r"\[((?:src|att):[^\]\s]+)\]")
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
CHAPTER_CANDIDATE_SCHEMA_VERSION = 1
MAX_CHECKPOINT_IDENTITY_BYTES = 16 * 1024
MAX_CHECKPOINT_IDENTITY_DEPTH = 8
MAX_CHECKPOINT_IDENTITY_NODES = 512
MAX_CHECKPOINT_IDENTITY_ITEMS = 128
_MISSING_CHECKPOINT_IDENTITY = object()
_CHECKPOINT_SECRET_RE = re.compile(
    r"(?i)(?:\bsk-[A-Za-z0-9_-]{12,}\b|\bbearer\s+[^\s,;]+|"
    r"\b(?:api[_-]?key|access[_-]?token|authorization|password|secret)\s*[:=])"
)
_CHECKPOINT_SECRET_KEY_RE = re.compile(
    r"(?i)^(?:api[_-]?key|access[_-]?token|authorization|password|secret)$"
)
_CHECKPOINT_SECRET_KEYS = frozenset(
    {
        "password",
        "passphrase",
        "secret",
        "clientsecret",
        "token",
        "accesstoken",
        "apikey",
        "key",
        "authorization",
        "credential",
    }
)


class ExecutorCheckpointIdentityError(ValueError):
    code = "EXECUTOR_CHECKPOINT_IDENTITY_INVALID"
    message = "executor checkpoint identity is invalid"

    def __init__(self) -> None:
        super().__init__(self.message)


def _normalized_checkpoint_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _canonical_checkpoint_identity(value: object) -> tuple[object, bytes]:
    if type(value) not in (str, dict):
        raise ValueError("executor checkpoint identity is invalid")
    nodes = 0
    active: set[int] = set()

    def validate(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if depth > MAX_CHECKPOINT_IDENTITY_DEPTH or nodes > MAX_CHECKPOINT_IDENTITY_NODES:
            raise ValueError("executor checkpoint identity is invalid")
        if item is None or type(item) is bool:
            return
        if type(item) is int:
            if not -(2**63) <= item <= 2**63 - 1:
                raise ValueError("executor checkpoint identity is invalid")
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("executor checkpoint identity is invalid")
            return
        if type(item) is str:
            try:
                encoded_item = item.encode("utf-8")
            except UnicodeError:
                raise ValueError("executor checkpoint identity is invalid") from None
            if (
                not encoded_item
                or len(encoded_item) > MAX_CHECKPOINT_IDENTITY_BYTES
                or _CHECKPOINT_SECRET_RE.search(item)
            ):
                raise ValueError("executor checkpoint identity is invalid")
            return
        if type(item) not in (dict, list):
            raise ValueError("executor checkpoint identity is invalid")
        identity = id(item)
        if identity in active:
            raise ValueError("executor checkpoint identity is invalid")
        active.add(identity)
        try:
            if type(item) is list:
                items = cast(list[object], item)
                if not items or len(items) > MAX_CHECKPOINT_IDENTITY_ITEMS:
                    raise ValueError("executor checkpoint identity is invalid")
                for child in items:
                    validate(child, depth + 1)
                return
            mapping = cast(dict[object, object], item)
            if not mapping or len(mapping) > MAX_CHECKPOINT_IDENTITY_ITEMS:
                raise ValueError("executor checkpoint identity is invalid")
            for key, child in mapping.items():
                if type(key) is not str:
                    raise ValueError("executor checkpoint identity is invalid")
                safe_key = key
                if (
                    _CHECKPOINT_SECRET_KEY_RE.fullmatch(safe_key)
                    or _normalized_checkpoint_key(safe_key) in _CHECKPOINT_SECRET_KEYS
                ):
                    raise ValueError("executor checkpoint identity is invalid")
                validate(safe_key, depth + 1)
                validate(child, depth + 1)
        finally:
            active.remove(identity)

    validate(value, 0)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("executor checkpoint identity is invalid") from None
    if len(encoded) > MAX_CHECKPOINT_IDENTITY_BYTES:
        raise ValueError("executor checkpoint identity is invalid")
    return json.loads(encoded), encoded


def _read_executor_checkpoint_identity(
    executor: IntensiveReadingExecutor,
) -> tuple[object, bytes] | None:
    failed = False
    first: object | None = None
    first_encoded = b""
    second_encoded = b""
    try:
        first_attribute = getattr(executor, "checkpoint_identity", _MISSING_CHECKPOINT_IDENTITY)
        if first_attribute is _MISSING_CHECKPOINT_IDENTITY:
            return None
        first_value = first_attribute() if callable(first_attribute) else first_attribute
        first, first_encoded = _canonical_checkpoint_identity(first_value)
        second_attribute = getattr(executor, "checkpoint_identity", _MISSING_CHECKPOINT_IDENTITY)
        if second_attribute is _MISSING_CHECKPOINT_IDENTITY:
            raise ValueError("executor checkpoint identity is invalid")
        second_value = second_attribute() if callable(second_attribute) else second_attribute
        _second, second_encoded = _canonical_checkpoint_identity(second_value)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        failed = True
    if failed or first_encoded != second_encoded:
        raise ExecutorCheckpointIdentityError()
    return first, first_encoded


def _executor_checkpoint_descriptor(
    executor: IntensiveReadingExecutor,
) -> tuple[object, bytes | None]:
    identity = _read_executor_checkpoint_identity(executor)
    from parsing_core.workbench.deepseek import DeepSeekExecutor

    if type(executor) is DeepSeekExecutor:
        if identity is None:
            raise ExecutorCheckpointIdentityError()
        value, encoded = identity
        return (
            {
                "checkpoint_reuse": "trusted-built-in",
                "type": "parsing_core.workbench.deepseek.DeepSeekExecutor",
                "identity": value,
            },
            encoded,
        )
    return (
        {
            "checkpoint_reuse": "disabled",
            "nonce": uuid4().hex,
        },
        None,
    )


def _chapter_configuration_binding(
    executor: IntensiveReadingExecutor,
    override: str | None,
) -> tuple[str, bytes | None]:
    executor_descriptor, trusted_identity = _executor_checkpoint_descriptor(executor)
    value = {
        "executor": executor_descriptor,
        "override": override,
        "template": READING_RULES,
        "task_package_schema_version": TASK_PACKAGE_SCHEMA_VERSION,
        "rounds": ROUNDS,
        "block_titles": BLOCK_TITLES,
        "fixed_kinds": sorted(FIXED_CHAPTER_KINDS),
        "schema_version": CHAPTER_CANDIDATE_SCHEMA_VERSION,
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), trusted_identity


def _chapter_configuration_fingerprint(
    executor: IntensiveReadingExecutor,
    override: str | None,
) -> str:
    return _chapter_configuration_binding(executor, override)[0]


def _refresh_topics_for_chapter(repo: WorkbenchRepository, chapter_id: str) -> None:
    for topic in repo.list_topics_for_chapter(chapter_id):
        refresh_topic_status(repo, topic.id)


class _ChapterReview(TypedDict):
    passed: bool
    issues: list[str]
    revised_blocks: dict[object, object]


def _sync_chapter_markdown(repo: WorkbenchRepository, chapter_id: str) -> None:
    failed = False
    try:
        sync_chapter_markdown(repo, chapter_id)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        failed = True
    if failed:
        raise ChapterMarkdownSyncError() from None


def _publish_chapter_markdown(
    repo: WorkbenchRepository,
    chapter_id: str,
    owner_id: str,
    review_run_id: str,
    publication_id: str,
    *,
    clock: Callable[[], int],
) -> None:
    failed = False
    try:
        publish_chapter_markdown(
            repo,
            chapter_id,
            owner_id,
            review_run_id,
            publication_id,
            clock=clock,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        failed = True
    if failed:
        raise ChapterMarkdownSyncError() from None


def _release_generation_claim(
    repo: WorkbenchRepository,
    chapter_id: str,
    owner_id: str,
    error: str,
    *,
    now: int | None = None,
) -> None:
    try:
        repo.release_chapter_generation(
            chapter_id,
            owner_id,
            error=error,
            now=now,
        )
    except ChapterGenerationConflictError:
        pass


def _sync_claimed_chapter_markdown(
    repo: WorkbenchRepository,
    chapter_id: str,
    start: ChapterGenerationStart,
    *,
    now: int,
    lease_ttl: int,
    clock: Callable[[], int] | None = None,
) -> None:
    failed = False
    try:
        pending = repo.pending_chapter_markdown_sync(chapter_id)
        if pending is None or pending["owner_id"] != start.owner_id:
            raise ChapterGenerationConflictError(
                "lease_lost",
                "chapter generation lease lost",
            )
        repo.heartbeat_chapter_generation(
            chapter_id,
            start.owner_id,
            now=now,
            lease_ttl=lease_ttl,
        )
        _publish_chapter_markdown(
            repo,
            chapter_id,
            start.owner_id,
            pending["review_run_id"],
            pending["publication_id"],
            clock=clock or (lambda: now),
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        failed = True
    if failed:
        raise ChapterMarkdownSyncError()


def recover_pending_chapter_markdown_sync(repo: WorkbenchRepository, chapter_id: str) -> bool:
    if repo.pending_chapter_markdown_sync(chapter_id) is None:
        return False
    recover_chapter_publication_journals(repo, chapter_id)
    now = int(time.time())
    start = repo.start_chapter_generation(chapter_id, now=now)
    if start.chapter.status != CHAPTER_SYNC_PENDING:
        _release_generation_claim(
            repo,
            chapter_id,
            start.owner_id,
            "staged chapter input changed",
            now=now,
        )
        return False
    try:
        _sync_claimed_chapter_markdown(
            repo,
            chapter_id,
            start,
            now=now,
            lease_ttl=7_200,
            clock=lambda: int(time.time()),
        )
    except Exception as exc:
        _release_generation_claim(
            repo,
            chapter_id,
            start.owner_id,
            _safe_chapter_error(exc),
            now=now,
        )
        raise
    return True


class IntensiveReadingPipeline:
    def __init__(
        self,
        repo: WorkbenchRepository,
        executor: IntensiveReadingExecutor,
        run_dir: str | Path,
        *,
        clock: Callable[[], int] | None = None,
        lease_ttl: int = 7_200,
        heartbeat_interval: float | None = None,
        generation_start: ChapterGenerationStart | None = None,
        configuration_fingerprint: str | None = None,
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
        self._run_metadata: dict[str, tuple[str, tuple[str, ...]]] = {}
        self._input_fingerprint = ""
        self._citation_ids: tuple[str, ...] = ()
        self.generation_start = generation_start
        (
            self.configuration_fingerprint,
            self._trusted_executor_checkpoint_identity,
        ) = _chapter_configuration_binding(
            executor,
            configuration_fingerprint,
        )

    def _verify_executor_configuration(self) -> None:
        expected = self._trusted_executor_checkpoint_identity
        if expected is None:
            return
        current = _read_executor_checkpoint_identity(self.executor)
        if current is None or current[1] != expected:
            raise ExecutorCheckpointIdentityError()

    def run_all(self, chapter_id: str) -> None:
        chapter = self.repo.get_chapter(chapter_id)
        if chapter is None:
            raise ValueError("chapter not found")
        start = self.generation_start or self.repo.start_chapter_generation(
            chapter_id,
            now=self.clock(),
            lease_ttl=self.lease_ttl,
        )
        try:
            if start.chapter.id != chapter_id:
                raise ValueError("chapter generation claim does not match chapter")
            if start.chapter.status == CHAPTER_SYNC_PENDING:
                now = self.clock()
                _sync_claimed_chapter_markdown(
                    self.repo,
                    chapter_id,
                    start,
                    now=now,
                    lease_ttl=self.lease_ttl,
                    clock=self.clock,
                )
                return
            mark_topics_stale_for_chapter(
                self.repo,
                chapter_id,
                f"chapter {chapter_id} changed",
                round_keys=ROUNDS,
            )
            recovered_rounds = self._restore_candidate_prefix(chapter_id, start.owner_id)
            for round_key in ROUNDS[:-1]:
                if round_key not in recovered_rounds:
                    self._run_candidate(chapter_id, start.owner_id, round_key)
            self._run_review_and_stage(chapter_id, start.owner_id)
            _sync_claimed_chapter_markdown(
                self.repo,
                chapter_id,
                start,
                now=self.clock(),
                lease_ttl=self.lease_ttl,
                clock=self.clock,
            )
        except Exception as exc:
            _release_generation_claim(
                self.repo,
                chapter_id,
                start.owner_id,
                _safe_chapter_error(exc),
            )
            raise

    def _run_with_heartbeat(
        self, chapter_id: str, owner_id: str, round_key: str, prompt: str
    ) -> str:
        self._verify_executor_configuration()
        stop = threading.Event()
        errors: list[Exception] = []

        def renew() -> None:
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
        self._verify_executor_configuration()
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
        self._accept_package(package)
        input_path = ""
        if round_key not in CODEX_ROUNDS:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            input_path = write_task_package(package, self.run_dir)
        try:
            output = self._run_with_heartbeat(chapter_id, owner_id, round_key, package.content)
            _validate_candidate_output(round_key, output, self._citation_ids)
            output_path = self.run_dir / f"{chapter_id}-{round_key}-output.md"
            self.run_dir.mkdir(parents=True, exist_ok=True)
            output_path.write_text(output, encoding="utf-8")
            self._candidate_paths[round_key] = (input_path, str(output_path))
            self.repo.finish_chapter_generation_run(
                run.id,
                owner_id,
                "COMPLETED",
                output=output,
                input_fingerprint=package.input_fingerprint,
                citation_ids=package.citation_ids,
                configuration_fingerprint=self.configuration_fingerprint,
                task_fingerprint=hashlib.sha256(package.content.encode("utf-8")).hexdigest(),
                now=self.clock(),
            )
        except Exception as exc:
            current = self.repo.get_chapter_generation_run(run.id)
            if current is None:
                raise RuntimeError("chapter generation run disappeared") from exc
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
                        package.input_fingerprint,
                        package.citation_ids,
                    )
            raise

    def _restore_candidate_prefix(self, chapter_id: str, owner_id: str) -> set[str]:
        self._verify_executor_configuration()
        checkpoints = self.repo.chapter_generation_checkpoints(chapter_id, owner_id)
        by_round = {checkpoint["round_key"]: checkpoint for checkpoint in checkpoints}
        restored: set[str] = set()
        for round_key in ROUNDS[:-1]:
            self._verify_executor_configuration()
            checkpoint = by_round.get(round_key)
            if checkpoint is None:
                break
            package = build_task_package(self.repo, chapter_id, round_key)
            self._accept_package(package)
            output = checkpoint["output"]
            if (
                checkpoint["input_fingerprint"] != package.input_fingerprint
                or checkpoint["citation_ids"] != package.citation_ids
                or checkpoint["configuration_fingerprint"] != self.configuration_fingerprint
                or checkpoint["task_fingerprint"]
                != hashlib.sha256(package.content.encode("utf-8")).hexdigest()
                or checkpoint["output_fingerprint"]
                != hashlib.sha256(output.encode("utf-8")).hexdigest()
            ):
                break
            try:
                _validate_candidate_output(round_key, output, package.citation_ids)
            except ValueError:
                break
            restored.add(round_key)
        invalid_rounds = [
            checkpoint["round_key"]
            for checkpoint in checkpoints
            if checkpoint["round_key"] not in restored
        ]
        self.repo.discard_chapter_generation_checkpoints(
            chapter_id,
            owner_id,
            invalid_rounds,
        )
        return restored

    def _run_review_and_stage(self, chapter_id: str, owner_id: str) -> None:
        candidates = self.repo.chapter_generation_candidates(chapter_id, owner_id)
        prompt = build_review_package(self.repo, chapter_id, candidates)
        run = self.repo.create_chapter_generation_run(
            chapter_id, owner_id, "review", now=self.clock()
        )
        try:
            raw = self._run_with_heartbeat(chapter_id, owner_id, "review", prompt)
            _validate_output_citations(raw, self._citation_ids)
            review = _parse_review(raw)
            if not review["passed"] or review["issues"]:
                raise ValueError("chapter review rejected")
            revised_values = review["revised_blocks"]
            if set(revised_values) != FIXED_CHAPTER_KINDS:
                raise ValueError("review must return exact fixed chapter blocks")
            if any(
                not isinstance(value, str) or not value.strip() for value in revised_values.values()
            ):
                raise ValueError("review fixed chapter blocks must be nonempty strings")
            revised = {
                key: value
                for key, value in revised_values.items()
                if isinstance(key, str) and isinstance(value, str)
            }
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
            if chapter is None:
                raise ValueError("chapter not found")
            self.repo.publish_chapter_generation(
                chapter_id,
                owner_id,
                blocks,
                (f"{chapter.title} 写作选题", cards),
                run.id,
                raw,
                self._candidate_paths,
                self._run_metadata,
                self._input_fingerprint,
                now=self.clock(),
            )
            return
        except Exception as exc:
            current = self.repo.get_chapter_generation_run(run.id)
            if current is None:
                raise RuntimeError("chapter generation run disappeared") from exc
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
            self._input_fingerprint = package.input_fingerprint
            self._citation_ids = package.citation_ids
            if round_key not in CODEX_ROUNDS:
                input_path = write_task_package(package, self.run_dir)
            self._verify_executor_configuration()
            output = self.executor.run(round_key, package.content)
            self._verify_executor_configuration()
            _validate_output_citations(output, package.citation_ids)
            if self.repo.chapter_input_snapshot(chapter_id)[1] != package.input_fingerprint:
                raise ValueError("chapter input fingerprint changed")
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
                input_fingerprint=package.input_fingerprint,
                citation_ids=package.citation_ids,
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
                input_fingerprint=package.input_fingerprint if "package" in locals() else "",
                citation_ids=package.citation_ids if "package" in locals() else (),
            )
            raise

    def _accept_package(self, package: TaskPackage) -> None:
        if not self._input_fingerprint:
            self._input_fingerprint = package.input_fingerprint
            self._citation_ids = package.citation_ids
        elif (
            package.input_fingerprint != self._input_fingerprint
            or package.citation_ids != self._citation_ids
        ):
            raise ValueError("chapter input fingerprint changed")
        self._run_metadata[package.round_key] = (
            package.input_fingerprint,
            package.citation_ids,
        )
        self._run_metadata["review"] = (
            package.input_fingerprint,
            package.citation_ids,
        )

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


def _validate_candidate_output(
    round_key: str,
    output: str,
    citation_ids: tuple[str, ...],
) -> None:
    if not output.strip():
        raise ValueError("chapter candidate must contain content")
    _validate_output_citations(output, citation_ids)
    if round_key == "mermaid":
        _extract_mermaid_diagrams(output)
    if round_key == "cards" and "选题卡" not in output:
        raise ValueError("chapter cards candidate is invalid")


def _safe_chapter_error(exc: Exception) -> str:
    value = str(exc)
    return (
        value[:300]
        if "/" not in value and "\\" not in value
        else f"{type(exc).__name__}: chapter round failed"
    )


def _validate_output_citations(output: str, allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(CITATION_RE.findall(output)) - set(allowed))
    if unknown:
        raise ValueError("unknown citation id")


def _parse_review(raw: str) -> _ChapterReview:
    try:
        value: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("chapter review must return JSON") from exc
    if not isinstance(value, dict) or set(value) != {"passed", "issues", "revised_blocks"}:
        raise ValueError("chapter review contract is invalid")
    passed: object = value["passed"]
    issues: object = value["issues"]
    revised: object = value["revised_blocks"]
    if not isinstance(passed, bool) or not isinstance(issues, list):
        raise ValueError("chapter review contract is invalid")
    if any(not isinstance(issue, str) for issue in issues):
        raise ValueError("chapter review contract is invalid")
    if not isinstance(revised, dict):
        raise ValueError("chapter review contract is invalid")
    return {
        "passed": passed,
        "issues": [issue for issue in issues if isinstance(issue, str)],
        "revised_blocks": dict(revised),
    }
