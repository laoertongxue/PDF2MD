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
