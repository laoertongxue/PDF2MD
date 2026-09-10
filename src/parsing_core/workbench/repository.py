from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, SupportsIndex, SupportsInt, TypedDict
from uuid import uuid4

from parsing_core.storage.connection_lock import (
    atomic_connection,
    lock_repository_methods,
    register_connection_lock,
)
from parsing_core.workbench.models import (
    Attachment,
    Card,
    Chapter,
    ChapterGenerationLease,
    ChapterGenerationRun,
    ChapterGenerationStart,
    Course,
    CourseChapter,
    CourseTopic,
    NoteBlock,
    RunRecord,
    Source,
    TopicCard,
    TopicGenerationLease,
    TopicGenerationStart,
    TopicMarkdownSyncState,
    TopicNoteBlock,
    TopicRunRecord,
)
from parsing_core.workbench.schema import CHAPTER_SYNC_PENDING

MAX_SOURCE_MARKDOWN_BYTES = 16 * 1024 * 1024
MAX_PUBLICATION_COURSE_SOURCES = 1_024
MAX_PUBLICATION_SOURCE_METADATA_BYTES = 4_096
_SOURCE_READ_ATTEMPTS = 3
_SOURCE_READ_CHUNK_BYTES = 64 * 1024


class _CourseRow(TypedDict):
    id: str
    title: str
    description: str
    root_dir: str
    created_at: int
    updated_at: int


class _SourceRow(TypedDict):
    id: str
    course_id: str
    kind: str
    file_path: str
    title: str
    markdown_path: str | None
    status: str
    created_at: int
    updated_at: int


class _ChapterRow(TypedDict):
    id: str
    source_id: str
    course_id: str
    seq: int
    title: str
    source_md_path: str
    status: str
    source_start: int
    source_end: int
    confirmed_snapshot_json: str
    confirmed_at: int | None
    created_at: int
    updated_at: int


class _TopicRow(TypedDict):
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
    generation_reason: str


class _TopicRunRow(TypedDict):
    id: str
    topic_id: str
    round_key: str
    status: str
    input_fingerprint: str
    output: str
    error: str
    started_at: int
    finished_at: int | None


class _CardRow(TypedDict):
    id: str
    course_id: str
    chapter_id: str
    kind: str
    title: str
    body: str
    favorite: bool
    created_at: int
    updated_at: int


class _TopicApiState(TypedDict):
    chapter_ids: list[str]
    blocking_chapter_ids: list[str]
    sync: TopicMarkdownSyncState | None


class _TopicDraftSpec(TypedDict):
    title: str
    description: str
    reason: str
    chapter_ids: list[str]


class _CourseSnapshot(TypedDict):
    id: str
    title: str
    description: str
    updated_at: int


class _SourceSnapshot(TypedDict):
    id: str
    title: str
    updated_at: int


class _CourseOutlineSource(TypedDict):
    id: str
    title: str


class _ReviewSnapshot(TypedDict):
    status: str | None
    stale: bool
    updated_at: int | None
    output: str | None


class _CourseOutlineNote(TypedDict):
    kind: str
    seq: int
    title: str
    body: str
    updated_at: int


class _CourseOutlineChapter(TypedDict):
    id: str
    status: str
    updated_at: int
    seq: int
    title: str
    source: _CourseOutlineSource
    review: _ReviewSnapshot
    notes: list[_CourseOutlineNote]


class _CourseTopicOutlineSnapshot(TypedDict):
    course: _CourseSnapshot
    chapters: list[_CourseOutlineChapter]


class _TopicMetadataSnapshot(TypedDict):
    id: str
    course_id: str
    title: str
    description: str
    confirmed: bool


class _TopicInputNote(TypedDict):
    id: str
    kind: str
    title: str
    body: str
    seq: int
    updated_at: int


class _TopicInputChapter(TypedDict):
    mapping_created_at: int
    source: _SourceSnapshot
    id: str
    seq: int
    title: str
    status: str
    updated_at: int
    review: _ReviewSnapshot
    notes: list[_TopicInputNote]


class _TopicInputSnapshot(TypedDict):
    topic: _TopicMetadataSnapshot
    chapters: list[_TopicInputChapter]


class _ChapterDraft(TypedDict):
    id: str
    seq: int
    title: str
    start: int
    end: int
    status: str


class _ChapterDraftSnapshot(TypedDict):
    source_id: str
    chapters: list[_ChapterDraft]


class _CitationAnchor(TypedDict):
    citation_id: str
    page: int
    paragraph: int
    text: str


class _ChapterInputChapter(TypedDict):
    id: str
    title: str
    start: int
    end: int
    snapshot: str
    content_hash: str


class _ChapterInputAttachment(TypedDict):
    id: str
    hash: str
    anchors: list[_CitationAnchor]


class _ChapterInputSnapshot(TypedDict):
    chapter: _ChapterInputChapter
    attachments: list[_ChapterInputAttachment]


class _CourseCardListItem(TypedDict):
    id: str
    origin_type: str
    origin_id: str
    origin_title: str
    card_type: str
    title: str
    content: str
    source_refs_json: str
    origin_seq: int
    tags_json: str
    status: str
    favorite: bool
    updated_at: int


ChapterGenerationConflictCause = Literal[
    "active_lease",
    "input_changed",
    "invalid_state",
    "lease_lost",
    "run_not_running",
]


class ChapterGenerationConflictError(ValueError):
    def __init__(self, cause: ChapterGenerationConflictCause, message: str) -> None:
        super().__init__(message)
        self.cause = cause


class _PendingChapterMarkdownSync(TypedDict):
    publication_id: str
    owner_id: str
    review_run_id: str
    input_fingerprint: str
    output_fingerprint: str
    error: str


class _ChapterGenerationCheckpoint(TypedDict):
    run_id: str
    round_key: str
    output: str
    input_fingerprint: str
    citation_ids: tuple[str, ...]
    configuration_fingerprint: str
    task_fingerprint: str
    output_fingerprint: str


MarkdownPublicationEntity = Literal["chapter", "topic"]


@dataclass(frozen=True)
class MarkdownPublicationClaim:
    entity_type: MarkdownPublicationEntity
    entity_id: str
    publication_id: str
    owner_id: str
    state_fingerprint: str
    state_revision: int
    content_fingerprint: str
    lease_expires_at: int


_FILE_PUBLICATION_RECEIPT_CAPABILITY = object()
_FILE_PUBLICATION_RECEIPT_SIGNING_KEY = os.urandom(32)


@dataclass(frozen=True)
class FilePublicationReceipt:
    publication_id: str
    file_fingerprint: str
    _capability: object
    _proof: bytes


def _file_publication_receipt_proof(
    publication_id: str,
    file_fingerprint: str,
) -> bytes:
    payload = f"{publication_id}:{file_fingerprint}".encode("ascii")
    return hmac.digest(_FILE_PUBLICATION_RECEIPT_SIGNING_KEY, payload, "sha256")


def _verified_file_publication_receipt(
    publication_id: str,
    file_fingerprint: str,
) -> FilePublicationReceipt:
    if (
        re.fullmatch(r"[0-9a-f]{32}", publication_id) is None
        or re.fullmatch(r"[0-9a-f]{64}", file_fingerprint) is None
    ):
        raise ValueError("file publication receipt is invalid")
    return FilePublicationReceipt(
        publication_id,
        file_fingerprint,
        _FILE_PUBLICATION_RECEIPT_CAPABILITY,
        _file_publication_receipt_proof(publication_id, file_fingerprint),
    )


class MarkdownPublicationFence:
    def __init__(self, claim: MarkdownPublicationClaim) -> None:
        self.claim = claim
        self._file_fingerprint: str | None = None

    @property
    def file_fingerprint(self) -> str | None:
        return self._file_fingerprint

    def bind_file_publication(
        self,
        receipt: FilePublicationReceipt | object,
        *legacy_values: object,
    ) -> None:
        if (
            legacy_values
            or not isinstance(receipt, FilePublicationReceipt)
            or receipt._capability is not _FILE_PUBLICATION_RECEIPT_CAPABILITY
            or not isinstance(receipt.publication_id, str)
            or not isinstance(receipt.file_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{32}", receipt.publication_id) is None
            or re.fullmatch(r"[0-9a-f]{64}", receipt.file_fingerprint) is None
            or not hmac.compare_digest(
                receipt._proof,
                _file_publication_receipt_proof(
                    receipt.publication_id,
                    receipt.file_fingerprint,
                ),
            )
        ):
            raise TypeError("verified file publication receipt is required")
        if receipt.publication_id != self.claim.publication_id:
            raise ValueError("file publication receipt does not match publication")
        if self._file_fingerprint is not None:
            raise RuntimeError("file publication receipt is already bound")
        self._file_fingerprint = receipt.file_fingerprint


class _CourseCardDetail(TypedDict):
    id: str
    origin_type: str
    tags: list[str]
    status: str
    favorite: bool
    updated_at: int
    title: str
    content: str
    course_id: str
    origin_id: str
    origin_title: str
    card_type: str
    source_refs: list[str]


def _now() -> int:
    return int(time.time())


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"invalid JSON constant: {constant}")


def _normalize_source_refs_json(value: object) -> str:
    if isinstance(value, str):
        value = json.loads(value, parse_constant=_reject_json_constant)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("source_refs_json must be a JSON array of strings")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _json_string_list(value: str) -> list[str]:
    decoded: object = json.loads(value, parse_constant=_reject_json_constant)
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise ValueError("expected a JSON array of strings")
    return [item for item in decoded if isinstance(item, str)]


def _citation_anchors_from_json(value: str) -> list[_CitationAnchor]:
    decoded: object = json.loads(value, parse_constant=_reject_json_constant)
    if not isinstance(decoded, list):
        raise ValueError("attachment anchors must be a JSON array")
    anchors: list[_CitationAnchor] = []
    for item in decoded:
        if not isinstance(item, dict):
            raise ValueError("attachment anchor is invalid")
        citation_id: object = item.get("citation_id")
        page: object = item.get("page")
        paragraph: object = item.get("paragraph")
        text: object = item.get("text")
        if (
            not isinstance(citation_id, str)
            or not isinstance(page, int)
            or isinstance(page, bool)
            or not isinstance(paragraph, int)
            or isinstance(paragraph, bool)
            or not isinstance(text, str)
        ):
            raise ValueError("attachment anchor is invalid")
        anchors.append(
            {
                "citation_id": citation_id,
                "page": page,
                "paragraph": paragraph,
                "text": text,
            }
        )
    return anchors


def _topic_draft_spec(value: Mapping[str, object]) -> _TopicDraftSpec:
    title = value.get("title")
    description = value.get("description")
    reason = value.get("reason")
    chapter_ids = value.get("chapter_ids")
    if (
        not isinstance(title, str)
        or not isinstance(description, str)
        or not isinstance(reason, str)
        or not isinstance(chapter_ids, Sequence)
        or isinstance(chapter_ids, (str, bytes))
        or any(not isinstance(chapter_id, str) for chapter_id in chapter_ids)
    ):
        raise ValueError("invalid topic draft specification")
    return {
        "title": title,
        "description": description,
        "reason": reason,
        "chapter_ids": [chapter_id for chapter_id in chapter_ids if isinstance(chapter_id, str)],
    }


def _integer_value(value: object) -> int:
    if isinstance(value, (str, bytes, bytearray)):
        return int(value)
    if isinstance(value, SupportsInt):
        return int(value)
    if isinstance(value, SupportsIndex):
        return int(value)
    raise TypeError("value must be integer-compatible")


def _normalize_stale_reason(reason: str) -> str:
    reason = reason.strip()
    if not reason or "\n" in reason or "\r" in reason:
        raise ValueError("reason must be a nonempty single line")
    return reason


def _stable_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _source_directory_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
    )


def _validate_source_markdown(info: os.stat_result, *, max_bytes: int) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("chapter source must be a regular file")
    if info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise ValueError("chapter source ownership is unsafe")
    unsafe_mode = stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID
    if info.st_mode & unsafe_mode:
        raise ValueError("chapter source mode is unsafe")
    if info.st_size > max_bytes:
        raise ValueError("chapter source exceeds size limit")


class _SourcePathChanged(RuntimeError):
    pass


