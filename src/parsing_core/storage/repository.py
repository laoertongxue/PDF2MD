import sqlite3
import time

from parsing_core.models.dataclasses import AIArtifact, Section, Task


class Repository:
    """封装 tasks/sections/ai_artifacts 三表的 CRUD。

    conn 生命周期由调用方管理，本类不负责 close。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._has_batch_id = "batch_id" in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}

    # --- tasks ---
    def create_task(self, t: Task) -> None:
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
        self.conn.commit()

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
        self.conn.commit()

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

    def delete_task(self, task_id: str) -> None:
        self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.conn.commit()

    # --- sections ---
    def create_section(self, s: Section) -> None:
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
        self.conn.commit()

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
        self.conn.commit()

    # --- ai_artifacts ---
    def create_artifact(self, a: AIArtifact) -> None:
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
        self.conn.commit()

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
        self.conn.commit()

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
    def create_batch(self, b: dict) -> None:
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
        self.conn.commit()

    def get_batch(self, batch_id: str) -> dict | None:
        cur = self.conn.execute(
            "SELECT id, status, concurrency, policy, priority, total_tasks, "
            "completed_tasks, created_at, finished_at FROM batches WHERE id = ?",
            (batch_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "status": row[1],
            "concurrency": row[2],
            "policy": row[3],
            "priority": row[4],
            "total_tasks": row[5],
            "completed_tasks": row[6],
            "created_at": row[7],
            "finished_at": row[8],
        }

    def list_batches_by_status(self, status: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT id, status, concurrency, policy, priority, total_tasks, "
            "completed_tasks, created_at, finished_at FROM batches "
            "WHERE status = ? ORDER BY created_at DESC",
            (status,),
        )
        return [
            {
                "id": r[0],
                "status": r[1],
                "concurrency": r[2],
                "policy": r[3],
                "priority": r[4],
                "total_tasks": r[5],
                "completed_tasks": r[6],
                "created_at": r[7],
                "finished_at": r[8],
            }
            for r in cur.fetchall()
        ]

    def list_all_batches(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT id, status, concurrency, policy, priority, total_tasks, "
            "completed_tasks, created_at, finished_at FROM batches ORDER BY created_at DESC"
        )
        return [
            {
                "id": r[0],
                "status": r[1],
                "concurrency": r[2],
                "policy": r[3],
                "priority": r[4],
                "total_tasks": r[5],
                "completed_tasks": r[6],
                "created_at": r[7],
                "finished_at": r[8],
            }
            for r in cur.fetchall()
        ]

    def update_batch_status(self, batch_id: str, status: str) -> None:
        self.conn.execute("UPDATE batches SET status = ? WHERE id = ?", (status, batch_id))
        self.conn.commit()

    def increment_batch_completed(self, batch_id: str) -> None:
        self.conn.execute(
            "UPDATE batches SET completed_tasks = completed_tasks + 1 WHERE id = ?",
            (batch_id,),
        )
        self.conn.commit()

    def finish_batch(self, batch_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE batches SET status = ?, finished_at = ? WHERE id = ?",
            (status, int(time.time()), batch_id),
        )
        self.conn.commit()

    def set_task_batch_id(self, task_id: str, batch_id: str) -> None:
        self.conn.execute("UPDATE tasks SET batch_id = ? WHERE id = ?", (batch_id, task_id))
        self.conn.commit()
