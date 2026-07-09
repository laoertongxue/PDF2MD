from pydantic import BaseModel, Field


class BatchCreateRequest(BaseModel):
    files: list[str] = Field(..., min_length=1)
    concurrency: int = Field(4, ge=1, le=32)
    priority: int = 0


class TaskCreateRequest(BaseModel):
    file_path: str
    model_tier: str = "stub"


class BatchResponse(BaseModel):
    batch_id: str
    task_ids: list[str]
    accepted: int
    rejected: int


TaskResponse = BatchResponse


class BatchStatus(BaseModel):
    batch_id: str
    status: str
    total_tasks: int
    completed_tasks: int
    tasks: list[dict]


class TaskStatus(BaseModel):
    task_id: str
    batch_id: str | None
    status: str
    sections: int
    completed: int
    error_msg: str | None


class WSEvent(BaseModel):
    seq: int
    batch_id: str
    task_id: str | None = None
    event: str
    payload: dict
    ts: int


class CourseCreateRequest(BaseModel):
    title: str
    description: str = ""
    root_dir: str


class CourseResponse(BaseModel):
    id: str
    title: str
    description: str
    root_dir: str


class SourceCreateRequest(BaseModel):
    kind: str = "main"
    file_path: str
    title: str


class SourceResponse(BaseModel):
    id: str
    course_id: str
    kind: str
    file_path: str
    title: str
    status: str


class ChapterResponse(BaseModel):
    id: str
    source_id: str
    course_id: str
    seq: int
    title: str
    status: str


class WorkbenchSettingsResponse(BaseModel):
    deepseek_model: str
    deepseek_key_masked: str | None = None


class DeepSeekSettingsRequest(BaseModel):
    api_key: str
    model: str = "deepseek-chat"


class CardResponse(BaseModel):
    id: str
    course_id: str
    chapter_id: str
    kind: str
    title: str
    body: str
    favorite: bool


class NoteBlockResponse(BaseModel):
    id: str
    chapter_id: str
    kind: str
    title: str
    body: str
    seq: int


class RunChapterRequest(BaseModel):
    executor: str = "stub"
