"""Parser selection utilities.

`detect_bank` uses simple text/file-name heuristics.
`get_parser` returns the parser implementation for the selected bank.
"""

from typing import Any, Literal

from cc_parser.parsers.base import StatementParser
from cc_parser.parsers.generic import GenericParser
from cc_parser.parsers.hdfc import HdfcParser
from cc_parser.parsers.icici import IciciParser
from cc_parser.parsers.idfc import IdfcParser
from cc_parser.parsers.sbi import SbiParser

BankChoice = Literal["auto", "icici", "hdfc", "sbi", "idfc", "generic"]


def detect_bank(raw_data: dict[str, Any]) -> str:
    """Infer bank profile from first pages and input file name.

    Args:
        raw_data: Raw extraction payload.

    Returns:
        One of: `icici`, `hdfc`, `sbi`, `idfc`, or `generic`.
    """
    pages = raw_data.get("pages", [])
    page_texts = []
    if isinstance(pages, list):
        for page in pages[:3]:
            if isinstance(page, dict):
                page_texts.append(str(page.get("text", "")))
    joined = "\n".join(page_texts).upper()
    file_name = str(raw_data.get("file", "")).upper()

    if "ICICI" in joined or "ICICI" in file_name:
        return "icici"
    if "HDFC" in joined or "HDFC" in file_name:
        return "hdfc"
    if "SBI" in joined or "SBI" in file_name:
        return "sbi"
    if "IDFC" in joined or "IDFC" in file_name:
        return "idfc"
    return "generic"


def get_parser(choice: BankChoice, raw_data: dict[str, Any]) -> StatementParser:
    """Return parser instance for explicit or auto-detected bank choice.

    Args:
        choice: User-selected parser profile (`auto` or explicit bank).
        raw_data: Raw extraction payload used for auto-detection.

    Returns:
        Parser implementation instance.
    """
    effective = detect_bank(raw_data) if choice == "auto" else choice
    if effective == "icici":
        return IciciParser()
    if effective == "hdfc":
        return HdfcParser()
    if effective == "sbi":
        return SbiParser()
    if effective == "idfc":
        return IdfcParser()
    return GenericParser()
