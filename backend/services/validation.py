"""Async direct datasheet review — per-IC with graph tools.

Each IC gets a review call with its datasheet PDF and circuit neighborhood.
ICs run concurrently with a semaphore. Provider-agnostic — routes through
the LLM provider abstraction so a stage env var (PROVIDER_VALIDATION) can
flip between Anthropic and Gemini without code changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

from backend.pinscopex.models import (
    ComponentConstraints,
    ComponentType,
    DesignGraph,
    Finding,
    NetType,
    ValidationReport,
)
from backend.pinscopex.validate import (
    SYSTEM_PROMPT,
    _MAX_REVIEW_TURNS,
    ReviewResult,
    _load_datasheets,
    _match_constraints,
    _build_constraints_map,
    assign_finding_ids,
    build_component_context,
    _parse_review,
)
from backend.pinscopex.utils import safe_mpn
from backend.pinscopex.pin_mux_check import check_pin_mux_feasibility
from backend.pinscopex.led_current_check import check_led_current

TRACE_VERSION = 1


def _is_deterministic(f: Finding) -> bool:
    """True for a finding produced by a deterministic check (not the LLM review)."""
    return bool(getattr(f, "source", None)) and f.source != "review"


def _run_deterministic_checks(
    graph: DesignGraph, constraints_map: dict
) -> list[Finding]:
    """Run the deterministic graph checks, fail-soft per check — a check bug
    can never break the review or the report."""
    out: list[Finding] = []
    for name, fn in (
        ("pin_mux_check", lambda: check_pin_mux_feasibility(graph, constraints_map)),
        ("led_current_check", lambda: check_led_current(graph)),
    ):
        try:
            out.extend(fn())
        except Exception:
            log.exception("deterministic check %s failed — skipping", name)
    return out


def _assistant_text(blocks) -> str:
    """Best-effort extraction of text content from a completion's raw
    assistant blocks. Provider-agnostic and never raises."""
    parts: list[str] = []
    try:
        for b in blocks or []:
            txt = getattr(b, "text", None)
            if txt is None and isinstance(b, dict):
                txt = b.get("text") if b.get("type") == "text" else None
            elif getattr(b, "type", None) not in (None, "text"):
                txt = None
            if isinstance(txt, str) and txt:
                parts.append(txt)
    except Exception:
        log.exception("trace: assistant_text extraction failed")
    return "\n".join(parts)
from backend.pinscopex.validation_tools import (
    ALL_TOOLS,
    SUBMIT_REVIEW_SCHEMA,
    ConstraintsMap,
    ExcerptState,
    execute_tool,
)
from backend.pinscopex.utils import safe_mpn

from backend.config import settings
from backend.services.api_logs import ApiLogger
from backend.services.normalize_findings import normalize_findings_async
from backend.services.dedupe_findings import dedupe_cross_ic_findings_async
from backend.services.llm import (
    Message,
    PdfBlock,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolSchema,
    call_with_fallback,
)

# Type for progress callback: (ref, turn, tool_name_or_status, detail)
ProgressCallback = Callable[[str, int, str, str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Tool schemas — defined as dicts in validation_tools.py, converted here
# ---------------------------------------------------------------------------


def _to_tool_schema(d: dict) -> ToolSchema:
    return ToolSchema(
        name=d["name"],
        description=d["description"],
        input_schema=d["input_schema"],
    )


_ALL_TOOL_SCHEMAS = [_to_tool_schema(t) for t in ALL_TOOLS]
_SUBMIT_TOOL_SCHEMA = _to_tool_schema(SUBMIT_REVIEW_SCHEMA)


# ---------------------------------------------------------------------------
# Review keywords for PDF page trimming
# ---------------------------------------------------------------------------

_REVIEW_KEYWORDS = re.compile(
    r"pin\s+(out|diagram|configuration|description|assignment|function|name|table|map)"
    r"|ball\s+map|package\s+(pin|drawing|outline)|signal\s+description"
    r"|absolute\s+maximum|recommended\s+operating|electrical\s+characteristics"
    r"|power\s+supply|thermal\s+(resistance|shutdown|pad)|ESD\s+(rating|tolerance)"
    r"|decoupling|bypass\s+capacitor|layout\s+(guideline|recommendation)"
    r"|application\s+(circuit|schematic|information|note)"
    r"|typical\s+application|reference\s+design",
    re.IGNORECASE,
)

_MAX_PDF_PAGES = 90

# Per-review excerpt budget — keeps fan-out cost bounded on hub ICs (e.g. an
# MCU connected to many neighbors). On exhaustion, the tool returns a budget
# message and the model is steered to submit WARNING with Unverified:
# assumption rather than fetching more.
#
# The global page budget got raised from 25→60 and gained a per-neighbor
# sub-budget after the U2-001 / U3-001 false positives: a single 25-page
# global cap was exhausted by one neighbor's pin_voltage_levels excerpt
# before the abs-max table could be read, so the reviewer was forced to
# guess at the very moment it was trying to verify a damage claim. 30 pages
# per neighbor fits the ~3 topic fetches (pin levels + abs-max + electrical)
# one interface check needs; 60 global allows ~2 such neighbors before the
# fan-out ceiling kicks in.
_PER_REVIEW_FETCH_BUDGET = 8
_PER_REVIEW_PAGE_BUDGET = 60
_PER_NEIGHBOR_PAGE_BUDGET = 30

# A signal net with more components than this is treated as a hub/bus and
# excluded from the neighbor set even if classified as "signal". Bounds
# fan-out on designs that use an oversized common signal (rare but possible).
_SIGNAL_NET_MAX_COMPONENTS = 8


def _signal_neighbors(graph: DesignGraph, ic_ref: str) -> set[str]:
    """Return the set of designators that share at least one *signal* net
    with ``ic_ref``. Excludes power/ground rails (which connect every IC and
    would otherwise fan the neighbor set out across the whole design) and
    excludes the IC under review itself.
    """
    comp = graph.components.get(ic_ref)
    if not comp:
        return set()
    neighbors: set[str] = set()
    for net_name in set(comp.pins.values()):
        net = graph.nets.get(net_name)
        if not net:
            continue
        if net.net_type in (NetType.POWER, NetType.GROUND):
            continue
        refs_on_net = {pc.component_ref for pc in net.pins}
        if len(refs_on_net) > _SIGNAL_NET_MAX_COMPONENTS:
            continue
        for ref in refs_on_net:
            if ref != ic_ref:
                neighbors.add(ref)
    return neighbors


def _select_review_pages(pdf_path: str) -> str:
    """Trim a datasheet PDF to pages relevant for design review.

    Returns path to trimmed PDF (or original if already small enough).

    Note: the reviewer cites the datasheet's *printed* page number (read from
    the page content/footer), not the page's physical position in the trimmed
    file — so `source_page` already matches the full original PDF the frontend
    serves. No trimmed→original remap is applied (an earlier remap attempt
    corrupted correct citations on large datasheets).
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(pdf_path)
    total = len(reader.pages)
    if total <= _MAX_PDF_PAGES:
        return pdf_path

    # Always keep first 5 pages (title, TOC, overview)
    keep: set[int] = set(range(min(5, total)))

    # Keyword-matched pages + neighbors
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if _REVIEW_KEYWORDS.search(text):
            for neighbor in (i - 1, i, i + 1):
                if 0 <= neighbor < total:
                    keep.add(neighbor)

    # Pad from front if under budget
    if len(keep) < _MAX_PDF_PAGES:
        for i in range(total):
            if len(keep) >= _MAX_PDF_PAGES:
                break
            keep.add(i)

    selected = sorted(keep)[:_MAX_PDF_PAGES]

    writer = PdfWriter()
    for i in selected:
        writer.add_page(reader.pages[i])

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    writer.write(tmp)
    tmp.close()
    return tmp.name


