import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from parsing_core.storage.schema import init_db
from parsing_core.workbench import codex_cli as codex_cli_module
from parsing_core.workbench import markdown_sync as markdown_sync_module
from parsing_core.workbench import pipeline as pipeline_module
from parsing_core.workbench import repository as repository_module
from parsing_core.workbench.deepseek import MODEL_NAME, DeepSeekClient, DeepSeekExecutor
from parsing_core.workbench.executors import StubIntensiveReadingExecutor
from parsing_core.workbench.hybrid import HybridIntensiveReadingExecutor
from parsing_core.workbench.pipeline import ChapterMarkdownSyncError, IntensiveReadingPipeline
from parsing_core.workbench.repository import (
    WorkbenchRepository,
    read_stable_source_markdown,
)
from parsing_core.workbench.schema import apply_workbench_schema
from parsing_core.workbench.task_package import build_task_package
from parsing_core.workbench.topic_state import NOT_READY, READY, STALE, refresh_topic_status


def setup_chapter(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("战略管理", "", str(tmp_path / "out"))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "战略教材")
    source_md = tmp_path / "ch1.md"
    source_md.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    chapter = repo.create_chapter(course.id, source.id, 0, "第一章", str(source_md))
    repo.update_chapter_status(chapter.id, "CONFIRMED")
    return repo, chapter


def setup_topic(repo, chapter, *, published=False):
    topic = repo.create_topic(
        chapter.course_id,
        len(repo.list_topics(chapter.course_id)),
        "竞争优势",
    )
    repo.update_topic(topic.id, confirmed=True)
    repo.replace_topic_chapters(topic.id, [chapter.id])
    if published:
        repo.replace_topic_note_blocks(topic.id, {"summary": "旧主题摘要"})
    return topic


def test_task_package_body_and_fingerprint_share_one_source_snapshot(tmp_path, monkeypatch):
    repo, chapter = setup_chapter(tmp_path)
    source_path = Path(chapter.source_md_path)
    old_package = build_task_package(repo, chapter.id, "structure")
    source_path.write_text("## 第一章\n新的战略正文。", encoding="utf-8")
    new_package = build_task_package(repo, chapter.id, "structure")
    source_path.write_text("## 第一章\n战略是选择。", encoding="utf-8")

    original_snapshot = repo.chapter_input_snapshot
    injected = False

    def replace_source_before_fingerprint(chapter_id, *args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            source_path.write_text("## 第一章\n新的战略正文。", encoding="utf-8")
        return original_snapshot(chapter_id, *args, **kwargs)

    monkeypatch.setattr(repo, "chapter_input_snapshot", replace_source_before_fingerprint)

    raced_package = build_task_package(repo, chapter.id, "structure")

    assert (raced_package.content, raced_package.input_fingerprint) in {
        (old_package.content, old_package.input_fingerprint),
        (new_package.content, new_package.input_fingerprint),
    }


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "unsafe-mode", "directory"])
def test_source_snapshot_rejects_unsafe_file_types_and_metadata(tmp_path, attack):
    source = tmp_path / "source.md"
    source.write_text("safe", encoding="utf-8")
    candidate = source
    if attack == "symlink":
        candidate = tmp_path / "source-link.md"
        candidate.symlink_to(source)
    elif attack == "hardlink":
        candidate = tmp_path / "source-hardlink.md"
        candidate.hardlink_to(source)
    elif attack == "unsafe-mode":
        source.chmod(0o666)
    else:
        candidate = tmp_path / "source-directory"
        candidate.mkdir()

    with pytest.raises(ValueError) as error:
        read_stable_source_markdown(candidate)

    assert str(tmp_path) not in str(error.value)


def test_source_snapshot_rejects_symlink_in_intermediate_directory(tmp_path):
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    source = real_directory / "source.md"
    source.write_text("safe", encoding="utf-8")
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(ValueError) as error:
        read_stable_source_markdown(linked_directory / "source.md")

    assert str(tmp_path) not in str(error.value)


def test_source_snapshot_retries_when_path_is_replaced_during_read(tmp_path, monkeypatch):
    source = tmp_path / "source.md"
    source.write_bytes(b"old source")
    replacement = tmp_path / "replacement.md"
    replacement.write_bytes(b"new source")
    original_read = repository_module.os.read
    replaced = False

    def replace_path_after_read(fd, size):
        nonlocal replaced
        chunk = original_read(fd, size)
        if chunk and not replaced:
            replaced = True
            repository_module.os.replace(replacement, source)
        return chunk

    monkeypatch.setattr(repository_module.os, "read", replace_path_after_read)

    content, fingerprint = read_stable_source_markdown(source)

    assert content == b"new source"
    assert fingerprint == hashlib.sha256(content).hexdigest()


def test_source_snapshot_rejects_foreign_owner_and_unstable_metadata(tmp_path, monkeypatch):
    source = tmp_path / "source.md"
    source.write_text("safe", encoding="utf-8")
    real_fstat = repository_module.os.fstat
    call_count = 0

    def changed_fstat(fd):
        nonlocal call_count
        call_count += 1
        info = real_fstat(fd)
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_uid=info.st_uid,
            st_nlink=info.st_nlink,
            st_size=info.st_size,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_mtime_ns=info.st_mtime_ns + call_count,
            st_ctime_ns=info.st_ctime_ns,
        )

    monkeypatch.setattr(repository_module.os, "fstat", changed_fstat)
    with pytest.raises(ValueError, match="changed while being read"):
        read_stable_source_markdown(source)

    def foreign_owner_fstat(fd):
        info = real_fstat(fd)
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_uid=info.st_uid + 1,
            st_nlink=info.st_nlink,
            st_size=info.st_size,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_mtime_ns=info.st_mtime_ns,
            st_ctime_ns=info.st_ctime_ns,
        )

    monkeypatch.setattr(repository_module.os, "fstat", foreign_owner_fstat)
    with pytest.raises(ValueError, match="ownership is unsafe"):
        read_stable_source_markdown(source)


def test_source_snapshot_read_is_bounded(tmp_path):
    source = tmp_path / "source.md"
    source.write_bytes(b"12345")

    with pytest.raises(ValueError, match="exceeds size limit"):
        read_stable_source_markdown(source, max_bytes=4)


