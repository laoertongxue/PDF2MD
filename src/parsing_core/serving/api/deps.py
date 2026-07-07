from typing import Annotated

from fastapi import Depends

from parsing_core.serving.scheduler import Scheduler

_scheduler_singleton: Scheduler | None = None


def set_scheduler(sch: Scheduler) -> None:
    global _scheduler_singleton
    _scheduler_singleton = sch


def get_scheduler() -> Scheduler:
    assert _scheduler_singleton is not None, "Scheduler not initialized"
    return _scheduler_singleton


SchedulerDep = Annotated[Scheduler, Depends(get_scheduler)]
