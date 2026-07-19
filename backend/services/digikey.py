"""DigiKey API integration — fetch datasheet PDFs and product parameters by MPN.

Uses DigiKey Product Information API v4 with OAuth2 client credentials.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
import traceback

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OAuth2 token cache
# ---------------------------------------------------------------------------

_token_cache: dict[str, str | float] = {"access_token": "", "expires_at": 0.0}

_BASE_URLS = {
    "production": "https://api.digikey.com",
    "sandbox": "https://sandbox-api.digikey.com",
}


async def _get_access_token() -> str:
    """Get a DigiKey OAuth2 access token, refreshing if expired."""
    now = time.time()
    if _token_cache["access_token"] and float(_token_cache["expires_at"]) > now + 60:
        return str(_token_cache["access_token"])

    base = _BASE_URLS.get(settings.digikey_environment, _BASE_URLS["production"])
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base}/v1/oauth2/token",
            data={
                "client_id": settings.digikey_client_id,
                "client_secret": settings.digikey_client_secret,
                "grant_type": "client_credentials",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 3600)
    logger.info("DigiKey OAuth token refreshed (expires in %ds)", data.get("expires_in", 3600))
    return str(_token_cache["access_token"])


# ---------------------------------------------------------------------------
# Product search
# ---------------------------------------------------------------------------


def _get_mpn(product: dict) -> str:
    return product.get("ManufacturerProductNumber") or product.get("ManufacturerPartNumber") or ""


def _get_ds_url(product: dict) -> str:
    url = product.get("DatasheetUrl") or product.get("PrimaryDatasheet") or ""
    # DigiKey sometimes returns protocol-relative URLs
    if url.startswith("//"):
        url = "https:" + url
    return url


async def _keyword_search(mpn: str) -> list[dict]:
    """Run a DigiKey keyword search and return the raw products list."""
    base = _BASE_URLS.get(settings.digikey_environment, _BASE_URLS["production"])
    token = await _get_access_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "X-DIGIKEY-Client-Id": settings.digikey_client_id,
        "X-DIGIKEY-Locale-Site": settings.digikey_locale_site,
        "X-DIGIKEY-Locale-Language": settings.digikey_locale_language,
        "X-DIGIKEY-Locale-Currency": settings.digikey_locale_currency,
        "Content-Type": "application/json",
    }

    body = {
        "Keywords": mpn,
        "Limit": 5,
        "Offset": 0,
        "ExcludeMarketPlaceProducts": True,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base}/products/v4/search/keyword",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

    return data.get("Products") or data.get("products") or []


def _find_product(mpn: str, products: list[dict]) -> dict | None:
    """Find the product whose MPN exactly matches ``mpn`` (case/space-insensitive).

    Returns None when no result has a matching MPN. We intentionally do NOT
    fall back to ``products[0]`` — keyword-search hits without an MPN match
    are usually for a different part, and silently returning them has
    polluted the library with wrong specs for non-MPN tokens like ``10uF``.
    """
    if not products:
        return None

    mpn_upper = mpn.upper().replace(" ", "")
    for product in products:
        if _get_mpn(product).upper().replace(" ", "") == mpn_upper:
            return product
    return None


async def _search_mpn(mpn: str) -> str | None:
    """Search DigiKey for an MPN and return the primary datasheet URL, or None."""
    products = await _keyword_search(mpn)
    product = _find_product(mpn, products)
    if not product:
        return None
    url = _get_ds_url(product)
    return url or None


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
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0"
    }
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers=HEADERS)
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


class DatasheetFetchResult:
    """Result of a datasheet fetch attempt."""

    def __init__(
        self,
        mpn: str,
        pdf_bytes: bytes | None = None,
        error: str | None = None,
        url: str | None = None,
    ):
        self.mpn = mpn
        self.pdf_bytes = pdf_bytes
        self.error = error
        self.url = url  # DigiKey datasheet URL (present even when PDF download fails)

    @property
    def ok(self) -> bool:
        return self.pdf_bytes is not None


async def fetch_datasheet(mpn: str) -> DatasheetFetchResult:
    """Fetch a datasheet PDF for the given MPN from DigiKey.

    Returns a DatasheetFetchResult with either pdf_bytes or an error message.
    The `url` field is set whenever DigiKey returns a datasheet link, even if
    the PDF download itself fails.
    Never raises — all errors are captured in the result.
    """
    if not settings.use_digikey:
        return DatasheetFetchResult(mpn, error="DigiKey API not configured")

    try:
        url = await _search_mpn(mpn)
    except httpx.HTTPStatusError as e:
        logger.warning("DigiKey search failed for %s: %s", mpn, e)
        return DatasheetFetchResult(mpn, error=f"DigiKey search failed ({e.response.status_code})")
    except Exception as e:
        msg = str(e) or type(e).__name__
        logger.warning("DigiKey search error for %s: %s", mpn, msg)
        return DatasheetFetchResult(mpn, error=f"DigiKey search error: {msg}")

    if not url:
        return DatasheetFetchResult(mpn, error="No datasheet found on DigiKey")

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

    logger.info("Fetched datasheet for %s (%d KB)", mpn, len(pdf_bytes) // 1024)
    return DatasheetFetchResult(mpn, pdf_bytes=pdf_bytes, url=url)


# ---------------------------------------------------------------------------
# Product parameters
# ---------------------------------------------------------------------------


@dataclass
class ProductParams:
    """Structured product parameters from a DigiKey search result."""

    mpn: str
    parameters: list[dict[str, str]] = field(default_factory=list)  # [{"name": ..., "value": ...}]
    category: str = ""
    description: str = ""


class ParamsFetchResult:
    """Result of a product parameters fetch attempt."""

    def __init__(self, mpn: str, params: ProductParams | None = None, error: str | None = None):
        self.mpn = mpn
        self.params = params
        self.error = error

    @property
    def ok(self) -> bool:
        return self.params is not None


def _parse_product_params(mpn: str, product: dict) -> ProductParams:
    """Extract structured parameters from a DigiKey product dict."""
    raw_params = product.get("Parameters") or product.get("parameters") or []
    parameters = []
    for p in raw_params:
        name = p.get("ParameterText") or p.get("parameterText") or ""
        value = p.get("ValueText") or p.get("valueText") or ""
        if name and value and value != "-":
            parameters.append({"name": name, "value": value})

    # Category
    cat = product.get("Category") or product.get("category") or {}
    category = cat.get("Name") or cat.get("name") or ""

    # Description
    desc_obj = product.get("Description") or product.get("description") or {}
    if isinstance(desc_obj, str):
        description = desc_obj
    else:
        description = (
            desc_obj.get("ProductDescription")
            or desc_obj.get("productDescription")
            or desc_obj.get("DetailedDescription")
            or desc_obj.get("detailedDescription")
            or ""
        )

    return ProductParams(
        mpn=mpn,
        parameters=parameters,
        category=category,
        description=description,
    )


async def fetch_params(mpn: str) -> ParamsFetchResult:
    """Fetch DigiKey product parameters for the given MPN.

    Returns structured parameter data (no PDF download needed).
    Never raises — all errors are captured in the result.
    """
    if not settings.use_digikey:
        return ParamsFetchResult(mpn, error="DigiKey API not configured")

    try:
        products = await _keyword_search(mpn)
    except httpx.HTTPStatusError as e:
        logger.warning("DigiKey search failed for %s: %s", mpn, e)
        return ParamsFetchResult(mpn, error=f"DigiKey search failed ({e.response.status_code})")
    except Exception as e:
        msg = str(e) or type(e).__name__
        msg = traceback.format_exc()
        logger.warning("DigiKey search error for %s: %s", mpn, msg)
        return ParamsFetchResult(mpn, error=f"DigiKey search error: {msg}")

    product = _find_product(mpn, products)
    if not product:
        return ParamsFetchResult(mpn, error="No results found on DigiKey")

    params = _parse_product_params(mpn, product)
    if not params.parameters:
        return ParamsFetchResult(mpn, error="No parameters available on DigiKey")

    logger.info("Fetched %d params for %s (category: %s)", len(params.parameters), mpn, params.category)
    return ParamsFetchResult(mpn, params=params)
