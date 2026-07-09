import sqlite3
import time
from uuid import uuid4

from parsing_core.workbench.models import Card, Chapter, Course, NoteBlock, Source


def _now() -> int:
    return int(time.time())


class WorkbenchRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

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