# ---------------------------------------------------------------------------
# Per-IC async review
# ---------------------------------------------------------------------------


async def review_ic_async(
    graph: DesignGraph,
    constraints_map: ConstraintsMap,
    ic_ref: str,
    pdf_path: str,
    on_progress: ProgressCallback | None = None,
    api_logger: ApiLogger | None = None,
    trace_git_commit: str = "unknown",
    pdf_dir: Path | None = None,
    storage=None,
    excerpt_cache: dict | None = None,
) -> tuple[ReviewResult, dict]:
    """Review one IC against its datasheet. Async, multi-turn.

    Returns ``(ReviewResult, trace)`` — ``trace`` is a transcript dict of the
    full agentic loop (turns, tool calls + outputs, final submission) for
    offline inspection. Trace assembly is best-effort and never affects the
    review result.
    """
    comp = graph.components[ic_ref]
    mpn = comp.mpn or comp.value

    # Datasheet identity for the trace — hash the original PDF, not the
    # trimmed copy, so the reference is stable across trim-heuristic changes.
    try:
        ds_md5 = hashlib.md5(Path(pdf_path).read_bytes()).hexdigest()
    except Exception:
        log.exception("trace: datasheet md5 failed for %s", ic_ref)
        ds_md5 = None

    # Pre-compute which designators the excerpt tool will accept for this
    # review (neighbors via signal nets only — power/GND fan-out filtered).
    connected_designators = _signal_neighbors(graph, ic_ref)

    # Designator -> MPN, so a finding citing a neighbor's datasheet excerpt
    # (source_designator) is referenced against — and viewed from — that
    # neighbor's datasheet rather than this IC's.
    mpn_by_designator = {
        ref: comp.mpn
        for ref, comp in graph.components.items()
        if comp.mpn
    }

    # Build the per-review state for the excerpt tool. ``cache`` is shared
    # across ICs in the same validate_design_async run so symmetric checks
    # (U2 fetches U3@abs_max, then U3 fetches U2@abs_max) don't redo pypdf
    # work.
    excerpt_state = ExcerptState(
        current_ic=ic_ref,
        connected_designators=connected_designators,
        graph=graph,
        pdf_dir=pdf_dir or Path(pdf_path).parent,
        storage=storage,
        cache=excerpt_cache if excerpt_cache is not None else {},
        fetch_budget=_PER_REVIEW_FETCH_BUDGET,
        page_budget=_PER_REVIEW_PAGE_BUDGET,
        per_neighbor_page_budget=_PER_NEIGHBOR_PAGE_BUDGET,
    )

    # Trim PDF up-front — both primary and fallback attempts share it.
    trimmed_pdf = _select_review_pages(pdf_path)
    try:
        async def _run(provider, model) -> tuple[ReviewResult, dict]:
            t0 = time.monotonic()
            total_input = 0
            total_output = 0
            total_cache_creation = 0
            total_cache_read = 0
            turns = 0

            session = await provider.create_session(
                model=model,
                system=SYSTEM_PROMPT,
                # Gemini 2.5/3 thinking models count thoughts against this cap.
                # 4096 was too tight: U3 (largest IC) burned the entire budget
                # on thinking and emitted zero visible output, dropping its
                # review silently.
                max_tokens=32768,
                # Deterministic sampling: same inputs → same findings across
                # reruns. The default temperature of 1.0 caused identical
                # netlists to produce very different reports (different
                # findings + severities) run-to-run.
                temperature=0.0,
            )
            try:
                context = build_component_context(graph, constraints_map, ic_ref)

                initial_msg = Message(
                    role="user",
                    content=[
                        PdfBlock(path=Path(trimmed_pdf), cacheable=True),
                        TextBlock(
                            text=f"Review this component's usage:\n\n{context}",
                            cacheable=True,
                        ),
                    ],
                )
                messages: list[Message] = [initial_msg]

                trace: dict = {
                    "trace_version": TRACE_VERSION,
                    "ic_ref": ic_ref,
                    "mpn": mpn,
                    "model": model,
                    "provider": provider.name,
                    "git_commit": trace_git_commit,
                    "datasheet": {"md5": ds_md5, "safe_mpn": safe_mpn(mpn)},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "max_turns": _MAX_REVIEW_TURNS,
                    "turns": [],
                    "final_submission": None,
                    "result": None,
                    "stop_reason": None,
                    "error": None,
                    "duration_ms": None,
                }

                # Set after a turn produces zero tool calls (model wrote
                # text only). Next turn is forced to submit_review so any
                # findings drafted as prose still make it to the report.
                force_submit_next_turn = False

                for turn in range(_MAX_REVIEW_TURNS):
                    is_last_turn = turn == _MAX_REVIEW_TURNS - 1

                    if is_last_turn or force_submit_next_turn:
                        tools = [_SUBMIT_TOOL_SCHEMA]
                        tool_choice: dict | str = {"name": "submit_review"}
                    else:
                        tools = _ALL_TOOL_SCHEMAS
                        tool_choice = "auto"

                    completion = await session.complete(
                        messages=messages,
                        tools=tools,
                        tool_choice=tool_choice,
                    )
                    turns += 1
                    print(f"{completion=}")
                    total_input += completion.usage.input_tokens
                    total_output += completion.usage.output_tokens
                    total_cache_creation += completion.usage.cache_creation_tokens
                    total_cache_read += completion.usage.cache_read_tokens

                    turn_record: dict = {
                        "index": turn,
                        "assistant_text": _assistant_text(
                            completion.raw_assistant_blocks
                        ),
                        "tool_calls": [],
                        "usage": {
                            "input_tokens": completion.usage.input_tokens,
                            "output_tokens": completion.usage.output_tokens,
                            "cache_creation_tokens": completion.usage.cache_creation_tokens,
                            "cache_read_tokens": completion.usage.cache_read_tokens,
                        },
                    }
                    try:
                        trace["turns"].append(turn_record)
                    except Exception:
                        log.exception("trace: turn append failed for %s", ic_ref)

                    # Check for submit_review
                    for tc in completion.tool_calls:
                        if tc.name == "submit_review":
                            result = _parse_review(
                                tc.input, ic_ref, mpn,
                                mpn_by_designator=mpn_by_designator,
                                connected=connected_designators,
                            )
                            turn_record["tool_calls"].append({
                                "name": "submit_review",
                                "input": tc.input,
                                "output": None,
                                "duration_ms": None,
                            })
                            trace["final_submission"] = tc.input
                            trace["stop_reason"] = "submit_review"
                            trace["result"] = {
                                "findings_count": len(result.findings),
                                "checked_areas": result.checked_areas,
                            }
                            trace["duration_ms"] = int((time.monotonic() - t0) * 1000)
                            if on_progress:
                                await on_progress(
                                    ic_ref, turn, "submit_review",
                                    f"{len(result.findings)} findings",
                                )
                            if api_logger:
                                api_logger.log(
                                    stage="review", identifier=ic_ref,
                                    model=model, provider=provider.name,
                                    input_tokens=total_input, output_tokens=total_output,
                                    cache_creation_input_tokens=total_cache_creation,
                                    cache_read_input_tokens=total_cache_read,
                                    duration_ms=int((time.monotonic() - t0) * 1000),
                                    stop_reason="submit_review", turns=turns,
                                )
                            if settings.normalize_findings_enabled:
                                try:
                                    normalized, norm_trace = await normalize_findings_async(
                                        ic_ref, mpn, result.findings,
                                        api_logger=api_logger,
                                        on_progress=on_progress,
                                    )
                                    trace["normalize"] = norm_trace
                                    result.findings = normalized
                                    trace["result"]["findings_count"] = len(normalized)
                                except Exception:
                                    log.exception(
                                        "normalize: unexpected failure for %s "
                                        "— keeping reviewer findings",
                                        ic_ref,
                                    )
                            return result, trace

                    # Process graph tool calls
                    tool_results: list[ToolResultBlock] = []
                    attached_pdfs: list[PdfBlock] = []
                    for tc in completion.tool_calls:
                        _tc_t0 = time.monotonic()
                        result_text, attachment = execute_tool(
                            graph, constraints_map, tc.name, tc.input,
                            state=excerpt_state,
                        )
                        turn_record["tool_calls"].append({
                            "name": tc.name,
                            "input": tc.input,
                            "output": result_text,
                            "duration_ms": int((time.monotonic() - _tc_t0) * 1000),
                        })
                        if on_progress:
                            await on_progress(
                                ic_ref, turn, tc.name, json.dumps(tc.input),
                            )
                        tool_results.append(ToolResultBlock(
                            tool_use_id=tc.id,
                            name=tc.name,
                            content=result_text,
                        ))
                        if attachment is not None:
                            attached_pdfs.append(attachment)

                    if not tool_results:
                        # Model emitted text but called no tools. This is a
                        # known failure mode (esp. with reasoning models)
                        # where the model writes findings as a JSON code
                        # block in prose instead of calling submit_review.
                        # Don't drop the work — append a nudge and force
                        # submit_review on the next iteration.
                        if not is_last_turn and not force_submit_next_turn:
                            messages.append(Message(
                                role="assistant",
                                content=completion.raw_assistant_blocks,
                            ))
                            messages.append(Message(
                                role="user",
                                content=[TextBlock(
                                    text=(
                                        "You produced text but did not call "
                                        "any tool. Findings only reach the "
                                        "report when submitted via the "
                                        "submit_review tool — text JSON is "
                                        "ignored. Call submit_review now with "
                                        "the findings you identified (or an "
                                        "empty findings array if none) and "
                                        "your checked_areas list."
                                    ),
                                )],
                            ))
                            force_submit_next_turn = True
                            continue
                        break

                    # Reset recovery flag once the model is calling tools again.
                    force_submit_next_turn = False

                    messages.append(Message(role="assistant", content=completion.raw_assistant_blocks))
                    # tool_result blocks first, then any PdfBlocks the tools
                    # attached (excerpt fetches). The Anthropic provider
                    # encodes each block independently — mixed-block user
                    # messages are supported and the cached initial PDF is
                    # not invalidated by appending uncached/cached content.
                    messages.append(Message(
                        role="user",
                        content=[*tool_results, *attached_pdfs],
                    ))

                # Fell through without submitting
                trace["stop_reason"] = "no_submission"
                trace["result"] = {"findings_count": 0, "checked_areas": []}
                trace["duration_ms"] = int((time.monotonic() - t0) * 1000)
                if api_logger:
                    api_logger.log(
                        stage="review", identifier=ic_ref,
                        model=model, provider=provider.name,
                        input_tokens=total_input, output_tokens=total_output,
                        cache_creation_input_tokens=total_cache_creation,
                        cache_read_input_tokens=total_cache_read,
                        duration_ms=int((time.monotonic() - t0) * 1000),
                        stop_reason="no_submission", turns=turns,
                    )
                return ReviewResult([], []), trace
            finally:
                await session.close()

        return await call_with_fallback("validation", _run)
    finally:
        if trimmed_pdf != pdf_path:
            Path(trimmed_pdf).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PDF resolution
