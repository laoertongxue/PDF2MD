import sqlite3
import time
from typing import TypedDict

from parsing_core.models.dataclasses import AIArtifact, Section, Task
from parsing_core.storage.connection_lock import (
    atomic_repository_methods,
    lock_repository_methods,
    register_connection_lock,
)

_WRITE_METHODS = (
    "create_task",
    "promote_preregistered_task",
    "materialize_cached_task",
    "rollback_cached_materialization",
    "create_batch_with_tasks",
    "update_task_status",
    "delete_task",
    "create_section",
    "create_sections",
    "update_section_ai_status",
    "create_artifact",
    "complete_section_with_artifact",
    "claim_task_resume",
    "release_task_resume",
    "update_task_status_fenced",
    "replace_sections_with_checkpoint",
    "complete_section_with_artifact_fenced",
    "increment_retry",
    "create_batch",
    "update_batch_status",
    "increment_batch_completed",
    "finish_batch",
    "set_batch_progress",
    "set_task_batch_id",
)


class CachedTaskMaterializationConflict(RuntimeError):
    pass


class TaskPromotionConflict(RuntimeError):
    pass


class ResumeClaimLost(RuntimeError):
    pass


class BatchRecord(TypedDict):
    id: str
    status: str
    concurrency: int
    policy: str
    priority: int
    total_tasks: int
    completed_tasks: int
    created_at: int
    finished_at: int | None


class TaskRecoveryRecord(TypedDict):
    task_id: str
    sectioning_complete: bool
    expected_sections: int
    resume_owner: str | None
    resume_generation: int


