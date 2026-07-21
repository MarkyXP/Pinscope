"""Async datasheet extraction using Claude API.

Ports the extraction steps from run_pipeline.py to async:
  - extract_pintable: Pin table + package info + taxonomy assignment
  - extract_pattern: Passive MPN pattern
  - extract_specs: Component specs (discrete, connectors, crystals, etc.)
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import time
from pathlib import Path

from backend.pinscopex.utils import safe_mpn
from backend.pinscopex.models import (
    CapacitorSpecs,
    ComponentConstraints,
    ComponentModel,
    ComponentType,
    DesignGraph,
    NetType,
    SimpleComponentSpecs,
)
from backend.pinscopex.taxonomy import (
    TAXONOMY_DIR,
    add_subtype,
    format_for_prompt,
    format_specs_for_prompt,
    get_specs_schema,
    get_subtype,
    has_specs,
    set_extra_specs,
    set_type_specs,
)

from backend.config import settings
from backend.services.api_logs import ApiLogger, CallMeta
from backend.services.llm import (
    Message,
    PdfBlock,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    call_with_fallback,
    get_provider,
)

# ---------------------------------------------------------------------------
# Tool schemas (from run_pipeline.py)
# ---------------------------------------------------------------------------

PINTABLE_TOOL = {
    "name": "save_pintable",
    "description": "Save the extracted pin table, package info, and component subtype.",
    "input_schema": {
        "type": "object",
        "properties": {
            "component_subtype": {
                "type": "string",
                "description": "Dotted taxonomy path using lowercase segments joined by periods. Must start with 'ic.'. Examples: ic.mcu, ic.power.ldo, ic.interface.usb_uart_bridge",
                "pattern": "^[a-z][a-z0-9_]+(\\.[a-z][a-z0-9_]+)*$",
            },
            "component_subtype_description": {
                "type": "string",
                "description": "Brief human-readable description of the component subtype, e.g. 'Low-dropout voltage regulator', 'USB to UART bridge IC'. Used when this is a new taxonomy entry.",
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
        "required": ["component_subtype", "component_subtype_description", "package_info", "pintable"],
    },
}

PATTERN_TOOL = {
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
                "description": "Dotted taxonomy path using lowercase segments joined by periods. Must start with 'passive.'. Examples: passive.resistor, passive.capacitor.ceramic, passive.inductor",
                "pattern": "^[a-z][a-z0-9_]+(\\.[a-z][a-z0-9_]+)*$",
            },
            "component_subtype_description": {
                "type": "string",
                "description": "Brief human-readable description of the component subtype, e.g. 'Multi-layer ceramic capacitor (MLCC)', 'Chip resistor'. Used when this is a new taxonomy entry.",
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
            "manufacturer", "series", "component_type", "component_subtype",
            "component_subtype_description", "description", "regex", "fields",
            "value_decoder", "example_mpns",
        ],
    },
}

SPECS_TOOL = {
    "name": "save_specs",
    "description": "Save extracted component specifications and pin table.",
    "input_schema": {
        "type": "object",
        "properties": {
            "component_subtype": {
                "type": "string",
                "description": "Dotted taxonomy path, e.g. discrete.diode.schottky, connector.usb",
                "pattern": "^[a-z][a-z0-9_]+(\\.[a-z][a-z0-9_]+)*$",
            },
            "component_subtype_description": {
                "type": "string",
                "description": "Brief description of the component subtype. Used when this is a new taxonomy entry.",
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
                "description": "Extracted parameter values keyed ONLY by parameter names from the PARAMETERS TO EXTRACT list. Use SPICE multiplier prefixes (k, M, m, u, n, p) with units. Use null for missing/inapplicable parameters. Do NOT add parameters not in the list.",
                "additionalProperties": {"type": ["string", "number", "null"]},
            },
        },
        "required": ["component_subtype", "component_subtype_description", "package_info", "pintable", "values"],
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MAX_PDF_PAGES = 45

log = logging.getLogger(__name__)

# Keywords used to find relevant pages for each extraction stage.
_PINTABLE_KEYWORDS = re.compile(
    r"pin\s*(out|diagram|configuration|description|assignment|function|name|table|map)"
    r"|ball\s*map|package\s*(pin|drawing|outline)|signal\s+description",
    re.IGNORECASE,
)


def _select_pages(
    pdf_path: str, keywords: re.Pattern, max_pages: int = _MAX_PDF_PAGES,
) -> str:
    """Return path to a trimmed PDF containing only relevant pages.

    Strategy:
    1. Always include pages 0-4 (title/TOC/overview).
    2. Scan all pages for keyword matches and include those + neighbors.
    3. If still under budget, pad with remaining pages from the front.
    Returns the original path if the PDF is already within limits.
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(pdf_path)
    total = len(reader.pages)
    if total <= max_pages:
        return pdf_path

    log.info("PDF %s has %d pages (limit %d) — selecting relevant pages", pdf_path, total, max_pages)

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
    log.info("Selected %d/%d pages for %s", len(selected), total, pdf_path)

    writer = PdfWriter()
    for i in selected:
        writer.add_page(reader.pages[i])

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    writer.write(tmp)
    tmp.close()
    return tmp.name


