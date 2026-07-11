from dataclasses import dataclass


@dataclass(frozen=True)
class Course:
    id: str
    title: str
    description: str
    root_dir: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class Source:
    id: str
    course_id: str
    kind: str
    file_path: str
    title: str
    markdown_path: str | None
    status: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class Chapter:
    id: str
    source_id: str
    course_id: str
    seq: int
    title: str
    source_md_path: str
    status: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class Attachment:
    id: str
    course_id: str
    chapter_id: str | None
    file_path: str
    title: str
    kind: str
    created_at: int


@dataclass(frozen=True)
class NoteBlock:
    id: str
    chapter_id: str
    kind: str
    title: str
    body: str
    seq: int
    updated_at: int


@dataclass(frozen=True)
class Card:
    id: str
    course_id: str
    chapter_id: str
    kind: str
    title: str
    body: str
    favorite: bool
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class RunRecord:
    id: str
    chapter_id: str
    round_key: str
    executor: str
    status: str
    input_path: str
    output_path: str
    output: str
    stale: bool
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class CourseTopic:
    id: str
    course_id: str
    seq: int
    title: str
    description: str
    status: str
    confirmed: bool
    stale_reason: str
    created_at: int
    updated_at: int
    generation_reason: str = ""


@dataclass(frozen=True)
class CourseChapter:
    chapter: Chapter
    source_title: str


@dataclass(frozen=True)
class TopicChapterLink:
    topic_id: str
    chapter_id: str
    created_at: int


@dataclass(frozen=True)
class TopicNoteBlock:
    id: str
    topic_id: str
    kind: str
    content: str
    updated_at: int


@dataclass(frozen=True)
class TopicCard:
    id: str
    topic_id: str
    card_type: str
    title: str
    content: str
    source_refs_json: str
    created_at: int


@dataclass(frozen=True)
class TopicRunRecord:
    id: str
    topic_id: str
    round_key: str
    status: str
    input_fingerprint: str
    output: str
    error: str
    started_at: int
    finished_at: int | None


@dataclass(frozen=True)
class TopicGenerationStart:
    topic: CourseTopic
    input_fingerprint: str
    stale_reason_baseline: str
    owner_id: str


@dataclass(frozen=True)
class TopicGenerationLease:
    topic_id: str
    owner_id: str
    heartbeat_at: int
    expires_at: int
