import os

TIER_CONFIGS = {
    "stub": {"model": "stub", "stream": False},
    "local": {
        "model": "ollama/llama3.2-vision:latest",
        "api_base": "http://localhost:11434",
        "stream": True,
    },
    "private": {
        "model": "openai/gpt-4o-mini",
        "api_base": None,
        "api_key": None,
        "stream": True,
    },
    "public": {
        "model": "openai/gpt-4o",
        "stream": True,
    },
}

PROMPT_CACHE_TIERS = {"public"}


def get_tier_config(tier: str) -> dict:
    cfg = TIER_CONFIGS.get(tier, TIER_CONFIGS["stub"]).copy()

    if tier == "local":
        cfg["model"] = os.getenv("PARSING_CORE_LOCAL_MODEL", cfg["model"])
        cfg["api_base"] = os.getenv("OLLAMA_HOST", cfg["api_base"])
    elif tier == "private":
        cfg["model"] = os.getenv("PARSING_CORE_PRIVATE_MODEL", cfg["model"])
        cfg["api_base"] = os.getenv("PARSING_CORE_PRIVATE_BASE_URL")
        cfg["api_key"] = os.getenv("PARSING_CORE_PRIVATE_API_KEY")
    elif tier == "public":
        cfg["model"] = os.getenv("PARSING_CORE_PUBLIC_MODEL", cfg["model"])

    cfg = {k: v for k, v in cfg.items() if v is not None}
    return cfg