def _to_tool(d: dict) -> ToolSchema:
    """Convert a tool-definition dict to our unified ToolSchema."""
    return ToolSchema(
        name=d["name"],
        description=d["description"],
        input_schema=d["input_schema"],
    )


_GENERATE_SPECS_TOOL = {
    "name": "save_specs_schema",
    "description": "Save the standardized parameter schema for a component type.",
    "input_schema": {
        "type": "object",
        "properties": {
            "specs": {
                "type": "array",
                "description": "Electrical parameters useful for schematic/design validation.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "snake_case name with unit suffix: e.g. voltage_rating_v, "
                                "current_rating_a, resistance_ohm, frequency_hz, capacitance_f, "
                                "power_w, inductance_h. Use _mm for length."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": "Brief description of the parameter and its common datasheet symbol.",
                        },
                        "unit": {
                            "type": "string",
                            "description": "SI unit: V, A, ohm, F, Hz, W, s, H, dB, mm, ppm. Omit for dimensionless.",
                        },
                        "required": {
                            "type": "boolean",
                            "description": "True if this parameter is essential for validation.",
                        },
                    },
                    "required": ["name", "description"],
                },
            },
        },
        "required": ["specs"],
    },
}


async def _generate_type_specs(
    component_type: str,
    taxonomy_dir: Path,
    api_logger: ApiLogger | None = None,
) -> list[dict]:
    """Generate type-level specs schema for a component type with no specs defined."""
    system = (
        "You are a hardware design expert defining standardized extraction parameters "
        "for electronic components. Given a component type, define 3-6 electrical "
        "parameters that are:\n"
        "1. Common across ALL subtypes of this component\n"
        "2. Useful for schematic/PCB design VALIDATION (checking connections, ratings, compatibility)\n"
        "3. Extractable from a typical datasheet\n\n"
        "Do NOT include mechanical, material, or cosmetic parameters.\n"
        "Do NOT include parameters only relevant to specific subtypes.\n\n"
        "Use snake_case names with unit suffix matching SI units:\n"
        "- Voltage: _v (unit: V)\n"
        "- Current: _a (unit: A)\n"
        "- Resistance: _ohm (unit: ohm)\n"
        "- Capacitance: _f (unit: F)\n"
        "- Frequency: _hz (unit: Hz)\n"
        "- Power: _w (unit: W)\n"
        "- Inductance: _h (unit: H)\n"
        "- Time: _s (unit: s)\n"
        "- Length: _mm (unit: mm)\n\n"
        "Values will use SPICE multiplier prefixes: k=1e3, M=1e6, m=1e-3, u=1e-6, n=1e-9, p=1e-12.\n\n"
        "Mark the single most important parameter as required.\n"
        "Call save_specs_schema with the parameter list."
    )

    async def _call(provider, model):
        session = await provider.create_session(model=model, system=system, max_tokens=10_000)
        t0 = time.monotonic()
        try:
            completion = await session.complete(
                messages=[Message("user", [TextBlock(
                    f"Define standardized extraction parameters for component type: {component_type}",
                )])],
                tools=[_to_tool(_GENERATE_SPECS_TOOL)],
                tool_choice={"name": "save_specs_schema"},
            )
        finally:
            await session.close()
        return completion, time.monotonic() - t0, provider.name, model

    completion, elapsed, provider_name, model = await call_with_fallback("specs", _call)

    if api_logger:
        api_logger.log(
            stage="generate_type_specs", identifier=component_type,
            model=model, provider=provider_name,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            cache_creation_input_tokens=completion.usage.cache_creation_tokens,
            cache_read_input_tokens=completion.usage.cache_read_tokens,
            duration_ms=int(elapsed * 1000),
            stop_reason=completion.stop_reason,
            turns=1,
        )

    for tc in completion.tool_calls:
        if tc.name == "save_specs_schema":
            specs = tc.input["specs"]
            set_type_specs(component_type, specs, taxonomy_dir)
            return specs
    return []


