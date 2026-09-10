from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from parsing_core.llm.base import LLMClient
from parsing_core.log import get_logger
from parsing_core.models.dataclasses import AIArtifact, Section, Task
from parsing_core.parser.chunker import split_sections
from parsing_core.parser.image_extractor import extract_images
from parsing_core.parser.markitdown_adapter import MarkItDownAdapter
from parsing_core.storage.cache import CacheService
from parsing_core.storage.fs_layout import (
    FsLayout,
    TaskDirectoryCapability,
    UnsafeTaskPathError,
)
from parsing_core.storage.repository import Repository, ResumeClaimLost
from parsing_core.utils.file_lock import snapshot
from parsing_core.utils.hashing import file_sha256, text_sha256

log = get_logger(__name__)

MAX_CACHED_IMAGE_FILES = 20_000
# Textbooks may contain many grouped figures, but should not need arbitrary trees.
MAX_CACHED_IMAGE_ENTRIES = 25_000
MAX_CACHED_IMAGE_DEPTH = 16
MAX_CACHED_IMAGE_FILE_BYTES = 100 * 1024 * 1024
MAX_CACHED_IMAGE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
# Publication fsync validates the freshly written task output tree. It must not
# share the cache-copy policy limits: a legitimate output tree (sections, ai
# artifacts, images, receipt) can exceed cache-copy test limits.
MAX_PUBLICATION_TREE_ENTRIES = 1_000_000
MAX_PUBLICATION_TREE_DEPTH = 64
MAX_MARKDOWN_IMAGE_LINK_LENGTH = 8 * 1024
MAX_MARKDOWN_IMAGE_SCAN_WORK_FACTOR = 8
_IMAGE_COPY_CHUNK_BYTES = 1024 * 1024
_SNAPSHOT_COPY_CHUNK_BYTES = 1024 * 1024
_RESUME_LOCKS_DIR = ".locks"
_PUBLICATION_RECEIPT_NAME = ".publication-receipt.json"
# The desktop sidecar is single-instance; flock remains the cross-process authority
# during startup cleanup and the database generation fences every persisted resume write.
_PROCESS_RESUME_OWNER = uuid.uuid4().hex
_ACTIVE_RESUME_OWNERS: set[str] = set()
_ACTIVE_RESUME_OWNERS_LOCK = threading.Lock()


class _InvalidCachedTask(Exception):
    pass


class TaskPublicationError(RuntimeError):
    pass


class UntrustedTaskDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class _ResumeClaim:
    owner: str
    generation: int
    lock_fd: int


@dataclass
class _MarkdownScanBudget:
    limit: int
    used: int = 0

    def consume(self, amount: int = 1) -> None:
        self.used += amount
        if self.used > self.limit:
            raise _InvalidCachedTask(
                "Markdown image scan exceeded linear work budget "
                f"(used={self.used}, limit={self.limit})"
            )


@dataclass(frozen=True)
class _CachedTaskData:
    sections: list[Section]
    artifacts: list[AIArtifact]
    raw_text: dict[str, str]
    ai_text: dict[str, str]


@dataclass
class _CacheValidationSession:
    valid: dict[str, _CachedTaskData]
    invalid: set[str]


@dataclass(frozen=True)
class _LocalImageReference:
    destination: str
    resolved_path: Path
    was_absolute: bool
    suffix: str


@dataclass(frozen=True)
class _MarkdownImageLink:
    start: int
    end: int
    destination_start: int
    destination_end: int
    destination: str


@dataclass
class _ImageCopyState:
    entry_count: int = 0
    file_count: int = 0
    total_bytes: int = 0


