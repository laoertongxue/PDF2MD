import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from pydantic import ValidationError

from parsing_core.serving.api.deps import SchedulerDep
from parsing_core.serving.models.topics import (
    TopicCardResponse,
    TopicCreateRequest,
    TopicGenerateRequest,
    TopicMappingRequest,
    TopicMergeRequest,
    TopicNoteBlockResponse,
    TopicPatchRequest,
    TopicReorderRequest,
    TopicResponse,
    TopicRunRequest,
    TopicRunResponse,
    TopicSplitRequest,
)
from parsing_core.workbench.codex_cli import CodexCliError, CodexCliExecutor, resolve_codex_path
from parsing_core.workbench.deepseek import DeepSeekClient, DeepSeekError, DeepSeekExecutor
from parsing_core.workbench.executors import StubIntensiveReadingExecutor
from parsing_core.workbench.hybrid import HybridIntensiveReadingExecutor
from parsing_core.workbench.keychain import KeychainError, read_secret
from parsing_core.workbench.markdown_sync import redact_sensitive_text
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.settings import load_settings
from parsing_core.workbench.topic_markdown_sync import (
    TopicMarkdownDeleteError,
    delete_unpublished_topic,
    merge_unpublished_topics,
)
from parsing_core.workbench.topic_outline import generate_topic_outline
from parsing_core.workbench.topic_pipeline import TopicFusionPipeline, TopicMarkdownSyncError

router = APIRouter(prefix="/api/workbench", tags=["workbench-topics"])
KEYCHAIN_SERVICE = "pdf2md.deepseek"
KEYCHAIN_ACCOUNT = "api-key"


def _repo(sch: SchedulerDep) -> WorkbenchRepository:
    return WorkbenchRepository(sch._query_orch.repo.conn)


def _settings_path(sch: SchedulerDep) -> Path:
    return Path(sch._query_orch.fs.base_dir) / "workbench-settings.json"


def _not_found(detail: str = "topic not found") -> HTTPException:
    return HTTPException(404, detail)


def _business_error(exc: Exception, *, conflict: bool = False) -> HTTPException:
    message = str(exc).lower()
    if "not found" in message:
        return _not_found("object not found")
    conflict_markers = (
        "running",
        "ready",
        "confirmed",
        "protected",
        "lease",
        "complete publication",
        "every topic",
        "no topics",
        "input changed",
        "owner lost",
        "already",
        "blocking chapter",
        "belong to the same course",
    )
    if conflict or any(marker in message for marker in conflict_markers):
        return HTTPException(409, "topic operation conflicts with current state")
    return HTTPException(400, "invalid topic operation")


def _safe_sync_error(error: str) -> str:
    if not error:
        return ""
    return "topic Markdown sync failed"


def _safe_run_error(error: str) -> str:
    if not error:
        return ""
    return "topic round execution failed"


def _topic_response(repo: WorkbenchRepository, topic, state: dict | None = None) -> TopicResponse:
    if state is None:
        chapters = repo.list_topic_chapters(topic.id)
        reviews = repo.list_topic_chapter_reviews(topic.id)
        chapter_ids = [chapter.id for chapter in chapters]
        blocking_ids = [
            chapter_id for chapter_id, status, stale in reviews if status != "DONE" or stale
        ]
        sync = repo.get_topic_markdown_sync_state(topic.id)
    else:
        chapter_ids = state["chapter_ids"]
        blocking_ids = state["blocking_chapter_ids"]
        sync = state["sync"]
    return TopicResponse(
        id=topic.id,
        course_id=topic.course_id,
        seq=topic.seq,
        title=topic.title,
        description=topic.description,
        generation_reason=topic.generation_reason,
        status=topic.status,
        confirmed=topic.confirmed,
        stale_reason=topic.stale_reason,
        chapter_ids=chapter_ids,
        blocking_chapter_ids=blocking_ids,
        sync_status=sync.status if sync else "PENDING",
        sync_error=_safe_sync_error(sync.error) if sync else "",
    )


def _sync_topics(repo: WorkbenchRepository, topic_ids: list[str]) -> None:
    for topic_id in topic_ids:
        try:
            TopicFusionPipeline(repo, StubIntensiveReadingExecutor()).retry_markdown_sync(topic_id)
        except TopicMarkdownSyncError:
            pass


class _StubTopicOutlineExecutor:
    def run(self, task_key: str, prompt: str) -> str:
        payload = json.loads(prompt.split("\nINPUT:\n", 1)[1])
        chapters = payload["chapters"]
        topics = [
            {
                "title": f"{chapter['source_title']} · {chapter['title']}",
                "description": f"Generated from {chapter['title']}",
                "chapter_ids": [chapter["id"]],
                "reason": "Deterministic stub outline",
            }
            for chapter in chapters
        ]
        return json.dumps({"topics": topics, "unmapped_chapter_ids": []})


