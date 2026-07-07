import pytest
from pydantic import ValidationError

from parsing_core.serving.models.api import (
    BatchCreateRequest,
    BatchResponse,
    TaskCreateRequest,
    TaskResponse,
    TaskStatus,
    WSEvent,
)


def test_batch_create_request_defaults():
    r = BatchCreateRequest(files=["/a/b.md"])
    assert r.concurrency == 4
    assert r.priority == 0


def test_batch_create_request_validates_files():
    with pytest.raises(ValidationError):
        BatchCreateRequest(files=[])


def test_batch_create_request_concurrency_bounds():
    with pytest.raises(ValidationError):
        BatchCreateRequest(files=["/a"], concurrency=0)
    with pytest.raises(ValidationError):
        BatchCreateRequest(files=["/a"], concurrency=33)


def test_batch_response():
    r = BatchResponse(batch_id="b1", task_ids=["t1", "t2"], accepted=2, rejected=0)
    assert r.accepted == 2


def test_task_create_request():
    r = TaskCreateRequest(file_path="/a/b.md")
    assert r.model_tier == "stub"


def test_task_status():
    t = TaskStatus(
        task_id="t1", batch_id="b1", status="COMPLETED", sections=3, completed=3, error_msg=None
    )
    assert t.completed == 3


def test_ws_event_minimal():
    e = WSEvent(seq=0, batch_id="b1", event="BATCH_STATE", payload={"status": "RUNNING"}, ts=0)
    assert e.task_id is None


def test_ws_event_with_task():
    e = WSEvent(
        seq=1,
        batch_id="b1",
        task_id="t1",
        event="TASK_STATE",
        payload={"status": "PARSING"},
        ts=100,
    )
    assert e.task_id == "t1"


def test_task_response_is_batch_response_alias():
    r = TaskResponse(batch_id="b1", task_ids=["t1"], accepted=1, rejected=0)
    assert r.accepted == 1
