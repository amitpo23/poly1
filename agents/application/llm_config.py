"""Central LLM model defaults for live decision agents."""
from __future__ import annotations

import os


DEFAULT_OPENAI_MODEL = "gpt-4o"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"

JSON_MODE_OPENAI_MODELS = {
    "gpt-4o",
    "gpt-4o-mini",
}

OPENAI_TOKEN_LIMITS = {
    "gpt-3.5-turbo-16k": 15000,
    "gpt-4-1106-preview": 95000,
    "gpt-4o": 120000,
    "gpt-4o-mini": 120000,
}


def openai_model(env_var: str = "OPENAI_MODEL") -> str:
    return os.getenv(env_var, "").strip() or DEFAULT_OPENAI_MODEL


def anthropic_model(env_var: str = "ANTHROPIC_MODEL") -> str:
    return os.getenv(env_var, "").strip() or DEFAULT_ANTHROPIC_MODEL
