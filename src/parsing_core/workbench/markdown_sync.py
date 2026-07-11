import fcntl
import json
import os
import re
import tempfile
import threading
import unicodedata
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from pathlib import Path

from parsing_core.workbench.models import Card, Chapter, NoteBlock, Source
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.topic_task_package import allocate_source_display_titles

MERMAID_FENCE_RE = re.compile(r"^\s*```mermaid\s*\n(.*?)```\s*$", re.DOTALL | re.IGNORECASE)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
MAX_NAME_BYTES = 180
JOURNAL_NAME = ".pdf2md-bundle-journal.json"
LOCK_NAME = ".pdf2md-bundle.lock"
OWNER_NAME = ".pdf2md-owner"
_BUNDLE_LOCKS: dict[tuple[int, int], threading.Lock] = {}
_BUNDLE_LOCKS_GUARD = threading.Lock()


@contextmanager
def _owned_fdopen(fd: int, *args, **kwargs):
    try:
        handle = os.fdopen(fd, *args, **kwargs)
    except BaseException:
        os.close(fd)
        raise
    with handle:
        yield handle


SAFE_COMPONENT_RE = re.compile(r"^[^/\\\x00]+$")


def safe_name(value: str, fallback: str = "untitled") -> str:
    value = unicodedata.normalize("NFC", value)
    value = CONTROL_RE.sub("", value).replace("/", "-").replace("\\", "-")
    value = value.strip().rstrip(". ")
    while value.startswith("."):
        value = value[1:]
    value = value.strip().rstrip(". ") or fallback
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_NAME_BYTES:
        return value
    suffix = "-" + __import__("hashlib").sha256(encoded).hexdigest()[:12]
    budget = MAX_NAME_BYTES - len(suffix)
    shortened = value
    while len(shortened.encode("utf-8")) > budget:
        shortened = shortened[:-1]
    return shortened.rstrip(". ") + suffix


def _normalized_safe_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def allocate_safe_source_names(sources: list[tuple[str, str]]) -> dict[str, str]:
    display_titles = allocate_source_display_titles(sources)
    safe_bases = {source_id: safe_name(display_titles[source_id]) for source_id, _ in sources}
    reserved = {_normalized_safe_name(value) for value in safe_bases.values()}
    assigned: set[str] = set()
    result = {}
    for source_id, _ in sources:
        base = safe_bases[source_id]
        normalized = _normalized_safe_name(base)
        if normalized not in assigned:
            candidate = base
        else:
            candidate = ""
            for suffix in range(2, 10_001):
                possible = safe_name(f"{base}（{suffix}）")
                normalized_possible = _normalized_safe_name(possible)
                if normalized_possible not in reserved and normalized_possible not in assigned:
                    candidate = possible
                    break
            if not candidate:
                raise ValueError("unable to allocate unique safe source directory")
        assigned.add(_normalized_safe_name(candidate))
        result[source_id] = candidate
    return result


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".pdf2md-tmp", dir=path.parent)
    backup_name: str | None = None
    replaced = False
    try:
        with _owned_fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temp_name, path.stat().st_mode & 0o777)
            backup_fd, backup_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".pdf2md-backup", dir=path.parent
            )
            os.close(backup_fd)
            os.unlink(backup_name)
            os.link(path, backup_name)
        else:
            os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        replaced = True
        _fsync_directory(path.parent)
    except Exception:
        backup_restored = False
        if replaced:
            try:
                if backup_name is not None and Path(backup_name).exists():
                    os.replace(backup_name, path)
                    backup_restored = True
                else:
                    path.unlink(missing_ok=True)
                try:
                    _fsync_directory(path.parent)
                except OSError:
                    pass
            except OSError:
                pass
        _unlink_if_generated(temp_name, ".pdf2md-tmp")
        if backup_name is not None and (not replaced or backup_restored):
            _unlink_if_generated(backup_name, ".pdf2md-backup")
        raise
    if backup_name is not None:
        try:
            os.unlink(backup_name)
            _fsync_directory(path.parent)
        except OSError as exc:
            raise OSError("atomic write committed but backup cleanup failed") from exc


