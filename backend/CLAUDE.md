# Pinscope Backend

FastAPI application providing async pipeline orchestration, project storage, and SSE progress streaming. Wraps the `pinscopex/` core library — calls existing functions with local paths, adds no domain logic of its own.

## Running

```bash
# From project root
python3 -m uvicorn backend.main:app --reload    # localhost:8000
```

Config reads from `.env` at project root (see `config.py`). Key settings: `LLM_API_KEY`, `LLM_PROVIDER` (default `anthropic`), per-stage model overrides (`model_pintable`, `model_pattern`, `model_specs`, `model_validation`, `model_auto_resolve`), `CORS_ORIGINS`, `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET`, `DIGIKEY_ENVIRONMENT`.

For local mode, leave `GCS_BUCKET` empty — uses `LocalStorageBackend` (`data/` directory) and no auth (user_id defaults to `"local"`, admin access granted).

## Architecture

```
backend/
├── main.py              # App entry, lifespan hook, CORS, auth middleware, router includes
├── config.py            # Pydantic Settings from .env
├── _version.py          # Reads app version from frontend/content/changelog.md (single source of truth)
├── Dockerfile           # Python 3.12-slim, copies taxonomy/ + changelog.md for runtime
├── pinscopex/           # Core library (models, parsers, graph, validator, taxonomy, derating)
│   ├── utils.py         # Shared utilities: safe_mpn(), natural_sort_key()
│   └── resolve_passives.py  # Passive MPN pattern matching + value decoders (R/C/L)
├── middleware/
│   └── auth.py          # JWT verification via JWKS (enabled when CLERK_JWKS_URL is set; off in OSS mode)
├── routers/
│   ├── deps.py          # Shared router dependencies: get_storage(), get_user_id(), resolve_or_404()
│   ├── projects.py      # CRUD + file upload + library check + collaborators + DigiKey endpoints
│   ├── pipeline.py      # Start, cancel, estimate, resume, restart, regen, SSE, status
│   ├── reports.py       # Report, comments, graph, datasheet, API logs, BOM, derating
│   ├── admin.py         # Admin-only: components, users, usage, projects, runs, settings
│   ├── feedback.py      # User feedback tickets
│   └── contact.py       # Contact form (email relay; inert unless email is configured)
└── services/
    ├── storage.py            # StorageBackend protocol + LocalStorageBackend
    ├── storage_gcs.py        # GCSStorageBackend (optional, Google Cloud Storage)
    ├── projects.py           # Project CRUD + library ops via StorageBackend
    ├── pipeline.py           # Multi-stage orchestrator + EventBroker + PipelineWorkspace
    ├── extraction.py         # Async LiteLLM calls (pintable, patterns, specs, auto-resolve, value fallback) + local skills + page trimming
    ├── validation.py         # Async agentic validation wrapper with per-IC error isolation
    ├── normalize_findings.py # Post-review per-IC normalize pass (downgrade-only)
    ├── dedupe_findings.py    # Cross-IC finding dedup
    ├── billing_hook.py       # Open-core billing seam (NullBilling here — pipelines run free)
    ├── digikey.py            # DigiKey API v4 — OAuth2, datasheet fetch, parameter fetch (exact MPN only)
    ├── purple_parts.py       # Optional external LCSC→MPN resolver (env-gated; fails soft when unset)
    ├── api_logs.py           # API call logging, cost calculation per pipeline run
    ├── cost_estimator.py     # Pre-flight pipeline estimate (read-only)
    ├── datasheet_store.py    # Content-addressed PDF storage: blobs/{md5}.pdf + refs/{safe_mpn}.json
    ├── admin_settings.py     # Admin-only settings (e.g., min_model_version threshold)
    ├── job_runner.py         # Optional Cloud Run job trigger (in-process asyncio locally)
    └── email.py              # Email notifications (optional, env-gated)
```

## Storage Abstraction

All file I/O goes through `StorageBackend` (protocol in `services/storage.py`):
- **LocalStorageBackend**: Maps keys to `data/` directory. Default.
- **GCSStorageBackend**: Uses `google-cloud-storage` SDK. Used when `GCS_BUCKET` is set.

Storage keys follow GCS-style paths: `users/{user_id}/projects/{id}/uploads/bom.csv`

The `pinscopex/` core library is **unaware of storage** — it operates on local paths. During pipeline execution, `PipelineWorkspace` downloads files to a temp dir, runs `pinscopex/` functions locally, then uploads results back.

## Project Storage

```
# data/ directory (or GCS bucket)
users/{user_id}/projects/{id}/
├── project.json                    # ProjectMeta (name, status, timestamps, user_id, total_cost_usd)
├── uploads/
│   ├── bom.csv
│   ├── netlist.asc          # OR netlist.edn for EDIF uploads
│   └── datasheets/*.pdf
├── extracted/                      # Per-project IC extractions
├── patterns/                       # Per-project passive patterns
├── models/                         # Per-project resolved specs
├── design_graph.json
├── bom_summary.json                # BOM summary table (collated from design graph)
├── derating.json                   # Capacitor voltage derating table
├── report.json                     # Findings + comments
└── api_logs.jsonl                  # Claude API call log (token counts, cost, timing)

library/                            # Shared across projects
├── extracted/                      # Shared IC extractions
├── patterns/                       # Shared passive patterns
├── models/                         # Shared component specs (discrete, connectors, etc.)
├── passives/                       # DigiKey-resolved passive specs (exact-MPN only)
└── datasheets/
    ├── blobs/{md5}.pdf             # Content-addressed PDF blobs (deduped)
    └── refs/{safe_mpn}.json        # MPN → blob pointer ({hash, blob_key})

taxonomy/                           # Component taxonomy (repo taxonomy/ dir in local mode)
```

