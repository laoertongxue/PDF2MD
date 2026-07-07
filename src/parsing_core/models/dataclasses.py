from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Task:
    id: str
    file_path: str
    snapshot_path: str
    file_sha256: str
    status: str  # PENDING|PARSING|SECTIONING|LLM_RUNNING|MERGING|COMPLETED|FAILED
    model_tier: str = "stub"
    created_at: int = 0
    updated_at: int = 0
    error_msg: str | None = None
    batch_id: str | None = None


@dataclass
class Section:
    id: str
    task_id: str
    seq: int
    raw_md_path: str
    sha256: str
    char_count: int
    ai_status: str = "PENDING"  # PENDING|RUNNING|COMPLETED|FAILED|PARTIAL_SUCCESS
    created_at: int = 0


@dataclass
class AIArtifact:
    id: str
    section_id: str
    ai_md_path: str = ""
    ai_md: str = ""  # 内存中的解读内容，落盘后清空可选
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    retry_count: int = 0
    model_name: str | None = None
    created_at: int = 0