async def _generate_extra_specs(
    subtype_key: str,
    subtype_description: str,
    component_type: str,
    taxonomy_dir: Path,
    api_logger: ApiLogger | None = None,
) -> list[dict]:
    """Generate extra_specs for a new subtype."""
    type_specs = get_specs_schema(component_type, directory=taxonomy_dir)
    existing_names = [s["name"] for s in type_specs]

    system = (
        "You are a hardware design expert defining subtype-specific extraction parameters "
        "for electronic components. Given a component subtype, define 2-5 additional "
        "electrical parameters that are:\n"
        "1. SPECIFIC to this subtype (not common across all subtypes of the parent type)\n"
        "2. Useful for schematic/PCB design VALIDATION (checking connections, ratings, compatibility)\n"
        "3. Extractable from a typical datasheet\n\n"
        "Do NOT include mechanical, material, or cosmetic parameters.\n"
        "Do NOT duplicate these existing type-level parameters: "
        f"{', '.join(existing_names)}\n\n"
        "Use snake_case names with unit suffix matching SI units:\n"
        "- Voltage: _v (V), Current: _a (A), Resistance: _ohm (ohm)\n"
        "- Capacitance: _f (F), Frequency: _hz (Hz), Power: _w (W)\n"
        "- Inductance: _h (H), Time: _s (s), Length: _mm (mm)\n\n"
        "Values will use SPICE multiplier prefixes: k=1e3, M=1e6, m=1e-3, u=1e-6, n=1e-9, p=1e-12.\n\n"
        "If this subtype needs NO additional parameters beyond the type-level ones, "
        "return an empty specs array.\n"
        "Call save_specs_schema."
    )

    async def _call(provider, model):
        session = await provider.create_session(model=model, system=system, max_tokens=10_000)
        t0 = time.monotonic()
        try:
            completion = await session.complete(
                messages=[Message("user", [TextBlock(
                    f"Component subtype: {subtype_key} — {subtype_description}\n"
                    f"Parent type: {component_type}\n"
                    f"Existing type-level parameters: {', '.join(existing_names)}",
                )])],
                tools=[_to_tool(_GENERATE_SPECS_TOOL)],
                tool_choice={"name": "save_specs_schema"},
            )
        finally:
            await session.close()
        return completion, time.monotonic() - t0, provider.name, model

    completion, elapsed, provider_name, model = await call_with_fallback("specs", _call)

    if api_logger:
        api_logger.log(
            stage="generate_extra_specs", identifier=subtype_key,
            model=model, provider=provider_name,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            cache_creation_input_tokens=completion.usage.cache_creation_tokens,
            cache_read_input_tokens=completion.usage.cache_read_tokens,
            duration_ms=int(elapsed * 1000),
            stop_reason=completion.stop_reason,
            turns=1,
        )

    for tc in completion.tool_calls:
        if tc.name == "save_specs_schema":
            extra = tc.input["specs"]
            if extra:
                set_extra_specs(subtype_key, extra, taxonomy_dir)
            return extra
    return []


# ---------------------------------------------------------------------------
# Extraction steps
# ---------------------------------------------------------------------------


