import io
import logging
from pathlib import Path

import litellm
import pymupdf

log = logging.getLogger(__name__)

MAX_PAGES_PER_CHUNK = 45


def _upload_one(data: bytes, filename: str) -> str:
    """Upload a single PDF blob and return its file ID."""
    file_obj = litellm.create_file(
        file=(filename, io.BytesIO(data), "application/pdf"),
        purpose="assistants",
        custom_llm_provider="azure",
    )
    return file_obj.id


def upload_azure_file(p: Path) -> list[str]:
    """Upload a PDF to Azure via LiteLLM and return file ID(s).

    If the PDF exceeds ``MAX_PAGES_PER_CHUNK`` pages it is split into
    consecutive chunks so each upload stays within Azure's per-file
    page limit. Returns a list of file IDs (one per chunk).
    """
    doc = pymupdf.open(str(p))
    page_count = len(doc)

    if page_count <= MAX_PAGES_PER_CHUNK:
        doc.close()
        return [_upload_one(p.read_bytes(), p.name)]

    log.info(
        "PDF '%s' has %d pages — splitting into %d-page chunks for Azure upload",
        p.name,
        page_count,
        MAX_PAGES_PER_CHUNK,
    )

    file_ids: list[str] = []
    stem = p.stem
    for start in range(0, page_count, MAX_PAGES_PER_CHUNK):
        end = min(start + MAX_PAGES_PER_CHUNK, page_count)
        chunk_doc = pymupdf.open()
        chunk_doc.insert_pdf(doc, from_page=start, to_page=end - 1)
        buf = chunk_doc.tobytes()
        chunk_doc.close()

        chunk_name = f"{stem}_p{start + 1}-{end}.pdf"
        file_ids.append(_upload_one(buf, chunk_name))

    doc.close()
    return file_ids
