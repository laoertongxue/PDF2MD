import os
from typing import Required, TypedDict


class TierConfig(TypedDict, total=False):
    model: Required[str]
    stream: Required[bool]
    api_base: str
    api_key: str


TIER_CONFIGS: dict[str, TierConfig] = {
    "stub": {"model": "stub", "stream": False},
    "local": {
        "model": "ollama/llama3.2-vision:latest",
        "api_base": "http://localhost:11434",
        "stream": True,
    },
    "private": {
        "model": "openai/gpt-4o-mini",
        "stream": True,
    },
    "public": {
        "model": "openai/gpt-4o",
        "stream": True,
    },
}

PROMPT_CACHE_TIERS: set[str] = {"public"}


def get_tier_config(tier: str) -> TierConfig:
    cfg = TIER_CONFIGS.get(tier, TIER_CONFIGS["stub"]).copy()

    if tier == "local":
        cfg["model"] = os.getenv("PARSING_CORE_LOCAL_MODEL", cfg["model"])
        cfg["api_base"] = os.getenv("OLLAMA_HOST", cfg["api_base"])
    elif tier == "private":
        cfg["model"] = os.getenv("PARSING_CORE_PRIVATE_MODEL", cfg["model"])
        api_base = os.getenv("PARSING_CORE_PRIVATE_BASE_URL")
        if api_base is not None:
            cfg["api_base"] = api_base
        else:
            cfg.pop("api_base", None)
        api_key = os.getenv("PARSING_CORE_PRIVATE_API_KEY")
        if api_key is not None:
            cfg["api_key"] = api_key
        else:
            cfg.pop("api_key", None)
    elif tier == "public":
        cfg["model"] = os.getenv("PARSING_CORE_PUBLIC_MODEL", cfg["model"])

    return cfg
