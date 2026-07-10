from pathlib import Path

from fastapi import APIRouter, HTTPException

from parsing_core.parser.markitdown_adapter import MarkItDownAdapter
from parsing_core.serving.api.deps import SchedulerDep
from parsing_core.serving.models.api import (
    CardResponse,
    ChapterResponse,
    CourseCreateRequest,
    CourseResponse,
    DeepSeekSettingsRequest,
    NoteBlockResponse,
    RunChapterRequest,
    SourceCreateRequest,
    SourceResponse,
    WorkbenchSettingsResponse,
)
from parsing_core.workbench.chapter_detection import detect_chapters
from parsing_core.workbench.codex_cli import CodexCliError, CodexCliExecutor, resolve_codex_path
from parsing_core.workbench.deepseek import DeepSeekClient, DeepSeekError, DeepSeekExecutor
from parsing_core.workbench.executors import StubIntensiveReadingExecutor
from parsing_core.workbench.hybrid import HybridIntensiveReadingExecutor
from parsing_core.workbench.keychain import KeychainError, mask_secret, read_secret, save_secret
from parsing_core.workbench.pipeline import ChapterMarkdownSyncError, IntensiveReadingPipeline
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.settings import WorkbenchSettings, load_settings, save_settings

router = APIRouter(prefix="/api/workbench", tags=["workbench"])
KEYCHAIN_SERVICE = "pdf2md.deepseek"
KEYCHAIN_ACCOUNT = "api-key"


def _repo(sch: SchedulerDep) -> WorkbenchRepository:
    return WorkbenchRepository(sch._query_orch.repo.conn)


def _settings_path(sch: SchedulerDep) -> Path:
    return Path(sch._query_orch.fs.base_dir) / "workbench-settings.json"


