import errno
import fcntl
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_TEXTBOOK_EXTENSIONS = {".pdf", ".doc", ".docx"}
TEXTBOOK_DIRECTORY_NAME = "教材原文件"
IMPORT_JOURNAL_SUFFIX = ".import-journal"
MAX_JOURNAL_BYTES = 16_384


class SourceImportInputError(ValueError):
    pass


class CourseStorageError(Exception):
    pass


class AtomicImportUnsupportedError(CourseStorageError):
    pass


class CourseStorageChangedError(CourseStorageError):
    pass


@dataclass(frozen=True)
class ImportedTextbook:
    title: str
    source_path: Path
    stored_path: Path


@dataclass
class _PublishedRecord:
    final_name: str
    temporary_name: str
    journal_name: str
    journal_fd: int
    device: int
    inode: int
    published: bool


def _resolve_source(source_path: Path) -> Path:
    if not source_path.is_absolute():
        raise SourceImportInputError("source_path must be an absolute path")
    try:
        resolved = source_path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SourceImportInputError("source_path must be an existing file") from exc
    if not resolved.is_file():
        raise SourceImportInputError("source_path must be an existing file")
    if resolved.suffix.lower() not in SUPPORTED_TEXTBOOK_EXTENSIONS:
        raise SourceImportInputError("source_path has an unsupported extension")
    return resolved


def _resolve_course_root(course_root: Path) -> Path:
    if not course_root.is_absolute():
        raise SourceImportInputError("course_root must be an absolute path")
    try:
        resolved = course_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SourceImportInputError("course_root must be an existing directory") from exc
    if not resolved.is_dir():
        raise SourceImportInputError("course_root must be an existing directory")
    return resolved


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _raise_storage_error(error: OSError, *, hardlink: bool = False) -> None:
    unsupported = {
        errno.EXDEV,
        errno.EPERM,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EOPNOTSUPP,
    }
    if hardlink and error.errno in unsupported:
        raise AtomicImportUnsupportedError(
            "course storage does not support atomic imports"
        ) from error
    raise CourseStorageError("course storage could not complete import") from error


