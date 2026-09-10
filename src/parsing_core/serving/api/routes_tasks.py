from pathlib import Path
from typing import cast

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from parsing_core.orchestrator import Orchestrator
from parsing_core.serving.api.deps import SchedulerDep
from parsing_core.serving.models.api import TaskCreateRequest, TaskResponse, TaskStatus

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.post("", response_model=TaskResponse)
async def create_task(req: TaskCreateRequest, sch: SchedulerDep) -> TaskResponse:
    result = await sch.submit_batch([req.file_path], concurrency=1, priority=0)
    return TaskResponse(
        batch_id=result.batch_id,
        task_ids=result.task_ids,
        accepted=result.accepted,
        rejected=result.rejected,
    )


@router.get("/{task_id}", response_model=TaskStatus)
async def get_task(task_id: str, sch: SchedulerDep) -> TaskStatus:
    orch = cast(Orchestrator, sch._query_orch)
    status = orch.status(task_id)
    if status["status"] == "NOT_FOUND":
        raise HTTPException(404, "task not found")
    task = orch.repo.get_task(task_id)
    return TaskStatus(
        task_id=task_id,
        batch_id=task.batch_id if task else None,
        status=cast(str, status["status"]),
        sections=cast(int, status["sections"]),
        completed=cast(int, status["completed"]),
        error_msg=cast(str | None, status.get("error_msg")),
    )


@router.delete("/{task_id}")
async def delete_task(task_id: str, sch: SchedulerDep) -> dict[str, object]:
    result = await sch.delete_task(task_id)
    if not result.get("purged"):
        raise HTTPException(404, "task not found")
    return result


@router.get("/{task_id}/merged", response_class=PlainTextResponse)
async def get_merged_md(task_id: str, sch: SchedulerDep) -> PlainTextResponse:
    orch = cast(Orchestrator, sch._query_orch)
    task = orch.repo.get_task(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    if task.status != "COMPLETED":
        raise HTTPException(
            409,
            detail={"code": "task_not_completed", "status": task.status},
        )
    merged_path = orch.fs.merged_path(task_id)
    p = Path(merged_path)
    if not p.exists():
        raise HTTPException(404, "merged.md not ready")
    return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/markdown")