def _read_configured_deepseek_key() -> str:
    try:
        api_key = read_secret(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    except KeychainError as exc:
        raise HTTPException(400, "deepseek api key not configured") from exc
    if not api_key.strip():
        raise HTTPException(400, "deepseek api key not configured")
    return api_key.strip()


def _read_masked_deepseek_key() -> str | None:
    try:
        api_key = read_secret(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    except KeychainError:
        return None
    return mask_secret(api_key.strip()) if api_key.strip() else None


def _resolve_inside(path: str, base: Path, message: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_relative_to(base):
        raise HTTPException(400, message)
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


def _course_response(course) -> CourseResponse:
    return CourseResponse(
        id=course.id,
        title=course.title,
        description=course.description,
        root_dir=course.root_dir,
    )


def _source_response(source) -> SourceResponse:
    return SourceResponse(
        id=source.id,
        course_id=source.course_id,
        kind=source.kind,
        file_path=source.file_path,
        title=source.title,
        status=source.status,
    )


def _chapter_response(chapter) -> ChapterResponse:
    return ChapterResponse(
        id=chapter.id,
        source_id=chapter.source_id,
        course_id=chapter.course_id,
        seq=chapter.seq,
        title=chapter.title,
        status=chapter.status,
    )


def _card_response(card) -> CardResponse:
    return CardResponse(
        id=card.id,
        course_id=card.course_id,
        chapter_id=card.chapter_id,
        kind=card.kind,
        title=card.title,
        body=card.body,
        favorite=card.favorite,
    )


def _note_block_response(block) -> NoteBlockResponse:
    return NoteBlockResponse(
        id=block.id,
        chapter_id=block.chapter_id,
        kind=block.kind,
        title=block.title,
        body=block.body,
        seq=block.seq,
    )


def _chapter_filename(seq: int, title: str) -> str:
    safe_title = title.replace("/", "_").replace("\\", "_")
    return f"{seq}-{safe_title}.md"


def _safe_dir_name(name: str) -> str:
    safe_name = name.replace("/", "-").replace("\\", "-").strip()
    if safe_name in {"", ".", ".."}:
        return "source"
    return safe_name


@router.post("/courses", response_model=CourseResponse)
async def create_course(req: CourseCreateRequest, sch: SchedulerDep):
    root_dir = _resolve_absolute_dir(req.root_dir)
    try:
        root_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(400, "root_dir cannot be created") from exc
    course = _repo(sch).create_course(req.title, req.description, str(root_dir))
    return _course_response(course)


@router.get("/courses", response_model=list[CourseResponse])
async def list_courses(sch: SchedulerDep):
    return [_course_response(course) for course in _repo(sch).list_courses()]


@router.post("/courses/{course_id}/sources", response_model=SourceResponse)
async def create_source(course_id: str, req: SourceCreateRequest, sch: SchedulerDep):
    repo = _repo(sch)
    course = repo.get_course(course_id)
    if course is None:
        raise HTTPException(404, "course not found")
    file_path = _resolve_inside(
        req.file_path,
        Path(course.root_dir).resolve(),
        "file_path must be inside course root_dir",
    )
    if not file_path.is_file():
        raise HTTPException(400, "file_path must be an existing file")
    source = repo.create_source(course_id, req.kind, str(file_path), req.title)
    return _source_response(source)


@router.get("/courses/{course_id}/sources", response_model=list[SourceResponse])
async def list_sources(course_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_course(course_id) is None:
        raise HTTPException(404, "course not found")
    return [_source_response(source) for source in repo.list_sources(course_id)]


@router.post("/sources/{source_id}/detect-chapters", response_model=list[ChapterResponse])
async def detect_source_chapters(source_id: str, sch: SchedulerDep):
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
            )
        )
    return [_chapter_response(chapter) for chapter in chapters]


@router.get("/sources/{source_id}/chapters", response_model=list[ChapterResponse])
async def list_chapters(source_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_source(source_id) is None:
        raise HTTPException(404, "source not found")
    return [_chapter_response(chapter) for chapter in repo.list_chapters(source_id)]


@router.get("/chapters/{chapter_id}", response_model=ChapterResponse)
async def get_chapter(chapter_id: str, sch: SchedulerDep):
    chapter = _repo(sch).get_chapter(chapter_id)
    if chapter is None:
        raise HTTPException(404, "chapter not found")
    return _chapter_response(chapter)


@router.get("/settings", response_model=WorkbenchSettingsResponse)
async def get_workbench_settings(sch: SchedulerDep):
    settings = load_settings(_settings_path(sch))
    return WorkbenchSettingsResponse(
        deepseek_model=settings.deepseek_model,
        deepseek_key_masked=_read_masked_deepseek_key(),
    )


@router.post("/settings/deepseek", response_model=WorkbenchSettingsResponse)
async def save_deepseek_settings(req: DeepSeekSettingsRequest, sch: SchedulerDep):
    if req.api_key is not None and not req.api_key.strip():
        raise HTTPException(400, "deepseek api key cannot be empty")
    settings = WorkbenchSettings(deepseek_model=req.model or "deepseek-chat")
    try:
        save_settings(_settings_path(sch), settings)
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
    )


@router.post("/settings/deepseek/test")
async def test_deepseek_settings(sch: SchedulerDep):
    settings = load_settings(_settings_path(sch))
    api_key = _read_configured_deepseek_key()
    try:
        DeepSeekClient(api_key, settings.deepseek_model).complete("请只回复 ok", timeout=30)
    except DeepSeekError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok"}


@router.post("/chapters/{chapter_id}/confirm", response_model=ChapterResponse)
async def confirm_chapter(chapter_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_chapter(chapter_id) is None:
        raise HTTPException(404, "chapter not found")
    repo.update_chapter_status(chapter_id, "CONFIRMED")
    return _chapter_response(repo.get_chapter(chapter_id))


@router.get("/courses/{course_id}/cards", response_model=list[CardResponse])
async def list_cards(course_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_course(course_id) is None:
        raise HTTPException(404, "course not found")
    return [_card_response(card) for card in repo.list_cards(course_id)]


@router.get("/chapters/{chapter_id}/note-blocks", response_model=list[NoteBlockResponse])
async def list_note_blocks(chapter_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_chapter(chapter_id) is None:
        raise HTTPException(404, "chapter not found")
    return [_note_block_response(block) for block in repo.list_note_blocks(chapter_id)]


@router.post("/chapters/{chapter_id}/run", response_model=ChapterResponse)
async def run_chapter(chapter_id: str, req: RunChapterRequest, sch: SchedulerDep):
    if req.executor != "stub":
        raise HTTPException(400, "unsupported executor")
    repo = _repo(sch)
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise HTTPException(404, "chapter not found")
    if chapter.status != "CONFIRMED":
        raise HTTPException(409, "chapter must be CONFIRMED before intensive reading")

    run_dir = Path(sch._query_orch.fs.base_dir) / "workbench-runs"
    try:
        IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), run_dir).run_all(chapter_id)
    except ChapterMarkdownSyncError as exc:
        repo.update_chapter_status(chapter_id, "FAILED")
        raise HTTPException(400, "course directory cannot be written") from exc
    except Exception as exc:
        repo.update_chapter_status(chapter_id, "FAILED")
        raise HTTPException(500, str(exc)) from exc
    repo.update_chapter_status(chapter_id, "COMPLETED")
    return _chapter_response(repo.get_chapter(chapter_id))


@router.post("/chapters/{chapter_id}/run-hybrid", response_model=ChapterResponse)
async def run_chapter_hybrid(chapter_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise HTTPException(404, "chapter not found")
    if chapter.status not in {"CONFIRMED", "FAILED"}:
        raise HTTPException(409, "chapter must be CONFIRMED or FAILED before hybrid reading")

    api_key = _read_configured_deepseek_key()

    settings = load_settings(_settings_path(sch))
    run_dir = Path(sch._query_orch.fs.base_dir) / "workbench-runs"

    try:
        codex_path = resolve_codex_path()
    except CodexCliError as exc:
        raise HTTPException(400, str(exc)) from exc

    deepseek_executor = DeepSeekExecutor(DeepSeekClient(api_key, settings.deepseek_model))
    codex_executor = CodexCliExecutor(codex_path, run_dir)
    executor = HybridIntensiveReadingExecutor(deepseek_executor, codex_executor)

    try:
        IntensiveReadingPipeline(repo, executor, run_dir).run_all(chapter_id)
    except ChapterMarkdownSyncError as exc:
        repo.update_chapter_status(chapter_id, "FAILED")
        raise HTTPException(400, "course directory cannot be written") from exc
    except Exception as exc:
        repo.update_chapter_status(chapter_id, "FAILED")
        raise HTTPException(500, str(exc)) from exc
    repo.update_chapter_status(chapter_id, "COMPLETED")
    return _chapter_response(repo.get_chapter(chapter_id))