def redact_sensitive_text(value: str) -> str:
    patterns = (
        r"file://[^\s<>()]+",
        r"(?<![\w:/.])/(?!/)(?:[^\s/<>()[\]{}'\"`]+/)*[^\s/<>()[\]{}'\"`]+",
        r"[A-Za-z]:\\[^\s<>()]+",
        r"\bsk-[A-Za-z0-9_-]{12,}\b",
        r"(?i)\b(?:api[_-]?key|access[_-]?token|secret)\s*[:=]\s*[^\s]+",
    )
    for pattern in patterns:
        value = re.sub(pattern, "[REDACTED]", value)
    return value


def open_secure_directory(root: Path, parts: list[str], *, create: bool = True) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        fd = os.open(root, flags)
        stack.callback(os.close, fd)
        root_identity = os.fstat(fd).st_dev, os.fstat(fd).st_ino
        for part in parts:
            if not SAFE_COMPONENT_RE.fullmatch(part) or part in {".", ".."}:
                raise ValueError("unsafe directory component")
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            stack.callback(os.close, child)
            fd = child
        verify_fd = os.open(root, flags)
        stack.callback(os.close, verify_fd)
        if (os.fstat(verify_fd).st_dev, os.fstat(verify_fd).st_ino) != root_identity:
            raise OSError("course root identity changed")
        return os.dup(fd)


def _write_fd_file(dir_fd: int, name: str, content: str) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
    try:
        with _owned_fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        _unlink_at(dir_fd, name)
        raise


