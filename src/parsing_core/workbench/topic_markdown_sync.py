from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from parsing_core.workbench import markdown_sync as markdown_publication
from parsing_core.workbench.markdown_sync import (
    LOCK_NAME,
    OWNER_NAME,
    _first_line_regular_file,
    _pure_mermaid,
    commit_markdown_publication,
    ensure_directory_owner,
    migrate_generated_directory,
    open_secure_directory,
    publication_bundle_fingerprint,
    recover_atomic_bundle,
    redact_sensitive_text,
    safe_name,
    textbook_dir,
)
from parsing_core.workbench.models import Chapter, Course, CourseTopic
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.topic_task_package import allocate_source_display_titles

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

SECTION_TITLES = {
    "overview": "1. 主题概要",
    "linked_sources": "2. 关联教材与章节",
    "core_concepts": "3. 核心概念",
    "viewpoint_comparison": "4. 教材观点对照",
    "consensus_disagreements": "5. 共识与分歧",
    "complementary_views": "6. 互补视角",
    "plain_explanation": "7. 通俗、有趣、生活化的解释",
    "textbook_cases": "8. 教材案例解读",
    "real_world_problem_solving": "9. 现实案例与问题解决",
    "integrated_framework": "10. 综合分析框架",
    "application_methods": "11. 实际应用方法",
    "further_thinking": "12. 延伸思考",
    "knowledge_mermaid": "13. Mermaid 知识结构图",
    "application_mermaid": "14. Mermaid 应用流程图",
}


class TopicMarkdownSyncError(RuntimeError):
    code = "TOPIC_MARKDOWN_PUBLICATION_FAILED"
    message = "topic Markdown publication failed"

    def __init__(self) -> None:
        super().__init__(self.message)


class TopicMarkdownDeleteError(Exception):
    pass


MERGE_JOURNAL_PREFIX = ".pdf2md-merge-"
MAX_PROTECTED_TOPIC_FILE_BYTES = 4 * 1024 * 1024


@dataclass
class _MergeRecord:
    topic: CourseTopic
    visible: str
    hidden: str
    topic_fd: int
    lock_fd: int
    identity: tuple[int, int]
    lock_identity: tuple[int, int]
    detached: bool
    placeholder_created: bool
    placeholder_resolved: bool


@dataclass(frozen=True)
class _TopicDirectoryContext:
    topic: CourseTopic
    course: Course
    chapters: list[Chapter]
    topic_dir: Path
    topic_fd: int
    marker: str


def _merge_file_hook(phase: str, parent_fd: int, topic_fd: int, name: str) -> None:
    pass


def _delete_race_hook(phase: str, parent_fd: int, topic_fd: int, name: str) -> None:
    pass


