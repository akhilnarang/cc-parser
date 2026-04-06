"""Verify browser.py extraction and parsing parity with CLI path."""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Import smoke test
# ---------------------------------------------------------------------------


def test_browser_module_imports():
    """browser.py must import without fitz, typer, or rich."""
    from cc_parser.browser import list_banks, parse_pdf  # noqa: F401


def test_list_banks_returns_all_options():
    from cc_parser.browser import list_banks

    banks = list_banks()
    assert "auto" in banks
    assert "icici" in banks
    assert "generic" in banks
    assert "bob" in banks
    assert len(banks) == 12


# ---------------------------------------------------------------------------
# _extract_raw shape tests
# ---------------------------------------------------------------------------

# Minimal valid PDF (1 blank page) generated inline.
_MINIMAL_PDF = (
    b"%PDF-1.0\n1 0 obj<</Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"xref\n0 4\n"
    b"0000000000 65535 f \n"
    b"0000000009 00000 n \n"
    b"0000000043 00000 n \n"
    b"0000000096 00000 n \n"
    b"trailer<</Root 1 0 R/Size 4>>\nstartxref\n173\n%%EOF"
)


def test_extract_raw_shape():
    """_extract_raw returns the dict shape parsers expect."""
    from cc_parser.browser import _extract_raw

    doc = _extract_raw(_MINIMAL_PDF, "test.pdf")

    # Top-level keys
    assert doc["file"] == "test.pdf"
    assert "pages" in doc
    assert "page_count" in doc
    assert isinstance(doc["pages"], list)
    assert doc["page_count"] >= 1

    # Page-level keys
    page = doc["pages"][0]
    assert "page_number" in page
    assert "text" in page
    assert "words" in page
    assert "width" in page
    assert "height" in page
    assert page["page_number"] == 1


def test_extract_raw_preserves_filename():
    """Bank detection relies on raw_data['file'] — verify it's forwarded."""
    from cc_parser.browser import _extract_raw

    doc = _extract_raw(_MINIMAL_PDF, "HDFC_March_2025.pdf")
    assert doc["file"] == "HDFC_March_2025.pdf"


def test_extract_raw_encrypted_no_password():
    """Encrypted PDF without password raises ValueError."""
    from cc_parser.browser import _extract_raw

    # We cannot easily create an encrypted PDF inline, so patch PdfReader.
    with patch("cc_parser.browser.PdfReader") as mock_reader_cls:
        instance = mock_reader_cls.return_value
        instance.is_encrypted = True
        with pytest.raises(ValueError, match="encrypted"):
            _extract_raw(b"fake", "test.pdf")


def test_extract_raw_encrypted_wrong_password():
    """Encrypted PDF with wrong password raises ValueError."""
    from cc_parser.browser import _extract_raw

    with patch("cc_parser.browser.PdfReader") as mock_reader_cls:
        instance = mock_reader_cls.return_value
        instance.is_encrypted = True
        instance.decrypt.return_value = 0
        with pytest.raises(ValueError, match="decrypt"):
            _extract_raw(b"fake", "test.pdf", password="wrong")


# ---------------------------------------------------------------------------
# parse_pdf contract tests
# ---------------------------------------------------------------------------


def test_parse_pdf_returns_expected_keys():
    """parse_pdf output must include the ParsedStatement fields."""
    from cc_parser.browser import parse_pdf

    result = parse_pdf(_MINIMAL_PDF, "test.pdf")
    # Core fields from ParsedStatement
    for key in (
        "file",
        "bank",
        "transactions",
        "payments_refunds",
        "possible_adjustment_pairs",
        "card_summaries",
        "overall_total",
        "reconciliation",
        "bank_detected",
        "bank_parser",
    ):
        assert key in result, f"Missing key: {key}"


def test_parse_pdf_file_is_basename_only():
    """Privacy contract: output must not contain directory paths."""
    from cc_parser.browser import parse_pdf

    result = parse_pdf(_MINIMAL_PDF, "statement.pdf")
    assert "/" not in result["file"]
    assert "\\" not in result["file"]


# ---------------------------------------------------------------------------
# Parity: browser vs CLI extractor (when both dependencies are available)
# ---------------------------------------------------------------------------


def _cli_extract_available() -> bool:
    """Check if the CLI extractor (fitz) is importable."""
    try:
        import fitz  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _cli_extract_available(), reason="fitz not available")
def test_browser_vs_cli_parity():
    """Browser parse_pdf and CLI path produce identical ParsedStatement.

    This test only runs when fitz (PyMuPDF) is available, i.e. in the
    full CLI environment, not in Pyodide.
    """
    from cc_parser.browser import parse_pdf as browser_parse
    from cc_parser.extractor import extract_raw_pdf
    from cc_parser.parsers.factory import get_parser

    pdf_bytes = _MINIMAL_PDF

    # Browser path
    browser_result = browser_parse(pdf_bytes, "test.pdf")

    # CLI path — write bytes to a temp file
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)

    try:
        raw = extract_raw_pdf(tmp_path, include_blocks=False, password=None)
        parser = get_parser("auto", raw)
        cli_parsed = parser.parse(raw).model_dump()
    finally:
        tmp_path.unlink()

    # Compare the fields that both paths produce.
    # Metadata / source fields intentionally differ.
    for key in (
        "bank",
        "name",
        "card_number",
        "due_date",
        "overall_total",
        "overall_reward_points",
        "transactions",
        "payments_refunds",
        "possible_adjustment_pairs",
    ):
        assert browser_result[key] == cli_parsed[key], (
            f"Parity mismatch on '{key}': browser={browser_result[key]!r} vs cli={cli_parsed[key]!r}"
        )
