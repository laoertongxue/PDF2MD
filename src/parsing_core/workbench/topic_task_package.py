import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from parsing_core.workbench.repository import WorkbenchRepository


class TopicNoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    title: str
    body: str
    seq: int
    updated_at: int


class TopicSourceChapter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_title: str
    source_display_title: str
    chapter_id: str
    seq: int
    title: str
    source_label: str
    note_blocks: list[TopicNoteInput]


class TopicTaskPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    course_id: str
    topic_id: str
    topic_title: str
    source_chapters: list[TopicSourceChapter]
    previous_outputs: dict[str, Any] = Field(default_factory=dict)
    input_fingerprint: str

    def as_prompt(self) -> str:
        return self.model_dump_json()


def build_topic_task_package(
    repo: WorkbenchRepository,
    topic_id: str,
    previous_outputs: dict[str, Any] | None = None,
) -> TopicTaskPackage:
    snapshot, fingerprint = repo.topic_input_snapshot(topic_id)
    topic = snapshot["topic"]
    chapters = snapshot["chapters"]
    if not topic["confirmed"] or not chapters or any(
        chapter["review"]["status"] != "DONE" or chapter["review"]["stale"]
        for chapter in chapters
    ):
        raise ValueError("topic dependencies are not ready")
    source_chapters = []
    sources: list[tuple[str, str]] = []
    seen_source_ids = set()
    for chapter in chapters:
        source_id = chapter["source"]["id"]
        if source_id not in seen_source_ids:
            sources.append((source_id, chapter["source"]["title"]))
            seen_source_ids.add(source_id)
    source_names = _allocate_source_display_titles(sources)
    for chapter in chapters:
        source_title = chapter["source"]["title"]
        display_title = source_names[chapter["source"]["id"]]
        source_chapters.append(
            TopicSourceChapter(
                source_title=source_title,
                source_display_title=display_title,
                chapter_id=chapter["id"],
                seq=chapter["seq"],
                title=chapter["title"],
                source_label=f"[《{display_title}》·第 {chapter['seq'] + 1} 章]",
                note_blocks=[
                    TopicNoteInput.model_validate(
                        {key: value for key, value in note.items() if key != "id"}
                    )
                    for note in chapter["notes"]
                ],
            )
        )
    package = TopicTaskPackage(
        course_id=topic["course_id"], topic_id=topic["id"],
        topic_title=topic["title"],
        source_chapters=source_chapters,
        previous_outputs=previous_outputs or {}, input_fingerprint=fingerprint,
    )
    if len(package.as_prompt()) > 200_000:
        raise ValueError("topic task package exceeds size limit")
    return package


def _normalized_title(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _allocate_source_display_titles(sources: list[tuple[str, str]]) -> dict[str, str]:
    reserved = {_normalized_title(title) for _, title in sources}
    assigned = set()
    seen_real_titles = set()
    result = {}
    for source_id, title in sources:
        normalized = _normalized_title(title)
        if normalized not in seen_real_titles:
            display_title = title
            seen_real_titles.add(normalized)
        else:
            display_title = ""
            for suffix in range(2, 10_001):
                candidate = f"{title}（{suffix}）"
                normalized_candidate = _normalized_title(candidate)
                if normalized_candidate not in reserved and normalized_candidate not in assigned:
                    display_title = candidate
                    break
            if not display_title:
                raise ValueError("unable to allocate unique source display title")
        normalized_display = _normalized_title(display_title)
        if normalized_display in assigned:
            raise ValueError("unable to allocate unique source display title")
        assigned.add(normalized_display)
        result[source_id] = display_title
    return result