def _deepseek_executor(sch: SchedulerDep):
    try:
        api_key = read_secret(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT).strip()
    except KeychainError as exc:
        raise HTTPException(400, "deepseek api key not configured") from exc
    if not api_key:
        raise HTTPException(400, "deepseek api key not configured")
    settings = load_settings(_settings_path(sch))
    return DeepSeekExecutor(DeepSeekClient(api_key, settings.deepseek_model))


def _hybrid_executor(sch: SchedulerDep, topic_id: str):
    repo = _repo(sch)
    topic = repo.get_topic(topic_id)
    if topic is None:
        raise _not_found()
    course = repo.get_course(topic.course_id)
    try:
        codex_path = resolve_codex_path()
    except CodexCliError as exc:
        raise HTTPException(400, "codex cli not configured") from exc
    run_dir = Path(course.root_dir) / ".pdf2md" / "topic-runs" / topic.id
    return HybridIntensiveReadingExecutor(
        _deepseek_executor(sch), CodexCliExecutor(codex_path, run_dir)
    )


@router.get("/courses/{course_id}/topics", response_model=list[TopicResponse])
def list_topics(course_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_course(course_id) is None:
        raise _not_found("course not found")
    topics = repo.list_topics(course_id)
    state = repo.course_topic_api_state(course_id)
    return [_topic_response(repo, topic, state[topic.id]) for topic in topics]


@router.post("/courses/{course_id}/topics", response_model=TopicResponse)
def create_topic(course_id: str, req: TopicCreateRequest, sch: SchedulerDep):
    repo = _repo(sch)
    try:
        topic = repo.create_topic_with_chapters(
            course_id, req.title, req.description, req.chapter_ids
        )
    except (ValueError, TypeError) as exc:
        raise _business_error(exc) from exc
    _sync_topics(repo, [topic.id])
    return _topic_response(repo, topic)


@router.post("/courses/{course_id}/topics/generate", response_model=list[TopicResponse])
def generate_topics(course_id: str, req: TopicGenerateRequest, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_course(course_id) is None:
        raise _not_found("course not found")
    executor = _StubTopicOutlineExecutor() if req.executor == "stub" else _deepseek_executor(sch)
    try:
        generate_topic_outline(repo, course_id, executor)
    except (ValidationError, ValueError) as exc:
        raise _business_error(exc) from exc
    except DeepSeekError as exc:
        raise HTTPException(502, "topic model execution failed") from exc
    topics = repo.list_topics(course_id)
    _sync_topics(repo, [topic.id for topic in topics])
    return [_topic_response(repo, topic) for topic in topics]


@router.post("/courses/{course_id}/topics/merge", response_model=TopicResponse)
def merge_topics(course_id: str, req: TopicMergeRequest, sch: SchedulerDep):
    repo = _repo(sch)
    try:
        topic = merge_unpublished_topics(
            repo,
            course_id,
            req.topic_ids,
            title=req.title,
            description=req.description,
            chapter_ids=req.chapter_ids,
        )
    except ValueError as exc:
        raise _business_error(exc) from exc
    _sync_topics(repo, [topic.id])
    return _topic_response(repo, topic)


@router.put("/courses/{course_id}/topics/reorder", response_model=list[TopicResponse])
def reorder_topics(course_id: str, req: TopicReorderRequest, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_course(course_id) is None:
        raise _not_found("course not found")
    try:
        topics = repo.reorder_topics(course_id, req.topic_ids)
    except ValueError as exc:
        raise _business_error(exc) from exc
    _sync_topics(repo, [topic.id for topic in topics])
    return [_topic_response(repo, topic) for topic in topics]


@router.post("/courses/{course_id}/topics/confirm", response_model=list[TopicResponse])
def confirm_topics(course_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    try:
        topics = repo.confirm_course_topics(course_id)
    except ValueError as exc:
        raise _business_error(exc, conflict=True) from exc
    _sync_topics(repo, [topic.id for topic in topics])
    return [_topic_response(repo, topic) for topic in topics]


@router.get("/topics/{topic_id}", response_model=TopicResponse)
def get_topic(topic_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    topic = repo.get_topic(topic_id)
    if topic is None:
        raise _not_found()
    return _topic_response(repo, topic)


@router.post("/topics/{topic_id}/split", response_model=list[TopicResponse])
def split_topic(topic_id: str, req: TopicSplitRequest, sch: SchedulerDep):
    repo = _repo(sch)
    try:
        topics = repo.split_topic(
            topic_id,
            title=req.title,
            description=req.description,
            new_chapter_ids=req.new_chapter_ids,
        )
    except ValueError as exc:
        raise _business_error(exc) from exc
    _sync_topics(repo, [topic.id for topic in topics])
    return [_topic_response(repo, topic) for topic in topics]


@router.patch("/topics/{topic_id}", response_model=TopicResponse)
def patch_topic(topic_id: str, req: TopicPatchRequest, sch: SchedulerDep):
    repo = _repo(sch)
    try:
        topic = repo.edit_topic_content(topic_id, title=req.title, description=req.description)
    except ValueError as exc:
        raise _business_error(exc) from exc
    _sync_topics(repo, [topic.id])
    return _topic_response(repo, topic)


@router.delete("/topics/{topic_id}", status_code=204)
def delete_topic(topic_id: str, sch: SchedulerDep):
    try:
        delete_unpublished_topic(_repo(sch), topic_id)
    except TopicMarkdownDeleteError as exc:
        raise HTTPException(507, "topic directory cleanup failed") from exc
    except OSError as exc:
        raise HTTPException(507, "topic directory cleanup failed") from exc
    except ValueError as exc:
        raise _business_error(exc) from exc
    return Response(status_code=204)


@router.put("/topics/{topic_id}/chapters", response_model=TopicResponse)
def map_topic(topic_id: str, req: TopicMappingRequest, sch: SchedulerDep):
    repo = _repo(sch)
    try:
        topic = repo.replace_topic_chapters_and_refresh(topic_id, req.chapter_ids)
    except ValueError as exc:
        raise _business_error(exc) from exc
    _sync_topics(repo, [topic.id])
    return _topic_response(repo, topic)


def _run_topic(topic_id: str, executor, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_topic(topic_id) is None:
        raise _not_found()
    pipeline = TopicFusionPipeline(repo, executor)
    try:
        pipeline.run(topic_id)
    except TopicMarkdownSyncError:
        pass
    except (ValueError, ValidationError) as exc:
        raise _business_error(exc) from exc
    except (DeepSeekError, CodexCliError) as exc:
        raise HTTPException(502, "topic model execution failed") from exc
    topic = repo.get_topic(topic_id)
    return _topic_response(repo, topic)


@router.post("/topics/{topic_id}/run", response_model=TopicResponse)
def run_topic(topic_id: str, req: TopicRunRequest, sch: SchedulerDep):
    return _run_topic(topic_id, StubIntensiveReadingExecutor(), sch)


@router.post("/topics/{topic_id}/run-hybrid", response_model=TopicResponse)
def run_topic_hybrid(topic_id: str, sch: SchedulerDep):
    return _run_topic(topic_id, _hybrid_executor(sch, topic_id), sch)


@router.post("/topics/{topic_id}/recover", response_model=TopicResponse)
def recover_topic(topic_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    try:
        topic = repo.recover_interrupted_topic_run(topic_id)
    except ValueError as exc:
        raise _business_error(exc, conflict=True) from exc
    return _topic_response(repo, topic)


@router.post("/topics/{topic_id}/sync/retry", response_model=TopicResponse)
def retry_topic_sync(topic_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_topic(topic_id) is None:
        raise _not_found()
    try:
        TopicFusionPipeline(repo, StubIntensiveReadingExecutor()).retry_markdown_sync(topic_id)
    except TopicMarkdownSyncError as exc:
        raise HTTPException(507, "topic Markdown sync failed") from exc
    except ValueError as exc:
        raise _business_error(exc, conflict=True) from exc
    return _topic_response(repo, repo.get_topic(topic_id))


@router.get("/topics/{topic_id}/note-blocks", response_model=list[TopicNoteBlockResponse])
def topic_note_blocks(topic_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_topic(topic_id) is None:
        raise _not_found()
    return repo.list_topic_note_blocks(topic_id)


@router.get("/topics/{topic_id}/cards", response_model=list[TopicCardResponse])
def topic_cards(topic_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_topic(topic_id) is None:
        raise _not_found()
    return [
        TopicCardResponse(
            id=card.id,
            topic_id=card.topic_id,
            card_type=card.card_type,
            title=card.title,
            content=card.content,
            source_refs=json.loads(card.source_refs_json),
            created_at=card.created_at,
        )
        for card in repo.list_topic_cards(topic_id)
    ]


@router.get("/topics/{topic_id}/runs", response_model=list[TopicRunResponse])
def topic_runs(topic_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_topic(topic_id) is None:
        raise _not_found()
    return [
        TopicRunResponse(
            id=run.id,
            topic_id=run.topic_id,
            round_key=run.round_key,
            status=run.status,
            input_fingerprint=run.input_fingerprint,
            output=redact_sensitive_text(run.output),
            error=_safe_run_error(run.error),
            started_at=run.started_at,
            finished_at=run.finished_at,
        )
        for run in repo.list_topic_runs(topic_id)
    ]