class TextbookImportBatch:
    def __init__(
        self,
        course_root: Path,
        registered_source_paths: set[str] | None = None,
    ):
        self.course_root = _resolve_course_root(course_root)
        self.target_path = self.course_root / TEXTBOOK_DIRECTORY_NAME
        self._directory_fd = self._open_directory()
        self._directory_stat = os.fstat(self._directory_fd)
        self._records: list[_PublishedRecord] = []
        self._committed = False
        self._closed = False
        if registered_source_paths is not None:
            self._recover_journals(registered_source_paths)

    def _open_directory(self) -> int:
        flags = _directory_open_flags()
        try:
            root_fd = os.open(self.course_root, flags)
        except OSError as exc:
            _raise_storage_error(exc)
        try:
            try:
                os.mkdir(TEXTBOOK_DIRECTORY_NAME, dir_fd=root_fd)
            except FileExistsError:
                pass
            try:
                return os.open(TEXTBOOK_DIRECTORY_NAME, flags, dir_fd=root_fd)
            except OSError as exc:
                _raise_storage_error(exc)
        finally:
            os.close(root_fd)

    @property
    def directory_fd(self) -> int:
        return self._directory_fd

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if not self._committed:
                self.rollback(exc_value)
        finally:
            self._close()

    def _close(self) -> None:
        if self._closed:
            return
        for record in self._records:
            try:
                os.close(record.journal_fd)
            except OSError:
                pass
        os.close(self._directory_fd)
        self._closed = True

    def verify_path_identity(self) -> None:
        try:
            current = os.lstat(self.target_path)
        except OSError as exc:
            raise CourseStorageChangedError("course storage changed during import") from exc
        if not stat.S_ISDIR(current.st_mode) or not _same_file(self._directory_stat, current):
            raise CourseStorageChangedError("course storage changed during import")

    def _open_temporary(self, source_name: str, mode: int) -> tuple[str, int]:
        while True:
            temporary_name = f".{source_name}.{uuid.uuid4().hex}.tmp"
            descriptor = None
            try:
                descriptor = os.open(
                    temporary_name,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    mode,
                    dir_fd=self._directory_fd,
                )
                os.fchmod(descriptor, mode)
                return temporary_name, descriptor
            except FileExistsError:
                continue
            except OSError as exc:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    self._unlink(temporary_name)
                _raise_storage_error(exc)

    def _unlink(self, name: str, original_error: BaseException | None = None) -> bool:
        try:
            os.unlink(name, dir_fd=self._directory_fd)
            return True
        except FileNotFoundError:
            return True
        except OSError as cleanup_error:
            if original_error is not None:
                original_error.add_note(f"file cleanup failed: {cleanup_error!r}")
            return False

    def _fsync_directory(self) -> None:
        try:
            os.fsync(self._directory_fd)
        except OSError as exc:
            _raise_storage_error(exc)

    def _journal_payload(self, record: _PublishedRecord) -> bytes:
        return json.dumps(
            {
                "version": 1,
                "final_name": record.final_name,
                "temporary_name": record.temporary_name,
                "device": record.device,
                "inode": record.inode,
                "published": record.published,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _write_all(self, descriptor: int, content: bytes) -> None:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError(errno.EIO, "journal write returned zero bytes")
            view = view[written:]

    def _create_journal(
        self,
        final_name: str,
        temporary_name: str,
        temporary_stat: os.stat_result,
    ) -> _PublishedRecord:
        journal_name = f".{uuid.uuid4().hex}{IMPORT_JOURNAL_SUFFIX}"
        descriptor = None
        try:
            descriptor = os.open(
                journal_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._directory_fd,
            )
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            record = _PublishedRecord(
                final_name=final_name,
                temporary_name=temporary_name,
                journal_name=journal_name,
                journal_fd=descriptor,
                device=temporary_stat.st_dev,
                inode=temporary_stat.st_ino,
                published=False,
            )
            self._write_all(descriptor, self._journal_payload(record))
            os.fsync(descriptor)
            self._fsync_directory()
            return record
        except OSError as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            self._unlink(journal_name)
            _raise_storage_error(exc)

    def _mark_journal_published(self, record: _PublishedRecord) -> None:
        record.published = True

    def _remove_journal(
        self,
        record: _PublishedRecord,
        original_error: BaseException | None = None,
    ) -> bool:
        try:
            os.close(record.journal_fd)
        except OSError as cleanup_error:
            if original_error is not None:
                original_error.add_note(f"journal close failed: {cleanup_error!r}")
            return False
        try:
            self._fsync_directory()
        except CourseStorageError as cleanup_error:
            if original_error is not None:
                original_error.add_note(f"journal cleanup failed: {cleanup_error!r}")
            return False
        return self._unlink(record.journal_name, original_error)

    def _publish(
        self,
        temporary_name: str,
        temporary_stat: os.stat_result,
        source_name: str,
    ) -> _PublishedRecord:
        source = Path(source_name).name
        stem = Path(source).stem
        suffix = Path(source).suffix
        index = 1
        while True:
            candidate = source if index == 1 else f"{stem}-{index}{suffix}"
            record = self._create_journal(candidate, temporary_name, temporary_stat)
            self._records.append(record)
            try:
                os.link(
                    temporary_name,
                    candidate,
                    src_dir_fd=self._directory_fd,
                    dst_dir_fd=self._directory_fd,
                    follow_symlinks=False,
                )
                self._mark_journal_published(record)
                return record
            except FileExistsError as exc:
                if not self._remove_journal(record):
                    raise CourseStorageError(
                        "course storage could not complete import"
                    ) from exc
                self._records.remove(record)
                index += 1
            except OSError as exc:
                _raise_storage_error(exc, hardlink=True)

    def import_file(self, source_path: Path) -> ImportedTextbook:
        resolved_source = _resolve_source(source_path)
        try:
            source_mode = stat.S_IMODE(resolved_source.stat().st_mode)
        except OSError as exc:
            raise SourceImportInputError("source_path must be readable") from exc

        temporary_name = None
        record = None
        descriptor = None
        try:
            temporary_name, descriptor = self._open_temporary(
                resolved_source.name,
                source_mode,
            )
            temporary_stat = os.fstat(descriptor)
            target = os.fdopen(descriptor, "wb")
            descriptor = None
            with target:
                with resolved_source.open("rb") as source_file:
                    shutil.copyfileobj(source_file, target)
                    target.flush()
                    os.fsync(target.fileno())
            record = self._publish(temporary_name, temporary_stat, resolved_source.name)
            if not self._unlink(temporary_name):
                raise CourseStorageError("course storage could not complete import")
            temporary_name = None
        except SourceImportInputError:
            raise
        except CourseStorageError as error:
            self.rollback(error)
            if record is None and temporary_name is not None:
                self._unlink(temporary_name, error)
            raise
        except OSError as exc:
            error = CourseStorageError("course storage could not complete import")
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as cleanup_error:
                    error.add_note(f"temporary close failed: {cleanup_error!r}")
            self.rollback(error)
            if record is None and temporary_name is not None:
                self._unlink(temporary_name, error)
            raise error from exc

        return ImportedTextbook(
            title=resolved_source.stem,
            source_path=resolved_source,
            stored_path=self.target_path / record.final_name,
        )

    def rollback(self, original_error: BaseException | None = None) -> None:
        for record in reversed(self._records):
            final_state = self._identity_state(record.final_name, record)
            temporary_state = self._identity_state(record.temporary_name, record)
            final_removed = final_state != "matching" or self._unlink(
                record.final_name,
                original_error,
            )
            temporary_removed = temporary_state != "matching" or self._unlink(
                record.temporary_name,
                original_error,
            )
            if final_removed and temporary_removed:
                self._remove_journal(record, original_error)
            else:
                try:
                    os.close(record.journal_fd)
                except OSError as cleanup_error:
                    if original_error is not None:
                        original_error.add_note(f"journal close failed: {cleanup_error!r}")
        self._records.clear()

    def commit(self) -> None:
        cleanup_error = CourseStorageError("course storage could not complete import")
        for record in self._records:
            self._remove_journal(record, cleanup_error)
        self._committed = True
        self._records.clear()

    def _valid_name(self, name: object) -> bool:
        return (
            isinstance(name, str)
            and name not in {"", ".", ".."}
            and Path(name).name == name
            and "/" not in name
            and "\\" not in name
        )

    def _load_recovery_record(self, journal_name: str) -> _PublishedRecord | None:
        descriptor = None
        try:
            descriptor = os.open(
                journal_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._directory_fd,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                return None
            content = os.read(descriptor, MAX_JOURNAL_BYTES + 1)
            if len(content) > MAX_JOURNAL_BYTES:
                raise ValueError("journal is too large")
            payload = json.loads(content)
            expected_keys = {
                "version",
                "final_name",
                "temporary_name",
                "device",
                "inode",
                "published",
            }
            if not isinstance(payload, dict) or set(payload) != expected_keys:
                raise ValueError("journal shape is invalid")
            if payload["version"] != 1:
                raise ValueError("journal version is invalid")
            if not self._valid_name(payload["final_name"]) or not self._valid_name(
                payload["temporary_name"]
            ):
                raise ValueError("journal path is invalid")
            if type(payload["device"]) is not int or type(payload["inode"]) is not int:
                raise ValueError("journal identity is invalid")
            if type(payload["published"]) is not bool:
                raise ValueError("journal state is invalid")
            return _PublishedRecord(
                final_name=payload["final_name"],
                temporary_name=payload["temporary_name"],
                journal_name=journal_name,
                journal_fd=descriptor,
                device=payload["device"],
                inode=payload["inode"],
                published=payload["published"],
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise CourseStorageError("course storage could not complete import") from exc

    def _identity_state(self, name: str, record: _PublishedRecord) -> str:
        try:
            current = os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return "missing"
        except OSError as exc:
            _raise_storage_error(exc)
        if (current.st_dev, current.st_ino) == (record.device, record.inode):
            return "matching"
        return "different"

    def _recover_journals(self, registered_source_paths: set[str]) -> None:
        try:
            journal_names = [
                name
                for name in os.listdir(self._directory_fd)
                if name.endswith(IMPORT_JOURNAL_SUFFIX)
            ]
        except OSError as exc:
            _raise_storage_error(exc)
        for journal_name in journal_names:
            record = self._load_recovery_record(journal_name)
            if record is None:
                continue
            stored_path = str(self.target_path / record.final_name)
            final_state = self._identity_state(record.final_name, record)
            temporary_state = self._identity_state(record.temporary_name, record)
            if stored_path in registered_source_paths:
                if temporary_state == "matching" and not self._unlink(record.temporary_name):
                    os.close(record.journal_fd)
                    raise CourseStorageError("course storage could not complete import")
                if not self._remove_journal(record):
                    raise CourseStorageError("course storage could not complete import")
                continue
            if final_state == "different" and record.published:
                os.close(record.journal_fd)
                raise CourseStorageError("course storage could not complete import")
            cleanup_succeeded = True
            if final_state == "matching" and not self._unlink(record.final_name):
                cleanup_succeeded = False
            if temporary_state == "matching" and not self._unlink(record.temporary_name):
                cleanup_succeeded = False
            if not cleanup_succeeded:
                os.close(record.journal_fd)
                raise CourseStorageError("course storage could not complete import")
            if not self._remove_journal(record):
                raise CourseStorageError("course storage could not complete import")


def import_textbook_file(course_root: Path, source_path: Path) -> ImportedTextbook:
    resolved_source = _resolve_source(source_path)
    with TextbookImportBatch(course_root) as batch:
        imported = batch.import_file(resolved_source)
        batch.verify_path_identity()
        batch.commit()
        return imported
