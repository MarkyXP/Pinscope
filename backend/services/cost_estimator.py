"""Pre-flight cost estimator for pipeline runs.

Walks the uploaded BOM + library cache and returns a low/high credit
range the user will see *before* they start a run.  Pure read-only:
no storage writes, no API calls.

The estimator is intentionally conservative.  Low/high bounds are
bracketed around a central estimate (0.7× / 1.4×) so the user always
sees a plausible range rather than a false-precision single number.

Per-call USD is computed from per-stage **token baselines**
(``STAGE_TOKEN_BASELINES``) multiplied by the runtime-resolved
provider+model rate from ``backend.services.llm.pricing.PRICING``.
This means a change to ``PROVIDER_VALIDATION`` / ``MODEL_VALIDATION``
(or any other per-stage routing env var) automatically updates the
estimate — no constant-bumping required. The baselines themselves
are hand-tuned from historical ``api_logs.jsonl`` aggregates and
should be recalibrated periodically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from backend.config import settings
from backend.pinscopex.parsers import parse_bom
from backend.pinscopex.resolve_passives import resolve_mpn
from backend.pinscopex.taxonomy import SIMPLE_TYPES, type_for_ref
from backend.pinscopex.utils import safe_mpn
from backend.services import projects as proj_svc
from backend.services.billing_hook import get_billing
from backend.services.llm.pricing import CACHE_RATES, PRICING
from backend.services.storage import StorageBackend


# ---------------------------------------------------------------------------
# Per-stage token baselines (model-aware estimator)
# ---------------------------------------------------------------------------

# Average tokens per call for a single sub-unit of each stage. Values
# come from aggregating recent ``api_logs.jsonl`` runs across staging +
# prod (see ``scripts/recalibrate_estimator_baselines.py`` follow-up;
# until that lands, eyeball + paste from gcloud-mined stats).
#
# ``settings_stage`` is the key passed to ``settings.model_for_stage`` /
# ``settings.provider_for_stage``. The estimator-stage names mirror the
# ``CostItem.kind`` Literal so the breakdown stays self-consistent.
STAGE_TOKEN_BASELINES: dict[str, dict[str, int | str]] = {
    "ic_extraction": {
        "settings_stage": "pintable",
        "input": 100, "output": 2000,
        "cache_create": 80_000, "cache_read": 170_000,
    },
    "simple_extraction": {
        "settings_stage": "specs",
        "input": 100, "output": 1000,
        "cache_create": 20_000, "cache_read": 20_000,
    },
    "passive_pattern": {
        "settings_stage": "pattern",
        "input": 100, "output": 7000,
        "cache_create": 60_000, "cache_read": 330_000,
    },
    "digikey_resolve": {
        "settings_stage": "auto_resolve",
        "input": 2000, "output": 200,
        "cache_create": 0, "cache_read": 0,
    },
    # Validation review per IC. The multi-turn validator pulls the
    # cached system + graph + datasheet on each turn (~4-5 turns/IC),
    # so cache_read dominates. Page-aware scaling was abandoned — token
    # counts already encompass the PDF via cache reuse, and IC
    # complexity correlates more weakly with raw page count than the
    # old per-page heuristic assumed.
    "review": {
        "settings_stage": "validation",
        "input": 13_500, "output": 2000,
        "cache_create": 110_000, "cache_read": 300_000,
    },
    # Per-IC normalize pass — dedup + severity re-grade. Runs on the
    # already-structured findings (no PDF, no graph tools), one turn,
    # validation-class model (Sonnet). A few hundred input tokens for the
    # rubric, a few hundred output tokens for the normalized list.
    "normalize": {
        "settings_stage": "normalize",
        "input": 1500, "output": 600,
        "cache_create": 0, "cache_read": 0,
    },
    # Cross-IC dedup — one call per run over all findings (no PDF/graph),
    # validation-class model (Sonnet). Slightly larger input than per-IC
    # normalize since it sees every IC's findings at once.
    "cross_ic_dedup": {
        "settings_stage": "normalize",
        "input": 2500, "output": 700,
        "cache_create": 0, "cache_read": 0,
    },
}

LOW_MULT: float = 0.7
HIGH_MULT: float = 1.4


def _resolve_provider_model(model_str: str) -> tuple[str, str]:
    """Split a LiteLLM model string into (provider, model_name).

    e.g. ``"anthropic/claude-sonnet-4-6"`` -> ``("anthropic", "claude-sonnet-4-6")``

    Falls back to ``("anthropic", model_str)`` when there is no slash.
    """
    if "/" in model_str:
        provider, model_name = model_str.split("/", 1)
        return provider, model_name
    return "anthropic", model_str


def estimate_stage_cost_usd(stage: str) -> float:
    """Per-call USD for one sub-unit of ``stage``, model-aware.

    Resolves the model from ``settings.model_for_stage()``, splits the
    LiteLLM model string (e.g. ``"anthropic/claude-sonnet-4-6"``) into
    provider + model name, and computes ``(input_tokens × rate) + ...``
    using the same ``PRICING`` / ``CACHE_RATES`` tables that real billing
    in ``services.llm.pricing.cost_for_entry`` reads. Changing a
    ``MODEL_*`` env var therefore updates the estimate automatically.

    Falls through to ``PRICING[provider]["default"]`` when the resolved
    model is missing from the table — same fallback semantics as the
    real billing code, so a missing pricing entry surfaces uniformly
    everywhere instead of crashing the estimator.

    Raises ``KeyError`` only when ``stage`` itself is unknown.
    """
    base = STAGE_TOKEN_BASELINES[stage]
    settings_stage = str(base["settings_stage"])
    model_str = settings.model_for_stage(settings_stage)
    provider, model_name = _resolve_provider_model(model_str)
    table = PRICING.get(provider) or PRICING["anthropic"]
    rates = table.get(model_name, table["default"])
    cache = CACHE_RATES.get(provider, CACHE_RATES["anthropic"])
    return (
        int(base["input"]) * rates["input"]
        + int(base["output"]) * rates["output"]
        + int(base["cache_create"]) * rates["input"] * cache["create"]
        + int(base["cache_read"]) * rates["input"] * cache["read"]
    ) / 1_000_000


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


UnitKind = Literal[
    "ic_extraction",
    "simple_extraction",
    "passive_pattern",
    "digikey_resolve",
    "review",
]


class CostItem(BaseModel):
    identifier: str   # MPN, ref, or a fixed token like a stage name
    kind: UnitKind
    api_cost_usd: float
    source: Literal["cache_hit", "api_call", "api_call_estimated"]
    note: str | None = None


class CostEstimate(BaseModel):
    """What the pipeline will likely cost this run."""
    api_cost_low: float
    api_cost_high: float
    api_cost_mid: float
    credits_low: float
    credits_high: float
    credits_mid: float
    breakdown: list[CostItem]
    ic_count: int
    simple_count: int
    passive_count: int
    cached_ic_count: int
    cached_simple_count: int
    cached_passive_count: int
    review_ic_count: int


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _load_library_patterns(storage: StorageBackend):
    """Load passive patterns from the library to test cache hits.

    This mirrors the pipeline's own seeding behaviour but keeps the
    estimator synchronous and side-effect free (downloads to a local
    tempdir only if the backend is remote).
    """
    try:
        return proj_svc.load_library_patterns(storage)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def estimate_pipeline_cost(
    storage: StorageBackend,
    user_id: str,
    project_id: str,
) -> CostEstimate:
    """Produce a CostEstimate for the given project without running anything.

    The BOM must already be uploaded; the netlist may or may not be.  If
    the BOM is missing, raises FileNotFoundError.
    """
    bom_key = proj_svc.get_bom_key(storage, user_id, project_id)
    if not bom_key:
        raise FileNotFoundError("BOM not uploaded for this project")

    meta = proj_svc.get_project(storage, user_id, project_id)
    col_map = (meta.bom_columns if meta else None) or {}
    ref_col = col_map.get("reference", "Reference")
    mpn_col = col_map.get("mpn", "Manufacturer Part Number")

    # Download BOM to a local temp path so parse_bom can read it.
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(storage.read_bytes(bom_key))
        bom_local_path = Path(tmp.name)

    try:
        bom = parse_bom(str(bom_local_path), reference_col=ref_col, mpn_col=mpn_col)
    finally:
        bom_local_path.unlink(missing_ok=True)

    # Classify unique MPNs by type.
    ic_mpns: set[str] = set()
    simple_mpns: set[str] = set()
    passive_mpns: set[str] = set()
    for ref, info in bom.items():
        mpn = info.get("mpn")
        if not mpn:
            continue
        typ = type_for_ref(ref)
        if typ == "ic":
            ic_mpns.add(mpn)
        elif typ == "passive":
            passive_mpns.add(mpn)
        elif typ in SIMPLE_TYPES:
            simple_mpns.add(mpn)

    # Load library patterns once so we can resolve passives against cache.
    patterns = _load_library_patterns(storage)

    breakdown: list[CostItem] = []
    cached_ic = 0
    cached_simple = 0
    cached_passive = 0

    # IC extraction
    for mpn in sorted(ic_mpns):
        if proj_svc.library_has_extraction(storage, mpn):
            breakdown.append(CostItem(
                identifier=mpn, kind="ic_extraction",
                api_cost_usd=0.0, source="cache_hit",
                note="library hit",
            ))
            cached_ic += 1
        else:
            breakdown.append(CostItem(
                identifier=mpn, kind="ic_extraction",
                api_cost_usd=round(estimate_stage_cost_usd("ic_extraction"), 4),
                source="api_call_estimated",
            ))

    # Simple component specs
    for mpn in sorted(simple_mpns):
        if proj_svc.library_has_model(storage, mpn):
            breakdown.append(CostItem(
                identifier=mpn, kind="simple_extraction",
                api_cost_usd=0.0, source="cache_hit",
            ))
            cached_simple += 1
        else:
            breakdown.append(CostItem(
                identifier=mpn, kind="simple_extraction",
                api_cost_usd=round(estimate_stage_cost_usd("simple_extraction"), 4),
                source="api_call_estimated",
            ))

    # Passives — pattern resolution covers many MPNs with one pattern
    unresolved_passives: list[str] = []
    for mpn in sorted(passive_mpns):
        if patterns and resolve_mpn(mpn, patterns) is not None:
            breakdown.append(CostItem(
                identifier=mpn, kind="passive_pattern",
                api_cost_usd=0.0, source="cache_hit",
                note="pattern match",
            ))
            cached_passive += 1
            continue
        if proj_svc.library_has_passive_model(storage, mpn):
            breakdown.append(CostItem(
                identifier=mpn, kind="passive_pattern",
                api_cost_usd=0.0, source="cache_hit",
                note="cached passive model",
            ))
            cached_passive += 1
            continue
        unresolved_passives.append(mpn)

    # Each unresolved passive MPN may contribute one pattern extraction.
    # Heuristic: N unique first-7-char prefixes = N new patterns.
    prefixes = {m[:7] for m in unresolved_passives}
    for prefix in sorted(prefixes):
        sample_mpn = next(m for m in unresolved_passives if m.startswith(prefix))
        breakdown.append(CostItem(
            identifier=sample_mpn, kind="passive_pattern",
            api_cost_usd=round(estimate_stage_cost_usd("passive_pattern"), 4),
            source="api_call_estimated",
            note=f"may cover {sum(1 for m in unresolved_passives if m.startswith(prefix))} MPNs",
        ))

    # Direct datasheet review — per IC that has a datasheet available.
    # Cost is the flat per-IC observed average; multi-turn cache reuse
    # makes this less page-sensitive than the old per-page heuristic
    # implied.
    review_per_ic = estimate_stage_cost_usd("review")
    if settings.normalize_findings_enabled:
        review_per_ic += estimate_stage_cost_usd("normalize")
    review_ic_count = 0
    for mpn in sorted(ic_mpns):
        pdf_path = _locate_datasheet_local(storage, user_id, project_id, mpn)
        has_pdf = pdf_path is not None
        has_library_pdf = (
            proj_svc.library_has_datasheet(storage, mpn) is not None
            if not has_pdf else False
        )
        if not (has_pdf or has_library_pdf):
            continue  # Skipped in pipeline — no datasheet, no review
        breakdown.append(CostItem(
            identifier=mpn, kind="review",
            api_cost_usd=round(review_per_ic, 4),
            source="api_call_estimated",
        ))
        review_ic_count += 1

    # One cross-IC dedup call per run, only when ≥2 ICs get reviewed (a
    # single-IC run has no cross-IC pair to merge — see _maybe_dedupe_cross_ic).
    if settings.cross_ic_dedup_enabled and review_ic_count > 1:
        breakdown.append(CostItem(
            identifier="cross-IC dedup", kind="review",
            api_cost_usd=round(estimate_stage_cost_usd("cross_ic_dedup"), 4),
            source="api_call_estimated",
            note="collapses one interface defect reported from both ICs",
        ))

    api_total = sum(item.api_cost_usd for item in breakdown)
    api_low = round(api_total * LOW_MULT, 4)
    api_high = round(api_total * HIGH_MULT, 4)

    billing = get_billing()
    return CostEstimate(
        api_cost_low=api_low,
        api_cost_high=api_high,
        api_cost_mid=round(api_total, 4),
        credits_low=billing.credits_for_api_cost(api_low),
        credits_high=billing.credits_for_api_cost(api_high),
        credits_mid=billing.credits_for_api_cost(api_total),
        breakdown=breakdown,
        ic_count=len(ic_mpns),
        simple_count=len(simple_mpns),
        passive_count=len(passive_mpns),
        cached_ic_count=cached_ic,
        cached_simple_count=cached_simple,
        cached_passive_count=cached_passive,
        review_ic_count=review_ic_count,
    )


def _locate_datasheet_local(
    storage: StorageBackend, user_id: str, project_id: str, mpn: str,
) -> Path | None:
    """Return a local Path to the datasheet PDF if it can be read quickly.

    For LocalStorageBackend, reads directly from disk.  For remote backends
    we skip the page-count read (returns None) — estimator will fall back
    to the mid-cap heuristic rather than downloading the PDF during pre-flight.
    """
    from backend.services.storage import LocalStorageBackend

    if not isinstance(storage, LocalStorageBackend):
        return None
    safe = safe_mpn(mpn)
    key = f"users/{user_id}/projects/{project_id}/uploads/datasheets/{safe}.pdf"
    if storage.exists(key):
        return storage._path(key)  # type: ignore[attr-defined]
    legacy = f"library/datasheets/{safe}.pdf"
    if storage.exists(legacy):
        return storage._path(legacy)  # type: ignore[attr-defined]
    return None