async def extract_pintable(
    mpn: str,
    pdf_path: str,
    output_dir: Path,
    taxonomy_dir: Path | None = None,
    api_logger: ApiLogger | None = None,
) -> Path:
    """Extract pin table from datasheet PDF. Returns path to constraints JSON."""
    tax_dir = taxonomy_dir or settings.taxonomy_dir
    taxonomy = format_for_prompt("ic", tax_dir)

    trimmed = _select_pages(pdf_path, _PINTABLE_KEYWORDS)
    skill_id, version = settings.get_skill("extract-pintable")
    system = (
        f"DYNAMIC CONTEXT FOR THIS EXTRACTION:\n"
        f"MPN: {mpn}\n\n"
        f"EXISTING IC TAXONOMY SUBTYPES:\n{taxonomy}\n\n"
        f"After reading the skill and extracting data, call save_pintable."
    )
    provider = get_provider("pintable")
    model = settings.model_for_stage("pintable")
    try:
        result, completion = await provider.run_skill(
            skill_name="extract-pintable",
            model=model,
            system=system,
            user_text=f"Extract pin table and package info for MPN: {mpn}",
            pdf_path=trimmed,
            output_tool=_to_tool(PINTABLE_TOOL),
        )
    finally:
        if trimmed != pdf_path:
            Path(trimmed).unlink(missing_ok=True)

    if api_logger:
        api_logger.log(
            stage="pintable", identifier=mpn, model=model,
            provider=provider.name, skill_id=skill_id,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            cache_creation_input_tokens=completion.usage.cache_creation_tokens,
            cache_read_input_tokens=completion.usage.cache_read_tokens,
            duration_ms=getattr(completion, "duration_ms", 0),
            stop_reason=completion.stop_reason,
            turns=getattr(completion, "turns", 1),
        )

    # Check that the datasheet actually matches the requested MPN
    base_family = result.get("package_info", {}).get("base_family", "")
    if base_family:
        mpn_norm = mpn.upper().replace("-", "").replace("_", "")
        bf_norm = base_family.upper().replace("-", "").replace("_", "")
        if bf_norm not in mpn_norm and mpn_norm not in bf_norm:
            raise ValueError(
                f"Datasheet mismatch for {mpn}: extracted base_family "
                f"'{base_family}' does not match the requested MPN. "
                f"The uploaded PDF may be the wrong datasheet."
            )

    # Empty pintable means extraction effectively failed — refuse to
    # persist it anywhere (including the shared library). Raising here
    # lets the pipeline's per-IC error handler mark this MPN as skipped.
    if not result.get("pintable"):
        raise ValueError(
            f"Empty pintable extracted for {mpn} — the uploaded PDF may "
            f"not be a valid datasheet for this component."
        )

    # Ensure taxonomy entry exists
    subtype = result["component_subtype"]
    subtype_desc = result.get("component_subtype_description", "")
    if not get_subtype(subtype, tax_dir):
        add_subtype(subtype, subtype_desc or f"(auto-added for {mpn})",
                    example_mpn=mpn, directory=tax_dir)

    constraints = ComponentConstraints(
        mpn=mpn,
        model_version=settings.get_default_model_version(),
        component_subtype=subtype,
        package_info=result["package_info"],
        pintable=result["pintable"],
        absolute_maximum_ratings=[],
        rules=[],
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    safe = safe_mpn(mpn)
    out_path = output_dir / f"{safe}.json"
    out_path.write_text(constraints.model_dump_json(indent=2) + "\n")
    return out_path


async def extract_pattern(
    pdf_path: str,
    mpns: list[str],
    output_dir: Path,
    trigger_mpn: str | None = None,
    taxonomy_dir: Path | None = None,
    api_logger: ApiLogger | None = None,
) -> Path | None:
    """Extract passive MPN pattern from datasheet. Returns path to pattern JSON.

    If *trigger_mpn* is provided and the extracted regex does not match it,
    returns ``None`` so the MPN falls through to specs extraction instead of
    being silently missed.
    """
    tax_dir = taxonomy_dir or settings.taxonomy_dir
    taxonomy = format_for_prompt("passive", tax_dir)

    skill_id, version = settings.get_skill("extract-pattern")
    system = (
        f"DYNAMIC CONTEXT FOR THIS EXTRACTION:\n\n"
        f"EXISTING PASSIVE TAXONOMY SUBTYPES:\n{taxonomy}\n\n"
        f"BOM MPNs that should match this pattern: {mpns}\n\n"
        f"After reading the skill and extracting data, call save_pattern."
    )
    provider = get_provider("pattern")
    model = settings.model_for_stage("pattern")
    result, completion = await provider.run_skill(
        skill_name="extract-pattern",
        model=model,
        system=system,
        user_text="Extract the part numbering pattern from this datasheet.",
        pdf_path=pdf_path,
        output_tool=_to_tool(PATTERN_TOOL),
    )

    if api_logger:
        api_logger.log(
            stage="pattern", identifier=Path(pdf_path).stem,
            model=model, provider=provider.name, skill_id=skill_id,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            cache_creation_input_tokens=completion.usage.cache_creation_tokens,
            cache_read_input_tokens=completion.usage.cache_read_tokens,
            duration_ms=getattr(completion, "duration_ms", 0),
            stop_reason=completion.stop_reason,
            turns=getattr(completion, "turns", 1),
        )

    # Validate: the pattern must at least match the MPN whose datasheet
    # was used, otherwise the extraction is useless for that MPN.
    regex = result.get("regex", "")
    if trigger_mpn and regex:
        try:
            if not re.match(regex, trigger_mpn):
                log.warning(
                    "Pattern regex from %s does not match trigger MPN %s — discarding",
                    Path(pdf_path).name, trigger_mpn,
                )
                return None
        except re.error:
            log.warning("Invalid regex from %s: %s", Path(pdf_path).name, regex)
            return None

    # Ensure taxonomy entry
    subtype = result.get("component_subtype", "")
    subtype_desc = result.get("component_subtype_description", "")
    if subtype and not get_subtype(subtype, tax_dir):
        add_subtype(subtype, subtype_desc or f"(auto-added for {result['manufacturer']} {result['series']})",
                    directory=tax_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{safe_mpn(result['manufacturer'])}_{safe_mpn(result['series'])}_{result['component_type']}.json"
    out_path = output_dir / filename

    out_path.write_text(json.dumps(result, indent=2) + "\n")
    return out_path


async def extract_specs(
    mpn: str,
    pdf_path: str,
    component_type: str,
    output_dir: Path,
    taxonomy_dir: Path | None = None,
    api_logger: ApiLogger | None = None,
) -> Path:
    """Extract specs from datasheet for a simple/discrete component.

    Returns path to the ComponentModel JSON in *output_dir*.
    """
    tax_dir = taxonomy_dir or settings.taxonomy_dir

    # Auto-generate type-level specs if none exist for this component type
    if not has_specs(component_type, tax_dir):
        try:
            await _generate_type_specs(component_type, tax_dir, api_logger)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Failed to auto-generate specs schema for %s", component_type,
                exc_info=True,
            )

    subtypes_text = format_for_prompt(component_type, tax_dir)
    specs_text = format_specs_for_prompt(component_type, tax_dir)

    skill_id, version = settings.get_skill("extract-specs")
    system = (
        f"DYNAMIC CONTEXT FOR THIS EXTRACTION:\n"
        f"MPN: {mpn}\n"
        f"Component type: {component_type}\n\n"
        f"EXISTING {component_type.upper()} TAXONOMY SUBTYPES:\n{subtypes_text}\n\n"
        f"{specs_text}\n\n"
        f"After reading the skill and extracting data, call save_specs."
    )
    provider = get_provider("specs")
    model = settings.model_for_stage("specs")
    result, completion = await provider.run_skill(
        skill_name="extract-specs",
        model=model,
        system=system,
        user_text=f"Extract specifications for MPN: {mpn}",
        pdf_path=pdf_path,
        output_tool=_to_tool(SPECS_TOOL),
    )

    if api_logger:
        api_logger.log(
            stage="specs", identifier=mpn, model=model,
            provider=provider.name, skill_id=skill_id,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            cache_creation_input_tokens=completion.usage.cache_creation_tokens,
            cache_read_input_tokens=completion.usage.cache_read_tokens,
            duration_ms=getattr(completion, "duration_ms", 0),
            stop_reason=completion.stop_reason,
            turns=getattr(completion, "turns", 1),
        )

    # Ensure taxonomy entry; auto-generate extra_specs for new subtypes
    subtype = result["component_subtype"]
    subtype_desc = result.get("component_subtype_description", "")
    if not get_subtype(subtype, tax_dir):
        add_subtype(
            subtype, subtype_desc or f"(auto-added for {mpn})",
            example_mpn=mpn, directory=tax_dir,
        )
        try:
            await _generate_extra_specs(
                subtype, subtype_desc or subtype,
                component_type, tax_dir, api_logger,
            )
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Failed to auto-generate extra_specs for %s", subtype,
                exc_info=True,
            )

    # Filter values to taxonomy-defined parameter names only
    allowed_keys = {s["name"] for s in get_specs_schema(component_type, subtype, tax_dir)}
    filtered_values = {k: v for k, v in result["values"].items() if k in allowed_keys}

    # Build and persist ComponentModel
    specs = SimpleComponentSpecs(
        specs_type=component_type,
        component_subtype=subtype,
        values=filtered_values,
        pintable=result.get("pintable", []),
        package_info=result.get("package_info"),
    )
    model_obj = ComponentModel(mpn=mpn, specs=specs)

    output_dir.mkdir(parents=True, exist_ok=True)
    safe = safe_mpn(mpn)
    out_path = output_dir / f"{safe}.json"
    out_path.write_text(model_obj.model_dump_json(indent=2) + "\n")
    return out_path


