
from parsing_core.llm.config import PROMPT_CACHE_TIERS, get_tier_config


def test_stub_tier_no_env_needed():
    cfg = get_tier_config("stub")
    assert cfg["model"] == "stub"
    assert not cfg["stream"]


def test_local_tier_defaults(monkeypatch):
    monkeypatch.delenv("PARSING_CORE_LOCAL_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    cfg = get_tier_config("local")
    assert "ollama" in cfg["model"]
    assert cfg["api_base"] == "http://localhost:11434"
    assert cfg["stream"]


def test_local_tier_env_override(monkeypatch):
    monkeypatch.setenv("PARSING_CORE_LOCAL_MODEL", "ollama/custom-model")
    monkeypatch.setenv("OLLAMA_HOST", "http://192.168.1.1:11434")
    cfg = get_tier_config("local")
    assert cfg["model"] == "ollama/custom-model"
    assert cfg["api_base"] == "http://192.168.1.1:11434"


def test_private_tier_with_base_url(monkeypatch):
    monkeypatch.setenv("PARSING_CORE_PRIVATE_BASE_URL", "https://vllm.internal/v1")
    monkeypatch.setenv("PARSING_CORE_PRIVATE_API_KEY", "sk-xxx")
    cfg = get_tier_config("private")
    assert cfg["api_base"] == "https://vllm.internal/v1"
    assert cfg["api_key"] == "sk-xxx"


def test_public_tier_default(monkeypatch):
    monkeypatch.delenv("PARSING_CORE_PUBLIC_MODEL", raising=False)
    cfg = get_tier_config("public")
    assert "gpt-4o" in cfg["model"]


def test_public_tier_env_override(monkeypatch):
    monkeypatch.setenv("PARSING_CORE_PUBLIC_MODEL", "claude-3-5-sonnet-20241022")
    cfg = get_tier_config("public")
    assert "claude" in cfg["model"]


def test_prompt_cache_tiers_only_public():
    assert "local" not in PROMPT_CACHE_TIERS
    assert "public" in PROMPT_CACHE_TIERS
