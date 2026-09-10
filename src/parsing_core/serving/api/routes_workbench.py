from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from parsing_core.parser.markitdown_adapter import MarkItDownAdapter
from parsing_core.serving.api.deps import SchedulerDep
from parsing_core.serving.models.api import (
    AttachmentImportRequest,
    AttachmentResponse,
    ChapterDraftReplaceRequest,
    ChapterDraftResponse,
    ChapterDraftState,
    ChapterResponse,
    CourseCardFavoriteRequest,
    CourseCardPatchRequest,
    CourseCardResponse,
    CourseCreateRequest,
    CourseResponse,
    DeepSeekSettingsRequest,
    FingerprintRequest,
    ImportedSourceResponse,
    NoteBlockResponse,
    RunChapterRequest,
    SourceCreateRequest,
    SourceImportRequest,
    SourceImportResponse,
    SourceResponse,
    WorkbenchSettingsResponse,
)
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.workbench.chapter_detection import detect_chapters
from parsing_core.workbench.codex_cli import CodexCliError, CodexCliExecutor, resolve_codex_path
from parsing_core.workbench.deepseek import DeepSeekClient, DeepSeekError, DeepSeekExecutor
from parsing_core.workbench.executors import (
    IntensiveReadingExecutor,
    StubIntensiveReadingExecutor,
)
from parsing_core.workbench.hybrid import HybridIntensiveReadingExecutor
from parsing_core.workbench.keychain import KeychainError, mask_secret, read_secret, save_secret
from parsing_core.workbench.markdown_sync import (
    ChapterMarkdownSyncError,
    sync_chapter_markdown,
)
from parsing_core.workbench.models import Chapter, Course, NoteBlock, Source, TopicNoteBlock
from parsing_core.workbench.ocr.baidu import BaiduOcrClient
from parsing_core.workbench.ocr.chapters import (
    load_chapter_confirmation,
    validate_chapter_confirmation,
)
from parsing_core.workbench.ocr.codex_vision import CodexVisionExecutor
from parsing_core.workbench.ocr.deepseek_intensive_reading import (
    DeepSeekIntensiveReadingGenerator,
)
from parsing_core.workbench.ocr.markdown_notes import build_intensive_reading_note
from parsing_core.workbench.ocr.orchestrator import (
    CancellationEvent,
    CancellationSignal,
    OcrOrchestrator,
)
from parsing_core.workbench.ocr.vision import RegisteredPdfSources, VisionClient
from parsing_core.workbench.ocr.workflow import OcrWorkflow
from parsing_core.workbench.pipeline import (
    FIXED_CHAPTER_KINDS,
    IntensiveReadingPipeline,
)
from parsing_core.workbench.repository import ChapterGenerationConflictError, WorkbenchRepository
from parsing_core.workbench.schema import CHAPTER_SYNC_PENDING
from parsing_core.workbench.settings import (
    WorkbenchSettings,
    load_settings,
    update_settings_fields,
)
from parsing_core.workbench.source_import import (
    ATTACHMENT_DIRECTORY_NAME,
    AtomicImportUnsupportedError,
    CourseStorageChangedError,
    CourseStorageError,
    SourceImportInputError,
    TextbookImportBatch,
    parse_imported_source,
)
from parsing_core.workbench.topic_markdown_sync import TopicMarkdownSyncError
from parsing_core.workbench.topic_pipeline import (
    FIXED_TOPIC_KINDS,
    TopicFusionPipeline,
    validate_mermaid_subset,
)

router = APIRouter(prefix="/api/workbench", tags=["workbench"])
KEYCHAIN_SERVICE = "pdf2md.deepseek"
KEYCHAIN_ACCOUNT = "api-key"
_OCR_WORKFLOWS: dict[str, OcrWorkflow] = {}
_OCR_WORKFLOWS_LOCK = threading.Lock()
_UNSAFE_MARKDOWN_RE = re.compile(
    r"(?ims)<\s*/?\s*(?:script|iframe|object|embed|svg|math)\b|"
    r"(?:\]\(\s*|^\s*\[[^\]\r\n]+\]\s*:\s*|(?:href|src)\s*=\s*['\"]?\s*|<\s*)"
    r"(?:javascript|vbscript|data)\s*:|\bon\w+\s*="
)


class _CoreRepository(Protocol):
    conn: sqlite3.Connection


class _CoreFileLayout(Protocol):
    base_dir: str


class _WorkbenchQueryOrchestrator(Protocol):
    repo: _CoreRepository
    fs: _CoreFileLayout


class _WorkbenchScheduler(Protocol):
    _query_orch: _WorkbenchQueryOrchestrator


class _RecognizeClient(Protocol):
    def recognize(self, *args: object, **kwargs: object) -> object: ...


class _TranscribeClient(Protocol):
    def transcribe_page(self, *args: object, **kwargs: object) -> object: ...


class _AdjudicateClient(Protocol):
    def adjudicate_page(self, *args: object, **kwargs: object) -> object: ...


class TopicBlockPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=20_000)
    expected_content: str = Field(max_length=20_000)


class TopicBlockPatchResponse(BaseModel):
    id: str
    topic_id: str
    kind: str
    content: str
    updated_at: int


class ChapterBlockPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=20_000)
    expected_body: str = Field(max_length=20_000)


def _query_orchestrator(sch: SchedulerDep) -> _WorkbenchQueryOrchestrator:
    return cast(_WorkbenchScheduler, sch)._query_orch


def _repo(sch: SchedulerDep) -> WorkbenchRepository:
    return WorkbenchRepository(_query_orchestrator(sch).repo.conn)


def _require_safe_markdown(value: str) -> None:
    if _UNSAFE_MARKDOWN_RE.search(value):
        raise HTTPException(422, {"code": "unsafe_markdown"})


def _settings_root(sch: SchedulerDep) -> FsLayout:
    fs = _query_orchestrator(sch).fs
    if type(fs) is not FsLayout:
        raise RuntimeError("workbench settings require the bound filesystem layout")
    return fs


