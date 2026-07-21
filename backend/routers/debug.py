"""Debug-only API router — skill-based PDF extraction endpoint.

This router is *only* included in ``main.py`` when ``settings.is_debug`` is
True (i.e. ``APP_VERSION`` is not set in the environment).

===============================================================================
DATA FLOW — How a PDF becomes structured extraction output
===============================================================================

Request flow::

    POST /debug/extract
    ├─ skill: "extract-pintable" | "extract-pattern" | "extract-specs"
    ├─ pdf_path: optional (defaults to first PDF in simple_project/ds/)
    └─ component_type: required for "extract-specs" (e.g. "diode", "capacitor")

    │
    ▼
[1] Resolve PDF path
    _resolve_pdf(pdf_path)
    ├─ If None → list simple_project/ds/*.pdf → return first match
    ├─ If relative → join with project root / simple_project/ds/
    └─ If absolute → use as-is
    │
    ▼
[2] Select relevant pages
    _select_pages(pdf_path, keywords, max_pages=90)
    ├─ Always include pages 0-4 (title, TOC, overview)
    ├─ Scan all pages for keyword matches → include matched + neighbors (±1)
    ├─ If still under budget, pad with remaining pages from the front
    └─ Write trimmed PDF to a temp file (caller is responsible for cleanup)
    │
    ▼
[3] Build system prompt
    _build_system_prompt(skill_name, pdf_path)
    ├─ Load skills/{skill_name}/SKILL.md
    ├─ Strip YAML frontmatter (lines between leading --- and ---)
    ├─ Append dynamic context:
    │   ├─ Existing taxonomy subtypes (for subtype assignment)
    │   ├─ BOM MPNs / example part numbers (for pattern matching)
    │   └─ Parameters to extract (for specs extraction)
    └─ Combine: skill_prompt + "\n\n" + dynamic_system
    │
    ▼
[4] Build user message
    Message(role="user", content=[PdfBlock, TextBlock])
    ├─ PdfBlock(path=trimmed_pdf_path)
    │   └─ Provider encodes based on model capabilities:
    │       ├─ supports_pdf_input(model) → base64 data-URI / Azure upload
    │       ├─ supports_vision(model) → pymupdf images (150 DPI, max 50)
    │       └─ fallback → pymupdf text extraction
    └─ TextBlock(text=user_instruction)
        └─ "Extract the data from this PDF using the save_xxx tool."
    │
    ▼
[5] Call LLM with forced tool
    provider.run_skill(
        skill_name=skill_name,
        model=model_for_stage(skill_name),
        system=combined_system,
        user_text="Extract data from this PDF.",
        pdf_path=trimmed_pdf_path,
        output_tool=<TOOL_SCHEMA>,
    )
    │
    ├─ create_session(model, system, max_tokens=8192, temperature=0.0)
    ├─ session.complete(messages=[user_msg], tools=[output_tool],
    │                    tool_choice={"name": output_tool.name})
    │   └─ litellm.acompletion(...) → ModelResponse
    │
    ├─ Extract completion.tool_calls[0].input
    │
    ├─ Run validate(data) from skills/{skill_name}/validate.py
    │   └─ Returns list[str] of errors (empty = valid)
    │
    └─ Return (data, Completion)
    │
    ▼
Response:
{
    "skill": "extract-pintable",
    "pdf_path": "/path/to/file.pdf",
    "pages_used": [0, 1, 2, 3, 4, 10, 11, 12],
    "model": "anthropic/claude-sonnet-4-6",
    "data": { ... extracted JSON ... },
    "validation_errors": [],
    "usage": {
        "input_tokens": 12345,
        "output_tokens": 678,
        "cache_creation_tokens": 8000,
        "cache_read_tokens": 0
    },
    "stop_reason": "tool_calls"
}

===============================================================================
UML SEQUENCE DIAGRAM
===============================================================================

User           Debug Router         LiteLLM Provider       LLM API
 │                  │                       │                  │
 │  POST /debug/extract                   │                  │
 │  {skill, pdf}    │                       │                  │
 │─────────────────>│                       │                  │
 │                  │                       │                  │
 │                  │  _resolve_pdf()       │                  │
 │                  │  _select_pages()      │                  │
 │                  │                       │                  │
 │                  │  run_skill()          │                  │
 │                  │  ├─ load SKILL.md     │                  │
 │                  │  ├─ strip frontmatter │                  │
 │                  │  ├─ build system      │                  │
 │                  │  ├─ create_session()  │                  │
 │                  │  │   └─ max_tokens=8192│                  │
 │                  │  │   └─ temperature=0.0│                  │
 │                  │  ├─ complete()        │                  │
 │                  │  │   ├─ encode PDF     │                  │
 │                  │  │  ┌─────────────────>│                  │
 │                  │  │  │  (base64/images)│                  │
 │                  │  │  │                 │                  │
 │                  │  │  │  litellm.       │                  │
 │                  │  │  │  acompletion()  │─────────────────>│
 │                  │  │  │                 │                  │
 │                  │  │  │  ModelResponse  │<─────────────────│
 │                  │  │  └─────────────────│                  │
 │                  │  │                   │                  │
 │                  │  ├─ extract tool call │                  │
 │                  │  ├─ validate(data)    │                  │
 │                  │  └─ return (data, comp)│                 │
 │                  │                       │                  │
 │                  │  JSONResponse         │                  │
 │                  │  {data, errors, …}    │                  │
 │<─────────────────│                       │                  │

===============================================================================
UML CLASS DIAGRAM — Extraction Pipeline
===============================================================================

┌──────────────────────┐       ┌─────────────────────────┐
│   DebugRouter        │       │   LiteLLMProvider       │
│──────────────────────│       │─────────────────────────│
│ + debug_extract()    │──────>│ + run_skill()           │
│ + debug_list()       │       │   - _SKILLS_DIR         │
│                      │       │   - _load_validate()    │
│                      │       └────────────┬────────────┘
│                      │                    │
│                      │       ┌────────────▼────────────┐
│                      │       │   LiteLLMSession        │
│                      │       │─────────────────────────│
│                      │       │ + complete()            │
│                      │       │   - _to_litellm_message │
│                      │       │   - _to_litellm_block   │
│                      │       │   - _handle_pdf_block   │
│                      │       └────────────┬────────────┘
│                      │                    │
│                      │       ┌────────────▼────────────┐
│                      │       │   PDF Processing        │
│                      │       │─────────────────────────│
│                      │       │ _encode_pdf_block()     │
│                      │       │ _pdf_to_images()        │
│                      │       │ _pdf_to_text()          │
│                      │       │ _select_pages()         │
│                      │       └─────────────────────────┘
└──────────────────────┘

===============================================================================
SKILL DESCRIPTIONS
===============================================================================

extract-pintable
  Purpose: Extract pin table, package info, and component subtype from an IC
           datasheet PDF.
  Output:  save_pintable tool → {component_subtype, package_info, pintable}
  Keywords: "pin out", "pin diagram", "pin configuration", "pin description",
            "pin assignment", "pin function", "pin name", "pin table", "pin map",
            "ball map", "package pin", "package drawing", "package outline",
            "signal description"
  Model:   settings.model_pintable or settings.default_model

extract-pattern
  Purpose: Extract passive component MPN pattern (resistor, capacitor, inductor)
           from a datasheet PDF. Builds a regex-based part number decoder.
  Output:  save_pattern tool → {manufacturer, series, component_type, regex,
            fields, value_decoder, example_mpns}
  Keywords: "part numbering", "ordering information", "part number", "MPN",
            "device code", "device code table", "part number decoding",
            "part number format", "model number", "model numbering",
            "device ordering", "ordering code"
  Model:   settings.model_pattern or settings.default_model

extract-specs
  Purpose: Extract pin table, package info, and electrical specifications from
           a discrete/simple component datasheet PDF.
  Output:  save_specs tool → {component_subtype, package_info, pintable, values}
  Keywords: "pin out", "pin diagram", "pin configuration", "pin description",
            "pin assignment", "pin function", "pin name", "pin table", "pin map",
            "ball map", "package pin", "package drawing", "package outline",
            "signal description", "electrical characteristics",
            "absolute maximum ratings", "recommended operating conditions",
            "electrical specifications", "specifications", "parameters"
  Model:   settings.model_specs or settings.default_model

===============================================================================
PDF PAGE SELECTION ALGORITHM
===============================================================================

  Input:  pdf_path, keywords (regex pattern), max_pages (default 90)
  Output: path to trimmed PDF (temp file) or original path if no trim needed

  Step 1: Read PDF with pypdf.PdfReader
  Step 2: If total_pages <= max_pages → return original path
  Step 3: Initialize keep = {0, 1, 2, 3, 4}  (always include first 5 pages)
  Step 4: For each page i:
            text = page.extract_text()
            if keywords.search(text):
                keep.add(i-1, i, i+1)  (matched page + neighbors)
  Step 5: If len(keep) < max_pages:
            pad with remaining pages from front (0, 1, 2, ...)
  Step 6: selected = sorted(keep)[:max_pages]
  Step 7: Write selected pages to temp file using PdfWriter
  Step 8: Return temp file path (caller must cleanup)

===============================================================================
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse

from backend.config import settings
from backend.services.llm import (
    Message,
    PdfBlock,
    TextBlock,
    ToolSchema,
    get_provider,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Router setup — prefix="/debug", tags=["debug"]
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/debug", tags=["debug"])

# ---------------------------------------------------------------------------
# Hello world endpoint (kept for quick health checks)
# ---------------------------------------------------------------------------


@router.get("/hello")
async def hello():
    return {"message": "hello world"}


# ---------------------------------------------------------------------------
# Tool schemas (mirrored from backend/services/extraction.py)
# These are the output tools that each skill is forced to call.
# ---------------------------------------------------------------------------

PINTABLE_TOOL: dict[str, Any] = {
    "name": "save_pintable",
    "description": "Save the extracted pin table, package info, and component subtype.",
    "input_schema": {
        "type": "object",
        "properties": {
            "component_subtype": {
                "type": "string",
                "description": (
                    "Dotted taxonomy path using lowercase segments joined by periods. "
                    "Must start with 'ic.'. Examples: ic.mcu, ic.power.ldo, "
                    "ic.interface.usb_uart_bridge"
                ),
                "pattern": "^[a-z][a-z0-9_]+(\\.[a-z][a-z0-9_]+)*$",
            },
            "component_subtype_description": {
                "type": "string",
                "description": (
                    "Brief human-readable description of the component subtype, "
                    "e.g. 'Low-dropout voltage regulator', 'USB to UART bridge IC'. "
                    "Used when this is a new taxonomy entry."
                ),
            },
            "package_info": {
                "type": "object",
                "properties": {
                    "base_family": {"type": "string"},
                    "package": {"type": "string"},
                    "pin_count": {"type": "integer"},
                    "description": {"type": "string"},
                },
                "required": ["base_family", "package", "pin_count"],
            },
            "pintable": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "number": {},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "functions": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["number", "name"],
                },
            },
        },
        "required": [
            "component_subtype",
            "component_subtype_description",
            "package_info",
            "pintable",
        ],
    },
}

PATTERN_TOOL: dict[str, Any] = {
    "name": "save_pattern",
    "description": "Save the extracted passive component MPN pattern.",
    "input_schema": {
        "type": "object",
        "properties": {
            "manufacturer": {"type": "string"},
            "series": {"type": "string"},
            "component_type": {
                "type": "string",
                "enum": ["resistor", "capacitor", "inductor"],
            },
            "component_subtype": {
                "type": "string",
                "description": (
                    "Dotted taxonomy path using lowercase segments joined by periods. "
                    "Must start with 'passive.'. Examples: passive.resistor, "
                    "passive.capacitor.ceramic, passive.inductor"
                ),
                "pattern": "^[a-z][a-z0-9_]+(\\.[a-z][a-z0-9_]+)*$",
            },
            "component_subtype_description": {
                "type": "string",
                "description": (
                    "Brief human-readable description of the component subtype. "
                    "Used when this is a new taxonomy entry."
                ),
            },
            "description": {"type": "string"},
            "regex": {"type": "string"},
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "position": {"type": "integer"},
                        "length": {"type": "integer"},
                        "description": {"type": "string"},
                        "lookup": {"type": "object"},
                    },
                    "required": ["name", "position", "length", "description"],
                },
            },
            "value_decoder": {"type": "object"},
            "example_mpns": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "manufacturer",
            "series",
            "component_type",
            "component_subtype",
            "component_subtype_description",
            "description",
            "regex",
            "fields",
            "value_decoder",
            "example_mpns",
        ],
    },
}

SPECS_TOOL: dict[str, Any] = {
    "name": "save_specs",
    "description": "Save extracted component specifications and pin table.",
    "input_schema": {
        "type": "object",
        "properties": {
            "component_subtype": {
                "type": "string",
                "description": (
                    "Dotted taxonomy path, e.g. discrete.diode.schottky, "
                    "connector.usb"
                ),
                "pattern": "^[a-z][a-z0-9_]+(\\.[a-z][a-z0-9_]+)*$",
            },
            "component_subtype_description": {
                "type": "string",
                "description": (
                    "Brief description of the component subtype. "
                    "Used when this is a new taxonomy entry."
                ),
            },
            "package_info": {
                "type": "object",
                "properties": {
                    "base_family": {"type": "string"},
                    "package": {"type": "string"},
                    "pin_count": {"type": "integer"},
                    "description": {"type": "string"},
                },
                "required": ["base_family", "package", "pin_count"],
            },
            "pintable": {
                "type": "array",
                "description": "Pin table for the component. Include ALL pins.",
                "items": {
                    "type": "object",
                    "properties": {
                        "number": {},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "functions": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["number", "name"],
                },
            },
            "values": {
                "type": "object",
                "description": (
                    "Extracted parameter values keyed ONLY by parameter names "
                    "from the PARAMETERS TO EXTRACT list. Use SPICE multiplier "
                    "prefixes (k, M, m, u, n, p) with units. Use null for "
                    "missing/inapplicable parameters."
                ),
                "additionalProperties": {"type": ["string", "number", "null"]},
            },
        },
        "required": [
            "component_subtype",
            "component_subtype_description",
            "package_info",
            "pintable",
            "values",
        ],
    },
}

# Map skill names to their output tool schemas
_SKILL_TO_TOOL: dict[str, dict[str, Any]] = {
    "extract-pintable": PINTABLE_TOOL,
    "extract-pattern": PATTERN_TOOL,
    "extract-specs": SPECS_TOOL,
}

# ---------------------------------------------------------------------------
# PDF keywords for page selection (mirrored from extraction.py)
# ---------------------------------------------------------------------------

_MAX_PDF_PAGES = 45

_PINTABLE_KEYWORDS = re.compile(
    r"pin\s*(out|diagram|configuration|description|assignment|function|name|table|map)"
    r"|ball\s*map|package\s*(pin|drawing|outline)|signal\s+description",
    re.IGNORECASE,
)

_PATTERN_KEYWORDS = re.compile(
    r"part\s*(numbering|number|code|naming|format|decoder)"
    r"|ordering\s*(information|info|code|data)"
    r"|device\s*(code|information|naming)"
    r"|model\s*(number|naming|numbering)"
    r"|MPN"
    r"|part\s+number\s+decod"
    r"|part\s+number\s+format",
    re.IGNORECASE,
)

_SPECS_KEYWORDS = re.compile(
    r"pin\s*(out|diagram|configuration|description|assignment|function|name|table|map)"
    r"|ball\s*map|package\s*(pin|drawing|outline)|signal\s+description"
    r"|electrical\s*(characteristics|specs|specifications)"
    r"|absolute\s*maximum\s*ratings"
    r"|recommended\s*operating\s*conditions"
    r"|specifications"
    r"|parameters",
    re.IGNORECASE,
)

_SKILL_TO_KEYWORDS: dict[str, re.Pattern] = {
    "extract-pintable": _PINTABLE_KEYWORDS,
    "extract-pattern": _PATTERN_KEYWORDS,
    "extract-specs": _SPECS_KEYWORDS,
}

# Default PDF directory relative to project root
_DEFAULT_PDF_DIR = Path(__file__).resolve().parent.parent.parent / "simple_project" / "ds"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_pdf(pdf_path: str | None) -> str:
    """Resolve a PDF file path for extraction.

    Resolution order:
    1. If ``pdf_path`` is an absolute path → use as-is (must exist).
    2. If ``pdf_path`` is a relative path → join with ``_DEFAULT_PDF_DIR``.
    3. If ``pdf_path`` is None → list ``_DEFAULT_PDF_DIR/*.pdf``, return first.

    Raises:
        FileNotFoundError: If the resolved path does not exist or no PDFs
            are found in the default directory.
    """
    if pdf_path:
        p = Path(pdf_path)
        if not p.is_absolute():
            p = _DEFAULT_PDF_DIR / p
        else:
            p = Path(pdf_path)
        if not p.is_file():
            raise FileNotFoundError(
                f"PDF not found: {p}. "
                f"Use an absolute path or a filename in {_DEFAULT_PDF_DIR}."
            )
        return str(p)

    # No path provided — list PDFs in the default directory
    if not _DEFAULT_PDF_DIR.is_dir():
        raise FileNotFoundError(
            f"Default PDF directory not found: {_DEFAULT_PDF_DIR}. "
            f"Place a PDF there or provide an explicit path."
        )

    pdfs = sorted(_DEFAULT_PDF_DIR.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(
            f"No PDF files found in {_DEFAULT_PDF_DIR}. "
            f"Available files: {sorted(_DEFAULT_PDF_DIR.iterdir())}"
        )

    logger.info(
        "No PDF path provided — using first PDF in default dir: %s (%d files available)",
        pdfs[0].name,
        len(pdfs),
    )
    return str(pdfs[0])


def _list_pdfs() -> list[dict[str, Any]]:
    """List all PDFs in the default directory with metadata."""
    if not _DEFAULT_PDF_DIR.is_dir():
        return []

    pdfs = []
    for p in sorted(_DEFAULT_PDF_DIR.glob("*.pdf")):
        size_kb = p.stat().st_size / 1024
        pdfs.append({
            "filename": p.name,
            "path": str(p),
            "size_kb": round(size_kb, 1),
        })
    return pdfs


def _select_pages(
    pdf_path: str, keywords: re.Pattern, max_pages: int = _MAX_PDF_PAGES,
) -> tuple[str, list[int]]:
    """Select relevant pages from a PDF for extraction.

    Strategy:
    1. Always include pages 0-4 (title/TOC/overview).
    2. Scan all pages for keyword matches and include those + neighbors (±1).
    3. If still under budget, pad with remaining pages from the front.

    Returns:
        (trimmed_pdf_path, selected_page_numbers) — the trimmed PDF may be
        the original path if no trimming was needed.
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(pdf_path)
    total = len(reader.pages)

    if total <= max_pages:
        logger.info(
            "PDF %s has %d pages (limit %d) — no trimming needed",
            pdf_path, total, max_pages,
        )
        return pdf_path, list(range(total))

    logger.info(
        "PDF %s has %d pages (limit %d) — selecting relevant pages",
        pdf_path, total, max_pages,
    )

    # Always keep the first 5 pages (title, TOC, overview)
    keep: set[int] = set(range(min(5, total)))

    # Scan pages for keyword hits and include neighbors (±1)
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if keywords.search(text):
            for neighbor in (i - 1, i, i + 1):
                if 0 <= neighbor < total:
                    keep.add(neighbor)

    # If still under budget, pad from the front
    if len(keep) < max_pages:
        for i in range(total):
            if len(keep) >= max_pages:
                break
            keep.add(i)

    selected = sorted(keep)[:max_pages]
    logger.info("Selected %d/%d pages for %s: %s", len(selected), total, pdf_path, selected)

    writer = PdfWriter()
    for i in selected:
        writer.add_page(reader.pages[i])

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    writer.write(tmp)
    tmp.close()
    return tmp.name, selected


def _build_system_prompt(skill_name: str, pdf_path: str) -> str:
    """Build the dynamic system prompt for a skill.

    Loads the SKILL.md, strips YAML frontmatter, and appends dynamic context
    (taxonomy subtypes, BOM MPNs, etc.) that varies per extraction.

    Returns:
        The combined system prompt string.
    """
    from backend.pinscopex.taxonomy import format_for_prompt, get_specs_schema

    skill_dir = Path(__file__).resolve().parent.parent.parent / "skills" / skill_name
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

    # Build dynamic context based on skill type
    dynamic_parts: list[str] = []

    if skill_name == "extract-pintable":
        # IC taxonomy subtypes for subtype assignment
        dynamic_parts.append("EXISTING IC TAXONOMY SUBTYPES:")
        dynamic_parts.append(format_for_prompt("ic"))

    elif skill_name == "extract-pattern":
        # Passive taxonomy subtypes + BOM MPNs for pattern matching
        dynamic_parts.append("EXISTING PASSIVE TAXONOMY SUBTYPES:")
        dynamic_parts.append(format_for_prompt("passive"))

        # Try to load BOM MPNs from simple_project
        bom_path = Path(__file__).resolve().parent.parent.parent / "simple_project" / "TI-MSP-KICAD9-TUTORIAL.csv"
        if bom_path.is_file():
            try:
                import csv
                with open(bom_path, newline="") as f:
                    reader = csv.DictReader(f)
                    mpns = [row.get("MPN", "").strip() for row in reader if row.get("MPN")]
                if mpns:
                    dynamic_parts.append("BOM MPNs (for pattern matching):")
                    for mpn in mpns:
                        dynamic_parts.append(f"  - {mpn}")
            except Exception:
                pass  # BOM parsing is best-effort

    elif skill_name == "extract-specs":
        # Type-level specs schema for parameter extraction
        dynamic_parts.append("PARAMETERS TO EXTRACT:")
        specs = get_specs_schema()
        if specs:
            for spec in specs:
                name = spec.get("name", "")
                unit = spec.get("unit", "")
                desc = spec.get("description", "")
                dynamic_parts.append(f"  - {name} ({unit}): {desc}")
        else:
            dynamic_parts.append("  (No type-level specs schema found — extract all visible parameters)")

    dynamic_system = "\n".join(dynamic_parts)
    return f"{skill_prompt}\n\n{dynamic_system}"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/list")
async def debug_list() -> JSONResponse:
    """List available skills and PDFs in the default directory.

    This endpoint helps you explore what's available for extraction without
    actually calling the LLM.

    Returns:
        - skills: List of available skill names with descriptions
        - pdfs: List of PDFs in the default directory with metadata
        - is_debug: Whether the app is running in debug mode
        - app_version: The current app version (DEBUG_yyyymmdd_hhmmss if not set)
    """
    available_skills = list(_SKILL_TO_TOOL.keys())
    pdfs = _list_pdfs()

    return JSONResponse(content={
        "is_debug": settings.is_debug,
        "app_version": settings.app_version,
        "skills": {
            name: {
                "name": name,
                "output_tool": tool["name"],
                "description": tool["description"],
            }
            for name, tool in _SKILL_TO_TOOL.items()
        },
        "pdfs": pdfs,
        "default_pdf_dir": str(_DEFAULT_PDF_DIR),
    })


@router.post("/extract")
async def debug_extract(
    skill: str = Form(
        default="extract-pintable",
        description=(
            "Which skill to run. One of: extract-pintable, extract-pattern, "
            "extract-specs"
        ),
    ),
    pdf_path: str | None = Form(
        default=None,
        description=(
            "Path to the PDF file. Can be a filename (resolved against "
            "simple_project/ds/) or an absolute path. If omitted, uses the "
            "first PDF in simple_project/ds/."
        ),
    ),
    component_type: str | None = Form(
        default=None,
        description=(
            "Component type for extract-specs (e.g. 'diode', 'capacitor', "
            "'resistor', 'inductor', 'connector', 'crystal'). Required when "
            "skill=extract-specs."
        ),
    ),
    upload_file: UploadFile | None = File(
        default=None,
        description=(
            "Upload a PDF file instead of using one from disk. Takes "
            "precedence over pdf_path if both are provided."
        ),
    ),
) -> JSONResponse:
    """Run a skill-based extraction on a PDF file.

    This is the main debug endpoint. It runs the same extraction pipeline that
    the normal API uses, but returns all intermediate results and metadata
    so you can debug and understand what's happening.

    ## Parameters

    You can provide the PDF in three ways (checked in order):
    1. **upload_file** — Upload a PDF file directly (multipart/form-data)
    2. **pdf_path** — A filename (resolved against ``simple_project/ds/``)
       or an absolute path to a PDF
    3. **None** — Uses the first PDF found in ``simple_project/ds/``

    ## Skill selection

    - **extract-pintable** — IC datasheets. Extracts pin table, package info,
      and assigns a taxonomy subtype (e.g. ``ic.mcu``).
    - **extract-pattern** — Passive component datasheets (resistors, capacitors,
      inductors). Extracts the MPN pattern/decoder as a regex.
    - **extract-specs** — Discrete/simple component datasheets (diodes,
      transistors, connectors, crystals). Extracts pin table, package info,
      and electrical specifications.

    ## Example calls

    Using a file from the default directory::

        curl -X POST http://localhost:8000/debug/extract \
            -F "skill=extract-pintable" \
            -F "pdf_path=MSPM0G3507SPTR.pdf"

    Uploading a file directly::

        curl -X POST http://localhost:8000/debug/extract \
            -F "skill=extract-pintable" \
            -F "upload_file=@/path/to/datasheet.pdf"

    Using the default PDF (no pdf_path specified)::

        curl -X POST http://localhost:8000/debug/extract \
            -F "skill=extract-pintable"

    Extract specs for a diode::

        curl -X POST http://localhost:8000/debug/extract \
            -F "skill=extract-specs" \
            -F "pdf_path=SPX3819M5-L-3-3.pdf" \
            -F "component_type=regulator"

    ## Response fields

    - **skill** — The skill that was run
    - **pdf_path** — The resolved PDF path used
    - **pages_used** — List of page numbers included in the trimmed PDF
    - **model** — The model used for extraction
    - **data** — The extracted structured data (what the LLM returned)
    - **validation_errors** — Any validation errors from validate.py (empty = valid)
    - **usage** — Token usage statistics (input, output, cache tokens)
    - **stop_reason** — Why the model stopped (typically "tool_calls")
    - **error** — If the extraction failed, an error message is included

    ## Error handling

    - 400: Invalid skill name, missing component_type for extract-specs,
      or file upload errors
    - 404: PDF not found
    - 500: LLM call failed, validation errors, or unexpected errors
    """
    # Validate skill name
    if skill not in _SKILL_TO_TOOL:
        return JSONResponse(
            status_code=400,
            content={
                "error": f"Unknown skill: {skill!r}. "
                         f"Available skills: {list(_SKILL_TO_TOOL.keys())}",
            },
        )

    # Validate component_type for extract-specs
    if skill == "extract-specs" and not component_type:
        return JSONResponse(
            status_code=400,
            content={
                "error": (
                    "component_type is required for extract-specs. "
                    "Examples: diode, capacitor, resistor, inductor, "
                    "connector, crystal, regulator"
                ),
            },
        )

    # Handle file upload
    resolved_pdf: str | None = None
    if upload_file is not None:
        # Save uploaded file to a temp location
        tmp_upload = tempfile.NamedTemporaryFile(
            suffix=".pdf", delete=False, prefix="debug_upload_",
        )
        content = await upload_file.read()
        tmp_upload.write(content)
        tmp_upload.close()
        resolved_pdf = tmp_upload.name
        logger.info(
            "Uploaded PDF: %s (%d bytes) → %s",
            upload_file.filename, len(content), resolved_pdf,
        )
    elif pdf_path:
        try:
            resolved_pdf = _resolve_pdf(pdf_path)
        except FileNotFoundError as exc:
            return JSONResponse(status_code=404, content={"error": str(exc)})
    else:
        try:
            resolved_pdf = _resolve_pdf(None)
        except FileNotFoundError as exc:
            return JSONResponse(status_code=404, content={"error": str(exc)})

    # Get keywords for page selection
    keywords = _SKILL_TO_KEYWORDS[skill]

    # Select relevant pages
    trimmed_pdf, pages_used = _select_pages(resolved_pdf, keywords)

    # Build system prompt
    try:
        system_prompt = _build_system_prompt(skill, trimmed_pdf)
    except FileNotFoundError as exc:
        return JSONResponse(
            status_code=500, content={"error": str(exc)},
        )

    # Get provider and model
    provider = get_provider(skill)
    model = settings.model_for_stage(skill)

    # Run the skill
    output_tool = ToolSchema(
        name=_SKILL_TO_TOOL[skill]["name"],
        description=_SKILL_TO_TOOL[skill]["description"],
        input_schema=_SKILL_TO_TOOL[skill]["input_schema"],
    )

    try:
        data, completion = await provider.run_skill(
            skill_name=skill,
            model=model,
            system=system_prompt,
            user_text="Extract the data from this PDF using the save tool.",
            pdf_path=trimmed_pdf,
            output_tool=output_tool,
        )
    except FileNotFoundError as exc:
        return JSONResponse(
            status_code=500, content={"error": str(exc)},
        )
    except RuntimeError as exc:
        return JSONResponse(
            status_code=500,
            content={
                "error": str(exc),
                "hint": (
                    "The model did not call the expected output tool. "
                    "This can happen if the model refuses to use tools, "
                    "or if the PDF content is unclear."
                ),
            },
        )
    except Exception as exc:
        logger.exception("Extraction failed for skill=%s", skill)
        return JSONResponse(
            status_code=500,
            content={"error": f"Extraction failed: {exc}"},
        )
    finally:
        # Clean up temp files
        if trimmed_pdf != resolved_pdf:
            try:
                Path(trimmed_pdf).unlink(missing_ok=True)
            except Exception:
                pass
        if upload_file is not None:
            try:
                Path(resolved_pdf).unlink(missing_ok=True)
            except Exception:
                pass

    # Build response
    return JSONResponse(content={
        "skill": skill,
        "pdf_path": resolved_pdf,
        "trimmed_pdf": trimmed_pdf if trimmed_pdf != resolved_pdf else None,
        "pages_used": pages_used,
        "model": model,
        "data": data,
        "validation_errors": [],  # run_skill logs warnings but doesn't raise
        "usage": {
            "input_tokens": completion.usage.input_tokens,
            "output_tokens": completion.usage.output_tokens,
            "cache_creation_tokens": completion.usage.cache_creation_tokens,
            "cache_read_tokens": completion.usage.cache_read_tokens,
        },
        "stop_reason": completion.stop_reason,
    })
