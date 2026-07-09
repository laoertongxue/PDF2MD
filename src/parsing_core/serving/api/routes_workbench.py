from pathlib import Path

from fastapi import APIRouter, HTTPException

from parsing_core.parser.markitdown_adapter import MarkItDownAdapter
from parsing_core.serving.api.deps import SchedulerDep
from parsing_core.serving.models.api import (
    ChapterResponse,
    CourseCreateRequest,
    CourseResponse,
    RunChapterRequest,
    SourceCreateRequest,
    SourceResponse,
)
from parsing_core.workbench.chapter_detection import detect_chapters
from parsing_core.workbench.executors import StubIntensiveReadingExecutor
from parsing_core.workbench.pipeline import IntensiveReadingPipeline
from parsing_core.workbench.repository import WorkbenchRepository

router = APIRouter(prefix="/api/workbench", tags=["workbench"])


def _repo(sch: SchedulerDep) -> WorkbenchRepository:
    return WorkbenchRepository(sch._query_orch.repo.conn)


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
    course = _repo(sch).create_course(req.title, req.description, req.root_dir)
    return _course_response(course)


@router.get("/courses", response_model=list[CourseResponse])
async def list_courses(sch: SchedulerDep):
    return [_course_response(course) for course in _repo(sch).list_courses()]


@router.post("/courses/{course_id}/sources", response_model=SourceResponse)
async def create_source(course_id: str, req: SourceCreateRequest, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_course(course_id) is None:
        raise HTTPException(404, "course not found")
    source = repo.create_source(course_id, req.kind, req.file_path, req.title)
    return _source_response(source)


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

    out_dir = Path(course.root_dir) / _safe_dir_name(source.title)
    out_dir.mkdir(parents=True, exist_ok=True)
    chapters = []
    for candidate in detect_chapters(markdown):
        chapter_path = out_dir / _chapter_filename(candidate.seq, candidate.title)
        chapter_path.write_text(candidate.raw_md, encoding="utf-8")
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


@router.post("/chapters/{chapter_id}/confirm", response_model=ChapterResponse)
async def confirm_chapter(chapter_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_chapter(chapter_id) is None:
        raise HTTPException(404, "chapter not found")
    repo.update_chapter_status(chapter_id, "CONFIRMED")
    return _chapter_response(repo.get_chapter(chapter_id))


@router.post("/chapters/{chapter_id}/run")
async def run_chapter(chapter_id: str, req: RunChapterRequest, sch: SchedulerDep):
    if req.executor != "stub":
        raise HTTPException(400, "unsupported executor")
    repo = _repo(sch)
    if repo.get_chapter(chapter_id) is None:
        raise HTTPException(404, "chapter not found")

    run_dir = Path(sch._query_orch.fs.base_dir) / "workbench-runs"
    IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), run_dir).run_all(chapter_id)
    return {"chapter_id": chapter_id, "status": "COMPLETED"}
