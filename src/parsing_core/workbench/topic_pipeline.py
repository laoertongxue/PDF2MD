from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import Annotated, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from parsing_core.workbench.executors import IntensiveReadingExecutor
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.topic_markdown_sync import (
    TopicMarkdownSyncError,
    sync_topic_map_markdown,
    sync_topic_markdown,
)
from parsing_core.workbench.topic_task_package import TopicTaskPackage, build_topic_task_package

TopicRoundKey = Literal[
    "alignment",
    "comparison",
    "plain_cases",
    "framework_application",
    "mermaid",
    "cards",
    "review",
]
TOPIC_ROUNDS: tuple[TopicRoundKey, ...] = (
    "alignment",
    "comparison",
    "plain_cases",
    "framework_application",
    "mermaid",
    "cards",
    "review",
)
FIXED_TOPIC_KINDS = (
    "overview",
    "linked_sources",
    "core_concepts",
    "viewpoint_comparison",
    "consensus_disagreements",
    "complementary_views",
    "plain_explanation",
    "textbook_cases",
    "real_world_problem_solving",
    "integrated_framework",
    "application_methods",
    "further_thinking",
    "knowledge_mermaid",
    "application_mermaid",
)
MAX_RESPONSE_CHARS = 40_000
MAX_TOTAL_OUTPUT_CHARS = 120_000
Text = Annotated[str, Field(min_length=1, max_length=12_000)]


class StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AlignmentOutput(StrictOutput):
    overview: Text
    linked_sources: Text
    core_concepts: Text


class ComparisonOutput(StrictOutput):
    viewpoint_comparison: Text
    consensus_disagreements: Text
    complementary_views: Text


class PlainCasesOutput(StrictOutput):
    plain_explanation: Text
    textbook_cases: Text
    real_world_problem_solving: Text


class FrameworkOutput(StrictOutput):
    integrated_framework: Text
    application_methods: Text
    further_thinking: Text


class MermaidOutput(StrictOutput):
    knowledge_diagram: Annotated[str, Field(min_length=1, max_length=20_000)]
    application_diagram: Annotated[str, Field(min_length=1, max_length=20_000)]


class TopicCardOutput(StrictOutput):
    card_type: Annotated[str, Field(min_length=1, max_length=40)]
    title: Annotated[str, Field(min_length=1, max_length=100)]
    content: Annotated[str, Field(min_length=1, max_length=2_000)]
    source_refs: Annotated[list[str], Field(min_length=1, max_length=20)]


class CardsOutput(StrictOutput):
    cards: Annotated[list[TopicCardOutput], Field(min_length=8, max_length=12)]


class ReviewOutput(StrictOutput):
    passed: Literal[True, False]
    issues: Annotated[list[str], Field(max_length=30)]


OUTPUT_MODELS: dict[TopicRoundKey, type[StrictOutput]] = {
    "alignment": AlignmentOutput,
    "comparison": ComparisonOutput,
    "plain_cases": PlainCasesOutput,
    "framework_application": FrameworkOutput,
    "mermaid": MermaidOutput,
    "cards": CardsOutput,
    "review": ReviewOutput,
}


@runtime_checkable
class _PromptValidatingExecutor(Protocol):
    def validate_prompt(self, task_key: str, prompt: str) -> None: ...