def test_pipeline_creates_blocks_cards_and_runs(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")

    pipeline.run_all(chapter.id)

    blocks = repo.list_note_blocks(chapter.id)
    runs = repo.list_runs(chapter.id)
    cards = repo.list_cards_by_chapter(chapter.id)
    assert {b.kind for b in blocks} >= {
        "summary",
        "knowledge_mermaid",
        "application_mermaid",
    }
    assert len(cards) >= 1
    assert len(runs) == 7


def test_two_connections_compete_for_chapter_generation(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    second_conn = sqlite3.connect(tmp_path / "workbench.db", check_same_thread=False)
    second_conn.execute("PRAGMA foreign_keys = ON")
    second = WorkbenchRepository(second_conn)
    barrier = threading.Barrier(2)
    outcomes = []

    def claim(candidate):
        barrier.wait()
        try:
            outcomes.append(candidate.start_chapter_generation(chapter.id).owner_id)
        except ValueError as exc:
            outcomes.append(str(exc))

    threads = [threading.Thread(target=claim, args=(candidate,)) for candidate in (repo, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(len(item) == 32 for item in outcomes) == 1
    assert any("already running" in item for item in outcomes)
    assert repo.get_chapter(chapter.id).status == "RUNNING"


def test_recover_expired_chapter_generation_marks_run_interrupted(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=10)
    run = repo.create_chapter_generation_run(chapter.id, start.owner_id, "structure", now=100)

    with pytest.raises(ValueError, match="lease not expired"):
        repo.recover_interrupted_chapter_run(chapter.id, now=109)
    recovered = repo.recover_interrupted_chapter_run(chapter.id, now=111)

    assert recovered.status == "FAILED"
    stored = repo.get_chapter_generation_run(run.id)
    assert stored.status == "FAILED"
    assert stored.error == "chapter generation interrupted"
    assert stored.error_code == "CHAPTER_GENERATION_INTERRUPTED"
    assert repo.get_chapter_generation_lease(chapter.id) is None


def test_expired_generation_resumes_from_first_missing_valid_candidate(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    calls: list[str] = []

    class CountingExecutor(StubIntensiveReadingExecutor):
        checkpoint_identity = "counting-executor-v1"

        def run(self, round_key: str, task_package: str) -> str:
            calls.append(round_key)
            return super().run(round_key, task_package)

    executor = CountingExecutor()
    first = IntensiveReadingPipeline(
        repo,
        executor,
        tmp_path / "first",
        clock=lambda: 100,
        lease_ttl=10,
        heartbeat_interval=60,
    )
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=10)
    first._run_candidate(chapter.id, start.owner_id, "structure")
    first._run_candidate(chapter.id, start.owner_id, "concepts")

    resumed = IntensiveReadingPipeline(
        repo,
        executor,
        tmp_path / "resumed",
        clock=lambda: 111,
        lease_ttl=60,
        heartbeat_interval=60,
    )
    resumed.run_all(chapter.id)

    assert calls.count("structure") == 2
    assert calls.count("concepts") == 2
    assert calls[-7:] == pipeline_module.ROUNDS
    assert repo.get_chapter(chapter.id).status == "COMPLETED"


@pytest.mark.parametrize("invalidation", ["input", "configuration", "corrupt"])
def test_expired_generation_invalidates_stale_or_corrupt_candidates(tmp_path, invalidation):
    repo, chapter = setup_chapter(tmp_path)
    calls: list[str] = []

    class CountingExecutor(StubIntensiveReadingExecutor):
        checkpoint_identity = "counting-executor-v1"

        def run(self, round_key: str, task_package: str) -> str:
            calls.append(round_key)
            return super().run(round_key, task_package)

    executor = CountingExecutor()
    first = IntensiveReadingPipeline(
        repo,
        executor,
        tmp_path / "first",
        clock=lambda: 100,
        lease_ttl=10,
        heartbeat_interval=60,
        configuration_fingerprint="configuration-v1",
    )
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=10)
    first._run_candidate(chapter.id, start.owner_id, "structure")
    first._run_candidate(chapter.id, start.owner_id, "concepts")

    configuration = "configuration-v1"
    if invalidation == "input":
        Path(chapter.source_md_path).write_text("## 第一章\n输入已经改变。", encoding="utf-8")
    elif invalidation == "configuration":
        configuration = "configuration-v2"
    else:
        repo.conn.execute(
            "UPDATE wb_chapter_generation_candidates SET output = 'tampered' "
            "WHERE chapter_id = ? AND round_key = 'structure'",
            (chapter.id,),
        )
        repo.conn.commit()

    IntensiveReadingPipeline(
        repo,
        executor,
        tmp_path / "resumed",
        clock=lambda: 111,
        lease_ttl=60,
        heartbeat_interval=60,
        configuration_fingerprint=configuration,
    ).run_all(chapter.id)

    assert calls.count("structure") == 2
    assert calls.count("concepts") == 2
    assert repo.get_chapter(chapter.id).status == "COMPLETED"


def test_checkpoint_binds_the_complete_task_package_content(tmp_path, monkeypatch):
    repo, chapter = setup_chapter(tmp_path)
    calls: list[str] = []

    class CountingExecutor(StubIntensiveReadingExecutor):
        checkpoint_identity = "counting-executor-v1"

        def run(self, round_key: str, task_package: str) -> str:
            calls.append(round_key)
            return super().run(round_key, task_package)

    executor = CountingExecutor()
    first = IntensiveReadingPipeline(
        repo,
        executor,
        tmp_path / "first",
        clock=lambda: 100,
        lease_ttl=10,
        heartbeat_interval=60,
    )
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=10)
    first._run_candidate(chapter.id, start.owner_id, "structure")
    original = pipeline_module.build_task_package

    def changed_task_package(repo_arg, chapter_id, round_key):
        package = original(repo_arg, chapter_id, round_key)
        return type(package)(
            package.chapter_id,
            package.round_key,
            package.title,
            package.content + "\n新增的实际 prompt 规则",
            package.input_fingerprint,
            package.citation_ids,
        )

    monkeypatch.setattr(pipeline_module, "build_task_package", changed_task_package)

    IntensiveReadingPipeline(
        repo,
        executor,
        tmp_path / "resumed",
        clock=lambda: 111,
        lease_ttl=60,
        heartbeat_interval=60,
    ).run_all(chapter.id)

    assert calls.count("structure") == 2


def test_checkpoint_without_explicit_identity_never_reuses_across_executor_instances(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    calls: list[str] = []

    class UnidentifiedExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key: str, task_package: str) -> str:
            calls.append(round_key)
            return super().run(round_key, task_package)

    first = IntensiveReadingPipeline(
        repo,
        UnidentifiedExecutor(),
        tmp_path / "first",
        clock=lambda: 100,
        lease_ttl=10,
        heartbeat_interval=60,
    )
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=10)
    first._run_candidate(chapter.id, start.owner_id, "structure")

    IntensiveReadingPipeline(
        repo,
        UnidentifiedExecutor(),
        tmp_path / "resumed",
        clock=lambda: 111,
        lease_ttl=60,
        heartbeat_interval=60,
    ).run_all(chapter.id)

    assert calls.count("structure") == 2


def test_third_party_checkpoint_identity_never_resumes_across_instances(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    calls: list[str] = []

    class IdentifiedExecutor(StubIntensiveReadingExecutor):
        def checkpoint_identity(self):
            return {"executor": "deterministic-test", "version": 1}

        def run(self, round_key: str, task_package: str) -> str:
            calls.append(round_key)
            return super().run(round_key, task_package)

    first = IntensiveReadingPipeline(
        repo,
        IdentifiedExecutor(),
        tmp_path / "first",
        clock=lambda: 100,
        lease_ttl=10,
        heartbeat_interval=60,
    )
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=10)
    first._run_candidate(chapter.id, start.owner_id, "structure")

    IntensiveReadingPipeline(
        repo,
        IdentifiedExecutor(),
        tmp_path / "resumed",
        clock=lambda: 111,
        lease_ttl=60,
        heartbeat_interval=60,
    ).run_all(chapter.id)

    assert calls.count("structure") == 2


def test_stateful_third_party_identity_cannot_reuse_artifact_from_changed_runtime(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    runtimes: list[str] = []

    class StatefulExecutor(StubIntensiveReadingExecutor):
        def __init__(self, runtime_after_identity: str):
            self.runtime = "runtime-before-identity"
            self.runtime_after_identity = runtime_after_identity

        def checkpoint_identity(self):
            self.runtime = self.runtime_after_identity
            return {"executor": "stateful-third-party", "version": 1}

        def run(self, round_key: str, task_package: str) -> str:
            if round_key == "structure":
                runtimes.append(self.runtime)
            return super().run(round_key, task_package)

    first = IntensiveReadingPipeline(
        repo,
        StatefulExecutor("runtime-B"),
        tmp_path / "first",
        clock=lambda: 100,
        lease_ttl=10,
        heartbeat_interval=60,
    )
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=10)
    first._run_candidate(chapter.id, start.owner_id, "structure")

    IntensiveReadingPipeline(
        repo,
        StatefulExecutor("runtime-A"),
        tmp_path / "resumed",
        clock=lambda: 111,
        lease_ttl=60,
        heartbeat_interval=60,
    ).run_all(chapter.id)

    assert runtimes == ["runtime-B", "runtime-A"]


def test_deepseek_checkpoint_does_not_reuse_same_model_from_different_endpoint(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    calls: list[str] = []

    class CountingDeepSeekExecutor(DeepSeekExecutor):
        def run(self, round_key: str, task_package: str) -> str:
            calls.append(round_key)
            return StubIntensiveReadingExecutor().run(round_key, task_package)

    first = IntensiveReadingPipeline(
        repo,
        CountingDeepSeekExecutor(
            DeepSeekClient("sk-test-only", MODEL_NAME, "https://endpoint-a.example/v1")
        ),
        tmp_path / "first",
        clock=lambda: 100,
        lease_ttl=10,
        heartbeat_interval=60,
    )
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=10)
    first._run_candidate(chapter.id, start.owner_id, "structure")

    IntensiveReadingPipeline(
        repo,
        CountingDeepSeekExecutor(
            DeepSeekClient("sk-test-only", MODEL_NAME, "https://endpoint-b.example/v1")
        ),
        tmp_path / "resumed",
        clock=lambda: 111,
        lease_ttl=60,
        heartbeat_interval=60,
    ).run_all(chapter.id)

    assert calls.count("structure") == 2


def test_checkpoint_identity_must_be_deterministic():
    calls = 0

    class ChangingIdentityExecutor(StubIntensiveReadingExecutor):
        def checkpoint_identity(self):
            nonlocal calls
            calls += 1
            return f"identity-{calls}"

    with pytest.raises(ValueError, match="checkpoint identity is invalid"):
        pipeline_module._chapter_configuration_fingerprint(ChangingIdentityExecutor(), None)


def test_checkpoint_identity_is_size_bounded():
    class OversizedIdentityExecutor(StubIntensiveReadingExecutor):
        checkpoint_identity = "x" * 65_536

    with pytest.raises(ValueError, match="checkpoint identity is invalid"):
        pipeline_module._chapter_configuration_fingerprint(OversizedIdentityExecutor(), None)


def test_checkpoint_identity_rejects_unnamed_top_level_primitive():
    class PrimitiveIdentityExecutor(StubIntensiveReadingExecutor):
        checkpoint_identity = True

    with pytest.raises(ValueError, match="checkpoint identity is invalid"):
        pipeline_module._chapter_configuration_fingerprint(PrimitiveIdentityExecutor(), None)


def test_checkpoint_identity_rejects_secret_without_echoing_it():
    secret = "sk-identity-never-persisted"

    class SecretIdentityExecutor(StubIntensiveReadingExecutor):
        checkpoint_identity = f"deepseek:{secret}"

    with pytest.raises(ValueError, match="checkpoint identity is invalid") as error:
        pipeline_module._chapter_configuration_fingerprint(SecretIdentityExecutor(), None)

    assert secret not in str(error.value)


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "Pass-Phrase",
        "CLIENT_secret",
        "token",
        "API.Key",
        "key",
        "Authorization",
        "cre-den_tial",
    ],
)
def test_checkpoint_identity_rejects_normalized_secret_keys(key):
    class SecretKeyExecutor(StubIntensiveReadingExecutor):
        checkpoint_identity = {key: "ordinary-looking-value"}

    with pytest.raises(Exception) as captured:
        pipeline_module._chapter_configuration_fingerprint(SecretKeyExecutor(), None)

    error = captured.value
    assert type(error).__name__ == "ExecutorCheckpointIdentityError"
    assert getattr(error, "code", None) == "EXECUTOR_CHECKPOINT_IDENTITY_INVALID"
    assert error.args == ("executor checkpoint identity is invalid",)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert getattr(error, "__notes__", []) == []


