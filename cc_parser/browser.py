"""Browser-side API for Pyodide environments.

Provides a single entrypoint that accepts raw PDF bytes and returns
a parsed statement as a plain dict.  Does NOT import cli.py or
extractor.py (which depend on fitz, typer, rich, getpass).
"""

import io
import sys
import types
from pathlib import PurePosixPath
from typing import Any

# Safety stub: pdfplumber.display imports pypdfium2 at module level.
# The import only triggers if page.to_image() is called (which we never do),
# but we stub it to guard against future pdfplumber internal changes.
if "pypdfium2" not in sys.modules:
    sys.modules["pypdfium2"] = types.ModuleType("pypdfium2")

import pdfplumber  # noqa: E402
from pypdf import PdfReader, PdfWriter  # noqa: E402

from cc_parser.parsers.factory import BankChoice, detect_bank, get_parser  # noqa: E402


def _extract_raw(
    pdf_bytes: bytes, filename: str, password: str | None = None
) -> dict[str, Any]:
    """Extract raw PDF structure using pdfplumber + pypdf only.

    This is a browser-specific replacement for extractor.extract_raw_pdf
    that avoids the fitz (PyMuPDF) dependency.  It produces the same
    raw_data shape that all parsers expect.

    Args:
        pdf_bytes: Raw PDF file content.
        filename: Original filename (used for bank detection).
        password: Password for encrypted PDFs, or None.

    Returns:
        Raw extraction dict consumed by parser modules.

    Raises:
        ValueError: For encryption/password errors.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    is_encrypted = bool(reader.is_encrypted)

    if is_encrypted:
        if not password:
            raise ValueError("PDF is encrypted. Password is required.")
        if reader.decrypt(password) == 0:
            raise ValueError("Failed to decrypt PDF. Check the password.")
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        pdf_bytes = buf.getvalue()

    raw_meta = reader.metadata or {}
    pypdf_metadata = {
        str(k): ("" if v is None else str(v)) for k, v in raw_meta.items()
    }

    # Strip both Unix and Windows path separators for basename safety.
    safe_name = PurePosixPath(filename.replace("\\", "/")).name or filename

    document: dict[str, Any] = {
        "file": safe_name,
        "source": "pdfplumber+pypdf",
        "metadata": {"pypdf": pypdf_metadata},
        "encryption": {
            "is_encrypted": is_encrypted,
            "was_decrypted": is_encrypted,
        },
        "pages": [],
    }

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        document["page_count"] = len(pdf.pages)
        for idx, page in enumerate(pdf.pages):
            document["pages"].append(
                {
                    "page_number": idx + 1,
                    "width": page.width,
                    "height": page.height,
                    "text": page.extract_text() or "",
                    "words": page.extract_words() or [],
                    "tables": page.extract_tables() or [],
                }
            )

    return document


def parse_pdf(
    pdf_bytes: bytes,
    filename: str = "statement.pdf",
    password: str | None = None,
    bank: str = "auto",
) -> dict[str, Any]:
    """Parse a credit card statement PDF and return structured output.

    This is the main entrypoint called from the Pyodide web worker.

    Args:
        pdf_bytes: Raw PDF file content.
        filename: Original filename (used for bank detection and output).
        password: Password for encrypted PDFs, or None.
        bank: Parser profile ("auto" or explicit bank name).

    Returns:
        ParsedStatement as a plain dict (via model_dump()).

    Raises:
        ValueError: For encryption/password errors.
    """
    raw_data = _extract_raw(pdf_bytes, filename, password)
    parser = get_parser(bank, raw_data)
    parsed = parser.parse(raw_data)
    result = parsed.model_dump()
    result["bank_detected"] = detect_bank(raw_data)
    result["bank_parser"] = parser.bank
    return result


def list_banks() -> list[str]:
    """Return available bank choices for the UI dropdown."""
    from typing import get_args

    # PEP 695 `type` aliases expose the underlying type via __value__
    target = getattr(BankChoice, "__value__", BankChoice)
    return list(get_args(target))
