"""Provider-agnostic LLM client layer.

All LLM API calls in the backend route through this package via the
``LLMProvider`` interface. The default provider is LiteLLM, which unifies
routing to any LLM (Anthropic, OpenAI, Gemini, etc.) via model name prefixes
(e.g. ``anthropic/claude-sonnet-4-6``, ``openai/gpt-4o``). Per-stage model
overrides via ``Settings.provider_for_stage_*`` env vars select the model
used for that stage.
"""

from backend.services.llm.factory import call_with_fallback, get_provider
from backend.services.llm.types import (
    Completion,
    ContentBlock,
    Message,
    PdfBlock,
    TextBlock,
    ToolCall,
    ToolChoice,
    ToolResultBlock,
    ToolSchema,
    Usage,
)

__all__ = [
    "Completion",
    "ContentBlock",
    "Message",
    "PdfBlock",
    "TextBlock",
    "ToolCall",
    "ToolChoice",
    "ToolResultBlock",
    "ToolSchema",
    "Usage",
    "call_with_fallback",
    "get_provider",
]
