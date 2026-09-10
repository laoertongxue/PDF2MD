from fastapi import APIRouter, HTTPException, Query

from parsing_core.serving.api.deps import SchedulerDep
from parsing_core.serving.models.api import BatchCreateRequest, BatchResponse, BatchStatus
from parsing_core.serving.scheduler import SchedulerCapacityError

router = APIRouter(prefix="/api/batches", tags=["batches"])


@router.post("", response_model=BatchResponse)
async def create_batch(req: BatchCreateRequest, sch: SchedulerDep) -> BatchResponse:
    try:
        return await sch.submit_batch(req.files, req.concurrency, req.priority)
    except SchedulerCapacityError as error:
        raise HTTPException(
            status_code=429,
            detail={"code": error.code, "message": error.message},
        ) from error


@router.get("/{batch_id}", response_model=BatchStatus)
async def get_batch(batch_id: str, sch: SchedulerDep) -> BatchStatus:
    batch = sch._query_orch.repo.get_batch(batch_id)
    if batch is None:
        raise HTTPException(404, "batch not found")
    tasks = sch._query_orch.repo.list_all_tasks()
    batch_tasks: list[dict[str, object]] = [
        {"task_id": t.id, "status": t.status, "file_path": t.file_path}
        for t in tasks
        if t.batch_id == batch_id
    ]
    return BatchStatus(
        batch_id=batch_id,
        status=batch["status"],
        total_tasks=batch["total_tasks"],
        completed_tasks=batch["completed_tasks"],
        tasks=batch_tasks,
    )


@router.get("", response_model=list[BatchStatus])
async def list_batches(
    sch: SchedulerDep,
    status: str | None = Query(default=None),
) -> list[BatchStatus]:
    if status:
        batches = sch._query_orch.repo.list_batches_by_status(status)
    else:
        batches = sch._query_orch.repo.list_all_batches()
    all_tasks = sch._query_orch.repo.list_all_tasks()
    return [
        BatchStatus(
            batch_id=b["id"],
            status=b["status"],
            total_tasks=b["total_tasks"],
            completed_tasks=b["completed_tasks"],
            tasks=[
                {"task_id": t.id, "status": t.status, "file_path": t.file_path}
                for t in all_tasks
                if t.batch_id == b["id"]
            ],
        )
        for b in batches
    ]


@router.delete("/{batch_id}")
async def delete_batch(batch_id: str, sch: SchedulerDep) -> dict[str, object]:
    return await sch.cancel_batch(batch_id)