def test_checkpoint_identity_error_discards_original_exception_graph():
    token = "identity-error-secret-token"

    rich_error = None
    try:
        cause = RuntimeError(f"cause Authorization: Bearer {token}")
        cause.add_note(f"/Users/private/{token}")
        raise OSError(f"stderr {token}") from cause
    except OSError as exc:
        rich_error = exc
    assert rich_error is not None

    class FailingIdentityExecutor(StubIntensiveReadingExecutor):
        @property
        def checkpoint_identity(self):
            raise rich_error

    with pytest.raises(Exception) as captured:
        pipeline_module._chapter_configuration_fingerprint(FailingIdentityExecutor(), None)

    error = captured.value
    assert type(error).__name__ == "ExecutorCheckpointIdentityError"
    assert error.args == ("executor checkpoint identity is invalid",)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert getattr(error, "__notes__", []) == []
    assert token not in repr(error)


def test_deepseek_configuration_drift_fails_before_executor_run(tmp_path, monkeypatch):
    repo, chapter = setup_chapter(tmp_path)
    calls: list[str] = []

    def complete(_client, prompt, **_kwargs):
        calls.append(prompt)
        return StubIntensiveReadingExecutor().run("structure", prompt)

    monkeypatch.setattr(DeepSeekClient, "complete", complete)
    client = DeepSeekClient("sk-test-only", MODEL_NAME, "https://endpoint-a.example/v1")
    executor = DeepSeekExecutor(client)
    pipeline = IntensiveReadingPipeline(
        repo,
        executor,
        tmp_path / "runs",
        clock=lambda: 100,
        lease_ttl=60,
        heartbeat_interval=60,
    )
    if hasattr(client, "_base_url"):
        object.__setattr__(client, "_base_url", "https://endpoint-b.example/v1")
    else:
        client.base_url = "https://endpoint-b.example/v1"
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=60)

    with pytest.raises(Exception) as captured:
        pipeline._run_candidate(chapter.id, start.owner_id, "structure")

    assert type(captured.value).__name__ == "ExecutorCheckpointIdentityError"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert calls == []


def test_deepseek_api_key_drift_fails_before_executor_run(tmp_path, monkeypatch):
    repo, chapter = setup_chapter(tmp_path)
    calls: list[str] = []

    def complete(_client, prompt, **_kwargs):
        calls.append(prompt)
        return StubIntensiveReadingExecutor().run("structure", prompt)

    monkeypatch.setattr(DeepSeekClient, "complete", complete)
    client = DeepSeekClient("sk-test-only-a", MODEL_NAME)
    pipeline = IntensiveReadingPipeline(
        repo,
        DeepSeekExecutor(client),
        tmp_path / "runs",
        clock=lambda: 100,
        lease_ttl=60,
        heartbeat_interval=60,
    )
    object.__setattr__(client, "_api_key", "sk-test-only-b")
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=60)

    with pytest.raises(Exception) as captured:
        pipeline._run_candidate(chapter.id, start.owner_id, "structure")

    assert type(captured.value).__name__ == "ExecutorCheckpointIdentityError"
    assert captured.value.args == ("executor checkpoint identity is invalid",)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert calls == []


def test_checkpoint_override_augments_instead_of_replacing_executor_identity(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    calls: list[str] = []

    class VersionedExecutor(StubIntensiveReadingExecutor):
        def __init__(self, identity: str):
            self.checkpoint_identity = identity

        def run(self, round_key: str, task_package: str) -> str:
            calls.append(round_key)
            return super().run(round_key, task_package)

    first = IntensiveReadingPipeline(
        repo,
        VersionedExecutor("executor-v1"),
        tmp_path / "first",
        clock=lambda: 100,
        lease_ttl=10,
        heartbeat_interval=60,
        configuration_fingerprint="deployment-a",
    )
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=10)
    first._run_candidate(chapter.id, start.owner_id, "structure")

    IntensiveReadingPipeline(
        repo,
        VersionedExecutor("executor-v2"),
        tmp_path / "resumed",
        clock=lambda: 111,
        lease_ttl=60,
        heartbeat_interval=60,
        configuration_fingerprint="deployment-a",
    ).run_all(chapter.id)

    assert calls.count("structure") == 2


