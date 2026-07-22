"""JLCPCB parts catalogue integration — fetch datasheet PDFs and product parameters by MPN.

Uses JLCPCB's public JSON API endpoints (no authentication required).
Reuses DatasheetFetchResult, ProductParams, and ParamsFetchResult from the
DigiKey module for a uniform interface.
"""

from __future__ import annotations

import logging
import re
import traceback

import httpx

from backend.services.digikey import DatasheetFetchResult, ParamsFetchResult, ProductParams

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

_BASE_URL = "https://jlcpcb.com"

_SEARCH_PATH = "/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList/v2"
_DETAIL_PATH = "/api/overseas-core-platform/shoppingCart/smtGood/getComponentDetail"

_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
}


# ---------------------------------------------------------------------------
# Search helpers
# ---------------------------------------------------------------------------


async def _keyword_search(mpn: str, *, page_size: int = 5) -> list[dict]:
    """Run a JLCPCB keyword search and return the raw component list."""
    body = {
        "currentPage": 1,
        "pageSize": page_size,
        "searchType": 2,
        "keyword": mpn,
        "searchSource": "search",
        "componentBrandList": [],
        "componentSpecificationList": [],
        "componentAttributeList": [],
        "paramList": [],
    }

    async with httpx.AsyncClient(timeout=20, headers=_HEADERS) as client:
        resp = await client.post(f"{_BASE_URL}{_SEARCH_PATH}", json=body)
        resp.raise_for_status()
        data = resp.json()

    if not data.get("success"):
        return []

    page_info = data.get("data", {}).get("componentPageInfo", {})
    return page_info.get("list") or []


def _get_mpn(component: dict) -> str:
    return component.get("componentModelEn") or ""


def _find_component(mpn: str, components: list[dict]) -> dict | None:
    """Find the component whose MPN exactly matches (case/space-insensitive).

    Returns None when no result has a matching MPN — does NOT fall back to
    the first result to avoid returning wrong data for ambiguous searches.
    """
    if not components:
        return None

    mpn_upper = mpn.upper().replace(" ", "").replace("-", "")
    for comp in components:
        comp_mpn = _get_mpn(comp).upper().replace(" ", "").replace("-", "")
        if comp_mpn == mpn_upper:
            return comp
    return None


def _get_component_code(component: dict) -> str:
    """Extract the LCSC component code (e.g. 'C8734')."""
    return component.get("componentCode") or ""


def _get_datasheet_url(component: dict) -> str:
    """Get the signed OSS datasheet URL from search results."""
    url = component.get("dataManualFileAccessIdUrl") or ""
    if not url:
        # Fall back to the LCSC datasheet viewer URL
        url = component.get("dataManualUrl") or ""
    return url


# ---------------------------------------------------------------------------
# Detail fetch (for parameters/attributes)
# ---------------------------------------------------------------------------


async def _fetch_detail(component_code: str) -> dict | None:
    """Fetch the full component detail by LCSC code."""
    async with httpx.AsyncClient(timeout=20, headers=_HEADERS) as client:
        resp = await client.get(
            f"{_BASE_URL}{_DETAIL_PATH}",
            params={"componentCode": component_code},
        )
        resp.raise_for_status()
        data = resp.json()

    if not data.get("success"):
        return None

    return data.get("data")


# ---------------------------------------------------------------------------
# PDF download + validation
# ---------------------------------------------------------------------------

_PDF_MAGIC = b"%PDF-"
_MIN_PDF_SIZE = 5_000  # 5 KB — anything smaller is probably an error page