Library lookups happen first — if an MPN was already extracted, it's reused without re-calling the API.

## Pipeline Stages

The pipeline runs async via `asyncio.create_task()`. Progress emitted as SSE events via `EventBroker` (async queue per subscriber). `PipelineWorkspace` handles download/upload. Pipelines can be cancelled mid-run via `POST /api/pipeline/{id}/cancel`.

1. **Parse BOM** — Read uploaded CSV/XLSX (uses stored column mappings from upload; XLSX converted to CSV via openpyxl)
2. **Extract IC Pintables** — Async LLM calls for pintable per IC MPN (datasheets keyword-trimmed via `pypdf`). Cache-miss MPNs are extracted **concurrently**, up to `IC_CONCURRENCY` (default 6) in flight at once.
2.5. **Extract Simple Components** — Specs extraction for discrete/simple components with datasheets
3. **Extract Passives** — Pattern-based extraction per MPN group, then a specs fallback per MPN.
3.5. **DigiKey Auto-Resolve (exact MPN)** — Fallback for unresolved passives; parameters mapped to taxonomy specs via Haiku. Requires exact MPN match so the shared `library/passives/` stays clean.
3.6. **Value Fallback (R/C/L/FB only)** — When DigiKey misses, parse the BOM `Value` string via Haiku into typed passive specs. Per-project only; never written to the shared library.
4. **Build Graph** — Call `pinscopex.graph.build_graph()` with local temp paths
5. **BOM Summary** — Collate components from design graph (no AI)
6. **Derating Table** — Capacitor voltage derating computation (no AI)
7. **Direct Datasheet Review** — Per-IC (isolated): LLM reads the datasheet PDF + circuit neighborhood from the graph, compares to reference application circuit, and submits findings via graph query tools. ICs are reviewed **concurrently**, up to `IC_CONCURRENCY` in flight at once.

**Concurrency knob** — `IC_CONCURRENCY` (`config.py: ic_concurrency`, default 6) governs parallelism for stage 2 (IC extraction), stage 3.5 (passive specs fallback), and stage 7 (review). Set `IC_CONCURRENCY=1` for fully sequential behavior.

## Key Patterns

- **StorageBackend protocol** — all file I/O is abstracted; swap local/GCS via `GCS_BUCKET` env var
- **PipelineWorkspace** — downloads to temp dir, runs pinscopex locally, uploads results
- **BillingHook seam (open-core)** — core code reaches billing exclusively through `services/billing_hook.py:get_billing()`. In this repo that's `NullBilling`: every pipeline runs free and no billing routes are mounted. Never import billing modules directly from core code — go through the hook.
- **Auth middleware** — JWT verification via a JWKS endpoint; disabled when `CLERK_JWKS_URL` is empty (local mode: `user_id="local"`, `is_admin()` returns True)
- **LiteLLM** for all LLM calls — extraction and validation (any provider via `LLM_PROVIDER`)
- **Local skills** — extraction uses skills from `skills/` directory (SKILL.md + schema.json + validate.py); loaded at runtime
- **Forced tool calls** for extraction — structured output via `tool_choice`
- **SSE via sse-starlette** — `EventBroker` manages per-project async queues with history replay
- **API call logging** — `ApiLogger` in `services/api_logs.py` collects per-call metadata; `CallMeta` returned from extraction functions
- **Traceability IDs** — findings get IDs (format: `{designator}-{001}`) for audit trails
- **Taxonomy specs schemas** — auto-generated via Claude per type/subtype; extraction discards parameters not in schema (`extra_specs`)
- **Shared router deps** — `routers/deps.py` centralizes `get_storage()`, `get_user_id()`, `resolve_or_404()` across all routers
- **DigiKey OAuth2** — Token caching in `services/digikey.py`; `_find_product` requires exact MPN (no silent first-match fallback)
- **Version stamping** — `backend/_version.py` reads the latest `##` heading from `frontend/content/changelog.md` and exports `PINSCOPE_VERSION`; stamped onto `ProjectMeta.pinscope_version` at `/start`
- **Datasheet page trimming** — `_select_pages()` in `extraction.py` keyword-trims large PDFs to reduce token costs
- **Content-addressed datasheets** — `datasheet_store.py` writes PDFs to `library/datasheets/blobs/{md5}.pdf` and maps MPNs via refs
- **Passive value decoders** — `pinscopex/resolve_passives.py` decodes EIA-198, R-notation, letter-decimal, EIA3/EIA4 for R/C/L values
- **Collaborator access** — `resolve_or_404()` grants access to both owner and collaborators
- **Per-IC review isolation** — In `services/validation.py`, each IC review is wrapped so a single bad payload is captured as a skipped component rather than aborting the run

## Guidelines

- Keep all LLM interaction in `services/extraction.py` and `services/validation.py`; logging in `services/api_logs.py`
- Keep all storage operations in `services/projects.py` (uses `StorageBackend`)
- Routers are thin — validate input, call service, return response
- Thread `user_id` from `request.state` through to all service calls
- Don't import from `backend/` in `pinscopex/` — dependency flows one way
- CORS is configured for `localhost:3000` by default; override with `CORS_ORIGINS` env var