def test_codex_checkpoint_identity_binds_path_version_config_and_binary(
    tmp_path,
    monkeypatch,
):
    identity = {
        "path": "/verified/native/codex-a",
        "version": "codex-cli 1",
        "sha256": "a" * 64,
    }

    class FakeRunner:
        def __init__(
            self,
            _path,
            *,
            timeout,
            max_stdin_bytes,
            max_stdout_bytes,
            max_stderr_bytes,
        ):
            self.timeout = timeout
            self.max_stdin_bytes = max_stdin_bytes
            self.max_stdout_bytes = max_stdout_bytes
            self.max_stderr_bytes = max_stderr_bytes
            self.codex_version = identity["version"]
            self.source_identity = SimpleNamespace(
                path=Path(identity["path"]),
                device=1,
                inode=2,
                uid=501,
                mode=0o100700,
                nlink=1,
                size=10,
                mtime_ns=11,
                ctime_ns=12,
                sha256=identity["sha256"],
            )
            self.identity = self.source_identity
            self.path = Path("/private/runtime/codex")

    monkeypatch.setattr(codex_cli_module, "SecureCodexRunner", FakeRunner)

    fingerprints = []
    for selected, native, version, digest in (
        ("/verified/wrapper/codex-a", "/verified/native/codex-a", "codex-cli 1", "a" * 64),
        ("/verified/wrapper/codex-b", "/verified/native/codex-a", "codex-cli 1", "a" * 64),
        ("/verified/wrapper/codex-a", "/verified/native/codex-b", "codex-cli 1", "a" * 64),
        ("/verified/wrapper/codex-a", "/verified/native/codex-a", "codex-cli 2", "a" * 64),
        ("/verified/wrapper/codex-a", "/verified/native/codex-a", "codex-cli 1", "b" * 64),
    ):
        identity.update(path=native, version=version, sha256=digest)
        executor = codex_cli_module.CodexCliExecutor(selected, tmp_path)
        fingerprints.append(pipeline_module._chapter_configuration_fingerprint(executor, None))

    assert len(set(fingerprints)) == len(fingerprints)


def test_candidate_gap_discards_later_checkpoint_and_resumes_at_gap(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    calls: list[str] = []

    class CountingExecutor(StubIntensiveReadingExecutor):
        checkpoint_identity = "counting-executor-v1"

        def run(self, round_key: str, task_package: str) -> str:
            calls.append(round_key)
            return super().run(round_key, task_package)

    executor = CountingExecutor()
    first = IntensiveReadingPipeline(
        repo,
        executor,
        tmp_path / "first",
        clock=lambda: 100,
        lease_ttl=10,
        heartbeat_interval=60,
    )
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=10)
    for round_key in ("structure", "concepts", "plain_explain"):
        first._run_candidate(chapter.id, start.owner_id, round_key)
    repo.conn.execute(
        "DELETE FROM wb_chapter_generation_candidates "
        "WHERE chapter_id = ? AND round_key = 'concepts'",
        (chapter.id,),
    )
    repo.conn.commit()

    IntensiveReadingPipeline(
        repo,
        executor,
        tmp_path / "resumed",
        clock=lambda: 111,
        lease_ttl=60,
        heartbeat_interval=60,
    ).run_all(chapter.id)

    assert calls.count("structure") == 2
    assert calls.count("concepts") == 2
    assert calls.count("plain_explain") == 2
    assert repo.get_chapter(chapter.id).status == "COMPLETED"


