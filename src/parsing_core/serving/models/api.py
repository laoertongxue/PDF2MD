import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from parsing_core.serving.config import MAX_BATCH_FILES, MAX_BATCH_PATH_LENGTH


class BatchCreateRequest(BaseModel):
    files: list[str] = Field(..., min_length=1, max_length=MAX_BATCH_FILES)
    concurrency: int = Field(4, ge=1, le=32)
    priority: int = 0

    @field_validator("files")
    @classmethod
    def validate_files(cls, files: list[str]) -> list[str]:
        if any(not path or len(path) > MAX_BATCH_PATH_LENGTH for path in files):
            raise ValueError(f"file paths must be 1..{MAX_BATCH_PATH_LENGTH} characters")
        return files


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
    tasks: list[dict[str, object]]


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
    payload: dict[str, object]
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
    anchors: list[dict[str, object]]


class WorkbenchSettingsResponse(BaseModel):
    deepseek_model: str
    deepseek_key_masked: str | None = None
    codex_cli_path: str | None = None
    baidu_key_masked: str | None = None

    @field_validator("deepseek_key_masked", "baidu_key_masked")
    @classmethod
    def ensure_secret_is_masked(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _normalize_secret(value)
        assert normalized is not None
        prefix, marker, suffix = normalized.partition("****")
        if (
            marker
            and 1 <= len(prefix) <= 8
            and len(suffix) == 4
            and "*" not in prefix
            and "*" not in suffix
        ):
            return normalized
        if len(normalized) <= 8:
            return "****"
        return f"{normalized[:3]}****{normalized[-4:]}"


class DeepSeekSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str | None = None
    model: Literal["deepseek-v4-pro"] = "deepseek-v4-pro"

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, api_key: str | None) -> str | None:
        return _normalize_secret(api_key)


class CodexSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)

    @field_validator("path")
    @classmethod
    def validate_path_bytes(cls, path: str) -> str:
        normalized = path.strip()
        try:
            encoded = normalized.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("path must be valid UTF-8") from exc
        if len(encoded) > 4096:
            raise ValueError("path must be at most 4096 UTF-8 bytes")
        if (
            not normalized
            or _has_control_character(normalized)
            or not os.path.isabs(normalized)
            or normalized.startswith("//")
            or os.path.normpath(normalized) != normalized
        ):
            raise ValueError("path must be a canonical absolute path")
        return normalized


class BaiduSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)

    @field_validator("api_key")
    @classmethod
    def validate_api_key_bytes(cls, api_key: str | None) -> str | None:
        return _normalize_secret(api_key)


def _normalize_secret(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("credential must be valid UTF-8") from exc
    if not normalized or _has_control_character(normalized) or len(encoded) > 4096:
        raise ValueError("credential must be 1..4096 safe UTF-8 bytes")
    return normalized


def _has_control_character(value: str) -> bool:
    return any(not character.isprintable() for character in value)


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