async def _download_pdf(url: str) -> bytes:
    """Download a PDF from a URL and validate it.

    Raises ValueError if the file isn't a valid PDF or is too small.
    Raises httpx.HTTPStatusError on 4xx/5xx responses.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
    }
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.content

    if not data.startswith(_PDF_MAGIC):
        raise ValueError("Downloaded file is not a valid PDF (bad magic bytes)")

    if len(data) < _MIN_PDF_SIZE:
        raise ValueError(f"PDF too small ({len(data)} bytes) — likely an error page")

    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_datasheet(mpn: str) -> DatasheetFetchResult:
    """Fetch a datasheet PDF for the given MPN from JLCPCB.

    Returns a DatasheetFetchResult with either pdf_bytes or an error message.
    The `url` field is set whenever JLCPCB returns a datasheet link, even if
    the PDF download itself fails.
    Never raises — all errors are captured in the result.
    """
    try:
        components = await _keyword_search(mpn)
    except httpx.HTTPStatusError as e:
        logger.warning("JLCPCB search failed for %s: %s", mpn, e)
        return DatasheetFetchResult(mpn, error=f"JLCPCB search failed ({e.response.status_code})")
    except Exception as e:
        msg = str(e) or type(e).__name__
        logger.warning("JLCPCB search error for %s: %s", mpn, msg)
        return DatasheetFetchResult(mpn, error=f"JLCPCB search error: {msg}")

    component = _find_component(mpn, components)
    if not component:
        return DatasheetFetchResult(mpn, error="No datasheet found on JLCPCB")

    url = _get_datasheet_url(component)
    if not url:
        return DatasheetFetchResult(mpn, error="No datasheet URL available on JLCPCB")

    try:
        pdf_bytes = await _download_pdf(url)
    except httpx.HTTPStatusError as e:
        logger.warning("Datasheet download blocked for %s (%s): %s", mpn, url, e)
        return DatasheetFetchResult(mpn, error=f"Download blocked ({e.response.status_code})", url=url)
    except ValueError as e:
        logger.warning("Invalid PDF for %s (%s): %s", mpn, url, e)
        return DatasheetFetchResult(mpn, error=str(e), url=url)
    except httpx.TimeoutException:
        logger.warning("Datasheet download timed out for %s (%s)", mpn, url)
        return DatasheetFetchResult(mpn, error="Download timed out", url=url)
    except Exception as e:
        msg = str(e) or type(e).__name__
        logger.warning("Datasheet download failed for %s (%s): %s", mpn, url, msg)
        return DatasheetFetchResult(mpn, error=f"Download failed: {msg}", url=url)

    logger.info("Fetched datasheet for %s from JLCPCB (%d KB)", mpn, len(pdf_bytes) // 1024)
    return DatasheetFetchResult(mpn, pdf_bytes=pdf_bytes, url=url)


async def fetch_params(mpn: str) -> ParamsFetchResult:
    """Fetch JLCPCB product parameters for the given MPN.

    Returns structured parameter data (no PDF download needed).
    Never raises — all errors are captured in the result.
    """
    try:
        components = await _keyword_search(mpn)
    except httpx.HTTPStatusError as e:
        logger.warning("JLCPCB search failed for %s: %s", mpn, e)
        return ParamsFetchResult(mpn, error=f"JLCPCB search failed ({e.response.status_code})")
    except Exception as e:
        msg = traceback.format_exc()
        logger.warning("JLCPCB search error for %s: %s", mpn, msg)
        return ParamsFetchResult(mpn, error=f"JLCPCB search error: {msg}")

    component = _find_component(mpn, components)
    if not component:
        return ParamsFetchResult(mpn, error="No results found on JLCPCB")

    component_code = _get_component_code(component)
    if not component_code:
        return ParamsFetchResult(mpn, error="No component code found on JLCPCB")

    # Fetch full detail (includes attributes/parameters)
    try:
        detail = await _fetch_detail(component_code)
    except httpx.HTTPStatusError as e:
        logger.warning("JLCPCB detail fetch failed for %s (%s): %s", mpn, component_code, e)
        return ParamsFetchResult(mpn, error=f"JLCPCB detail failed ({e.response.status_code})")
    except Exception as e:
        msg = str(e) or type(e).__name__
        logger.warning("JLCPCB detail error for %s (%s): %s", mpn, component_code, msg)
        return ParamsFetchResult(mpn, error=f"JLCPCB detail error: {msg}")

    if not detail:
        return ParamsFetchResult(mpn, error="No detail data returned from JLCPCB")

    params = _parse_detail_params(mpn, detail)
    if not params.parameters:
        return ParamsFetchResult(mpn, error="No parameters available on JLCPCB")

    logger.info(
        "Fetched %d params for %s from JLCPCB (category: %s)",
        len(params.parameters), mpn, params.category,
    )
    return ParamsFetchResult(mpn, params=params)


# ---------------------------------------------------------------------------
# Parameter parsing
# ---------------------------------------------------------------------------


def _parse_detail_params(mpn: str, detail: dict) -> ProductParams:
    """Extract structured parameters from a JLCPCB detail dict."""
    raw_attrs = detail.get("attributes") or []
    parameters = []
    for attr in raw_attrs:
        name = attr.get("attribute_name_en") or ""
        value = attr.get("attribute_value_name") or ""
        if name and value and value != "-":
            parameters.append({"name": name, "value": value})

    # Category: combine first + second sort names
    first_sort = detail.get("firstSortName") or ""
    second_sort = detail.get("secondSortName") or ""
    category = second_sort or first_sort

    # Description
    description = detail.get("describe") or ""

    return ProductParams(
        mpn=mpn,
        parameters=parameters,
        category=category,
        description=description,
    )