# ---------------------------------------------------------------------------
# Auto-resolve specs from DigiKey parameters (no PDF needed)
# ---------------------------------------------------------------------------

AUTO_RESOLVE_TOOL = {
    "name": "save_resolved_specs",
    "description": "Save the resolved component specifications mapped from distributor parameters.",
    "input_schema": {
        "type": "object",
        "properties": {
            "component_subtype": {
                "type": "string",
                "description": "Dotted taxonomy path, e.g. discrete.diode.schottky, connector.usb",
            },
            "component_subtype_description": {
                "type": "string",
                "description": "Brief description of the component subtype.",
            },
            "values": {
                "type": "object",
                "description": "Parameter values keyed by taxonomy spec names. Use SPICE multiplier prefixes with units.",
                "additionalProperties": {"type": ["string", "number", "null"]},
            },
            "package": {
                "type": ["string", "null"],
                "description": "Package type, e.g. SOD-123, SOT-23, TO-220",
            },
        },
        "required": ["component_subtype", "values"],
    },
}

_AUTO_RESOLVE_SYSTEM = """\
You are a hardware component classifier and parameter mapper.

Given distributor product parameters for an electronic component, you must:
1. Classify the component into the correct taxonomy subtype
2. Map the parameter values to the standardized taxonomy parameters

COMPONENT TYPE: {component_type}

EXISTING SUBTYPES:
{subtypes_text}

{specs_text}

RULES:
- Map distributor parameter values to the taxonomy parameter names listed above.
- Use SPICE multiplier prefixes (T=1e12, G=1e9, M=1e6, k=1e3, m=1e-3, u=1e-6, n=1e-9, p=1e-12) with units.
  Examples: 30V, 500mA, 47mohm, 18pF, 8MHz, 10nC, 250mW.
- Always include the unit with the multiplier in the value string.
- If a distributor parameter doesn't map to any taxonomy parameter, skip it.
- If a taxonomy parameter isn't available from the distributor data, use null.
- Pick the most specific matching subtype from the list above.

Call save_resolved_specs with the mapped values.\
"""