# ---------------------------------------------------------------------------


def _find_pdf(
    mpn: str,
    pdf_dir: Path,
    storage=None,
) -> Path | None:
    """Find the datasheet PDF for an MPN.  Checks local dir first,
    then tries to download from the library.
    """
    safe = safe_mpn(mpn)
    local = pdf_dir / f"{safe}.pdf"
    if local.is_file():
        return local

    if storage:
        from backend.services import projects as proj_svc
        lib_key = proj_svc.library_has_datasheet(storage, mpn)
        if lib_key:
            storage.download_to_local(lib_key, local)
            if local.is_file():
                return local

    return None


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


BeforeIcCallback = Callable[[str], Awaitable[bool]]
"""Gate callback — called with the IC ref before review. Return False to pause."""

OnIcDoneCallback = Callable[[str, "ReviewResult", "ApiLogger | None"], Awaitable[None]]
"""Callback after each IC finishes successfully — used to charge credits.

Receives the IC's private ``ApiLogger`` (the calls made during this review)
so the charge can be attributed to exactly this IC under concurrency."""

OnIcErrorCallback = Callable[[str, BaseException], Awaitable[None]]
"""Callback after an IC review raises — used to record a SkippedItem so the
failure surfaces in the project's skipped_components list."""

OnDedupeDoneCallback = Callable[["ApiLogger | None"], Awaitable[None]]
"""Callback after the cross-IC dedup pass finishes — used to charge for that
single LLM call (it runs once at end-of-run, outside any per-IC logger)."""