def test_failed_generation_preserves_previous_published_chapter(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    repo.upsert_note_block(chapter.id, "summary", "本章概要", "旧成功内容", 0)
    repo.create_card(chapter.course_id, chapter.id, "topic", "旧卡片", "旧卡片内容")
    repo.update_chapter_status(chapter.id, "FAILED")

    class FailingExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            if round_key == "concepts":
                raise RuntimeError("boom")
            return super().run(round_key, task_package)

    with pytest.raises(RuntimeError, match="boom"):
        IntensiveReadingPipeline(repo, FailingExecutor(), tmp_path / "runs").run_all(chapter.id)

    assert repo.list_note_blocks(chapter.id)[0].body == "旧成功内容"
    assert repo.list_cards_by_chapter(chapter.id)[0].title == "旧卡片"
    assert repo.get_chapter(chapter.id).status == "FAILED"


def test_review_receives_all_six_candidates_and_rejection_does_not_publish(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    seen = {}

    class RejectingReview(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            if round_key == "review":
                package = json.loads(task_package)
                seen.update(package["candidates"])
                return json.dumps(
                    {"passed": False, "issues": ["来源不足"], "revised_blocks": {}},
                    ensure_ascii=False,
                )
            return super().run(round_key, task_package)

    with pytest.raises(ValueError, match="chapter review rejected"):
        IntensiveReadingPipeline(repo, RejectingReview(), tmp_path / "runs").run_all(chapter.id)

    assert set(seen) == {
        "structure",
        "concepts",
        "plain_explain",
        "application",
        "mermaid",
        "cards",
    }
    assert repo.list_note_blocks(chapter.id) == []
    assert repo.list_cards_by_chapter(chapter.id) == []
    assert repo.get_chapter(chapter.id).status == "FAILED"


def test_review_must_return_exact_fixed_blocks(tmp_path):
    repo, chapter = setup_chapter(tmp_path)

    class InvalidReview(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            if round_key == "review":
                return json.dumps({"passed": True, "issues": [], "revised_blocks": {}})
            return super().run(round_key, task_package)

    with pytest.raises(ValueError, match="fixed chapter blocks"):
        IntensiveReadingPipeline(repo, InvalidReview(), tmp_path / "runs").run_all(chapter.id)

    assert repo.get_chapter(chapter.id).status == "FAILED"


@pytest.mark.parametrize(
    ("failure_step", "trigger_sql"),
    [
        (
            "blocks",
            "CREATE TRIGGER fail_publish BEFORE INSERT ON wb_note_blocks "
            "BEGIN SELECT RAISE(ABORT, 'blocks failed'); END",
        ),
        (
            "cards",
            "CREATE TRIGGER fail_publish BEFORE INSERT ON wb_cards "
            "BEGIN SELECT RAISE(ABORT, 'cards failed'); END",
        ),
        (
            "runs",
            "CREATE TRIGGER fail_publish BEFORE UPDATE ON wb_runs "
            "BEGIN SELECT RAISE(ABORT, 'runs failed'); END",
        ),
        (
            "review",
            "CREATE TRIGGER fail_publish BEFORE UPDATE ON wb_chapter_generation_runs "
            "BEGIN SELECT RAISE(ABORT, 'review failed'); END",
        ),
        (
            "chapter",
            "CREATE TRIGGER fail_publish BEFORE UPDATE ON wb_chapters "
            "WHEN NEW.status = 'COMPLETED' BEGIN SELECT RAISE(ABORT, 'chapter failed'); END",
        ),
        (
            "lease",
            "CREATE TRIGGER fail_publish BEFORE DELETE ON wb_chapter_generation_leases "
            "BEGIN SELECT RAISE(ABORT, 'lease failed'); END",
        ),
    ],
)
def test_publish_chapter_generation_rolls_back_every_table_on_each_step_failure(
    tmp_path, failure_step, trigger_sql
):
    repo, chapter = setup_chapter(tmp_path)
    repo.upsert_note_block(chapter.id, "summary", "本章概要", "旧块", 0)
    repo.create_card(chapter.course_id, chapter.id, "topic", "旧卡", "旧卡内容")
    repo.upsert_run(chapter.id, "structure", "old", "DONE", "old-in", "old-out", "旧轮次")
    repo.update_chapter_status(chapter.id, "FAILED")
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=10_000_000_000)
    candidate = repo.create_chapter_generation_run(chapter.id, start.owner_id, "structure", now=101)
    repo.finish_chapter_generation_run(
        candidate.id, start.owner_id, "COMPLETED", output="新候选", now=102
    )
    review = repo.create_chapter_generation_run(chapter.id, start.owner_id, "review", now=103)

    tracked_tables = (
        "wb_note_blocks",
        "wb_cards",
        "wb_runs",
        "wb_chapter_generation_runs",
        "wb_chapter_generation_candidates",
        "wb_chapters",
        "wb_chapter_generation_leases",
    )
    before = {
        table: repo.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in tracked_tables
    }
    if failure_step in {"chapter", "lease"}:
        repo.publish_chapter_generation(
            chapter.id,
            start.owner_id,
            {"summary": ("本章概要", "新块", 0)},
            ("新卡", "新卡内容"),
            review.id,
            '{"passed":true,"issues":[],"revised_blocks":{}}',
            {"structure": ("new-in", "new-out")},
        )
        before = {
            table: repo.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tracked_tables
        }
        repo.conn.execute(trigger_sql)
        with pytest.raises(sqlite3.IntegrityError, match=f"{failure_step} failed"):
            pending = repo.pending_chapter_markdown_sync(chapter.id)
            assert pending is not None
            with repo.fence_chapter_markdown_publication(
                chapter.id,
                start.owner_id,
                review.id,
                pending["publication_id"],
                clock=lambda: 104,
            ) as publication:
                publication.bind_file_publication(
                    repository_module._verified_file_publication_receipt(
                        pending["publication_id"],
                        "f" * 64,
                    )
                )
    else:
        repo.conn.execute(trigger_sql)

        with pytest.raises(sqlite3.IntegrityError, match=f"{failure_step} failed"):
            repo.publish_chapter_generation(
                chapter.id,
                start.owner_id,
                {"summary": ("本章概要", "新块", 0)},
                ("新卡", "新卡内容"),
                review.id,
                '{"passed":true,"issues":[],"revised_blocks":{}}',
                {"structure": ("new-in", "new-out")},
            )

    after = {
        table: repo.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in tracked_tables
    }
    assert after == before


def test_pipeline_materializes_generated_mermaid_output(tmp_path):
    class CustomMermaidExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key: str, task_package: str) -> str:
            if round_key == "mermaid":
                return """\
## 知识结构图

```mermaid
flowchart TD
  StrategyChoice[战略选择] --> TradeoffMap[取舍地图]
```

## 应用流程图

```mermaid
flowchart LR
  ScenarioScan[场景扫描] --> ActionLoop[行动闭环]
```
"""
            return super().run(round_key, task_package)

    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, CustomMermaidExecutor(), tmp_path / "runs")

    pipeline.run_all(chapter.id)

    note_path = tmp_path / "out" / "教材" / "战略教材" / "01-第一章" / "intensive-note.md"
    note = note_path.read_text(encoding="utf-8")
    assert "StrategyChoice[战略选择]" in note
    assert "ScenarioScan[场景扫描]" in note
    assert "A[概念] --> B[结构]" not in note


def test_pipeline_rejects_unknown_citation_id(tmp_path):
    repo, chapter = setup_chapter(tmp_path)

    class UnknownCitation(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            if round_key == "structure":
                return "引用 [src:not-allowed:p1:para1]"
            return super().run(round_key, task_package)

    with pytest.raises(ValueError, match="unknown citation"):
        IntensiveReadingPipeline(repo, UnknownCitation(), tmp_path / "runs").run_all(chapter.id)


def test_pipeline_rejects_changed_input_before_publish(tmp_path):
    repo, chapter = setup_chapter(tmp_path)

    class MutatingReview(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            result = super().run(round_key, task_package)
            if round_key == "review":
                Path(chapter.source_md_path).write_text("changed", encoding="utf-8")
            return result

    with pytest.raises(ValueError, match="input fingerprint changed"):
        IntensiveReadingPipeline(repo, MutatingReview(), tmp_path / "runs").run_all(chapter.id)


def test_pipeline_persists_task_package_provenance_on_runs(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs").run_all(
        chapter.id
    )
    runs = repo.list_runs(chapter.id)
    assert all(run.input_fingerprint for run in runs)
    assert all(isinstance(json.loads(run.citation_ids_json), list) for run in runs)


def _counting_executor(executor_kind: str):
    class CountingStubExecutor(StubIntensiveReadingExecutor):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, round_key: str, task_package: str) -> str:
            self.calls.append(round_key)
            return super().run(round_key, task_package)

    if executor_kind == "stub":
        executor = CountingStubExecutor()
        return executor, lambda: len(executor.calls)
    deepseek = CountingStubExecutor()
    codex = CountingStubExecutor()
    return (
        HybridIntensiveReadingExecutor(deepseek, codex),
        lambda: len(deepseek.calls) + len(codex.calls),
    )


@pytest.mark.parametrize("executor_kind", ["stub", "hybrid"])
@pytest.mark.parametrize(
    ("failure_kind", "expected_error"),
    [
        ("sync", pipeline_module.ChapterMarkdownSyncError),
        ("crash", RuntimeError),
    ],
)
def test_staged_publish_recovery_keeps_data_and_skips_executor_rerun(
    tmp_path, monkeypatch, executor_kind, failure_kind, expected_error
):
    repo, chapter = setup_chapter(tmp_path)
    executor, call_count = _counting_executor(executor_kind)
    pipeline = IntensiveReadingPipeline(repo, executor, tmp_path / "runs")
    sync_attempts = {"count": 0}
    real_publish = pipeline_module.publish_chapter_markdown

    def fail_once(*args, **kwargs):
        sync_attempts["count"] += 1
        if sync_attempts["count"] == 1:
            if failure_kind == "sync":
                raise OSError("cannot write staged Markdown")
            raise RuntimeError("crash after staging publish")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "publish_chapter_markdown", fail_once)

    with pytest.raises(expected_error):
        pipeline.run_all(chapter.id)

    first_call_count = call_count()
    assert first_call_count == 7
    assert repo.get_chapter(chapter.id).status == "SYNC_PENDING"
    assert repo.get_chapter_generation_lease(chapter.id) is None
    assert repo.pending_chapter_markdown_sync(chapter.id) is not None
    assert {block.kind for block in repo.list_note_blocks(chapter.id)} >= {
        "summary",
        "knowledge_mermaid",
        "application_mermaid",
    }
    assert len(repo.list_cards_by_chapter(chapter.id)) == 1

    assert pipeline_module.recover_pending_chapter_markdown_sync(repo, chapter.id) is True

    assert call_count() == first_call_count
    assert repo.get_chapter(chapter.id).status == "COMPLETED"
    assert repo.get_chapter_generation_lease(chapter.id) is None
    assert repo.pending_chapter_markdown_sync(chapter.id) is None


def test_topic_invalidation_failure_after_claim_does_not_leave_running_lease(tmp_path, monkeypatch):
    repo, chapter = setup_chapter(tmp_path)

    def fail_stale(*_args, **_kwargs):
        raise RuntimeError("topic stale failed")

    monkeypatch.setattr(pipeline_module, "mark_topics_stale_for_chapter", fail_stale)

    with pytest.raises(RuntimeError, match="topic stale failed"):
        IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs").run_all(
            chapter.id
        )

    assert repo.get_chapter(chapter.id).status == "FAILED"
    assert repo.get_chapter_generation_lease(chapter.id) is None
    assert repo.pending_chapter_markdown_sync(chapter.id) is None