async def auto_resolve_specs(
    mpn: str,
    digikey_params: list[dict[str, str]],
    digikey_category: str,
    digikey_description: str,
    component_type: str,
    taxonomy_dir: Path | None = None,
    api_logger: ApiLogger | None = None,
) -> ComponentModel:
    """Map DigiKey product parameters to taxonomy specs using a lightweight model.

    Returns a ComponentModel ready to persist. Raises on failure.
    """
    tax_dir = taxonomy_dir or settings.taxonomy_dir

    # Auto-generate type-level specs if none exist
    if not has_specs(component_type, tax_dir):
        try:
            await _generate_type_specs(component_type, tax_dir, api_logger=api_logger)
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Failed to auto-generate specs schema for %s", component_type,
                exc_info=True,
            )

    subtypes_text = format_for_prompt(component_type, tax_dir)
    specs_text = format_specs_for_prompt(component_type, tax_dir)

    system = _AUTO_RESOLVE_SYSTEM.format(
        component_type=component_type,
        subtypes_text=subtypes_text,
        specs_text=specs_text,
    )

    # Format DigiKey params as readable text
    params_lines = [f"- {p['name']}: {p['value']}" for p in digikey_params]
    user_text = (
        f"MPN: {mpn}\n"
        f"Category: {digikey_category}\n"
        f"Description: {digikey_description}\n\n"
        f"DISTRIBUTOR PARAMETERS:\n" + "\n".join(params_lines)
    )

    async def _call(provider, model_name):
        session = await provider.create_session(
            model=model_name, system=system, max_tokens=10_000,
        )
        t0 = time.monotonic()
        try:
            completion = await session.complete(
                messages=[Message("user", [TextBlock(user_text)])],
                tools=[_to_tool(AUTO_RESOLVE_TOOL)],
                tool_choice={"name": "save_resolved_specs"},
            )
        finally:
            await session.close()
        return completion, time.monotonic() - t0, provider.name, model_name

    completion, elapsed, provider_name, model_name = await call_with_fallback(
        "auto_resolve", _call,
    )

    # Parse forced tool response
    result: dict | None = None
    for tc in completion.tool_calls:
        if tc.name == "save_resolved_specs":
            result = tc.input
            break
    if not result:
        raise RuntimeError(f"Auto-resolve failed for {mpn}: no tool response")

    import logging as _logging
    _logging.getLogger(__name__).info(
        "Auto-resolved %s → %s in %.1fs (model=%s, in=%d, out=%d)",
        mpn, result.get("component_subtype", "?"), elapsed, model_name,
        completion.usage.input_tokens, completion.usage.output_tokens,
    )
    if api_logger:
        api_logger.log(
            stage="auto_resolve", identifier=mpn,
            model=model_name, provider=provider_name,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            cache_creation_input_tokens=completion.usage.cache_creation_tokens,
            cache_read_input_tokens=completion.usage.cache_read_tokens,
            duration_ms=int(elapsed * 1000),
            stop_reason=completion.stop_reason,
            turns=1,
        )

    # Ensure taxonomy entry for new subtypes
    subtype = result.get("component_subtype", "")
    subtype_desc = result.get("component_subtype_description", "")
    if subtype and not get_subtype(subtype, tax_dir):
        add_subtype(
            subtype, subtype_desc or f"(auto-added for {mpn})",
            example_mpn=mpn, directory=tax_dir,
        )

    # Filter values to taxonomy-defined parameter names only
    allowed_keys = {s["name"] for s in get_specs_schema(component_type, subtype, tax_dir)}
    raw_values = result.get("values", {})
    filtered_values = {k: v for k, v in raw_values.items() if k in allowed_keys}

    # Include package in values if taxonomy defines it
    pkg = result.get("package")
    if pkg and "package" in allowed_keys:
        filtered_values.setdefault("package", pkg)

    specs = SimpleComponentSpecs(
        specs_type=component_type,
        component_subtype=subtype,
        values=filtered_values,
    )

    # Convert passive SimpleComponentSpecs to typed models
    if component_type == "passive":
        from backend.pinscopex.resolve_passives import simple_to_typed_passive_specs
        typed = simple_to_typed_passive_specs(specs)
        return ComponentModel(mpn=mpn, specs=typed)

    return ComponentModel(mpn=mpn, specs=specs)


