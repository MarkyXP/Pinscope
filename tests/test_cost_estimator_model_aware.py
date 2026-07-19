"""Verify the cost estimator auto-updates when model routing env vars change.

With LiteLLM the provider is encoded in the model string prefix
(e.g. ``"anthropic/claude-sonnet-4-6"``, ``"gemini/gemini-3-flash"``).
``estimate_stage_cost_usd`` splits the model string and prices against
``services.llm.pricing.PRICING`` — the same table that real billing reads.

These tests pin that contract: same stage, two different models, two
different costs (in the direction the rate table predicts).
"""

from __future__ import annotations

import pytest

from backend.config import settings
from backend.services.cost_estimator import (
    STAGE_TOKEN_BASELINES,
    _resolve_provider_model,
    estimate_stage_cost_usd,
)
from backend.services.llm.pricing import PRICING


@pytest.fixture
def restore_settings():
    """Snapshot per-stage model fields; restore after the test."""
    fields = [
        "default_model",
        "model_validation",
        "model_pintable",
        "model_pattern",
        "model_specs",
        "model_auto_resolve",
        "model_normalize",
    ]
    snapshot = {f: getattr(settings, f) for f in fields}
    yield
    for f, v in snapshot.items():
        setattr(settings, f, v)


def test_resolve_provider_model_splits_prefix():
    """_resolve_provider_model extracts provider from LiteLLM model string."""
    assert _resolve_provider_model("anthropic/claude-sonnet-4-6") == (
        "anthropic", "claude-sonnet-4-6"
    )
    assert _resolve_provider_model("gemini/gemini-3-flash") == (
        "gemini", "gemini-3-flash"
    )
    # No slash -> fallback to anthropic
    assert _resolve_provider_model("claude-sonnet-4-6") == (
        "anthropic", "claude-sonnet-4-6"
    )


def test_review_cost_changes_with_validation_model(restore_settings):
    """Routing validation to Sonnet vs Haiku should produce different
    per-IC review costs — and Haiku should be cheaper than Sonnet."""
    settings.model_validation = "anthropic/claude-sonnet-4-6"
    sonnet_cost = estimate_stage_cost_usd("review")

    settings.model_validation = "anthropic/claude-haiku-4-5"
    haiku_cost = estimate_stage_cost_usd("review")

    assert sonnet_cost > 0
    assert haiku_cost > 0
    # Haiku is ~3x cheaper than Sonnet on input ($1 vs $3) and 3x on
    # output ($5 vs $15). The blended ratio with cache_read should
    # land Haiku at <50% of Sonnet's cost — wide enough margin to be
    # robust to baseline tweaks.
    assert haiku_cost < sonnet_cost * 0.6


def test_review_cost_changes_with_provider_prefix(restore_settings):
    """Changing the model prefix from anthropic to gemini must
    swap the rate table the estimator pulls from."""
    settings.model_validation = "anthropic/claude-sonnet-4-6"
    anthropic_cost = estimate_stage_cost_usd("review")

    settings.model_validation = "gemini/gemini-3.1-pro-preview"
    gemini_cost = estimate_stage_cost_usd("review")

    # Both > 0 and they're different — the test doesn't lock direction
    # because the cache-read multiplier asymmetry between providers
    # could legitimately swing it either way as the rate tables evolve.
    assert anthropic_cost > 0
    assert gemini_cost > 0
    assert abs(anthropic_cost - gemini_cost) > 0.01, (
        f"expected materially different costs, got "
        f"anthropic={anthropic_cost!r} gemini={gemini_cost!r}"
    )


def test_unknown_model_falls_back_to_default_rate(restore_settings):
    """A model not in PRICING[provider] should price against
    PRICING[provider]['default'], not crash."""
    settings.model_validation = "anthropic/claude-totally-made-up-2099"
    cost = estimate_stage_cost_usd("review")

    # Verify it matches the default rate for anthropic
    settings.model_validation = "anthropic/also-made-up-2099"
    cost2 = estimate_stage_cost_usd("review")

    assert cost > 0
    assert cost == pytest.approx(cost2, rel=1e-9)


def test_unknown_provider_falls_back_to_anthropic(restore_settings):
    """A provider prefix not in PRICING falls back to anthropic default."""
    settings.model_validation = "openai/gpt-4o"
    cost = estimate_stage_cost_usd("review")
    assert cost > 0  # Should use anthropic default rates, not crash


def test_baselines_cover_every_estimator_stage_kind():
    """STAGE_TOKEN_BASELINES must have an entry for every CostItem.kind
    the estimator emits — otherwise estimate_stage_cost_usd crashes
    with a KeyError mid-estimate."""
    expected = {
        "ic_extraction", "simple_extraction", "passive_pattern",
        "digikey_resolve", "review",
    }
    assert expected.issubset(STAGE_TOKEN_BASELINES.keys()), (
        f"missing baselines: {expected - set(STAGE_TOKEN_BASELINES.keys())}"
    )


def test_settings_stages_are_known_to_config(restore_settings):
    """The 'settings_stage' field of every baseline must be a key
    accepted by Settings.model_for_stage / provider_for_stage."""
    for stage, base in STAGE_TOKEN_BASELINES.items():
        s = str(base["settings_stage"])
        # Should not raise; should return non-empty strings for
        # provider+model.
        provider = settings.provider_for_stage(s)
        model = settings.model_for_stage(s)
        assert provider, f"empty provider for stage {stage!r} -> {s!r}"
        assert model, f"empty model for stage {stage!r} -> {s!r}"


def test_pricing_table_has_all_default_entries():
    """estimate_stage_cost_usd's safety-net fall-through assumes every
    provider has a 'default' row. Pin that contract."""
    for provider, table in PRICING.items():
        assert "default" in table, f"PRICING[{provider!r}] missing 'default'"
