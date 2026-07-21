"""Backend configuration via environment variables."""

import importlib.util
import os
from datetime import datetime
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings

# Resolve paths relative to the project root (one level up from backend/)
_BACKEND_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _BACKEND_DIR.parent

# Skills directory — skills are now local (SKILL.md + schema.json + validate.py)
_SKILLS_DIR = _PROJECT_ROOT / "skills"


class Settings(BaseSettings):
    # API key — kept for backward compat. LiteLLM reads the key from the
    # env var that matches the model provider (e.g. ANTHROPIC_API_KEY for
    # "anthropic/..." models, OPENAI_API_KEY for "openai/..." models).
    anthropic_api_key: str = ""

    # Default model in LiteLLM format (e.g. "anthropic/claude-sonnet-4-6").
    # Per-stage overrides fall back to this value.
    default_model: str = "anthropic/claude-sonnet-4-6"

    # Per-stage model overrides (LiteLLM format — e.g. "openai/gpt-4o").
    # Fall back to default_model if empty.
    model_pintable: str = ""
    model_pattern: str = ""
    model_specs: str = ""
    model_validation: str = "anthropic/claude-sonnet-4-6"
    model_auto_resolve: str = "anthropic/claude-haiku-4-5-20251001"
    model_normalize: str = "anthropic/claude-sonnet-4-6"

    # Provider routing — provider_default is the global default; per-stage
    # overrides win when non-empty. Kept for backward compat; with LiteLLM
    # the provider is determined by the model prefix, so this field is
    # mostly cosmetic.
    provider_default: str = "litellm"
    provider_pintable: str = ""
    provider_pattern: str = ""
    provider_specs: str = ""
    provider_validation: str = ""
    provider_auto_resolve: str = ""
    provider_normalize: str = ""

    # Per-stage fallback model — used if the primary stage call raises.
    # Leave empty to disable fallback for that stage. If
    # fallback_model_<stage> is set but empty, the fallback uses
    # default_model.
    fallback_model_pintable: str = ""
    fallback_model_pattern: str = ""
    fallback_model_specs: str = ""
    fallback_model_validation: str = ""
    fallback_model_auto_resolve: str = ""
    fallback_model_normalize: str = ""

    # PDF → image render DPI (used by litellm_provider when a model doesn't
    # support native PDF input but does support vision).
    pdf_render_dpi: int = 150

    # Max parallel IC agents — the single knob controlling concurrency for
    # BOTH the IC pintable extraction stage and the direct datasheet review
    # stage. Change this one number (or the IC_CONCURRENCY env var) to scale
    # how many ICs are processed in parallel.
    ic_concurrency: int = 6

    # Per-IC normalize pass — dedup findings sharing a root cause and
    # re-grade severity against a fixed rubric. Runs after submit_review.
    normalize_findings_enabled: bool = True

    # Cross-IC dedup pass — collapse one physical interface defect reported
    # from both ICs (e.g. a 5V-into-3V3 net flagged once per endpoint) into a
    # single finding. Runs once after all per-IC reviews complete.
    cross_ic_dedup_enabled: bool = True

    # Paths (relative to project root, used by LocalStorageBackend)
    data_dir: Path = _PROJECT_ROOT / "data"
    taxonomy_dir: Path = _PROJECT_ROOT / "taxonomy"

    # GCS (if set, use GCSStorageBackend; otherwise LocalStorageBackend)
    gcs_bucket: str = ""

    # Clerk authentication
    clerk_secret_key: str = ""
    clerk_publishable_key: str = ""
    clerk_jwks_url: str = ""

    # DigiKey API (optional — enables auto-fetch datasheets)
    digikey_client_id: str = ""
    digikey_client_secret: str = ""
    digikey_environment: str = "production"
    digikey_locale_site: str = "US"
    digikey_locale_language: str = "en"
    digikey_locale_currency: str = "USD"

    # Purple Parts API (optional — converts LCSC codes to MPNs before DigiKey)
    purple_parts_url: str = ""
    purple_parts_api_key: str = ""

    # Email notifications (Gmail API via service account)
    email_sender: str = ""
    email_frontend_url: str = ""
    email_admin_notify: str = ""  # fixed recipient for pipeline-started alerts
    contact_recipient: str = ""  # where /api/contact submissions are delivered

    # Stripe billing (pay-as-you-go only — no subscription prices needed)
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    # Open-core: master switch for the credits/Stripe billing system.
    # True = credit gating + charges + billing/credits routers.
    # False = OSS/self-host mode: pipelines run free, billing routes unmounted.
    # Defaults to whether the private billing modules exist in this checkout
    # (present in the cloud/gateway repo, absent in the open-source core), so
    # a bare core checkout runs free with no configuration. An explicit
    # BILLING_ENABLED env var always wins.
    billing_enabled: bool = Field(
        default_factory=lambda: importlib.util.find_spec(
            "backend.services.stripe_billing"
        )
        is not None
    )

    # Onboarding survey (Google Sheet)
    survey_sheet_id: str = ""

    # CORS
    cors_origins: list[str] = ["http://localhost:3000"]

    # Cloud Run Job worker (pipeline runner)
    pipeline_worker_job_name: str = "pinscopex-pipeline-worker"
    pipeline_worker_region: str = "us-central1"
    pipeline_worker_project: str = ""  # GCP project id; defaults to GOOGLE_CLOUD_PROJECT or metadata
    pipeline_worker_timeout_seconds: int = 3600

    # Sweeper: a "running" project is considered stale if its last update
    # timestamp is older than this and the worker execution is in a
    # terminal Cloud Run state (or the executor isn't reachable).
    pipeline_sweeper_stale_seconds: int = 60

    model_config = {
        "env_file": str(_BACKEND_DIR / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @property
    def app_version(self) -> str:
        """Return the app version, defaulting to DEBUG_yyyymmdd_hhmmss."""
        version = os.getenv("APP_VERSION", "").strip()
        if not version:
            version = f"DEBUG_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        return version

    @property
    def is_debug(self) -> bool:
        """True when APP_VERSION is not set (i.e. we're running in debug mode)."""
        return not os.getenv("APP_VERSION", "").strip()

    @property
    def use_stripe(self) -> bool:
        return bool(self.stripe_secret_key)

    @property
    def use_digikey(self) -> bool:
        return bool(self.digikey_client_id and self.digikey_client_secret)

    @property
    def use_purple_parts(self) -> bool:
        return bool(self.purple_parts_url and self.purple_parts_api_key)

    @property
    def use_gcs(self) -> bool:
        return bool(self.gcs_bucket)

    @property
    def use_auth(self) -> bool:
        return bool(self.clerk_secret_key and self.clerk_jwks_url)

    @property
    def use_email(self) -> bool:
        return bool(self.email_sender and self.email_frontend_url)

    def provider_for_stage(self, stage: str) -> str:
        """Return the LLM provider name for a pipeline stage.

        With LiteLLM this is always "litellm" — the model prefix determines
        the actual provider. Kept for backward compat with call sites that
        expect a provider name.
        """
        override = getattr(self, f"provider_{stage}", "")
        return override or self.provider_default

    def model_for_stage(self, stage: str) -> str:
        """Return the model for a pipeline stage (LiteLLM format).

        Falls back to model_<stage>, then default_model.
        """
        override = getattr(self, f"model_{stage}", "")
        return override or self.default_model

    def fallback_for_stage(self, stage: str) -> str | None:
        """Return the fallback model for the stage, or None if no fallback
        is configured. Used by call_with_fallback() to retry once with a
        different model when the primary call raises.
        """
        fb_model = getattr(self, f"fallback_model_{stage}", "")
        if not fb_model:
            fb_model = self.default_model
        return fb_model if fb_model else None

    def get_default_model_version(self) -> str:
        """Return the default model_version for new extractions."""
        return "1.0.0"

    def get_skill(self, name: str) -> tuple[str, str]:
        """Return (skill_name, skill_dir_path) for a local skill.

        Validates that the skill directory exists with the required files.
        Returns a 2-tuple for backward compat with existing call sites
        (which unpack as ``skill_id, version = settings.get_skill(...)``,
        and only use the first element for logging).
        """
        skill_dir = _SKILLS_DIR / name
        if not (skill_dir / "SKILL.md").exists():
            raise RuntimeError(
                f"Skill '{name}' not found at {skill_dir}. "
                f"Ensure skills/{name}/SKILL.md exists."
            )
        return name, str(skill_dir)


settings = Settings()