# ---------------------------------------------------------------------------
# Value-based fallback (last resort when no MPN, no datasheet, no DigiKey hit)
# ---------------------------------------------------------------------------

_PASSIVE_PREFIX_HINT: dict[str, str] = {
    "C": "capacitor — populate value_farads",
    "R": "resistor — populate value_ohms",
    "L": "inductor — populate value_henries",
    "FB": "ferrite bead — populate value_ohms (impedance)",
}

_VALUE_RESOLVE_SYSTEM = """\
You are parsing a passive component value string from a schematic BOM when no
manufacturer part number and no datasheet are available. The only signal you
have is a value string (e.g. "10uF", "4.7k", "100nH") and the reference-designator
prefix telling you whether it is R/C/L.

COMPONENT TYPE: {component_type}

EXISTING SUBTYPES:
{subtypes_text}

{specs_text}

CRITICAL RULES:
- You ONLY have a value string. You do NOT know the tolerance, voltage rating,
  dielectric, package, or power rating. Never invent these.
- Populate EXACTLY TWO fields: ``value_formatted`` (a normalized human-readable
  string) and the matching primary numeric field
  (``value_farads`` / ``value_ohms`` / ``value_henries``). Leave every other
  parameter out (do not include a null entry — omit the key entirely).
- Express numeric values with SPICE multiplier prefixes and units
  (u=1e-6, n=1e-9, p=1e-12, k=1e3, M=1e6). Examples: ``10uF``, ``4.7kohm``, ``100nH``.
- Pick the GENERIC parent subtype — e.g. ``passive.capacitor``, ``passive.resistor``,
  ``passive.inductor``. Do NOT guess a more specific subtype (ceramic, tantalum,
  film, etc.) from a value alone. Only use subtypes that already exist in the
  EXISTING SUBTYPES list.
- If the value string is ambiguous or clearly not a passive component value
  (e.g. an IC part number, a net name), still produce your best guess but keep
  it to the parent subtype.

Call save_resolved_specs with the mapped values.\
"""


