import json
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from parsing_core.workbench.executors import TextExecutor
from parsing_core.workbench.repository import WorkbenchRepository

MAX_PROMPT_CHARS = 300_000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOPICS = 200
MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 2_000
MAX_REASON_CHARS = 2_000
MAX_CHAPTERS_PER_TOPIC = 500
MAX_UNMAPPED_CHAPTERS = 500


class TopicOutlineItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(max_length=MAX_TITLE_CHARS)
    description: str = Field(max_length=MAX_DESCRIPTION_CHARS)
    chapter_ids: list[StrictStr] = Field(
        min_length=1,
        max_length=MAX_CHAPTERS_PER_TOPIC,
    )
    reason: str = Field(max_length=MAX_REASON_CHARS)

    @field_validator("title", mode="before")
    @classmethod
    def normalized_title(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("description", "reason", mode="before")
    @classmethod
    def normalized_nonempty_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = unicodedata.normalize("NFKC", value).strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("chapter_ids", mode="before")
    @classmethod
    def normalized_chapter_ids(cls, values: object) -> object:
        if not isinstance(values, list):
            return values
        normalized = [value.strip() if isinstance(value, str) else value for value in values]
        if any(not value for value in normalized):
            raise ValueError("chapter IDs must not be blank")
        return normalized


class TopicOutlineResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    topics: list[TopicOutlineItem] = Field(min_length=1, max_length=MAX_TOPICS)
    unmapped_chapter_ids: list[StrictStr] = Field(max_length=MAX_UNMAPPED_CHAPTERS)

    @field_validator("unmapped_chapter_ids", mode="before")
    @classmethod
    def normalized_unmapped_chapter_ids(cls, values: object) -> object:
        if not isinstance(values, list):
            return values
        normalized = [value.strip() if isinstance(value, str) else value for value in values]
        if any(not value for value in normalized):
            raise ValueError("unmapped chapter IDs must not be blank")
        return normalized


def _normalized_title(title: str) -> str:
    return unicodedata.normalize("NFKC", title).casefold()


def _build_prompt(repo: WorkbenchRepository, course_id: str) -> tuple[str, list[str], str]:
    snapshot, fingerprint = repo.course_topic_outline_snapshot(course_id)
    chapters = snapshot["chapters"]
    if not chapters:
        raise ValueError("course has no confirmed or generated chapters")

    blocking = [
        chapter["id"]
        for chapter in chapters
        if chapter["review"]["status"] != "DONE" or chapter["review"]["stale"]
    ]
    if blocking:
        raise ValueError(f"blocking chapter review IDs: {', '.join(blocking)}")

    payload = {
        "course": snapshot["course"],
        "chapters": [
            {
                "id": chapter["id"],
                "source_title": chapter["source"]["title"],
                "title": chapter["title"],
                "notes": [
                    {"kind": note["kind"], "title": note["title"], "body": note["body"]}
                    for note in chapter["notes"]
                ],
            }
            for chapter in chapters
        ],
    }
    instructions = (
        "Return one JSON object only, without Markdown fences. Create course topics from "
        "the supplied intensive-reading notes. Each topic needs title, description, "
        "chapter_ids, and reason. Return unmapped_chapter_ids too."
    )
    prompt = instructions + "\nINPUT:\n" + json.dumps(payload, ensure_ascii=False)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"topic outline prompt exceeds {MAX_PROMPT_CHARS} characters")
    return prompt, [chapter["id"] for chapter in chapters], fingerprint


def _validate_result(result: TopicOutlineResult, input_ids: list[str]) -> None:
    normalized_titles = [_normalized_title(topic.title) for topic in result.topics]
    if len(normalized_titles) != len(set(normalized_titles)):
        raise ValueError("topic titles must be unique after normalization")
    allowed = set(input_ids)
    mapped = set()
    for topic in result.topics:
        if len(topic.chapter_ids) != len(set(topic.chapter_ids)):
            raise ValueError("duplicate chapter ID within topic")
        if not set(topic.chapter_ids) <= allowed:
            raise ValueError("topic chapter IDs must belong to this input")
        mapped.update(topic.chapter_ids)
    unmapped = result.unmapped_chapter_ids
    if len(unmapped) != len(set(unmapped)):
        raise ValueError("duplicate unmapped chapter ID")
    unmapped_set = set(unmapped)
    if not unmapped_set <= allowed:
        raise ValueError("unmapped chapter IDs must belong to this input")
    if mapped & unmapped_set:
        raise ValueError("mapped and unmapped chapter IDs overlap")
    if mapped | unmapped_set != allowed:
        raise ValueError("mapped and unmapped chapter IDs must cover all input chapters")


def generate_topic_outline(
    repo: WorkbenchRepository,
    course_id: str,
    executor: TextExecutor,
) -> TopicOutlineResult:
    prompt, input_ids, fingerprint = _build_prompt(repo, course_id)
    validate_prompt = getattr(executor, "validate_prompt", None)
    if validate_prompt is not None:
        validate_prompt("topic_outline", prompt)
    output = executor.run("topic_outline", prompt)
    if len(output.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError(f"topic outline response exceeds {MAX_RESPONSE_BYTES} bytes")
    result = TopicOutlineResult.model_validate_json(output)
    _validate_result(result, input_ids)
    repo.replace_course_topic_drafts(
        course_id,
        [
            {
                "title": topic.title,
                "description": topic.description,
                "reason": topic.reason,
                "chapter_ids": topic.chapter_ids,
            }
            for topic in result.topics
        ],
        expected_fingerprint=fingerprint,
    )
    return result
