"""Bank detection from the PDF document title.

Slice renders its statements from HTML and prints no bank branding on the
page. Detection used to work only because the saved file name held the word
"slice". A caller that parses an email attachment from a temporary file has
no such name, so the statement fell through to the generic parser and
yielded no transactions. The PDF `/Title` carries the brand and must be
enough on its own.
"""

from cc_parser.parsers.factory import detect_bank


def _raw(title: str = "", file_name: str = "tmp1234.pdf", header: str = "") -> dict:
    """Build a minimal extraction payload for detection only."""
    return {
        "file": file_name,
        "metadata": {"pypdf": {"/Title": title}},
        "pages": [{"page_number": 1, "text": header}],
    }


def test_title_detects_slice_without_filename_or_header():
    """A slice statement is detected from `/Title` alone."""
    raw = _raw(
        title="slice credit card statement",
        header="CARDHOLDER'S\nCREDIT CARD STATEMENT\nXXXX XXXX XXXX 0002\nDUE ON 5 APR",
    )
    assert detect_bank(raw) == "slice"


def test_generic_title_does_not_claim_a_bank():
    """An untitled or unbranded title leaves detection to header and name."""
    assert detect_bank(_raw(title="Statement")) == "generic"
    assert detect_bank({"file": "tmp1234.pdf", "pages": []}) == "generic"