def _unlink_at(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def _validate_journal_name(name: str, suffix: str) -> None:
    if Path(name).name != name or not name.startswith(".pdf2md-") or not name.endswith(suffix):
        raise ValueError("unsafe bundle journal entry")


def _write_journal(dir_fd: int, journal: dict) -> None:
    temp = f".pdf2md-{os.urandom(8).hex()}.journal-tmp"
    _write_fd_file(dir_fd, temp, json.dumps(journal, ensure_ascii=False, sort_keys=True))
    os.replace(temp, JOURNAL_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    os.fsync(dir_fd)


def recover_atomic_bundle(dir_fd: int) -> None:
    try:
        fd = os.open(JOURNAL_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    with _owned_fdopen(fd, encoding="utf-8") as handle:
        journal = json.load(handle)
    if journal.get("phase") not in {"PREPARED", "COMMITTED"}:
        raise ValueError("invalid bundle journal phase")
    entries = journal.get("entries")
    if not isinstance(entries, list):
        raise ValueError("invalid bundle journal")
    for entry in entries:
        target, temp, backup = entry["target"], entry["temp"], entry.get("backup")
        if Path(target).name != target:
            raise ValueError("unsafe bundle target")
        _validate_journal_name(temp, ".bundle-tmp")
        if backup is not None:
            _validate_journal_name(backup, ".bundle-backup")
        if journal["phase"] == "PREPARED":
            if backup is not None:
                try:
                    os.replace(backup, target, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
            elif not entry["existed"]:
                _unlink_at(dir_fd, target)
        _unlink_at(dir_fd, temp)
        if backup is not None:
            _unlink_at(dir_fd, backup)
    _unlink_at(dir_fd, JOURNAL_NAME)
    os.fsync(dir_fd)


def atomic_write_bundle_fd(
    dir_fd: int,
    contents: dict[str, str],
    *,
    fence: Callable[[], object] | None = None,
) -> None:
    stat = os.fstat(dir_fd)
    identity = (stat.st_dev, stat.st_ino)
    with _BUNDLE_LOCKS_GUARD:
        thread_lock = _BUNDLE_LOCKS.setdefault(identity, threading.Lock())
    thread_lock.acquire()
    try:
        lock_fd = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if fence is not None:
                fence()
            _atomic_write_bundle_locked(dir_fd, contents, fence=fence)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
    finally:
        thread_lock.release()


def _atomic_write_bundle_locked(
    dir_fd: int,
    contents: dict[str, str],
    *,
    fence: Callable[[], object] | None,
) -> None:
    recover_atomic_bundle(dir_fd)
    entries = []
    try:
        for target, content in contents.items():
            if Path(target).name != target:
                raise ValueError("bundle targets must be basenames")
            token = os.urandom(8).hex()
            temp = f".pdf2md-{token}.bundle-tmp"
            backup = f".pdf2md-{token}.bundle-backup"
            _write_fd_file(dir_fd, temp, content)
            existed = True
            try:
                os.link(
                    target,
                    backup,
                    src_dir_fd=dir_fd,
                    dst_dir_fd=dir_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                existed = False
                backup = None
            entries.append({"target": target, "temp": temp, "backup": backup, "existed": existed})
        journal = {"phase": "PREPARED", "entries": entries}
        _write_journal(dir_fd, journal)
        if fence is not None:
            fence()
        for entry in entries:
            os.replace(entry["temp"], entry["target"], src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
        journal["phase"] = "COMMITTED"
        _write_journal(dir_fd, journal)
        recover_atomic_bundle(dir_fd)
    except BaseException:
        try:
            recover_atomic_bundle(dir_fd)
        finally:
            for entry in entries:
                _unlink_at(dir_fd, entry["temp"])
                if entry["backup"] is not None:
                    _unlink_at(dir_fd, entry["backup"])
        raise


def atomic_write_bundle(
    directory: Path,
    contents: dict[str, str],
    *,
    fence: Callable[[], object] | None = None,
) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        atomic_write_bundle_fd(fd, contents, fence=fence)
    finally:
        os.close(fd)


def _fsync_directory(directory: Path) -> None:
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _unlink_if_generated(name: str, suffix: str) -> None:
    path = Path(name)
    if path.name.startswith(".") and path.name.endswith(suffix):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def textbook_dir(repo: WorkbenchRepository, source: Source) -> Path:
    course = repo.get_course(source.course_id)
    if course is None:
        raise ValueError("course not found")
    sources = repo.list_sources(source.course_id)
    names = allocate_safe_source_names([(item.id, item.title) for item in sources])
    return Path(course.root_dir) / "教材" / names[source.id]


def _first_line_regular_file(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    with path.open(encoding="utf-8", errors="ignore") as handle:
        return handle.readline().rstrip("\r\n")


def _owner_value(entity_type: str, entity_id: str) -> str:
    return f"{entity_type}:{entity_id}"


def _has_owner(path: Path, entity_type: str, entity_id: str) -> bool:
    return _first_line_regular_file(path / OWNER_NAME) == _owner_value(entity_type, entity_id)


def ensure_directory_owner(
    dir_fd: int,
    entity_type: str,
    entity_id: str,
    *,
    allow_create_or_replace: bool,
) -> None:
    expected = _owner_value(entity_type, entity_id)
    try:
        fd = os.open(OWNER_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        current = None
    else:
        with _owned_fdopen(fd, encoding="utf-8") as handle:
            current = handle.readline().rstrip("\r\n")
    if current == expected:
        return
    if not allow_create_or_replace:
        raise FileExistsError("directory ownership marker does not match")
    temp = f".pdf2md-{os.urandom(8).hex()}.owner-tmp"
    _write_fd_file(dir_fd, temp, expected + "\n")
    os.replace(temp, OWNER_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    os.fsync(dir_fd)


def _directory_with_marker(root: Path, marker: str) -> Path | None:
    if not root.exists():
        return None
    matches = []
    levels = 2 if marker.startswith("<!-- chapter-id:") else 1
    candidates = [root]
    for _ in range(levels):
        next_candidates = []
        for directory in candidates:
            for child in directory.iterdir():
                if child.is_symlink():
                    raise OSError("symlink directory rejected")
                if child.is_dir():
                    next_candidates.append(child)
        candidates = next_candidates
    marker_file = "intensive-note.md" if levels == 2 else "topic-map.md"
    for directory in candidates:
        if _first_line_regular_file(directory / marker_file) == marker:
            matches.append(directory)
    unique = list(dict.fromkeys(matches))
    if len(unique) > 1:
        raise ValueError("multiple generated directories contain the same marker")
    return unique[0] if unique else None


def migrate_generated_directory(
    root: Path,
    target: Path,
    marker: str,
    legacy: Path | None = None,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> Path:
    current = _directory_with_marker(root, marker)
    if current is None and legacy is not None and legacy.exists() and not target.exists():
        current = legacy
    if current is None and target.exists():
        if entity_type and entity_id and _has_owner(target, entity_type, entity_id):
            return target
        raise FileExistsError(f"target directory already exists: {target.name}")
    if current is not None and current != target:
        if target.exists():
            raise FileExistsError(f"target directory already exists: {target.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        with ExitStack() as stack:
            source_parent_fd = os.open(current.parent, flags)
            stack.callback(os.close, source_parent_fd)
            target_parent_fd = os.open(target.parent, flags)
            stack.callback(os.close, target_parent_fd)
            os.replace(
                current.name,
                target.name,
                src_dir_fd=source_parent_fd,
                dst_dir_fd=target_parent_fd,
            )
    return target


def sync_chapter_markdown(repo: WorkbenchRepository, chapter_id: str) -> dict[str, str]:
    with ExitStack() as stack:
        return _sync_chapter_markdown(repo, chapter_id, stack)


def _sync_chapter_markdown(
    repo: WorkbenchRepository, chapter_id: str, stack: ExitStack
) -> dict[str, str]:
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise ValueError("chapter not found")
    source = repo.get_source(chapter.source_id)
    course = repo.get_course(chapter.course_id)
    if source is None or course is None:
        raise ValueError("chapter dependencies not found")
    course_root = Path(course.root_dir)
    course_root.mkdir(parents=True, exist_ok=True)
    if course_root.is_symlink():
        raise OSError("course root symlink rejected")
    source_root = textbook_dir(repo, source)
    chapter_name = f"{chapter.seq + 1:02d}-{safe_name(chapter.title)}"
    target = source_root / chapter_name
    legacy = Path(course.root_dir) / chapter_name
    textbooks_fd = open_secure_directory(course_root, ["教材"])
    stack.callback(os.close, textbooks_fd)
    source_fd = open_secure_directory(course_root, ["教材", source_root.name])
    stack.callback(os.close, source_fd)
    target_existed = target.exists()
    chapter_dir = migrate_generated_directory(
        course_root / "教材",
        target,
        f"<!-- chapter-id: {chapter.id} -->",
        legacy,
        entity_type="chapter",
        entity_id=chapter.id,
    )
    relative = chapter_dir.relative_to(course_root)
    chapter_fd = open_secure_directory(course_root, list(relative.parts))
    stack.callback(os.close, chapter_fd)
    formal_owner = _first_line_regular_file(chapter_dir / "intensive-note.md") == (
        f"<!-- chapter-id: {chapter.id} -->"
    )
    ensure_directory_owner(
        chapter_fd,
        "chapter",
        chapter.id,
        allow_create_or_replace=not target_existed or formal_owner,
    )
    attachments_fd = open_secure_directory(course_root, [*relative.parts, "attachments"])
    stack.callback(os.close, attachments_fd)
    runs_fd = open_secure_directory(course_root, [*relative.parts, "runs"])
    stack.callback(os.close, runs_fd)

    source_path = chapter_dir / "source.md"
    source_md_path = Path(chapter.source_md_path)
    source_content = source_md_path.read_text(encoding="utf-8") if source_md_path.exists() else ""
    note_path = chapter_dir / "intensive-note.md"
    cards_path = chapter_dir / "cards.md"
    atomic_write_bundle_fd(
        chapter_fd,
        {
            "source.md": source_content,
            "intensive-note.md": _render_note(chapter, repo.list_note_blocks(chapter.id)),
            "cards.md": _render_cards(chapter, repo.list_cards_by_chapter(chapter.id)),
        },
    )
    run_contents = {}
    for run in repo.list_runs(chapter.id):
        run_contents[f"{safe_name(run.round_key)}.md"] = "\n".join(
            [
                f"# {run.round_key}",
                "",
                f"状态：{run.status}",
                f"过期：{'是' if run.stale else '否'}",
                f"执行器：{run.executor}",
                "",
                "## 输出",
                "",
                redact_sensitive_text(run.output),
                "",
            ]
        )
    if run_contents:
        atomic_write_bundle_fd(runs_fd, run_contents)
    return {"source": str(source_path), "note": str(note_path), "cards": str(cards_path)}


def _render_note(chapter: Chapter, blocks: list[NoteBlock]) -> str:
    lines = [f"<!-- chapter-id: {chapter.id} -->", f"# {chapter.title}", ""]
    for block in blocks:
        lines.extend([f"## {block.title}", ""])
        if block.kind.endswith("_mermaid"):
            lines.extend(["```mermaid", _pure_mermaid(block.body), "```", ""])
        else:
            lines.extend([block.body, ""])
    return "\n".join(lines)


def _pure_mermaid(body: str) -> str:
    match = MERMAID_FENCE_RE.match(body)
    return (match.group(1) if match else body).strip()


def _render_cards(chapter: Chapter, cards: list[Card]) -> str:
    lines = [f"# {chapter.title} 写作卡片", ""]
    for card in cards:
        lines.extend(
            [
                f"## {card.title}",
                "",
                f"类型：{card.kind}",
                f"收藏：{'是' if card.favorite else '否'}",
                "",
                card.body,
                "",
            ]
        )
    return "\n".join(lines)
