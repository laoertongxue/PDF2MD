import multiprocessing
import os
import stat
import traceback
from pathlib import Path

import pytest

import parsing_core.storage.fs_layout as fs_layout_module
from parsing_core.storage.fs_layout import FsLayout


def _fs_layout_persistent_eintr_worker(
    base_dir: str,
    operation: str,
    results: object,
) -> None:
    def interrupt_forever(*_args: object, **_kwargs: object) -> object:
        raise InterruptedError

    try:
        if operation == "open":
            Path(base_dir).mkdir(mode=0o700)
        setattr(fs_layout_module.os, operation, interrupt_forever)
        FsLayout(base_dir=base_dir)
    except fs_layout_module.UnsafeTaskPathError as exc:
        results.put(str(exc))  # type: ignore[attr-defined]
    except BaseException:
        results.put(traceback.format_exc())  # type: ignore[attr-defined]
    else:
        results.put("unexpected success")  # type: ignore[attr-defined]


def test_task_dir_pattern(tmp_path: Path):
    fs = FsLayout(base_dir=str(tmp_path))
    d = fs.task_dir("t1")
    assert d == str(tmp_path / "tasks" / "t1")
    assert Path(d).exists()
    assert not (tmp_path / "t1").exists()


def test_layout_creates_explicit_private_tasks_hierarchy(tmp_path: Path):
    base = tmp_path / "app-data"

    fs = FsLayout(base_dir=str(base))
    images = Path(fs.images_dir("task-1"))

    assert images == base / "tasks" / "task-1" / "images"
    for directory in (base, base / "tasks", images.parent, images):
        assert directory.stat(follow_symlinks=False).st_uid == os.getuid()
        assert stat.S_IMODE(directory.stat(follow_symlinks=False).st_mode) == 0o700
    assert not (base / "task-1").exists()


def test_section_raw_path(tmp_path: Path):
    fs = FsLayout(base_dir=str(tmp_path))
    p = fs.section_raw_path("t1", 0)
    assert p.endswith("tasks/t1/0.raw.md")


def test_section_ai_path(tmp_path: Path):
    fs = FsLayout(base_dir=str(tmp_path))
    p = fs.section_ai_path("t1", 0)
    assert p.endswith("tasks/t1/0.ai.md")


def test_merged_path(tmp_path: Path):
    fs = FsLayout(base_dir=str(tmp_path))
    p = fs.merged_path("t1")
    assert p.endswith("tasks/t1/merged.md")


