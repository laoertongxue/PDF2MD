import re
from dataclasses import dataclass


@dataclass
class ChapterCandidate:
    seq: int
    title: str
    raw_md: str
    start: int = 0
    end: int = 0


HEADING_RE = re.compile(r"^(#{1,2})\s+(.+?)\s*$", re.MULTILINE)
CHAPTER_TITLE_RE = re.compile(r"^(?:第.{1,12}章(?:\s|$)|chapter\s+\w+)", re.IGNORECASE)


def detect_chapters(markdown: str) -> list[ChapterCandidate]:
    headings = list(HEADING_RE.finditer(markdown))
    h1 = [match for match in headings if len(match.group(1)) == 1]
    h2 = [match for match in headings if len(match.group(1)) == 2]
    chapter_headings = [match for match in headings if CHAPTER_TITLE_RE.match(match.group(2))]
    # A single H1 is normally the document title. Multiple H1s define chapters;
    # otherwise H2s are the chapter level.
    matches = chapter_headings if len(chapter_headings) > 1 else (h1 if len(h1) > 1 else h2)
    if not matches:
        return [
            ChapterCandidate(
                seq=0, title="全文", raw_md=markdown.strip(), start=0, end=len(markdown)
            )
        ]

    chapters: list[ChapterCandidate] = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        raw = markdown[start:end].strip()
        chapters.append(
            ChapterCandidate(seq=i, title=match.group(2).strip(), raw_md=raw, start=start, end=end)
        )
    return chapters
