"""LiteLLM provider — unified wrapper around litellm.acompletion.

Translates the unified ``Message`` / ``Completion`` shapes into LiteLLM's
native message format. One provider handles all models; the model string
must use the LiteLLM format (e.g. ``anthropic/claude-sonnet-4-6`` or
``openai/gpt-4o``).

Caching: LiteLLM passes through ``cache_control`` markers for providers
that support it (Anthropic, OpenAI). This provider does not enforce
per-request breakpoint limits — that logic is provider-specific and
handled by the upstream SDK.

PDF handling: If the model supports native PDF input, the PDF is sent as-is.
Otherwise the provider falls back to images (via pymupdf) if the model
supports vision, or to plain text extraction if neither is available.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

import litellm
import pymupdf
from litellm.utils import supports_pdf_input, supports_vision

from backend.config import settings
from backend.services.llm.base import LLMProvider, LLMSession
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

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Translation helpers — unified types ↔ LiteLLM dicts
# ---------------------------------------------------------------------------


def _encode_pdf_block(path: Path | str) -> dict:
    """Encode a PDF file as a base64 data-URI document block.

    LiteLLM normalises this to the provider's native format
    (Anthropic ``document``, OpenAI ``image_url``, etc.).
    """
    data = base64.standard_b64encode(Path(path).read_bytes()).decode()
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": data,
        },
    }


def _pdf_to_images(path: Path | str, dpi: int = 150) -> list[dict]:
    """Render every page of a PDF to PNG images at the given DPI.

    Returns a list of LiteLLM-compatible image blocks.
    """
    doc = pymupdf.open(str(path))
    blocks: list[dict] = []
    zoom = dpi / 72.0  # PDF default is 72 DPI
    mat = pymupdf.Matrix(zoom, zoom, 0, 0)
    for page in doc:
        pix = page.get_pixmap(matrix=mat)
        img_b64 = base64.standard_b64encode(pix.tobytes("png")).decode()
        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"},
            }
        )
    doc.close()
    return blocks


def _pdf_to_text(path: Path | str) -> str:
    """Extract plain text from every page of a PDF."""
    doc = pymupdf.open(str(path))
    texts = [page.get_text() for page in doc]
    doc.close()
    return "\n\n".join(texts)


def _to_litellm_block(b: ContentBlock, model: str) -> dict | list[dict]:
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, PdfBlock):
        return _handle_pdf_block(b, model)
    if isinstance(b, ToolCall):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    if isinstance(b, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": b.tool_use_id,
            "content": b.content,
        }
    raise TypeError(f"Unknown ContentBlock: {type(b).__name__}")


def _handle_pdf_block(b: PdfBlock, model: str) -> dict | list[dict]:
    """Decide how to encode a PdfBlock based on model capabilities.

    Priority: native PDF → images (if vision) → text extraction.
    """
    if supports_pdf_input(model=model):
        return _encode_pdf_block(b.path)

    if supports_vision(model=model):
        dpi = settings.pdf_render_dpi
        log.warning(
            "Model '%s' does not support native PDF input — rendering as images "
            "at %s DPI via pymupdf",
            model,
            dpi,
        )
        return _pdf_to_images(b.path, dpi=dpi)

    log.warning(
        "Model '%s' does not support PDF or vision — extracting text only",
        model,
    )
    return {"type": "text", "text": _pdf_to_text(b.path)}


def _to_litellm_message(m: Message, model: str) -> dict:
    blocks: list[dict] = []
    for b in m.content:
        result = _to_litellm_block(b, model)
        if isinstance(result, list):
            blocks.extend(result)
        else:
            blocks.append(result)
    return {"role": m.role, "content": blocks}


def _to_litellm_tool(t: ToolSchema) -> dict:
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.input_schema,
        },
    }


def _to_litellm_tool_choice(c: ToolChoice) -> dict | str | None:
    if c == "auto":
        return "auto"
    if c == "none":
        return "none"
    if isinstance(c, dict) and "name" in c:
        return {"type": "function", "function": {"name": c["name"]}}
    raise ValueError(f"Invalid tool_choice: {c!r}")


def _from_litellm_response(resp) -> Completion:
    """Parse a litellm ModelResponse into a unified Completion."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    raw_blocks: list[ContentBlock] = []

    # for block in resp.choices[0].message.content:
    #     btype = getattr(block, "type", None)
    #     if btype == "text":
    #         text_parts.append(block.text)
    #         raw_blocks.append(TextBlock(text=block.text))
    #     elif btype == "tool_use":
    #         tc = ToolCall(id=block.id, name=block.name, input=dict(block.input))
    #         tool_calls.append(tc)
    #         raw_blocks.append(tc)
    text_parts = resp.choices[0].message.content or ""
    print("--------------------------------------------------")
    print(f"{resp}")
    if resp.choices[0].message.tool_calls:
        print(f"{resp.choices[0].message.tool_calls[0].id=}")
        print(f"{resp.choices[0].message.tool_calls[0].function.name=}")
        print(f"{resp.choices[0].message.tool_calls[0].function.arguments=}")
    print("--------------------------------------------------")
    tool_calls = [
        ToolCall(
            id=tool_call.id,
            name=tool_call.function.name,
            input=json.loads(tool_call.function.arguments),  # ["values"]
        )
        for tool_call in resp.choices[0].message.tool_calls or []
    ]
    usage = resp.usage
    usage_obj = Usage(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cache_creation_tokens=getattr(usage, "prompt_tokens_details", None)
        and getattr(usage.prompt_tokens_details, "cached_tokens", 0)
        or 0,
        cache_read_tokens=getattr(usage, "completion_tokens_details", None)
        and getattr(usage.completion_tokens_details, "cached_tokens", 0)
        or 0,
    )

    return Completion(
        text="".join(text_parts),
        tool_calls=tool_calls,
        usage=usage_obj,
        stop_reason=resp.choices[0].finish_reason,
        raw_assistant_blocks=raw_blocks,
    )


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class LiteLLMSession(LLMSession):
    provider_name = "litellm"

    def __init__(
        self,
        *,
        model: str,
        system: str,
        max_tokens: int,
        temperature: float | None = None,
        **extra_kwargs: Any,
    ) -> None:
        self.model = model
        self._system = system
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._extra_kwargs = extra_kwargs

    async def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        tool_choice: ToolChoice = "auto",
    ) -> Completion:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [_to_litellm_message(m, self.model) for m in messages],
            "stream": False,
        }
        if self._system:
            kwargs["messages"] = [
                {"role": "system", "content": self._system},
                *kwargs["messages"],
            ]
        if self._max_tokens:
            kwargs["max_tokens"] = self._max_tokens
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if tools:
            kwargs["tools"] = [_to_litellm_tool(t) for t in tools]
            kwargs["tool_choice"] = _to_litellm_tool_choice(tool_choice)
        kwargs.update(self._extra_kwargs)

        resp = await litellm.acompletion(**kwargs)
        # print(kwargs, resp)
        return _from_litellm_response(resp)
        return _from_litellm_response(resp.choices[0].message)

    async def close(self) -> None:
        # LiteLLM has no persistent session state to clean up.
        pass


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class LiteLLMProvider(LLMProvider):
    """Unified LiteLLM provider.

    Accepts any model string that LiteLLM supports (e.g.
    ``anthropic/claude-sonnet-4-6``, ``openai/gpt-4o``,
    ``gemini/gemini-2.5-pro``). Routes automatically based on the
    model prefix.
    """

    name = "litellm"

    def __init__(self, **extra_kwargs: Any) -> None:
        self._extra_kwargs = extra_kwargs

    async def create_session(
        self,
        *,
        model: str,
        system: str,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMSession:
        return LiteLLMSession(
            model=model,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            **self._extra_kwargs,
        )

    # Resolve skills/ directory relative to the project root
    # (same root that config.py uses: backend/../skills/)
    _SKILLS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "skills"

    async def run_skill(
        self,
        *,
        skill_name: str,
        model: str,
        system: str,
        user_text: str,
        pdf_path: str | None,
        output_tool: ToolSchema,
    ) -> tuple[dict, Completion]:
        """Execute a local skill: SKILL.md → LLM call → local validation.

        1. Load SKILL.md as the system prompt (prepended to caller's system).
        2. Build a user message with optional PdfBlock + TextBlock.
        3. Call the model with a forced tool choice.
        4. Extract the tool input and run local validate(data).
        5. Return (data, Completion).
        """
        skill_dir = self._SKILLS_DIR / skill_name
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            raise FileNotFoundError(
                f"Skill '{skill_name}' not found at {skill_md}. "
                f"Ensure skills/{skill_name}/SKILL.md exists."
            )

        # Strip YAML frontmatter (lines between leading --- and ---)
        raw = skill_md.read_text()
        skill_prompt = raw
        if raw.startswith("---"):
            end = raw.find("---", 3)
            if end != -1:
                skill_prompt = raw[end + 3 :].lstrip("\n")

        # Combine skill prompt with caller's dynamic system context
        combined_system = f"{skill_prompt}\n\n{system}"

        # Build user message
        blocks: list[ContentBlock] = []
        if pdf_path is not None:
            blocks.append(PdfBlock(path=Path(pdf_path)))
        blocks.append(TextBlock(text=user_text))
        user_msg = Message(role="user", content=blocks)

        # Create session and force the tool call
        session = await self.create_session(
            model=model,
            system=combined_system,
            max_tokens=8192,
            temperature=0.0,
        )
        try:
            completion = await session.complete(
                messages=[user_msg],
                tools=[output_tool],
                tool_choice={"name": output_tool.name},
            )
        finally:
            await session.close()

        if not completion.tool_calls:
            raise RuntimeError(
                f"Skill '{skill_name}' returned no tool calls. "
                f"Model may have refused to use the output tool."
            )

        data = completion.tool_calls[0].input

        # Run local validation
        validate_fn = self._load_validate(skill_dir)
        errors = validate_fn(data)
        if errors:
            log.warning("Skill '%s' validation failed: %s", skill_name, errors)
            # Return data anyway — caller may decide to retry or accept

        return data, completion

    @staticmethod
    def _load_validate(skill_dir: Path) -> "callable":
        """Dynamically import validate(data: dict) -> list[str] from a skill's validate.py."""
        validate_py = skill_dir / "validate.py"
        if not validate_py.exists():
            log.warning("No validate.py at %s — skipping validation", validate_py)
            return lambda data: []  # no-op validator

        spec = importlib.util.spec_from_file_location(
            f"skill_validate_{skill_dir.name}", validate_py
        )
        if spec is None or spec.loader is None:
            return lambda data: []

        mod = importlib.util.module_from_spec(spec)
        # Isolate from project sys.modules so re-loading doesn't cache stale code
        sys.modules[mod.__name__] = mod
        spec.loader.exec_module(mod)

        if not hasattr(mod, "validate"):
            log.warning(
                "validate.py at %s has no validate() function — skipping",
                validate_py,
            )
            return lambda data: []

        return mod.validate