def test_expired_staged_claim_recovers_sync_only_without_executor_rerun(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    executor, call_count = _counting_executor("stub")
    base_time = int(pipeline_module.time.time())
    crashed = IntensiveReadingPipeline(
        repo,
        executor,
        tmp_path / "runs",
        clock=lambda: base_time,
        lease_ttl=10,
        heartbeat_interval=60,
    )
    start = repo.start_chapter_generation(chapter.id, now=base_time, lease_ttl=10)
    for round_key in pipeline_module.ROUNDS[:-1]:
        crashed._run_candidate(chapter.id, start.owner_id, round_key)
    crashed._run_review_and_stage(chapter.id, start.owner_id)
    assert call_count() == 7
    assert repo.get_chapter(chapter.id).status == "SYNC_PENDING"

    recovered = IntensiveReadingPipeline(
        repo,
        executor,
        tmp_path / "runs",
        clock=lambda: base_time + 11,
        lease_ttl=60,
        heartbeat_interval=60,
    )
    recovered.run_all(chapter.id)

    assert call_count() == 7
    assert repo.get_chapter(chapter.id).status == "COMPLETED"
    assert repo.get_chapter_generation_lease(chapter.id) is None


def test_owner_taken_over_during_sync_cannot_publish_final_markdown(tmp_path, monkeypatch):
    repo, chapter = setup_chapter(tmp_path)
    base_time = 10_000
    clock = {"now": base_time}
    pipeline = IntensiveReadingPipeline(
        repo,
        StubIntensiveReadingExecutor(),
        tmp_path / "runs",
        clock=lambda: clock["now"],
        lease_ttl=60,
        heartbeat_interval=60,
    )
    start = repo.start_chapter_generation(chapter.id, now=base_time, lease_ttl=60)
    for round_key in pipeline_module.ROUNDS[:-1]:
        pipeline._run_candidate(chapter.id, start.owner_id, round_key)
    pipeline._run_review_and_stage(chapter.id, start.owner_id)
    staged_start = type(start)(repo.get_chapter(chapter.id), start.owner_id)

    second_conn = sqlite3.connect(
        tmp_path / "workbench.db",
        timeout=2,
        check_same_thread=False,
    )
    second_conn.execute("PRAGMA foreign_keys = ON")
    second = WorkbenchRepository(second_conn)
    entered_sync = threading.Event()
    takeover_started = threading.Event()
    winner = []
    original_prepare = markdown_sync_module._prepare_atomic_bundle_locked
    blocked = False

    def block_after_first_bundle(*args, **kwargs):
        nonlocal blocked
        entries = original_prepare(*args, **kwargs)
        if not blocked:
            blocked = True
            clock["now"] = base_time + 61
            entered_sync.set()
            assert takeover_started.wait(timeout=2)
            time.sleep(0.05)
        return entries

    def take_over() -> None:
        assert entered_sync.wait(timeout=2)
        takeover_started.set()
        winner.append(
            second.start_chapter_generation(
                chapter.id,
                now=clock["now"],
                lease_ttl=60,
            )
        )

    monkeypatch.setattr(
        markdown_sync_module,
        "_prepare_atomic_bundle_locked",
        block_after_first_bundle,
    )
    takeover_thread = threading.Thread(target=take_over)
    takeover_thread.start()

    with pytest.raises(ChapterMarkdownSyncError):
        pipeline_module._sync_claimed_chapter_markdown(
            repo,
            chapter.id,
            staged_start,
            now=base_time,
            lease_ttl=60,
            clock=lambda: clock["now"],
        )
    takeover_thread.join(timeout=2)
    assert not takeover_thread.is_alive()

    note_path = tmp_path / "out" / "教材" / "战略教材" / "01-第一章" / "intensive-note.md"
    assert not note_path.exists()
    assert len(winner) == 1
    assert second.pending_chapter_markdown_sync(chapter.id)["owner_id"] == winner[0].owner_id

    clock["now"] += 1
    assert pipeline_module.recover_pending_chapter_markdown_sync(second, chapter.id) is True
    assert note_path.exists()
    assert second.get_chapter(chapter.id).status == "COMPLETED"
    winner_markdown = note_path.read_bytes()

    pipeline_module._release_generation_claim(
        repo,
        chapter.id,
        start.owner_id,
        "old owner cleanup",
    )

    assert note_path.read_bytes() == winner_markdown


def test_owner_that_lost_lease_before_sync_cannot_publish_markdown(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(
        repo,
        StubIntensiveReadingExecutor(),
        tmp_path / "runs",
        clock=lambda: 100,
        lease_ttl=10,
        heartbeat_interval=60,
    )
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=10)
    for round_key in pipeline_module.ROUNDS[:-1]:
        pipeline._run_candidate(chapter.id, start.owner_id, round_key)
    pipeline._run_review_and_stage(chapter.id, start.owner_id)
    staged_start = type(start)(repo.get_chapter(chapter.id), start.owner_id)
    second_conn = sqlite3.connect(tmp_path / "workbench.db")
    second_conn.execute("PRAGMA foreign_keys = ON")
    second = WorkbenchRepository(second_conn)
    winner = second.start_chapter_generation(chapter.id, now=111, lease_ttl=60)

    with pytest.raises(ChapterMarkdownSyncError):
        pipeline_module._sync_claimed_chapter_markdown(
            repo,
            chapter.id,
            staged_start,
            now=111,
            lease_ttl=60,
            clock=lambda: 111,
        )

    assert second.pending_chapter_markdown_sync(chapter.id)["owner_id"] == winner.owner_id
    note_path = tmp_path / "out" / "教材" / "战略教材" / "01-第一章" / "intensive-note.md"
    assert not note_path.exists()


def test_source_change_during_sync_rolls_back_final_markdown(tmp_path, monkeypatch):
    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(
        repo,
        StubIntensiveReadingExecutor(),
        tmp_path / "runs",
        clock=lambda: 100,
        lease_ttl=60,
        heartbeat_interval=60,
    )
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=60)
    for round_key in pipeline_module.ROUNDS[:-1]:
        pipeline._run_candidate(chapter.id, start.owner_id, round_key)
    pipeline._run_review_and_stage(chapter.id, start.owner_id)
    staged_start = type(start)(repo.get_chapter(chapter.id), start.owner_id)
    original_prepare = markdown_sync_module._prepare_atomic_bundle_locked
    changed = False

    def change_source_after_first_bundle(*args, **kwargs):
        nonlocal changed
        entries = original_prepare(*args, **kwargs)
        if not changed:
            changed = True
            Path(chapter.source_md_path).write_text(
                "## 第一章\n同步期间输入变化。",
                encoding="utf-8",
            )
        return entries

    monkeypatch.setattr(
        markdown_sync_module,
        "_prepare_atomic_bundle_locked",
        change_source_after_first_bundle,
    )

    with pytest.raises(ChapterMarkdownSyncError):
        pipeline_module._sync_claimed_chapter_markdown(
            repo,
            chapter.id,
            staged_start,
            now=100,
            lease_ttl=60,
            clock=lambda: 100,
        )

    note_path = tmp_path / "out" / "教材" / "战略教材" / "01-第一章" / "intensive-note.md"
    assert not note_path.exists()
    assert repo.get_chapter(chapter.id).status == "SYNC_PENDING"


