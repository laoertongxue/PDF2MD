from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from parsing_core.workbench.models import CourseTopic
    from parsing_core.workbench.repository import WorkbenchRepository

DRAFT = "DRAFT"
NOT_READY = "NOT_READY"
READY = "READY"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
STALE = "STALE"
FAILED = "FAILED"

@dataclass(frozen=True)
class TopicReadiness:
    status: str
    blocking_chapter_ids: list[str]


def evaluate_topic_readiness(
    repo: "WorkbenchRepository",
    topic_id: str,
) -> TopicReadiness:
    topic = repo.get_topic(topic_id)
    if topic is None:
        raise ValueError("topic not found")

    reviews = repo.list_topic_chapter_reviews(topic_id)
    if not topic.confirmed or not reviews:
        return TopicReadiness(DRAFT, [])

    blocking_ids = [
        chapter_id
        for chapter_id, status, stale in reviews
        if status != "DONE" or stale
    ]
    blocking_ids = list(dict.fromkeys(blocking_ids))
    if blocking_ids:
        return TopicReadiness(NOT_READY, blocking_ids)
    return TopicReadiness(READY, [])


def refresh_topic_status(repo: "WorkbenchRepository", topic_id: str) -> "CourseTopic":
    return repo.refresh_topic_status(topic_id)


def mark_topic_stale(
    repo: "WorkbenchRepository",
    topic_id: str,
    reason: str,
) -> "CourseTopic":
    return repo.mark_topic_stale(topic_id, reason)


def mark_topics_stale_for_chapter(
    repo: "WorkbenchRepository",
    chapter_id: str,
    reason: str,
    *,
    round_keys: list[str] | None = None,
) -> list["CourseTopic"]:
    return repo.invalidate_chapter_dependencies(chapter_id, round_keys or [], reason)