def save_settings(
    trusted_root: FsLayout,
    settings: WorkbenchSettings,
) -> WorkbenchSettings:
    """Merge the route-owned model field without replacing unrelated settings."""

    return update_settings_fields(
        trusted_root,
        deepseek_model=settings.deepseek_model,
    )


def _read_configured_deepseek_key() -> str:
    try:
        api_key = read_secret(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    except KeychainError as exc:
        raise HTTPException(400, "deepseek api key not configured") from exc
    if not api_key.strip():
        raise HTTPException(400, "deepseek api key not configured")
    return api_key.strip()


def _release_chapter_generation_claim(
    repo: WorkbenchRepository,
    chapter_id: str,
    owner_id: str,
    *,
    error: str,
) -> None:
    try:
        repo.release_chapter_generation(chapter_id, owner_id, error=error)
    except ChapterGenerationConflictError:
        pass


def _read_masked_deepseek_key() -> str | None:
    try:
        api_key = read_secret(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    except KeychainError:
        return None
    return mask_secret(api_key.strip()) if api_key.strip() else None


def _read_masked_baidu_key() -> str | None:
    api_key = os.environ.get("PDF2MD_BAIDU_API_KEY", "").strip()
    return mask_secret(api_key) if api_key else None


def _resolve_inside(path: str, base: Path) -> Path:
    try:
        resolved = Path(path).expanduser().resolve()
        resolved_base = base.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(400, {"code": "path_escape"}) from None
    if not resolved.is_relative_to(resolved_base):
        raise HTTPException(400, {"code": "path_escape"})
    return resolved


def _resolve_absolute_dir(path: str) -> Path:
    raw = Path(path)
    if not raw.is_absolute():
        raise HTTPException(400, "root_dir must be an absolute path")
    expanded = raw.expanduser()
    try:
        return expanded.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(400, "root_dir is invalid") from exc


def _course_response(course: Course) -> CourseResponse:
    return CourseResponse(
        id=course.id,
        title=course.title,
        description=course.description,
        root_dir=course.root_dir,
    )


def _source_response(source: Source) -> SourceResponse:
    return SourceResponse(
        id=source.id,
        course_id=source.course_id,
        kind=source.kind,
        file_path=source.file_path,
        title=source.title,
        status=source.status,
    )


def _ocr_state_root(course: Course, source: Source) -> Path:
    return Path(course.root_dir) / ".pdf2md" / "ocr" / source.id


def _find_vision_helper() -> Path:
    candidates = []
    configured = os.environ.get("PDF2MD_VISION_HELPER")
    if configured:
        candidates.append(Path(configured))
    resources = os.environ.get("PDF2MD_RESOURCES")
    if resources:
        resources_path = Path(resources)
        candidates.extend(
            [resources_path / "vision-ocr", resources_path / "vision-ocr-aarch64-apple-darwin"]
        )
    project_root = Path(__file__).resolve().parents[4]
    candidates.extend(
        [
            project_root / "parsing-core-app/src-tauri/binaries/vision-ocr-aarch64-apple-darwin",
            project_root / "parsing-core-app/src-tauri/binaries/vision-ocr",
        ]
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("Apple Vision OCR helper is not available")


class _DeadlineAdapter:
    def __init__(self, client: object) -> None:
        self.client = client

    def recognize(self, *args: object, **kwargs: object) -> object:
        return cast(_RecognizeClient, self.client).recognize(*args, **kwargs)

    def transcribe_page(self, *args: object, **kwargs: object) -> object:
        return cast(_TranscribeClient, self.client).transcribe_page(*args, **kwargs)

    def adjudicate_page(self, *args: object, **kwargs: object) -> object:
        return cast(_AdjudicateClient, self.client).adjudicate_page(*args, **kwargs)


def _load_ocr_image(
    path: str | Path,
    *,
    deadline: float | None = None,
    cancel_event: CancellationEvent | None = None,
) -> bytes:
    return OcrOrchestrator._load_image(path, deadline=deadline, cancel_event=cancel_event)


def _ocr_workflow(source: Source, course: Course) -> OcrWorkflow:
    with _OCR_WORKFLOWS_LOCK:
        existing = _OCR_WORKFLOWS.get(source.id)
        if existing is not None:
            return existing
        pdf_path = Path(source.file_path)
        if pdf_path.suffix.lower() != ".pdf":
            raise HTTPException(422, "无人值守 OCR 当前只接受 PDF 教材")
        state_root = _ocr_state_root(course, source)

        def factory(is_cancelled: Callable[[], bool]) -> OcrOrchestrator:
            cancel_signal = CancellationSignal(is_cancelled)
            try:
                helper = _find_vision_helper()
                codex_path = resolve_codex_path()
                baidu_key = os.environ.get("PDF2MD_BAIDU_API_KEY", "").strip()
                if not baidu_key:
                    raise RuntimeError("百度 OCR 未配置，任务已阻断")
                validator = RegisteredPdfSources([pdf_path])
                vision = VisionClient(
                    helper_path=helper,
                    cache_root=state_root / "cache",
                    source_validator=validator,
                    helper_version="bundled-vision",
                    timeout=90,
                )
                codex = CodexVisionExecutor(
                    codex_path=codex_path,
                    temp_root=state_root / "codex-tmp",
                    trusted_image_root=vision.cache.pages_dir,
                    timeout=180,
                    cancel_event=cancel_signal,
                )
                baidu = BaiduOcrClient(api_key=baidu_key)
            except Exception as exc:
                if cancel_signal.is_set():
                    raise RuntimeError("OCR cancelled") from None
                from parsing_core.workbench.ocr.workflow import WorkflowBlockedError

                raise WorkflowBlockedError(str(exc)) from exc
            return OcrOrchestrator(
                vision=_DeadlineAdapter(vision),
                codex=_DeadlineAdapter(codex),
                baidu=_DeadlineAdapter(baidu),
                state_root=state_root,
                image_loader=_load_ocr_image,
                is_cancelled=is_cancelled,
                cancel_event=cancel_signal,
            )

        workflow = OcrWorkflow(
            source_path=pdf_path, state_root=state_root, orchestrator_factory=factory
        )
        _OCR_WORKFLOWS[source.id] = workflow
        return workflow


class OcrChapterConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapter_id: str = Field(min_length=1, max_length=128)


class _CourseNotFoundError(Exception):
    pass


class _SourceSaveError(Exception):
    pass


def _import_sources_sync(
    course_id: str,
    paths: list[str],
    titles: list[str] | None,
    sch: SchedulerDep,
) -> SourceImportResponse:
    repo = _repo(sch)
    course = repo.get_course(course_id)
    if course is None:
        raise _CourseNotFoundError

    with TextbookImportBatch(
        Path(course.root_dir),
        repo.source_file_paths_for_root(course.root_dir),
    ) as batch:
        imported_textbooks = [batch.import_file(Path(path)) for path in paths]
        try:
            sources = repo.create_sources_guarded(
                course_id,
                [
                    (
                        "main",
                        str(imported.stored_path),
                        titles[index] if titles is not None else imported.title,
                    )
                    for index, imported in enumerate(imported_textbooks)
                ],
                batch.verify_path_identity,
            )
        except CourseStorageChangedError:
            raise
        except Exception as exc:
            raise _SourceSaveError from exc
        batch.commit()

    return SourceImportResponse(
        items=[
            ImportedSourceResponse(
                source_id=source.id,
                title=source.title,
                stored_path=source.file_path,
            )
            for source in sources
        ]
    )


def _chapter_response(chapter: Chapter) -> ChapterResponse:
    return ChapterResponse(
        id=chapter.id,
        source_id=chapter.source_id,
        course_id=chapter.course_id,
        seq=chapter.seq,
        title=chapter.title,
        status=chapter.status,
    )


def _note_block_response(block: NoteBlock) -> NoteBlockResponse:
    return NoteBlockResponse(
        id=block.id,
        chapter_id=block.chapter_id,
        kind=block.kind,
        title=block.title,
        body=block.body,
        seq=block.seq,
    )


def _topic_block_response(block: TopicNoteBlock) -> TopicBlockPatchResponse:
    return TopicBlockPatchResponse(
        id=block.id,
        topic_id=block.topic_id,
        kind=block.kind,
        content=block.content,
        updated_at=block.updated_at,
    )


def _decode_string_list(value: str) -> list[str]:
    decoded: object = json.loads(value)
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise ValueError("expected a JSON array of strings")
    return [item for item in decoded if isinstance(item, str)]


def _decode_attachment_anchors(value: str) -> list[dict[str, object]]:
    decoded: object = json.loads(value)
    if not isinstance(decoded, list) or any(not isinstance(item, dict) for item in decoded):
        raise ValueError("expected a JSON array of objects")
    return [cast(dict[str, object], item) for item in decoded]


def _mapping_str(value: Mapping[str, object], key: str) -> str:
    item = value[key]
    if not isinstance(item, str):
        raise ValueError(f"card {key} is invalid")
    return item


def _mapping_bool(value: Mapping[str, object], key: str) -> bool:
    item = value[key]
    if not isinstance(item, bool):
        raise ValueError(f"card {key} is invalid")
    return item


def _mapping_int(value: Mapping[str, object], key: str) -> int:
    item = value[key]
    if not isinstance(item, int) or isinstance(item, bool):
        raise ValueError(f"card {key} is invalid")
    return item


def _chapter_filename(seq: int, title: str) -> str:
    safe_title = title.replace("/", "_").replace("\\", "_")
    return f"{seq}-{safe_title}.md"


def _safe_dir_name(name: str) -> str:
    safe_name = name.replace("/", "-").replace("\\", "-").strip()
    if safe_name in {"", ".", ".."}:
        return "source"
    return safe_name


@router.post("/courses", response_model=CourseResponse)
async def create_course(req: CourseCreateRequest, sch: SchedulerDep) -> CourseResponse:
    def create_course_transaction() -> CourseResponse:
        root_dir = _resolve_absolute_dir(req.root_dir)
        try:
            root_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(400, "root_dir cannot be created") from exc
        course = _repo(sch).create_course(req.title, req.description, str(root_dir))
        return _course_response(course)

    return await run_in_threadpool(create_course_transaction)


@router.get("/courses", response_model=list[CourseResponse])
async def list_courses(sch: SchedulerDep) -> list[CourseResponse]:
    def list_courses_transaction() -> list[CourseResponse]:
        return [_course_response(course) for course in _repo(sch).list_courses()]

    return await run_in_threadpool(list_courses_transaction)


@router.post("/courses/{course_id}/sources", response_model=SourceResponse)
async def create_source(
    course_id: str, req: SourceCreateRequest, sch: SchedulerDep
) -> SourceResponse:
    def create_source_transaction() -> SourceResponse:
        repo = _repo(sch)
        course = repo.get_course(course_id)
        if course is None:
            raise HTTPException(404, "course not found")
        file_path = _resolve_inside(req.file_path, Path(course.root_dir))
        if not file_path.is_file():
            raise HTTPException(400, "file_path must be an existing file")
        source = repo.create_source(course_id, req.kind, str(file_path), req.title)
        return _source_response(source)

    return await run_in_threadpool(create_source_transaction)


@router.post(
    "/courses/{course_id}/sources/import",
    response_model=SourceImportResponse,
)
async def import_sources(
    course_id: str, req: SourceImportRequest, sch: SchedulerDep
) -> SourceImportResponse:
    try:
        return await run_in_threadpool(
            _import_sources_sync,
            course_id,
            req.paths,
            req.titles,
            sch,
        )
    except _CourseNotFoundError as exc:
        raise HTTPException(404, "course not found") from exc
    except SourceImportInputError as exc:
        raise HTTPException(400, "textbook file could not be imported") from exc
    except AtomicImportUnsupportedError as exc:
        raise HTTPException(507, "course storage does not support atomic imports") from exc
    except CourseStorageChangedError as exc:
        raise HTTPException(500, "course storage changed during import") from exc
    except CourseStorageError as exc:
        raise HTTPException(507, "course storage could not complete import") from exc
    except _SourceSaveError as exc:
        raise HTTPException(500, "textbook sources could not be saved") from exc


@router.get("/courses/{course_id}/sources", response_model=list[SourceResponse])
async def list_sources(course_id: str, sch: SchedulerDep) -> list[SourceResponse]:
    def list_sources_transaction() -> list[SourceResponse]:
        repo = _repo(sch)
        if repo.get_course(course_id) is None:
            raise HTTPException(404, "course not found")
        return [_source_response(source) for source in repo.list_sources(course_id)]

    return await run_in_threadpool(list_sources_transaction)


@router.post("/sources/{source_id}/ocr")
async def start_source_ocr(source_id: str, sch: SchedulerDep) -> dict[str, object]:
    def start_ocr_transaction() -> dict[str, object]:
        repo = _repo(sch)
        source = repo.get_source(source_id)
        if source is None:
            raise HTTPException(404, "source not found")
        course = repo.get_course(source.course_id)
        if course is None:
            raise HTTPException(404, "course not found")
        workflow = _ocr_workflow(source, course)
        try:
            workflow.start()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return workflow.status()

    return await run_in_threadpool(start_ocr_transaction)


@router.get("/sources/{source_id}/ocr/status")
async def source_ocr_status(source_id: str, sch: SchedulerDep) -> dict[str, object]:
    def ocr_status_transaction() -> dict[str, object]:
        repo = _repo(sch)
        source = repo.get_source(source_id)
        if source is None:
            raise HTTPException(404, "source not found")
        course = repo.get_course(source.course_id)
        if course is None:
            raise HTTPException(404, "course not found")
        return _ocr_workflow(source, course).status()

    return await run_in_threadpool(ocr_status_transaction)


@router.post("/sources/{source_id}/ocr/cancel")
async def cancel_source_ocr(source_id: str, sch: SchedulerDep) -> dict[str, object]:
    def cancel_ocr_transaction() -> dict[str, object]:
        repo = _repo(sch)
        source = repo.get_source(source_id)
        if source is None:
            raise HTTPException(404, "source not found")
        course = repo.get_course(source.course_id)
        if course is None:
            raise HTTPException(404, "course not found")
        workflow = _ocr_workflow(source, course)
        workflow.cancel()
        return workflow.status()

    return await run_in_threadpool(cancel_ocr_transaction)


@router.post("/sources/{source_id}/ocr/chapters")
async def recognize_source_chapters(source_id: str, sch: SchedulerDep) -> dict[str, object]:
    def detect_ocr_chapters_transaction() -> dict[str, object]:
        repo = _repo(sch)
        source = repo.get_source(source_id)
        if source is None:
            raise HTTPException(404, "source not found")
        course = repo.get_course(source.course_id)
        if course is None:
            raise HTTPException(404, "course not found")
        workflow = _ocr_workflow(source, course)
        try:
            return workflow.detect_chapters()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return await run_in_threadpool(detect_ocr_chapters_transaction)


@router.post("/sources/{source_id}/ocr/chapters/confirm")
async def confirm_source_chapter(
    source_id: str, req: OcrChapterConfirmationRequest, sch: SchedulerDep
) -> dict[str, object]:
    def confirm_selected_chapter() -> dict[str, object]:
        repo = _repo(sch)
        source = repo.get_source(source_id)
        if source is None:
            raise HTTPException(404, "source not found")
        course = repo.get_course(source.course_id)
        if course is None:
            raise HTTPException(404, "course not found")
        return _ocr_workflow(source, course).confirm_chapter(req.chapter_id)

    try:
        return await run_in_threadpool(confirm_selected_chapter)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/sources/{source_id}/ocr/generate")
async def generate_source_note(
    source_id: str, req: OcrChapterConfirmationRequest, sch: SchedulerDep
) -> dict[str, object]:
    def generate_note_transaction() -> dict[str, object]:
        repo = _repo(sch)
        source = repo.get_source(source_id)
        if source is None:
            raise HTTPException(404, "source not found")
        course = repo.get_course(source.course_id)
        if course is None:
            raise HTTPException(404, "course not found")
        workflow = _ocr_workflow(source, course)
        final, pages, tree = workflow.completed_chapter_context()
        confirmation = load_chapter_confirmation(workflow.paths.confirmation)
        validate_chapter_confirmation(confirmation, tree)
        if confirmation["chapter_id"] != req.chapter_id:
            raise ValueError("chapter confirmation target mismatch")
        base = build_intensive_reading_note(
            tree,
            confirmation,
            pages,
            source_id=source.id,
        )
        api_key = _read_configured_deepseek_key()
        settings = load_settings(_settings_root(sch))
        generator = DeepSeekIntensiveReadingGenerator(
            DeepSeekClient(api_key, settings.deepseek_model)
        )

        def generate_publication(output_path: Path) -> dict[str, object]:
            generated = generator.generate(base, output_path=output_path)
            metadata: object = generated.get("metadata")
            if not isinstance(metadata, dict):
                return generated
            publication_note: dict[str, object] = dict(generated)
            publication_metadata = cast(dict[str, object], dict(metadata))
            publication_metadata.pop("note_fingerprint", None)
            publication_note["metadata"] = publication_metadata
            return publication_note

        note, markdown_path = workflow.generate_and_publish(
            generate_publication,
            expected_final=final,
            expected_tree=tree,
            confirmation=confirmation,
        )
        return {
            "status": "completed",
            "publishable": True,
            "markdown_path": str(markdown_path),
            "markdown": note["markdown"],
            "input_fingerprint": final["input_fingerprint"],
        }

    try:
        return await run_in_threadpool(generate_note_transaction)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(409, "精读结果未通过证据门禁，未发布") from exc


@router.post("/sources/{source_id}/detect-chapters", response_model=list[ChapterResponse])
async def detect_source_chapters(source_id: str, sch: SchedulerDep) -> list[ChapterResponse]:
    def detect_transaction() -> list[ChapterResponse]:
        repo = _repo(sch)
        source = repo.get_source(source_id)
        if source is None:
            raise HTTPException(404, "source not found")
        course = repo.get_course(source.course_id)
        if course is None:
            raise HTTPException(404, "course not found")
        source_path = Path(source.file_path)
        if source_path.suffix.lower() in {".md", ".txt"}:
            markdown = source_path.read_text(encoding="utf-8")
        else:
            markdown = MarkItDownAdapter().parse(str(source_path))

        existing_chapters = repo.list_chapters(source.id)
        if any(chapter.status != "DRAFT" for chapter in existing_chapters):
            raise HTTPException(409, "source has confirmed or generated chapters")
        repo.delete_chapters_by_source(source.id)
        out_dir = Path(course.root_dir) / _safe_dir_name(source.title)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(400, "course directory cannot be written") from exc
        chapters = []
        for candidate in detect_chapters(markdown):
            chapter_path = out_dir / _chapter_filename(candidate.seq, candidate.title)
            try:
                chapter_path.write_text(candidate.raw_md, encoding="utf-8")
            except (OSError, RuntimeError, ValueError) as exc:
                raise HTTPException(400, "course directory cannot be written") from exc
            chapters.append(
                repo.create_chapter(
                    course_id=course.id,
                    source_id=source.id,
                    seq=candidate.seq,
                    title=candidate.title,
                    source_md_path=str(chapter_path),
                    source_start=candidate.start,
                    source_end=candidate.end,
                )
            )
        return [_chapter_response(chapter) for chapter in chapters]

    return await run_in_threadpool(detect_transaction)


def _chapter_draft_state(repo: WorkbenchRepository, source_id: str) -> ChapterDraftState:
    chapters = repo.list_chapters(source_id)
    return ChapterDraftState(
        chapters=[
            ChapterDraftResponse(
                **_chapter_response(chapter).model_dump(),
                start=chapter.source_start,
                end=chapter.source_end,
            )
            for chapter in chapters
        ],
        fingerprint=repo.chapter_draft_snapshot(source_id)[1],
    )


@router.get("/sources/{source_id}/chapter-drafts", response_model=ChapterDraftState)
async def get_chapter_drafts(source_id: str, sch: SchedulerDep) -> ChapterDraftState:
    def chapter_drafts_transaction() -> ChapterDraftState:
        repo = _repo(sch)
        if repo.get_source(source_id) is None:
            raise HTTPException(404, "source not found")
        return _chapter_draft_state(repo, source_id)

    return await run_in_threadpool(chapter_drafts_transaction)


def _replace_chapter_drafts_transaction(
    source_id: str, req: ChapterDraftReplaceRequest, sch: SchedulerDep
) -> ChapterDraftState:
    repo = _repo(sch)
    source = repo.get_source(source_id)
    if source is None:
        raise HTTPException(404, "source not found")
    source_path = Path(source.file_path)
    markdown = (
        source_path.read_text(encoding="utf-8")
        if source_path.suffix.lower() in {".md", ".txt"}
        else MarkItDownAdapter().parse(str(source_path))
    )
    course = repo.get_course(source.course_id)
    if course is None:
        raise RuntimeError("course missing for source")
    out_dir = Path(course.root_dir) / _safe_dir_name(source.title)
    specs: list[dict[str, object]] = []
    publications: list[tuple[Path, str]] = []
    for seq, item in enumerate(req.chapters):
        if item.end > len(markdown) or item.end <= item.start:
            raise HTTPException(400, "invalid chapter boundary")
        chapter_path = out_dir / _chapter_filename(seq, item.title)
        specs.append(
            {
                "id": item.id,
                "title": item.title,
                "start": item.start,
                "end": item.end,
                "source_md_path": str(chapter_path),
            }
        )
        publications.append((chapter_path, markdown[item.start : item.end].strip()))
    if len({path for path, _ in publications}) != len(publications):
        raise HTTPException(400, "duplicate chapter file target")
    published: list[tuple[Path, Path | None]] = []

    def publish_files() -> None:
        staged: list[tuple[Path, Path]] = []
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            for target, content in publications:
                temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
                temporary.write_text(content, encoding="utf-8")
                staged.append((target, temporary))
            for target, temporary in staged:
                backup = None
                if target.exists():
                    backup = target.with_name(f".{target.name}.{uuid4().hex}.bak")
                    os.replace(target, backup)
                try:
                    os.replace(temporary, target)
                except Exception:
                    if backup is not None:
                        os.replace(backup, target)
                    raise
                published.append((target, backup))
        except Exception:
            rollback_files()
            for _, temporary in staged:
                temporary.unlink(missing_ok=True)
            raise

    def rollback_files() -> None:
        for target, backup in reversed(published):
            target.unlink(missing_ok=True)
            if backup is not None:
                os.replace(backup, target)
        published.clear()

    try:
        repo.replace_chapter_drafts(
            source_id,
            specs,
            expected_fingerprint=req.expected_fingerprint,
            publish=publish_files,
            compensate=rollback_files,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(507, "chapter draft files could not be published") from exc
    for _, backup in published:
        if backup is not None:
            backup.unlink(missing_ok=True)
    return _chapter_draft_state(repo, source_id)


@router.put("/sources/{source_id}/chapter-drafts", response_model=ChapterDraftState)
async def replace_chapter_drafts(
    source_id: str, req: ChapterDraftReplaceRequest, sch: SchedulerDep
) -> ChapterDraftState:
    return await run_in_threadpool(_replace_chapter_drafts_transaction, source_id, req, sch)


@router.post("/sources/{source_id}/chapter-drafts/confirm", response_model=ChapterDraftState)
async def confirm_chapter_drafts(
    source_id: str, req: FingerprintRequest, sch: SchedulerDep
) -> ChapterDraftState:
    def confirm_chapter_drafts_transaction() -> ChapterDraftState:
        repo = _repo(sch)
        if repo.get_source(source_id) is None:
            raise HTTPException(404, "source not found")
        try:
            repo.confirm_chapter_drafts(source_id, req.expected_fingerprint)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _chapter_draft_state(repo, source_id)

    return await run_in_threadpool(confirm_chapter_drafts_transaction)


def _import_chapter_attachments_transaction(
    chapter_id: str, req: AttachmentImportRequest, sch: SchedulerDep
) -> list[AttachmentResponse]:
    repo = _repo(sch)
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise HTTPException(404, "chapter not found")
    course = repo.get_course(chapter.course_id)
    if course is None:
        raise RuntimeError("course missing for chapter")
    parser = MarkItDownAdapter()
    try:
        with TextbookImportBatch(
            Path(course.root_dir), target_directory=ATTACHMENT_DIRECTORY_NAME
        ) as batch:
            imported = [batch.import_file(Path(path)) for path in req.paths]
            specs: list[
                tuple[
                    str,
                    str,
                    str | None,
                    str,
                    str,
                    str,
                    str,
                    str,
                    Sequence[Mapping[str, object]],
                ]
            ] = []
            for item in imported:
                text, content_hash, anchors = parse_imported_source(item.stored_path, parser)
                specs.append(
                    (
                        chapter.course_id,
                        chapter.source_id,
                        chapter.id,
                        str(item.stored_path),
                        item.title,
                        item.stored_path.suffix.lower().lstrip("."),
                        text,
                        content_hash,
                        anchors,
                    )
                )
            records = repo.create_attachments_guarded(
                specs,
                lambda: batch.commit(retain_records=True),
                batch.rollback,
            )
            batch.finalize()
    except SourceImportInputError as exc:
        raise HTTPException(400, "attachment file could not be imported") from exc
    except Exception as exc:
        raise HTTPException(500, "attachment import could not be committed") from exc
    return [
        AttachmentResponse(
            id=item.id,
            course_id=item.course_id,
            source_id=cast(str, item.source_id),
            chapter_id=cast(str, item.chapter_id),
            file_path=item.file_path,
            title=item.title,
            kind=item.kind,
            content_hash=item.content_hash,
            anchors=_decode_attachment_anchors(item.anchors_json),
        )
        for item in records
    ]


@router.post("/chapters/{chapter_id}/attachments/import", response_model=list[AttachmentResponse])
async def import_chapter_attachments(
    chapter_id: str, req: AttachmentImportRequest, sch: SchedulerDep
) -> list[AttachmentResponse]:
    return await run_in_threadpool(_import_chapter_attachments_transaction, chapter_id, req, sch)


@router.get("/sources/{source_id}/chapters", response_model=list[ChapterResponse])
async def list_chapters(source_id: str, sch: SchedulerDep) -> list[ChapterResponse]:
    def list_chapters_transaction() -> list[ChapterResponse]:
        repo = _repo(sch)
        if repo.get_source(source_id) is None:
            raise HTTPException(404, "source not found")
        return [_chapter_response(chapter) for chapter in repo.list_chapters(source_id)]

    return await run_in_threadpool(list_chapters_transaction)


@router.get("/chapters/{chapter_id}", response_model=ChapterResponse)
async def get_chapter(chapter_id: str, sch: SchedulerDep) -> ChapterResponse:
    def get_chapter_transaction() -> ChapterResponse:
        chapter = _repo(sch).get_chapter(chapter_id)
        if chapter is None:
            raise HTTPException(404, "chapter not found")
        return _chapter_response(chapter)

    return await run_in_threadpool(get_chapter_transaction)


@router.get("/settings", response_model=WorkbenchSettingsResponse)
async def get_workbench_settings(sch: SchedulerDep) -> WorkbenchSettingsResponse:
    def load_settings_transaction() -> WorkbenchSettingsResponse:
        settings = load_settings(_settings_root(sch))
        return WorkbenchSettingsResponse(
            deepseek_model=settings.deepseek_model,
            deepseek_key_masked=_read_masked_deepseek_key(),
            codex_cli_path=settings.codex_cli_path,
            baidu_key_masked=_read_masked_baidu_key(),
        )

    return await run_in_threadpool(load_settings_transaction)


@router.post("/settings/deepseek", response_model=WorkbenchSettingsResponse)
async def save_deepseek_settings(
    req: DeepSeekSettingsRequest, sch: SchedulerDep
) -> WorkbenchSettingsResponse:
    if req.api_key is not None and not req.api_key.strip():
        raise HTTPException(400, "deepseek api key cannot be empty")
    if req.model != "deepseek-v4-pro":
        raise HTTPException(400, "only deepseek-v4-pro is supported")

    def save_settings_transaction() -> WorkbenchSettingsResponse:
        settings = WorkbenchSettings(deepseek_model=req.model or "deepseek-v4-pro")
        try:
            settings = save_settings(_settings_root(sch), settings)
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc
        if req.api_key is not None:
            api_key = req.api_key.strip()
            try:
                save_secret(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, api_key)
            except KeychainError as exc:
                raise HTTPException(500, str(exc)) from exc
            masked_key = mask_secret(api_key)
        else:
            masked_key = _read_masked_deepseek_key()
        return WorkbenchSettingsResponse(
            deepseek_model=settings.deepseek_model,
            deepseek_key_masked=masked_key,
            codex_cli_path=settings.codex_cli_path,
            baidu_key_masked=_read_masked_baidu_key(),
        )

    return await run_in_threadpool(save_settings_transaction)


@router.post("/settings/deepseek/test")
async def test_deepseek_settings(sch: SchedulerDep) -> dict[str, str]:
    def test_connection() -> None:
        settings = load_settings(_settings_root(sch))
        api_key = _read_configured_deepseek_key()
        DeepSeekClient(api_key, settings.deepseek_model).complete("请只回复 ok", timeout=30)

    try:
        await run_in_threadpool(test_connection)
    except DeepSeekError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok"}


@router.post("/chapters/{chapter_id}/confirm", response_model=ChapterResponse)
async def confirm_chapter(chapter_id: str, sch: SchedulerDep) -> ChapterResponse:
    def confirm_transaction() -> ChapterResponse:
        try:
            confirmed = _repo(sch).confirm_chapter(chapter_id)
        except LookupError as exc:
            raise HTTPException(404, "chapter not found") from exc
        except ValueError as exc:
            raise HTTPException(409, "chapter cannot be confirmed in current state") from exc
        return _chapter_response(confirmed)

    return await run_in_threadpool(confirm_transaction)


@router.get("/courses/{course_id}/cards", response_model=list[CourseCardResponse])
async def list_cards(course_id: str, sch: SchedulerDep) -> list[CourseCardResponse]:
    def list_cards_transaction() -> list[CourseCardResponse]:
        repo = _repo(sch)
        if repo.get_course(course_id) is None:
            raise HTTPException(404, "course not found")
        responses: list[CourseCardResponse] = []
        for row in repo.list_course_cards(course_id):
            responses.append(
                _card_response(
                    {
                        "id": row["id"],
                        "origin_type": row["origin_type"],
                        "origin_id": row["origin_id"],
                        "origin_title": row["origin_title"],
                        "card_type": row["card_type"],
                        "title": row["title"],
                        "content": row["content"],
                        "source_refs": _decode_string_list(row["source_refs_json"]),
                        "tags": _decode_string_list(row["tags_json"]),
                        "status": row["status"],
                        "favorite": row["favorite"],
                        "updated_at": row["updated_at"],
                    }
                )
            )
        return responses

    return await run_in_threadpool(list_cards_transaction)


def _card_response(card: Mapping[str, object]) -> CourseCardResponse:
    origin_type_value = _mapping_str(card, "origin_type")
    if origin_type_value not in {"chapter", "topic"}:
        raise ValueError("card origin_type is invalid")
    origin_type = cast(Literal["chapter", "topic"], origin_type_value)
    status_value = _mapping_str(card, "status")
    if status_value not in {"ACTIVE", "ARCHIVED"}:
        raise ValueError("card status is invalid")
    status = cast(Literal["ACTIVE", "ARCHIVED"], status_value)
    source_refs = card["source_refs"]
    tags = card["tags"]
    if not isinstance(source_refs, list) or any(not isinstance(item, str) for item in source_refs):
        raise ValueError("card source_refs is invalid")
    if not isinstance(tags, list) or any(not isinstance(item, str) for item in tags):
        raise ValueError("card tags is invalid")
    return CourseCardResponse(
        id=_mapping_str(card, "id"),
        origin_type=origin_type,
        origin_id=_mapping_str(card, "origin_id"),
        origin_title=_mapping_str(card, "origin_title"),
        card_type=_mapping_str(card, "card_type"),
        title=_mapping_str(card, "title"),
        content=_mapping_str(card, "content"),
        source_refs=[item for item in source_refs if isinstance(item, str)],
        tags=[item for item in tags if isinstance(item, str)],
        status=status,
        favorite=_mapping_bool(card, "favorite"),
        updated_at=_mapping_int(card, "updated_at"),
    )


@router.patch("/cards/{card_id}", response_model=CourseCardResponse)
async def patch_course_card(
    card_id: str, req: CourseCardPatchRequest, sch: SchedulerDep
) -> CourseCardResponse:
    def patch_course_card_transaction() -> CourseCardResponse:
        try:
            card = _repo(sch).update_course_card(
                card_id,
                title=req.title.strip(),
                content=req.content,
                tags=req.tags,
                status=req.status,
                expected_updated_at=req.expected_updated_at,
            )
        except LookupError as exc:
            raise HTTPException(404, "card not found") from exc
        except ValueError as exc:
            raise HTTPException(409, "card changed") from exc
        return _card_response(card)

    return await run_in_threadpool(patch_course_card_transaction)


@router.patch("/cards/{card_id}/favorite", response_model=CourseCardResponse)
async def patch_course_card_favorite(
    card_id: str,
    req: CourseCardFavoriteRequest,
    sch: SchedulerDep,
) -> CourseCardResponse:
    def patch_course_card_favorite_transaction() -> CourseCardResponse:
        try:
            card = _repo(sch).set_course_card_favorite(
                card_id,
                req.favorite,
                req.expected_updated_at,
            )
        except LookupError as exc:
            raise HTTPException(404, "card not found") from exc
        except ValueError as exc:
            raise HTTPException(409, "card changed") from exc
        return _card_response(card)

    return await run_in_threadpool(patch_course_card_favorite_transaction)


@router.patch("/topics/{topic_id}/note-blocks/{kind}", response_model=TopicBlockPatchResponse)
async def patch_topic_note_block(
    topic_id: str, kind: str, req: TopicBlockPatchRequest, sch: SchedulerDep
) -> TopicBlockPatchResponse:
    if kind not in FIXED_TOPIC_KINDS:
        raise HTTPException(422, "unknown topic block kind")
    if kind.endswith("_mermaid"):
        try:
            validate_mermaid_subset(req.content)
        except ValueError as exc:
            raise HTTPException(422, {"code": "invalid_mermaid"}) from exc
    else:
        _require_safe_markdown(req.content)

    def patch_transaction() -> TopicNoteBlock:
        repo = _repo(sch)
        block = repo.prepare_topic_note_block_update(
            topic_id, kind, req.content, req.expected_content
        )
        TopicFusionPipeline(repo, StubIntensiveReadingExecutor()).retry_markdown_sync(topic_id)
        return block

    try:
        block = await run_in_threadpool(patch_transaction)
    except TopicMarkdownSyncError as exc:
        raise HTTPException(507, "topic Markdown sync failed; database edit retained") from exc
    except ValueError as exc:
        if "changed" in str(exc) or "already syncing" in str(exc):
            raise HTTPException(409, "topic block edit conflicts with current state") from exc
        raise HTTPException(404, "topic note block not found") from exc
    return _topic_block_response(block)


@router.get("/chapters/{chapter_id}/note-blocks", response_model=list[NoteBlockResponse])
async def list_note_blocks(chapter_id: str, sch: SchedulerDep) -> list[NoteBlockResponse]:
    def list_note_blocks_transaction() -> list[NoteBlockResponse]:
        repo = _repo(sch)
        if repo.get_chapter(chapter_id) is None:
            raise HTTPException(404, "chapter not found")
        return [_note_block_response(block) for block in repo.list_note_blocks(chapter_id)]

    return await run_in_threadpool(list_note_blocks_transaction)


@router.patch("/chapters/{chapter_id}/note-blocks/{kind}", response_model=NoteBlockResponse)
async def patch_chapter_note_block(
    chapter_id: str, kind: str, req: ChapterBlockPatchRequest, sch: SchedulerDep
) -> NoteBlockResponse:
    if kind not in FIXED_CHAPTER_KINDS:
        raise HTTPException(422, "unknown chapter block kind")
    if kind.endswith("_mermaid"):
        try:
            validate_mermaid_subset(req.body)
        except ValueError as exc:
            raise HTTPException(422, {"code": "invalid_mermaid"}) from exc
    else:
        _require_safe_markdown(req.body)

    def patch_transaction() -> NoteBlock:
        repo = _repo(sch)
        block = repo.patch_chapter_note_block(chapter_id, kind, req.body, req.expected_body)
        sync_chapter_markdown(repo, chapter_id)
        return block

    try:
        block = await run_in_threadpool(patch_transaction)
    except ValueError as exc:
        if "changed" in str(exc):
            raise HTTPException(409, "chapter block edit conflicts with current state") from exc
        raise HTTPException(404, "chapter note block not found") from exc
    except OSError as exc:
        raise HTTPException(507, "chapter Markdown sync failed; database edit retained") from exc
    return _note_block_response(block)


@router.post("/chapters/{chapter_id}/run", response_model=ChapterResponse)
async def run_chapter(
    chapter_id: str, req: RunChapterRequest, sch: SchedulerDep
) -> ChapterResponse:
    if req.executor != "stub":
        raise HTTPException(400, "unsupported executor")

    def run_transaction() -> ChapterResponse:
        repo = _repo(sch)
        chapter = repo.get_chapter(chapter_id)
        if chapter is None:
            raise HTTPException(404, "chapter not found")
        try:
            start = repo.start_chapter_generation(chapter_id)
        except ChapterGenerationConflictError as exc:
            if exc.cause == "invalid_state":
                raise HTTPException(
                    409,
                    "chapter must be CONFIRMED before intensive reading",
                ) from exc
            raise HTTPException(409, "chapter reading conflicts with current state") from exc

        try:
            run_dir = Path(_query_orchestrator(sch).fs.base_dir) / "workbench-runs"
            executor = StubIntensiveReadingExecutor()
            IntensiveReadingPipeline(
                repo,
                executor,
                run_dir,
                generation_start=start,
            ).run_all(chapter_id)
            completed = repo.get_chapter(chapter_id)
            if completed is None:
                raise RuntimeError("chapter disappeared during generation")
            if completed.status != "COMPLETED":
                raise HTTPException(500, "intensive reading did not finalize")
            return _chapter_response(completed)
        except HTTPException:
            _release_chapter_generation_claim(
                repo,
                chapter_id,
                start.owner_id,
                error="intensive reading request failed",
            )
            raise
        except ChapterGenerationConflictError as exc:
            _release_chapter_generation_claim(
                repo,
                chapter_id,
                start.owner_id,
                error="intensive reading conflict",
            )
            raise HTTPException(409, "chapter reading conflicts with current state") from exc
        except ChapterMarkdownSyncError as exc:
            _release_chapter_generation_claim(
                repo,
                chapter_id,
                start.owner_id,
                error="chapter markdown sync failed",
            )
            raise HTTPException(400, "course directory cannot be written") from exc
        except Exception as exc:
            _release_chapter_generation_claim(
                repo,
                chapter_id,
                start.owner_id,
                error="intensive reading failed",
            )
            raise HTTPException(500, "intensive reading failed") from exc

    return await run_in_threadpool(run_transaction)


@router.post("/chapters/{chapter_id}/run-hybrid", response_model=ChapterResponse)
async def run_chapter_hybrid(chapter_id: str, sch: SchedulerDep) -> ChapterResponse:
    def run_transaction() -> ChapterResponse:
        repo = _repo(sch)
        chapter = repo.get_chapter(chapter_id)
        if chapter is None:
            raise HTTPException(404, "chapter not found")
        try:
            start = repo.start_chapter_generation(chapter_id)
        except ChapterGenerationConflictError as exc:
            raise HTTPException(409, "chapter hybrid reading conflicts with current state") from exc

        try:
            run_dir = Path(_query_orchestrator(sch).fs.base_dir) / "workbench-runs"
            executor: IntensiveReadingExecutor
            if start.chapter.status == CHAPTER_SYNC_PENDING:
                executor = StubIntensiveReadingExecutor()
            else:
                api_key = _read_configured_deepseek_key()
                settings = load_settings(_settings_root(sch))
                codex_path = resolve_codex_path()
                codex_executor = CodexCliExecutor(codex_path, run_dir)
                deepseek_executor = DeepSeekExecutor(
                    DeepSeekClient(api_key, settings.deepseek_model)
                )
                executor = HybridIntensiveReadingExecutor(deepseek_executor, codex_executor)
            IntensiveReadingPipeline(
                repo,
                executor,
                run_dir,
                generation_start=start,
            ).run_all(chapter_id)
            completed = repo.get_chapter(chapter_id)
            if completed is None:
                raise RuntimeError("chapter disappeared during generation")
            if completed.status != "COMPLETED":
                raise HTTPException(500, "hybrid intensive reading did not finalize")
            return _chapter_response(completed)
        except HTTPException:
            _release_chapter_generation_claim(
                repo,
                chapter_id,
                start.owner_id,
                error="hybrid intensive reading request failed",
            )
            raise
        except CodexCliError as exc:
            _release_chapter_generation_claim(
                repo,
                chapter_id,
                start.owner_id,
                error="codex cli is not available",
            )
            raise HTTPException(400, "codex cli is not available") from exc
        except ChapterGenerationConflictError as exc:
            _release_chapter_generation_claim(
                repo,
                chapter_id,
                start.owner_id,
                error="hybrid intensive reading conflict",
            )
            raise HTTPException(409, "chapter hybrid reading conflicts with current state") from exc
        except ChapterMarkdownSyncError as exc:
            _release_chapter_generation_claim(
                repo,
                chapter_id,
                start.owner_id,
                error="chapter markdown sync failed",
            )
            raise HTTPException(400, "course directory cannot be written") from exc
        except Exception as exc:
            _release_chapter_generation_claim(
                repo,
                chapter_id,
                start.owner_id,
                error="hybrid intensive reading failed",
            )
            raise HTTPException(500, "hybrid intensive reading failed") from exc

    return await run_in_threadpool(run_transaction)