def _identity(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _entry_identity(parent_fd: int, name: str) -> tuple[int, int]:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("topic directory identity is protected")
    return info.st_dev, info.st_ino


def _regular_entry_identity(dir_fd: int, name: str) -> tuple[int, int]:
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("topic directory contains protected file type")
    return info.st_dev, info.st_ino


def _read_regular_file_at(dir_fd: int, name: str) -> bytes:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        raise ValueError("topic directory contains protected file type") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("topic directory contains protected file type")
        if before.st_size > MAX_PROTECTED_TOPIC_FILE_BYTES:
            raise ValueError("topic protected file exceeds size limit")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                raise ValueError("topic protected file changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        if _protected_file_identity(before) != _protected_file_identity(after):
            raise ValueError("topic protected file changed while being read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _protected_file_identity(info: os.stat_result) -> tuple[int, ...]:
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


def _validate_delete_directory(
    parent_fd: int,
    topic_fd: int,
    name: str,
    expected_identity: tuple[int, int],
    expected_lock_identity: tuple[int, int],
    topic_id: str,
) -> dict[str, bytes]:
    if (
        _identity(topic_fd) != expected_identity
        or _entry_identity(parent_fd, name) != expected_identity
    ):
        raise ValueError("topic directory identity is protected")
    if _regular_entry_identity(topic_fd, LOCK_NAME) != expected_lock_identity:
        raise ValueError("topic directory lock is protected")
    expected_names = {LOCK_NAME, OWNER_NAME, "topic-map.md"}
    if set(os.listdir(topic_fd)) != expected_names:
        raise ValueError("topic directory contains protected user files")
    contents = {item: _read_regular_file_at(topic_fd, item) for item in expected_names}
    if contents[OWNER_NAME].splitlines()[:1] != [f"topic:{topic_id}".encode()]:
        raise ValueError("topic directory ownership is protected")
    marker = f"<!-- topic-id: {topic_id} -->".encode()
    if contents["topic-map.md"].splitlines()[:1] != [marker]:
        raise ValueError("topic directory ownership is protected")
    return contents


def _restore_delete_directory(
    parent_fd: int,
    topic_fd: int,
    name: str,
    expected_identity: tuple[int, int],
    contents: dict[str, bytes],
    removed_names: set[str],
) -> None:
    try:
        current_identity = _entry_identity(parent_fd, name)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise ValueError("topic deletion recovery found protected concurrent content") from exc
        restore_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    else:
        if current_identity != expected_identity or _identity(topic_fd) != expected_identity:
            raise ValueError("topic deletion recovery found protected identity change")
        restore_fd = os.dup(topic_fd)
    try:
        for item in removed_names:
            content = contents[item]
            try:
                fd = os.open(
                    item,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=restore_fd,
                )
            except FileExistsError as exc:
                raise ValueError(
                    "topic deletion recovery found protected concurrent content"
                ) from exc
            try:
                os.write(fd, content)
                os.fsync(fd)
            finally:
                os.close(fd)
        os.fsync(restore_fd)
    finally:
        os.close(restore_fd)


def _remove_created_placeholder(
    parent_fd: int,
    topic_fd: int,
    name: str,
    expected_identity: tuple[int, int],
    lock_identity: tuple[int, int],
) -> None:
    try:
        current_identity = _entry_identity(parent_fd, name)
    except FileNotFoundError:
        return
    if current_identity != expected_identity or _identity(topic_fd) != expected_identity:
        raise ValueError("topic placeholder identity changed during deletion")
    if set(os.listdir(topic_fd)) != {LOCK_NAME}:
        raise ValueError("topic placeholder contains protected concurrent content")
    if _regular_entry_identity(topic_fd, LOCK_NAME) != lock_identity:
        raise ValueError("topic placeholder lock changed during deletion")
    os.unlink(LOCK_NAME, dir_fd=topic_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _remove_detached_topic_directory(
    parent_fd: int,
    topic_fd: int,
    name: str,
    expected_identity: tuple[int, int],
    lock_identity: tuple[int, int],
    topic_id: str,
) -> None:
    _validate_delete_directory(
        parent_fd, topic_fd, name, expected_identity, lock_identity, topic_id
    )
    _merge_file_hook("before_hidden_cleanup", parent_fd, topic_fd, name)
    os.unlink("topic-map.md", dir_fd=topic_fd)
    os.unlink(OWNER_NAME, dir_fd=topic_fd)
    os.unlink(LOCK_NAME, dir_fd=topic_fd)
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _cleanup_committed_merge_journals(repo: WorkbenchRepository, parent_fd: int) -> None:
    for name in sorted(
        item for item in os.listdir(parent_fd) if item.startswith(MERGE_JOURNAL_PREFIX)
    ):
        parts = name.removeprefix(MERGE_JOURNAL_PREFIX).split("-", 1)
        if len(parts) != 2 or repo.get_topic(parts[0]) is not None:
            continue
        topic_id = parts[0]
        topic_fd = -1
        lock_fd = -1
        try:
            topic_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            lock_fd = os.open(LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW, dir_fd=topic_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if set(os.listdir(topic_fd)) == {LOCK_NAME}:
                _remove_created_placeholder(
                    parent_fd, topic_fd, name, _identity(topic_fd), _identity(lock_fd)
                )
            else:
                _remove_detached_topic_directory(
                    parent_fd,
                    topic_fd,
                    name,
                    _identity(topic_fd),
                    _identity(lock_fd),
                    topic_id,
                )
        except (OSError, ValueError):
            pass
        finally:
            if lock_fd >= 0:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            if topic_fd >= 0:
                os.close(topic_fd)


def merge_unpublished_topics(
    repo: WorkbenchRepository,
    course_id: str,
    topic_ids: list[str],
    *,
    title: str,
    description: str = "",
    chapter_ids: list[str] | None = None,
) -> CourseTopic:
    topics: list[CourseTopic] = []
    for topic_id in topic_ids:
        topic = repo.get_topic(topic_id)
        if topic is None:
            raise ValueError("topic not found")
        topics.append(topic)
    course = repo.get_course(course_id)
    if course is None:
        raise ValueError("course not found")
    if any(topic.course_id != course_id for topic in topics):
        raise ValueError("all topics must belong to the same course")

    parent_fd = open_secure_directory(Path(course.root_dir), ["课程主题"])
    operation_id = os.urandom(8).hex()
    records: list[_MergeRecord] = []
    try:
        _cleanup_committed_merge_journals(repo, parent_fd)
        for topic in sorted(topics, key=lambda item: (item.seq, item.id)):
            visible = f"{topic.seq + 1:02d}-{safe_name(topic.title)}"
            placeholder_created = False
            _merge_file_hook("before_target_prepare", parent_fd, -1, visible)
            try:
                os.mkdir(visible, 0o700, dir_fd=parent_fd)
                placeholder_created = True
            except FileExistsError:
                pass
            topic_fd = os.open(
                visible,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            if placeholder_created:
                lock_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            else:
                lock_flags = os.O_RDWR | os.O_NOFOLLOW
            try:
                lock_fd = os.open(LOCK_NAME, lock_flags, 0o600, dir_fd=topic_fd)
            except FileNotFoundError:
                entries = set(os.listdir(topic_fd))
                if not entries <= {OWNER_NAME, "topic-map.md"} or OWNER_NAME not in entries:
                    os.close(topic_fd)
                    raise ValueError("topic directory lock is protected") from None
                owner = _read_regular_file_at(topic_fd, OWNER_NAME)
                if owner.splitlines()[:1] != [f"topic:{topic.id}".encode()]:
                    os.close(topic_fd)
                    raise ValueError("topic directory ownership is protected") from None
                try:
                    lock_fd = os.open(
                        LOCK_NAME,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=topic_fd,
                    )
                except FileExistsError:
                    lock_fd = os.open(LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW, dir_fd=topic_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            record = _MergeRecord(
                topic=topic,
                visible=visible,
                hidden=f"{MERGE_JOURNAL_PREFIX}{topic.id}-{operation_id}",
                topic_fd=topic_fd,
                lock_fd=lock_fd,
                identity=_identity(topic_fd),
                lock_identity=_identity(lock_fd),
                detached=False,
                placeholder_created=placeholder_created,
                placeholder_resolved=False,
            )
            records.append(record)
            if placeholder_created:
                if (
                    _entry_identity(parent_fd, visible) != record.identity
                    or set(os.listdir(topic_fd)) != {LOCK_NAME}
                    or _regular_entry_identity(topic_fd, LOCK_NAME) != record.lock_identity
                ):
                    raise ValueError("topic placeholder contains protected concurrent content")
            else:
                _validate_delete_directory(
                    parent_fd,
                    topic_fd,
                    visible,
                    record.identity,
                    record.lock_identity,
                    topic.id,
                )

        with repo._connection_lock:
            if repo.conn.in_transaction:
                raise ValueError("topic merge requires outermost transaction ownership")
            for record in records:
                current = repo.get_topic(record.topic.id)
                current_course = repo.get_course(current.course_id) if current else None
                current_name = (
                    f"{current.seq + 1:02d}-{safe_name(current.title)}" if current else ""
                )
                if (
                    current != record.topic
                    or current_course is None
                    or current_course.root_dir != course.root_dir
                    or current_name != record.visible
                ):
                    raise ValueError("topic changed during merge")
                if current.status == "RUNNING":
                    raise ValueError("running topic is protected")
                if repo.has_published_topic_output(current.id):
                    raise ValueError("topic with published output is protected")
                if record.placeholder_created:
                    if (
                        _entry_identity(parent_fd, record.visible) != record.identity
                        or set(os.listdir(record.topic_fd)) != {LOCK_NAME}
                        or _regular_entry_identity(record.topic_fd, LOCK_NAME)
                        != record.lock_identity
                    ):
                        raise ValueError("topic placeholder contains protected concurrent content")
                else:
                    _validate_delete_directory(
                        parent_fd,
                        record.topic_fd,
                        record.visible,
                        record.identity,
                        record.lock_identity,
                        current.id,
                    )
            try:
                for record in records:
                    _merge_file_hook("before_detach", parent_fd, record.topic_fd, record.visible)
                    os.rename(
                        record.visible,
                        record.hidden,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    record.detached = True
                os.fsync(parent_fd)
                merged = repo.merge_topics(
                    course_id,
                    topic_ids,
                    title=title,
                    description=description,
                    chapter_ids=chapter_ids,
                )
            except BaseException:
                for record in reversed(records):
                    if record.detached:
                        if record.placeholder_created:
                            _remove_created_placeholder(
                                parent_fd,
                                record.topic_fd,
                                record.hidden,
                                record.identity,
                                record.lock_identity,
                            )
                            record.placeholder_resolved = True
                        else:
                            os.rename(
                                record.hidden,
                                record.visible,
                                src_dir_fd=parent_fd,
                                dst_dir_fd=parent_fd,
                            )
                        record.detached = False
                os.fsync(parent_fd)
                raise

        for record in records:
            try:
                if record.placeholder_created:
                    _merge_file_hook(
                        "before_placeholder_cleanup",
                        parent_fd,
                        record.topic_fd,
                        record.hidden,
                    )
                    _remove_created_placeholder(
                        parent_fd,
                        record.topic_fd,
                        record.hidden,
                        record.identity,
                        record.lock_identity,
                    )
                    record.placeholder_resolved = True
                else:
                    _remove_detached_topic_directory(
                        parent_fd,
                        record.topic_fd,
                        record.hidden,
                        record.identity,
                        record.lock_identity,
                        record.topic.id,
                    )
            except (OSError, ValueError):
                pass
        return merged
    finally:
        for record in reversed(records):
            try:
                if (
                    record.placeholder_created
                    and not record.placeholder_resolved
                    and not record.detached
                ):
                    _remove_created_placeholder(
                        parent_fd,
                        record.topic_fd,
                        record.visible,
                        record.identity,
                        record.lock_identity,
                    )
                    record.placeholder_resolved = True
                fcntl.flock(record.lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(record.lock_fd)
                os.close(record.topic_fd)
        os.close(parent_fd)


def delete_unpublished_topic(repo: WorkbenchRepository, topic_id: str) -> None:
    topic = repo.get_topic(topic_id)
    if topic is None:
        raise ValueError("topic not found")
    course = repo.get_course(topic.course_id)
    if course is None:
        raise ValueError("course not found")

    course_root = Path(course.root_dir)
    name = f"{topic.seq + 1:02d}-{safe_name(topic.title)}"
    parent_fd = open_secure_directory(course_root, ["课程主题"])
    try:
        _delete_race_hook("before_target_open", parent_fd, -1, name)
        placeholder_created = False
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            placeholder_created = True
        except FileExistsError:
            pass
        try:
            topic_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError("topic directory identity is protected") from exc
        directory_locked = False
        try:
            fcntl.flock(topic_fd, fcntl.LOCK_EX)
            directory_locked = True
            expected_identity = _identity(topic_fd)
            if placeholder_created:
                lock_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            else:
                lock_flags = os.O_RDWR | os.O_NOFOLLOW
            try:
                lock_fd = os.open(LOCK_NAME, lock_flags, 0o600, dir_fd=topic_fd)
            except FileNotFoundError:
                entries = set(os.listdir(topic_fd))
                if not entries <= {OWNER_NAME, "topic-map.md"} or OWNER_NAME not in entries:
                    raise ValueError("topic directory lock is protected") from None
                owner = _read_regular_file_at(topic_fd, OWNER_NAME)
                if owner.splitlines()[:1] != [f"topic:{topic_id}".encode()]:
                    raise ValueError("topic directory ownership is protected") from None
                try:
                    lock_fd = os.open(
                        LOCK_NAME,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=topic_fd,
                    )
                except FileExistsError:
                    lock_fd = os.open(LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW, dir_fd=topic_fd)
            except OSError as exc:
                raise ValueError("topic directory lock is protected") from exc
            try:
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    raise ValueError("topic directory lock is protected")
                lock_identity = _identity(lock_fd)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                with repo._connection_lock:
                    placeholder_resolved = False
                    contents: dict[str, bytes] = {}
                    removed_names: set[str] = set()
                    try:
                        if placeholder_created:
                            _delete_race_hook("placeholder_locked", parent_fd, topic_fd, name)
                        if repo.conn.in_transaction:
                            raise ValueError(
                                "topic deletion requires outermost transaction ownership"
                            )
                        current_topic = repo.get_topic(topic_id)
                        if current_topic is None:
                            raise ValueError("topic not found")
                        current_course = repo.get_course(current_topic.course_id)
                        if current_course is None:
                            raise ValueError("course not found")
                        current_name = (
                            f"{current_topic.seq + 1:02d}-{safe_name(current_topic.title)}"
                        )
                        if (
                            current_topic.course_id != topic.course_id
                            or current_course.root_dir != course.root_dir
                            or current_name != name
                        ):
                            raise ValueError("topic directory identity is protected")
                        if current_topic.status == "RUNNING":
                            raise ValueError("topic is already running")
                        if repo.has_published_topic_output(topic_id):
                            raise ValueError("topic with published output is protected")

                        if placeholder_created:
                            with repo._atomic(immediate=True):
                                repo.delete_topic_guarded(topic_id)
                                _remove_created_placeholder(
                                    parent_fd,
                                    topic_fd,
                                    name,
                                    expected_identity,
                                    lock_identity,
                                )
                            placeholder_resolved = True
                            return

                        contents = _validate_delete_directory(
                            parent_fd,
                            topic_fd,
                            name,
                            expected_identity,
                            lock_identity,
                            topic_id,
                        )
                        _delete_race_hook("before_revalidate", parent_fd, topic_fd, name)
                        contents = _validate_delete_directory(
                            parent_fd,
                            topic_fd,
                            name,
                            expected_identity,
                            lock_identity,
                            topic_id,
                        )
                        _delete_race_hook("before_unlink", parent_fd, topic_fd, name)
                        os.unlink("topic-map.md", dir_fd=topic_fd)
                        removed_names.add("topic-map.md")
                        os.unlink(OWNER_NAME, dir_fd=topic_fd)
                        removed_names.add(OWNER_NAME)
                        _delete_race_hook("after_generated_unlink", parent_fd, topic_fd, name)
                        if set(os.listdir(topic_fd)) != {LOCK_NAME}:
                            raise ValueError(
                                "topic directory contains protected concurrent content"
                            )
                        if _entry_identity(parent_fd, name) != expected_identity:
                            raise ValueError("topic directory identity is protected")
                        if _regular_entry_identity(topic_fd, LOCK_NAME) != lock_identity:
                            raise ValueError("topic directory lock is protected")
                        os.unlink(LOCK_NAME, dir_fd=topic_fd)
                        removed_names.add(LOCK_NAME)
                        os.rmdir(name, dir_fd=parent_fd)
                        repo.delete_topic_guarded(topic_id)
                    except Exception as exc:
                        if removed_names:
                            try:
                                _restore_delete_directory(
                                    parent_fd,
                                    topic_fd,
                                    name,
                                    expected_identity,
                                    contents,
                                    removed_names,
                                )
                            except ValueError:
                                raise
                            except OSError as restore_exc:
                                raise TopicMarkdownDeleteError(
                                    "topic directory cleanup failed and restoration failed"
                                ) from restore_exc
                        if isinstance(exc, ValueError):
                            raise
                        if placeholder_created:
                            raise TopicMarkdownDeleteError(
                                "topic placeholder cleanup failed"
                            ) from exc
                        raise TopicMarkdownDeleteError("topic directory cleanup failed") from exc
                    finally:
                        if placeholder_created and not placeholder_resolved:
                            _remove_created_placeholder(
                                parent_fd,
                                topic_fd,
                                name,
                                expected_identity,
                                lock_identity,
                            )
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        finally:
            try:
                if directory_locked:
                    fcntl.flock(topic_fd, fcntl.LOCK_UN)
            finally:
                os.close(topic_fd)
    finally:
        os.close(parent_fd)


def sync_topic_markdown(
    repo: WorkbenchRepository,
    topic_id: str,
    *,
    fence: Callable[[], object] | None = None,
    owner_id: str | None = None,
    clock: Callable[[], int] | None = None,
    lease_ttl: int = 600,
) -> dict[str, str]:
    failed = False
    result: dict[str, str] | None = None
    try:
        result = _publish_topic_markdown(
            repo,
            topic_id,
            mapping_only=False,
            fence=fence,
            owner_id=owner_id,
            clock=clock,
            lease_ttl=lease_ttl,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        failed = True
    if failed or result is None:
        raise TopicMarkdownSyncError()
    return result


def sync_topic_map_markdown(
    repo: WorkbenchRepository,
    topic_id: str,
    *,
    fence: Callable[[], object] | None = None,
    owner_id: str | None = None,
    clock: Callable[[], int] | None = None,
    lease_ttl: int = 600,
) -> Path:
    failed = False
    result: dict[str, str] | None = None
    try:
        result = _publish_topic_markdown(
            repo,
            topic_id,
            mapping_only=True,
            fence=fence,
            owner_id=owner_id,
            clock=clock,
            lease_ttl=lease_ttl,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        failed = True
    if failed or result is None:
        raise TopicMarkdownSyncError()
    return Path(result["map"])


def recover_topic_publication_journals(
    repo: WorkbenchRepository,
    topic_id: str,
) -> None:
    topic = repo.get_topic(topic_id)
    if topic is None:
        raise ValueError("topic not found")
    course = repo.get_course(topic.course_id)
    if course is None:
        raise ValueError("course not found")
    course_root = Path(course.root_dir)
    if course_root.is_symlink():
        raise OSError("course root cannot be opened safely")
    if not course_root.exists():
        return
    if not course_root.is_dir():
        raise OSError("course root cannot be opened safely")
    topics_root = course_root / "课程主题"
    marker = f"<!-- topic-id: {topic.id} -->"
    topic_dir = markdown_publication._directory_with_marker(topics_root, marker)
    if topic_dir is None:
        target = topics_root / f"{topic.seq + 1:02d}-{safe_name(topic.title)}"
        if (
            not target.is_dir()
            or target.is_symlink()
            or _first_line_regular_file(target / OWNER_NAME) != f"topic:{topic.id}"
        ):
            return
        topic_dir = target
    relative = topic_dir.relative_to(course_root)

    def committed(token: str) -> bool:
        return repo.markdown_publication_committed("topic", topic_id, token)

    with ExitStack() as stack:
        topic_fd = open_secure_directory(
            course_root,
            list(relative.parts),
            create=False,
        )
        stack.callback(os.close, topic_fd)
        recover_atomic_bundle(topic_fd, transaction_committed=committed)
        try:
            runs_fd = open_secure_directory(
                course_root,
                [*relative.parts, "runs"],
                create=False,
            )
        except FileNotFoundError:
            return
        stack.callback(os.close, runs_fd)
        recover_atomic_bundle(runs_fd, transaction_committed=committed)


def _publish_topic_markdown(
    repo: WorkbenchRepository,
    topic_id: str,
    *,
    mapping_only: bool,
    fence: Callable[[], object] | None,
    owner_id: str | None,
    clock: Callable[[], int] | None,
    lease_ttl: int,
) -> dict[str, str]:
    current_time = clock or (lambda: int(time.time()))
    if owner_id is None:
        if repo.get_topic_markdown_sync_state(topic_id) is None:
            repo.set_topic_markdown_sync_state(topic_id, "PENDING")
        sync_claim = repo.claim_topic_markdown_sync(
            topic_id,
            now=current_time(),
            lease_ttl=lease_ttl,
        )
        owner_id = sync_claim.owner_id
    else:
        repo.fence_topic_markdown_sync(
            topic_id,
            owner_id,
            now=current_time(),
            lease_ttl=lease_ttl,
        )
    try:
        state_fingerprint, state_revision = repo.markdown_publication_state_snapshot(
            "topic", topic_id
        )
        with ExitStack() as stack:
            bundles: list[tuple[int, dict[str, str]]] = []
            if mapping_only:
                context = _prepare_topic_directory(repo, topic_id, stack)
                map_text = _topic_map_text(
                    repo,
                    context.topic,
                    context.course,
                    context.chapters,
                    context.marker,
                )
                bundles.append(
                    (context.topic_fd, {"topic-map.md": redact_sensitive_text(map_text)})
                )
                result = {"map": str(context.topic_dir / "topic-map.md")}
            else:
                result = _sync_topic_markdown(
                    repo,
                    topic_id,
                    stack=stack,
                    bundle_writer=lambda dir_fd, contents: bundles.append((dir_fd, contents)),
                )
            if fence is not None:
                fence()
            claim = repo.claim_markdown_publication(
                "topic",
                topic_id,
                state_fingerprint,
                publication_bundle_fingerprint(bundles),
                state_revision=state_revision,
                owner_id=owner_id,
                now=current_time(),
                lease_ttl=lease_ttl,
            )
            markdown_publication._publication_race_hook("topic", topic_id)
            commit_markdown_publication(
                repo,
                claim,
                bundles,
                fence_context=lambda: repo.fence_topic_markdown_publication(
                    claim,
                    clock=current_time,
                ),
            )
            return result
    except BaseException:
        try:
            repo.finish_topic_markdown_sync(
                topic_id,
                owner_id,
                "FAILED",
                "TOPIC_MARKDOWN_PUBLICATION_FAILED",
                now=current_time(),
            )
        except Exception:
            pass
        raise


def _prepare_topic_directory(
    repo: WorkbenchRepository,
    topic_id: str,
    stack: ExitStack,
) -> _TopicDirectoryContext:
    topic = repo.get_topic(topic_id)
    if topic is None:
        raise ValueError("topic not found")
    course = repo.get_course(topic.course_id)
    if course is None:
        raise ValueError("course not found")
    chapters = repo.list_topic_chapters(topic_id)
    course_root = Path(course.root_dir)
    course_root.mkdir(parents=True, exist_ok=True)
    if course_root.is_symlink():
        raise OSError("course root symlink rejected")
    topics_root = course_root / "课程主题"
    target = topics_root / f"{topic.seq + 1:02d}-{safe_name(topic.title)}"
    topics_fd = open_secure_directory(course_root, ["课程主题"])
    stack.callback(os.close, topics_fd)
    target_existed = target.exists()
    marker = f"<!-- topic-id: {topic.id} -->"
    topic_dir = migrate_generated_directory(
        topics_root, target, marker, entity_type="topic", entity_id=topic.id
    )
    relative = topic_dir.relative_to(course_root)
    topic_fd = open_secure_directory(course_root, list(relative.parts))
    stack.callback(os.close, topic_fd)
    formal_owner = _first_line_regular_file(topic_dir / "topic-map.md") == marker
    ensure_directory_owner(
        topic_fd,
        "topic",
        topic.id,
        allow_create_or_replace=not target_existed or formal_owner,
    )
    return _TopicDirectoryContext(
        topic=topic,
        course=course,
        chapters=chapters,
        topic_dir=topic_dir,
        topic_fd=topic_fd,
        marker=marker,
    )


def _topic_map_text(
    repo: WorkbenchRepository,
    topic: CourseTopic,
    course: Course,
    chapters: list[Chapter],
    marker: str,
) -> str:
    course_sources = repo.list_sources(topic.course_id)
    sources = {source.id: source for source in course_sources}
    display = allocate_source_display_titles(
        [(source.id, source.title) for source in course_sources]
    )
    lines = [
        marker,
        f"# {topic.title}",
        "",
        topic.description,
        "",
        f"生成原因：{topic.generation_reason}",
        f"状态：{topic.status}",
        f"已确认：{'是' if topic.confirmed else '否'}",
        "",
        "## 教材章节",
        "",
    ]
    for chapter in chapters:
        source = sources[chapter.source_id]
        chapter_note = (
            textbook_dir(repo, source)
            / f"{chapter.seq + 1:02d}-{safe_name(chapter.title)}"
            / "intensive-note.md"
        )
        relative = Path("../..") / chapter_note.relative_to(course.root_dir)
        label = f"[《{display[source.id]}》·第 {chapter.seq + 1} 章]"
        lines.append(f"- {label} [{chapter.title}]({relative.as_posix()})")
    lines.append("")
    return "\n".join(lines)


def _sync_topic_markdown(
    repo: WorkbenchRepository,
    topic_id: str,
    *,
    stack: ExitStack,
    bundle_writer: Callable[[int, dict[str, str]], None],
) -> dict[str, str]:
    context = _prepare_topic_directory(repo, topic_id, stack)
    topic = context.topic
    course = context.course
    chapters = context.chapters
    topic_dir = context.topic_dir
    topic_fd = context.topic_fd
    marker = context.marker
    blocks = {item.kind: item.content for item in repo.list_topic_note_blocks(topic_id)}
    if set(blocks) != set(FIXED_TOPIC_KINDS):
        raise ValueError("topic must contain exactly fourteen blocks")
    cards = repo.list_topic_cards(topic_id)
    if not 8 <= len(cards) <= 12:
        raise ValueError("topic cards must contain 8..12 items")
    display = allocate_source_display_titles(
        [(source.id, source.title) for source in repo.list_sources(topic.course_id)]
    )
    allowed_refs = {
        f"[《{display[chapter.source_id]}》·第 {chapter.seq + 1} 章]" for chapter in chapters
    }
    parsed_refs: list[list[str]] = []
    for card in cards:
        try:
            raw_refs: object = json.loads(card.source_refs_json)
        except json.JSONDecodeError as exc:
            raise ValueError("topic card source refs must be list[str]") from exc
        if (
            not isinstance(raw_refs, list)
            or not raw_refs
            or any(not isinstance(ref, str) for ref in raw_refs)
        ):
            raise ValueError("topic card source refs must be list[str]")
        refs = [ref for ref in raw_refs if isinstance(ref, str)]
        if not set(refs) <= allowed_refs:
            raise ValueError("topic card contains unknown source ref")
        parsed_refs.append(refs)

    course_root = Path(course.root_dir)
    relative = topic_dir.relative_to(course_root)
    runs_fd = open_secure_directory(course_root, [*relative.parts, "runs"])
    stack.callback(os.close, runs_fd)
    note_lines = [marker, f"# {topic.title}", ""]
    for kind in FIXED_TOPIC_KINDS:
        note_lines.extend([f"## {SECTION_TITLES[kind]}", ""])
        if kind.endswith("mermaid"):
            note_lines.extend(["```mermaid", _pure_mermaid(blocks[kind]), "```", ""])
        else:
            note_lines.extend([blocks[kind], ""])
    note_lines.extend(["## 15. 写作卡片", ""])
    if len(cards) != len(parsed_refs):
        raise RuntimeError("topic card reference count mismatch")
    for index, card in enumerate(cards):
        refs = parsed_refs[index]
        note_lines.extend(
            [
                f"### {card.title}",
                "",
                f"类型：{card.card_type}",
                f"来源：{'、'.join(refs)}",
                "",
                card.content,
                "",
            ]
        )
    note_lines.append("")

    card_lines = [f"# {topic.title} 写作卡片", ""]
    for index, card in enumerate(cards):
        refs = parsed_refs[index]
        card_lines.extend(
            [
                f"## {card.title}",
                "",
                f"类型：{card.card_type}",
                f"来源：{'、'.join(refs)}",
                "",
                card.content,
                "",
            ]
        )

    map_text = _topic_map_text(repo, topic, course, chapters, marker)

    paths = {
        "map": topic_dir / "topic-map.md",
        "note": topic_dir / "intensive-note.md",
        "cards": topic_dir / "cards.md",
    }
    topic_contents = {
        "topic-map.md": map_text,
        "intensive-note.md": "\n".join(note_lines),
        "cards.md": "\n".join(card_lines),
    }
    topic_contents = {
        name: redact_sensitive_text(content) for name, content in topic_contents.items()
    }
    bundle_writer(topic_fd, topic_contents)
    run_contents: dict[str, str] = {}
    for run in repo.list_topic_runs(topic_id):
        name = f"{run.started_at}-{safe_name(run.id)}-{safe_name(run.round_key)}.md"
        run_contents[name] = redact_sensitive_text(
            "\n".join(
                [
                    f"# {run.round_key}",
                    "",
                    f"状态：{run.status}",
                    f"输出：{run.output}",
                    f"错误：{run.error}",
                    "",
                ]
            )
        )
    if run_contents:
        bundle_writer(runs_fd, run_contents)
    return {key: str(path) for key, path in paths.items()}