SOURCE_LABEL_RE = re.compile(r"\[《[^\]\n]+》·第\s*\d+\s*章\]")
MERMAID_HEADER_RE = re.compile(r"^(?:graph|flowchart)\s+(?:TB|TD|BT|RL|LR)$")
MERMAID_NODE_ID = r"[A-Za-z_][A-Za-z0-9_-]*"
MERMAID_NODE = rf"{MERMAID_NODE_ID}(?:\[[^\[\]<>]*\]|\([^()<>]*\)|\{{[^{{}}<>]*\}})?"
MERMAID_EDGE_RE = re.compile(
    rf"^{MERMAID_NODE}\s*(?:-->|---|-.->|==>|--?>\|[^|<>]+\||--\s+[^<>]+\s+-->)\s*{MERMAID_NODE}$"
)
MERMAID_NODE_RE = re.compile(rf"^{MERMAID_NODE}$")
MERMAID_SUBGRAPH_RE = re.compile(
    rf"^subgraph\s+(?:{MERMAID_NODE_ID}(?:\[[^\[\]<>]+\])?|[^\[\]{{}}()<>]+)$"
)
MERMAID_STYLE_RE = re.compile(
    rf"^(?:classDef\s+{MERMAID_NODE_ID}\s+[A-Za-z0-9_#.,:%;()\-\s]+|"
    rf"class\s+{MERMAID_NODE_ID}(?:,{MERMAID_NODE_ID})*\s+{MERMAID_NODE_ID}|"
    rf"style\s+{MERMAID_NODE_ID}\s+[A-Za-z0-9_#.,:%;()\-\s]+|"
    r"linkStyle\s+(?:\d+|default)(?:,\d+)*\s+[A-Za-z0-9_#.,:%;()\-\s]+)$"
)
MERMAID_ACTIVE_CONTENT_RE = re.compile(
    r"\burl\s*\(|\b(?:javascript|data|vbscript)\s*:", re.IGNORECASE
)
MERMAID_ACTIVE_STYLE_RE = re.compile(r"^(?:classDef|style|linkStyle)\s", re.IGNORECASE)
MERMAID_DIRECTIVE_RE = re.compile(r"^%%\s*\{", re.IGNORECASE)
MERMAID_PROMPT = """Mermaid output contract:
- Return raw Mermaid only, without code fences.
- Use only graph or flowchart with direction TB, TD, BT, RL, or LR.
- Use ASCII node IDs; node labels may use [], (), or {} and may contain Chinese text.
- Supported lines: %% comments, node declarations, edges, subgraph/end,
  classDef, class, style, and linkStyle.
- Include at least one edge. Do not use click, URLs, HTML, scripts, or other syntax.
"""


def _balanced_mermaid_delimiters(value: str) -> bool:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    quote = None
    escaped = False
    for char in value:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in pairs and (not stack or stack.pop() != pairs[char]):
            return False
    return not stack and quote is None


def validate_mermaid_subset(diagram: str) -> None:
    if "```" in diagram or not _balanced_mermaid_delimiters(diagram):
        raise ValueError("invalid Mermaid diagram")
    lines = diagram.splitlines()
    if not lines or not MERMAID_HEADER_RE.fullmatch(lines[0].strip()):
        raise ValueError("invalid Mermaid diagram")
    edge_count = 0
    subgraphs = 0
    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line:
            continue
        if MERMAID_DIRECTIVE_RE.match(line):
            raise ValueError("invalid Mermaid diagram")
        if line.startswith("%%"):
            continue
        lowered = line.lower()
        if (
            lowered.startswith("click ")
            or "http://" in lowered
            or "https://" in lowered
            or (MERMAID_ACTIVE_STYLE_RE.match(line) and MERMAID_ACTIVE_CONTENT_RE.search(line))
            or re.search(r"<\s*/?\s*(?:script|iframe|object|embed|a)\b", lowered)
        ):
            raise ValueError("invalid Mermaid diagram")
        if line == "end":
            if subgraphs == 0:
                raise ValueError("invalid Mermaid diagram")
            subgraphs -= 1
        elif MERMAID_SUBGRAPH_RE.fullmatch(line):
            subgraphs += 1
        elif MERMAID_EDGE_RE.fullmatch(line):
            edge_count += 1
        elif MERMAID_NODE_RE.fullmatch(line) or MERMAID_STYLE_RE.fullmatch(line):
            continue
        else:
            raise ValueError("invalid Mermaid diagram")
    if edge_count < 1 or subgraphs:
        raise ValueError("invalid Mermaid diagram")


def _topic_prompt(round_key: TopicRoundKey, package: TopicTaskPackage) -> str:
    instruction = MERMAID_PROMPT if round_key == "mermaid" else "Return strict JSON only."
    prompt = json.dumps(
        {"instructions": instruction, "task_package": package.model_dump()},
        ensure_ascii=False,
    )
    if len(prompt) > 210_000:
        raise ValueError("topic prompt exceeds size limit")
    return prompt


