import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from parsing_core.llm.base import LLMClient
from parsing_core.models.dataclasses import AIArtifact, Section, Task
from parsing_core.parser.chunker import split_sections
from parsing_core.parser.image_extractor import extract_images
from parsing_core.parser.markitdown_adapter import MarkItDownAdapter
from parsing_core.storage.cache import CacheService
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.utils.file_lock import snapshot
from parsing_core.utils.hashing import file_sha256


class Orchestrator:
    def __init__(
        self,
        repo: Repository,
        fs: FsLayout,
        llm: LLMClient,
        db_path: str,
        on_progress: Callable[[str, str, dict], None] | None = None,
    ) -> None:
        self.repo = repo
        self.fs = fs
        self.llm = llm
        self.db_path = db_path
        self.parser = MarkItDownAdapter()
        self.cache = CacheService(repo)
        self.on_progress = on_progress

    def parse_file(
        self,
        file_path: str,
        force: bool = False,
        task_id: str | None = None,
        batch_id: str | None = None,
    ) -> dict:
        # 1. 副本（永不触碰原文件）
        snap = snapshot(file_path)
        sha = file_sha256(snap)

        # 2. 文件级缓存命中
        if not force:
            hit = self.cache.find_completed_task_by_file_sha256(sha)
            if hit:
                return {
                    "task_id": hit.id,
                    "merged_md_path": self.fs.merged_path(hit.id),
                    "sections": len(self.repo.list_sections(hit.id)),
                    "cached": True,
                    "status": "COMPLETED",
                }

        # 3. 建任务
        task_id = task_id or str(uuid.uuid4())
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
        self.repo.create_task(task)
        self._maybe_progress(task_id, "TASK_STATE", {"status": "PARSING"})

        try:
            # 4. MarkItDown 解析
            raw_md = self.parser.parse(snap)

            # 5. 图片落盘
            images_dir = self.fs.images_dir(task_id)
            raw_md, _imgs = extract_images(raw_md, images_dir)

            # 6. 分节
            self.repo.update_task_status(task_id, "SECTIONING")
            self._maybe_progress(task_id, "TASK_STATE", {"status": "SECTIONING"})
            chunks = split_sections(raw_md)

            # 7. 落原文节到磁盘 + 写 DB
            now = int(time.time())
            for chunk in chunks:
                sid = str(uuid.uuid4())
                raw_path = self.fs.section_raw_path(task_id, chunk.seq)
                Path(raw_path).write_text(chunk.raw, encoding="utf-8")
                sec = Section(
                    id=sid,
                    task_id=task_id,
                    seq=chunk.seq,
                    raw_md_path=raw_path,
                    sha256=chunk.sha256,
                    char_count=chunk.char_count,
                    ai_status="PENDING",
                    created_at=now,
                )
                self.repo.create_section(sec)

            # 8. 节级 LLM 调用（含节级缓存命中复用）
            self.repo.update_task_status(task_id, "LLM_RUNNING")
            self._maybe_progress(task_id, "TASK_STATE", {"status": "LLM_RUNNING"})
            sections = self.repo.list_sections(task_id)
            for sec in sections:
                self._interpret_section(task_id, sec)

            # 9. 合流
            self.repo.update_task_status(task_id, "MERGING")
            self._maybe_progress(task_id, "TASK_STATE", {"status": "MERGING"})
            merged = self._merge(task_id, file_path)
            merged_path = self.fs.merged_path(task_id)
            Path(merged_path).write_text(merged, encoding="utf-8")

            self.repo.update_task_status(task_id, "COMPLETED")
            self._maybe_progress(task_id, "TASK_STATE", {"status": "COMPLETED"})

            # 清理副本
            try:
                Path(snap).unlink()
            except OSError:
                pass

            return {
                "task_id": task_id,
                "merged_md_path": merged_path,
                "sections": len(sections),
                "cached": False,
                "status": "COMPLETED",
            }
        except Exception as e:
            self.repo.update_task_status(task_id, "FAILED", error_msg=str(e))
            self._maybe_progress(task_id, "TASK_STATE", {"status": "FAILED", "error": str(e)})
            raise

    def _interpret_section(self, task_id: str, sec: Section) -> None:
        # 节级缓存命中：复用已有 artifact 的 ai_md_path 落盘
        hit = self.cache.find_completed_artifact_by_section_sha256(sec.sha256)
        if hit:
            ai_path = self.fs.section_ai_path(task_id, sec.seq)
            Path(ai_path).write_text(
                Path(hit.ai_md_path).read_text(encoding="utf-8"), encoding="utf-8"
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
            self.repo.create_artifact(cached)
            self.repo.update_section_ai_status(sec.id, "COMPLETED")
            return

        # 否则调 LLM 落盘
        raw_md = Path(sec.raw_md_path).read_text(encoding="utf-8")
        artifact = self.llm.interpret(sec, raw_md)
        ai_path = self.fs.section_ai_path(task_id, sec.seq)
        Path(ai_path).write_text(artifact.ai_md, encoding="utf-8")
        artifact.ai_md_path = ai_path
        self.repo.create_artifact(artifact)
        self.repo.update_section_ai_status(sec.id, "COMPLETED")

    def _merge(self, task_id: str, original_file_path: str) -> str:
        sections = self.repo.list_sections(task_id)
        out = [
            f"> 任务 ID: {task_id}",
            f"> 源文件: {original_file_path}",
            f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        for s in sections:
            raw = Path(s.raw_md_path).read_text(encoding="utf-8")
            title = self._section_title(s.seq, raw)
            out.append(f"## 第 {s.seq + 1} 节：{title}")
            out.append("")
            out.append(raw.rstrip())
            out.append("")
            artifact = self.repo.get_artifact_by_section(s.id)
            if artifact:
                ai_text = Path(artifact.ai_md_path).read_text(encoding="utf-8")
                out.append(ai_text.rstrip())
            else:
                out.append("### ▸ AI 解读")
                out.append("")
                out.append("⚠ 此节解读失败，可重试。")
            out.append("")
            out.append("---")
            out.append("")
        return "\n".join(out)

    @staticmethod
    def _section_title(seq: int, raw: str) -> str:
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or f"节 {seq + 1}"
        return f"节 {seq + 1}"

    def _maybe_progress(self, task_id: str, event_kind: str, payload: dict) -> None:
        if self.on_progress is None:
            return
        self.on_progress(task_id, event_kind, payload)

    def resume(self, task_id: str) -> dict:
        task = self.repo.get_task(task_id)
        if task is None:
            return {"task_id": task_id, "status": "NOT_FOUND"}

        sections = self.repo.list_sections(task_id)
        pending = [s for s in sections if s.ai_status in ("PENDING", "RUNNING", "FAILED")]
        if task.status == "COMPLETED" and not pending:
            return {"task_id": task_id, "status": "ALREADY_COMPLETED"}

        for s in pending:
            self._interpret_section(task_id, s)

        merged = self._merge(task_id, task.file_path)
        merged_path = self.fs.merged_path(task_id)
        Path(merged_path).write_text(merged, encoding="utf-8")
        self.repo.update_task_status(task_id, "COMPLETED")
        return {
            "task_id": task_id,
            "merged_md_path": merged_path,
            "status": "COMPLETED",
            "sections": len(sections),
        }

    def status(self, task_id: str) -> dict:
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

    def list_all(self) -> list[dict]:
        out = []
        for t in self.repo.list_all_tasks():
            out.append({"task_id": t.id, "status": t.status, "file_path": t.file_path})
        return out

    def purge(self, task_id: str) -> dict:
        task = self.repo.get_task(task_id)
        if task is None:
            return {"task_id": task_id, "purged": False}
        d = self.fs.task_dir(task_id)
        try:
            shutil.rmtree(d)
        except FileNotFoundError:
            pass
        if task.snapshot_path and Path(task.snapshot_path).exists():
            try:
                Path(task.snapshot_path).unlink()
            except OSError:
                pass
        self.repo.delete_task(task_id)
        return {"task_id": task_id, "purged": True}