@lock_repository_methods
@atomic_repository_methods(_WRITE_METHODS)
class Repository:
    """封装 tasks/sections/ai_artifacts 三表的 CRUD。

    conn 生命周期由调用方管理，本类不负责 close。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._connection_lock, self._connection_lock_finalizer = register_connection_lock(
            self,
            conn,
        )
        with self._connection_lock:
            self._has_batch_id = "batch_id" in {
                row[1] for row in conn.execute("PRAGMA table_info(tasks)")
            }

    # --- tasks ---
    def create_task(self, t: Task) -> None:
        self._promote_or_insert_task(t)

    def _promote_or_insert_task(self, t: Task) -> None:
        if self._has_batch_id and self._try_promote_task(t):
            return
        self._insert_task(t)

    def promote_preregistered_task(self, task: Task) -> None:
        if not self._try_promote_task(task):
            raise TaskPromotionConflict(f"task target {task.id} is missing or no longer eligible")

    def _try_promote_task(self, task: Task) -> bool:
        sql = (
            "UPDATE tasks SET snapshot_path = ?, file_sha256 = ?, status = ?, "
            "model_tier = ?, updated_at = ?, error_msg = ? "
            "WHERE id = ? AND file_path = ? AND status IN ('WAITING', 'PENDING')"
        )
        parameters: tuple[object, ...] = (
            task.snapshot_path,
            task.file_sha256,
            task.status,
            task.model_tier,
            task.updated_at,
            task.error_msg,
            task.id,
            task.file_path,
        )
        if self._has_batch_id:
            sql += " AND (batch_id = ? OR (batch_id IS NULL AND ? IS NULL))"
            parameters += (task.batch_id, task.batch_id)
        return self.conn.execute(sql, parameters).rowcount == 1

    def materialize_cached_task(
        self,
        task: Task,
        sections: list[Section],
        artifacts: list[AIArtifact],
    ) -> None:
        if task.status != "COMPLETED":
            raise ValueError("cached materialization task must be completed")
        if not sections:
            raise ValueError("cached materialization requires at least one section")
        if any(section.ai_status != "COMPLETED" for section in sections):
            raise ValueError("cached materialization sections must be completed")
        section_ids = {section.id for section in sections}
        if len(section_ids) != len(sections):
            raise ValueError("cached materialization contains duplicate section IDs")
        if any(section.task_id != task.id for section in sections):
            raise ValueError("cached materialization section belongs to another task")
        if len(artifacts) != len(sections):
            raise ValueError("cached materialization requires one artifact per section")
        if {artifact.section_id for artifact in artifacts} != section_ids:
            raise ValueError("cached materialization artifact does not match sections")
        self._validate_section_batch(sections)

        self._promote_materialization_target(task)
        for section in sections:
            self._insert_section(section)
        for artifact in artifacts:
            self._insert_artifact(artifact)
        self._set_sectioning_checkpoint(task.id, len(sections))

    def _promote_materialization_target(self, task: Task) -> None:
        if not self._try_promote_task(task):
            raise CachedTaskMaterializationConflict(
                f"cached task target {task.id} is missing or no longer eligible"
            )

    def rollback_cached_materialization(self, task_id: str) -> None:
        self.conn.execute(
            "DELETE FROM ai_artifacts WHERE section_id IN "
            "(SELECT id FROM sections WHERE task_id = ?)",
            (task_id,),
        )
        self.conn.execute("DELETE FROM sections WHERE task_id = ?", (task_id,))
        self.conn.execute(
            "UPDATE task_recovery SET sectioning_complete = 0, expected_sections = 0 "
            "WHERE task_id = ?",
            (task_id,),
        )
        self.conn.execute(
            "UPDATE tasks SET status = 'WAITING', snapshot_path = '', file_sha256 = '', "
            "updated_at = ?, error_msg = NULL WHERE id = ? AND status = 'COMPLETED'",
            (int(time.time()), task_id),
        )

    def _insert_task(self, t: Task) -> None:
        if self._has_batch_id:
            self.conn.execute(
                "INSERT INTO tasks (id, file_path, snapshot_path, file_sha256, status, "
                "model_tier, created_at, updated_at, error_msg, batch_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    t.id,
                    t.file_path,
                    t.snapshot_path,
                    t.file_sha256,
                    t.status,
                    t.model_tier,
                    t.created_at,
                    t.updated_at,
                    t.error_msg,
                    t.batch_id,
                ),
            )
        else:
            self.conn.execute(
                "INSERT INTO tasks (id, file_path, snapshot_path, file_sha256, status, "
                "model_tier, created_at, updated_at, error_msg) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    t.id,
                    t.file_path,
                    t.snapshot_path,
                    t.file_sha256,
                    t.status,
                    t.model_tier,
                    t.created_at,
                    t.updated_at,
                    t.error_msg,
                ),
            )
        self._ensure_task_recovery(t.id)

    def _ensure_task_recovery(self, task_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO task_recovery (task_id) VALUES (?)",
            (task_id,),
        )

    def get_task(self, task_id: str) -> Task | None:
        sql = (
            "SELECT id, file_path, snapshot_path, file_sha256, status, model_tier, "
            "created_at, updated_at, error_msg"
            + (", batch_id" if self._has_batch_id else "")
            + " FROM tasks WHERE id = ?"
        )
        cur = self.conn.execute(sql, (task_id,))
        row = cur.fetchone()
        if not row:
            return None
        return Task(
            id=row[0],
            file_path=row[1],
            snapshot_path=row[2],
            file_sha256=row[3],
            status=row[4],
            model_tier=row[5],
            created_at=row[6],
            updated_at=row[7],
            error_msg=row[8],
            batch_id=row[9] if self._has_batch_id else None,
        )

    def update_task_status(self, task_id: str, status: str, error_msg: str | None = None) -> None:
        self.conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ?, error_msg = ? WHERE id = ?",
            (status, int(time.time()), error_msg, task_id),
        )

    def find_completed_task_by_file_sha256(self, sha: str) -> Task | None:
        sql = (
            "SELECT id, file_path, snapshot_path, file_sha256, status, model_tier, "
            "created_at, updated_at, error_msg"
            + (", batch_id" if self._has_batch_id else "")
            + " FROM tasks WHERE file_sha256 = ? AND status = 'COMPLETED' LIMIT 1"
        )
        cur = self.conn.execute(sql, (sha,))
        row = cur.fetchone()
        if not row:
            return None
        return Task(
            id=row[0],
            file_path=row[1],
            snapshot_path=row[2],
            file_sha256=row[3],
            status=row[4],
            model_tier=row[5],
            created_at=row[6],
            updated_at=row[7],
            error_msg=row[8],
            batch_id=row[9] if self._has_batch_id else None,
        )

    def list_tasks_by_status(self, status: str) -> list[Task]:
        sql = (
            "SELECT id, file_path, snapshot_path, file_sha256, status, model_tier, "
            "created_at, updated_at, error_msg"
            + (", batch_id" if self._has_batch_id else "")
            + " FROM tasks WHERE status = ? ORDER BY created_at DESC"
        )
        cur = self.conn.execute(sql, (status,))
        return [
            Task(
                id=r[0],
                file_path=r[1],
                snapshot_path=r[2],
                file_sha256=r[3],
                status=r[4],
                model_tier=r[5],
                created_at=r[6],
                updated_at=r[7],
                error_msg=r[8],
                batch_id=r[9] if self._has_batch_id else None,
            )
            for r in cur.fetchall()
        ]

    def list_all_tasks(self) -> list[Task]:
        sql = (
            "SELECT id, file_path, snapshot_path, file_sha256, status, model_tier, "
            "created_at, updated_at, error_msg"
            + (", batch_id" if self._has_batch_id else "")
            + " FROM tasks ORDER BY created_at DESC"
        )
        cur = self.conn.execute(sql)
        return [
            Task(
                id=r[0],
                file_path=r[1],
                snapshot_path=r[2],
                file_sha256=r[3],
                status=r[4],
                model_tier=r[5],
                created_at=r[6],
                updated_at=r[7],
                error_msg=r[8],
                batch_id=r[9] if self._has_batch_id else None,
            )
            for r in cur.fetchall()
        ]

    def list_waiting_tasks_page(
        self,
        *,
        after_id: str | None,
        limit: int,
    ) -> list[Task]:
        if type(limit) is not int or not 1 <= limit <= 512:
            raise ValueError("limit must be between 1 and 512")
        if after_id is not None and type(after_id) is not str:
            raise ValueError("after_id must be a string or None")
        sql = (
            "SELECT id, file_path, snapshot_path, file_sha256, status, model_tier, "
            "created_at, updated_at, error_msg"
            + (", batch_id" if self._has_batch_id else "")
            + " FROM tasks WHERE status IN ('WAITING', 'PENDING') "
        )
        parameters: tuple[object, ...]
        if after_id is None:
            parameters = (limit,)
        else:
            sql += "AND id > ? "
            parameters = (after_id, limit)
        sql += "ORDER BY id ASC LIMIT ?"
        rows = self.conn.execute(sql, parameters).fetchall()
        return [
            Task(
                id=row[0],
                file_path=row[1],
                snapshot_path=row[2],
                file_sha256=row[3],
                status=row[4],
                model_tier=row[5],
                created_at=row[6],
                updated_at=row[7],
                error_msg=row[8],
                batch_id=row[9] if self._has_batch_id else None,
            )
            for row in rows
        ]

    def list_task_ids_with_materialized_children(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT task_id FROM sections "
            "UNION SELECT s.task_id FROM ai_artifacts a "
            "JOIN sections s ON s.id = a.section_id "
            "UNION SELECT task_id FROM task_recovery WHERE resume_owner IS NOT NULL"
        ).fetchall()
        return {str(row[0]) for row in rows}

    def delete_task(self, task_id: str) -> None:
        self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    # --- recovery / resume fencing ---
    def get_task_recovery(self, task_id: str) -> TaskRecoveryRecord | None:
        row = self.conn.execute(
            "SELECT task_id, sectioning_complete, expected_sections, resume_owner, "
            "resume_generation FROM task_recovery WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return TaskRecoveryRecord(
            task_id=row[0],
            sectioning_complete=bool(row[1]),
            expected_sections=row[2],
            resume_owner=row[3],
            resume_generation=row[4],
        )

    def list_claimed_task_recoveries(self) -> list[TaskRecoveryRecord]:
        rows = self.conn.execute(
            "SELECT task_id, sectioning_complete, expected_sections, resume_owner, "
            "resume_generation FROM task_recovery WHERE resume_owner IS NOT NULL"
        ).fetchall()
        return [
            TaskRecoveryRecord(
                task_id=row[0],
                sectioning_complete=bool(row[1]),
                expected_sections=row[2],
                resume_owner=row[3],
                resume_generation=row[4],
            )
            for row in rows
        ]

    def claim_task_resume(self, task_id: str, owner: str) -> int | None:
        if not owner:
            raise ValueError("resume owner must not be empty")
        self._ensure_task_recovery(task_id)
        claimed = self.conn.execute(
            "UPDATE task_recovery SET resume_owner = ?, "
            "resume_generation = resume_generation + 1 "
            "WHERE task_id = ? AND resume_owner IS NULL",
            (owner, task_id),
        )
        if claimed.rowcount != 1:
            return None
        row = self.conn.execute(
            "SELECT resume_generation FROM task_recovery WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise ResumeClaimLost("resume claim disappeared after acquisition")
        return int(row[0])

    def release_task_resume(self, task_id: str, owner: str, generation: int) -> bool:
        released = self.conn.execute(
            "UPDATE task_recovery SET resume_owner = NULL "
            "WHERE task_id = ? AND resume_owner = ? AND resume_generation = ?",
            (task_id, owner, generation),
        )
        return released.rowcount == 1

    def update_task_status_fenced(
        self,
        task_id: str,
        status: str,
        owner: str,
        generation: int,
        error_msg: str | None = None,
    ) -> None:
        updated = self.conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ?, error_msg = ? "
            "WHERE id = ? AND EXISTS ("
            "SELECT 1 FROM task_recovery r WHERE r.task_id = tasks.id "
            "AND r.resume_owner = ? AND r.resume_generation = ?)",
            (status, int(time.time()), error_msg, task_id, owner, generation),
        )
        if updated.rowcount != 1:
            raise ResumeClaimLost("resume claim no longer owns task status")

    def _assert_resume_claim(self, task_id: str, owner: str, generation: int) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM task_recovery "
            "WHERE task_id = ? AND resume_owner = ? AND resume_generation = ?",
            (task_id, owner, generation),
        ).fetchone()
        if row is None:
            raise ResumeClaimLost("resume claim no longer owns task")

    # --- sections ---
    def create_section(self, s: Section) -> None:
        self._insert_section(s)

    def create_sections(self, sections: list[Section]) -> None:
        task_id = self._validate_section_batch(sections)
        for section in sections:
            self._insert_section(section)
        self._set_sectioning_checkpoint(task_id, len(sections))

    @staticmethod
    def _validate_section_batch(sections: list[Section]) -> str:
        if not sections:
            raise ValueError("section batch must not be empty")
        task_id = sections[0].task_id
        if any(section.task_id != task_id for section in sections):
            raise ValueError("section batch must belong to one task")
        seqs = [section.seq for section in sections]
        if len(set(seqs)) != len(seqs):
            raise ValueError("section batch sequence numbers must be unique")
        if sorted(seqs) != list(range(len(sections))):
            raise ValueError("section batch sequence numbers must be contiguous from zero")
        return task_id

    def _set_sectioning_checkpoint(self, task_id: str, expected_sections: int) -> None:
        self._ensure_task_recovery(task_id)
        updated = self.conn.execute(
            "UPDATE task_recovery SET sectioning_complete = 1, expected_sections = ? "
            "WHERE task_id = ?",
            (expected_sections, task_id),
        )
        if updated.rowcount != 1:
            raise ValueError(f"task {task_id} has no recovery checkpoint")

    def replace_sections_with_checkpoint(
        self,
        task_id: str,
        sections: list[Section],
        owner: str,
        generation: int,
    ) -> None:
        section_task_id = self._validate_section_batch(sections)
        if section_task_id != task_id:
            raise ValueError("replacement sections belong to another task")
        self._assert_resume_claim(task_id, owner, generation)
        self.conn.execute("DELETE FROM sections WHERE task_id = ?", (task_id,))
        reset = self.conn.execute(
            "UPDATE task_recovery SET sectioning_complete = 0, expected_sections = 0 "
            "WHERE task_id = ? AND resume_owner = ? AND resume_generation = ?",
            (task_id, owner, generation),
        )
        if reset.rowcount != 1:
            raise ResumeClaimLost("resume claim lost while resetting section checkpoint")
        for section in sections:
            self._insert_section(section)
        completed = self.conn.execute(
            "UPDATE task_recovery SET sectioning_complete = 1, expected_sections = ? "
            "WHERE task_id = ? AND resume_owner = ? AND resume_generation = ?",
            (len(sections), task_id, owner, generation),
        )
        if completed.rowcount != 1:
            raise ResumeClaimLost("resume claim lost while completing section checkpoint")

    def _insert_section(self, s: Section) -> None:
        self.conn.execute(
            "INSERT INTO sections (id, task_id, seq, raw_md_path, sha256, char_count, "
            "ai_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s.id,
                s.task_id,
                s.seq,
                s.raw_md_path,
                s.sha256,
                s.char_count,
                s.ai_status,
                s.created_at,
            ),
        )

    def list_sections(self, task_id: str) -> list[Section]:
        cur = self.conn.execute(
            "SELECT id, task_id, seq, raw_md_path, sha256, char_count, ai_status, created_at "
            "FROM sections WHERE task_id = ? ORDER BY seq",
            (task_id,),
        )
        return [
            Section(
                id=r[0],
                task_id=r[1],
                seq=r[2],
                raw_md_path=r[3],
                sha256=r[4],
                char_count=r[5],
                ai_status=r[6],
                created_at=r[7],
            )
            for r in cur.fetchall()
        ]

    def get_section(self, section_id: str) -> Section | None:
        cur = self.conn.execute(
            "SELECT id, task_id, seq, raw_md_path, sha256, char_count, ai_status, created_at "
            "FROM sections WHERE id = ?",
            (section_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return Section(
            id=row[0],
            task_id=row[1],
            seq=row[2],
            raw_md_path=row[3],
            sha256=row[4],
            char_count=row[5],
            ai_status=row[6],
            created_at=row[7],
        )

    def update_section_ai_status(self, section_id: str, status: str) -> None:
        self.conn.execute("UPDATE sections SET ai_status = ? WHERE id = ?", (status, section_id))

    # --- ai_artifacts ---
    def create_artifact(self, a: AIArtifact) -> None:
        self._insert_artifact(a)

    def complete_section_with_artifact(
        self,
        artifact: AIArtifact,
        *,
        artifact_already_persisted: bool = False,
    ) -> None:
        self._complete_section_with_artifact(
            artifact,
            artifact_already_persisted=artifact_already_persisted,
        )

    def complete_section_with_artifact_fenced(
        self,
        artifact: AIArtifact,
        task_id: str,
        owner: str,
        generation: int,
        *,
        artifact_already_persisted: bool = False,
    ) -> None:
        self._assert_resume_claim(task_id, owner, generation)
        section = self.conn.execute(
            "SELECT task_id FROM sections WHERE id = ?",
            (artifact.section_id,),
        ).fetchone()
        if section is None or section[0] != task_id:
            raise ValueError("artifact section does not belong to the resumed task")
        self._complete_section_with_artifact(
            artifact,
            artifact_already_persisted=artifact_already_persisted,
        )

    def _complete_section_with_artifact(
        self,
        artifact: AIArtifact,
        *,
        artifact_already_persisted: bool,
    ) -> None:
        section_exists = self.conn.execute(
            "SELECT 1 FROM sections WHERE id = ?",
            (artifact.section_id,),
        ).fetchone()
        if section_exists is None:
            raise ValueError(f"section {artifact.section_id} does not exist")

        if artifact_already_persisted:
            stored = self.conn.execute(
                "SELECT id, section_id, ai_md_path, tokens_in, tokens_out, cost_usd, "
                "retry_count, model_name, created_at FROM ai_artifacts "
                "WHERE id = ? AND section_id = ?",
                (artifact.id, artifact.section_id),
            ).fetchone()
            expected = (
                artifact.id,
                artifact.section_id,
                artifact.ai_md_path,
                artifact.tokens_in,
                artifact.tokens_out,
                artifact.cost_usd,
                artifact.retry_count,
                artifact.model_name,
                artifact.created_at,
            )
            if stored != expected:
                raise ValueError("persisted artifact no longer matches the section")
        else:
            self.conn.execute(
                "DELETE FROM ai_artifacts WHERE section_id = ?",
                (artifact.section_id,),
            )
            self._insert_artifact(artifact)

        updated = self.conn.execute(
            "UPDATE sections SET ai_status = 'COMPLETED' WHERE id = ?",
            (artifact.section_id,),
        )
        if updated.rowcount != 1:
            raise ValueError(f"section {artifact.section_id} does not exist")

    def _insert_artifact(self, a: AIArtifact) -> None:
        self.conn.execute(
            "INSERT INTO ai_artifacts (id, section_id, ai_md_path, tokens_in, tokens_out, "
            "cost_usd, retry_count, model_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                a.id,
                a.section_id,
                a.ai_md_path,
                a.tokens_in,
                a.tokens_out,
                a.cost_usd,
                a.retry_count,
                a.model_name,
                a.created_at,
            ),
        )

    def get_artifact_by_section(self, section_id: str) -> AIArtifact | None:
        cur = self.conn.execute(
            "SELECT id, section_id, ai_md_path, tokens_in, tokens_out, cost_usd, "
            "retry_count, model_name, created_at FROM ai_artifacts WHERE section_id = ?",
            (section_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return AIArtifact(
            id=row[0],
            section_id=row[1],
            ai_md_path=row[2],
            ai_md="",
            tokens_in=row[3],
            tokens_out=row[4],
            cost_usd=row[5],
            retry_count=row[6],
            model_name=row[7],
            created_at=row[8],
        )

    def increment_retry(self, artifact_id: str) -> None:
        self.conn.execute(
            "UPDATE ai_artifacts SET retry_count = retry_count + 1 WHERE id = ?",
            (artifact_id,),
        )

    def find_completed_artifact_by_section_sha256(self, sha: str) -> AIArtifact | None:
        cur = self.conn.execute(
            "SELECT a.id, a.section_id, a.ai_md_path, a.tokens_in, a.tokens_out, a.cost_usd, "
            "a.retry_count, a.model_name, a.created_at FROM ai_artifacts a "
            "JOIN sections s ON a.section_id = s.id "
            "WHERE s.sha256 = ? AND s.ai_status = 'COMPLETED' LIMIT 1",
            (sha,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return AIArtifact(
            id=row[0],
            section_id=row[1],
            ai_md_path=row[2],
            ai_md="",
            tokens_in=row[3],
            tokens_out=row[4],
            cost_usd=row[5],
            retry_count=row[6],
            model_name=row[7],
            created_at=row[8],
        )

    # --- batches ---
    def create_batch(self, b: BatchRecord) -> None:
        self._insert_batch(b)

    def create_batch_with_tasks(self, b: BatchRecord, tasks: list[Task]) -> None:
        self._insert_batch(b)
        for task in tasks:
            self._insert_task(task)

    def _insert_batch(self, b: BatchRecord) -> None:
        self.conn.execute(
            "INSERT INTO batches (id, status, concurrency, policy, priority, "
            "total_tasks, completed_tasks, created_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                b["id"],
                b["status"],
                b["concurrency"],
                b["policy"],
                b["priority"],
                b["total_tasks"],
                b["completed_tasks"],
                b["created_at"],
                b["finished_at"],
            ),
        )

    def get_batch(self, batch_id: str) -> BatchRecord | None:
        cur = self.conn.execute(
            "SELECT id, status, concurrency, policy, priority, total_tasks, "
            "completed_tasks, created_at, finished_at FROM batches WHERE id = ?",
            (batch_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return BatchRecord(
            id=row[0],
            status=row[1],
            concurrency=row[2],
            policy=row[3],
            priority=row[4],
            total_tasks=row[5],
            completed_tasks=row[6],
            created_at=row[7],
            finished_at=row[8],
        )

    def list_batches_by_status(self, status: str) -> list[BatchRecord]:
        cur = self.conn.execute(
            "SELECT id, status, concurrency, policy, priority, total_tasks, "
            "completed_tasks, created_at, finished_at FROM batches "
            "WHERE status = ? ORDER BY created_at DESC",
            (status,),
        )
        return [
            BatchRecord(
                id=r[0],
                status=r[1],
                concurrency=r[2],
                policy=r[3],
                priority=r[4],
                total_tasks=r[5],
                completed_tasks=r[6],
                created_at=r[7],
                finished_at=r[8],
            )
            for r in cur.fetchall()
        ]

    def list_all_batches(self) -> list[BatchRecord]:
        cur = self.conn.execute(
            "SELECT id, status, concurrency, policy, priority, total_tasks, "
            "completed_tasks, created_at, finished_at FROM batches ORDER BY created_at DESC"
        )
        return [
            BatchRecord(
                id=r[0],
                status=r[1],
                concurrency=r[2],
                policy=r[3],
                priority=r[4],
                total_tasks=r[5],
                completed_tasks=r[6],
                created_at=r[7],
                finished_at=r[8],
            )
            for r in cur.fetchall()
        ]

    def update_batch_status(self, batch_id: str, status: str) -> None:
        self.conn.execute("UPDATE batches SET status = ? WHERE id = ?", (status, batch_id))

    def increment_batch_completed(self, batch_id: str) -> None:
        self.conn.execute(
            "UPDATE batches SET completed_tasks = completed_tasks + 1 WHERE id = ?",
            (batch_id,),
        )

    def finish_batch(self, batch_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE batches SET status = ?, finished_at = ? WHERE id = ?",
            (status, int(time.time()), batch_id),
        )

    def set_batch_progress(
        self,
        batch_id: str,
        completed: int,
        status: str | None = None,
    ) -> None:
        if completed < 0:
            raise ValueError("completed must be non-negative")
        cursor = self.conn.execute(
            "UPDATE batches SET "
            "completed_tasks = MAX(completed_tasks, ?), "
            "status = CASE "
            "WHEN status = 'CANCELLED' OR ? = 'CANCELLED' THEN 'CANCELLED' "
            "WHEN status = 'FAILED' OR ? = 'FAILED' THEN 'FAILED' "
            "WHEN status = 'COMPLETED' OR ? = 'COMPLETED' THEN 'COMPLETED' "
            "WHEN ? IS NOT NULL THEN ? ELSE status END, "
            "finished_at = CASE WHEN ? IS NOT NULL "
            "THEN COALESCE(finished_at, ?) ELSE finished_at END "
            "WHERE id = ?",
            (
                completed,
                status,
                status,
                status,
                status,
                status,
                status,
                int(time.time()),
                batch_id,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"batch not found: {batch_id}")

    def set_task_batch_id(self, task_id: str, batch_id: str) -> None:
        self.conn.execute("UPDATE tasks SET batch_id = ? WHERE id = ?", (batch_id, task_id))