def _parse_output(round_key: TopicRoundKey, raw: str) -> StrictOutput:
    if len(raw) > MAX_RESPONSE_CHARS:
        raise ValueError("topic round response exceeds size limit")
    try:
        value: object = json.loads(raw)
        output_model = OUTPUT_MODELS[round_key]
        return output_model.model_validate(value)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid {round_key} JSON output") from exc


OutputT = TypeVar("OutputT", bound=StrictOutput)


def _require_output(  # noqa: UP047 - Python 3.9 compile compatibility
    outputs: Mapping[TopicRoundKey, StrictOutput],
    round_key: TopicRoundKey,
    output_type: type[OutputT],
) -> OutputT:
    output = outputs.get(round_key)
    if not isinstance(output, output_type):
        raise ValueError(f"topic {round_key} output is missing")
    return output


def _topic_text_blocks(outputs: Mapping[TopicRoundKey, StrictOutput]) -> dict[str, str]:
    alignment = _require_output(outputs, "alignment", AlignmentOutput)
    comparison = _require_output(outputs, "comparison", ComparisonOutput)
    plain_cases = _require_output(outputs, "plain_cases", PlainCasesOutput)
    framework = _require_output(outputs, "framework_application", FrameworkOutput)
    return {
        "overview": alignment.overview,
        "linked_sources": alignment.linked_sources,
        "core_concepts": alignment.core_concepts,
        "viewpoint_comparison": comparison.viewpoint_comparison,
        "consensus_disagreements": comparison.consensus_disagreements,
        "complementary_views": comparison.complementary_views,
        "plain_explanation": plain_cases.plain_explanation,
        "textbook_cases": plain_cases.textbook_cases,
        "real_world_problem_solving": plain_cases.real_world_problem_solving,
        "integrated_framework": framework.integrated_framework,
        "application_methods": framework.application_methods,
        "further_thinking": framework.further_thinking,
    }


def _validate_local(outputs: Mapping[TopicRoundKey, StrictOutput], labels: list[str]) -> None:
    if len(labels) != len(set(labels)):
        raise ValueError("topic source labels are not unique")
    legal_labels = set(labels)
    blocks = _topic_text_blocks(outputs)
    if set(blocks) != set(FIXED_TOPIC_KINDS[:12]) or any(
        not value.strip() for value in blocks.values()
    ):
        raise ValueError("topic text blocks are incomplete")
    for value in blocks.values():
        found = set(SOURCE_LABEL_RE.findall(value))
        if not found <= legal_labels:
            raise ValueError("topic output contains unknown source label")
    if not legal_labels <= set(SOURCE_LABEL_RE.findall(blocks["linked_sources"])):
        raise ValueError("linked sources are incomplete")
    mermaid = _require_output(outputs, "mermaid", MermaidOutput)
    for diagram in (mermaid.knowledge_diagram, mermaid.application_diagram):
        validate_mermaid_subset(diagram)
    cards = _require_output(outputs, "cards", CardsOutput).cards
    seen_labels = set()
    for card in cards:
        refs = set(card.source_refs)
        if not refs <= legal_labels:
            raise ValueError("topic card contains unknown source reference")
        seen_labels.update(refs)
    all_text = "\n".join(blocks.values())
    covered = {label for label in labels if label in all_text} | seen_labels
    if covered != legal_labels:
        raise ValueError("not every source is represented")
    if sum(len(item.model_dump_json()) for item in outputs.values()) > MAX_TOTAL_OUTPUT_CHARS:
        raise ValueError("topic output exceeds total size limit")


