import json
import os
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from parsing_core.workbench.markdown_sync import (
    _first_line_regular_file,
    _pure_mermaid,
    atomic_write_bundle_fd,
    ensure_directory_owner,
    migrate_generated_directory,
    open_secure_directory,
    redact_sensitive_text,
    safe_name,
    textbook_dir,
)
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.topic_task_package import allocate_source_display_titles

FIXED_TOPIC_KINDS = (
    "overview",
    "linked_sources",
    "core_concepts",
    "viewpoint_comparison",
    "consensus_disagreements",
    "complementary_views",
    "plain_explanation",
    "textbook_cases",
    "real_world_problem_solving",
    "integrated_framework",
    "application_methods",
    "further_thinking",
    "knowledge_mermaid",
    "application_mermaid",
)

SECTION_TITLES = {
    "overview": "主题概要",
    "linked_sources": "关联教材与章节",
    "core_concepts": "核心概念",
    "viewpoint_comparison": "教材观点对照",
    "consensus_disagreements": "共识与分歧",
    "complementary_views": "互补视角",
    "plain_explanation": "通俗有趣生活化解释",
    "textbook_cases": "教材案例",
    "real_world_problem_solving": "现实案例与问题解决",
    "integrated_framework": "综合分析框架",
    "application_methods": "实际应用方法",
    "further_thinking": "延伸思考",
    "knowledge_mermaid": "Mermaid知识结构图",
    "application_mermaid": "Mermaid应用流程图",
}


class TopicMarkdownSyncError(Exception):
    pass


def sync_topic_markdown(
    repo: WorkbenchRepository,
    topic_id: str,
    *,
    fence: Callable[[], object] | None = None,
) -> dict[str, str]:
    with ExitStack() as stack:
        return _sync_topic_markdown(repo, topic_id, fence=fence, stack=stack)


def _sync_topic_markdown(
    repo: WorkbenchRepository,
    topic_id: str,
    *,
    fence: Callable[[], object] | None,
    stack: ExitStack,
) -> dict[str, str]:
    topic = repo.get_topic(topic_id)
    if topic is None:
        raise ValueError("topic not found")
    course = repo.get_course(topic.course_id)
    if course is None:
        raise ValueError("course not found")
    blocks = {item.kind: item.content for item in repo.list_topic_note_blocks(topic_id)}
    if set(blocks) != set(FIXED_TOPIC_KINDS):
        raise ValueError("topic must contain exactly fourteen blocks")
    cards = repo.list_topic_cards(topic_id)
    if not 8 <= len(cards) <= 12:
        raise ValueError("topic cards must contain 8..12 items")
    chapters = repo.list_topic_chapters(topic_id)
    sources = {chapter.source_id: repo.get_source(chapter.source_id) for chapter in chapters}
    display = allocate_source_display_titles(
        [(source.id, source.title) for source in repo.list_sources(topic.course_id)]
    )
    allowed_refs = {
        f"[《{display[chapter.source_id]}》·第 {chapter.seq + 1} 章]" for chapter in chapters
    }
    parsed_refs = []
    for card in cards:
        try:
            refs = json.loads(card.source_refs_json)
        except json.JSONDecodeError as exc:
            raise ValueError("topic card source refs must be list[str]") from exc
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
            raise ValueError("topic card source refs must be list[str]")
        if not set(refs) <= allowed_refs:
            raise ValueError("topic card contains unknown source ref")
        parsed_refs.append(refs)

    course_root = Path(course.root_dir)
    course_root.mkdir(parents=True, exist_ok=True)
    if course_root.is_symlink():
        raise OSError("course root symlink rejected")
    topics_root = Path(course.root_dir) / "课程主题"
    target = topics_root / f"{topic.seq + 1:02d}-{safe_name(topic.title)}"
    topics_fd = open_secure_directory(course_root, ["课程主题"])
    stack.callback(os.close, topics_fd)
    target_existed = target.exists()
    topic_dir = migrate_generated_directory(
        topics_root,
        target,
        f"<!-- topic-id: {topic.id} -->",
        entity_type="topic",
        entity_id=topic.id,
    )
    relative = topic_dir.relative_to(course_root)
    topic_fd = open_secure_directory(course_root, list(relative.parts))
    stack.callback(os.close, topic_fd)
    formal_owner = _first_line_regular_file(topic_dir / "topic-map.md") == (
        f"<!-- topic-id: {topic.id} -->"
    )
    ensure_directory_owner(
        topic_fd,
        "topic",
        topic.id,
        allow_create_or_replace=not target_existed or formal_owner,
    )
    runs_fd = open_secure_directory(course_root, [*relative.parts, "runs"])
    stack.callback(os.close, runs_fd)
    marker = f"<!-- topic-id: {topic.id} -->"
    note_lines = [marker, f"# {topic.title}", ""]
    for kind in FIXED_TOPIC_KINDS:
        note_lines.extend([f"## {SECTION_TITLES[kind]}", ""])
        if kind.endswith("mermaid"):
            note_lines.extend(["```mermaid", _pure_mermaid(blocks[kind]), "```", ""])
        else:
            note_lines.extend([blocks[kind], ""])
    note_lines.extend(["## 写作卡片", ""])
    for card in cards:
        note_lines.extend([f"- [{card.title}](cards.md#{safe_name(card.title)})：{card.content}"])
    note_lines.append("")

    card_lines = [f"# {topic.title} 写作卡片", ""]
    for card, refs in zip(cards, parsed_refs, strict=True):
        card_lines.extend(
            [
                f"## {card.title}",
                "",
                f"类型：{card.card_type}",
                f"来源：{'、'.join(refs)}",
                "",
                card.content,
                "",
            ]
        )

    map_lines = [
        marker,
        f"# {topic.title}",
        "",
        topic.description,
        "",
        f"生成原因：{topic.generation_reason}",
        f"状态：{topic.status}",
        f"已确认：{'是' if topic.confirmed else '否'}",
        "",
        "## 教材章节",
        "",
    ]
    for chapter in chapters:
        source = sources[chapter.source_id]
        chapter_note = (
            textbook_dir(repo, source)
            / f"{chapter.seq + 1:02d}-{safe_name(chapter.title)}"
            / "intensive-note.md"
        )
        relative = Path("../..") / chapter_note.relative_to(course.root_dir)
        label = f"[《{display[source.id]}》·第 {chapter.seq + 1} 章]"
        map_lines.append(f"- {label} [{chapter.title}]({relative.as_posix()})")
    map_lines.append("")

    paths = {
        "map": topic_dir / "topic-map.md",
        "note": topic_dir / "intensive-note.md",
        "cards": topic_dir / "cards.md",
    }
    atomic_write_bundle_fd(
        topic_fd,
        {
            "topic-map.md": "\n".join(map_lines),
            "intensive-note.md": "\n".join(note_lines),
            "cards.md": "\n".join(card_lines),
        },
        fence=fence,
    )
    run_contents = {}
    for run in repo.list_topic_runs(topic_id):
        name = f"{run.started_at}-{safe_name(run.id)}-{safe_name(run.round_key)}.md"
        output = redact_sensitive_text(run.output)
        error = redact_sensitive_text(run.error)
        run_contents[name] = "\n".join(
            [
                f"# {run.round_key}",
                "",
                f"状态：{run.status}",
                f"输出：{output}",
                f"错误：{error}",
                "",
            ]
        )
    if run_contents:
        atomic_write_bundle_fd(runs_fd, run_contents, fence=fence)
    return {key: str(path) for key, path in paths.items()}
