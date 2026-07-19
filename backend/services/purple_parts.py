"""Purple Parts API client — LCSC code → MPN resolution.

Wraps the external `purple-parts` HTTP service (a read-only API over the
jlcparts/LCSC catalogue, deployed at the URL in `settings.purple_parts_url`).
Used by the BOM-parse stage to convert LCSC codes (e.g. "C12345") into
manufacturer part numbers before the DigiKey resolver runs.

The remote service is Cloud Run with IAM auth, so calls send a Google
identity token (audience = purple_parts_url) plus an X-API-Key header. In
Cloud Run the identity token is minted automatically via ADC + the
metadata server; locally `fetch_id_token` only works if
GOOGLE_APPLICATION_CREDENTIALS points at a service-account key file. On
local dev with user creds the helper logs a debug line and the call is
skipped (returns an empty result), which the caller treats as a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional
import httpx


from backend.config import settings

logger = logging.getLogger(__name__)

_LCSC_RE = re.compile(r"^C\d+$", re.IGNORECASE)

# Identity tokens are valid for ~1h; refresh ~10 min early.
_TOKEN_TTL_SECONDS = 50 * 60
_token_cache: dict[str, float | str] = {"token": "", "expires_at": 0.0}
_token_lock = asyncio.Lock()

# Conservative batch size — purple-parts accepts up to 500 per request.
_BATCH_SIZE = 400


def is_lcsc_code(value: str | None) -> bool:
    """Return True if `value` looks like an LCSC part number (e.g. C12345)."""
    if not value:
        return False
    return bool(_LCSC_RE.match(value.strip()))


async def _get_identity_token() -> str | None:
    """Mint a Google ID token for the purple-parts audience, cached.

    Returns None when credentials don't support identity-token minting
    (typical for local dev with `gcloud auth application-default login` user
    creds). Caller should treat None as "skip the purple-parts call."
    """
    now = time.time()
    cached = _token_cache.get("token", "")
    if cached and float(_token_cache.get("expires_at", 0.0)) > now:
        return str(cached)

    async with _token_lock:
        cached = _token_cache.get("token", "")
        if cached and float(_token_cache.get("expires_at", 0.0)) > now:
            return str(cached)

        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import id_token as gid_token
        except ImportError:
            logger.warning("google-auth not installed; purple-parts disabled")
            return None

        loop = asyncio.get_running_loop()
        try:
            token = await loop.run_in_executor(
                None,
                lambda: gid_token.fetch_id_token(Request(), settings.purple_parts_url),
            )
        except Exception as e:
            logger.debug(
                "purple-parts: identity-token mint failed (%s: %s) — "
                "expected for local user creds, skipping",
                type(e).__name__, e,
            )
            return None

        _token_cache["token"] = token
        _token_cache["expires_at"] = now + _TOKEN_TTL_SECONDS
        return token


def detect_lcsc_column(csv_bytes: bytes, mpn_col: str) -> bool:
    """Return True when every non-empty value in `mpn_col` matches `^C\\d+$`.

    Used by the upload endpoint to auto-detect when the user's chosen MPN
    column is actually an LCSC column (i.e. the user pasted LCSC ids into
    the MPN slot, or labeled their LCSC column as "Manufacturer Part Number").
    Column-level — a single non-LCSC entry disqualifies the column so that
    BOMs mixing real MPNs with LCSC ids aren't silently mangled.
    """
    import csv as csv_mod
    import io

    text = csv_bytes.decode("utf-8", errors="replace")
    reader = csv_mod.DictReader(io.StringIO(text))
    if not reader.fieldnames or mpn_col not in reader.fieldnames:
        return False

    seen_any = False
    for row in reader:
        val = (row.get(mpn_col) or "").strip()
        if not val:
            continue
        if not is_lcsc_code(val):
            return False
        seen_any = True
    return seen_any


async def resolve_lcsc_column_bytes(
    csv_bytes: bytes,
    *,
    mpn_col: str = "Manufacturer Part Number",
) -> tuple[bytes, int, dict[str, str], dict[str, dict]]:
    """Replace every value in `mpn_col` with the manufacturer part number
    resolved via purple-parts.

    Returns `(new_csv_bytes, rows_updated, lcsc_to_mpn_map, lcsc_payloads_map)`.
    The first map is keyed by LCSC id (e.g. "C12044") → resolved MPN string,
    so the caller can surface "C12044 → STM32F103C8T6" in the UI. The second
    map is keyed by the same LCSC id → the full purple-parts payload (mpn,
    manufacturer, package, description, category, subcategory, ...) so the
    caller can cache it on the project for the wizard's per-row resolve
    endpoint. Preserves column order, headers, and untouched cells. No-op
    when purple-parts isn't configured.
    """
    import csv as csv_mod
    import io

    if not settings.use_purple_parts:
        return csv_bytes, 0, {}, {}

    text = csv_bytes.decode("utf-8", errors="replace")
    reader = csv_mod.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    rows = list(reader)

    if not rows or mpn_col not in fieldnames:
        return csv_bytes, 0, {}, {}

    todo: list[tuple[int, str]] = []
    for i, row in enumerate(rows):
        code = (row.get(mpn_col) or "").strip()
        if is_lcsc_code(code):
            todo.append((i, code))

    if not todo:
        return csv_bytes, 0, {}, {}

    unique_codes = sorted({c for _, c in todo})
    resolved = await lookup_lcsc_batch(unique_codes)

    updated = 0
    lcsc_to_mpn: dict[str, str] = {}
    lcsc_payloads: dict[str, dict] = {}
    for i, code in todo:
        part = resolved.get(code)
        if part and part.get("mpn"):
            rows[i][mpn_col] = part["mpn"]
            lcsc_to_mpn[code] = part["mpn"]
            lcsc_payloads[code] = dict(part)
            updated += 1

    if updated == 0:
        return csv_bytes, 0, {}, {}

    out = io.StringIO()
    writer = csv_mod.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8"), updated, lcsc_to_mpn, lcsc_payloads


async def lookup_lcsc_batch(lcsc_codes: list[str]) -> dict[str, Optional[dict]]:
    """Batch LCSC → MPN lookup.

    Returns `{lcsc_code: part_dict_or_None}` for every code in input. Misses,
    invalid codes, and (after warning) total failures all return None values
    so the caller can treat the result as a uniform per-code map. The pipeline
    never aborts on a purple-parts miss; the row simply stays unresolved and
    the existing DigiKey/Haiku paths handle it.

    Part dict shape: {lcsc, mpn, manufacturer, package, description, stock,
    basic, preferred}.
    """
    if not settings.use_purple_parts:
        return {c: None for c in lcsc_codes}

    codes = [c for c in (raw.strip() for raw in lcsc_codes) if c]
    if not codes:
        return {}

    token = await _get_identity_token()
    if token is None:
        logger.info("purple-parts: no identity token, skipping batch of %d", len(codes))
        return {c: None for c in codes}

    base_url = settings.purple_parts_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-API-Key": settings.purple_parts_api_key,
        "Content-Type": "application/json",
    }

    results: dict[str, Optional[dict]] = {c: None for c in codes}

    async with httpx.AsyncClient(timeout=15) as client:
        for i in range(0, len(codes), _BATCH_SIZE):
            chunk = codes[i:i + _BATCH_SIZE]
            try:
                resp = await client.post(
                    f"{base_url}/v1/parts/by-lcsc/batch",
                    headers=headers,
                    json={"ids": chunk},
                )
                resp.raise_for_status()
                body = resp.json()
            except httpx.HTTPStatusError as e:
                logger.warning(
                    "purple-parts: batch call failed %s for chunk of %d",
                    e.response.status_code, len(chunk),
                )
                continue
            except Exception as e:
                logger.warning("purple-parts: batch call error: %s", e)
                continue

            for code, part in (body.get("results") or {}).items():
                results[code] = part

    return results


def _norm_mpn(value: str | None) -> str:
    """Normalize an MPN for comparison: drop whitespace, uppercase."""
    return "".join((value or "").split()).upper()


def _pick_exact(query: str, candidates: list[dict]) -> Optional[dict]:
    """Return the candidate whose ``mpn`` exactly matches ``query``.

    Match is case- and whitespace-insensitive. purple-parts' ``by-mpn``
    endpoint returns exact matches first and then prefix matches, but we
    re-check rather than trust ordering — a prefix-only hit (e.g. a series
    family for a more specific MPN) must be treated as a miss so it can't
    pollute the shared passive library. Mirrors the exact-MPN discipline of
    ``services.digikey._find_product``.
    """
    q = _norm_mpn(query)
    for part in candidates:
        if part and _norm_mpn(part.get("mpn")) == q:
            return part
    return None


async def lookup_mpn_batch(mpns: list[str]) -> dict[str, Optional[dict]]:
    """Reverse lookup: manufacturer part number → LCSC catalogue record.

    Fans the unique MPNs out to purple-parts' batch endpoint
    (``POST /v1/parts/by-mpn/batch``) in chunks of ``_BATCH_SIZE`` — one indexed
    query per chunk instead of a GET per MPN, which is what stalled huge-BOM
    uploads when the by-mpn query was seq-scanning. Returns
    ``{mpn: part_dict_or_None}`` keyed by the *input* MPN string.

    The endpoint is exact-match only, and we additionally run :func:`_pick_exact`
    over each MPN's candidate list (case/whitespace-insensitive) to keep the
    exact-MPN discipline — a prefix / family hit can carry the wrong
    voltage / dielectric / package and must never reach the shared
    ``library/passives``. Misses, missing creds (no identity token), and per-chunk
    failures all come back as ``None`` so the caller can treat the map uniformly.
    No-op (all ``None``) when purple-parts isn't configured.

    Part dict shape matches :func:`lookup_lcsc_batch`: {lcsc, mpn, manufacturer,
    package, description, category, subcategory, stock, basic, preferred}.
    """
    if not settings.use_purple_parts:
        return {m: None for m in mpns}

    # Preserve input keys but query each unique, non-empty MPN once.
    names = list(dict.fromkeys(m.strip() for m in mpns if m and m.strip()))
    if not names:
        return {}

    token = await _get_identity_token()
    if token is None:
        logger.info("purple-parts: no identity token, skipping by-mpn batch of %d", len(names))
        return {m: None for m in names}

    base_url = settings.purple_parts_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-API-Key": settings.purple_parts_api_key,
        "Content-Type": "application/json",
    }

    results: dict[str, Optional[dict]] = {m: None for m in names}

    async with httpx.AsyncClient(timeout=15) as client:
        for i in range(0, len(names), _BATCH_SIZE):
            chunk = names[i:i + _BATCH_SIZE]
            try:
                resp = await client.post(
                    f"{base_url}/v1/parts/by-mpn/batch",
                    headers=headers,
                    json={"mpns": chunk},
                )
                resp.raise_for_status()
                body = resp.json()
            except httpx.HTTPStatusError as e:
                logger.warning(
                    "purple-parts: by-mpn batch call failed %s for chunk of %d",
                    e.response.status_code, len(chunk),
                )
                continue
            except Exception as e:
                msg = str(e) or type(e).__name__
                logger.warning("purple-parts: by-mpn batch call error: %s", msg)
                continue

            # Each MPN maps to a candidate list; keep only the exact match.
            for mpn, candidates in (body.get("results") or {}).items():
                results[mpn] = _pick_exact(mpn, candidates or [])

    return results