def _validate_round(round_key: TopicRoundKey, output: StrictOutput, labels: list[str]) -> None:
    if not isinstance(output, OUTPUT_MODELS[round_key]):
        raise ValueError(f"invalid {round_key} output type")
    legal_labels = set(labels)
    if isinstance(output, AlignmentOutput):
        values = (output.overview, output.linked_sources, output.core_concepts)
        for value in values:
            if not set(SOURCE_LABEL_RE.findall(value)) <= legal_labels:
                raise ValueError("topic output contains unknown source label")
        if not legal_labels <= set(SOURCE_LABEL_RE.findall(output.linked_sources)):
            raise ValueError("linked sources are incomplete")
    elif isinstance(output, ComparisonOutput):
        for value in (
            output.viewpoint_comparison,
            output.consensus_disagreements,
            output.complementary_views,
        ):
            if not set(SOURCE_LABEL_RE.findall(value)) <= legal_labels:
                raise ValueError("topic output contains unknown source label")
    elif isinstance(output, PlainCasesOutput):
        for value in (
            output.plain_explanation,
            output.textbook_cases,
            output.real_world_problem_solving,
        ):
            if not set(SOURCE_LABEL_RE.findall(value)) <= legal_labels:
                raise ValueError("topic output contains unknown source label")
    elif isinstance(output, FrameworkOutput):
        for value in (
            output.integrated_framework,
            output.application_methods,
            output.further_thinking,
        ):
            if not set(SOURCE_LABEL_RE.findall(value)) <= legal_labels:
                raise ValueError("topic output contains unknown source label")
    elif isinstance(output, MermaidOutput):
        validate_mermaid_subset(output.knowledge_diagram)
        validate_mermaid_subset(output.application_diagram)
    elif isinstance(output, CardsOutput):
        for card in output.cards:
            if not set(card.source_refs) <= legal_labels:
                raise ValueError("topic card contains unknown source reference")


def _assert_unique_source_labels(package: TopicTaskPackage) -> None:
    labels = [chapter.source_label for chapter in package.source_chapters]
    if len(labels) != len(set(labels)):
        raise ValueError("topic source labels are not unique")


def _safe_error(exc: BaseException) -> str:
    if (
        isinstance(exc, (ValueError, ValidationError))
        and "/" not in str(exc)
        and "\\" not in str(exc)
    ):
        return str(exc)[:300]
    return f"{type(exc).__name__}: topic round execution failed"