def test_default_base_uses_appdata(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    fs = FsLayout()
    assert str(tmp_path) in fs.base_dir


def test_new_base_directory_is_private(tmp_path: Path):
    base = tmp_path / "app-data"

    FsLayout(base_dir=str(base))

    assert stat.S_IMODE(base.stat().st_mode) == 0o700
    assert stat.S_IMODE((base / "tasks").stat().st_mode) == 0o700


def test_new_base_directory_is_0700_under_restrictive_umask(tmp_path: Path):
    base = tmp_path / "app-data"
    previous_umask = os.umask(0o777)
    try:
        FsLayout(base_dir=str(base))
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(base.stat().st_mode) == 0o700
    assert stat.S_IMODE((base / "tasks").stat().st_mode) == 0o700


@pytest.mark.parametrize("creation_umask", [0o022, 0o777])
def test_multilevel_new_base_chain_is_private_under_any_umask(
    tmp_path: Path,
    creation_umask: int,
):
    base = tmp_path / "level-one" / "level-two" / "app-data"
    previous_umask = os.umask(creation_umask)
    try:
        FsLayout(base_dir=str(base))
    finally:
        os.umask(previous_umask)

    for directory in (base.parent.parent, base.parent, base):
        directory_stat = directory.stat(follow_symlinks=False)
        assert directory_stat.st_uid == os.getuid()
        assert stat.S_ISDIR(directory_stat.st_mode)
        assert stat.S_IMODE(directory_stat.st_mode) == 0o700


def test_base_chain_rejects_symlinked_existing_parent(tmp_path: Path):
    anchor = tmp_path / "anchor"
    outside = tmp_path / "outside"
    anchor.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    (anchor / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe base"):
        FsLayout(base_dir=str(anchor / "linked" / "app-data"))

    assert not (outside / "app-data").exists()


def test_base_chain_rejects_group_or_world_writable_existing_anchor(tmp_path: Path):
    anchor = tmp_path / "anchor"
    anchor.mkdir(mode=0o700)
    anchor.chmod(0o777)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe base"):
        FsLayout(base_dir=str(anchor / "managed" / "app-data"))

    assert not (anchor / "managed").exists()


def test_task_dir_rejects_runtime_ancestor_symlink_with_same_base_inode(
    tmp_path: Path,
):
    anchor = tmp_path / "anchor"
    anchor.mkdir(mode=0o700)
    base = anchor / "managed" / "app-data"
    fs = FsLayout(base_dir=str(base))
    displaced = tmp_path / "anchor-original"
    anchor.rename(displaced)
    anchor.symlink_to(displaced, target_is_directory=True)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe base"):
        fs.task_dir("task-after-ancestor-symlink")

    task_path = displaced / "managed" / "app-data" / "tasks" / "task-after-ancestor-symlink"
    assert not task_path.exists()


def test_task_dir_rejects_runtime_ancestor_replacement_with_same_base_inode(
    tmp_path: Path,
):
    anchor = tmp_path / "anchor"
    anchor.mkdir(mode=0o700)
    base = anchor / "managed" / "app-data"
    fs = FsLayout(base_dir=str(base))
    displaced = tmp_path / "anchor-original"
    anchor.rename(displaced)
    anchor.mkdir(mode=0o700)
    (displaced / "managed").rename(anchor / "managed")

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe base"):
        fs.task_dir("task-after-ancestor-replace")

    assert not (base / "tasks" / "task-after-ancestor-replace").exists()


@pytest.mark.parametrize("legacy_mode", [0o755, 0o750])
def test_owned_legacy_base_directory_is_tightened_once(
    tmp_path: Path,
    legacy_mode: int,
):
    base = tmp_path / "app-data"
    base.mkdir(mode=legacy_mode)
    base.chmod(legacy_mode)

    FsLayout(base_dir=str(base))

    assert stat.S_IMODE(base.stat().st_mode) == 0o700


@pytest.mark.parametrize("unsafe_mode", [0o770, 0o707, 0o777])
def test_group_or_world_writable_base_directory_is_rejected(
    tmp_path: Path,
    unsafe_mode: int,
):
    base = tmp_path / "app-data"
    base.mkdir(mode=0o700)
    base.chmod(unsafe_mode)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe base directory"):
        FsLayout(base_dir=str(base))


def test_foreign_owned_base_directory_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    base = tmp_path / "app-data"
    base.mkdir(mode=0o700)
    real_fstat = fs_layout_module.os.fstat

    def foreign_fstat(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        values = list(result)
        values[4] = os.getuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(fs_layout_module.os, "fstat", foreign_fstat)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe base directory"):
        FsLayout(base_dir=str(base))


def test_base_directory_creation_retries_one_interrupted_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    base = tmp_path / "app-data"
    real_mkdir = fs_layout_module.os.mkdir
    interrupted = False

    def interrupt_once(
        path: str | os.PathLike[str],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal interrupted
        if os.fspath(path) in {str(base), base.name} and not interrupted:
            interrupted = True
            raise InterruptedError
        real_mkdir(path, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(fs_layout_module.os, "mkdir", interrupt_once)

    FsLayout(base_dir=str(base))

    assert interrupted
    assert stat.S_IMODE(base.stat().st_mode) == 0o700


@pytest.mark.parametrize("primary_operation", ["mkdir", "open"])
def test_cleanup_close_failure_does_not_mask_primary_directory_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary_operation: str,
):
    base = tmp_path / "app-data"
    real_mkdir = fs_layout_module.os.mkdir
    real_open = fs_layout_module.os.open
    real_close = fs_layout_module.os.close
    close_failed = False

    def fail_target_mkdir(
        path: str | os.PathLike[str],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if os.fspath(path) == base.name and dir_fd is not None:
            raise OSError("primary mkdir failure")
        real_mkdir(path, mode=mode, dir_fd=dir_fd)

    def fail_target_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if os.fspath(path) == base.name and dir_fd is not None:
            raise OSError("primary open failure")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def close_then_raise_once(fd: int) -> None:
        nonlocal close_failed
        real_close(fd)
        if not close_failed:
            close_failed = True
            raise OSError("secondary close failure")

    failing_operation = fail_target_mkdir if primary_operation == "mkdir" else fail_target_open
    monkeypatch.setattr(fs_layout_module.os, primary_operation, failing_operation)
    monkeypatch.setattr(fs_layout_module.os, "close", close_then_raise_once)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError) as captured:
        FsLayout(base_dir=str(base))

    assert captured.value.__cause__ is not None
    assert f"primary {primary_operation} failure" in str(captured.value.__cause__)
    assert close_failed


def test_cleanup_close_failure_without_primary_error_is_exposed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_close = fs_layout_module.os.close
    close_failed = False

    def close_then_raise_once(fd: int) -> None:
        nonlocal close_failed
        real_close(fd)
        if not close_failed:
            close_failed = True
            raise OSError("standalone close failure")

    monkeypatch.setattr(fs_layout_module.os, "close", close_then_raise_once)

    with pytest.raises(
        fs_layout_module.UnsafeTaskPathError,
        match="could not close directory",
    ) as captured:
        FsLayout(base_dir=str(tmp_path / "app-data"))

    assert isinstance(captured.value.__cause__, OSError)
    assert str(captured.value.__cause__) == "standalone close failure"
    assert close_failed


@pytest.mark.parametrize("operation", ["mkdir", "open"])
def test_fs_layout_persistent_eintr_fails_within_hard_process_timeout(
    tmp_path: Path,
    operation: str,
):
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    process = context.Process(
        target=_fs_layout_persistent_eintr_worker,
        args=(str(tmp_path / f"eintr-{operation}"), operation, results),
    )
    process.start()
    process.join(2)
    if process.is_alive():
        process.terminate()
        process.join(2)
        pytest.fail(f"persistent EINTR in FsLayout {operation} exceeded hard timeout")

    assert process.exitcode == 0
    assert results.get(timeout=1).startswith("unsafe base directory")


@pytest.mark.parametrize(
    "task_id",
    ["", ".", "..", "../outside", "nested/task", r"nested\\task", "/absolute"],
)
def test_task_id_must_be_a_safe_single_path_component(tmp_path: Path, task_id: str):
    fs = FsLayout(base_dir=str(tmp_path))

    with pytest.raises(
        fs_layout_module.UnsafeTaskPathError,
        match="task ID|task_id|path component",
    ):
        fs.task_dir(task_id)


def test_task_dir_rejects_existing_symlink_without_touching_target(tmp_path: Path):
    base = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "0.raw.md"
    sentinel.write_text("external", encoding="utf-8")
    fs = FsLayout(base_dir=str(base))
    (base / "tasks" / "unsafe-task").symlink_to(outside, target_is_directory=True)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe|symlink|directory"):
        fs.task_dir("unsafe-task")

    assert sentinel.read_text(encoding="utf-8") == "external"


def test_task_dir_rejects_existing_non_directory(tmp_path: Path):
    fs = FsLayout(base_dir=str(tmp_path))
    (tmp_path / "tasks" / "not-a-directory").write_text("file", encoding="utf-8")

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe|directory"):
        fs.task_dir("not-a-directory")


def test_owned_task_and_images_directories_are_tightened_to_0700(tmp_path: Path):
    fs = FsLayout(base_dir=str(tmp_path))
    task = Path(fs.task_dir("task-1"))
    images = task / "images"
    images.mkdir(mode=0o755)
    task.chmod(0o755)
    images.chmod(0o750)

    assert fs.images_dir("task-1") == str(images)
    assert stat.S_IMODE(task.stat().st_mode) == 0o700
    assert stat.S_IMODE(images.stat().st_mode) == 0o700


@pytest.mark.parametrize("unsafe_mode", [0o720, 0o702, 0o777])
def test_group_or_world_writable_task_directory_is_rejected(
    tmp_path: Path,
    unsafe_mode: int,
):
    fs = FsLayout(base_dir=str(tmp_path))
    task = Path(fs.task_dir("unsafe-task"))
    task.chmod(unsafe_mode)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe task"):
        fs.task_dir("unsafe-task")


def test_task_and_images_creation_is_0700_under_restrictive_umask(tmp_path: Path):
    fs = FsLayout(base_dir=str(tmp_path))
    previous_umask = os.umask(0o777)
    try:
        images = Path(fs.images_dir("task-umask"))
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(images.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(images.stat().st_mode) == 0o700


@pytest.mark.parametrize("unsafe_mode", [0o720, 0o702, 0o777])
def test_group_or_world_writable_tasks_directory_is_rejected(
    tmp_path: Path,
    unsafe_mode: int,
):
    base = tmp_path / "data"
    base.mkdir(mode=0o700)
    tasks = base / "tasks"
    tasks.mkdir(mode=0o700)
    tasks.chmod(unsafe_mode)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe tasks"):
        FsLayout(base_dir=str(base))


def test_task_dir_rejects_tasks_directory_replaced_after_initialization(
    tmp_path: Path,
):
    base = tmp_path / "data"
    fs = FsLayout(base_dir=str(base))
    tasks = base / "tasks"
    displaced = base / "tasks-displaced"
    tasks.rename(displaced)
    tasks.mkdir(mode=0o700)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe tasks"):
        fs.task_dir("task-after-tasks-swap")

    assert not (tasks / "task-after-tasks-swap").exists()
    assert not (displaced / "task-after-tasks-swap").exists()


def test_task_dir_rejects_base_replaced_after_layout_initialization(tmp_path: Path):
    base = tmp_path / "data"
    displaced = tmp_path / "data-displaced"
    fs = FsLayout(base_dir=str(base))
    base.rename(displaced)
    base.mkdir(mode=0o700)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe base"):
        fs.task_dir("task-after-swap")

    assert not (base / "tasks" / "task-after-swap").exists()
    assert not (displaced / "tasks" / "task-after-swap").exists()


def test_images_dir_rejects_task_replaced_during_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    base = tmp_path / "data"
    fs = FsLayout(base_dir=str(base))
    task = Path(fs.task_dir("task-swap"))
    displaced = base / "tasks" / "task-swap-displaced"
    real_mkdir = fs_layout_module.os.mkdir
    swapped = False

    def replace_task_before_images(
        name: str,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if name == "images" and not swapped:
            task.rename(displaced)
            task.mkdir(mode=0o700)
            swapped = True
        real_mkdir(name, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(fs_layout_module.os, "mkdir", replace_task_before_images)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe task"):
        fs.images_dir("task-swap")

    assert not (task / "images").exists()


def test_images_dir_rejects_existing_symlink_without_touching_target(tmp_path: Path):
    base = tmp_path / "data"
    outside = tmp_path / "outside-images"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("external", encoding="utf-8")
    fs = FsLayout(base_dir=str(base))
    task_dir = Path(fs.task_dir("task-1"))
    (task_dir / "images").symlink_to(outside, target_is_directory=True)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe|symlink|directory"):
        fs.images_dir("task-1")

    assert sentinel.read_text(encoding="utf-8") == "external"


def test_unsafe_task_path_error_is_a_distinct_runtime_error():
    assert issubclass(fs_layout_module.UnsafeTaskPathError, RuntimeError)


def test_task_entry_scan_is_returned_in_bounded_batches(tmp_path: Path) -> None:
    fs = FsLayout(base_dir=str(tmp_path / "data"))
    tasks = Path(fs.tasks_dir())
    for index in range(1_025):
        (tasks / f"task-{index:04d}").mkdir(mode=0o700)

    batches = list(fs.iter_task_entries(batch_size=64))

    assert sum(len(batch) for batch in batches) == 1_025
    assert batches
    assert max(len(batch) for batch in batches) <= 64
    assert len({entry.name for batch in batches for entry in batch}) == 1_025


def test_remove_task_tree_quarantines_and_deletes_only_bound_tree(tmp_path: Path) -> None:
    fs = FsLayout(base_dir=str(tmp_path / "data"))
    task = Path(fs.images_dir("task-delete")).parent
    nested = task / "images" / "nested"
    nested.mkdir(mode=0o700)
    (task / "merged.md").write_text("merged", encoding="utf-8")
    (nested / "figure.png").write_bytes(b"png")
    identity = fs.task_identity("task-delete")

    assert identity is not None
    assert fs.remove_task_tree("task-delete", expected_identity=identity) is True
    assert not task.exists()
    assert fs.remove_task_tree("task-delete") is False


def test_remove_task_tree_does_not_delete_replacement_at_quarantine_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs = FsLayout(base_dir=str(tmp_path / "data"))
    task = Path(fs.task_dir("task-race"))
    original_marker = task / "original.md"
    original_marker.write_text("original", encoding="utf-8")
    expected_identity = fs.task_identity("task-race")
    displaced = task.with_name("task-race-displaced")
    replacement_marker = task / "replacement.md"
    real_rename = fs_layout_module.os.rename
    swapped = False

    def swap_at_quarantine(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if source == "task-race" and "quarantine" in destination and not swapped:
            real_rename(
                source,
                "task-race-displaced",
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            os.mkdir("task-race", mode=0o700, dir_fd=src_dir_fd)
            replacement_task_fd = os.open(
                "task-race",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=src_dir_fd,
            )
            try:
                replacement_fd = os.open(
                    "replacement.md",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=replacement_task_fd,
                )
                try:
                    os.write(replacement_fd, b"replacement")
                finally:
                    os.close(replacement_fd)
            finally:
                os.close(replacement_task_fd)
            swapped = True
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(fs_layout_module.os, "rename", swap_at_quarantine)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="identity changed"):
        fs.remove_task_tree("task-race", expected_identity=expected_identity)

    assert swapped
    assert replacement_marker.read_text(encoding="utf-8") == "replacement"
    assert (displaced / "original.md").read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "fifo"])
def test_remove_task_tree_rejects_unsafe_tree_entries(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    fs = FsLayout(base_dir=str(tmp_path / "data"))
    task = Path(fs.task_dir(f"task-{unsafe_kind}"))
    outside = tmp_path / f"outside-{unsafe_kind}.txt"
    outside.write_text("outside", encoding="utf-8")
    unsafe = task / "unsafe"
    if unsafe_kind == "symlink":
        unsafe.symlink_to(outside)
    elif unsafe_kind == "hardlink":
        os.link(outside, unsafe)
    else:
        os.mkfifo(unsafe, mode=0o600)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe tree entry"):
        fs.remove_task_tree(f"task-{unsafe_kind}")

    assert outside.read_text(encoding="utf-8") == "outside"
