"""Raw PDF extraction utilities.

This module is intentionally bank-agnostic. It only handles:
- encrypted PDF detection/decryption,
- metadata extraction,
- page-level text/word/table/block extraction.

Bank-specific parsing and normalization live under cc_parser/parsers/.
"""

import io
from pathlib import Path
from typing import Any

import fitz
import pdfplumber
from pypdf import PdfReader, PdfWriter


def metadata_from_pypdf(reader: PdfReader) -> dict[str, str]:
    """Return normalized PDF metadata as string key/value pairs.

    Args:
        reader: Initialized `PdfReader` instance.

    Returns:
        Metadata dictionary with string keys and string values.
    """
    raw_metadata = reader.metadata or {}
    metadata: dict[str, str] = {}
    for key, value in raw_metadata.items():
        metadata[str(key)] = "" if value is None else str(value)
    return metadata


def is_pdf_encrypted(pdf_path: Path) -> bool:
    """Check whether a PDF requires a password.

    Args:
        pdf_path: Path to input PDF.

    Returns:
        True if the PDF is encrypted, else False.
    """
    reader = PdfReader(str(pdf_path))
    return bool(reader.is_encrypted)


def prepare_pdf_bytes_if_encrypted(
    pdf_path: Path, password: str | None
) -> tuple[bytes | None, dict[str, bool], dict[str, str]]:
    """Decrypt encrypted PDFs and return in-memory bytes when needed.

    Args:
        pdf_path: Path to input PDF.
        password: Password for encrypted PDFs.

    Returns:
        Tuple of `(pdf_bytes, encryption_info, pypdf_metadata)` where
        `pdf_bytes` is None for non-encrypted inputs.

    Raises:
        ValueError: If PDF is encrypted and password is missing/invalid.
    """
    reader = PdfReader(str(pdf_path))
    is_encrypted = bool(reader.is_encrypted)

    if not is_encrypted:
        return (
            None,
            {"is_encrypted": False, "was_decrypted": False},
            metadata_from_pypdf(reader),
        )

    if not password:
        raise ValueError("PDF is encrypted. Password is required.")

    decrypt_result = reader.decrypt(password)
    if decrypt_result == 0:
        raise ValueError("Failed to decrypt PDF. Check the password.")

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    buffer = io.BytesIO()
    writer.write(buffer)
    decrypted_bytes = buffer.getvalue()

    return (
        decrypted_bytes,
        {"is_encrypted": True, "was_decrypted": True},
        metadata_from_pypdf(reader),
    )


def blocks_from_pymupdf(page: fitz.Page) -> list[dict[str, Any]]:
    """Extract text blocks with bounding boxes from a page.

    Args:
        page: PyMuPDF page object.

    Returns:
        List of block dictionaries containing coordinates and text.
    """
    blocks = []
    for block in page.get_text("blocks"):
        x0, y0, x1, y1, text, block_no, block_type = block
        blocks.append(
            {
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "text": text,
                "block_no": block_no,
                "block_type": block_type,
            }
        )
    return blocks


def extract_raw_pdf(
    pdf_path: Path, include_blocks: bool, password: str | None
) -> dict[str, Any]:
    """Extract raw document structure used by parser modules.

    Args:
        pdf_path: Path to input PDF.
        include_blocks: Whether to include PyMuPDF block extraction.
        password: Password used only if PDF is encrypted.

    Returns:
        Raw extraction dictionary with metadata and per-page content.
    """
    pdf_bytes, encryption_info, pypdf_metadata = prepare_pdf_bytes_if_encrypted(
        pdf_path, password
    )

    document: dict[str, Any] = {
        "file": pdf_path.name,
        "source": "pdfplumber+pymupdf+pypdf",
        "metadata": {"pypdf": pypdf_metadata},
        "encryption": encryption_info,
        "pages": [],
    }

    if pdf_bytes is None:
        plumber_context = pdfplumber.open(str(pdf_path))
        fitz_context = fitz.open(str(pdf_path))
    else:
        plumber_context = pdfplumber.open(io.BytesIO(pdf_bytes))
        fitz_context = fitz.open(stream=pdf_bytes, filetype="pdf")

    with plumber_context as plumber_doc, fitz_context as fitz_doc:
        document["page_count"] = len(plumber_doc.pages)
        document["metadata"]["pymupdf"] = fitz_doc.metadata

        for page_index, page in enumerate(plumber_doc.pages):
            page_payload: dict[str, Any] = {
                "page_number": page_index + 1,
                "width": page.width,
                "height": page.height,
                "text": page.extract_text() or "",
                "words": page.extract_words() or [],
                "tables": page.extract_tables() or [],
            }

            if include_blocks:
                fitz_page = fitz_doc.load_page(page_index)
                page_payload["blocks"] = blocks_from_pymupdf(fitz_page)

            document["pages"].append(page_payload)

    return document
