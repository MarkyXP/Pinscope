import io
import logging
from pathlib import Path

import litellm

log = logging.getLogger(__name__)


def upload_azure_file(p: Path) -> str:
    """Upload a PDF to Azure via LiteLLM and return its file ID."""
    file_obj = litellm.create_file(
        file=(p.name, io.BytesIO(p.read_bytes()), "application/pdf"),
        purpose="assistants",
        custom_llm_provider="azure",
    )
    return file_obj.id
