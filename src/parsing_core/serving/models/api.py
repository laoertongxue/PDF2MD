from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


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


class SourceImportRequest(BaseModel):
    paths: list[str] = Field(..., min_length=1, max_length=50)
    titles: list[str] | None = Field(default=None, max_length=50)

    @field_validator("titles")
    @classmethod
    def validate_titles(
        cls,
        titles: list[str] | None,
        info: ValidationInfo,
    ) -> list[str] | None:
        if titles is None:
            return None
        cleaned = [title.strip() for title in titles]
        if any(not title or len(title) > 120 for title in cleaned):
            raise ValueError("titles must be non-empty and at most 120 characters")
        if len(cleaned) != len(info.data.get("paths", [])):
            raise ValueError("titles must align with paths")
        return cleaned


class ImportedSourceResponse(BaseModel):
    source_id: str
    title: str
    stored_path: str


class SourceImportResponse(BaseModel):
    items: list[ImportedSourceResponse]


class ChapterResponse(BaseModel):
    id: str
    source_id: str
    course_id: str
    seq: int
    title: str
    status: str


class ChapterDraftResponse(ChapterResponse):
    start: int
    end: int


class ChapterDraftSpec(BaseModel):
    id: str | None = None
    title: str = Field(..., min_length=1, max_length=200)
    start: int = Field(..., ge=0)
    end: int = Field(..., gt=0)


class ChapterDraftReplaceRequest(BaseModel):
    expected_fingerprint: str
    chapters: list[ChapterDraftSpec]


class FingerprintRequest(BaseModel):
    expected_fingerprint: str


class ChapterDraftState(BaseModel):
    chapters: list[ChapterDraftResponse]
    fingerprint: str


class AttachmentImportRequest(BaseModel):
    paths: list[str] = Field(..., min_length=1, max_length=50)


class AttachmentResponse(BaseModel):
    id: str
    course_id: str
    source_id: str
    chapter_id: str
    file_path: str
    title: str
    kind: str
    content_hash: str
    anchors: list[dict]


class WorkbenchSettingsResponse(BaseModel):
    deepseek_model: str
    deepseek_key_masked: str | None = None


class DeepSeekSettingsRequest(BaseModel):
    api_key: str | None = None
    model: str = "deepseek-chat"


class CardResponse(BaseModel):
    id: str
    course_id: str
    chapter_id: str
    kind: str
    title: str
    body: str
    favorite: bool


class CourseCardResponse(BaseModel):
    id: str
    origin_type: Literal["chapter", "topic"]
    origin_id: str
    origin_title: str
    card_type: str
    title: str
    content: str
    source_refs: list[str]
    tags: list[str]
    status: Literal["ACTIVE", "ARCHIVED"]
    favorite: bool
    updated_at: int


class CourseCardPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20_000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    status: Literal["ACTIVE", "ARCHIVED"]
    expected_updated_at: int = Field(ge=0)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, tags: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(tag.strip() for tag in tags))
        if any(not tag or len(tag) > 40 for tag in cleaned):
            raise ValueError("tags must be non-empty and at most 40 characters")
        return cleaned


class CourseCardFavoriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    favorite: bool
    expected_updated_at: int = Field(ge=0)


class NoteBlockResponse(BaseModel):
    id: str
    chapter_id: str
    kind: str
    title: str
    body: str
    seq: int


class RunChapterRequest(BaseModel):
    executor: str = "stub"
