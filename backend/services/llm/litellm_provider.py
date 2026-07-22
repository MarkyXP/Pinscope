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

import asyncio
import base64
import importlib.util
import json
import logging
import random
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


def _encode_pdf_block(path: Path | str, model: str) -> dict:
    """Encode a PDF file as a base64 data-URI document block.

    LiteLLM normalises this to the provider's native format
    (Anthropic ``document``, OpenAI ``image_url``, etc.).

    For Azure, the PDF is uploaded as a file and referenced by ID.

    Docs: https://docs.litellm.ai/docs/completion/document_understanding
    """
    if model.startswith("azure/"):
        from backend.services.llm.azure_file_upload import upload_azure_file

        file_id = upload_azure_file(Path(path))
        return {"type": "file", "file": {"file_id": file_id}}

    encoded_file = base64.b64encode(Path(path).read_bytes()).decode()
    base64_url = f"data:application/pdf;base64,{encoded_file}"
    return {
        "type": "file",
        "file": {"file_data": base64_url},
    }


def _pdf_to_images(
    path: Path | str, dpi: int = 150, max_images: int = 50
) -> list[dict]:
    """Render pages of a PDF to PNG images at the given DPI.

    Returns a list of LiteLLM-compatible image blocks. If the PDF has more
    pages than ``max_images``, the excess pages are appended as a single
    text block so no content is lost.

    Docs: https://docs.litellm.ai/docs/completion/vision
    """
    doc = pymupdf.open(str(path))
    blocks: list[dict] = []
    zoom = dpi / 72.0  # PDF default is 72 DPI
    mat = pymupdf.Matrix(zoom, zoom, 0, 0)
    for i, page in enumerate(doc):
        if i < max_images:
            pix = page.get_pixmap(matrix=mat)
            img_b64 = base64.standard_b64encode(pix.tobytes("png")).decode()
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                }
            )
    # Append remaining pages as text if we hit the image limit
    if len(doc) > max_images:
        log.warning(
            "PDF has %d pages but image limit is %d — appending pages %d–%d as text",
            len(doc),
            max_images,
            max_images + 1,
            len(doc),
        )
        overflow_texts = [doc[i].get_text() for i in range(max_images, len(doc))]
        blocks.append({"type": "text", "text": "\n\n".join(overflow_texts)})
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
        return _encode_pdf_block(b.path, model=model)

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
    return {"type": "text", "text": "# " + b.path.name + "\n\n" + _pdf_to_text(b.path)}


def _is_openai_compat(model: str) -> bool:
    """Return True when the target provider expects OpenAI-format messages.

    Anthropic (native, Bedrock, Vertex) uses its own tool_use/tool_result
    content-block schema.  Every other provider (Azure, OpenAI, Gemini via
    OpenAI compat, …) expects the OpenAI function-call / tool-role schema.
    """
    return not (
        model.startswith("anthropic/")
        or "bedrock/anthropic" in model
        or model.startswith("vertex_ai/claude")
    )


def _to_litellm_message(m: Message, model: str) -> dict:
    blocks: list[dict] = []
    for b in m.content:
        result = _to_litellm_block(b, model)
        if isinstance(result, list):
            blocks.extend(result)
        else:
            blocks.append(result)
    return {"role": m.role, "content": blocks}


def _to_litellm_messages(messages: list[Message], model: str) -> list[dict]:
    """Convert a list of Messages to LiteLLM dicts.

    For Anthropic-native models the existing per-block encoding is used
    unchanged (tool_result inside user messages, tool_use inside assistant).

    For OpenAI-compatible providers (Azure, OpenAI, …) two transformations
    are applied:
    * ToolResultBlocks inside a user message are emitted as individual
      ``{"role": "tool", ...}`` messages — the only format Azure accepts.
    * ToolCall blocks inside an assistant message are emitted in the
      ``tool_calls`` array field instead of the content list.
    """
    if not _is_openai_compat(model):
        return [_to_litellm_message(m, model) for m in messages]

    result: list[dict] = []
    for m in messages:
        if m.role == "user":
            tool_results = [b for b in m.content if isinstance(b, ToolResultBlock)]
            other_blocks = [b for b in m.content if not isinstance(b, ToolResultBlock)]
            # Each ToolResultBlock becomes its own tool-role message.
            for tr in tool_results:
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": tr.tool_use_id,
                        "content": tr.content,
                    }
                )
            # Remaining content (text, PDFs, …) stays in a user message.
            if other_blocks:
                blocks: list[dict] = []
                for b in other_blocks:
                    r = _to_litellm_block(b, model)
                    if isinstance(r, list):
                        blocks.extend(r)
                    else:
                        blocks.append(r)
                result.append({"role": "user", "content": blocks})
        elif m.role == "assistant":
            tc_blocks = [b for b in m.content if isinstance(b, ToolCall)]
            text_blocks = [b for b in m.content if isinstance(b, TextBlock)]
            text_str: str | None = "".join(b.text for b in text_blocks) or None
            msg: dict = {"role": "assistant", "content": text_str}
            if tc_blocks:
                msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.input),
                        },
                    }
                    for tc in tc_blocks
                ]
            result.append(msg)
        else:
            result.append(_to_litellm_message(m, model))
    return result


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

    def safe_parse_json(s: str) -> dict:
        try:
            return json.loads(s)
        except Exception as e:
            log.warning("Failed to parse tool_call arguments as JSON: %s", e)
            return {}

    if settings.is_debug:
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
            input=safe_parse_json(tool_call.function.arguments),  # ["values"]
        )
        for tool_call in resp.choices[0].message.tool_calls or []
    ]
    # Populate raw_assistant_blocks so multi-turn conversation history
    # carries the assistant's text and tool_calls for the next request.
    if isinstance(text_parts, str) and text_parts:
        raw_blocks.append(TextBlock(text=text_parts))
    for tc in tool_calls:
        raw_blocks.append(tc)
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
            "messages": _to_litellm_messages(messages, self.model),
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

        _max_retries = 6
        _base_delay = 5.0
        _cap = 120.0
        for _attempt in range(_max_retries + 1):
            try:
                resp = await litellm.acompletion(**kwargs)
                return _from_litellm_response(resp)
            except litellm.RateLimitError as exc:
                if _attempt >= _max_retries:
                    raise
                # Honour Retry-After if the provider sends one.
                retry_after: float | None = None
                try:
                    hdr = getattr(exc, "response", None) and exc.response.headers.get(
                        "Retry-After"
                    )
                    if hdr:
                        retry_after = float(hdr)
                except Exception:
                    pass
                delay = min(
                    retry_after or (_base_delay * (2**_attempt)),
                    _cap,
                )
                # ±10 % jitter
                delay *= 1 + random.uniform(-0.1, 0.1)
                log.warning(
                    "RateLimitError on attempt %d/%d for model %s — "
                    "retrying in %.1fs",
                    _attempt + 1,
                    _max_retries,
                    self.model,
                    delay,
                )
                await asyncio.sleep(delay)

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