async def resolve_from_value(
    *,
    mpn: str,
    value: str,
    ref_prefix: str,
    component_type: str = "passive",
    taxonomy_dir: Path | None = None,
    api_logger: ApiLogger | None = None,
) -> ComponentModel:
    """Map a bare BOM value string (e.g. ``10uF``) to typed passive specs.

    Last-resort fallback used when the BOM's MPN column contains a value rather
    than a real part number and DigiKey has no matching hit. Only sets the
    primary value — never fabricates tolerance, voltage, dielectric, or package.
    Never auto-adds new taxonomy subtypes; callers should NOT persist the result
    to the shared library because the ``mpn`` is not a real part number.
    """
    tax_dir = taxonomy_dir or settings.taxonomy_dir

    if not has_specs(component_type, tax_dir):
        try:
            await _generate_type_specs(component_type, tax_dir, api_logger=api_logger)
        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to auto-generate specs schema for %s", component_type,
                exc_info=True,
            )

    subtypes_text = format_for_prompt(component_type, tax_dir)
    specs_text = format_specs_for_prompt(component_type, tax_dir)

    system = _VALUE_RESOLVE_SYSTEM.format(
        component_type=component_type,
        subtypes_text=subtypes_text,
        specs_text=specs_text,
    )

    hint = _PASSIVE_PREFIX_HINT.get(ref_prefix.upper(), "")
    user_text = (
        f"BOM token (used as MPN): {mpn}\n"
        f"BOM value: {value}\n"
        f"Reference prefix: {ref_prefix}"
        + (f" ({hint})" if hint else "")
    )

    async def _call(provider, model_name):
        session = await provider.create_session(
            model=model_name, system=system, max_tokens=512,
        )
        t0 = time.monotonic()
        try:
            completion = await session.complete(
                messages=[Message("user", [TextBlock(user_text)])],
                tools=[_to_tool(AUTO_RESOLVE_TOOL)],
                tool_choice={"name": "save_resolved_specs"},
            )
        finally:
            await session.close()
        return completion, time.monotonic() - t0, provider.name, model_name

    completion, elapsed, provider_name, model_name = await call_with_fallback(
        "auto_resolve", _call,
    )

    result: dict | None = None
    for tc in completion.tool_calls:
        if tc.name == "save_resolved_specs":
            result = tc.input
            break
    if not result:
        raise RuntimeError(f"Value fallback failed for {mpn}: no tool response")

    logging.getLogger(__name__).info(
        "Resolved from value %s=%r → %s in %.1fs (model=%s)",
        mpn, value, result.get("component_subtype", "?"), elapsed, model_name,
    )
    if api_logger:
        api_logger.log(
            stage="value_resolve", identifier=mpn,
            model=model_name, provider=provider_name,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            cache_creation_input_tokens=completion.usage.cache_creation_tokens,
            cache_read_input_tokens=completion.usage.cache_read_tokens,
            duration_ms=int(elapsed * 1000),
            stop_reason=completion.stop_reason,
            turns=1,
        )

    subtype = result.get("component_subtype", "") or "passive"
    # Do NOT auto-add subtypes here — we only have a value, not a real part.
    if not get_subtype(subtype, tax_dir):
        subtype = component_type  # fall back to top-level type

    allowed_keys = {s["name"] for s in get_specs_schema(component_type, subtype, tax_dir)}
    raw_values = result.get("values", {})
    filtered_values = {k: v for k, v in raw_values.items() if k in allowed_keys and v is not None}

    specs = SimpleComponentSpecs(
        specs_type=component_type,
        component_subtype=subtype,
        values=filtered_values,
    )

    if component_type == "passive":
        from backend.pinscopex.resolve_passives import simple_to_typed_passive_specs
        typed = simple_to_typed_passive_specs(specs)
        return ComponentModel(mpn=mpn, specs=typed)

    return ComponentModel(mpn=mpn, specs=specs)

