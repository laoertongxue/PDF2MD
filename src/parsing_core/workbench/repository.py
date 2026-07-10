import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from parsing_core.storage.connection_lock import (
    atomic_connection,
    lock_repository_methods,
    register_connection_lock,
)
from parsing_core.workbench.models import (
    Card,
    Chapter,
    Course,
    CourseTopic,
    NoteBlock,
    RunRecord,
    Source,
    TopicCard,
    TopicNoteBlock,
    TopicRunRecord,
)


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


def _normalize_stale_reason(reason: str) -> str:
    reason = reason.strip()
    if not reason or "\n" in reason or "\r" in reason:
        raise ValueError("reason must be a nonempty single line")
    return reason


def _temporary_topic_sequences(existing: list[object], count: int) -> list[int]:
    sqlite_min = -(2**63)
    sqlite_max = 2**63 - 1
    occupied = set(existing) | set(range(count))
    result = []

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

    def create_course(self, title: str, description: str, root_dir: str) -> Course:
        row = {
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
    ) -> CourseTopic:
        now = _now()
        row = {
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
        }
        with self._atomic():
            self.conn.execute(
                """
                INSERT INTO wb_topics
                  (
                    id, course_id, seq, title, description, status, confirmed,
                    stale_reason, created_at, updated_at
                  )
                VALUES
                  (
                    :id, :course_id, :seq, :title, :description, :status, :confirmed,
                    :stale_reason, :created_at, :updated_at
                  )
                """,
                {**row, "confirmed": int(row["confirmed"])},
            )
        return CourseTopic(**row)

    def get_topic(self, topic_id: str) -> CourseTopic | None:
        row = self.conn.execute("SELECT * FROM wb_topics WHERE id = ?", (topic_id,)).fetchone()
        return self._topic(row) if row else None

    def list_topics(self, course_id: str) -> list[CourseTopic]:
        rows = self.conn.execute(
            "SELECT * FROM wb_topics WHERE course_id = ? ORDER BY seq, id",
            (course_id,),
        ).fetchall()
        return [self._topic(row) for row in rows]

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
            for temporary_seq, topic_id in zip(temporary_sequences, current_ids, strict=True):
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
        return self.list_topics(course_id)

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

    def _invalidate_topic(self, row: sqlite3.Row | tuple, reason: str) -> None:
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
        return self.list_topic_note_blocks(topic_id)

    def list_topic_note_blocks(self, topic_id: str) -> list[TopicNoteBlock]:
        rows = self.conn.execute(
            "SELECT * FROM wb_topic_note_blocks WHERE topic_id = ? ORDER BY kind, id",
            (topic_id,),
        ).fetchall()
        return [TopicNoteBlock(*row) for row in rows]

    def replace_topic_cards(self, topic_id: str, cards: list[dict]) -> list[TopicCard]:
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
        return self.list_topic_cards(topic_id)

    def list_topic_cards(self, topic_id: str) -> list[TopicCard]:
        rows = self.conn.execute(
            "SELECT * FROM wb_topic_cards WHERE topic_id = ? ORDER BY created_at, rowid",
            (topic_id,),
        ).fetchall()
        return [TopicCard(*row) for row in rows]

    def create_topic_run(
        self,
        topic_id: str,
        round_key: str,
        input_fingerprint: str,
    ) -> TopicRunRecord:
        row = {
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
        with self._atomic():
            cursor = self.conn.execute(
                """
                UPDATE wb_topic_runs
                SET status = ?, output = ?, error = ?, finished_at = ?
                WHERE id = ? AND status = 'RUNNING'
                """,
                (status, output, error, _now(), run_id),
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

    def _topic(self, row: sqlite3.Row | tuple) -> CourseTopic:
        values = list(row)
        values[6] = bool(values[6])
        return CourseTopic(*values)

    def create_source(self, course_id: str, kind: str, file_path: str, title: str) -> Source:
        row = {
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

    def list_sources(self, course_id: str) -> list[Source]:
        rows = self.conn.execute(
            "SELECT * FROM wb_sources WHERE course_id = ? ORDER BY created_at, id",
            (course_id,),
        ).fetchall()
        return [Source(*row) for row in rows]

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
    ) -> Chapter:
        source = self.conn.execute(
            "SELECT course_id FROM wb_sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        if source is None or source[0] != course_id:
            raise ValueError("source does not belong to course")

        row = {
            "id": uuid4().hex,
            "source_id": source_id,
            "course_id": course_id,
            "seq": seq,
            "title": title,
            "source_md_path": source_md_path,
            "status": "DRAFT",
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.conn.execute(
            """
            INSERT INTO wb_chapters
              (id, source_id, course_id, seq, title, source_md_path, status, created_at, updated_at)
            VALUES
              (
                :id, :source_id, :course_id, :seq, :title, :source_md_path,
                :status, :created_at, :updated_at
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

    def delete_chapters_by_source(self, source_id: str) -> None:
        self.conn.execute("DELETE FROM wb_chapters WHERE source_id = ?", (source_id,))
        self.conn.commit()

    def get_chapter(self, chapter_id: str) -> Chapter | None:
        row = self.conn.execute(
            "SELECT * FROM wb_chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()
        return Chapter(*row) if row else None

    def update_chapter_status(self, chapter_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE wb_chapters SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), chapter_id),
        )
        self.conn.commit()

    def upsert_note_block(
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
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM wb_note_blocks WHERE chapter_id = ? AND kind = ?",
            (chapter_id, kind),
        ).fetchone()
        return NoteBlock(*row)

    def list_note_blocks(self, chapter_id: str) -> list[NoteBlock]:
        rows = self.conn.execute(
            "SELECT * FROM wb_note_blocks WHERE chapter_id = ? ORDER BY seq",
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
    ) -> RunRecord:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO wb_runs
              (
                id, chapter_id, round_key, executor, status, input_path,
                output_path, output, stale, created_at, updated_at
              )
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chapter_id, round_key) DO UPDATE SET
              executor = excluded.executor,
              status = excluded.status,
              input_path = excluded.input_path,
              output_path = excluded.output_path,
              output = excluded.output,
              stale = excluded.stale,
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
            ),
        )
        self.conn.commit()
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
        chapter = self.conn.execute(
            "SELECT course_id FROM wb_chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()
        if chapter is None or chapter[0] != course_id:
            raise ValueError("chapter does not belong to course")

        row = {
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
        self.conn.commit()
        return Card(**row)

    def list_cards(self, course_id: str) -> list[Card]:
        rows = self.conn.execute(
            """
            SELECT * FROM wb_cards
            WHERE course_id = ?
            ORDER BY favorite DESC, updated_at DESC
            """,
            (course_id,),
        ).fetchall()
        return [self._card(row) for row in rows]

    def list_cards_by_chapter(self, chapter_id: str) -> list[Card]:
        rows = self.conn.execute(
            """
            SELECT * FROM wb_cards
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

    def _card(self, row: sqlite3.Row | tuple) -> Card:
        values = list(row)
        values[6] = bool(values[6])
        return Card(*values)

    def _run(self, row: sqlite3.Row | tuple) -> RunRecord:
        values = list(row)
        values[8] = bool(values[8])
        return RunRecord(*values)