def _open_source_path(
    path: str | Path, *, max_bytes: int
) -> tuple[int, tuple[tuple[int, ...], ...], tuple[int, ...]]:
    candidate = Path(path).expanduser()
    parts = candidate.parts
    if candidate.is_absolute():
        directory_path = os.sep
        components = parts[1:]
    else:
        directory_path = "."
        components = parts
    if not components or any(part in {"", ".", ".."} for part in components):
        raise ValueError("chapter source path is invalid")

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    source_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_fd: int | None = None
    source_fd: int | None = None
    identities: list[tuple[int, ...]] = []
    try:
        directory_fd = os.open(directory_path, directory_flags)
        root_info = os.fstat(directory_fd)
        if not stat.S_ISDIR(root_info.st_mode):
            raise ValueError("chapter source path is invalid")
        identities.append(_source_directory_identity(root_info))
        for component in components[:-1]:
            before = os.stat(component, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise ValueError("chapter source path cannot contain symlinks")
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            opened = os.fstat(next_fd)
            after = os.stat(component, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or _source_directory_identity(before) != _source_directory_identity(opened)
                or _source_directory_identity(opened) != _source_directory_identity(after)
            ):
                os.close(next_fd)
                raise _SourcePathChanged
            os.close(directory_fd)
            directory_fd = next_fd
            identities.append(_source_directory_identity(opened))

        filename = components[-1]
        before = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        _validate_source_markdown(before, max_bytes=max_bytes)
        source_fd = os.open(filename, source_flags, dir_fd=directory_fd)
        opened = os.fstat(source_fd)
        _validate_source_markdown(opened, max_bytes=max_bytes)
        after = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        _validate_source_markdown(after, max_bytes=max_bytes)
        if _source_identity(before) != _source_identity(opened) or _source_identity(
            opened
        ) != _source_identity(after):
            raise _SourcePathChanged
        result_fd = source_fd
        source_fd = None
        return result_fd, tuple(identities), _source_identity(opened)
    except FileNotFoundError:
        raise FileNotFoundError("chapter source not found") from None
    except _SourcePathChanged:
        raise
    except ValueError:
        raise
    except OSError:
        raise ValueError("chapter source cannot be opened safely") from None
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def read_stable_source_markdown(
    path: str | Path, *, max_bytes: int = MAX_SOURCE_MARKDOWN_BYTES
) -> tuple[bytes, str]:
    if max_bytes < 0:
        raise ValueError("source size limit must be nonnegative")
    for _attempt in range(_SOURCE_READ_ATTEMPTS):
        fd: int | None = None
        verification_fd: int | None = None
        try:
            fd, path_identity, opened_identity = _open_source_path(
                path,
                max_bytes=max_bytes,
            )
            chunks: list[bytes] = []
            total = 0
            while total <= max_bytes:
                chunk = os.read(
                    fd,
                    min(_SOURCE_READ_CHUNK_BYTES, max_bytes + 1 - total),
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > max_bytes:
                raise ValueError("chapter source exceeds size limit")
            after = os.fstat(fd)
            verification_fd, path_identity_after, verified_identity = _open_source_path(
                path,
                max_bytes=max_bytes,
            )
            if (
                path_identity != path_identity_after
                or opened_identity != _source_identity(after)
                or opened_identity != verified_identity
                or total != after.st_size
            ):
                continue
            _validate_source_markdown(after, max_bytes=max_bytes)
            content = b"".join(chunks)
            return content, hashlib.sha256(content).hexdigest()
        except _SourcePathChanged:
            continue
        finally:
            if verification_fd is not None:
                os.close(verification_fd)
            if fd is not None:
                os.close(fd)
    raise ValueError("chapter source changed while being read")


@dataclass(frozen=True)
class _PersistedError:
    code: str
    message: str


_NO_PERSISTED_ERROR = _PersistedError("", "")


def _chapter_generation_error(value: str) -> _PersistedError:
    if value == "interrupted":
        return _PersistedError(
            "CHAPTER_GENERATION_INTERRUPTED",
            "chapter generation interrupted",
        )
    if value == "staged input changed":
        return _PersistedError(
            "CHAPTER_INPUT_CHANGED",
            "chapter input changed",
        )
    return _PersistedError(
        "CHAPTER_GENERATION_FAILED",
        "chapter generation failed",
    )


def _chapter_publication_error(value: str) -> _PersistedError:
    if value == "staged input changed":
        return _PersistedError(
            "CHAPTER_INPUT_CHANGED",
            "chapter input changed",
        )
    if value == "interrupted before Markdown sync":
        return _PersistedError(
            "CHAPTER_MARKDOWN_PUBLICATION_INTERRUPTED",
            "chapter Markdown publication interrupted",
        )
    return _PersistedError(
        "CHAPTER_MARKDOWN_PUBLICATION_FAILED",
        "chapter Markdown publication failed",
    )


def _topic_generation_error(value: str) -> _PersistedError:
    if value == "interrupted":
        return _PersistedError(
            "TOPIC_GENERATION_INTERRUPTED",
            "topic generation interrupted",
        )
    return _PersistedError(
        "TOPIC_GENERATION_FAILED",
        "topic generation failed",
    )


def _topic_publication_error() -> _PersistedError:
    return _PersistedError(
        "TOPIC_MARKDOWN_PUBLICATION_FAILED",
        "topic Markdown publication failed",
    )


def _temporary_topic_sequences(existing: list[object], count: int) -> list[int]:
    sqlite_min = -(2**63)
    sqlite_max = 2**63 - 1
    occupied = set(existing) | set(range(count))
    result: list[int] = []

    candidate = -1
    while len(result) < count and candidate >= sqlite_min:
        if candidate not in occupied:
            result.append(candidate)
        candidate -= 1

    candidate = sqlite_max
    while len(result) < count and candidate >= 0:
        if candidate not in occupied:
            result.append(candidate)
        candidate -= 1

    if len(result) != count:
        raise ValueError("no temporary SQLite INTEGER sequence values available")
    return result


@lock_repository_methods
class WorkbenchRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._connection_lock, self._connection_lock_finalizer = register_connection_lock(
            self,
            conn,
        )

    @contextmanager
    def _atomic(self, *, immediate: bool = False) -> Iterator[None]:
        lock_token = uuid4().hex
        with atomic_connection(
            self.conn,
            self._connection_lock,
            immediate=immediate,
            nested_write=(
                "UPDATE wb_topics SET updated_at = updated_at WHERE id = ?",
                (f"__lock_{lock_token}",),
            ),
        ):
            yield

    def _pending_chapter_publication(self, chapter_id: str) -> _PendingChapterMarkdownSync | None:
        row = self.conn.execute(
            "SELECT publication_id, owner_id, review_run_id, input_fingerprint, "
            "output_fingerprint, error "
            "FROM wb_chapter_generation_publications WHERE chapter_id = ? "
            "AND status = ?",
            (chapter_id, CHAPTER_SYNC_PENDING),
        ).fetchone()
        if row is None:
            return None
        return {
            "publication_id": row[0],
            "owner_id": row[1],
            "review_run_id": row[2],
            "input_fingerprint": row[3],
            "output_fingerprint": row[4],
            "error": row[5],
        }

    def _chapter_publication_output_fingerprint(self, chapter_id: str) -> str:
        blocks = [
            {
                "kind": block.kind,
                "title": block.title,
                "body": block.body,
                "seq": block.seq,
            }
            for block in self.list_note_blocks(chapter_id)
        ]
        cards = [
            {"kind": card.kind, "title": card.title, "body": card.body}
            for card in self.list_cards_by_chapter(chapter_id)
            if card.kind == "topic"
        ]
        return _stable_fingerprint({"blocks": blocks, "cards": cards})

    def _pending_publication_is_current(
        self, chapter_id: str, pending: _PendingChapterMarkdownSync
    ) -> bool:
        return (
            self.chapter_input_snapshot(chapter_id)[1] == pending["input_fingerprint"]
            and self._chapter_publication_output_fingerprint(chapter_id)
            == pending["output_fingerprint"]
        )

    def _publication_course_sources(
        self,
        course_id: str,
    ) -> tuple[dict[str, str], list[tuple[int, str, str, int, int]]]:
        rows = self.conn.execute(
            "SELECT "
            "CASE WHEN length(CAST(sources.id AS BLOB)) <= ? THEN sources.id END, "
            "CASE WHEN length(CAST(sources.title AS BLOB)) <= ? THEN sources.title END, "
            "sources.created_at, sources.rowid "
            "FROM wb_sources AS sources WHERE sources.course_id = ? "
            "ORDER BY sources.created_at, sources.rowid LIMIT ?",
            (
                MAX_PUBLICATION_SOURCE_METADATA_BYTES,
                MAX_PUBLICATION_SOURCE_METADATA_BYTES,
                course_id,
                MAX_PUBLICATION_COURSE_SOURCES + 1,
            ),
        ).fetchall()
        if len(rows) > MAX_PUBLICATION_COURSE_SOURCES:
            raise ValueError("course source limit exceeded")
        source_titles: dict[str, str] = {}
        state: list[tuple[int, str, str, int, int]] = []
        for position, row in enumerate(rows):
            source_id, title, created_at, rowid = row
            if type(source_id) is not str or type(title) is not str:
                raise ValueError("course source metadata limit exceeded")
            source_titles[source_id] = title
            state.append((position, source_id, title, int(created_at), int(rowid)))
        return source_titles, state

    def markdown_publication_state_fingerprint(
        self,
        entity_type: MarkdownPublicationEntity,
        entity_id: str,
    ) -> str:
        if entity_type == "chapter":
            chapter = self.get_chapter(entity_id)
            if chapter is None:
                raise ValueError("chapter not found")
            course = self.get_course(chapter.course_id)
            if course is None:
                raise ValueError("chapter dependencies not found")
            source_titles, course_source_state = self._publication_course_sources(chapter.course_id)
            source_title = source_titles.get(chapter.source_id)
            if source_title is None:
                raise ValueError("chapter dependencies not found")
            source_bytes, source_hash = read_stable_source_markdown(chapter.source_md_path)
            del source_bytes
            blocks = [
                (block.kind, block.title, block.body, block.seq)
                for block in self.list_note_blocks(entity_id)
            ]
            cards = [
                (card.kind, card.title, card.body, card.favorite)
                for card in self.list_cards_by_chapter(entity_id)
            ]
            runs = [
                (run.round_key, run.executor, run.status, run.output, run.stale)
                for run in self.list_runs(entity_id)
            ]
            return _stable_fingerprint(
                {
                    "entity": (chapter.id, chapter.seq, chapter.title),
                    "location": (course.root_dir, chapter.source_id, source_title),
                    "course_sources": course_source_state,
                    "source_hash": source_hash,
                    "blocks": blocks,
                    "cards": cards,
                    "runs": runs,
                }
            )
        topic = self.get_topic(entity_id)
        if topic is None:
            raise ValueError("topic not found")
        course = self.get_course(topic.course_id)
        if course is None:
            raise ValueError("course not found")
        chapters = self.list_topic_chapters(entity_id)
        source_titles, course_source_state = self._publication_course_sources(topic.course_id)
        topic_blocks = [
            (block.kind, block.content) for block in self.list_topic_note_blocks(entity_id)
        ]
        topic_cards = [
            (card.card_type, card.title, card.content, card.source_refs_json)
            for card in self.list_topic_cards(entity_id)
        ]
        topic_runs = [
            (run.id, run.round_key, run.status, run.output, run.error, run.started_at)
            for run in self.list_topic_runs(entity_id)
        ]
        return _stable_fingerprint(
            {
                "entity": (
                    topic.id,
                    topic.seq,
                    topic.title,
                    topic.description,
                    topic.status,
                    topic.confirmed,
                    topic.generation_reason,
                ),
                "location": course.root_dir,
                "course_sources": course_source_state,
                "chapters": [
                    (
                        chapter.id,
                        chapter.seq,
                        chapter.title,
                        chapter.source_id,
                        source_titles[chapter.source_id],
                    )
                    for chapter in chapters
                ],
                "blocks": topic_blocks,
                "cards": topic_cards,
                "runs": topic_runs,
            }
        )

    def markdown_publication_revision(self) -> int:
        row = self.conn.execute(
            "SELECT revision FROM wb_markdown_state_revision WHERE singleton = 1"
        ).fetchone()
        if row is None or type(row[0]) is not int or row[0] < 0:
            raise RuntimeError("Markdown publication revision is unavailable")
        return row[0]

    def markdown_publication_state_snapshot(
        self,
        entity_type: MarkdownPublicationEntity,
        entity_id: str,
    ) -> tuple[str, int]:
        for _attempt in range(3):
            before = self.markdown_publication_revision()
            fingerprint = self.markdown_publication_state_fingerprint(entity_type, entity_id)
            after = self.markdown_publication_revision()
            if before == after:
                return fingerprint, after
        raise ValueError("Markdown publication state changed")

    def claim_markdown_publication(
        self,
        entity_type: MarkdownPublicationEntity,
        entity_id: str,
        state_fingerprint: str,
        content_fingerprint: str,
        *,
        state_revision: int | None = None,
        owner_id: str | None = None,
        publication_id: str | None = None,
        now: int | None = None,
        lease_ttl: int = 600,
    ) -> MarkdownPublicationClaim:
        if lease_ttl <= 0:
            raise ValueError("publication lease_ttl must be positive")
        if not state_fingerprint or not content_fingerprint:
            raise ValueError("publication fingerprints must be nonempty")
        if state_revision is not None and (type(state_revision) is not int or state_revision < 0):
            raise ValueError("publication revision is invalid")
        now = _now() if now is None else now
        owner_id = owner_id or uuid4().hex
        publication_id = publication_id or uuid4().hex
        with self._atomic(immediate=True):
            current_revision = self.markdown_publication_revision()
            if state_revision is None:
                state_revision = current_revision
            if (
                current_revision != state_revision
                or self.markdown_publication_state_fingerprint(entity_type, entity_id)
                != state_fingerprint
            ):
                raise ValueError("Markdown publication state changed")
            self.conn.execute(
                """
                INSERT INTO wb_markdown_publications
                  (entity_type, entity_id, publication_id, owner_id, state_fingerprint,
                   state_revision, content_fingerprint, lease_expires_at,
                   error_code, error_message, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', ?)
                ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                  publication_id = excluded.publication_id,
                  owner_id = excluded.owner_id,
                  state_fingerprint = excluded.state_fingerprint,
                  state_revision = excluded.state_revision,
                  content_fingerprint = excluded.content_fingerprint,
                  lease_expires_at = excluded.lease_expires_at,
                  error_code = '', error_message = '', updated_at = excluded.updated_at
                """,
                (
                    entity_type,
                    entity_id,
                    publication_id,
                    owner_id,
                    state_fingerprint,
                    state_revision,
                    content_fingerprint,
                    now + lease_ttl,
                    now,
                ),
            )
        return MarkdownPublicationClaim(
            entity_type,
            entity_id,
            publication_id,
            owner_id,
            state_fingerprint,
            state_revision,
            content_fingerprint,
            now + lease_ttl,
        )

    def _validate_markdown_publication_no_commit(
        self,
        claim: MarkdownPublicationClaim,
        *,
        now: int,
    ) -> None:
        row = self.conn.execute(
            "SELECT publication_id, owner_id, state_fingerprint, state_revision, "
            "content_fingerprint, "
            "lease_expires_at FROM wb_markdown_publications "
            "WHERE entity_type = ? AND entity_id = ?",
            (claim.entity_type, claim.entity_id),
        ).fetchone()
        if row is None or tuple(row[:5]) != (
            claim.publication_id,
            claim.owner_id,
            claim.state_fingerprint,
            claim.state_revision,
            claim.content_fingerprint,
        ):
            raise ValueError("Markdown publication was superseded")
        if row[5] <= now:
            raise ValueError("Markdown publication lease lost")
        if (
            self.markdown_publication_revision() != claim.state_revision
            or self.markdown_publication_state_fingerprint(claim.entity_type, claim.entity_id)
            != claim.state_fingerprint
        ):
            raise ValueError("Markdown publication state changed")

    def _complete_markdown_publication_no_commit(
        self,
        fence: MarkdownPublicationFence,
        *,
        now: int,
    ) -> None:
        file_fingerprint = fence.file_fingerprint
        if file_fingerprint is None:
            raise RuntimeError("file publication receipt is required")
        claim = fence.claim
        self.conn.execute(
            """
            INSERT INTO wb_markdown_publication_receipts
              (entity_type, entity_id, publication_id, state_fingerprint,
               state_revision, content_fingerprint, file_fingerprint, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
              publication_id = excluded.publication_id,
              state_fingerprint = excluded.state_fingerprint,
              state_revision = excluded.state_revision,
              content_fingerprint = excluded.content_fingerprint,
              file_fingerprint = excluded.file_fingerprint,
              completed_at = excluded.completed_at
            """,
            (
                claim.entity_type,
                claim.entity_id,
                claim.publication_id,
                claim.state_fingerprint,
                claim.state_revision,
                claim.content_fingerprint,
                file_fingerprint,
                now,
            ),
        )
        cursor = self.conn.execute(
            "DELETE FROM wb_markdown_publications "
            "WHERE entity_type = ? AND entity_id = ? AND publication_id = ? AND owner_id = ?",
            (claim.entity_type, claim.entity_id, claim.publication_id, claim.owner_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Markdown publication was superseded")

    @contextmanager
    def fence_markdown_publication(
        self,
        claim: MarkdownPublicationClaim,
        *,
        clock: Callable[[], int] | None = None,
    ) -> Iterator[MarkdownPublicationFence]:
        current_time = clock or _now
        with self._atomic(immediate=True):
            self._validate_markdown_publication_no_commit(claim, now=current_time())
            fence = MarkdownPublicationFence(claim)
            yield fence
            completed_at = current_time()
            self._validate_markdown_publication_no_commit(claim, now=completed_at)
            self._complete_markdown_publication_no_commit(fence, now=completed_at)

    def markdown_publication_committed(
        self,
        entity_type: MarkdownPublicationEntity,
        entity_id: str,
        publication_id: str,
    ) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM wb_markdown_publication_receipts "
            "WHERE entity_type = ? AND entity_id = ? AND publication_id = ?",
            (entity_type, entity_id, publication_id),
        ).fetchone()
        return row is not None

    def abandon_markdown_publication(self, claim: MarkdownPublicationClaim) -> None:
        with self._atomic(immediate=True):
            self.conn.execute(
                "DELETE FROM wb_markdown_publications "
                "WHERE entity_type = ? AND entity_id = ? AND publication_id = ? "
                "AND owner_id = ?",
                (
                    claim.entity_type,
                    claim.entity_id,
                    claim.publication_id,
                    claim.owner_id,
                ),
            )

    def _interrupt_generation_no_commit(
        self,
        chapter_id: str,
        owner_id: str,
        *,
        now: int,
        error: str,
    ) -> None:
        stored_error = _chapter_generation_error(error)
        self.conn.execute(
            "UPDATE wb_chapter_generation_runs SET status = 'FAILED', error = ?, "
            "error_code = ?, error_message = ?, "
            "finished_at = ? WHERE chapter_id = ? AND owner_id = ? "
            "AND status IN ('RUNNING', 'SYNC_PENDING')",
            (
                stored_error.message,
                stored_error.code,
                stored_error.message,
                now,
                chapter_id,
                owner_id,
            ),
        )
        self.conn.execute(
            "UPDATE wb_chapters SET status = 'FAILED', updated_at = ? "
            "WHERE id = ? AND status IN ('RUNNING', 'SYNC_PENDING')",
            (now, chapter_id),
        )
        self.conn.execute(
            "DELETE FROM wb_chapter_generation_leases WHERE chapter_id = ? AND owner_id = ?",
            (chapter_id, owner_id),
        )

    def _discard_pending_publication_no_commit(
        self,
        chapter_id: str,
        owner_id: str,
        *,
        now: int,
        error: str = "staged input changed",
    ) -> None:
        stored_error = _chapter_generation_error(error)
        self.conn.execute(
            "UPDATE wb_chapter_generation_runs SET status = 'FAILED', error = ?, "
            "error_code = ?, error_message = ?, "
            "finished_at = ? WHERE chapter_id = ? AND owner_id = ?",
            (
                stored_error.message,
                stored_error.code,
                stored_error.message,
                now,
                chapter_id,
                owner_id,
            ),
        )
        self.conn.execute(
            "DELETE FROM wb_chapter_generation_candidates WHERE chapter_id = ? AND owner_id = ?",
            (chapter_id, owner_id),
        )
        self.conn.execute(
            "UPDATE wb_runs SET status = 'FAILED', stale = 1, updated_at = ? WHERE chapter_id = ?",
            (now, chapter_id),
        )
        self.conn.execute("DELETE FROM wb_note_blocks WHERE chapter_id = ?", (chapter_id,))
        self.conn.execute(
            "DELETE FROM wb_cards WHERE chapter_id = ? AND kind = 'topic'", (chapter_id,)
        )
        self.conn.execute(
            "DELETE FROM wb_chapter_generation_publications WHERE chapter_id = ? AND owner_id = ?",
            (chapter_id, owner_id),
        )
        self.conn.execute(
            "DELETE FROM wb_chapter_generation_leases WHERE chapter_id = ?",
            (chapter_id,),
        )
        self.conn.execute(
            "UPDATE wb_chapters SET status = 'FAILED', updated_at = ? "
            "WHERE id = ? AND status IN ('RUNNING', 'SYNC_PENDING')",
            (now, chapter_id),
        )

    def _create_chapter_generation_lease_no_commit(
        self, chapter_id: str, owner_id: str, *, now: int, lease_ttl: int
    ) -> None:
        self.conn.execute(
            "INSERT INTO wb_chapter_generation_leases "
            "(chapter_id, owner_id, heartbeat_at, expires_at) VALUES (?, ?, ?, ?)",
            (chapter_id, owner_id, now, now + lease_ttl),
        )

    def _refresh_topics_for_chapter_no_commit(self, chapter_id: str, *, now: int) -> None:
        for topic in self.list_topics_for_chapter(chapter_id):
            if topic.status not in {"DRAFT", "NOT_READY", "READY"}:
                continue
            status = self._topic_readiness_status(topic)
            self.conn.execute(
                "UPDATE wb_topics SET status = ?, stale_reason = '', updated_at = ? "
                "WHERE id = ? AND status IN ('DRAFT', 'NOT_READY', 'READY')",
                (status, now, topic.id),
            )

    def start_chapter_generation(
        self, chapter_id: str, *, now: int | None = None, lease_ttl: int = 7_200
    ) -> ChapterGenerationStart:
        now = _now() if now is None else now
        if lease_ttl <= 0:
            raise ValueError("lease_ttl must be positive")
        with self._atomic(immediate=True):
            chapter = self.get_chapter(chapter_id)
            if chapter is None:
                raise ValueError("chapter not found")
            lease = self.get_chapter_generation_lease(chapter_id)
            if lease is not None and lease.expires_at > now:
                raise ChapterGenerationConflictError("active_lease", "chapter is already running")
            pending = self._pending_chapter_publication(chapter_id)
            if pending is not None:
                if self._pending_publication_is_current(chapter_id, pending):
                    owner_id = uuid4().hex
                    previous_owner = pending["owner_id"]
                    self.conn.execute(
                        "UPDATE wb_chapter_generation_runs SET owner_id = ? "
                        "WHERE chapter_id = ? AND owner_id = ?",
                        (owner_id, chapter_id, previous_owner),
                    )
                    self.conn.execute(
                        "UPDATE wb_chapter_generation_candidates SET owner_id = ? "
                        "WHERE chapter_id = ? AND owner_id = ?",
                        (owner_id, chapter_id, previous_owner),
                    )
                    self.conn.execute(
                        "UPDATE wb_chapter_generation_publications "
                        "SET owner_id = ?, error = '', error_code = '', "
                        "error_message = '', updated_at = ? "
                        "WHERE chapter_id = ? AND owner_id = ? AND status = ?",
                        (
                            owner_id,
                            now,
                            chapter_id,
                            previous_owner,
                            CHAPTER_SYNC_PENDING,
                        ),
                    )
                    self.conn.execute(
                        "DELETE FROM wb_chapter_generation_leases WHERE chapter_id = ?",
                        (chapter_id,),
                    )
                    self._create_chapter_generation_lease_no_commit(
                        chapter_id, owner_id, now=now, lease_ttl=lease_ttl
                    )
                    self.conn.execute(
                        "UPDATE wb_chapters SET status = ?, updated_at = ? WHERE id = ?",
                        (CHAPTER_SYNC_PENDING, now, chapter_id),
                    )
                    updated = self.get_chapter(chapter_id)
                    if updated is None:
                        raise RuntimeError("chapter disappeared during generation takeover")
                    return ChapterGenerationStart(updated, owner_id)
                self._discard_pending_publication_no_commit(
                    chapter_id,
                    pending["owner_id"],
                    now=now,
                )
                chapter = self.get_chapter(chapter_id)
                if chapter is None:
                    raise RuntimeError("chapter disappeared during pending invalidation")
            elif lease is not None:
                self._interrupt_generation_no_commit(
                    chapter_id,
                    lease.owner_id,
                    now=now,
                    error="interrupted",
                )
                chapter = self.get_chapter(chapter_id)
                if chapter is None:
                    raise RuntimeError("chapter disappeared during generation recovery")
            elif chapter.status in {"RUNNING", CHAPTER_SYNC_PENDING}:
                self.conn.execute(
                    "UPDATE wb_chapters SET status = 'FAILED', updated_at = ? WHERE id = ?",
                    (now, chapter_id),
                )
                chapter = self.get_chapter(chapter_id)
                if chapter is None:
                    raise RuntimeError("chapter disappeared during generation recovery")
            if chapter.status not in {"CONFIRMED", "FAILED"}:
                raise ChapterGenerationConflictError(
                    "invalid_state",
                    "chapter must be CONFIRMED or FAILED before intensive reading",
                )
            cursor = self.conn.execute(
                "UPDATE wb_chapters SET status = 'RUNNING', updated_at = ? "
                "WHERE id = ? AND status = ?",
                (now, chapter_id, chapter.status),
            )
            if cursor.rowcount != 1:
                raise ChapterGenerationConflictError("active_lease", "chapter is already running")
            owner_id = uuid4().hex
            self.conn.execute(
                "UPDATE wb_chapter_generation_candidates SET owner_id = ? WHERE chapter_id = ?",
                (owner_id, chapter_id),
            )
            self._create_chapter_generation_lease_no_commit(
                chapter_id, owner_id, now=now, lease_ttl=lease_ttl
            )
            updated_chapter = self.get_chapter(chapter_id)
            if updated_chapter is None:
                raise RuntimeError("chapter disappeared during generation start")
            return ChapterGenerationStart(updated_chapter, owner_id)

    def get_chapter_generation_lease(self, chapter_id: str) -> ChapterGenerationLease | None:
        row = self.conn.execute(
            "SELECT * FROM wb_chapter_generation_leases WHERE chapter_id = ?", (chapter_id,)
        ).fetchone()
        return ChapterGenerationLease(*row) if row else None

    def heartbeat_chapter_generation(
        self, chapter_id: str, owner_id: str, *, now: int | None = None, lease_ttl: int = 7_200
    ) -> ChapterGenerationLease:
        now = _now() if now is None else now
        with self._atomic(immediate=True):
            cursor = self.conn.execute(
                "UPDATE wb_chapter_generation_leases SET heartbeat_at = ?, expires_at = ? "
                "WHERE chapter_id = ? AND owner_id = ? AND expires_at > ?",
                (now, now + lease_ttl, chapter_id, owner_id, now),
            )
            if cursor.rowcount != 1:
                raise ChapterGenerationConflictError(
                    "lease_lost",
                    "chapter generation lease lost",
                )
            lease = self.get_chapter_generation_lease(chapter_id)
            if lease is None:
                raise RuntimeError("chapter generation lease was not persisted")
            return lease

    def create_chapter_generation_run(
        self, chapter_id: str, owner_id: str, round_key: str, *, now: int | None = None
    ) -> ChapterGenerationRun:
        now = _now() if now is None else now
        run_id = uuid4().hex
        with self._atomic(immediate=True):
            lease = self.get_chapter_generation_lease(chapter_id)
            if lease is None or lease.owner_id != owner_id:
                raise ChapterGenerationConflictError(
                    "lease_lost",
                    "chapter generation lease lost",
                )
            self.conn.execute(
                "INSERT INTO wb_chapter_generation_runs "
                "(id, chapter_id, owner_id, round_key, status, started_at) "
                "VALUES (?, ?, ?, ?, 'RUNNING', ?)",
                (run_id, chapter_id, owner_id, round_key, now),
            )
        run = self.get_chapter_generation_run(run_id)
        if run is None:
            raise RuntimeError("chapter generation run was not persisted")
        return run

    def get_chapter_generation_run(self, run_id: str) -> ChapterGenerationRun | None:
        row = self.conn.execute(
            "SELECT * FROM wb_chapter_generation_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return ChapterGenerationRun(*row) if row else None

    def finish_chapter_generation_run(
        self,
        run_id: str,
        owner_id: str,
        status: str,
        *,
        output: str = "",
        error: str = "",
        input_fingerprint: str = "",
        citation_ids: tuple[str, ...] = (),
        configuration_fingerprint: str = "",
        task_fingerprint: str = "",
        now: int | None = None,
    ) -> ChapterGenerationRun:
        now = _now() if now is None else now
        stored_error = (
            _NO_PERSISTED_ERROR if status == "COMPLETED" else _chapter_generation_error(error)
        )
        with self._atomic(immediate=True):
            cursor = self.conn.execute(
                "UPDATE wb_chapter_generation_runs SET status = ?, output = ?, error = ?, "
                "error_code = ?, error_message = ?, "
                "finished_at = ? WHERE id = ? AND owner_id = ? AND status = 'RUNNING'",
                (
                    status,
                    output,
                    stored_error.message,
                    stored_error.code,
                    stored_error.message,
                    now,
                    run_id,
                    owner_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ChapterGenerationConflictError(
                    "run_not_running",
                    "chapter generation run is not RUNNING",
                )
            if status == "COMPLETED":
                run = self.get_chapter_generation_run(run_id)
                if run is None:
                    raise RuntimeError("chapter generation run disappeared")
                self.conn.execute(
                    "INSERT INTO wb_chapter_generation_candidates "
                    "(run_id, chapter_id, owner_id, round_key, output, input_fingerprint, "
                    "citation_ids_json, configuration_fingerprint, task_fingerprint, "
                    "output_fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        run.chapter_id,
                        owner_id,
                        run.round_key,
                        output,
                        input_fingerprint,
                        json.dumps(citation_ids, ensure_ascii=False),
                        configuration_fingerprint,
                        task_fingerprint,
                        hashlib.sha256(output.encode("utf-8")).hexdigest(),
                        now,
                    ),
                )
        run = self.get_chapter_generation_run(run_id)
        if run is None:
            raise RuntimeError("chapter generation run disappeared")
        return run

    def chapter_generation_candidates(self, chapter_id: str, owner_id: str) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT round_key, output FROM wb_chapter_generation_candidates "
            "WHERE chapter_id = ? AND owner_id = ? ORDER BY created_at, run_id",
            (chapter_id, owner_id),
        ).fetchall()
        return dict(rows)

    def chapter_generation_checkpoints(
        self, chapter_id: str, owner_id: str
    ) -> list[_ChapterGenerationCheckpoint]:
        rows = self.conn.execute(
            "SELECT run_id, round_key, output, input_fingerprint, citation_ids_json, "
            "configuration_fingerprint, task_fingerprint, output_fingerprint "
            "FROM wb_chapter_generation_candidates WHERE chapter_id = ? AND owner_id = ? "
            "ORDER BY created_at, run_id",
            (chapter_id, owner_id),
        ).fetchall()
        return [
            {
                "run_id": row[0],
                "round_key": row[1],
                "output": row[2],
                "input_fingerprint": row[3],
                "citation_ids": tuple(_json_string_list(row[4])),
                "configuration_fingerprint": row[5],
                "task_fingerprint": row[6],
                "output_fingerprint": row[7],
            }
            for row in rows
        ]

    def discard_chapter_generation_checkpoints(
        self,
        chapter_id: str,
        owner_id: str,
        round_keys: Sequence[str],
    ) -> None:
        keys = tuple(dict.fromkeys(round_keys))
        if not keys:
            return
        placeholders = ",".join("?" for _ in keys)
        with self._atomic(immediate=True):
            self.conn.execute(
                "DELETE FROM wb_chapter_generation_candidates "
                f"WHERE chapter_id = ? AND owner_id = ? AND round_key IN ({placeholders})",
                (chapter_id, owner_id, *keys),
            )

    def release_chapter_generation(
        self,
        chapter_id: str,
        owner_id: str,
        *,
        error: str = "interrupted",
        now: int | None = None,
    ) -> Chapter:
        now = _now() if now is None else now
        with self._atomic(immediate=True):
            lease = self.get_chapter_generation_lease(chapter_id)
            if lease is None or lease.owner_id != owner_id:
                raise ChapterGenerationConflictError(
                    "lease_lost",
                    "chapter generation lease lost",
                )
            pending = self._pending_chapter_publication(chapter_id)
            if pending is not None and pending["owner_id"] == owner_id:
                stored_error = _chapter_publication_error(error)
                self.conn.execute(
                    "UPDATE wb_chapter_generation_publications SET error = ?, "
                    "error_code = ?, error_message = ?, updated_at = ? "
                    "WHERE chapter_id = ? AND owner_id = ? AND status = ?",
                    (
                        stored_error.message,
                        stored_error.code,
                        stored_error.message,
                        now,
                        chapter_id,
                        owner_id,
                        CHAPTER_SYNC_PENDING,
                    ),
                )
                self.conn.execute(
                    "UPDATE wb_chapter_generation_runs SET status = ?, error = ?, "
                    "error_code = ?, error_message = ?, "
                    "finished_at = NULL WHERE id = ? AND owner_id = ?",
                    (
                        CHAPTER_SYNC_PENDING,
                        stored_error.message,
                        stored_error.code,
                        stored_error.message,
                        pending["review_run_id"],
                        owner_id,
                    ),
                )
                self.conn.execute(
                    "UPDATE wb_runs SET status = ?, updated_at = ? "
                    "WHERE chapter_id = ? AND round_key = 'review'",
                    (CHAPTER_SYNC_PENDING, now, chapter_id),
                )
                self.conn.execute(
                    "UPDATE wb_chapters SET status = ?, updated_at = ? WHERE id = ?",
                    (CHAPTER_SYNC_PENDING, now, chapter_id),
                )
                self.conn.execute(
                    "DELETE FROM wb_chapter_generation_leases "
                    "WHERE chapter_id = ? AND owner_id = ?",
                    (chapter_id, owner_id),
                )
            else:
                self._interrupt_generation_no_commit(
                    chapter_id,
                    owner_id,
                    now=now,
                    error=error,
                )
            chapter = self.get_chapter(chapter_id)
            if chapter is None:
                raise RuntimeError("chapter disappeared during generation failure")
            return chapter

    def fail_chapter_generation(self, chapter_id: str, owner_id: str) -> Chapter:
        return self.release_chapter_generation(chapter_id, owner_id, error="interrupted")

    def recover_interrupted_chapter_run(
        self, chapter_id: str, *, now: int | None = None
    ) -> Chapter:
        now = _now() if now is None else now
        with self._atomic(immediate=True):
            lease = self.get_chapter_generation_lease(chapter_id)
            if lease is None:
                raise ValueError("chapter generation lease not found")
            if lease.expires_at > now:
                raise ValueError("chapter generation lease not expired")
            pending = self._pending_chapter_publication(chapter_id)
            if pending is not None and pending["owner_id"] == lease.owner_id:
                stored_error = _chapter_publication_error("interrupted before Markdown sync")
                self.conn.execute(
                    "UPDATE wb_chapter_generation_publications "
                    "SET error = ?, error_code = ?, error_message = ?, updated_at = ? "
                    "WHERE chapter_id = ? AND owner_id = ?",
                    (
                        stored_error.message,
                        stored_error.code,
                        stored_error.message,
                        now,
                        chapter_id,
                        lease.owner_id,
                    ),
                )
                self.conn.execute(
                    "UPDATE wb_chapter_generation_runs SET error = ?, error_code = ?, "
                    "error_message = ? "
                    "WHERE id = ? AND owner_id = ? AND status = ?",
                    (
                        stored_error.message,
                        stored_error.code,
                        stored_error.message,
                        pending["review_run_id"],
                        lease.owner_id,
                        CHAPTER_SYNC_PENDING,
                    ),
                )
                self.conn.execute(
                    "DELETE FROM wb_chapter_generation_leases WHERE chapter_id = ?",
                    (chapter_id,),
                )
            else:
                self._interrupt_generation_no_commit(
                    chapter_id,
                    lease.owner_id,
                    now=now,
                    error="interrupted",
                )
            chapter = self.get_chapter(chapter_id)
            if chapter is None:
                raise RuntimeError("chapter disappeared during generation recovery")
            return chapter

    def pending_chapter_markdown_sync(self, chapter_id: str) -> _PendingChapterMarkdownSync | None:
        return self._pending_chapter_publication(chapter_id)

    def publish_chapter_generation(
        self,
        chapter_id: str,
        owner_id: str,
        blocks: dict[str, tuple[str, str, int]],
        card: tuple[str, str],
        review_run_id: str,
        review_output: str,
        run_paths: dict[str, tuple[str, str]] | None = None,
        run_metadata: dict[str, tuple[str, tuple[str, ...]]] | None = None,
        expected_input_fingerprint: str = "",
        *,
        now: int | None = None,
    ) -> Chapter:
        now = _now() if now is None else now
        with self._atomic(immediate=True):
            lease = self.get_chapter_generation_lease(chapter_id)
            if lease is None or lease.owner_id != owner_id or lease.expires_at <= now:
                raise ChapterGenerationConflictError(
                    "lease_lost",
                    "chapter generation lease lost",
                )
            chapter = self.get_chapter(chapter_id)
            if chapter is None:
                raise ValueError("chapter not found")
            current_fingerprint = self.chapter_input_snapshot(chapter_id)[1]
            publication_input_fingerprint = expected_input_fingerprint or current_fingerprint
            if current_fingerprint != publication_input_fingerprint:
                raise ValueError("chapter input fingerprint changed")
            self.conn.execute("DELETE FROM wb_note_blocks WHERE chapter_id = ?", (chapter_id,))
            for kind, (title, body, seq) in blocks.items():
                self._upsert_note_block_no_commit(chapter_id, kind, title, body, seq)
            self.conn.execute(
                "DELETE FROM wb_cards WHERE chapter_id = ? AND kind = 'topic'", (chapter_id,)
            )
            self._create_card_no_commit(chapter.course_id, chapter_id, "topic", card[0], card[1])
            candidates = self.chapter_generation_candidates(chapter_id, owner_id)
            for round_key, output in candidates.items():
                input_path, output_path = (run_paths or {}).get(round_key, ("", ""))
                input_fingerprint, citation_ids = (run_metadata or {}).get(round_key, ("", ()))
                self._upsert_run_no_commit(
                    chapter_id,
                    round_key,
                    "generation",
                    "DONE",
                    input_path,
                    output_path,
                    output,
                    False,
                    input_fingerprint,
                    citation_ids,
                )
            review_fingerprint, review_citations = (run_metadata or {}).get("review", ("", ()))
            self._upsert_run_no_commit(
                chapter_id,
                "review",
                "generation",
                CHAPTER_SYNC_PENDING,
                "",
                "",
                review_output,
                False,
                review_fingerprint,
                review_citations,
            )
            review_cursor = self.conn.execute(
                "UPDATE wb_chapter_generation_runs SET status = ?, output = ?, error = '', "
                "error_code = '', error_message = '', "
                "finished_at = NULL "
                "WHERE id = ? AND owner_id = ? AND status = 'RUNNING'",
                (CHAPTER_SYNC_PENDING, review_output, review_run_id, owner_id),
            )
            if review_cursor.rowcount != 1:
                raise ChapterGenerationConflictError(
                    "run_not_running",
                    "chapter generation run is not RUNNING",
                )
            chapter_cursor = self.conn.execute(
                "UPDATE wb_chapters SET status = ?, updated_at = ? "
                "WHERE id = ? AND status = 'RUNNING'",
                (CHAPTER_SYNC_PENDING, now, chapter_id),
            )
            if chapter_cursor.rowcount != 1:
                raise ChapterGenerationConflictError(
                    "lease_lost",
                    "chapter generation lease lost",
                )
            output_fingerprint = self._chapter_publication_output_fingerprint(chapter_id)
            publication_id = uuid4().hex
            self.conn.execute(
                "INSERT INTO wb_chapter_generation_publications "
                "(chapter_id, publication_id, owner_id, review_run_id, input_fingerprint, "
                "output_fingerprint, status, error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, '', ?)",
                (
                    chapter_id,
                    publication_id,
                    owner_id,
                    review_run_id,
                    publication_input_fingerprint,
                    output_fingerprint,
                    CHAPTER_SYNC_PENDING,
                    now,
                ),
            )
            published_chapter = self.get_chapter(chapter_id)
            if published_chapter is None:
                raise RuntimeError("chapter disappeared during generation publish")
            return published_chapter

    def _validate_chapter_publication_no_commit(
        self,
        chapter_id: str,
        owner_id: str,
        review_run_id: str,
        publication_id: str,
        *,
        now: int,
    ) -> _PendingChapterMarkdownSync:
        lease = self.get_chapter_generation_lease(chapter_id)
        if lease is None or lease.owner_id != owner_id or lease.expires_at <= now:
            raise ChapterGenerationConflictError(
                "lease_lost",
                "chapter generation lease lost",
            )
        pending = self._pending_chapter_publication(chapter_id)
        if (
            pending is None
            or pending["publication_id"] != publication_id
            or pending["owner_id"] != owner_id
            or pending["review_run_id"] != review_run_id
        ):
            raise ChapterGenerationConflictError(
                "run_not_running",
                "chapter generation run is not staged",
            )
        if not self._pending_publication_is_current(chapter_id, pending):
            raise ChapterGenerationConflictError(
                "input_changed",
                "staged chapter input changed",
            )
        return pending

    def _validate_chapter_markdown_claim_no_commit(
        self,
        claim: MarkdownPublicationClaim,
        *,
        now: int,
    ) -> None:
        try:
            self._validate_markdown_publication_no_commit(claim, now=now)
        except ValueError as exc:
            message = str(exc)
            if "state changed" in message:
                raise ChapterGenerationConflictError(
                    "input_changed",
                    "staged chapter input changed",
                ) from exc
            raise ChapterGenerationConflictError(
                "lease_lost",
                "chapter generation lease lost",
            ) from exc

    def _finalize_chapter_generation_no_commit(
        self,
        chapter_id: str,
        owner_id: str,
        review_run_id: str,
        pending: _PendingChapterMarkdownSync,
        file_fingerprint: str,
        *,
        now: int,
    ) -> Chapter:
        review_cursor = self.conn.execute(
            "UPDATE wb_chapter_generation_runs SET status = 'COMPLETED', error = '', "
            "error_code = '', error_message = '', "
            "finished_at = ? WHERE id = ? AND owner_id = ? AND status = ?",
            (now, review_run_id, owner_id, CHAPTER_SYNC_PENDING),
        )
        if review_cursor.rowcount != 1:
            raise ChapterGenerationConflictError(
                "run_not_running",
                "chapter generation run is not SYNC_PENDING",
            )
        public_review_cursor = self.conn.execute(
            "UPDATE wb_runs SET status = 'DONE', stale = 0, updated_at = ? "
            "WHERE chapter_id = ? AND round_key = 'review' AND status = ?",
            (now, chapter_id, CHAPTER_SYNC_PENDING),
        )
        if public_review_cursor.rowcount != 1:
            raise ChapterGenerationConflictError(
                "run_not_running",
                "public chapter review is not SYNC_PENDING",
            )
        chapter_cursor = self.conn.execute(
            "UPDATE wb_chapters SET status = 'COMPLETED', updated_at = ? "
            "WHERE id = ? AND status = ?",
            (now, chapter_id, CHAPTER_SYNC_PENDING),
        )
        if chapter_cursor.rowcount != 1:
            raise ChapterGenerationConflictError(
                "lease_lost",
                "chapter generation lease lost",
            )
        self._refresh_topics_for_chapter_no_commit(chapter_id, now=now)
        self.conn.execute(
            "INSERT INTO wb_chapter_publication_receipts "
            "(chapter_id, publication_id, output_fingerprint, file_fingerprint, completed_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chapter_id) DO UPDATE SET publication_id = excluded.publication_id, "
            "output_fingerprint = excluded.output_fingerprint, "
            "file_fingerprint = excluded.file_fingerprint, "
            "completed_at = excluded.completed_at",
            (
                chapter_id,
                pending["publication_id"],
                pending["output_fingerprint"],
                file_fingerprint,
                now,
            ),
        )
        self.conn.execute(
            "DELETE FROM wb_chapter_generation_publications "
            "WHERE chapter_id = ? AND owner_id = ? AND publication_id = ?",
            (chapter_id, owner_id, pending["publication_id"]),
        )
        lease_cursor = self.conn.execute(
            "DELETE FROM wb_chapter_generation_leases WHERE chapter_id = ? AND owner_id = ?",
            (chapter_id, owner_id),
        )
        if lease_cursor.rowcount != 1:
            raise ChapterGenerationConflictError(
                "lease_lost",
                "chapter generation lease lost",
            )
        finalized = self.get_chapter(chapter_id)
        if finalized is None:
            raise RuntimeError("chapter disappeared during generation finalize")
        return finalized

    @contextmanager
    def fence_chapter_markdown_publication(
        self,
        chapter_id: str,
        owner_id: str,
        review_run_id: str,
        publication_id: str,
        *,
        claim: MarkdownPublicationClaim | None = None,
        clock: Callable[[], int] | None = None,
    ) -> Iterator[MarkdownPublicationFence]:
        current_time = clock or _now
        if claim is None:
            pending = self._pending_chapter_publication(chapter_id)
            lease = self.get_chapter_generation_lease(chapter_id)
            if pending is None or lease is None:
                raise ChapterGenerationConflictError(
                    "run_not_running",
                    "chapter generation run is not staged",
                )
            claimed_at = current_time()
            with self._atomic(immediate=True):
                self._validate_chapter_publication_no_commit(
                    chapter_id,
                    owner_id,
                    review_run_id,
                    publication_id,
                    now=claimed_at,
                )
            claim = self.claim_markdown_publication(
                "chapter",
                chapter_id,
                self.markdown_publication_state_fingerprint("chapter", chapter_id),
                pending["output_fingerprint"],
                owner_id=owner_id,
                publication_id=publication_id,
                now=claimed_at,
                lease_ttl=max(1, lease.expires_at - claimed_at),
            )
        if (
            claim.entity_type != "chapter"
            or claim.entity_id != chapter_id
            or claim.owner_id != owner_id
            or claim.publication_id != publication_id
        ):
            raise ValueError("chapter publication claim does not match generation")
        with self._atomic(immediate=True):
            self._validate_chapter_markdown_claim_no_commit(claim, now=current_time())
            pending = self._validate_chapter_publication_no_commit(
                chapter_id,
                owner_id,
                review_run_id,
                publication_id,
                now=current_time(),
            )
            fence = MarkdownPublicationFence(claim)
            yield fence
            if fence.file_fingerprint is None:
                raise RuntimeError("file publication receipt is required")
            finalized_at = current_time()
            self._validate_chapter_markdown_claim_no_commit(claim, now=finalized_at)
            pending = self._validate_chapter_publication_no_commit(
                chapter_id,
                owner_id,
                review_run_id,
                publication_id,
                now=finalized_at,
            )
            self._finalize_chapter_generation_no_commit(
                chapter_id,
                owner_id,
                review_run_id,
                pending,
                fence.file_fingerprint,
                now=finalized_at,
            )
            self._complete_markdown_publication_no_commit(fence, now=finalized_at)

    def chapter_publication_committed(self, chapter_id: str, publication_id: str) -> bool:
        if self.markdown_publication_committed("chapter", chapter_id, publication_id):
            return True
        row = self.conn.execute(
            "SELECT 1 FROM wb_chapter_publication_receipts "
            "WHERE chapter_id = ? AND publication_id = ?",
            (chapter_id, publication_id),
        ).fetchone()
        return row is not None

    def finalize_chapter_generation(
        self, chapter_id: str, owner_id: str, review_run_id: str, *, now: int | None = None
    ) -> Chapter:
        pending = self._pending_chapter_publication(chapter_id)
        if pending is None:
            raise ChapterGenerationConflictError(
                "run_not_running",
                "chapter generation run is not staged",
            )
        finalized_at = _now() if now is None else now
        try:
            with self.fence_chapter_markdown_publication(
                chapter_id,
                owner_id,
                review_run_id,
                pending["publication_id"],
                clock=lambda: finalized_at,
            ):
                pass
        except ChapterGenerationConflictError as exc:
            if exc.cause != "input_changed":
                raise
            with self._atomic(immediate=True):
                current = self._pending_chapter_publication(chapter_id)
                if current is not None and current["owner_id"] == owner_id:
                    self._discard_pending_publication_no_commit(
                        chapter_id,
                        owner_id,
                        now=finalized_at,
                    )
            raise
        finalized = self.get_chapter(chapter_id)
        if finalized is None:
            raise RuntimeError("chapter generation was not finalized")
        return finalized

    def create_course(self, title: str, description: str, root_dir: str) -> Course:
        row: _CourseRow = {
            "id": uuid4().hex,
            "title": title,
            "description": description,
            "root_dir": root_dir,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.conn.execute(
            """
            INSERT INTO wb_courses (id, title, description, root_dir, created_at, updated_at)
            VALUES (:id, :title, :description, :root_dir, :created_at, :updated_at)
            """,
            row,
        )
        self.conn.commit()
        return Course(**row)

    def get_course(self, course_id: str) -> Course | None:
        row = self.conn.execute(
            "SELECT * FROM wb_courses WHERE id = ?",
            (course_id,),
        ).fetchone()
        return Course(*row) if row else None

    def list_courses(self) -> list[Course]:
        rows = self.conn.execute("SELECT * FROM wb_courses ORDER BY updated_at DESC").fetchall()
        return [Course(*row) for row in rows]

    def create_topic(
        self,
        course_id: str,
        seq: int,
        title: str,
        description: str = "",
        generation_reason: str = "",
    ) -> CourseTopic:
        now = _now()
        row: _TopicRow = {
            "id": uuid4().hex,
            "course_id": course_id,
            "seq": seq,
            "title": title,
            "description": description,
            "status": "DRAFT",
            "confirmed": False,
            "stale_reason": "",
            "created_at": now,
            "updated_at": now,
            "generation_reason": generation_reason,
        }
        with self._atomic():
            self.conn.execute(
                """
                INSERT INTO wb_topics
                  (
                    id, course_id, seq, title, description, status, confirmed,
                    stale_reason, created_at, updated_at, generation_reason
                  )
                VALUES
                  (
                    :id, :course_id, :seq, :title, :description, :status, :confirmed,
                    :stale_reason, :created_at, :updated_at, :generation_reason
                  )
                """,
                {**row, "confirmed": int(row["confirmed"])},
            )
        return CourseTopic(**row)

    def create_topic_with_chapters(
        self,
        course_id: str,
        title: str,
        description: str = "",
        chapter_ids: list[str] | None = None,
    ) -> CourseTopic:
        with self._atomic(immediate=True):
            if self.get_course(course_id) is None:
                raise ValueError("course not found")
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM wb_topics WHERE course_id = ?",
                (course_id,),
            ).fetchone()[0]
            topic = self.create_topic(course_id, seq, title, description)
            if chapter_ids is not None:
                if not chapter_ids:
                    raise ValueError("chapter_ids must not be empty")
                self.replace_topic_chapters(topic.id, chapter_ids)
            self._mark_topic_markdown_pending(topic.id)
            return self._topic_by_id(topic.id)

    def confirm_course_topics(self, course_id: str) -> list[CourseTopic]:
        with self._atomic(immediate=True):
            if self.get_course(course_id) is None:
                raise ValueError("course not found")
            topics = self.list_topics(course_id)
            if not topics:
                raise ValueError("course has no topics")
            for topic in topics:
                chapters = self.list_topic_chapters(topic.id)
                if not chapters or any(chapter.course_id != course_id for chapter in chapters):
                    raise ValueError("every topic must map to this course")
            now = _now()
            self.conn.execute(
                "UPDATE wb_topics SET confirmed = 1, updated_at = ? WHERE course_id = ?",
                (now, course_id),
            )
            for topic in topics:
                status = self._topic_readiness_status(self._topic_by_id(topic.id))
                self.conn.execute(
                    "UPDATE wb_topics SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, topic.id),
                )
                self._mark_topic_markdown_pending(topic.id, now=now)
        return self.list_topics(course_id)

    def edit_topic_content(
        self, topic_id: str, *, title: str | None = None, description: str | None = None
    ) -> CourseTopic:
        with self._atomic(immediate=True):
            topic = self._topic_by_id(topic_id)
            if topic.status == "RUNNING":
                raise ValueError("topic is already running")
            self.update_topic(topic_id, title=title, description=description)
            self._mark_topic_markdown_pending(topic_id)
            if self._has_published_topic_output(topic_id):
                return self.mark_topic_stale(topic_id, "topic metadata changed")
            return self.refresh_topic_status(topic_id)

    def replace_topic_chapters_and_refresh(
        self, topic_id: str, chapter_ids: list[str]
    ) -> CourseTopic:
        with self._atomic(immediate=True):
            topic = self._topic_by_id(topic_id)
            if topic.status == "RUNNING":
                raise ValueError("topic is already running")
            self.replace_topic_chapters(topic_id, chapter_ids)
            self._mark_topic_markdown_pending(topic_id)
            if self._has_published_topic_output(topic_id):
                return self.mark_topic_stale(topic_id, "topic chapter mapping changed")
            return self.refresh_topic_status(topic_id)

    def delete_topic_guarded(self, topic_id: str) -> None:
        with self._atomic(immediate=True):
            topic = self._topic_by_id(topic_id)
            if topic.status == "RUNNING":
                raise ValueError("topic is already running")
            if self._has_published_topic_output(topic_id):
                raise ValueError("topic with published output is protected")
            self.delete_topic(topic_id)

    def merge_topics(
        self,
        course_id: str,
        topic_ids: list[str],
        *,
        title: str,
        description: str = "",
        chapter_ids: list[str] | None = None,
    ) -> CourseTopic:
        with self._atomic(immediate=True):
            topics = [self._topic_by_id(topic_id) for topic_id in topic_ids]
            if len(topic_ids) < 2 or len(topic_ids) != len(set(topic_ids)):
                raise ValueError("at least two unique topics are required")
            if any(topic.course_id != course_id for topic in topics):
                raise ValueError("all topics must belong to the same course")
            if any(topic.status == "RUNNING" for topic in topics):
                raise ValueError("running topic is protected")
            if any(self._has_published_topic_output(topic.id) for topic in topics):
                raise ValueError("topic with published output is protected")
            merged_chapter_ids = chapter_ids
            if merged_chapter_ids is None:
                merged_chapter_ids = list(
                    dict.fromkeys(
                        chapter.id
                        for topic in topics
                        for chapter in self.list_topic_chapters(topic.id)
                    )
                )
            if not merged_chapter_ids:
                raise ValueError("merged topic must have chapters")
            merged = self.create_topic_with_chapters(
                course_id, title, description, merged_chapter_ids
            )
            for topic in topics:
                self.delete_topic(topic.id)
            self.update_topic(
                merged.id,
                status="DRAFT",
                confirmed=False,
                stale_reason="",
            )
            self._mark_topic_markdown_pending(merged.id)
            return self._topic_by_id(merged.id)

    def split_topic(
        self,
        topic_id: str,
        *,
        title: str,
        description: str = "",
        new_chapter_ids: list[str],
    ) -> tuple[CourseTopic, CourseTopic]:
        with self._atomic(immediate=True):
            original = self._topic_by_id(topic_id)
            if original.status == "RUNNING":
                raise ValueError("running topic is protected")
            original_ids = [chapter.id for chapter in self.list_topic_chapters(topic_id)]
            new_ids = list(dict.fromkeys(new_chapter_ids))
            if not new_ids or not set(new_ids) < set(original_ids):
                raise ValueError("new chapters must be a nonempty proper subset")
            remaining_ids = [chapter_id for chapter_id in original_ids if chapter_id not in new_ids]
            new_topic = self.create_topic_with_chapters(
                original.course_id, title, description, new_ids
            )
            self.replace_topic_chapters(topic_id, remaining_ids)
            if self._has_published_topic_output(topic_id):
                self.update_topic(
                    topic_id,
                    status="STALE",
                    stale_reason="topic chapter mapping changed by split",
                )
            else:
                self.update_topic(
                    topic_id,
                    status="DRAFT",
                    confirmed=False,
                    stale_reason="",
                )
            self._mark_topic_markdown_pending(topic_id)
            self._mark_topic_markdown_pending(new_topic.id)
            return self._topic_by_id(topic_id), self._topic_by_id(new_topic.id)

    def get_topic(self, topic_id: str) -> CourseTopic | None:
        row = self.conn.execute("SELECT * FROM wb_topics WHERE id = ?", (topic_id,)).fetchone()
        return self._topic(row) if row else None

    def list_topics(self, course_id: str) -> list[CourseTopic]:
        rows = self.conn.execute(
            "SELECT * FROM wb_topics WHERE course_id = ? ORDER BY seq, id",
            (course_id,),
        ).fetchall()
        return [self._topic(row) for row in rows]

    def course_topic_api_state(self, course_id: str) -> dict[str, _TopicApiState]:
        state: dict[str, _TopicApiState] = {
            topic.id: {"chapter_ids": [], "blocking_chapter_ids": [], "sync": None}
            for topic in self.list_topics(course_id)
        }
        links = self.conn.execute(
            """
            SELECT tc.topic_id, c.id, r.status, COALESCE(r.stale, 0)
            FROM wb_topic_chapters tc
            JOIN wb_topics t ON t.id = tc.topic_id
            JOIN wb_chapters c ON c.id = tc.chapter_id
            LEFT JOIN wb_runs r ON r.chapter_id = c.id AND r.round_key = 'review'
            WHERE t.course_id = ?
            ORDER BY t.seq, c.source_id, c.seq, c.id
            """,
            (course_id,),
        ).fetchall()
        for topic_id, chapter_id, review_status, stale in links:
            state[topic_id]["chapter_ids"].append(chapter_id)
            if review_status != "DONE" or stale:
                state[topic_id]["blocking_chapter_ids"].append(chapter_id)
        sync_rows = self.conn.execute(
            """
            SELECT s.* FROM wb_topic_markdown_sync s
            JOIN wb_topics t ON t.id = s.topic_id
            WHERE t.course_id = ?
            """,
            (course_id,),
        ).fetchall()
        for row in sync_rows:
            state[row[0]]["sync"] = TopicMarkdownSyncState(*row)
        return state

    def replace_course_topic_drafts(
        self,
        course_id: str,
        topic_specs: Sequence[Mapping[str, object]],
        expected_fingerprint: str,
    ) -> list[CourseTopic]:
        with self._atomic(immediate=True):
            _, current_fingerprint = self.course_topic_outline_snapshot(course_id)
            if current_fingerprint != expected_fingerprint:
                raise ValueError("course outline input snapshot changed")
            topics = self.list_topics(course_id)
            protected = [
                topic.id
                for topic in topics
                if topic.confirmed
                or topic.status != "DRAFT"
                or self._has_published_topic_output(topic.id)
            ]
            if protected:
                raise ValueError(f"protected topic prevents replacement: {', '.join(protected)}")

            normalized_specs = [_topic_draft_spec(spec) for spec in topic_specs]
            chapter_ids = [
                chapter_id for spec in normalized_specs for chapter_id in spec["chapter_ids"]
            ]
            chapters = self._chapters_by_ids(list(dict.fromkeys(chapter_ids)))
            if len(chapters) != len(set(chapter_ids)) or any(
                chapter.course_id != course_id for chapter in chapters
            ):
                raise ValueError("all chapters must exist and belong to the course")

            self.conn.execute(
                "DELETE FROM wb_topics WHERE course_id = ? AND confirmed = 0",
                (course_id,),
            )
            now = _now()
            for seq, spec in enumerate(normalized_specs):
                topic_id = uuid4().hex
                self.conn.execute(
                    """
                    INSERT INTO wb_topics
                      (id, course_id, seq, title, description, status, confirmed,
                       stale_reason, created_at, updated_at, generation_reason)
                    VALUES (?, ?, ?, ?, ?, 'DRAFT', 0, '', ?, ?, ?)
                    """,
                    (
                        topic_id,
                        course_id,
                        seq,
                        spec["title"],
                        spec["description"],
                        now,
                        now,
                        spec["reason"],
                    ),
                )
                self.conn.executemany(
                    """
                    INSERT INTO wb_topic_chapters (topic_id, chapter_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    ((topic_id, chapter_id, now) for chapter_id in spec["chapter_ids"]),
                )
                self._mark_topic_markdown_pending(topic_id, now=now)
        return self.list_topics(course_id)

    def course_topic_outline_snapshot(
        self, course_id: str
    ) -> tuple[_CourseTopicOutlineSnapshot, str]:
        rows = self.conn.execute(
            """
            SELECT
              co.id, co.title, co.description, co.updated_at,
              c.id, c.status, c.updated_at, c.seq, c.title,
              s.id, s.title,
              r.status, COALESCE(r.stale, 0), r.updated_at, r.output,
              n.kind, n.seq, n.title, n.body, n.updated_at
            FROM wb_courses co
            LEFT JOIN wb_chapters c
              ON c.course_id = co.id AND c.status IN ('CONFIRMED', 'COMPLETED')
            LEFT JOIN wb_sources s ON s.id = c.source_id
            LEFT JOIN wb_runs r
              ON r.chapter_id = c.id AND r.round_key = 'review'
            LEFT JOIN wb_note_blocks n ON n.chapter_id = c.id
            WHERE co.id = ?
            ORDER BY s.title, s.id, c.seq, c.id, n.seq, n.kind, n.id
            """,
            (course_id,),
        ).fetchall()
        if not rows:
            raise ValueError("course not found")

        first = rows[0]
        snapshot: _CourseTopicOutlineSnapshot = {
            "course": {
                "id": first[0],
                "title": first[1],
                "description": first[2],
                "updated_at": first[3],
            },
            "chapters": [],
        }
        by_id: dict[str, _CourseOutlineChapter] = {}
        for row in rows:
            chapter_id = row[4]
            if chapter_id is None:
                continue
            chapter = by_id.get(chapter_id)
            if chapter is None:
                chapter = {
                    "id": chapter_id,
                    "status": row[5],
                    "updated_at": row[6],
                    "seq": row[7],
                    "title": row[8],
                    "source": {"id": row[9], "title": row[10]},
                    "review": {
                        "status": row[11],
                        "stale": bool(row[12]),
                        "updated_at": row[13],
                        "output": row[14],
                    },
                    "notes": [],
                }
                by_id[chapter_id] = chapter
                snapshot["chapters"].append(chapter)
            if row[15] is not None:
                chapter["notes"].append(
                    {
                        "kind": row[15],
                        "seq": row[16],
                        "title": row[17],
                        "body": row[18],
                        "updated_at": row[19],
                    }
                )
        return snapshot, _stable_fingerprint(snapshot)

    def update_topic(
        self,
        topic_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        confirmed: bool | None = None,
        stale_reason: str | None = None,
    ) -> CourseTopic:
        if confirmed is not None and type(confirmed) is not bool:
            raise TypeError("confirmed must be bool")
        values = {
            "title": title,
            "description": description,
            "status": status,
            "confirmed": int(confirmed) if confirmed is not None else None,
            "stale_reason": stale_reason,
        }
        assignments = [f"{name} = ?" for name, value in values.items() if value is not None]
        with self._atomic():
            if assignments:
                params = [value for value in values.values() if value is not None]
                params.extend((_now(), topic_id))
                cursor = self.conn.execute(
                    f"UPDATE wb_topics SET {', '.join(assignments)}, updated_at = ? WHERE id = ?",
                    params,
                )
                if cursor.rowcount == 0:
                    raise ValueError("topic not found")
            topic = self.get_topic(topic_id)
            if topic is None:
                raise ValueError("topic not found")
            return topic

    def reorder_topics(self, course_id: str, topic_ids: list[str]) -> list[CourseTopic]:
        with self._atomic(immediate=True):
            rows = self.conn.execute(
                "SELECT id, seq FROM wb_topics WHERE course_id = ? ORDER BY seq, id",
                (course_id,),
            ).fetchall()
            current_ids = [row[0] for row in rows]
            if len(topic_ids) != len(set(topic_ids)) or set(topic_ids) != set(current_ids):
                raise ValueError("topic_ids must contain every course topic exactly once")

            temporary_sequences = _temporary_topic_sequences(
                [row[1] for row in rows],
                len(rows),
            )
            if len(temporary_sequences) != len(current_ids):
                raise RuntimeError("temporary topic sequence count mismatch")
            for index, temporary_seq in enumerate(temporary_sequences):
                topic_id = current_ids[index]
                self.conn.execute(
                    "UPDATE wb_topics SET seq = ? WHERE id = ?",
                    (temporary_seq, topic_id),
                )
            now = _now()
            for seq, topic_id in enumerate(topic_ids):
                self.conn.execute(
                    "UPDATE wb_topics SET seq = ?, updated_at = ? WHERE id = ?",
                    (seq, now, topic_id),
                )
                self._mark_topic_markdown_pending(topic_id, now=now)
        return self.list_topics(course_id)

    def _mark_topic_markdown_pending(self, topic_id: str, *, now: int | None = None) -> None:
        now = _now() if now is None else now
        self.conn.execute(
            """
            INSERT INTO wb_topic_markdown_sync (topic_id, status, error, updated_at)
            VALUES (?, 'PENDING', '', ?)
            ON CONFLICT(topic_id) DO UPDATE SET
              status = 'PENDING', error = '', updated_at = excluded.updated_at,
              owner_id = '', lease_expires_at = 0, error_code = '', error_message = ''
            """,
            (topic_id, now),
        )

    def delete_topic(self, topic_id: str) -> None:
        with self._atomic():
            self.conn.execute("DELETE FROM wb_topics WHERE id = ?", (topic_id,))

    def replace_topic_chapters(self, topic_id: str, chapter_ids: list[str]) -> list[Chapter]:
        with self._atomic(immediate=True):
            topic = self.get_topic(topic_id)
            if topic is None:
                raise ValueError("topic not found")
            unique_ids = list(dict.fromkeys(chapter_ids))
            chapters = self._chapters_by_ids(unique_ids)
            wrong_course = any(chapter.course_id != topic.course_id for chapter in chapters)
            if len(chapters) != len(unique_ids) or wrong_course:
                raise ValueError("all chapters must exist and belong to the topic course")

            now = _now()
            self.conn.execute("DELETE FROM wb_topic_chapters WHERE topic_id = ?", (topic_id,))
            self.conn.executemany(
                "INSERT INTO wb_topic_chapters (topic_id, chapter_id, created_at) VALUES (?, ?, ?)",
                ((topic_id, chapter_id, now) for chapter_id in unique_ids),
            )
        return self.list_topic_chapters(topic_id)

    def list_topic_chapters(self, topic_id: str) -> list[Chapter]:
        rows = self.conn.execute(
            """
            SELECT c.*
            FROM wb_chapters c
            JOIN wb_topic_chapters tc ON tc.chapter_id = c.id
            WHERE tc.topic_id = ?
            ORDER BY c.source_id, c.seq, c.id
            """,
            (topic_id,),
        ).fetchall()
        return [Chapter(*row) for row in rows]

    def list_topics_for_chapter(self, chapter_id: str) -> list[CourseTopic]:
        rows = self.conn.execute(
            """
            SELECT t.*
            FROM wb_topics t
            JOIN wb_topic_chapters tc ON tc.topic_id = t.id
            WHERE tc.chapter_id = ?
            ORDER BY t.seq, t.id
            """,
            (chapter_id,),
        ).fetchall()
        return [self._topic(row) for row in rows]

    def list_topic_chapter_reviews(
        self,
        topic_id: str,
    ) -> list[tuple[str, str | None, bool]]:
        rows = self.conn.execute(
            """
            SELECT c.id, r.status, COALESCE(r.stale, 0)
            FROM wb_chapters c
            JOIN wb_topic_chapters tc ON tc.chapter_id = c.id
            LEFT JOIN wb_runs r
              ON r.chapter_id = c.id AND r.round_key = 'review'
            WHERE tc.topic_id = ?
            ORDER BY c.source_id, c.seq, c.id
            """,
            (topic_id,),
        ).fetchall()
        return [(row[0], row[1], bool(row[2])) for row in rows]

    def topic_input_snapshot(self, topic_id: str) -> tuple[_TopicInputSnapshot, str]:
        rows = self.conn.execute(
            """
            SELECT t.id, t.course_id, t.title, t.description, t.confirmed, t.updated_at,
                   tc.created_at, s.id, s.title, s.updated_at,
                   c.id, c.seq, c.title, c.status, c.updated_at,
                   r.status, COALESCE(r.stale, 0), r.updated_at, r.output,
                   n.id, n.kind, n.title, n.body, n.seq, n.updated_at
            FROM wb_topics t
            LEFT JOIN wb_topic_chapters tc ON tc.topic_id = t.id
            LEFT JOIN wb_chapters c ON c.id = tc.chapter_id
            LEFT JOIN wb_sources s ON s.id = c.source_id
            LEFT JOIN wb_runs r ON r.chapter_id = c.id AND r.round_key = 'review'
            LEFT JOIN wb_note_blocks n ON n.chapter_id = c.id
            WHERE t.id = ?
            ORDER BY s.title, s.id, c.seq, c.id, n.seq, n.kind, n.id
            """,
            (topic_id,),
        ).fetchall()
        if not rows:
            raise ValueError("topic not found")
        first = rows[0]
        snapshot: _TopicInputSnapshot = {
            "topic": {
                "id": first[0],
                "course_id": first[1],
                "title": first[2],
                "description": first[3],
                "confirmed": bool(first[4]),
            },
            "chapters": [],
        }
        chapters: dict[str, _TopicInputChapter] = {}
        for row in rows:
            if row[10] is None:
                continue
            chapter = chapters.get(row[10])
            if chapter is None:
                chapter = {
                    "mapping_created_at": row[6],
                    "source": {"id": row[7], "title": row[8], "updated_at": row[9]},
                    "id": row[10],
                    "seq": row[11],
                    "title": row[12],
                    "status": row[13],
                    "updated_at": row[14],
                    "review": {
                        "status": row[15],
                        "stale": bool(row[16]),
                        "updated_at": row[17],
                        "output": row[18],
                    },
                    "notes": [],
                }
                chapters[row[10]] = chapter
                snapshot["chapters"].append(chapter)
            if row[19] is not None:
                chapter["notes"].append(
                    {
                        "id": row[19],
                        "kind": row[20],
                        "title": row[21],
                        "body": row[22],
                        "seq": row[23],
                        "updated_at": row[24],
                    }
                )
        return snapshot, _stable_fingerprint(snapshot)

    def start_topic_generation(
        self,
        topic_id: str,
        expected_fingerprint: str,
        *,
        now: int | None = None,
        lease_ttl: int = 7_200,
    ) -> TopicGenerationStart:
        now = _now() if now is None else now
        if lease_ttl <= 0:
            raise ValueError("lease_ttl must be positive")
        with self._atomic(immediate=True):
            topic = self._topic_by_id(topic_id)
            if topic.status == "RUNNING":
                raise ValueError("topic is already running")
            if topic.status in {"DRAFT", "NOT_READY"}:
                raise ValueError("topic is not ready")
            if topic.status not in {"READY", "STALE", "FAILED"}:
                raise ValueError("topic cannot be started")
            snapshot, fingerprint = self.topic_input_snapshot(topic_id)
            if fingerprint != expected_fingerprint:
                raise ValueError("topic input changed")
            if (
                not snapshot["topic"]["confirmed"]
                or not snapshot["chapters"]
                or any(
                    chapter["review"]["status"] != "DONE" or chapter["review"]["stale"]
                    for chapter in snapshot["chapters"]
                )
            ):
                raise ValueError("topic dependencies are not ready")
            baseline = topic.stale_reason
            cursor = self.conn.execute(
                "UPDATE wb_topics SET status = 'RUNNING', updated_at = ? "
                "WHERE id = ? AND status = ?",
                (_now(), topic_id, topic.status),
            )
            if cursor.rowcount != 1:
                raise ValueError("topic is already running")
            owner_id = uuid4().hex
            self.conn.execute(
                """
                INSERT INTO wb_topic_generation_leases
                  (topic_id, owner_id, heartbeat_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (topic_id, owner_id, now, now + lease_ttl),
            )
            return TopicGenerationStart(
                self._topic_by_id(topic_id), fingerprint, baseline, owner_id
            )

    def get_topic_generation_lease(self, topic_id: str) -> TopicGenerationLease | None:
        row = self.conn.execute(
            "SELECT * FROM wb_topic_generation_leases WHERE topic_id = ?",
            (topic_id,),
        ).fetchone()
        return TopicGenerationLease(*row) if row else None

    def heartbeat_topic_generation(
        self,
        topic_id: str,
        owner_id: str,
        *,
        now: int | None = None,
        lease_ttl: int = 7_200,
    ) -> TopicGenerationLease:
        now = _now() if now is None else now
        if lease_ttl <= 0:
            raise ValueError("lease_ttl must be positive")
        with self._atomic(immediate=True):
            cursor = self.conn.execute(
                """
                UPDATE wb_topic_generation_leases
                SET heartbeat_at = ?, expires_at = ?
                WHERE topic_id = ? AND owner_id = ? AND expires_at > ?
                """,
                (now, now + lease_ttl, topic_id, owner_id, now),
            )
            if cursor.rowcount != 1:
                raise ValueError("topic generation lease lost")
            lease = self.get_topic_generation_lease(topic_id)
            if lease is None:
                raise ValueError("topic generation lease lost")
            return lease

    def publish_topic_generation(
        self,
        topic_id: str,
        expected_fingerprint: str,
        stale_reason_baseline: str,
        blocks: dict[str, str],
        cards: Sequence[Mapping[str, object]],
        *,
        review_run_id: str,
        owner_id: str,
        review_output: str,
        now: int | None = None,
    ) -> CourseTopic:
        now = _now() if now is None else now
        with self._atomic(immediate=True):
            topic = self._topic_by_id(topic_id)
            if topic.status != "RUNNING":
                raise ValueError("topic is not running")
            _, fingerprint = self.topic_input_snapshot(topic_id)
            if fingerprint != expected_fingerprint or topic.stale_reason != stale_reason_baseline:
                raise ValueError("topic input changed")
            lease = self.conn.execute(
                """
                SELECT 1 FROM wb_topic_generation_leases
                WHERE topic_id = ? AND owner_id = ? AND expires_at > ?
                """,
                (topic_id, owner_id, now),
            ).fetchone()
            if lease is None:
                raise ValueError("topic generation lease lost")
            self.replace_topic_note_blocks(topic_id, blocks)
            self.replace_topic_cards(topic_id, cards)
            cursor = self.conn.execute(
                """
                UPDATE wb_topic_runs
                SET status = 'COMPLETED', output = ?, error = '', error_code = '',
                    error_message = '', finished_at = ?
                WHERE id = ? AND topic_id = ? AND round_key = 'review' AND status = 'RUNNING'
                """,
                (review_output, now, review_run_id, topic_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("review topic run is not RUNNING")
            self.conn.execute(
                "UPDATE wb_topics SET status = 'COMPLETED', stale_reason = '', updated_at = ? "
                "WHERE id = ? AND status = 'RUNNING'",
                (now, topic_id),
            )
            self.conn.execute(
                "DELETE FROM wb_topic_generation_leases WHERE topic_id = ? AND owner_id = ?",
                (topic_id, owner_id),
            )
            self.conn.execute(
                """
                INSERT INTO wb_topic_markdown_sync (topic_id, status, error, updated_at)
                VALUES (?, 'PENDING', '', ?)
                ON CONFLICT(topic_id) DO UPDATE SET
                  status = 'PENDING', error = '', updated_at = excluded.updated_at,
                  owner_id = '', lease_expires_at = 0,
                  error_code = '', error_message = ''
                """,
                (topic_id, now),
            )
            return self._topic_by_id(topic_id)

    def get_topic_markdown_sync_state(self, topic_id: str) -> TopicMarkdownSyncState | None:
        row = self.conn.execute(
            "SELECT * FROM wb_topic_markdown_sync WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        return TopicMarkdownSyncState(*row) if row else None

    def set_topic_markdown_sync_state(
        self, topic_id: str, status: str, error: str = ""
    ) -> TopicMarkdownSyncState:
        if status not in {"PENDING", "SYNCED", "FAILED"}:
            raise ValueError("invalid topic Markdown sync status")
        if self.get_topic(topic_id) is None:
            raise ValueError("topic not found")
        now = _now()
        stored_error = _topic_publication_error() if status == "FAILED" else _NO_PERSISTED_ERROR
        with self._atomic():
            self.conn.execute(
                """
                INSERT INTO wb_topic_markdown_sync
                  (topic_id, status, error, updated_at, error_code, error_message)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(topic_id) DO UPDATE SET
                  status = excluded.status, error = excluded.error,
                  updated_at = excluded.updated_at, owner_id = '', lease_expires_at = 0,
                  error_code = excluded.error_code,
                  error_message = excluded.error_message
                """,
                (
                    topic_id,
                    status,
                    stored_error.message,
                    now,
                    stored_error.code,
                    stored_error.message,
                ),
            )
        state = self.get_topic_markdown_sync_state(topic_id)
        if state is None:
            raise RuntimeError("topic Markdown sync state was not persisted")
        return state

    def claim_topic_markdown_sync(
        self, topic_id: str, *, now: int | None = None, lease_ttl: int = 600
    ) -> TopicMarkdownSyncState:
        if lease_ttl <= 0:
            raise ValueError("lease_ttl must be positive")
        now = _now() if now is None else now
        owner_id = uuid4().hex
        with self._atomic(immediate=True):
            cursor = self.conn.execute(
                """
                UPDATE wb_topic_markdown_sync
                SET status = 'SYNCING', error = '', updated_at = ?, owner_id = ?,
                    lease_expires_at = ?, error_code = '', error_message = ''
                WHERE topic_id = ? AND (
                  status IN ('PENDING', 'FAILED', 'SYNCED')
                  OR (status = 'SYNCING' AND lease_expires_at <= ?)
                )
                """,
                (now, owner_id, now + lease_ttl, topic_id, now),
            )
            if cursor.rowcount != 1:
                state = self.get_topic_markdown_sync_state(topic_id)
                if state is not None and state.status == "SYNCING":
                    raise ValueError("topic Markdown is already syncing")
                raise ValueError("topic Markdown is not pending or failed")
        state = self.get_topic_markdown_sync_state(topic_id)
        if state is None:
            raise RuntimeError("topic Markdown sync claim was not persisted")
        return state

    def finish_topic_markdown_sync(
        self,
        topic_id: str,
        owner_id: str,
        status: str,
        error: str = "",
        *,
        now: int | None = None,
    ) -> TopicMarkdownSyncState:
        if status not in {"SYNCED", "FAILED"}:
            raise ValueError("invalid finished topic Markdown sync status")
        now = _now() if now is None else now
        stored_error = _topic_publication_error() if status == "FAILED" else _NO_PERSISTED_ERROR
        with self._atomic(immediate=True):
            cursor = self.conn.execute(
                """
                UPDATE wb_topic_markdown_sync
                SET status = ?, error = ?, error_code = ?, error_message = ?,
                    updated_at = ?, owner_id = '', lease_expires_at = 0
                WHERE topic_id = ? AND status = 'SYNCING' AND owner_id = ?
                """,
                (
                    status,
                    stored_error.message,
                    stored_error.code,
                    stored_error.message,
                    now,
                    topic_id,
                    owner_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("topic Markdown sync owner lost")
        state = self.get_topic_markdown_sync_state(topic_id)
        if state is None:
            raise RuntimeError("topic Markdown sync finish was not persisted")
        return state

    def fence_topic_markdown_sync(
        self,
        topic_id: str,
        owner_id: str,
        *,
        now: int | None = None,
        lease_ttl: int = 600,
    ) -> TopicMarkdownSyncState:
        if lease_ttl <= 0:
            raise ValueError("lease_ttl must be positive")
        now = _now() if now is None else now
        with self._atomic(immediate=True):
            cursor = self.conn.execute(
                """
                UPDATE wb_topic_markdown_sync
                SET updated_at = ?, lease_expires_at = ?
                WHERE topic_id = ? AND status = 'SYNCING' AND owner_id = ?
                  AND lease_expires_at > ?
                """,
                (now, now + lease_ttl, topic_id, owner_id, now),
            )
            if cursor.rowcount != 1:
                raise ValueError("topic Markdown sync owner lost")
        state = self.get_topic_markdown_sync_state(topic_id)
        if state is None:
            raise RuntimeError("topic Markdown sync fence was not persisted")
        return state

    @contextmanager
    def fence_topic_markdown_publication(
        self,
        claim: MarkdownPublicationClaim,
        *,
        clock: Callable[[], int] | None = None,
    ) -> Iterator[MarkdownPublicationFence]:
        if claim.entity_type != "topic":
            raise ValueError("topic publication claim is invalid")
        current_time = clock or _now
        with self._atomic(immediate=True):
            now = current_time()
            state = self.get_topic_markdown_sync_state(claim.entity_id)
            if (
                state is None
                or state.status != "SYNCING"
                or state.owner_id != claim.owner_id
                or state.lease_expires_at <= now
            ):
                raise ValueError("topic Markdown sync owner lost")
            self._validate_markdown_publication_no_commit(claim, now=now)
            fence = MarkdownPublicationFence(claim)
            yield fence
            completed_at = current_time()
            if fence.file_fingerprint is None:
                raise RuntimeError("file publication receipt is required")
            state = self.get_topic_markdown_sync_state(claim.entity_id)
            if (
                state is None
                or state.status != "SYNCING"
                or state.owner_id != claim.owner_id
                or state.lease_expires_at <= completed_at
            ):
                raise ValueError("topic Markdown sync owner lost")
            self._validate_markdown_publication_no_commit(claim, now=completed_at)
            cursor = self.conn.execute(
                "UPDATE wb_topic_markdown_sync SET status = 'SYNCED', error = '', "
                "error_code = '', error_message = '', "
                "updated_at = ?, owner_id = '', lease_expires_at = 0 "
                "WHERE topic_id = ? AND status = 'SYNCING' AND owner_id = ?",
                (completed_at, claim.entity_id, claim.owner_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("topic Markdown sync owner lost")
            self._complete_markdown_publication_no_commit(fence, now=completed_at)

    def fail_topic_generation(self, topic_id: str, owner_id: str) -> CourseTopic:
        with self._atomic(immediate=True):
            topic = self._topic_by_id(topic_id)
            if topic.status != "RUNNING":
                return topic
            lease = self.conn.execute(
                """
                SELECT 1 FROM wb_topic_generation_leases
                WHERE topic_id = ? AND owner_id = ?
                """,
                (topic_id, owner_id),
            ).fetchone()
            if lease is None:
                raise ValueError("topic generation lease lost")
            published = self.conn.execute(
                """
                SELECT
                  EXISTS(SELECT 1 FROM wb_topic_note_blocks WHERE topic_id = ?)
                  OR EXISTS(SELECT 1 FROM wb_topic_cards WHERE topic_id = ?)
                """,
                (topic_id, topic_id),
            ).fetchone()[0]
            if published:
                status = "STALE"
            else:
                status = self._topic_readiness_status(topic)
                if status == "READY":
                    status = "FAILED"
            self.conn.execute(
                "UPDATE wb_topics SET status = ?, updated_at = ? "
                "WHERE id = ? AND status = 'RUNNING'",
                (status, _now(), topic_id),
            )
            self.conn.execute(
                "DELETE FROM wb_topic_generation_leases WHERE topic_id = ? AND owner_id = ?",
                (topic_id, owner_id),
            )
            return self._topic_by_id(topic_id)

    def recover_interrupted_topic_run(
        self, topic_id: str, *, now: int | None = None, owner_id: str | None = None
    ) -> CourseTopic:
        now = _now() if now is None else now
        with self._atomic(immediate=True):
            topic = self._topic_by_id(topic_id)
            if topic.status != "RUNNING":
                raise ValueError("topic is not running")
            lease = self.get_topic_generation_lease(topic_id)
            if lease is not None:
                if owner_id is not None and lease.owner_id != owner_id:
                    raise ValueError("topic generation lease owner mismatch")
                if lease.expires_at > now:
                    raise ValueError("topic generation lease not expired")
            stored_error = _topic_generation_error("interrupted")
            self.conn.execute(
                """
                UPDATE wb_topic_runs
                SET status = 'FAILED', output = '', error = ?, error_code = ?,
                    error_message = ?, finished_at = ?
                WHERE topic_id = ? AND status = 'RUNNING'
                """,
                (
                    stored_error.message,
                    stored_error.code,
                    stored_error.message,
                    now,
                    topic_id,
                ),
            )
            published = self.conn.execute(
                """
                SELECT
                  EXISTS(SELECT 1 FROM wb_topic_note_blocks WHERE topic_id = ?)
                  OR EXISTS(SELECT 1 FROM wb_topic_cards WHERE topic_id = ?)
                """,
                (topic_id, topic_id),
            ).fetchone()[0]
            if published:
                status = "STALE"
            else:
                status = self._topic_readiness_status(topic)
                if status == "READY":
                    status = "FAILED"
            self.conn.execute(
                "UPDATE wb_topics SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, topic_id),
            )
            if lease is not None:
                self.conn.execute(
                    "DELETE FROM wb_topic_generation_leases "
                    "WHERE topic_id = ? AND owner_id = ? AND expires_at <= ?",
                    (topic_id, lease.owner_id, now),
                )
            return self._topic_by_id(topic_id)

    def has_published_topic_output(self, topic_id: str) -> bool:
        return self._has_published_topic_output(topic_id)

    def _has_published_topic_output(self, topic_id: str) -> bool:
        row = self.conn.execute(
            """
            SELECT
              EXISTS(SELECT 1 FROM wb_topic_note_blocks WHERE topic_id = ?)
              OR EXISTS(SELECT 1 FROM wb_topic_cards WHERE topic_id = ?)
              OR EXISTS(
                SELECT 1 FROM wb_topic_runs
                WHERE topic_id = ? AND status = 'COMPLETED'
              )
            """,
            (topic_id, topic_id, topic_id),
        ).fetchone()
        return bool(row[0])

    def invalidate_chapter_dependencies(
        self,
        chapter_id: str,
        round_keys: list[str],
        reason: str,
    ) -> list[CourseTopic]:
        reason = _normalize_stale_reason(reason)

        with self._atomic(immediate=True):
            if round_keys:
                placeholders = ", ".join("?" for _ in round_keys)
                self.conn.execute(
                    f"""
                    UPDATE wb_runs
                    SET stale = 1, updated_at = ?
                    WHERE chapter_id = ? AND round_key IN ({placeholders})
                    """,
                    (_now(), chapter_id, *round_keys),
                )
            rows = self.conn.execute(
                """
                SELECT t.*
                FROM wb_topics t
                JOIN wb_topic_chapters tc ON tc.topic_id = t.id
                WHERE tc.chapter_id = ?
                ORDER BY t.seq, t.id
                """,
                (chapter_id,),
            ).fetchall()
            topic_ids = [row[0] for row in rows]
            for row in rows:
                self._invalidate_topic(row, reason)
            return [self._topic_by_id(topic_id) for topic_id in topic_ids]

    def _invalidate_topic(self, row: sqlite3.Row | tuple[object, ...], reason: str) -> None:
        topic = self._topic(row)
        if topic.status == "RUNNING":
            self._append_stale_reason(topic.id, reason, keep_status=True)
        elif self._has_published_topic_output(topic.id):
            self._append_stale_reason(topic.id, reason, keep_status=False)
        else:
            self.conn.execute(
                """
                UPDATE wb_topics
                SET status = ?, stale_reason = '', updated_at = ?
                WHERE id = ?
                """,
                (self._topic_readiness_status(topic), _now(), topic.id),
            )

    def _append_stale_reason(self, topic_id: str, reason: str, *, keep_status: bool) -> None:
        status_assignment = "" if keep_status else "status = 'STALE',"
        self.conn.execute(
            f"""
            UPDATE wb_topics
            SET {status_assignment}
                stale_reason = CASE
                  WHEN stale_reason = '' THEN ?
                  WHEN instr(
                    char(10) || stale_reason || char(10),
                    char(10) || ? || char(10)
                  ) > 0 THEN stale_reason
                  ELSE stale_reason || char(10) || ?
                END,
                updated_at = ?
            WHERE id = ?
            """,
            (reason, reason, reason, _now(), topic_id),
        )

    def _topic_readiness_status(self, topic: CourseTopic) -> str:
        reviews = self.list_topic_chapter_reviews(topic.id)
        if not topic.confirmed or not reviews:
            return "DRAFT"
        for _, status, stale in reviews:
            if status != "DONE" or stale:
                return "NOT_READY"
        return "READY"

    def _topic_by_id(self, topic_id: str) -> CourseTopic:
        row = self.conn.execute("SELECT * FROM wb_topics WHERE id = ?", (topic_id,)).fetchone()
        if row is None:
            raise ValueError("topic not found")
        return self._topic(row)

    def refresh_topic_status(self, topic_id: str) -> CourseTopic:
        with self._atomic(immediate=True):
            topic = self._topic_by_id(topic_id)
            if topic.status not in {"DRAFT", "NOT_READY", "READY"}:
                return topic
            status = self._topic_readiness_status(topic)
            self.conn.execute(
                """
                UPDATE wb_topics
                SET status = ?, stale_reason = '', updated_at = ?
                WHERE id = ? AND status IN ('DRAFT', 'NOT_READY', 'READY')
                """,
                (status, _now(), topic_id),
            )
            return self._topic_by_id(topic_id)

    def mark_topic_stale(self, topic_id: str, reason: str) -> CourseTopic:
        reason = _normalize_stale_reason(reason)
        with self._atomic(immediate=True):
            row = self.conn.execute(
                "SELECT * FROM wb_topics WHERE id = ?",
                (topic_id,),
            ).fetchone()
            if row is None:
                raise ValueError("topic not found")
            self._invalidate_topic(row, reason)
            return self._topic_by_id(topic_id)

    def replace_topic_note_blocks(
        self,
        topic_id: str,
        blocks: dict[str, str],
    ) -> list[TopicNoteBlock]:
        with self._atomic(immediate=True):
            if self.get_topic(topic_id) is None:
                raise ValueError("topic not found")
            now = _now()
            kinds = list(blocks)
            if kinds:
                placeholders = ", ".join("?" for _ in kinds)
                self.conn.execute(
                    "DELETE FROM wb_topic_note_blocks "
                    f"WHERE topic_id = ? AND kind NOT IN ({placeholders})",
                    (topic_id, *kinds),
                )
            else:
                self.conn.execute(
                    "DELETE FROM wb_topic_note_blocks WHERE topic_id = ?",
                    (topic_id,),
                )
            self.conn.executemany(
                """
                INSERT INTO wb_topic_note_blocks (id, topic_id, kind, content, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(topic_id, kind) DO UPDATE SET
                  content = excluded.content,
                  updated_at = excluded.updated_at
                """,
                ((uuid4().hex, topic_id, kind, content, now) for kind, content in blocks.items()),
            )
            self._mark_topic_markdown_pending(topic_id, now=now)
        return self.list_topic_note_blocks(topic_id)

    def list_topic_note_blocks(self, topic_id: str) -> list[TopicNoteBlock]:
        rows = self.conn.execute(
            "SELECT * FROM wb_topic_note_blocks WHERE topic_id = ? ORDER BY kind, id",
            (topic_id,),
        ).fetchall()
        return [TopicNoteBlock(*row) for row in rows]

    def prepare_topic_note_block_update(
        self,
        topic_id: str,
        kind: str,
        content: str,
        expected_content: str,
        *,
        now: int | None = None,
    ) -> TopicNoteBlock:
        now = _now() if now is None else now
        with self._atomic(immediate=True):
            row = self.conn.execute(
                "SELECT content FROM wb_topic_note_blocks WHERE topic_id = ? AND kind = ?",
                (topic_id, kind),
            ).fetchone()
            if row is None:
                raise ValueError("topic note block not found")
            if row[0] != expected_content:
                raise ValueError("topic note block changed")
            sync = self.conn.execute(
                "SELECT status, lease_expires_at FROM wb_topic_markdown_sync WHERE topic_id = ?",
                (topic_id,),
            ).fetchone()
            if sync is not None and sync[0] == "SYNCING":
                if sync[1] > now:
                    raise ValueError("topic Markdown is already syncing")
                stored_error = _topic_publication_error()
                self.conn.execute(
                    "UPDATE wb_topic_markdown_sync SET status = 'FAILED', "
                    "error = ?, error_code = ?, error_message = ?, updated_at = ?, "
                    "owner_id = '', lease_expires_at = 0 "
                    "WHERE topic_id = ? AND status = 'SYNCING' AND lease_expires_at <= ?",
                    (
                        stored_error.message,
                        stored_error.code,
                        stored_error.message,
                        now,
                        topic_id,
                        now,
                    ),
                )
            self.conn.execute(
                "UPDATE wb_topic_note_blocks SET content = ?, updated_at = ? "
                "WHERE topic_id = ? AND kind = ?",
                (content, now, topic_id, kind),
            )
            self.conn.execute(
                """
                INSERT INTO wb_topic_markdown_sync (topic_id, status, error, updated_at)
                VALUES (?, 'PENDING', '', ?)
                ON CONFLICT(topic_id) DO UPDATE SET
                  status = 'PENDING', error = '', updated_at = excluded.updated_at,
                  owner_id = '', lease_expires_at = 0,
                  error_code = '', error_message = ''
                """,
                (topic_id, now),
            )
        return next(block for block in self.list_topic_note_blocks(topic_id) if block.kind == kind)

    def replace_topic_cards(
        self, topic_id: str, cards: Sequence[Mapping[str, object]]
    ) -> list[TopicCard]:
        with self._atomic(immediate=True):
            if self.get_topic(topic_id) is None:
                raise ValueError("topic not found")
            rows = []
            now = _now()
            for card in cards:
                rows.append(
                    (
                        uuid4().hex,
                        topic_id,
                        card["card_type"],
                        card["title"],
                        card["content"],
                        _normalize_source_refs_json(card["source_refs_json"]),
                        now,
                    )
                )
            self.conn.execute("DELETE FROM wb_topic_cards WHERE topic_id = ?", (topic_id,))
            self.conn.executemany(
                """
                INSERT INTO wb_topic_cards
                  (id, topic_id, card_type, title, content, source_refs_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._mark_topic_markdown_pending(topic_id, now=now)
        return self.list_topic_cards(topic_id)

    def list_topic_cards(self, topic_id: str) -> list[TopicCard]:
        rows = self.conn.execute(
            "SELECT id, topic_id, card_type, title, content, source_refs_json, created_at "
            "FROM wb_topic_cards WHERE topic_id = ? ORDER BY created_at, rowid",
            (topic_id,),
        ).fetchall()
        return [TopicCard(*row) for row in rows]

    def create_topic_run(
        self,
        topic_id: str,
        round_key: str,
        input_fingerprint: str,
    ) -> TopicRunRecord:
        row: _TopicRunRow = {
            "id": uuid4().hex,
            "topic_id": topic_id,
            "round_key": round_key,
            "status": "RUNNING",
            "input_fingerprint": input_fingerprint,
            "output": "",
            "error": "",
            "started_at": _now(),
            "finished_at": None,
        }
        with self._atomic():
            self.conn.execute(
                """
                INSERT INTO wb_topic_runs
                  (
                    id, topic_id, round_key, status, input_fingerprint, output,
                    error, started_at, finished_at
                  )
                VALUES
                  (
                    :id, :topic_id, :round_key, :status, :input_fingerprint, :output,
                    :error, :started_at, :finished_at
                  )
                """,
                row,
            )
        return TopicRunRecord(**row)

    def finish_topic_run(
        self,
        run_id: str,
        status: str,
        *,
        output: str = "",
        error: str = "",
    ) -> TopicRunRecord:
        if status not in {"COMPLETED", "FAILED"}:
            raise ValueError("finished topic run status must be COMPLETED or FAILED")
        if status == "COMPLETED":
            error = ""
        else:
            output = ""
        stored_error = (
            _NO_PERSISTED_ERROR if status == "COMPLETED" else _topic_generation_error(error)
        )
        with self._atomic():
            cursor = self.conn.execute(
                """
                UPDATE wb_topic_runs
                SET status = ?, output = ?, error = ?, error_code = ?,
                    error_message = ?, finished_at = ?
                WHERE id = ? AND status = 'RUNNING'
                """,
                (
                    status,
                    output,
                    stored_error.message,
                    stored_error.code,
                    stored_error.message,
                    _now(),
                    run_id,
                ),
            )
            if cursor.rowcount == 0:
                existing = self.conn.execute(
                    "SELECT status FROM wb_topic_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                if existing is None:
                    raise ValueError("topic run not found")
                raise ValueError("topic run is not RUNNING")
            row = self.conn.execute(
                "SELECT * FROM wb_topic_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return TopicRunRecord(*row)

    def list_topic_runs(self, topic_id: str) -> list[TopicRunRecord]:
        rows = self.conn.execute(
            "SELECT * FROM wb_topic_runs WHERE topic_id = ? ORDER BY started_at, rowid",
            (topic_id,),
        ).fetchall()
        return [TopicRunRecord(*row) for row in rows]

    def _chapters_by_ids(self, chapter_ids: list[str]) -> list[Chapter]:
        if not chapter_ids:
            return []
        placeholders = ", ".join("?" for _ in chapter_ids)
        rows = self.conn.execute(
            f"SELECT * FROM wb_chapters WHERE id IN ({placeholders})",
            chapter_ids,
        ).fetchall()
        return [Chapter(*row) for row in rows]

    def _topic(self, row: sqlite3.Row | tuple[object, ...]) -> CourseTopic:
        values = list(row)
        values[6] = bool(values[6])
        return CourseTopic(*values)

    def create_source(self, course_id: str, kind: str, file_path: str, title: str) -> Source:
        row: _SourceRow = {
            "id": uuid4().hex,
            "course_id": course_id,
            "kind": kind,
            "file_path": file_path,
            "title": title,
            "markdown_path": None,
            "status": "IMPORTED",
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.conn.execute(
            """
            INSERT INTO wb_sources
              (id, course_id, kind, file_path, title, markdown_path, status, created_at, updated_at)
            VALUES
              (
                :id, :course_id, :kind, :file_path, :title, :markdown_path,
                :status, :created_at, :updated_at
              )
            """,
            row,
        )
        self.conn.commit()
        return Source(**row)

    def create_sources(
        self,
        course_id: str,
        source_specs: list[tuple[str, str, str]],
    ) -> list[Source]:
        rows = self._source_rows(course_id, source_specs)
        with self._atomic():
            self._insert_source_rows(rows)
        return [Source(**row) for row in rows]

    def create_sources_guarded(
        self,
        course_id: str,
        source_specs: list[tuple[str, str, str]],
        guard: Callable[[], None],
    ) -> list[Source]:
        rows = self._source_rows(course_id, source_specs)
        with self._atomic(immediate=True):
            course = self.conn.execute(
                "SELECT 1 FROM wb_courses WHERE id = ?",
                (course_id,),
            ).fetchone()
            if course is None:
                raise ValueError("course not found")
            # These guards detect identity changes at the transaction boundaries;
            # the batch's stable directory fd confines later cleanup after a rename.
            guard()
            self._insert_source_rows(rows)
            guard()
        return [Source(**row) for row in rows]

    def _source_rows(
        self,
        course_id: str,
        source_specs: list[tuple[str, str, str]],
    ) -> list[_SourceRow]:
        now = _now()
        return [
            {
                "id": uuid4().hex,
                "course_id": course_id,
                "kind": kind,
                "file_path": file_path,
                "title": title,
                "markdown_path": None,
                "status": "IMPORTED",
                "created_at": now,
                "updated_at": now,
            }
            for kind, file_path, title in source_specs
        ]

    def _insert_source_rows(self, rows: Sequence[_SourceRow]) -> None:
        for row in rows:
            self.conn.execute(
                """
                INSERT INTO wb_sources
                  (
                    id, course_id, kind, file_path, title, markdown_path,
                    status, created_at, updated_at
                  )
                VALUES
                  (
                    :id, :course_id, :kind, :file_path, :title, :markdown_path,
                    :status, :created_at, :updated_at
                  )
                """,
                row,
            )

    def list_sources(self, course_id: str) -> list[Source]:
        rows = self.conn.execute(
            "SELECT * FROM wb_sources WHERE course_id = ? ORDER BY created_at, rowid",
            (course_id,),
        ).fetchall()
        return [Source(*row) for row in rows]

    def source_file_paths(self, course_id: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT file_path FROM wb_sources WHERE course_id = ?",
            (course_id,),
        ).fetchall()
        return {row[0] for row in rows}

    def source_file_paths_for_root(self, root_dir: str) -> set[str]:
        rows = self.conn.execute(
            """
            SELECT sources.file_path
            FROM wb_sources AS sources
            JOIN wb_courses AS courses ON courses.id = sources.course_id
            WHERE courses.root_dir = ?
            """,
            (root_dir,),
        ).fetchall()
        return {row[0] for row in rows}

    def get_source(self, source_id: str) -> Source | None:
        row = self.conn.execute(
            "SELECT * FROM wb_sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        return Source(*row) if row else None

    def create_chapter(
        self,
        course_id: str,
        source_id: str,
        seq: int,
        title: str,
        source_md_path: str,
        source_start: int = 0,
        source_end: int = 0,
    ) -> Chapter:
        source = self.conn.execute(
            "SELECT course_id FROM wb_sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        if source is None or source[0] != course_id:
            raise ValueError("source does not belong to course")

        row: _ChapterRow = {
            "id": uuid4().hex,
            "source_id": source_id,
            "course_id": course_id,
            "seq": seq,
            "title": title,
            "source_md_path": source_md_path,
            "status": "DRAFT",
            "source_start": source_start,
            "source_end": source_end,
            "confirmed_snapshot_json": "",
            "confirmed_at": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.conn.execute(
            """
            INSERT INTO wb_chapters
              (id, source_id, course_id, seq, title, source_md_path, status,
               source_start, source_end, confirmed_snapshot_json, confirmed_at,
               created_at, updated_at)
            VALUES
              (
                :id, :source_id, :course_id, :seq, :title, :source_md_path,
                :status, :source_start, :source_end, :confirmed_snapshot_json,
                :confirmed_at, :created_at, :updated_at
              )
            """,
            row,
        )
        self.conn.commit()
        return Chapter(**row)

    def list_chapters(self, source_id: str) -> list[Chapter]:
        rows = self.conn.execute(
            "SELECT * FROM wb_chapters WHERE source_id = ? ORDER BY seq",
            (source_id,),
        ).fetchall()
        return [Chapter(*row) for row in rows]

    def list_course_chapters(self, course_id: str) -> list[CourseChapter]:
        rows = self.conn.execute(
            """
            SELECT c.*, s.title
            FROM wb_chapters c
            JOIN wb_sources s ON s.id = c.source_id
            WHERE c.course_id = ? AND c.status IN ('CONFIRMED', 'COMPLETED')
            ORDER BY s.title, s.id, c.seq, c.id
            """,
            (course_id,),
        ).fetchall()
        return [CourseChapter(Chapter(*row[:-1]), row[-1]) for row in rows]

    def delete_chapters_by_source(self, source_id: str) -> None:
        self.conn.execute("DELETE FROM wb_chapters WHERE source_id = ?", (source_id,))
        self.conn.commit()

    def get_chapter(self, chapter_id: str) -> Chapter | None:
        row = self.conn.execute(
            "SELECT * FROM wb_chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()
        return Chapter(*row) if row else None

    def confirm_chapter(self, chapter_id: str) -> Chapter:
        with self._atomic(immediate=True):
            chapter = self.get_chapter(chapter_id)
            if chapter is None:
                raise LookupError("chapter not found")
            if chapter.status == "CONFIRMED":
                return chapter
            if chapter.status not in {"DRAFT", "COMPLETED", "FAILED"}:
                raise ValueError("chapter cannot be confirmed in current state")
            cursor = self.conn.execute(
                "UPDATE wb_chapters SET status = 'CONFIRMED', updated_at = ? "
                "WHERE id = ? AND status = ?",
                (_now(), chapter_id, chapter.status),
            )
            if cursor.rowcount != 1:
                raise ValueError("chapter cannot be confirmed in current state")
            confirmed = self.get_chapter(chapter_id)
            if confirmed is None:
                raise RuntimeError("chapter disappeared during confirmation")
            return confirmed

    def transition_chapter_status(
        self,
        chapter_id: str,
        expected_statuses: Sequence[str],
        target_status: str,
    ) -> tuple[Chapter, bool]:
        expected = tuple(dict.fromkeys(expected_statuses))
        if not expected:
            raise ValueError("expected_statuses cannot be empty")
        with self._atomic(immediate=True):
            chapter = self.get_chapter(chapter_id)
            if chapter is None:
                raise LookupError("chapter not found")
            if chapter.status not in expected:
                return chapter, False
            placeholders = ", ".join("?" for _ in expected)
            cursor = self.conn.execute(
                f"UPDATE wb_chapters SET status = ?, updated_at = ? "
                f"WHERE id = ? AND status IN ({placeholders})",
                (target_status, _now(), chapter_id, *expected),
            )
            if cursor.rowcount != 1:
                current = self.get_chapter(chapter_id)
                if current is None:
                    raise RuntimeError("chapter disappeared during status transition")
                return current, False
            updated = self.get_chapter(chapter_id)
            if updated is None:
                raise RuntimeError("chapter disappeared during status transition")
            return updated, True

    def update_chapter_status(self, chapter_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE wb_chapters SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), chapter_id),
        )
        self.conn.commit()

    def chapter_draft_snapshot(self, source_id: str) -> tuple[_ChapterDraftSnapshot, str]:
        chapters = self.list_chapters(source_id)
        value: _ChapterDraftSnapshot = {
            "source_id": source_id,
            "chapters": [
                {
                    "id": item.id,
                    "seq": item.seq,
                    "title": item.title,
                    "start": item.source_start,
                    "end": item.source_end,
                    "status": item.status,
                }
                for item in chapters
            ],
        }
        return value, _stable_fingerprint(value)

    def replace_chapter_drafts(
        self,
        source_id: str,
        drafts: Sequence[Mapping[str, object]],
        *,
        expected_fingerprint: str,
        publish: Callable[[], None] | None = None,
        compensate: Callable[[], None] | None = None,
    ) -> list[Chapter]:
        published = False
        try:
            with self._atomic(immediate=True):
                self.validate_chapter_draft_replace(source_id, expected_fingerprint)
                current = self.list_chapters(source_id)
                existing = {item.id: item for item in current}
                ids: list[str] = []
                for draft in drafts:
                    chapter_id = draft.get("id")
                    if not chapter_id:
                        ids.append(uuid4().hex)
                    elif isinstance(chapter_id, str):
                        ids.append(chapter_id)
                    else:
                        raise ValueError("chapter id must be a string")
                if len(ids) != len(set(ids)):
                    raise ValueError("duplicate chapter id")
                self.conn.execute("DELETE FROM wb_chapters WHERE source_id = ?", (source_id,))
                now = _now()
                source = self.get_source(source_id)
                if source is None:
                    raise ValueError("source not found")
                if len(ids) != len(drafts):
                    raise RuntimeError("chapter draft count mismatch")
                for seq, chapter_id in enumerate(ids):
                    draft = drafts[seq]
                    old = existing.get(chapter_id)
                    start = _integer_value(draft["start"])
                    end = _integer_value(draft["end"])
                    if start < 0 or end <= start:
                        raise ValueError("invalid chapter boundary")
                    self.conn.execute(
                        """
                    INSERT INTO wb_chapters
                      (id, source_id, course_id, seq, title, source_md_path, status,
                       source_start, source_end, confirmed_snapshot_json, confirmed_at,
                       created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?, '', NULL, ?, ?)
                    """,
                        (
                            chapter_id,
                            source_id,
                            source.course_id,
                            seq,
                            str(draft["title"]).strip(),
                            str(draft.get("source_md_path") or (old.source_md_path if old else "")),
                            start,
                            end,
                            old.created_at if old else now,
                            now,
                        ),
                    )
                if publish is not None:
                    publish()
                    published = True
        except Exception:
            if published and compensate is not None:
                compensate()
            raise
        return self.list_chapters(source_id)

    def validate_chapter_draft_replace(self, source_id: str, expected_fingerprint: str) -> None:
        current, fingerprint = self.chapter_draft_snapshot(source_id)
        if fingerprint != expected_fingerprint:
            raise ValueError("chapter drafts changed")
        if any(item["status"] != "DRAFT" for item in current["chapters"]):
            raise ValueError("chapter drafts already confirmed")

    def confirm_chapter_drafts(self, source_id: str, expected_fingerprint: str) -> list[Chapter]:
        with self._atomic(immediate=True):
            snapshot, fingerprint = self.chapter_draft_snapshot(source_id)
            if fingerprint != expected_fingerprint:
                raise ValueError("chapter drafts changed")
            if not snapshot["chapters"] or any(
                x["status"] != "DRAFT" for x in snapshot["chapters"]
            ):
                raise ValueError("chapter drafts already confirmed")
            payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            now = _now()
            self.conn.execute(
                "UPDATE wb_chapters SET status = 'CONFIRMED', confirmed_snapshot_json = ?, "
                "confirmed_at = ?, updated_at = ? WHERE source_id = ? AND status = 'DRAFT'",
                (payload, now, now, source_id),
            )
        return self.list_chapters(source_id)

    def create_attachment(
        self,
        course_id: str,
        source_id: str,
        chapter_id: str | None,
        file_path: str,
        title: str,
        kind: str,
        parsed_text: str,
        content_hash: str,
        anchors: Sequence[Mapping[str, object]],
    ) -> Attachment:
        source = self.get_source(source_id)
        if source is None or source.course_id != course_id:
            raise ValueError("source does not belong to course")
        if chapter_id is not None:
            chapter = self.get_chapter(chapter_id)
            if chapter is None or chapter.course_id != course_id or chapter.source_id != source_id:
                raise ValueError("chapter does not belong to source")
        row = (
            uuid4().hex,
            course_id,
            chapter_id,
            file_path,
            title,
            kind,
            _now(),
            source_id,
            parsed_text,
            content_hash,
            json.dumps(anchors, ensure_ascii=False),
        )
        self.conn.execute(
            """
            INSERT INTO wb_attachments
              (id, course_id, chapter_id, file_path, title, kind, created_at,
               source_id, parsed_text, content_hash, anchors_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
        self.conn.commit()
        return Attachment(*row)

    def create_attachments_guarded(
        self,
        specs: Sequence[
            tuple[
                str,
                str,
                str | None,
                str,
                str,
                str,
                str,
                str,
                Sequence[Mapping[str, object]],
            ]
        ],
        publish: Callable[[], None],
        compensate: Callable[[], None],
    ) -> list[Attachment]:
        published = False
        rows = []
        try:
            with self._atomic(immediate=True):
                for spec in specs:
                    (
                        course_id,
                        source_id,
                        chapter_id,
                        file_path,
                        title,
                        kind,
                        text,
                        digest,
                        anchors,
                    ) = spec
                    source = self.get_source(source_id)
                    chapter = self.get_chapter(chapter_id) if chapter_id else None
                    if source is None or source.course_id != course_id:
                        raise ValueError("source does not belong to course")
                    if chapter_id and (
                        chapter is None
                        or chapter.course_id != course_id
                        or chapter.source_id != source_id
                    ):
                        raise ValueError("chapter does not belong to source")
                    row = (
                        uuid4().hex,
                        course_id,
                        chapter_id,
                        file_path,
                        title,
                        kind,
                        _now(),
                        source_id,
                        text,
                        digest,
                        json.dumps(anchors, ensure_ascii=False),
                    )
                    self.conn.execute(
                        """
                        INSERT INTO wb_attachments
                          (id, course_id, chapter_id, file_path, title, kind, created_at,
                           source_id, parsed_text, content_hash, anchors_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        row,
                    )
                    rows.append(row)
                publish()
                published = True
        except Exception:
            if published:
                compensate()
            raise
        return [Attachment(*row) for row in rows]

    def list_attachments(self, chapter_id: str) -> list[Attachment]:
        rows = self.conn.execute(
            """
            SELECT id, course_id, chapter_id, file_path, title, kind, created_at,
                   source_id, parsed_text, content_hash, anchors_json
            FROM wb_attachments WHERE chapter_id = ? ORDER BY created_at, id
            """,
            (chapter_id,),
        ).fetchall()
        return [Attachment(*row) for row in rows]

    def chapter_input_snapshot(
        self, chapter_id: str, *, source_content_hash: str | None = None
    ) -> tuple[_ChapterInputSnapshot, str]:
        chapter = self.get_chapter(chapter_id)
        if chapter is None:
            raise ValueError("chapter not found")
        attachments = self.list_attachments(chapter_id)
        source_hash = source_content_hash
        if source_hash is None:
            try:
                _source_content, source_hash = read_stable_source_markdown(chapter.source_md_path)
            except FileNotFoundError:
                source_hash = ""
        value: _ChapterInputSnapshot = {
            "chapter": {
                "id": chapter.id,
                "title": chapter.title,
                "start": chapter.source_start,
                "end": chapter.source_end,
                "snapshot": chapter.confirmed_snapshot_json,
                "content_hash": source_hash,
            },
            "attachments": [
                {
                    "id": item.id,
                    "hash": item.content_hash,
                    "anchors": _citation_anchors_from_json(item.anchors_json),
                }
                for item in attachments
            ],
        }
        return value, _stable_fingerprint(value)

    def upsert_note_block(
        self,
        chapter_id: str,
        kind: str,
        title: str,
        body: str,
        seq: int,
    ) -> NoteBlock:
        block = self._upsert_note_block_no_commit(chapter_id, kind, title, body, seq)
        self.conn.commit()
        return block

    def _upsert_note_block_no_commit(
        self,
        chapter_id: str,
        kind: str,
        title: str,
        body: str,
        seq: int,
    ) -> NoteBlock:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO wb_note_blocks
              (id, chapter_id, kind, title, body, seq, updated_at)
            VALUES
              (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chapter_id, kind) DO UPDATE SET
              title = excluded.title,
              body = excluded.body,
              seq = excluded.seq,
              updated_at = excluded.updated_at
            """,
            (uuid4().hex, chapter_id, kind, title, body, seq, now),
        )
        row = self.conn.execute(
            "SELECT * FROM wb_note_blocks WHERE chapter_id = ? AND kind = ?",
            (chapter_id, kind),
        ).fetchone()
        return NoteBlock(*row)

    def patch_chapter_note_block(
        self, chapter_id: str, kind: str, body: str, expected_body: str
    ) -> NoteBlock:
        with self._atomic(immediate=True):
            cursor = self.conn.execute(
                "UPDATE wb_note_blocks SET body = ?, updated_at = ? "
                "WHERE chapter_id = ? AND kind = ? AND body = ?",
                (body, _now(), chapter_id, kind, expected_body),
            )
            if cursor.rowcount != 1:
                exists = self.conn.execute(
                    "SELECT 1 FROM wb_note_blocks WHERE chapter_id = ? AND kind = ?",
                    (chapter_id, kind),
                ).fetchone()
                if exists is None:
                    raise ValueError("chapter note block not found")
                raise ValueError("chapter note block changed")
            row = self.conn.execute(
                "SELECT * FROM wb_note_blocks WHERE chapter_id = ? AND kind = ?",
                (chapter_id, kind),
            ).fetchone()
            return NoteBlock(*row)

    def list_note_blocks(self, chapter_id: str) -> list[NoteBlock]:
        rows = self.conn.execute(
            "SELECT * FROM wb_note_blocks WHERE chapter_id = ? ORDER BY seq, id",
            (chapter_id,),
        ).fetchall()
        return [NoteBlock(*row) for row in rows]

    def upsert_run(
        self,
        chapter_id: str,
        round_key: str,
        executor: str,
        status: str,
        input_path: str,
        output_path: str,
        output: str,
        stale: bool = False,
        input_fingerprint: str = "",
        citation_ids: tuple[str, ...] = (),
    ) -> RunRecord:
        run = self._upsert_run_no_commit(
            chapter_id,
            round_key,
            executor,
            status,
            input_path,
            output_path,
            output,
            stale,
            input_fingerprint,
            citation_ids,
        )
        self.conn.commit()
        return run

    def _upsert_run_no_commit(
        self,
        chapter_id: str,
        round_key: str,
        executor: str,
        status: str,
        input_path: str,
        output_path: str,
        output: str,
        stale: bool = False,
        input_fingerprint: str = "",
        citation_ids: tuple[str, ...] = (),
    ) -> RunRecord:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO wb_runs
              (
                id, chapter_id, round_key, executor, status, input_path,
                output_path, output, stale, created_at, updated_at,
                input_fingerprint, citation_ids_json
              )
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chapter_id, round_key) DO UPDATE SET
              executor = excluded.executor,
              status = excluded.status,
              input_path = excluded.input_path,
              output_path = excluded.output_path,
              output = excluded.output,
              stale = excluded.stale,
              input_fingerprint = excluded.input_fingerprint,
              citation_ids_json = excluded.citation_ids_json,
              updated_at = excluded.updated_at
            """,
            (
                uuid4().hex,
                chapter_id,
                round_key,
                executor,
                status,
                input_path,
                output_path,
                output,
                int(stale),
                now,
                now,
                input_fingerprint,
                json.dumps(citation_ids, ensure_ascii=False),
            ),
        )
        row = self.conn.execute(
            "SELECT * FROM wb_runs WHERE chapter_id = ? AND round_key = ?",
            (chapter_id, round_key),
        ).fetchone()
        return self._run(row)

    def list_runs(self, chapter_id: str) -> list[RunRecord]:
        rows = self.conn.execute(
            "SELECT * FROM wb_runs WHERE chapter_id = ? ORDER BY created_at",
            (chapter_id,),
        ).fetchall()
        return [self._run(row) for row in rows]

    def mark_runs_stale(self, chapter_id: str, round_keys: list[str]) -> None:
        if not round_keys:
            return
        placeholders = ", ".join("?" for _ in round_keys)
        self.conn.execute(
            f"""
            UPDATE wb_runs
            SET stale = 1, updated_at = ?
            WHERE chapter_id = ? AND round_key IN ({placeholders})
            """,
            (_now(), chapter_id, *round_keys),
        )
        self.conn.commit()

    def create_card(
        self,
        course_id: str,
        chapter_id: str,
        kind: str,
        title: str,
        body: str,
    ) -> Card:
        card = self._create_card_no_commit(course_id, chapter_id, kind, title, body)
        self.conn.commit()
        return card

    def _create_card_no_commit(
        self,
        course_id: str,
        chapter_id: str,
        kind: str,
        title: str,
        body: str,
    ) -> Card:
        chapter = self.conn.execute(
            "SELECT course_id FROM wb_chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()
        if chapter is None or chapter[0] != course_id:
            raise ValueError("chapter does not belong to course")

        row: _CardRow = {
            "id": uuid4().hex,
            "course_id": course_id,
            "chapter_id": chapter_id,
            "kind": kind,
            "title": title,
            "body": body,
            "favorite": False,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.conn.execute(
            """
            INSERT INTO wb_cards
              (id, course_id, chapter_id, kind, title, body, favorite, created_at, updated_at)
            VALUES
              (
                :id, :course_id, :chapter_id, :kind, :title, :body, :favorite,
                :created_at, :updated_at
              )
            """,
            {**row, "favorite": int(row["favorite"])},
        )
        return Card(**row)

    def list_cards(self, course_id: str) -> list[Card]:
        rows = self.conn.execute(
            """
            SELECT id, course_id, chapter_id, kind, title, body, favorite, created_at, updated_at
            FROM wb_cards
            WHERE course_id = ?
            ORDER BY favorite DESC, updated_at DESC
            """,
            (course_id,),
        ).fetchall()
        return [self._card(row) for row in rows]

    def list_course_cards(self, course_id: str) -> list[_CourseCardListItem]:
        rows = self.conn.execute(
            """
            SELECT * FROM (
            SELECT wc.id, 'chapter' AS origin_type, wc.chapter_id AS origin_id,
                   ch.title AS origin_title, wc.kind AS card_type, wc.title,
                   wc.body AS content, json_array(wc.chapter_id) AS source_refs_json,
                   ch.seq AS origin_seq, wc.tags_json, wc.status, wc.favorite, wc.updated_at
            FROM wb_cards wc
            JOIN wb_chapters ch ON ch.id = wc.chapter_id
            WHERE wc.course_id = ?
            UNION ALL
            SELECT tc.id, 'topic', tc.topic_id, t.title, tc.card_type, tc.title,
                   tc.content, tc.source_refs_json, t.seq, tc.tags_json, tc.status,
                   tc.favorite, tc.updated_at
            FROM wb_topic_cards tc
            JOIN wb_topics t ON t.id = tc.topic_id
            WHERE t.course_id = ?
            ) AS course_cards
            ORDER BY CASE origin_type WHEN 'chapter' THEN 0 ELSE 1 END,
                     origin_seq, origin_id, id
            """,
            (course_id, course_id),
        ).fetchall()
        return [
            {
                "id": row[0],
                "origin_type": row[1],
                "origin_id": row[2],
                "origin_title": row[3],
                "card_type": row[4],
                "title": row[5],
                "content": row[6],
                "source_refs_json": row[7],
                "origin_seq": row[8],
                "tags_json": row[9],
                "status": row[10],
                "favorite": bool(row[11]),
                "updated_at": row[12],
            }
            for row in rows
        ]

    def _course_card(self, card_id: str) -> _CourseCardDetail | None:
        row = self.conn.execute(
            """
            SELECT wc.id, 'chapter', wc.tags_json, wc.status, wc.favorite, wc.updated_at,
                   wc.title, wc.body, wc.course_id, wc.chapter_id, ch.title, wc.kind,
                   json_array(wc.chapter_id)
            FROM wb_cards wc JOIN wb_chapters ch ON ch.id = wc.chapter_id WHERE wc.id = ?
            UNION ALL
            SELECT tc.id, 'topic', tc.tags_json, tc.status, tc.favorite, tc.updated_at,
                   tc.title, tc.content, t.course_id, tc.topic_id, t.title, tc.card_type,
                   tc.source_refs_json
            FROM wb_topic_cards tc JOIN wb_topics t ON t.id = tc.topic_id WHERE tc.id = ?
            """,
            (card_id, card_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "origin_type": row[1],
            "tags": _json_string_list(row[2]),
            "status": row[3],
            "favorite": bool(row[4]),
            "updated_at": row[5],
            "title": row[6],
            "content": row[7],
            "course_id": row[8],
            "origin_id": row[9],
            "origin_title": row[10],
            "card_type": row[11],
            "source_refs": _json_string_list(row[12]),
        }

    def update_course_card(
        self,
        card_id: str,
        *,
        title: str,
        content: str,
        tags: list[str],
        status: str,
        expected_updated_at: int,
    ) -> _CourseCardDetail:
        current = self._course_card(card_id)
        if current is None:
            raise LookupError("card not found")
        if current["updated_at"] != expected_updated_at:
            raise ValueError("card changed")
        updated_at = max(_now(), expected_updated_at + 1)
        table = "wb_cards" if current["origin_type"] == "chapter" else "wb_topic_cards"
        content_column = "body" if table == "wb_cards" else "content"
        with self._atomic(immediate=True):
            cursor = self.conn.execute(
                f"UPDATE {table} SET title = ?, {content_column} = ?, tags_json = ?, "
                "status = ?, updated_at = ? WHERE id = ? AND updated_at = ?",
                (
                    title,
                    content,
                    json.dumps(tags, ensure_ascii=False),
                    status,
                    updated_at,
                    card_id,
                    expected_updated_at,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("card changed")
        updated = self._course_card(card_id)
        if updated is None:
            raise RuntimeError("card disappeared during update")
        return updated

    def set_course_card_favorite(
        self,
        card_id: str,
        favorite: bool,
        expected_updated_at: int,
    ) -> _CourseCardDetail:
        current = self._course_card(card_id)
        if current is None:
            raise LookupError("card not found")
        if current["updated_at"] != expected_updated_at:
            raise ValueError("card changed")
        updated_at = max(_now(), expected_updated_at + 1)
        table = "wb_cards" if current["origin_type"] == "chapter" else "wb_topic_cards"
        with self._atomic(immediate=True):
            cursor = self.conn.execute(
                f"UPDATE {table} SET favorite = ?, updated_at = ? WHERE id = ? AND updated_at = ?",
                (int(favorite), updated_at, card_id, expected_updated_at),
            )
            if cursor.rowcount != 1:
                raise ValueError("card changed")
        updated = self._course_card(card_id)
        if updated is None:
            raise RuntimeError("card disappeared during favorite update")
        return updated

    def list_cards_by_chapter(self, chapter_id: str) -> list[Card]:
        rows = self.conn.execute(
            """
            SELECT id, course_id, chapter_id, kind, title, body, favorite, created_at, updated_at
            FROM wb_cards
            WHERE chapter_id = ?
            ORDER BY updated_at DESC
            """,
            (chapter_id,),
        ).fetchall()
        return [self._card(row) for row in rows]

    def delete_cards_by_chapter_and_kind(self, chapter_id: str, kind: str) -> None:
        self.conn.execute(
            "DELETE FROM wb_cards WHERE chapter_id = ? AND kind = ?",
            (chapter_id, kind),
        )
        self.conn.commit()

    def update_card(self, card_id: str, title: str, body: str) -> None:
        self.conn.execute(
            "UPDATE wb_cards SET title = ?, body = ?, updated_at = ? WHERE id = ?",
            (title, body, _now(), card_id),
        )
        self.conn.commit()

    def set_card_favorite(self, card_id: str, favorite: bool) -> None:
        self.conn.execute(
            "UPDATE wb_cards SET favorite = ?, updated_at = ? WHERE id = ?",
            (int(favorite), _now(), card_id),
        )
        self.conn.commit()

    def _card(self, row: sqlite3.Row | tuple[object, ...]) -> Card:
        values = list(row)
        values[6] = bool(values[6])
        return Card(*values)

    def _run(self, row: sqlite3.Row | tuple[object, ...]) -> RunRecord:
        values = list(row)
        values[8] = bool(values[8])
        return RunRecord(*values)
