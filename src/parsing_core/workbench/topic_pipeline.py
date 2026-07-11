import json
import re
import threading
import time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.topic_markdown_sync import (
    TopicMarkdownSyncError,
    sync_topic_map_markdown,
    sync_topic_markdown,
)
from parsing_core.workbench.topic_task_package import build_topic_task_package

TOPIC_ROUNDS = (
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


OUTPUT_MODELS = {
    "alignment": AlignmentOutput,
    "comparison": ComparisonOutput,
    "plain_cases": PlainCasesOutput,
    "framework_application": FrameworkOutput,
    "mermaid": MermaidOutput,
    "cards": CardsOutput,
    "review": ReviewOutput,
}
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
        if not line or line.startswith("%%"):
            continue
        lowered = line.lower()
        if (
            lowered.startswith("click ")
            or "http://" in lowered
            or "https://" in lowered
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


def _topic_prompt(round_key: str, package: Any) -> str:
    instruction = MERMAID_PROMPT if round_key == "mermaid" else "Return strict JSON only."
    prompt = json.dumps(
        {"instructions": instruction, "task_package": package.model_dump()},
        ensure_ascii=False,
    )
    if len(prompt) > 210_000:
        raise ValueError("topic prompt exceeds size limit")
    return prompt


def _parse_output(round_key: str, raw: str) -> StrictOutput:
    if len(raw) > MAX_RESPONSE_CHARS:
        raise ValueError("topic round response exceeds size limit")
    try:
        value = json.loads(raw)
        return OUTPUT_MODELS[round_key].model_validate(value)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid {round_key} JSON output") from exc


def _validate_local(outputs: dict[str, StrictOutput], labels: list[str]) -> None:
    if len(labels) != len(set(labels)):
        raise ValueError("topic source labels are not unique")
    legal_labels = set(labels)
    blocks: dict[str, str] = {}
    for round_key in ("alignment", "comparison", "plain_cases", "framework_application"):
        blocks.update(outputs[round_key].model_dump())
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
    mermaid = outputs["mermaid"]
    for diagram in (mermaid.knowledge_diagram, mermaid.application_diagram):
        validate_mermaid_subset(diagram)
    cards = outputs["cards"].cards
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


def _validate_round(round_key: str, output: StrictOutput, labels: list[str]) -> None:
    legal_labels = set(labels)
    if round_key in {"alignment", "comparison", "plain_cases", "framework_application"}:
        for value in output.model_dump().values():
            if not set(SOURCE_LABEL_RE.findall(value)) <= legal_labels:
                raise ValueError("topic output contains unknown source label")
        if round_key == "alignment" and not legal_labels <= set(
            SOURCE_LABEL_RE.findall(output.linked_sources)
        ):
            raise ValueError("linked sources are incomplete")
    elif round_key == "mermaid":
        validate_mermaid_subset(output.knowledge_diagram)
        validate_mermaid_subset(output.application_diagram)
    elif round_key == "cards":
        for card in output.cards:
            if not set(card.source_refs) <= legal_labels:
                raise ValueError("topic card contains unknown source reference")


def _assert_unique_source_labels(package: Any) -> None:
    labels = [chapter.source_label for chapter in package.source_chapters]
    if len(labels) != len(set(labels)):
        raise ValueError("topic source labels are not unique")


def _safe_error(exc: Exception) -> str:
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
        executor: Any,
        *,
        clock: Any = None,
        lease_ttl: int = 7_200,
        heartbeat_interval: float | None = None,
        markdown_sync_lease_ttl: int = 600,
    ):
        self.repo = repo
        self.executor = executor
        self.clock = clock or (lambda: int(time.time()))
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

        def fence() -> None:
            self.repo.fence_topic_markdown_sync(
                topic_id,
                claim.owner_id,
                now=self.clock(),
                lease_ttl=self.markdown_sync_lease_ttl,
            )

        try:
            sync = sync_topic_map_markdown if mapping_only else sync_topic_markdown
            sync(self.repo, topic_id, fence=fence)
        except BaseException as exc:
            self.repo.finish_topic_markdown_sync(
                topic_id,
                claim.owner_id,
                "FAILED",
                _safe_error(exc),
                now=self.clock(),
            )
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise TopicMarkdownSyncError("topic Markdown sync failed") from exc
        self.repo.finish_topic_markdown_sync(topic_id, claim.owner_id, "SYNCED", now=self.clock())

    def _run_executor_with_heartbeat(
        self, topic_id: str, owner_id: str, round_key: str, prompt: str
    ) -> str:
        stop = threading.Event()
        lost = threading.Event()
        heartbeat_errors = []

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
        outputs: dict[str, StrictOutput] = {}
        previous: dict[str, Any] = {}
        labels = [chapter.source_label for chapter in initial.source_chapters]
        try:
            for round_key in TOPIC_ROUNDS:
                package = build_topic_task_package(self.repo, topic_id, previous)
                prompt = _topic_prompt(round_key, package)
                run = self.repo.create_topic_run(topic_id, round_key, start.input_fingerprint)
                try:
                    self._heartbeat(topic_id, start.owner_id)
                    validate_prompt = getattr(self.executor, "validate_prompt", None)
                    if validate_prompt is not None:
                        validate_prompt(round_key, prompt)
                    raw = self._run_executor_with_heartbeat(
                        topic_id, start.owner_id, round_key, prompt
                    )
                    parsed = _parse_output(round_key, raw)
                    _validate_round(round_key, parsed, labels)
                    if round_key == "review" and (not parsed.passed or parsed.issues):
                        raise ValueError("topic review rejected")
                    self._heartbeat(topic_id, start.owner_id)
                    outputs[round_key] = parsed
                    previous[round_key] = parsed.model_dump()
                    if round_key != "review":
                        self.repo.finish_topic_run(run.id, "COMPLETED", output=raw)
                        continue
                    _validate_local(outputs, labels)
                    blocks = {
                        **outputs["alignment"].model_dump(),
                        **outputs["comparison"].model_dump(),
                        **outputs["plain_cases"].model_dump(),
                        **outputs["framework_application"].model_dump(),
                        "knowledge_mermaid": outputs["mermaid"].knowledge_diagram,
                        "application_mermaid": outputs["mermaid"].application_diagram,
                    }
                    cards = [
                        {
                            **card.model_dump(exclude={"source_refs"}),
                            "source_refs_json": card.source_refs,
                        }
                        for card in outputs["cards"].cards
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
