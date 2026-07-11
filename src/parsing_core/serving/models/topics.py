from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TopicCreateRequest(StrictModel):
    title: str = Field(default="Untitled topic", min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    chapter_ids: list[str] | None = None


class TopicPatchRequest(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_change(self):
        if self.title is None and self.description is None:
            raise ValueError("title or description is required")
        return self


class TopicMappingRequest(StrictModel):
    chapter_ids: list[str] = Field(min_length=1)


class TopicMergeRequest(StrictModel):
    topic_ids: list[str] = Field(min_length=2)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    chapter_ids: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def normalize_and_validate(self):
        self.title = self.title.strip()
        if not self.title or len(self.topic_ids) != len(set(self.topic_ids)):
            raise ValueError("title and unique topic_ids are required")
        return self


class TopicSplitRequest(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    new_chapter_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def normalize_and_validate(self):
        self.title = self.title.strip()
        if not self.title or len(self.new_chapter_ids) != len(set(self.new_chapter_ids)):
            raise ValueError("title and unique new_chapter_ids are required")
        return self


class TopicReorderRequest(StrictModel):
    topic_ids: list[str] = Field(min_length=1)


class TopicGenerateRequest(StrictModel):
    executor: Literal["stub", "deepseek", "hybrid"] = "stub"


class TopicRunRequest(StrictModel):
    executor: Literal["stub"] = "stub"


class TopicResponse(StrictModel):
    id: str
    course_id: str
    seq: int
    title: str
    description: str
    generation_reason: str
    status: str
    confirmed: bool
    stale_reason: str
    chapter_ids: list[str]
    blocking_chapter_ids: list[str]
    sync_status: str
    sync_error: str


class TopicNoteBlockResponse(StrictModel):
    id: str
    topic_id: str
    kind: str
    content: str
    updated_at: int


class TopicCardResponse(StrictModel):
    id: str
    topic_id: str
    card_type: str
    title: str
    content: str
    source_refs: list[str]
    created_at: int


class TopicRunResponse(StrictModel):
    id: str
    topic_id: str
    round_key: str
    status: str
    input_fingerprint: str
    output: str
    error: str
    started_at: int
    finished_at: int | None
