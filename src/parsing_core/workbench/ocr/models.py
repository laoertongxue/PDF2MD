from dataclasses import dataclass
from typing import Literal

OcrEngine = Literal["apple_vision", "codex_vision", "baidu_pp_structure"]
OcrDecisionStatus = Literal[
    "direct", "automated_adjudicated", "waiting_resource", "failed"
]
PageBlockType = Literal[
    "title", "body", "page_number", "footnote", "table", "formula", "image", "text"
]


@dataclass(frozen=True)
class OcrPage:
    id: str
    source_id: str
    page_number: int
    render_config_hash: str
    image_path: str
    input_hash: str
    created_at: int


@dataclass(frozen=True)
class OcrObservation:
    id: str
    page_id: str
    engine: OcrEngine
    input_hash: str
    engine_config_hash: str
    payload_json: str
    created_at: int


@dataclass(frozen=True)
class OcrDiff:
    id: str
    page_id: str
    left_observation_id: str
    right_observation_id: str
    diff_json: str
    adjudication_reason: str
    created_at: int


@dataclass(frozen=True)
class OcrDecision:
    page_id: str
    status: OcrDecisionStatus
    final_blocks_json: str
    evidence_json: str
    confidence: float
    decided_at: int


@dataclass(frozen=True)
class PageBlock:
    id: str
    page_id: str
    seq: int
    block_type: PageBlockType
    text: str
    bbox_json: str
    confidence: float
    created_at: int


@dataclass(frozen=True)
class OcrLease:
    page_id: str
    owner_id: str
    heartbeat_at: int
    expires_at: int
    input_fingerprint: str
