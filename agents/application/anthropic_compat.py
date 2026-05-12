"""Lightweight helpers for the Anthropic API fallback.

Kept in a separate module (no heavy deps) so unit tests can import
this without pulling in langchain, httpx, or the full executor stack.
"""
from __future__ import annotations
from typing import Optional


def to_anthropic_messages(messages) -> tuple[Optional[str], list[dict]]:
    """Convert LangChain messages or a plain string to Anthropic API format.

    Returns (system_text_or_None, list_of_role_content_dicts).

    Handles:
    - str  → single user message
    - list of LangChain HumanMessage / SystemMessage / AIMessage objects
    """
    if isinstance(messages, str):
        return None, [{"role": "user", "content": messages.strip()}]

    system: Optional[str] = None
    anth_msgs: list[dict] = []
    for m in messages:
        # Detect by class name so this module doesn't need to import langchain
        cls_name = type(m).__name__
        if cls_name == "SystemMessage":
            system = m.content
        elif cls_name == "HumanMessage":
            anth_msgs.append({"role": "user", "content": m.content.strip()})
        else:
            content = getattr(m, "content", str(m)).strip()
            if content:  # skip empty / whitespace-only assistant turns
                anth_msgs.append({"role": "assistant", "content": content})
    return system, anth_msgs