def test_sync_recovery_rolls_back_uncommitted_journal_before_input_invalidation(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")
    start = repo.start_chapter_generation(chapter.id)
    for round_key in pipeline_module.ROUNDS[:-1]:
        pipeline._run_candidate(chapter.id, start.owner_id, round_key)
    pipeline._run_review_and_stage(chapter.id, start.owner_id)
    pending = repo.pending_chapter_markdown_sync(chapter.id)
    assert pending is not None
    chapter_dir = tmp_path / "out" / "教材" / "战略教材" / "01-第一章"
    chapter_dir.mkdir(parents=True)
    invalid_note = chapter_dir / "intensive-note.md"
    invalid_note.write_text("invalid old owner publication", encoding="utf-8")
    journal = chapter_dir / markdown_sync_module.JOURNAL_NAME
    journal.write_text(
        json.dumps(
            {
                "phase": "PREPARED",
                "transaction_id": pending["publication_id"],
                "entries": [
                    {
                        "target": "intensive-note.md",
                        "temp": ".pdf2md-deadbeef.bundle-tmp",
                        "backup": None,
                        "existed": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    Path(chapter.source_md_path).write_text("## 第一章\n输入已经改变。", encoding="utf-8")
    repo.conn.execute(
        "DELETE FROM wb_chapter_generation_leases WHERE chapter_id = ?",
        (chapter.id,),
    )
    repo.conn.commit()

    assert pipeline_module.recover_pending_chapter_markdown_sync(repo, chapter.id) is False

    assert not invalid_note.exists()
    assert not journal.exists()
    assert repo.get_chapter(chapter.id).status == "FAILED"
    assert repo.pending_chapter_markdown_sync(chapter.id) is None


@pytest.mark.parametrize("change_kind", ["source", "attachment", "note"])
def test_sync_pending_change_discards_old_candidate_and_runs_full_generation(
    tmp_path, monkeypatch, change_kind
):
    repo, chapter = setup_chapter(tmp_path)
    executor, call_count = _counting_executor("stub")
    sync_attempts = {"count": 0}
    real_publish = pipeline_module.publish_chapter_markdown

    def fail_once(*args, **kwargs):
        sync_attempts["count"] += 1
        if sync_attempts["count"] == 1:
            raise OSError("cannot write staged Markdown")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "publish_chapter_markdown", fail_once)
    with pytest.raises(pipeline_module.ChapterMarkdownSyncError):
        IntensiveReadingPipeline(repo, executor, tmp_path / "first").run_all(chapter.id)
    assert call_count() == 7
    assert repo.get_chapter(chapter.id).status == "SYNC_PENDING"

    if change_kind == "source":
        Path(chapter.source_md_path).write_text("## 第一章\n输入已变化。", encoding="utf-8")
    elif change_kind == "attachment":
        repo.create_attachment(
            chapter.course_id,
            chapter.source_id,
            chapter.id,
            str(tmp_path / "case.pdf"),
            "新案例",
            "pdf",
            "案例正文",
            "new-attachment-hash",
            [
                {
                    "citation_id": "att:new:p1:para1",
                    "page": 1,
                    "paragraph": 1,
                    "text": "案例正文",
                }
            ],
        )
    else:
        summary = next(
            block for block in repo.list_note_blocks(chapter.id) if block.kind == "summary"
        )
        repo.upsert_note_block(chapter.id, "summary", summary.title, "人工修改的待发布笔记", 0)

    IntensiveReadingPipeline(repo, executor, tmp_path / "second").run_all(chapter.id)

    assert call_count() == 14
    assert repo.get_chapter(chapter.id).status == "COMPLETED"
    assert repo.pending_chapter_markdown_sync(chapter.id) is None


def test_completed_generation_does_not_depend_on_postcommit_topic_refresh(tmp_path, monkeypatch):
    repo, chapter = setup_chapter(tmp_path)
    topic = setup_topic(repo, chapter)

    def unexpected_refresh(*_args, **_kwargs):
        raise RuntimeError("postcommit refresh must not run")

    monkeypatch.setattr(pipeline_module, "_refresh_topics_for_chapter", unexpected_refresh)

    IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs").run_all(
        chapter.id
    )

    assert repo.get_chapter(chapter.id).status == "COMPLETED"
    assert repo.get_topic(topic.id).status == READY


def test_pipeline_marks_round_failed_when_mermaid_output_is_incomplete(tmp_path):
    class BrokenMermaidExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key: str, task_package: str) -> str:
            if round_key == "mermaid":
                return "没有 Mermaid 图"
            return super().run(round_key, task_package)

    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, BrokenMermaidExecutor(), tmp_path / "runs")

    with pytest.raises(ValueError):
        pipeline.run_all(chapter.id)

    mermaid_run = [run for run in repo.list_runs(chapter.id) if run.round_key == "mermaid"][0]
    assert mermaid_run.status == "FAILED"


def test_rerun_marks_later_rounds_stale(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")
    pipeline.run_all(chapter.id)

    pipeline.rerun(chapter.id, "concepts")

    stale = {r.round_key for r in repo.list_runs(chapter.id) if r.stale}
    assert "concepts" not in stale
    assert {"plain_explain", "application", "mermaid", "cards", "review"} <= stale


def test_rerun_cards_does_not_duplicate_cards(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")
    pipeline.run_all(chapter.id)
    assert len(repo.list_cards_by_chapter(chapter.id)) == 1

    pipeline.rerun(chapter.id, "cards")

    assert len(repo.list_cards_by_chapter(chapter.id)) == 1


def test_pipeline_allows_failed_chapter_rerun(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    repo.update_chapter_status(chapter.id, "FAILED")
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")

    pipeline.run_all(chapter.id)

    assert len(repo.list_runs(chapter.id)) == 7


@pytest.mark.parametrize("operation", ["run_all", "rerun"])
def test_pipeline_rejects_missing_chapter_without_run_side_effects(tmp_path, operation):
    repo, _ = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")
    args = ("missing-chapter",) if operation == "run_all" else ("missing-chapter", "concepts")

    with pytest.raises(ValueError, match="^chapter not found$"):
        getattr(pipeline, operation)(*args)

    run_count = repo.conn.execute("SELECT COUNT(*) FROM wb_runs").fetchone()[0]
    assert run_count == 0


@pytest.mark.parametrize("operation", ["run_all", "rerun"])
def test_pipeline_wraps_markdown_sync_filesystem_errors(tmp_path, monkeypatch, operation):
    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")

    def fail_sync(*_args, **_kwargs):
        raise OSError(f"cannot write {tmp_path}/private/intensive-note.md")

    monkeypatch.setattr(
        pipeline_module,
        "publish_chapter_markdown" if operation == "run_all" else "sync_chapter_markdown",
        fail_sync,
    )
    args = (chapter.id,) if operation == "run_all" else (chapter.id, "concepts")

    with pytest.raises(pipeline_module.ChapterMarkdownSyncError):
        getattr(pipeline, operation)(*args)


def test_chapter_markdown_sync_error_has_safe_code_message_and_empty_exception_chain(
    tmp_path,
    monkeypatch,
):
    repo, chapter = setup_chapter(tmp_path)
    token = "chapter-public-exception-token"
    absolute_path = "/Users/private/chapter.md"
    stderr = "stderr: upstream request failed"

    try:
        cause = RuntimeError(f"cause {token} {absolute_path}")
        cause.add_note(f"cause note {stderr}")
        raise OSError(f"Authorization: Bearer {token} {absolute_path} {stderr}") from cause
    except OSError as error_with_context:
        error_with_context.add_note(f"outer note {token}")
        rich_error = error_with_context

    def fail_sync(*_args, **_kwargs):
        raise rich_error

    monkeypatch.setattr(pipeline_module, "sync_chapter_markdown", fail_sync)

    with pytest.raises(ChapterMarkdownSyncError) as captured:
        IntensiveReadingPipeline(
            repo,
            StubIntensiveReadingExecutor(),
            tmp_path / "runs",
        ).rerun(chapter.id, "concepts")

    error = captured.value
    assert error.code == "CHAPTER_MARKDOWN_PUBLICATION_FAILED"
    assert error.args == ("chapter Markdown publication failed",)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert getattr(error, "__notes__", []) == []
    exposed = repr(
        (error.args, error.__cause__, error.__context__, getattr(error, "__notes__", []))
    )
    for forbidden in (token, absolute_path, stderr, "Bearer"):
        assert forbidden not in exposed


@pytest.mark.parametrize("operation", ["run_all", "rerun"])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, UnicodeError])
def test_pipeline_wraps_all_markdown_sync_errors(tmp_path, monkeypatch, operation, error_type):
    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")

    def fail_sync(*_args, **_kwargs):
        raise error_type("chapter not found")

    monkeypatch.setattr(
        pipeline_module,
        "publish_chapter_markdown" if operation == "run_all" else "sync_chapter_markdown",
        fail_sync,
    )
    args = (chapter.id,) if operation == "run_all" else (chapter.id, "concepts")

    with pytest.raises(ChapterMarkdownSyncError) as captured:
        getattr(pipeline, operation)(*args)

    assert captured.value.code == "CHAPTER_MARKDOWN_PUBLICATION_FAILED"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_pipeline_skips_codex_task_files_for_mermaid_and_review_rounds(tmp_path):
    class FakeDeepSeekExecutor(StubIntensiveReadingExecutor):
        pass

    class FakeCodexExecutor(StubIntensiveReadingExecutor):
        pass

    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(
        repo,
        HybridIntensiveReadingExecutor(FakeDeepSeekExecutor(), FakeCodexExecutor()),
        tmp_path / "runs",
    )

    pipeline.run_all(chapter.id)

    run_dir = tmp_path / "runs"
    assert not (run_dir / f"{chapter.id}-mermaid-task.md").exists()
    assert not (run_dir / f"{chapter.id}-review-task.md").exists()
    assert (run_dir / f"{chapter.id}-structure-task.md").exists()
    assert (run_dir / f"{chapter.id}-cards-task.md").exists()

    runs = {run.round_key: run for run in repo.list_runs(chapter.id)}
    assert runs["mermaid"].input_path == ""
    assert runs["review"].input_path == ""
    assert runs["structure"].input_path.endswith(f"{chapter.id}-structure-task.md")


def test_run_all_refreshes_unpublished_topic_after_review_completes(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    topic = setup_topic(repo, chapter)
    assert refresh_topic_status(repo, topic.id).status == NOT_READY

    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")
    pipeline.run_all(chapter.id)

    assert repo.get_topic(topic.id).status == READY


@pytest.mark.parametrize("operation", ["run_all", "rerun"])
def test_pipeline_invalidates_topics_before_execution(tmp_path, operation):
    repo, chapter = setup_chapter(tmp_path)
    IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "initial").run_all(
        chapter.id
    )
    published_topic = setup_topic(repo, chapter, published=True)
    unpublished_topic = setup_topic(repo, chapter)
    assert refresh_topic_status(repo, unpublished_topic.id).status == READY
    unrelated_source = repo.create_source(chapter.course_id, "attachment", "/tmp/case.pdf", "案例")
    unrelated_chapter = repo.create_chapter(
        chapter.course_id, unrelated_source.id, 0, "案例", str(tmp_path / "case.md")
    )
    unrelated = setup_topic(repo, unrelated_chapter, published=True)
    repo.update_topic(unrelated.id, status=READY)
    observed_statuses = []

    class AssertingExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key: str, task_package: str) -> str:
            if not observed_statuses:
                observed_statuses.append(
                    (
                        repo.get_topic(unpublished_topic.id).status,
                        repo.get_topic(published_topic.id).status,
                        repo.get_topic(unrelated.id).status,
                    )
                )
            return super().run(round_key, task_package)

    pipeline = IntensiveReadingPipeline(repo, AssertingExecutor(), tmp_path / "changed")
    if operation == "run_all":
        repo.update_chapter_status(chapter.id, "FAILED")
    args = (chapter.id,) if operation == "run_all" else (chapter.id, "concepts")
    getattr(pipeline, operation)(*args)

    assert observed_statuses == [(NOT_READY, STALE, READY)]
    assert repo.get_topic(published_topic.id).status == STALE
    assert repo.get_topic(unrelated.id).status == READY


def test_failed_rerun_makes_unpublished_topic_not_ready(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "initial").run_all(
        chapter.id
    )
    topic = setup_topic(repo, chapter)
    assert refresh_topic_status(repo, topic.id).status == READY

    class FailingExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key: str, task_package: str) -> str:
            raise RuntimeError("executor failed")

    pipeline = IntensiveReadingPipeline(repo, FailingExecutor(), tmp_path / "failed")
    with pytest.raises(RuntimeError, match="executor failed"):
        pipeline.rerun(chapter.id, "concepts")

    assert repo.get_topic(topic.id).status == NOT_READY


def test_failed_rerun_keeps_published_topic_stale_and_outputs(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "initial").run_all(
        chapter.id
    )
    topic = setup_topic(repo, chapter, published=True)
    old_notes = repo.list_topic_note_blocks(topic.id)

    class FailingExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key: str, task_package: str) -> str:
            raise RuntimeError("executor failed")

    pipeline = IntensiveReadingPipeline(repo, FailingExecutor(), tmp_path / "failed")
    with pytest.raises(RuntimeError, match="executor failed"):
        pipeline.rerun(chapter.id, "concepts")

    assert repo.get_topic(topic.id).status == STALE
    assert repo.list_topic_note_blocks(topic.id) == old_notes