class TopicFusionPipeline:
    def __init__(
        self,
        repo: WorkbenchRepository,
        executor: IntensiveReadingExecutor,
        *,
        clock: Callable[[], int] | None = None,
        lease_ttl: int = 7_200,
        heartbeat_interval: float | None = None,
        markdown_sync_lease_ttl: int = 600,
    ) -> None:
        self.repo = repo
        self.executor = executor
        self.clock: Callable[[], int] = clock or (lambda: int(time.time()))
        self.lease_ttl = lease_ttl
        self.markdown_sync_lease_ttl = markdown_sync_lease_ttl
        self.heartbeat_interval = (
            min(60.0, lease_ttl / 3) if heartbeat_interval is None else heartbeat_interval
        )
        if self.lease_ttl <= 0 or self.heartbeat_interval <= 0 or self.markdown_sync_lease_ttl <= 0:
            raise ValueError("lease ttl and heartbeat interval must be positive")

    def _heartbeat(self, topic_id: str, owner_id: str) -> None:
        self.repo.heartbeat_topic_generation(
            topic_id,
            owner_id,
            now=self.clock(),
            lease_ttl=self.lease_ttl,
        )

    def retry_markdown_sync(self, topic_id: str) -> None:
        topic = self.repo.get_topic(topic_id)
        if topic is None:
            raise ValueError("topic not found")
        if self.repo.get_topic_markdown_sync_state(topic_id) is None:
            self.repo.set_topic_markdown_sync_state(topic_id, "PENDING")
        blocks = self.repo.list_topic_note_blocks(topic_id)
        cards = self.repo.list_topic_cards(topic_id)
        complete = {block.kind for block in blocks} == set(FIXED_TOPIC_KINDS) and 8 <= len(
            cards
        ) <= 12
        self._sync_published_markdown(
            topic_id,
            mapping_only=topic.status != "COMPLETED" or not complete,
        )

    def _sync_published_markdown(self, topic_id: str, *, mapping_only: bool = False) -> None:
        claim = self.repo.claim_topic_markdown_sync(
            topic_id, now=self.clock(), lease_ttl=self.markdown_sync_lease_ttl
        )

        failed_error: BaseException | None = None
        try:
            sync = sync_topic_map_markdown if mapping_only else sync_topic_markdown
            sync(
                self.repo,
                topic_id,
                owner_id=claim.owner_id,
                clock=self.clock,
                lease_ttl=self.markdown_sync_lease_ttl,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            failed_error = exc
        if failed_error is not None:
            try:
                self.repo.finish_topic_markdown_sync(
                    topic_id,
                    claim.owner_id,
                    "FAILED",
                    "TOPIC_MARKDOWN_PUBLICATION_FAILED",
                    now=self.clock(),
                )
            except Exception:
                pass
            raise TopicMarkdownSyncError() from None

    def _run_executor_with_heartbeat(
        self, topic_id: str, owner_id: str, round_key: str, prompt: str
    ) -> str:
        stop = threading.Event()
        lost = threading.Event()
        heartbeat_errors: list[Exception] = []

        def renew() -> None:
            while not stop.wait(self.heartbeat_interval):
                try:
                    self._heartbeat(topic_id, owner_id)
                except Exception as exc:
                    heartbeat_errors.append(exc)
                    lost.set()
                    return

        thread = threading.Thread(
            target=renew,
            name=f"topic-lease-heartbeat-{topic_id}",
            daemon=True,
        )
        thread.start()
        try:
            output = self.executor.run(round_key, prompt)
        finally:
            stop.set()
            thread.join()
        if lost.is_set():
            raise ValueError("topic generation lease lost") from heartbeat_errors[0]
        self._heartbeat(topic_id, owner_id)
        return output

    def run(self, topic_id: str) -> None:
        initial = build_topic_task_package(self.repo, topic_id)
        _assert_unique_source_labels(initial)
        start = self.repo.start_topic_generation(
            topic_id,
            initial.input_fingerprint,
            now=self.clock(),
            lease_ttl=self.lease_ttl,
        )
        outputs: dict[TopicRoundKey, StrictOutput] = {}
        previous: dict[str, object] = {}
        labels = [chapter.source_label for chapter in initial.source_chapters]
        try:
            for round_key in TOPIC_ROUNDS:
                package = build_topic_task_package(self.repo, topic_id, previous)
                prompt = _topic_prompt(round_key, package)
                run = self.repo.create_topic_run(topic_id, round_key, start.input_fingerprint)
                try:
                    self._heartbeat(topic_id, start.owner_id)
                    if isinstance(self.executor, _PromptValidatingExecutor):
                        self.executor.validate_prompt(round_key, prompt)
                    raw = self._run_executor_with_heartbeat(
                        topic_id, start.owner_id, round_key, prompt
                    )
                    parsed = _parse_output(round_key, raw)
                    _validate_round(round_key, parsed, labels)
                    if round_key == "review":
                        if not isinstance(parsed, ReviewOutput):
                            raise ValueError("invalid review output type")
                        if not parsed.passed or parsed.issues:
                            raise ValueError("topic review rejected")
                    self._heartbeat(topic_id, start.owner_id)
                    outputs[round_key] = parsed
                    previous[round_key] = parsed.model_dump()
                    if round_key != "review":
                        self.repo.finish_topic_run(run.id, "COMPLETED", output=raw)
                        continue
                    _validate_local(outputs, labels)
                    mermaid = _require_output(outputs, "mermaid", MermaidOutput)
                    cards_output = _require_output(outputs, "cards", CardsOutput)
                    blocks = _topic_text_blocks(outputs)
                    blocks.update(
                        {
                            "knowledge_mermaid": mermaid.knowledge_diagram,
                            "application_mermaid": mermaid.application_diagram,
                        }
                    )
                    cards = [
                        {
                            "card_type": card.card_type,
                            "title": card.title,
                            "content": card.content,
                            "source_refs_json": card.source_refs,
                        }
                        for card in cards_output.cards
                    ]
                    self.repo.publish_topic_generation(
                        topic_id,
                        start.input_fingerprint,
                        start.stale_reason_baseline,
                        blocks,
                        cards,
                        review_run_id=run.id,
                        owner_id=start.owner_id,
                        review_output=raw,
                        now=self.clock(),
                    )
                    self._sync_published_markdown(topic_id)
                except Exception as exc:
                    current = next(
                        item for item in self.repo.list_topic_runs(topic_id) if item.id == run.id
                    )
                    if current.status == "RUNNING":
                        self.repo.finish_topic_run(run.id, "FAILED", error=_safe_error(exc))
                    raise
        except Exception:
            try:
                self.repo.fail_topic_generation(topic_id, start.owner_id)
            except ValueError as lease_error:
                if "lease lost" not in str(lease_error):
                    raise
            raise
