import re
from dataclasses import dataclass


@dataclass
class ChapterCandidate:
    seq: int
    title: str
    raw_md: str


H2_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)


def detect_chapters(markdown: str) -> list[ChapterCandidate]:
    matches = list(H2_RE.finditer(markdown))
    if not matches:
        return [ChapterCandidate(seq=0, title="全文", raw_md=markdown.strip())]

    chapters: list[ChapterCandidate] = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        raw = markdown[start:end].strip()
        chapters.append(ChapterCandidate(seq=i, title=match.group(1).strip(), raw_md=raw))
    return chapters
