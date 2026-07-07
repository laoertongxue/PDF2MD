import re
from dataclasses import dataclass

from parsing_core.utils.hashing import text_sha256

MAX_SECTION_CHARS = 4000
MIN_SECTION_CHARS = 100
MIN_SHORT_PARA_THRESHOLD = 100  # < 此长度视为短段

HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
TABLE_BLOCK_RE = re.compile(r"(\n\|[^\n]+\|\n\|[\s:|-]+\|\n(?:\|[^\n]+\|\n?)+)", re.MULTILINE)


@dataclass
class Section:
    seq: int
    title: str
    raw: str
    sha256: str
    char_count: int


def split_sections(markdown: str) -> list[Section]:
    units = _split_by_structure(markdown)
    units = _split_long(units)
    units = _merge_short(units)
    return [
        Section(seq=i, title=_extract_title(u), raw=u, sha256=text_sha256(u), char_count=len(u))
        for i, u in enumerate(units)
    ]


def _split_by_structure(markdown: str) -> list[str]:
    # 按 H2/H3 标题、独立表格、独立大段落切
    parts: list[str] = []
    lines = markdown.splitlines(keepends=True)
    current: list[str] = []

    def flush():
        nonlocal current
        if current:
            parts.append("".join(current))
            current = []

    for line in lines:
        if re.match(r"^#\s+", line):
            # H1 视为文档标题，不计为节
            continue
        if re.match(r"^#{2,3}\s+", line):
            flush()
            current.append(line)
        else:
            current.append(line)
    flush()

    # 再按独立表格切（在已有 parts 上做二次细分）
    refined: list[str] = []
    for p in parts:
        start = 0
        for m in TABLE_BLOCK_RE.finditer(p):
            if m.start() > start:
                refined.append(p[start : m.start()])
            refined.append(m.group(1))
            start = m.end()
        if start < len(p):
            refined.append(p[start:])

    return [r for r in refined if r.strip()]


def _split_long(units: list[str]) -> list[str]:
    out: list[str] = []
    for u in units:
        if len(u) <= MAX_SECTION_CHARS:
            out.append(u)
            continue
        # 按段落边界（空行）切
        paras = re.split(r"(\n\n+)", u)
        chunk = ""
        for seg in paras:
            if len(seg) > MAX_SECTION_CHARS:
                # 段落本身超长且无内部边界，硬切
                if chunk:
                    out.append(chunk)
                    chunk = ""
                for i in range(0, len(seg), MAX_SECTION_CHARS):
                    out.append(seg[i : i + MAX_SECTION_CHARS])
            elif len(chunk) + len(seg) <= MAX_SECTION_CHARS:
                chunk += seg
            else:
                if chunk:
                    out.append(chunk)
                chunk = seg
        if chunk:
            out.append(chunk)
    return out


def _merge_short(units: list[str]) -> list[str]:
    if not units:
        return []
    out: list[str] = [units[0]]
    for u in units[1:]:
        if len(u) < MIN_SHORT_PARA_THRESHOLD and not _is_table(u) and not _is_header_line(u):
            out[-1] = out[-1] + u
        else:
            out.append(u)
    return out


def _is_table(text: str) -> bool:
    return bool(TABLE_BLOCK_RE.search(text))


def _is_header_line(text: str) -> bool:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return bool(re.match(r"^#{1,6}\s+", first_line))


def _extract_title(text: str) -> str:
    m = HEADER_RE.match(text.strip())
    return m.group(2) if m else "(无标题)"