async def validate_design_async(
    graph_path: str,
    output_path: str,
    datasheets_dir: str = "datasheets/extracted",
    pdf_dir: str = "uploads/datasheets",
    on_progress: ProgressCallback | None = None,
    api_logger: ApiLogger | None = None,
    storage=None,
    skip_refs: set[str] | None = None,
    before_ic: BeforeIcCallback | None = None,
    on_ic_done: OnIcDoneCallback | None = None,
    on_ic_error: OnIcErrorCallback | None = None,
    on_dedupe_done: OnDedupeDoneCallback | None = None,
    project_prefix: str | None = None,
    run_meta: dict | None = None,
) -> ValidationReport:
    """Review every IC against its datasheet.

    By default runs concurrently via an asyncio.Semaphore.  When ``before_ic``
    is supplied, reviews are executed sequentially so the callback can
    decide whether to pause the run between ICs.  In that mode the report
    is written incrementally after each IC so a pause preserves all
    completed findings.

    ``skip_refs`` is consumed on the first pass — any IC in the set is
    skipped without starting a review (used to resume a paused run).
    """
    skip_refs = skip_refs or set()

    raw = json.loads(Path(graph_path).read_text())
    graph = DesignGraph.model_validate(raw)
    datasheets = _load_datasheets(datasheets_dir)
    constraints_map = _build_constraints_map(datasheets)

    # Deterministic graph checks (pin-mux feasibility, LED current). Pure
    # functions of the graph; fail-soft. Seeded into all_findings below.
    deterministic_findings = _run_deterministic_checks(graph, constraints_map)

    pdf_dir_path = Path(pdf_dir)

    # Collect ICs that have a datasheet PDF available
    ic_tasks: list[tuple[str, str]] = []  # (ref, pdf_path)
    not_reviewed: list[dict] = []  # ICs skipped for lack of a datasheet PDF
    for ref, comp in sorted(graph.components.items()):
        if comp.component_type != ComponentType.IC:
            continue
        mpn = comp.mpn or comp.value
        pdf = _find_pdf(mpn, pdf_dir_path, storage=storage)
        if pdf:
            ic_tasks.append((ref, str(pdf)))
        else:
            not_reviewed.append({"designator": ref, "reason": "no datasheet PDF"})
            if on_progress:
                await on_progress(ref, 0, "skipped", "no datasheet PDF")

    # Load any previously-written report so we can accumulate findings
    # across a pause/resume cycle without losing prior results.
    existing_path = Path(output_path)
    preserved_findings: list[Finding] = []
    preserved_coverage: dict[str, list[str]] = {}
    preserved_comments = None
    if existing_path.is_file():
        try:
            existing = json.loads(existing_path.read_text())
            preserved_comments = existing.get("comments")
            if before_ic is not None:
                # Resume mode — keep findings for refs we're about to skip
                for f in existing.get("findings", []):
                    ref = f.get("component_ref") or f.get("designator") or ""
                    if ref in skip_refs:
                        preserved_findings.append(Finding.model_validate(f))
                for ref, areas in (existing.get("coverage") or {}).items():
                    if ref in skip_refs:
                        preserved_coverage[ref] = list(areas)
        except (json.JSONDecodeError, OSError):
            pass

    # Seed deterministic findings exactly once. On resume, preserved_findings may
    # already contain them (they were written to the prior report), so strip any
    # deterministic findings before re-seeding to avoid double-counting.
    preserved_review = [f for f in preserved_findings if not _is_deterministic(f)]
    all_findings: list[Finding] = list(preserved_review) + list(deterministic_findings)
    all_coverage: dict[str, list[str]] = dict(preserved_coverage)
    review_errors: dict[str, str] = {}

    def _sanitize_coverage(src: dict[str, list[str]]) -> dict[str, list[str]]:
        """Drop any entries that aren't a list of strings so one IC's bad
        payload can't fail the whole ValidationReport validation."""
        clean: dict[str, list[str]] = {}
        for ref, areas in src.items():
            if isinstance(areas, list) and all(isinstance(a, str) for a in areas):
                clean[ref] = areas
            else:
                print(f"[validation] dropping coverage for {ref}: {areas!r}")
        return clean

    def _write_report(paused: bool = False) -> ValidationReport:
        assign_finding_ids(all_findings)
        summary = {"total": len(all_findings), "ERROR": 0, "WARNING": 0, "INFO": 0}
        for f in all_findings:
            summary[f.status] = summary.get(f.status, 0) + 1
        try:
            report = ValidationReport(
                project=Path(graph_path).stem,
                timestamp=datetime.now(timezone.utc).isoformat(),
                findings=all_findings,
                summary=summary,
                coverage=_sanitize_coverage(all_coverage),
                review_errors=dict(review_errors),
                not_reviewed=not_reviewed,
            )
        except Exception as exc:
            print(f"[validation] report build failed, retrying without coverage: {exc}")
            report = ValidationReport(
                project=Path(graph_path).stem,
                timestamp=datetime.now(timezone.utc).isoformat(),
                findings=all_findings,
                summary=summary,
                coverage={},
                review_errors=dict(review_errors),
                not_reviewed=not_reviewed,
            )
        report_dict = json.loads(report.model_dump_json(indent=2))
        if preserved_comments is not None:
            report_dict["comments"] = preserved_comments
        if paused:
            report_dict["partial"] = True
        existing_path.write_text(json.dumps(report_dict, indent=2))
        return report

    git_commit = (run_meta or {}).get("git_commit", "unknown")

    def _write_trace(trace: dict, ref: str) -> None:
        """Persist a per-IC review trace. Best-effort: a trace failure must
        never break the review, the report, or the pipeline."""
        if not storage or not project_prefix or not trace:
            return
        try:
            key = f"{project_prefix}/review_traces/{safe_mpn(ref)}.json"
            storage.write_json(key, trace)
        except Exception:
            log.exception("trace: write failed for %s", ref)

    async def _maybe_dedupe_cross_ic() -> None:
        """Collapse one interface defect reported from both ICs into a single
        finding. Runs once, after all per-IC reviews, when findings span ≥2
        ICs. Mutates ``all_findings`` in place. Best-effort: any failure keeps
        the per-IC findings (the dedup function is itself fail-soft)."""
        if not settings.cross_ic_dedup_enabled:
            return
        # Deterministic findings never enter the LLM dedupe — it has no datasheet
        # basis to judge a pin-mux/LED finding, and merging could mangle them.
        review = [f for f in all_findings if not _is_deterministic(f)]
        deterministic = [f for f in all_findings if _is_deterministic(f)]
        if len({f.designator for f in review}) < 2:
            return  # nothing cross-IC to merge
        # Gated path: charge via a private logger merged by on_dedupe_done.
        # Legacy path (no callback): log straight to the shared logger so the
        # call still shows up in api_logs even though nothing is charged.
        private = (
            ApiLogger(free=api_logger.free)
            if (api_logger is not None and on_dedupe_done is not None)
            else None
        )
        try:
            deduped, dedupe_trace = await dedupe_cross_ic_findings_async(
                review,
                api_logger=private if private is not None else api_logger,
                on_progress=on_progress,
            )
        except Exception:
            log.exception("cross-IC dedupe failed — keeping per-IC findings")
            return
        all_findings[:] = deduped + deterministic
        if storage and project_prefix and dedupe_trace:
            try:
                storage.write_json(
                    f"{project_prefix}/review_traces/_cross_ic_dedupe.json",
                    dedupe_trace,
                )
            except Exception:
                log.exception("trace: cross-IC dedupe write failed")
        # Charge for the single dedup call (gated path only — the private
        # logger merges into the shared log and bills exactly this call).
        if private is not None and on_dedupe_done is not None:
            try:
                await on_dedupe_done(private)
            except Exception:
                log.exception("on_dedupe_done callback failed")

    def _stub_trace(ref: str, error: str) -> dict:
        """Minimal trace for an IC whose review raised before producing one,
        so an eval harness still sees a record for every attempted IC."""
        try:
            comp = graph.components.get(ref)
            mpn = (comp.mpn or comp.value) if comp else ref
        except Exception:
            mpn = ref
        return {
            "trace_version": TRACE_VERSION,
            "ic_ref": ref,
            "mpn": mpn,
            "git_commit": git_commit,
            "datasheet": {"md5": None, "safe_mpn": safe_mpn(mpn)},
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "turns": [],
            "final_submission": None,
            "result": None,
            "stop_reason": "error",
            "error": error,
            "duration_ms": None,
        }

    # Cross-IC excerpt cache — symmetric interface checks (U2 fetches U3@X,
    # U3 fetches U2@X) reuse the trimmed PDF instead of redoing pypdf work.
    # LLM-side ephemeral cache can't span ICs (different conversation prefix),
    # so the win here is purely pypdf I/O.
    excerpt_cache: dict = {}

    def _cleanup_excerpt_cache() -> None:
        for entry in excerpt_cache.values():
            try:
                if isinstance(entry, tuple) and len(entry) == 2:
                    Path(entry[0]).unlink(missing_ok=True)
            except Exception:
                pass

    if before_ic is None:
        # Legacy concurrent path (no credit gate)
        sem = asyncio.Semaphore(settings.ic_concurrency)

        async def _review_one(ref: str, pdf_path: str) -> tuple[ReviewResult, dict]:
            async with sem:
                return await review_ic_async(
                    graph, constraints_map, ref, pdf_path,
                    on_progress=on_progress, api_logger=api_logger,
                    trace_git_commit=git_commit,
                    pdf_dir=pdf_dir_path, storage=storage,
                    excerpt_cache=excerpt_cache,
                )

        results = await asyncio.gather(
            *(_review_one(ref, pdf) for ref, pdf in ic_tasks if ref not in skip_refs),
            return_exceptions=True,
        )
        remaining_tasks = [t for t in ic_tasks if t[0] not in skip_refs]
        for i, result in enumerate(results):
            ref = remaining_tasks[i][0]
            if isinstance(result, BaseException):
                msg = f"{type(result).__name__}: {result}"
                log.exception("Review failed for %s", ref, exc_info=result)
                review_errors[ref] = msg
                _write_trace(_stub_trace(ref, msg), ref)
                if on_progress:
                    await on_progress(ref, 0, "error", msg)
                if on_ic_error is not None:
                    try:
                        await on_ic_error(ref, result)
                    except Exception:
                        log.exception("on_ic_error callback failed for %s", ref)
            elif isinstance(result, tuple):
                rr, trace = result
                _write_trace(trace, ref)
                all_findings.extend(rr.findings)
                if rr.checked_areas:
                    all_coverage[ref] = rr.checked_areas
        await _maybe_dedupe_cross_ic()
        try:
            return _write_report(paused=False)
        finally:
            _cleanup_excerpt_cache()

    # Gated concurrent path — used by the pipeline with credit enforcement.
    # Runs up to ``ic_concurrency`` reviews in parallel while keeping the
    # per-IC credit gate, incremental report/trace writes, and the charging
    # callback. Each IC reviews against a private ApiLogger so concurrent
    # reviews don't interleave their API entries — on_ic_done charges exactly
    # that IC's calls.
    sem = asyncio.Semaphore(settings.ic_concurrency)
    stop = False  # set once a gate trips — stops *starting* new reviews

    async def _gated_review_one(ref: str, pdf_path: str) -> None:
        nonlocal stop
        async with sem:
            if stop:
                return
            try:
                ok = await before_ic(ref)
            except Exception:
                ok = True
            if not ok:
                # Out of credits — don't start this or any further IC.
                stop = True
                return
            private = ApiLogger(free=api_logger.free) if api_logger is not None else None
            try:
                result, trace = await review_ic_async(
                    graph, constraints_map, ref, pdf_path,
                    on_progress=on_progress, api_logger=private,
                    trace_git_commit=git_commit,
                    pdf_dir=pdf_dir_path, storage=storage,
                    excerpt_cache=excerpt_cache,
                )
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                log.exception("Review failed for %s", ref)
                review_errors[ref] = msg
                _write_trace(_stub_trace(ref, msg), ref)
                if on_progress:
                    await on_progress(ref, 0, "error", msg)
                if on_ic_error is not None:
                    try:
                        await on_ic_error(ref, exc)
                    except Exception:
                        log.exception("on_ic_error callback failed for %s", ref)
                # Persist the error into the report so the run finishes with a
                # complete picture even if every IC fails.
                try:
                    _write_report(paused=False)
                except Exception:
                    log.exception("incremental report write failed after error on %s", ref)
                return
            # Merge results — synchronous block, atomic under asyncio (no await
            # until the trailing callbacks), so concurrent completions can't
            # corrupt all_findings / all_coverage.
            all_findings.extend(result.findings)
            if result.checked_areas:
                all_coverage[ref] = result.checked_areas
            # Incremental write — preserves state if the process dies.
            # Never let a single IC's bad payload kill the whole pipeline.
            try:
                _write_report(paused=False)
            except Exception as exc:
                print(f"[validation] incremental write failed after {ref}: {exc}")
                all_coverage.pop(ref, None)
                if on_progress:
                    await on_progress(ref, 0, "warning", f"report write failed: {exc}")
            # Per-IC trace flush — written as each IC completes so a cancel/pause
            # preserves every completed trace.
            _write_trace(trace, ref)
            if on_ic_done is not None:
                try:
                    await on_ic_done(ref, result, private)
                except Exception:
                    log.exception("on_ic_done callback failed for %s", ref)

    results = await asyncio.gather(
        *(_gated_review_one(ref, pdf) for ref, pdf in ic_tasks if ref not in skip_refs),
        return_exceptions=True,
    )
    # Surface a hard cancellation so the pipeline's run handler cleans up.
    # Per-IC review failures stay isolated (captured into review_errors above).
    for r in results:
        if isinstance(r, asyncio.CancelledError):
            raise r

    # Dedup only a *complete* run — a paused/partial run may gain more
    # findings on resume, and merging now could collapse a pair before its
    # counterpart exists.
    if not stop:
        await _maybe_dedupe_cross_ic()
    try:
        return _write_report(paused=bool(stop))
    finally:
        _cleanup_excerpt_cache()