class Orchestrator:
    def __init__(
        self,
        repo: Repository,
        fs: FsLayout,
        llm: LLMClient,
        db_path: str,
        on_progress: Callable[[str, str, dict[str, object]], None] | None = None,
    ) -> None:
        self.repo = repo
        self.fs = fs
        self.llm = llm
        self.db_path = db_path
        self.parser = MarkItDownAdapter()
        self.cache = CacheService(repo)
        self.on_progress = on_progress
        self._clear_stale_resume_claims()

    @staticmethod
    def _resume_lock_name(task_id: str) -> str:
        lock_name = f"{task_id}.lock"
        if len(lock_name.encode("utf-8")) <= 255:
            return lock_name
        return f"{hashlib.sha256(task_id.encode('utf-8')).hexdigest()}.lock"

    def _try_open_resume_lock(
        self,
        task_id: str,
        *,
        blocking: bool = False,
    ) -> int | None:
        self.fs.validate_task_id(task_id)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        lock_name = self._resume_lock_name(task_id)
        missing_error: FileNotFoundError | None = None
        for _attempt in range(3):
            base_fd = self.fs.open_base_fd()
            locks_fd: int | None = None
            lock_fd: int | None = None
            try:
                try:
                    os.mkdir(_RESUME_LOCKS_DIR, mode=0o700, dir_fd=base_fd)
                except FileExistsError:
                    pass
                try:
                    locks_fd = os.open(
                        _RESUME_LOCKS_DIR,
                        directory_flags,
                        dir_fd=base_fd,
                    )
                    if not stat.S_ISDIR(os.fstat(locks_fd).st_mode):
                        raise UntrustedTaskDataError("resume lock root is not a directory")
                    os.fchmod(locks_fd, 0o700)
                    lock_fd = os.open(
                        lock_name,
                        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=locks_fd,
                    )
                except FileNotFoundError as error:
                    missing_error = error
                    continue
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    raise UntrustedTaskDataError("resume lock is not a regular file")
                os.fchmod(lock_fd, 0o600)
                operation = fcntl.LOCK_EX
                if not blocking:
                    operation |= fcntl.LOCK_NB
                try:
                    fcntl.flock(lock_fd, operation)
                except BlockingIOError:
                    os.close(lock_fd)
                    lock_fd = None
                    return None
                claimed_fd = lock_fd
                lock_fd = None
                return claimed_fd
            finally:
                if lock_fd is not None:
                    os.close(lock_fd)
                if locks_fd is not None:
                    os.close(locks_fd)
                os.close(base_fd)
        if missing_error is not None:
            raise missing_error
        raise RuntimeError("task lock could not be opened")

    @staticmethod
    def _close_resume_lock(lock_fd: int) -> None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    def _clear_stale_resume_claims(self) -> None:
        for recovery in self.repo.list_claimed_task_recoveries():
            owner = recovery["resume_owner"]
            if owner is None:
                continue
            with _ACTIVE_RESUME_OWNERS_LOCK:
                if owner in _ACTIVE_RESUME_OWNERS:
                    continue
            try:
                lock_fd = self._try_open_resume_lock(recovery["task_id"])
            except (OSError, RuntimeError):
                log.warning(
                    "stale_resume_claim_lock_unavailable task_id=%s",
                    recovery["task_id"],
                )
                continue
            if lock_fd is None:
                continue
            try:
                self.repo.release_task_resume(
                    recovery["task_id"],
                    owner,
                    recovery["resume_generation"],
                )
            finally:
                self._close_resume_lock(lock_fd)

    def _try_claim_resume(self, task_id: str) -> _ResumeClaim | None:
        lock_fd = self._try_open_resume_lock(task_id)
        if lock_fd is None:
            return None
        owner: str | None = None
        try:
            if self.repo.get_task(task_id) is None:
                return None
            owner = f"{_PROCESS_RESUME_OWNER}:{uuid.uuid4().hex}"
            with _ACTIVE_RESUME_OWNERS_LOCK:
                _ACTIVE_RESUME_OWNERS.add(owner)
            generation = self.repo.claim_task_resume(task_id, owner)
            if generation is None:
                return None
            claim = _ResumeClaim(owner=owner, generation=generation, lock_fd=lock_fd)
            owner = None
            lock_fd = None
            return claim
        finally:
            if owner is not None:
                with _ACTIVE_RESUME_OWNERS_LOCK:
                    _ACTIVE_RESUME_OWNERS.discard(owner)
            if lock_fd is not None:
                self._close_resume_lock(lock_fd)

    def _release_resume(self, task_id: str, claim: _ResumeClaim) -> None:
        try:
            self.repo.release_task_resume(
                task_id,
                claim.owner,
                claim.generation,
            )
        finally:
            with _ACTIVE_RESUME_OWNERS_LOCK:
                _ACTIVE_RESUME_OWNERS.discard(claim.owner)
            self._close_resume_lock(claim.lock_fd)

    @staticmethod
    def _stable_file_identity(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_size,
            file_stat.st_mtime_ns,
            file_stat.st_ctime_ns,
        )

    def _verified_snapshot_copy(self, task: Task) -> str:
        source_fd: int | None = None
        private_fd: int | None = None
        private_path = ""
        try:
            try:
                source_fd = os.open(
                    task.snapshot_path,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                )
            except OSError as error:
                raise UntrustedTaskDataError("snapshot cannot be opened safely") from error
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise UntrustedTaskDataError("snapshot is not a regular file")

            private_fd, private_path = tempfile.mkstemp(suffix=Path(task.snapshot_path).suffix)
            digest = hashlib.sha256()
            private_stream = os.fdopen(private_fd, "wb")
            private_fd = None
            with private_stream:
                while True:
                    chunk = os.read(source_fd, _SNAPSHOT_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    private_stream.write(chunk)

            after = os.fstat(source_fd)
            if self._stable_file_identity(before) != self._stable_file_identity(after):
                raise UntrustedTaskDataError("snapshot changed while being verified")
            if digest.hexdigest() != task.file_sha256:
                raise UntrustedTaskDataError("snapshot hash does not match the task")
            return private_path
        except BaseException:
            if private_path:
                self._remove_snapshot(private_path)
            raise
        finally:
            if private_fd is not None:
                os.close(private_fd)
            if source_fd is not None:
                os.close(source_fd)

    def _update_task_status(
        self,
        task_id: str,
        status: str,
        error_msg: str | None = None,
        claim: _ResumeClaim | None = None,
    ) -> None:
        if claim is None:
            self.repo.update_task_status(task_id, status, error_msg=error_msg)
            return
        self.repo.update_task_status_fenced(
            task_id,
            status,
            claim.owner,
            claim.generation,
            error_msg=error_msg,
        )

    def _safe_read_task_text(
        self,
        task_id: str,
        file_name: str,
        *,
        capability: TaskDirectoryCapability | None = None,
    ) -> str:
        if not file_name or Path(file_name).name != file_name:
            raise UntrustedTaskDataError("task output name is not a safe path component")
        self.fs.validate_task_id(task_id)
        task_fd: int | None = None
        file_fd: int | None = None
        try:
            if capability is None:
                task_fd = self.fs.open_task_fd(task_id)
            else:
                if capability.task_id != task_id:
                    raise UntrustedTaskDataError("task capability belongs to another task")
                capability.verify()
                task_fd = os.dup(capability.task_fd)
            file_fd = os.open(
                file_name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=task_fd,
            )
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise UntrustedTaskDataError("task output is not a regular file")
            with os.fdopen(
                file_fd,
                "r",
                encoding="utf-8",
                newline="",
                closefd=False,
            ) as task_file:
                content = task_file.read()
            after = os.fstat(file_fd)
            if self._stable_file_identity(before) != self._stable_file_identity(after):
                raise UntrustedTaskDataError("task output changed while being read")
            if capability is not None:
                capability.verify()
            return content
        except UntrustedTaskDataError:
            raise
        except (OSError, UnicodeError, UnsafeTaskPathError) as error:
            raise UntrustedTaskDataError("task output cannot be read safely") from error
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if task_fd is not None:
                os.close(task_fd)

    def _read_section_raw(
        self,
        task_id: str,
        section: Section,
        *,
        capability: TaskDirectoryCapability | None = None,
    ) -> str:
        if section.task_id != task_id or section.seq < 0:
            raise UntrustedTaskDataError("section does not belong to the task")
        task_path = capability.task_path if capability is not None else self.fs.task_path(task_id)
        expected_path = Path(task_path) / f"{section.seq}.raw.md"
        if Path(section.raw_md_path) != expected_path:
            raise UntrustedTaskDataError("section raw path is outside the task")
        raw = self._safe_read_task_text(
            task_id,
            f"{section.seq}.raw.md",
            capability=capability,
        )
        if text_sha256(raw) != section.sha256:
            raise UntrustedTaskDataError("section raw hash does not match")
        if len(raw) != section.char_count:
            raise UntrustedTaskDataError("section raw character count does not match")
        return raw

    def _read_section_ai(
        self,
        task_id: str,
        section: Section,
        artifact: AIArtifact,
        *,
        capability: TaskDirectoryCapability | None = None,
    ) -> str:
        if artifact.section_id != section.id:
            raise UntrustedTaskDataError("AI artifact belongs to another section")
        task_path = capability.task_path if capability is not None else self.fs.task_path(task_id)
        expected_path = Path(task_path) / f"{section.seq}.ai.md"
        if Path(artifact.ai_md_path) != expected_path:
            raise UntrustedTaskDataError("AI artifact path is outside the task")
        interpreted = self._safe_read_task_text(
            task_id,
            f"{section.seq}.ai.md",
            capability=capability,
        )
        if not interpreted.strip():
            raise UntrustedTaskDataError("AI artifact is empty")
        return interpreted

    def parse_file(
        self,
        file_path: str,
        force: bool = False,
        task_id: str | None = None,
        batch_id: str | None = None,
    ) -> dict[str, object]:
        explicit_task_id = task_id is not None
        task_lock_fd: int | None = None
        if task_id is not None:
            self.fs.validate_task_id(task_id)
            task_lock_fd = self._try_open_resume_lock(task_id, blocking=True)
            if task_lock_fd is None:
                raise RuntimeError("blocking task lock acquisition unexpectedly failed")
        try:
            # 1. 副本（永不触碰原文件）
            snap = snapshot(file_path)
            validation_session = _CacheValidationSession(valid={}, invalid=set())
            try:
                sha = file_sha256(snap)
            except BaseException:
                self._remove_snapshot(snap)
                raise

            # 2. 文件级缓存命中
            if not force:
                try:
                    hit = self.cache.find_completed_task_by_file_sha256(sha)
                except BaseException:
                    self._remove_snapshot(snap)
                    raise
                if hit:
                    cached_result = self._use_file_cache(
                        hit,
                        file_path=file_path,
                        snapshot_path=snap,
                        accepted_task_id=task_id,
                        batch_id=batch_id,
                        validation_session=validation_session,
                        locked_task_id=task_id if task_lock_fd is not None else None,
                    )
                    if cached_result is not None:
                        return cached_result

            # 3. 建任务
            task_id = task_id or str(uuid.uuid4())
            if task_lock_fd is None:
                task_lock_fd = self._try_open_resume_lock(task_id, blocking=True)
                if task_lock_fd is None:
                    raise RuntimeError("blocking task lock acquisition unexpectedly failed")
            now = int(time.time())
            task = Task(
                id=task_id,
                file_path=file_path,
                snapshot_path=snap,
                file_sha256=sha,
                status="PARSING",
                model_tier="stub",
                created_at=now,
                updated_at=now,
                batch_id=batch_id,
            )
            try:
                if explicit_task_id:
                    self.repo.promote_preregistered_task(task)
                else:
                    self.repo.create_task(task)
            except BaseException:
                self._remove_snapshot(snap)
                raise
            self._maybe_progress(task_id, "TASK_STATE", {"status": "PARSING"})

            return self._run_registered_parse(task, validation_session, reset_outputs=False)
        finally:
            if task_lock_fd is not None:
                self._close_resume_lock(task_lock_fd)

    def _run_registered_parse(
        self,
        task: Task,
        validation_session: _CacheValidationSession,
        *,
        reset_outputs: bool,
        claim: _ResumeClaim | None = None,
    ) -> dict[str, object]:
        task_id = task.id
        capability: TaskDirectoryCapability | None = None

        try:
            parser_input = task.snapshot_path
            private_snapshot = ""
            if reset_outputs:
                private_snapshot = self._verified_snapshot_copy(task)
                parser_input = private_snapshot
            try:
                if reset_outputs:
                    self._reset_task_outputs_for_reparse(task_id)
                capability = self.fs.open_task_capability(
                    task_id,
                    create=True,
                    with_images=True,
                )

                # 4. MarkItDown 解析
                raw_md = self.parser.parse(parser_input)
            finally:
                if private_snapshot:
                    self._remove_snapshot(private_snapshot)

            # 5. 图片落盘
            capability.verify()
            raw_md, _imgs = extract_images(raw_md, capability.images_path)
            capability.verify()

            # 6. 分节
            self._update_task_status(task_id, "SECTIONING", claim=claim)
            self._maybe_progress(task_id, "TASK_STATE", {"status": "SECTIONING"})
            chunks = split_sections(raw_md)
            if not chunks:
                raise RuntimeError("parser produced no sections")

            # 7. 落原文节到磁盘 + 写 DB
            now = int(time.time())
            prepared_sections: list[Section] = []
            for chunk in chunks:
                sid = str(uuid.uuid4())
                raw_path = self._write_task_text(
                    task_id,
                    f"{chunk.seq}.raw.md",
                    chunk.raw,
                    capability=capability,
                )
                prepared_sections.append(
                    Section(
                        id=sid,
                        task_id=task_id,
                        seq=chunk.seq,
                        raw_md_path=raw_path,
                        sha256=chunk.sha256,
                        char_count=chunk.char_count,
                        ai_status="PENDING",
                        created_at=now,
                    )
                )
            if claim is None:
                self.repo.create_sections(prepared_sections)
            else:
                self.repo.replace_sections_with_checkpoint(
                    task_id,
                    prepared_sections,
                    claim.owner,
                    claim.generation,
                )

            # 8. 节级 LLM 调用（含节级缓存命中复用）
            self._update_task_status(task_id, "LLM_RUNNING", claim=claim)
            self._maybe_progress(task_id, "TASK_STATE", {"status": "LLM_RUNNING"})
            sections = self.repo.list_sections(task_id)
            for sec in sections:
                self._interpret_section(
                    task_id,
                    sec,
                    validation_session,
                    claim=claim,
                    capability=capability,
                )

            # 9. 合流
            self._update_task_status(task_id, "MERGING", claim=claim)
            self._maybe_progress(task_id, "TASK_STATE", {"status": "MERGING"})
            merged = self._merge(
                task_id,
                task.file_path,
                capability=capability,
            )
            merged_path = self._write_task_text(
                task_id,
                "merged.md",
                merged,
                capability=capability,
            )

            self._write_publication_receipt(capability, kind="parse")
            capability.verify()
            self._update_task_status(task_id, "COMPLETED", claim=claim)
            capability.verify()
            self._maybe_progress(task_id, "TASK_STATE", {"status": "COMPLETED"})

            # 清理副本
            self._remove_snapshot(task.snapshot_path)

            return {
                "task_id": task_id,
                "merged_md_path": merged_path,
                "sections": len(sections),
                "cached": False,
                "status": "COMPLETED",
            }
        except Exception as e:
            log.exception("pipeline_failed task_id=%s file=%s", task_id, task.file_path)
            failure_recorded = False
            if not isinstance(e, ResumeClaimLost):
                try:
                    self._update_task_status(
                        task_id,
                        "FAILED",
                        error_msg=str(e),
                        claim=claim,
                    )
                except ResumeClaimLost:
                    log.warning("resume_claim_lost_while_failing task_id=%s", task_id)
                else:
                    failure_recorded = True
            if failure_recorded:
                self._maybe_progress(
                    task_id,
                    "TASK_STATE",
                    {"status": "FAILED", "error": str(e)},
                )
            raise
        finally:
            if capability is not None:
                capability.close()

    @staticmethod
    def _is_generated_markdown_name(name: str) -> bool:
        if name == "merged.md":
            return True
        parts = name.split(".")
        return (
            len(parts) == 3
            and parts[0].isdigit()
            and parts[1] in {"raw", "ai"}
            and parts[2] == "md"
        )

    @classmethod
    def _is_generated_text_temp_name(cls, name: str) -> bool:
        if not name.startswith(".") or not name.endswith(".tmp"):
            return False
        output_name, separator, nonce = name[1:-4].rpartition(".")
        return bool(separator and nonce and cls._is_generated_markdown_name(output_name))

    def _reset_task_outputs_for_reparse(self, task_id: str) -> None:
        identity = self.fs.task_identity(task_id)
        if identity is None:
            return
        if not self.fs.remove_task_tree(
            task_id,
            expected_identity=identity,
            allow_unsafe_entries=True,
        ):
            raise TaskPublicationError("could not quarantine task output for reparse")
        if self.fs.task_identity(task_id) is not None:
            raise TaskPublicationError("task output reappeared during reparse reset")

    def _write_task_text(
        self,
        task_id: str,
        file_name: str,
        content: str,
        *,
        capability: TaskDirectoryCapability | None = None,
    ) -> str:
        if Path(file_name).name != file_name or not file_name:
            raise ValueError("task output name must be a single path component")
        owned_capability = capability is None
        active = capability or self.fs.open_task_capability(task_id, create=True)
        if active.task_id != task_id:
            raise ValueError("task capability belongs to another task")
        temp_name = f".{file_name}.{uuid.uuid4().hex}.tmp"
        temp_fd: int | None = None
        try:
            active.verify()
            temp_fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=active.task_fd,
            )
            payload = content.encode("utf-8", "strict")
            view = memoryview(payload)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise OSError("task output write made no progress")
                view = view[written:]
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = None
            active.verify()
            os.replace(
                temp_name,
                file_name,
                src_dir_fd=active.task_fd,
                dst_dir_fd=active.task_fd,
            )
            os.fsync(active.task_fd)
            active.verify()
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            try:
                os.unlink(temp_name, dir_fd=active.task_fd)
            except FileNotFoundError:
                pass
            if owned_capability:
                active.close()
        return str(Path(active.task_path) / file_name)

    def _write_publication_receipt(
        self,
        capability: TaskDirectoryCapability,
        *,
        kind: str,
    ) -> None:
        capability.verify()
        self._fsync_directory_tree(capability.task_fd)
        capability.verify()
        payload = json.dumps(
            {
                "version": 1,
                "task_id": capability.task_id,
                "kind": kind,
                "token": uuid.uuid4().hex,
                "directory_identity": list(capability.task_identity),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._write_task_text(
            capability.task_id,
            _PUBLICATION_RECEIPT_NAME,
            payload,
            capability=capability,
        )
        capability.verify()

    @staticmethod
    def _fsync_directory_tree(root_fd: int) -> None:
        entry_count = 0

        def sync(directory_fd: int, depth: int) -> None:
            nonlocal entry_count
            if depth > MAX_PUBLICATION_TREE_DEPTH:
                raise TaskPublicationError("task output tree exceeds depth limit")
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    entry_count += 1
                    if entry_count > MAX_PUBLICATION_TREE_ENTRIES:
                        raise TaskPublicationError("task output tree exceeds entry limit")
                    name = entry.name
                    if type(name) is not str or not name or len(name.encode("utf-8")) > 255:
                        raise TaskPublicationError("task output tree has an unsafe name")
                    entry_stat = os.stat(
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if stat.S_ISLNK(entry_stat.st_mode):
                        raise TaskPublicationError("task output tree contains a symbolic link")
                    if stat.S_ISDIR(entry_stat.st_mode):
                        child_fd = os.open(
                            name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory_fd,
                        )
                        try:
                            opened_stat = os.fstat(child_fd)
                            if not Orchestrator._same_entry_identity(entry_stat, opened_stat):
                                raise TaskPublicationError("task output directory identity changed")
                            sync(child_fd, depth + 1)
                        finally:
                            os.close(child_fd)
                        continue
                    if not stat.S_ISREG(entry_stat.st_mode) or entry_stat.st_nlink != 1:
                        raise TaskPublicationError("task output tree contains an unsafe file")
                    file_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=directory_fd,
                    )
                    try:
                        opened_stat = os.fstat(file_fd)
                        if not Orchestrator._same_entry_identity(entry_stat, opened_stat):
                            raise TaskPublicationError("task output file identity changed")
                        os.fsync(file_fd)
                    finally:
                        os.close(file_fd)
            os.fsync(directory_fd)

        sync(root_fd, 0)

    def _interpret_section(
        self,
        task_id: str,
        sec: Section,
        validation_session: _CacheValidationSession,
        *,
        claim: _ResumeClaim | None = None,
        capability: TaskDirectoryCapability | None = None,
    ) -> None:
        raw_md = self._read_section_raw(task_id, sec, capability=capability)
        existing = self.repo.get_artifact_by_section(sec.id)
        if existing is not None and self._is_valid_resumable_artifact(
            task_id,
            sec,
            existing,
            capability=capability,
        ):
            self._complete_section_with_artifact(
                task_id,
                existing,
                claim,
                artifact_already_persisted=True,
            )
            return

        # 节级缓存命中：复用已有 artifact 的 ai_md_path 落盘
        hit = self.cache.find_completed_artifact_by_section_sha256(sec.sha256)
        if hit:
            try:
                cached_ai = self._validated_section_cache_text(hit, validation_session)
            except _InvalidCachedTask:
                log.warning(
                    "section_cache_invalid section_id=%s cached_artifact_id=%s",
                    sec.id,
                    hit.id,
                )
            else:
                ai_path = self._write_task_text(
                    task_id,
                    f"{sec.seq}.ai.md",
                    cached_ai,
                    capability=capability,
                )
                cached = AIArtifact(
                    id=str(uuid.uuid4()),
                    section_id=sec.id,
                    ai_md_path=ai_path,
                    ai_md="",
                    tokens_in=hit.tokens_in,
                    tokens_out=hit.tokens_out,
                    cost_usd=hit.cost_usd,
                    retry_count=0,
                    model_name=hit.model_name,
                    created_at=int(time.time()),
                )
                self._complete_section_with_artifact(task_id, cached, claim)
                return

        # 否则调 LLM 落盘
        artifact = self.llm.interpret(sec, raw_md)
        if artifact.section_id != sec.id:
            raise RuntimeError("LLM artifact belongs to another section")
        ai_path = self._write_task_text(
            task_id,
            f"{sec.seq}.ai.md",
            artifact.ai_md,
            capability=capability,
        )
        artifact.ai_md_path = ai_path
        self._complete_section_with_artifact(task_id, artifact, claim)

    def _complete_section_with_artifact(
        self,
        task_id: str,
        artifact: AIArtifact,
        claim: _ResumeClaim | None,
        *,
        artifact_already_persisted: bool = False,
    ) -> None:
        if claim is None:
            self.repo.complete_section_with_artifact(
                artifact,
                artifact_already_persisted=artifact_already_persisted,
            )
            return
        self.repo.complete_section_with_artifact_fenced(
            artifact,
            task_id,
            claim.owner,
            claim.generation,
            artifact_already_persisted=artifact_already_persisted,
        )

    def _is_valid_resumable_artifact(
        self,
        task_id: str,
        sec: Section,
        artifact: AIArtifact,
        *,
        capability: TaskDirectoryCapability | None = None,
    ) -> bool:
        try:
            self._read_section_ai(
                task_id,
                sec,
                artifact,
                capability=capability,
            )
        except UntrustedTaskDataError:
            return False
        return True

    def _validated_section_cache_text(
        self,
        artifact: AIArtifact,
        validation_session: _CacheValidationSession,
    ) -> str:
        source_section = self.repo.get_section(artifact.section_id)
        if source_section is None:
            raise _InvalidCachedTask("cached AI artifact has no section")
        source_task = self.repo.get_task(source_section.task_id)
        if source_task is None or source_task.status != "COMPLETED":
            raise _InvalidCachedTask("cached AI artifact has no completed task")
        cached = self._validated_cached_task(source_task, validation_session)
        if all(candidate.id != artifact.id for candidate in cached.artifacts):
            raise _InvalidCachedTask("cached AI artifact does not belong to validated task")
        cached_ai = cached.ai_text[source_section.id]
        if self._contains_reference_style_image(cached_ai):
            raise _InvalidCachedTask("cached AI artifact contains a reference-style image")
        source_dir = Path(self.fs.task_path(source_task.id))
        for link in self._validated_markdown_image_links(cached_ai, source_dir):
            if self._local_image_reference(link.destination, source_dir) is not None:
                raise _InvalidCachedTask("cached AI artifact contains a local image")
        return cached_ai

    def _merge(
        self,
        task_id: str,
        original_file_path: str,
        *,
        capability: TaskDirectoryCapability | None = None,
    ) -> str:
        sections = self.repo.list_sections(task_id)
        raw_text = {
            section.id: self._read_section_raw(
                task_id,
                section,
                capability=capability,
            )
            for section in sections
        }
        artifacts = {
            section.id: artifact
            for section in sections
            if (artifact := self.repo.get_artifact_by_section(section.id)) is not None
        }
        ai_text = {
            section.id: self._read_section_ai(
                task_id,
                section,
                artifacts[section.id],
                capability=capability,
            )
            for section in sections
            if section.id in artifacts
        }
        return self._render_merged(task_id, original_file_path, sections, raw_text, ai_text)

    def _render_merged(
        self,
        task_id: str,
        original_file_path: str,
        sections: list[Section],
        raw_text: dict[str, str],
        ai_text: dict[str, str],
        generated_at: str | None = None,
    ) -> str:
        generated_at = generated_at or time.strftime("%Y-%m-%d %H:%M:%S")
        out = [
            f"> 任务 ID: {task_id}",
            f"> 源文件: {original_file_path}",
            f"> 生成时间: {generated_at}",
            "",
        ]
        for s in sections:
            raw = raw_text[s.id]
            title = self._section_title(s.seq, raw)
            out.append(f"## 第 {s.seq + 1} 节：{title}")
            out.append("")
            out.append(raw.rstrip())
            out.append("")
            interpreted = ai_text.get(s.id)
            if interpreted is not None:
                out.append(interpreted.rstrip())
            else:
                out.append("### ▸ AI 解读")
                out.append("")
                out.append("⚠ 此节解读失败，可重试。")
            out.append("")
            out.append("---")
            out.append("")
        return "\n".join(out)

    def _use_file_cache(
        self,
        hit: Task,
        *,
        file_path: str,
        snapshot_path: str,
        accepted_task_id: str | None,
        batch_id: str | None,
        validation_session: _CacheValidationSession,
        locked_task_id: str | None,
    ) -> dict[str, object] | None:
        try:
            cached = self._validated_cached_task(hit, validation_session)
        except _InvalidCachedTask as error:
            log.warning(
                "file_cache_invalid cached_task_id=%s reason=%s",
                hit.id,
                error,
            )
            return None
        except BaseException:
            self._remove_snapshot(snapshot_path)
            raise

        if accepted_task_id is None or accepted_task_id == hit.id:
            return self._reuse_direct_file_cache(
                hit,
                snapshot_path=snapshot_path,
                validation_session=validation_session,
                lock_already_held=locked_task_id == hit.id,
            )

        try:
            result = self._materialize_cached_task(
                hit,
                cached,
                file_path=file_path,
                accepted_task_id=accepted_task_id,
                batch_id=batch_id,
            )
        except (OSError, _InvalidCachedTask) as error:
            validation_session.valid.pop(hit.id, None)
            validation_session.invalid.add(hit.id)
            log.warning(
                "file_cache_materialization_failed cached_task_id=%s error=%s",
                hit.id,
                error,
            )
            return None
        except BaseException:
            self._remove_snapshot(snapshot_path)
            raise
        self._remove_snapshot(snapshot_path)
        return result

    def _reuse_direct_file_cache(
        self,
        hit: Task,
        *,
        snapshot_path: str,
        validation_session: _CacheValidationSession,
        lock_already_held: bool,
    ) -> dict[str, object] | None:
        lock_fd: int | None = None
        try:
            if not lock_already_held:
                lock_fd = self._try_open_resume_lock(hit.id, blocking=True)
                if lock_fd is None:
                    raise RuntimeError("blocking task lock acquisition unexpectedly failed")

            current_hit = self.repo.get_task(hit.id)
            validation_session.valid.pop(hit.id, None)
            if (
                current_hit is None
                or current_hit.status != "COMPLETED"
                or current_hit.file_sha256 != hit.file_sha256
            ):
                validation_session.invalid.add(hit.id)
                return None
            try:
                cached = self._validated_cached_task(current_hit, validation_session)
            except _InvalidCachedTask as error:
                log.warning(
                    "file_cache_invalid_after_lock cached_task_id=%s reason=%s",
                    hit.id,
                    error,
                )
                return None

            self._remove_snapshot(snapshot_path)
            return {
                "task_id": current_hit.id,
                "merged_md_path": self.fs.merged_path(current_hit.id),
                "sections": len(cached.sections),
                "cached": True,
                "status": "COMPLETED",
            }
        except BaseException:
            self._remove_snapshot(snapshot_path)
            raise
        finally:
            if lock_fd is not None:
                self._close_resume_lock(lock_fd)

    def _validated_cached_task(
        self,
        task: Task,
        validation_session: _CacheValidationSession,
    ) -> _CachedTaskData:
        if task.id in validation_session.invalid:
            raise _InvalidCachedTask("cached task already failed validation")
        cached = validation_session.valid.get(task.id)
        if cached is not None:
            return cached
        try:
            cached = self._load_cached_task(task)
        except _InvalidCachedTask:
            validation_session.invalid.add(task.id)
            raise
        validation_session.valid[task.id] = cached
        return cached

    def _load_cached_task(self, hit: Task) -> _CachedTaskData:
        source_dir = Path(self.fs.task_path(hit.id))
        merged_path = source_dir / "merged.md"
        images_dir = source_dir / "images"
        try:
            if not source_dir.is_dir() or source_dir.is_symlink():
                raise _InvalidCachedTask("task directory is missing or unsafe")
            if not images_dir.is_dir() or images_dir.is_symlink():
                raise _InvalidCachedTask("images directory is missing or unsafe")
            self._validate_image_tree_bounded(
                images_dir,
                error_type=_InvalidCachedTask,
                label="cached images",
            )

            sections = self.repo.list_sections(hit.id)
            if not sections:
                raise _InvalidCachedTask("cached task has no sections")
            artifacts: list[AIArtifact] = []
            raw_text: dict[str, str] = {}
            ai_text: dict[str, str] = {}
            for section in sections:
                raw_path = Path(section.raw_md_path)
                if raw_path != source_dir / f"{section.seq}.raw.md":
                    raise _InvalidCachedTask("section path is outside the cached task")
                raw = self._read_cached_text(raw_path, source_dir)
                if text_sha256(raw) != section.sha256:
                    raise _InvalidCachedTask("section content hash does not match")
                if section.ai_status != "COMPLETED":
                    raise _InvalidCachedTask("cached section is not completed")
                artifact = self.repo.get_artifact_by_section(section.id)
                if artifact is None:
                    raise _InvalidCachedTask("cached section has no AI artifact")
                ai_path = Path(artifact.ai_md_path)
                if ai_path != source_dir / f"{section.seq}.ai.md":
                    raise _InvalidCachedTask("AI artifact path is outside the cached task")
                interpreted = self._read_cached_text(ai_path, source_dir)
                if not interpreted.strip():
                    raise _InvalidCachedTask("AI artifact is empty")
                self._validate_local_images(raw, source_dir)
                self._validate_local_images(interpreted, source_dir)
                artifacts.append(artifact)
                raw_text[section.id] = raw
                ai_text[section.id] = interpreted
            merged = self._read_cached_text(merged_path, source_dir)
            self._validate_local_images(merged, source_dir)
            generated_at = self._cached_merged_timestamp(merged, hit)
            expected_merged = self._render_merged(
                hit.id,
                hit.file_path,
                sections,
                raw_text,
                ai_text,
                generated_at=generated_at,
            )
            if merged != expected_merged:
                raise _InvalidCachedTask("merged document does not match cached sections")
        except _InvalidCachedTask:
            raise
        except (OSError, UnicodeError) as error:
            raise _InvalidCachedTask(str(error)) from error

        return _CachedTaskData(
            sections=sections,
            artifacts=artifacts,
            raw_text=raw_text,
            ai_text=ai_text,
        )

    @staticmethod
    def _read_cached_text(path: Path, source_dir: Path) -> str:
        Orchestrator._reject_symlink_components(path, source_dir)
        if path.parent != source_dir:
            raise _InvalidCachedTask("cached markdown file is outside the task root")
        directory_fd = os.open(
            source_dir,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            file_fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
        except OSError:
            os.close(directory_fd)
            raise
        try:
            path_stat = os.fstat(file_fd)
            if not stat.S_ISREG(path_stat.st_mode):
                raise _InvalidCachedTask("cached markdown file is not regular")
            with os.fdopen(file_fd, "r", encoding="utf-8", closefd=False) as cached_file:
                return cached_file.read()
        finally:
            os.close(file_fd)
            os.close(directory_fd)

    @staticmethod
    def _reject_symlink_components(path: Path, source_dir: Path) -> None:
        if source_dir.is_symlink():
            raise _InvalidCachedTask("cached task directory is a symlink")
        try:
            relative = path.relative_to(source_dir)
        except ValueError as error:
            raise _InvalidCachedTask("cached path is outside the task directory") from error
        current = source_dir
        for part in relative.parts:
            if part == "..":
                current = current.parent
            elif part != ".":
                current = current / part
            if current.is_symlink():
                raise _InvalidCachedTask("cached path contains a symlink")

    @staticmethod
    def _local_image_reference(
        destination: str,
        source_dir: Path,
    ) -> _LocalImageReference | None:
        try:
            parsed = urlsplit(destination)
            if parsed.scheme == "file":
                raise _InvalidCachedTask("file URI images are not allowed")
            if parsed.scheme or parsed.netloc or destination.startswith("//"):
                return None
            if not parsed.path or parsed.path.startswith("#"):
                return None

            decoded_path = unquote(parsed.path)
            if "\x00" in decoded_path:
                raise _InvalidCachedTask("image destination contains a NUL byte")
            lexical_path = Path(decoded_path)
            was_absolute = lexical_path.is_absolute()
            candidate = lexical_path if was_absolute else source_dir / lexical_path
            Orchestrator._reject_symlink_components(candidate, source_dir)
            resolved_path = candidate.resolve(strict=True)
            resolved_images_dir = (source_dir / "images").resolve(strict=True)
            resolved_path.relative_to(resolved_images_dir)
            if not resolved_path.is_file():
                raise _InvalidCachedTask("referenced cached image is not a file")
        except _InvalidCachedTask:
            raise
        except (OSError, ValueError) as error:
            raise _InvalidCachedTask("invalid cached image destination") from error
        return _LocalImageReference(
            destination=destination,
            resolved_path=resolved_path,
            was_absolute=was_absolute,
            suffix=destination[len(parsed.path) :],
        )

    @staticmethod
    def _validate_local_images(markdown: str, source_dir: Path) -> None:
        for link in Orchestrator._validated_markdown_image_links(markdown, source_dir):
            Orchestrator._local_image_reference(link.destination, source_dir)

    @staticmethod
    def _validated_markdown_image_links(
        markdown: str,
        source_dir: Path,
    ) -> list[_MarkdownImageLink]:
        links = Orchestrator._markdown_image_links(markdown)
        source_images_prefix = str(source_dir / "images")
        unparsed_pieces: list[str] = []
        cursor = 0
        for link in links:
            unparsed_pieces.append(markdown[cursor : link.destination_start])
            cursor = link.destination_end
        unparsed_pieces.append(markdown[cursor:])
        unparsed = "".join(unparsed_pieces)
        if source_images_prefix in unparsed or source_images_prefix in unquote(unparsed):
            raise _InvalidCachedTask("cached Markdown contains an unparsed source image path")
        return links

    @staticmethod
    def _contains_reference_style_image(markdown: str) -> bool:
        budget = _MarkdownScanBudget(limit=len(markdown) * MAX_MARKDOWN_IMAGE_SCAN_WORK_FACTOR)
        cursor = 0
        while cursor < len(markdown):
            budget.consume()
            if not markdown.startswith("![", cursor):
                cursor += 1
                continue
            start = cursor
            if Orchestrator._markdown_character_is_escaped(markdown, start, budget):
                cursor += 2
                continue
            cursor += 2
            depth = 0
            while cursor < len(markdown):
                budget.consume()
                Orchestrator._check_markdown_link_length(start, cursor)
                char = markdown[cursor]
                if char == "\\":
                    if cursor + 1 < len(markdown):
                        budget.consume()
                    cursor += 2
                    continue
                if char == "[":
                    depth += 1
                elif char == "]":
                    if depth:
                        depth -= 1
                    else:
                        break
                cursor += 1
            if cursor >= len(markdown):
                return False
            cursor += 1
            if cursor >= len(markdown) or markdown[cursor] != "(":
                return True
        return False

    @staticmethod
    def _markdown_image_links(markdown: str) -> list[_MarkdownImageLink]:
        links: list[_MarkdownImageLink] = []
        budget = _MarkdownScanBudget(limit=len(markdown) * MAX_MARKDOWN_IMAGE_SCAN_WORK_FACTOR)
        cursor = 0
        while cursor < len(markdown):
            budget.consume()
            if markdown[cursor] != "!" or not markdown.startswith("![", cursor):
                cursor += 1
                continue
            start = cursor
            if Orchestrator._markdown_character_is_escaped(markdown, start, budget):
                cursor += 2
                continue
            link = Orchestrator._scan_markdown_image_link(markdown, start, budget)
            if link is None:
                cursor = start + 2
                continue
            links.append(link)
            cursor = link.end
        return links

    @staticmethod
    def _scan_markdown_image_link(
        markdown: str,
        start: int,
        budget: _MarkdownScanBudget,
    ) -> _MarkdownImageLink | None:
        cursor = start + 2
        alt_depth = 0
        while cursor < len(markdown):
            budget.consume()
            Orchestrator._check_markdown_link_length(start, cursor)
            char = markdown[cursor]
            if char in "\r\n":
                return None
            if char == "\\":
                if cursor + 1 < len(markdown):
                    budget.consume()
                cursor += 2
                continue
            if char == "[":
                alt_depth += 1
            elif char == "]":
                if alt_depth:
                    alt_depth -= 1
                else:
                    break
            cursor += 1
        if cursor >= len(markdown) or cursor + 1 >= len(markdown):
            return None
        cursor += 1
        budget.consume()
        if markdown[cursor] != "(":
            return None
        cursor += 1
        while cursor < len(markdown) and markdown[cursor] in " \t":
            budget.consume()
            cursor += 1
        destination_start = cursor
        if cursor < len(markdown) and markdown[cursor] == "<":
            destination_start = cursor + 1
            cursor = destination_start
            while cursor < len(markdown):
                budget.consume()
                Orchestrator._check_markdown_link_length(start, cursor)
                char = markdown[cursor]
                if char in "\r\n":
                    return None
                if char == "\\":
                    if cursor + 1 < len(markdown):
                        budget.consume()
                    cursor += 2
                    continue
                if char == ">":
                    destination_end = cursor
                    cursor += 1
                    break
                cursor += 1
            else:
                return None
        else:
            depth = 0
            while cursor < len(markdown):
                budget.consume()
                Orchestrator._check_markdown_link_length(start, cursor)
                char = markdown[cursor]
                if char in "\r\n":
                    return None
                if char == "\\":
                    if cursor + 1 < len(markdown):
                        budget.consume()
                    cursor += 2
                    continue
                if char == "(":
                    depth += 1
                elif char == ")":
                    if depth == 0:
                        destination_end = cursor
                        return Orchestrator._make_markdown_image_link(
                            markdown,
                            start,
                            cursor + 1,
                            destination_start,
                            destination_end,
                            budget,
                        )
                    depth -= 1
                elif char in " \t" and depth == 0:
                    destination_end = cursor
                    break
                cursor += 1
            else:
                return None

        closing = Orchestrator._scan_image_link_trailer(
            markdown,
            start,
            cursor,
            budget,
        )
        if closing is None:
            return None
        return Orchestrator._make_markdown_image_link(
            markdown,
            start,
            closing,
            destination_start,
            destination_end,
            budget,
        )

    @staticmethod
    def _scan_image_link_trailer(
        markdown: str,
        start: int,
        cursor: int,
        budget: _MarkdownScanBudget,
    ) -> int | None:
        while cursor < len(markdown) and markdown[cursor] in " \t":
            budget.consume()
            cursor += 1
        if cursor >= len(markdown):
            return None
        budget.consume()
        Orchestrator._check_markdown_link_length(start, cursor)
        if markdown[cursor] == ")":
            return cursor + 1

        delimiter = markdown[cursor]
        if delimiter not in {'"', "'", "("}:
            return None
        closing_delimiter = ")" if delimiter == "(" else delimiter
        cursor += 1
        while cursor < len(markdown):
            budget.consume()
            Orchestrator._check_markdown_link_length(start, cursor)
            char = markdown[cursor]
            if char in "\r\n":
                return None
            if char == "\\":
                if cursor + 1 < len(markdown):
                    budget.consume()
                cursor += 2
                continue
            if char == closing_delimiter:
                cursor += 1
                break
            cursor += 1
        else:
            return None
        while cursor < len(markdown) and markdown[cursor] in " \t":
            budget.consume()
            cursor += 1
        if cursor >= len(markdown) or markdown[cursor] != ")":
            return None
        budget.consume()
        Orchestrator._check_markdown_link_length(start, cursor)
        return cursor + 1

    @staticmethod
    def _make_markdown_image_link(
        markdown: str,
        start: int,
        end: int,
        destination_start: int,
        destination_end: int,
        budget: _MarkdownScanBudget,
    ) -> _MarkdownImageLink:
        Orchestrator._check_markdown_link_length(start, end)
        raw_destination = markdown[destination_start:destination_end]
        destination: list[str] = []
        cursor = 0
        while cursor < len(raw_destination):
            budget.consume()
            if raw_destination[cursor] == "\\" and cursor + 1 < len(raw_destination):
                cursor += 1
                budget.consume()
            destination.append(raw_destination[cursor])
            cursor += 1
        return _MarkdownImageLink(
            start=start,
            end=end,
            destination_start=destination_start,
            destination_end=destination_end,
            destination="".join(destination),
        )

    @staticmethod
    def _check_markdown_link_length(start: int, cursor: int) -> None:
        if cursor - start > MAX_MARKDOWN_IMAGE_LINK_LENGTH:
            raise _InvalidCachedTask(
                f"Markdown image link exceeds {MAX_MARKDOWN_IMAGE_LINK_LENGTH} bytes"
            )

    @staticmethod
    def _markdown_character_is_escaped(
        markdown: str,
        index: int,
        budget: _MarkdownScanBudget,
    ) -> bool:
        backslashes = 0
        index -= 1
        while index >= 0 and markdown[index] == "\\":
            budget.consume()
            backslashes += 1
            index -= 1
        return backslashes % 2 == 1

    @staticmethod
    def _cached_merged_timestamp(merged: str, hit: Task) -> str:
        lines = merged.splitlines()
        if len(lines) < 3:
            raise _InvalidCachedTask("merged document header is incomplete")
        if lines[0] != f"> 任务 ID: {hit.id}":
            raise _InvalidCachedTask("merged document has an invalid task ID")
        if lines[1] != f"> 源文件: {hit.file_path}":
            raise _InvalidCachedTask("merged document has an invalid source path")
        prefix = "> 生成时间: "
        if not lines[2].startswith(prefix) or not lines[2][len(prefix) :]:
            raise _InvalidCachedTask("merged document has an invalid generation time")
        return lines[2][len(prefix) :]

    @staticmethod
    def _rewrite_cached_image_links(
        markdown: str,
        source_dir: Path,
        target_dir: Path,
    ) -> str:
        source_images = (source_dir / "images").resolve(strict=True)
        pieces: list[str] = []
        cursor = 0
        links = Orchestrator._validated_markdown_image_links(markdown, source_dir)
        for link in links:
            reference = Orchestrator._local_image_reference(link.destination, source_dir)
            if reference is None or not reference.was_absolute:
                continue
            relative = reference.resolved_path.relative_to(source_images)
            pieces.append(markdown[cursor : link.destination_start])
            pieces.append(quote(str(target_dir / "images" / relative), safe="/") + reference.suffix)
            cursor = link.destination_end
        if cursor == 0:
            return markdown
        pieces.append(markdown[cursor:])
        return "".join(pieces)

    @staticmethod
    def _copy_cached_images_bounded(source_images: Path, target_images: Path) -> None:
        try:
            target_images.mkdir()
        except OSError as error:
            raise TaskPublicationError("could not create cached image target") from error
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        source_fd = os.open(source_images, directory_flags)
        try:
            Orchestrator._copy_cached_image_directory(
                source_fd,
                target_images,
                _ImageCopyState(),
                directory_flags,
                depth=0,
            )
        finally:
            os.close(source_fd)
        try:
            Orchestrator._validate_image_tree_bounded(
                target_images,
                error_type=TaskPublicationError,
                label="copied images",
            )
        except OSError as error:
            raise TaskPublicationError("could not validate cached image target") from error

    @staticmethod
    def _copy_cached_image_directory(
        source_fd: int,
        target_dir: Path,
        state: _ImageCopyState,
        directory_flags: int,
        depth: int,
    ) -> None:
        with os.scandir(source_fd) as entries:
            for entry in entries:
                entry_depth = depth + 1
                Orchestrator._record_image_entry(
                    state,
                    entry_depth,
                    OSError,
                    "cached images",
                )
                source_stat = os.stat(
                    entry.name,
                    dir_fd=source_fd,
                    follow_symlinks=False,
                )
                if stat.S_ISLNK(source_stat.st_mode):
                    raise OSError("cached images contain a symlink")
                target_path = target_dir / entry.name
                if stat.S_ISDIR(source_stat.st_mode):
                    try:
                        target_path.mkdir()
                    except OSError as error:
                        raise TaskPublicationError(
                            "could not create cached image target directory"
                        ) from error
                    child_fd = os.open(
                        entry.name,
                        directory_flags,
                        dir_fd=source_fd,
                    )
                    try:
                        opened_stat = os.fstat(child_fd)
                        if not Orchestrator._same_entry_identity(
                            source_stat,
                            opened_stat,
                        ):
                            raise OSError("cached image directory changed during copy")
                        Orchestrator._copy_cached_image_directory(
                            child_fd,
                            target_path,
                            state,
                            directory_flags,
                            depth=entry_depth,
                        )
                    finally:
                        os.close(child_fd)
                    continue
                if not stat.S_ISREG(source_stat.st_mode):
                    raise OSError("cached images contain a non-regular file")
                Orchestrator._record_image_file(
                    state,
                    source_stat,
                    OSError,
                    "cached images",
                )
                Orchestrator._copy_cached_image_file(
                    source_fd,
                    entry.name,
                    source_stat,
                    target_path,
                    state,
                )

    @staticmethod
    def _copy_cached_image_file(
        source_dir_fd: int,
        source_name: str,
        initial_stat: os.stat_result,
        target_path: Path,
        state: _ImageCopyState,
    ) -> None:
        source_fd = os.open(
            source_name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=source_dir_fd,
        )
        try:
            opened_stat = os.fstat(source_fd)
            if not Orchestrator._same_file_snapshot(initial_stat, opened_stat):
                raise OSError("cached image changed before copy")
            expected_size = initial_stat.st_size
            total_before_file = state.total_bytes - expected_size
            with os.fdopen(source_fd, "rb", closefd=False) as source_file:
                try:
                    target_file = target_path.open("xb")
                except OSError as error:
                    raise TaskPublicationError("could not create cached image target") from error
                with target_file:
                    copied = 0
                    while chunk := source_file.read(_IMAGE_COPY_CHUNK_BYTES):
                        copied += len(chunk)
                        if copied > expected_size:
                            raise OSError("cached image grew during copy")
                        if total_before_file + copied > MAX_CACHED_IMAGE_TOTAL_BYTES:
                            raise OSError(
                                "cached images exceed total byte limit "
                                f"({MAX_CACHED_IMAGE_TOTAL_BYTES})"
                            )
                        try:
                            target_file.write(chunk)
                        except OSError as error:
                            raise TaskPublicationError(
                                "could not write cached image target"
                            ) from error
            finished_stat = os.fstat(source_fd)
            path_stat = os.stat(
                source_name,
                dir_fd=source_dir_fd,
                follow_symlinks=False,
            )
            if copied != expected_size:
                raise OSError("cached image length changed during copy")
            if not Orchestrator._same_file_snapshot(initial_stat, finished_stat):
                raise OSError("cached image identity changed during copy")
            if not Orchestrator._same_file_snapshot(initial_stat, path_stat):
                raise OSError("cached image path changed during copy")
        finally:
            os.close(source_fd)

    @staticmethod
    def _validate_image_tree_bounded(
        images_dir: Path,
        *,
        error_type: type[Exception],
        label: str,
    ) -> None:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(images_dir, directory_flags)
        try:
            if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
                raise error_type(f"{label} root is not a directory")
            Orchestrator._validate_image_directory(
                root_fd,
                _ImageCopyState(),
                directory_flags,
                depth=0,
                error_type=error_type,
                label=label,
            )
        finally:
            os.close(root_fd)

    @staticmethod
    def _validate_image_directory(
        directory_fd: int,
        state: _ImageCopyState,
        directory_flags: int,
        *,
        depth: int,
        error_type: type[Exception],
        label: str,
    ) -> None:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                entry_depth = depth + 1
                Orchestrator._record_image_entry(
                    state,
                    entry_depth,
                    error_type,
                    label,
                )
                entry_stat = os.stat(
                    entry.name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if stat.S_ISLNK(entry_stat.st_mode):
                    raise error_type(f"{label} contain a symlink")
                if stat.S_ISDIR(entry_stat.st_mode):
                    child_fd = os.open(
                        entry.name,
                        directory_flags,
                        dir_fd=directory_fd,
                    )
                    try:
                        opened_stat = os.fstat(child_fd)
                        if not Orchestrator._same_entry_identity(
                            entry_stat,
                            opened_stat,
                        ):
                            raise error_type(f"{label} directory changed during validation")
                        Orchestrator._validate_image_directory(
                            child_fd,
                            state,
                            directory_flags,
                            depth=entry_depth,
                            error_type=error_type,
                            label=label,
                        )
                    finally:
                        os.close(child_fd)
                    continue
                if not stat.S_ISREG(entry_stat.st_mode):
                    raise error_type(f"{label} contain a non-regular file")
                Orchestrator._record_image_file(
                    state,
                    entry_stat,
                    error_type,
                    label,
                )

    @staticmethod
    def _record_image_entry(
        state: _ImageCopyState,
        depth: int,
        error_type: type[Exception],
        label: str,
    ) -> None:
        state.entry_count += 1
        if state.entry_count > MAX_CACHED_IMAGE_ENTRIES:
            raise error_type(f"{label} exceed entry limit ({MAX_CACHED_IMAGE_ENTRIES})")
        if depth > MAX_CACHED_IMAGE_DEPTH:
            raise error_type(f"{label} exceed depth limit ({MAX_CACHED_IMAGE_DEPTH})")

    @staticmethod
    def _record_image_file(
        state: _ImageCopyState,
        file_stat: os.stat_result,
        error_type: type[Exception],
        label: str,
    ) -> None:
        state.file_count += 1
        if state.file_count > MAX_CACHED_IMAGE_FILES:
            raise error_type(f"{label} exceed file limit ({MAX_CACHED_IMAGE_FILES})")
        if file_stat.st_size > MAX_CACHED_IMAGE_FILE_BYTES:
            raise error_type(
                f"{label} contain a file over byte limit ({MAX_CACHED_IMAGE_FILE_BYTES})"
            )
        state.total_bytes += file_stat.st_size
        if state.total_bytes > MAX_CACHED_IMAGE_TOTAL_BYTES:
            raise error_type(f"{label} exceed total byte limit ({MAX_CACHED_IMAGE_TOTAL_BYTES})")

    @staticmethod
    def _same_entry_identity(expected: os.stat_result, actual: os.stat_result) -> bool:
        return (
            expected.st_dev == actual.st_dev
            and expected.st_ino == actual.st_ino
            and expected.st_mode == actual.st_mode
        )

    @staticmethod
    def _same_file_snapshot(expected: os.stat_result, actual: os.stat_result) -> bool:
        return (
            Orchestrator._same_entry_identity(expected, actual)
            and expected.st_size == actual.st_size
            and expected.st_mtime_ns == actual.st_mtime_ns
        )

    def _materialize_cached_task(
        self,
        hit: Task,
        cached: _CachedTaskData,
        *,
        file_path: str,
        accepted_task_id: str,
        batch_id: str | None,
    ) -> dict[str, object]:
        source_dir = Path(self.fs.task_path(hit.id))
        target_dir = Path(self.fs.task_path(accepted_task_id))
        if self.fs.task_identity(accepted_task_id) is not None:
            raise TaskPublicationError("cache publication target already exists")
        staging = self.fs.create_task_staging_capability(accepted_task_id)
        staging_dir = Path(staging.directory_path)
        staging_name = staging.entry_name
        published_identity = staging.task_identity
        published = False
        published_capability: TaskDirectoryCapability | None = None
        now = int(time.time())
        sections: list[Section] = []
        artifacts: list[AIArtifact] = []
        raw_text: dict[str, str] = {}
        ai_text: dict[str, str] = {}
        try:
            staging.verify()
            self._copy_cached_images_bounded(
                source_dir / "images",
                staging_dir / "images",
            )
            staging.verify()
            artifacts_by_section = {artifact.section_id: artifact for artifact in cached.artifacts}
            for source_section in cached.sections:
                section_id = str(uuid.uuid4())
                raw = self._rewrite_cached_image_links(
                    cached.raw_text[source_section.id],
                    source_dir,
                    target_dir,
                )
                interpreted = self._rewrite_cached_image_links(
                    cached.ai_text[source_section.id],
                    source_dir,
                    target_dir,
                )
                raw_name = f"{source_section.seq}.raw.md"
                ai_name = f"{source_section.seq}.ai.md"
                staging.verify()
                self._write_publication_text(staging_dir / raw_name, raw)
                staging.verify()
                self._write_publication_text(staging_dir / ai_name, interpreted)
                staging.verify()
                section = Section(
                    id=section_id,
                    task_id=accepted_task_id,
                    seq=source_section.seq,
                    raw_md_path=str(target_dir / raw_name),
                    sha256=text_sha256(raw),
                    char_count=len(raw),
                    ai_status="COMPLETED",
                    created_at=now,
                )
                source_artifact = artifacts_by_section[source_section.id]
                artifact = AIArtifact(
                    id=str(uuid.uuid4()),
                    section_id=section_id,
                    ai_md_path=str(target_dir / ai_name),
                    ai_md="",
                    tokens_in=source_artifact.tokens_in,
                    tokens_out=source_artifact.tokens_out,
                    cost_usd=source_artifact.cost_usd,
                    retry_count=source_artifact.retry_count,
                    model_name=source_artifact.model_name,
                    created_at=now,
                )
                sections.append(section)
                artifacts.append(artifact)
                raw_text[section_id] = raw
                ai_text[section_id] = interpreted

            merged = self._render_merged(
                accepted_task_id,
                file_path,
                sections,
                raw_text,
                ai_text,
            )
            self._write_publication_text(staging_dir / "merged.md", merged)
            staging.verify()
            staging.close()
            self._publish_staging_directory(
                staging_dir,
                accepted_task_id,
            )
            published = True
            published_capability = self.fs.open_task_capability(accepted_task_id)
            if published_capability.task_identity != published_identity:
                raise TaskPublicationError("cache publication identity changed")
            published_capability.verify()

            task = Task(
                id=accepted_task_id,
                file_path=file_path,
                snapshot_path="",
                file_sha256=hit.file_sha256,
                status="COMPLETED",
                model_tier=hit.model_tier,
                created_at=now,
                updated_at=now,
                batch_id=batch_id,
            )
            self.repo.materialize_cached_task(task, sections, artifacts)
            published_capability.verify()
        except BaseException as primary:
            current = self.repo.get_task(accepted_task_id)
            if current is not None and current.status == "COMPLETED":
                try:
                    self.repo.rollback_cached_materialization(accepted_task_id)
                except BaseException as cleanup_error:
                    try:
                        primary.add_note(
                            f"cached materialization rollback failed: {cleanup_error!r}"
                        )
                    except BaseException:
                        pass
            try:
                if published:
                    self.fs.remove_task_tree(
                        accepted_task_id,
                        expected_identity=published_identity,
                    )
                else:
                    self.fs.remove_staging_tree(
                        staging_name,
                        expected_identity=published_identity,
                    )
            except (OSError, RuntimeError):
                pass
            raise
        finally:
            if published_capability is not None:
                published_capability.close()
            try:
                staging.close()
            except (OSError, RuntimeError):
                pass
            if not published:
                try:
                    self.fs.remove_staging_tree(
                        staging_name,
                        expected_identity=published_identity,
                    )
                except (OSError, RuntimeError):
                    pass

        return {
            "task_id": accepted_task_id,
            "merged_md_path": str(target_dir / "merged.md"),
            "sections": len(sections),
            "cached": True,
            "status": "COMPLETED",
        }

    @staticmethod
    def _write_publication_text(path: Path, content: str) -> None:
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as error:
            raise TaskPublicationError(
                f"could not write cache publication file {path.name}"
            ) from error

    def _publish_staging_directory(
        self,
        staging_dir: Path,
        accepted_task_id: str,
    ) -> None:
        expected_parent = Path(self.fs.tasks_dir())
        if staging_dir.parent != expected_parent:
            raise TaskPublicationError("unsafe cache publication staging path")
        try:
            expected_stat = os.lstat(staging_dir)
        except OSError as error:
            raise TaskPublicationError("could not inspect cache publication staging") from error
        capability: TaskDirectoryCapability | None = None
        try:
            capability = self.fs.open_task_staging_capability(
                staging_dir.name,
                accepted_task_id,
                expected_identity=(expected_stat.st_dev, expected_stat.st_ino),
            )
            self._fsync_directory_tree(capability.task_fd)
            capability.verify()
            self.fs.publish_task_staging(capability)
            self._write_publication_receipt(capability, kind="cache")
            capability.verify()
        except (OSError, UnsafeTaskPathError) as error:
            raise TaskPublicationError("could not publish cached task output") from error
        finally:
            if capability is not None:
                capability.close()

    @staticmethod
    def _remove_snapshot(snapshot_path: str) -> None:
        try:
            Path(snapshot_path).unlink()
        except OSError:
            pass

    @staticmethod
    def _section_title(seq: int, raw: str) -> str:
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or f"节 {seq + 1}"
        return f"节 {seq + 1}"

    def _maybe_progress(
        self,
        task_id: str,
        event_kind: str,
        payload: dict[str, object],
    ) -> None:
        if self.on_progress is None:
            return
        self.on_progress(task_id, event_kind, payload)

    def _has_trusted_section_checkpoint(
        self,
        task_id: str,
        sections: list[Section],
    ) -> bool:
        recovery = self.repo.get_task_recovery(task_id)
        if (
            recovery is None
            or not recovery["sectioning_complete"]
            or recovery["expected_sections"] <= 0
            or recovery["expected_sections"] != len(sections)
            or [section.seq for section in sections] != list(range(len(sections)))
        ):
            return False
        try:
            for section in sections:
                self._read_section_raw(task_id, section)
        except UntrustedTaskDataError:
            return False
        return True

    def _sections_requiring_interpretation(
        self,
        task_id: str,
        sections: list[Section],
    ) -> list[Section]:
        pending: list[Section] = []
        for section in sections:
            artifact = self.repo.get_artifact_by_section(section.id)
            if (
                section.ai_status != "COMPLETED"
                or artifact is None
                or not self._is_valid_resumable_artifact(task_id, section, artifact)
            ):
                pending.append(section)
        return pending

    def resume(self, task_id: str) -> dict[str, object]:
        task = self.repo.get_task(task_id)
        if task is None:
            return {"task_id": task_id, "status": "NOT_FOUND"}

        claim = self._try_claim_resume(task_id)
        if claim is None:
            if self.repo.get_task(task_id) is None:
                return {"task_id": task_id, "status": "NOT_FOUND"}
            return {"task_id": task_id, "status": "BUSY"}
        try:
            current_task = self.repo.get_task(task_id)
            if current_task is None:
                return {"task_id": task_id, "status": "NOT_FOUND"}
            task = current_task
            sections = self.repo.list_sections(task_id)
            validation_session = _CacheValidationSession(valid={}, invalid=set())
            if not self._has_trusted_section_checkpoint(task_id, sections):
                self._update_task_status(task_id, "PARSING", claim=claim)
                self._maybe_progress(task_id, "TASK_STATE", {"status": "PARSING"})
                return self._run_registered_parse(
                    task,
                    validation_session,
                    reset_outputs=True,
                    claim=claim,
                )

            pending = self._sections_requiring_interpretation(task_id, sections)
            if task.status == "COMPLETED" and not pending:
                return {"task_id": task_id, "status": "ALREADY_COMPLETED"}

            try:
                self._update_task_status(task_id, "LLM_RUNNING", claim=claim)
                self._maybe_progress(task_id, "TASK_STATE", {"status": "LLM_RUNNING"})
                for section in pending:
                    self._interpret_section(
                        task_id,
                        section,
                        validation_session,
                        claim=claim,
                    )

                self._update_task_status(task_id, "MERGING", claim=claim)
                self._maybe_progress(task_id, "TASK_STATE", {"status": "MERGING"})
                merged = self._merge(task_id, task.file_path)
                merged_path = self._write_task_text(task_id, "merged.md", merged)
                self._update_task_status(task_id, "COMPLETED", claim=claim)
                self._maybe_progress(task_id, "TASK_STATE", {"status": "COMPLETED"})
                self._remove_snapshot(task.snapshot_path)
                return {
                    "task_id": task_id,
                    "merged_md_path": merged_path,
                    "status": "COMPLETED",
                    "sections": len(sections),
                }
            except UntrustedTaskDataError:
                self._update_task_status(task_id, "PARSING", claim=claim)
                self._maybe_progress(task_id, "TASK_STATE", {"status": "PARSING"})
                return self._run_registered_parse(
                    task,
                    validation_session,
                    reset_outputs=True,
                    claim=claim,
                )
            except Exception as error:
                log.exception("resume_failed task_id=%s file=%s", task_id, task.file_path)
                if not isinstance(error, ResumeClaimLost):
                    try:
                        self._update_task_status(
                            task_id,
                            "FAILED",
                            error_msg=str(error),
                            claim=claim,
                        )
                    except ResumeClaimLost:
                        log.warning("resume_claim_lost_while_failing task_id=%s", task_id)
                    else:
                        self._maybe_progress(
                            task_id,
                            "TASK_STATE",
                            {"status": "FAILED", "error": str(error)},
                        )
                raise
        finally:
            self._release_resume(task_id, claim)

    def status(self, task_id: str) -> dict[str, object]:
        task = self.repo.get_task(task_id)
        if task is None:
            return {"task_id": task_id, "status": "NOT_FOUND"}
        sections = self.repo.list_sections(task_id)
        return {
            "task_id": task_id,
            "status": task.status,
            "sections": len(sections),
            "completed": sum(1 for s in sections if s.ai_status == "COMPLETED"),
            "error_msg": task.error_msg,
        }

    def list_all(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for t in self.repo.list_all_tasks():
            out.append({"task_id": t.id, "status": t.status, "file_path": t.file_path})
        return out

    def _purge_locked(self, task_id: str) -> dict[str, object]:
        task = self.repo.get_task(task_id)
        if task is None:
            return {"task_id": task_id, "purged": False}

        try:
            task_identity = self.fs.task_identity(task_id)
            if task_identity is not None and not self.fs.remove_task_tree(
                task_id,
                expected_identity=task_identity,
            ):
                raise TaskPublicationError("could not quarantine task output for purge")
            if self.fs.task_identity(task_id) is not None:
                raise TaskPublicationError("task output still exists after purge")
        except UnsafeTaskPathError:
            raise
        except OSError as error:
            raise TaskPublicationError("could not purge task output") from error

        if task.snapshot_path:
            try:
                os.unlink(task.snapshot_path)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise TaskPublicationError("could not purge task snapshot") from error
            if os.path.lexists(task.snapshot_path):
                raise TaskPublicationError("task snapshot still exists after purge")

        self.repo.delete_task(task_id)
        if self.repo.get_task(task_id) is not None:
            raise TaskPublicationError("task database row still exists after purge")
        if self.fs.task_identity(task_id) is not None or (
            task.snapshot_path and os.path.lexists(task.snapshot_path)
        ):
            raise TaskPublicationError("task files reappeared during purge")
        return {"task_id": task_id, "purged": True}

    def purge(self, task_id: str) -> dict[str, object]:
        lock_fd = self._try_open_resume_lock(task_id, blocking=True)
        if lock_fd is None:
            raise RuntimeError("blocking task lock acquisition unexpectedly failed")
        try:
            return self._purge_locked(task_id)
        finally:
            self._close_resume_lock(lock_fd)

    def try_purge(self, task_id: str) -> dict[str, object] | None:
        lock_fd = self._try_open_resume_lock(task_id)
        if lock_fd is None:
            return None
        try:
            return self._purge_locked(task_id)
        finally:
            self._close_resume_lock(lock_fd)