@pytest.mark.parametrize(
    ("failure_mode", "error_type"),
    [
        ("missing_source", FileNotFoundError),
        ("mkdir", PermissionError),
        ("write_package", OSError),
        ("executor", RuntimeError),
    ],
)
def test_rerun_records_safe_failed_run_for_preparation_and_execution_errors(
    tmp_path,
    monkeypatch,
    failure_mode,
    error_type,
):
    repo, chapter = setup_chapter(tmp_path)
    IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "initial").run_all(
        chapter.id
    )
    run_dir = tmp_path / "failed"

    class FailingExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key: str, task_package: str) -> str:
            if failure_mode == "executor":
                raise RuntimeError(f"cannot access {tmp_path}/private/model")
            return super().run(round_key, task_package)

    if failure_mode == "missing_source":
        Path(chapter.source_md_path).unlink()
    elif failure_mode == "mkdir":
        original_mkdir = Path.mkdir

        def fail_run_dir_mkdir(path, *args, **kwargs):
            if path == run_dir:
                raise PermissionError(f"cannot create {tmp_path}/private/runs")
            return original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_run_dir_mkdir)
    elif failure_mode == "write_package":

        def fail_write_package(package, base_dir):
            raise OSError(f"cannot write {tmp_path}/private/task.md")

        monkeypatch.setattr(pipeline_module, "write_task_package", fail_write_package)

    pipeline = IntensiveReadingPipeline(repo, FailingExecutor(), run_dir)
    with pytest.raises(error_type):
        pipeline.rerun(chapter.id, "concepts")

    runs = {run.round_key: run for run in repo.list_runs(chapter.id)}
    assert runs["concepts"].status == "FAILED"
    assert runs["concepts"].stale is False
    assert error_type.__name__ in runs["concepts"].output
    assert "intensive reading round failed" in runs["concepts"].output
    assert str(tmp_path) not in runs["concepts"].output
    assert runs["review"].stale is True


@pytest.mark.parametrize("failure_stage", ["executor", "publication"])
def test_pipeline_never_persists_executor_or_publication_exception_details(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    repo, chapter = setup_chapter(tmp_path)
    raw_error = (
        "教材正文：战略就是选择 Authorization: Bearer access-secret "
        "API_KEY=api-secret token=session-secret /Users/private/chapter.md"
    )

    class FailingExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key: str, task_package: str) -> str:
            if failure_stage == "executor":
                raise RuntimeError(raw_error)
            return super().run(round_key, task_package)

    if failure_stage == "publication":

        def fail_publication(*args, **kwargs):
            raise OSError(raw_error)

        monkeypatch.setattr(
            markdown_sync_module,
            "_locked_atomic_write_bundles_fd",
            fail_publication,
        )

    pipeline = IntensiveReadingPipeline(repo, FailingExecutor(), tmp_path / "runs")
    expected_error = RuntimeError if failure_stage == "executor" else ChapterMarkdownSyncError
    with pytest.raises(expected_error):
        pipeline.run_all(chapter.id)

    generation_errors = repo.conn.execute(
        "SELECT error, error_code, error_message "
        "FROM wb_chapter_generation_runs WHERE chapter_id = ? AND error <> ''",
        (chapter.id,),
    ).fetchall()
    publication_errors = repo.conn.execute(
        "SELECT error, error_code, error_message "
        "FROM wb_chapter_generation_publications WHERE chapter_id = ? AND error <> ''",
        (chapter.id,),
    ).fetchall()
    persisted = json.dumps(
        [tuple(row) for row in generation_errors + publication_errors],
        ensure_ascii=False,
    )
    assert generation_errors
    if failure_stage == "publication":
        assert publication_errors
    for forbidden in (
        "战略就是选择",
        "Bearer",
        "access-secret",
        "api-secret",
        "session-secret",
        "/Users/private",
    ):
        assert forbidden not in persisted
