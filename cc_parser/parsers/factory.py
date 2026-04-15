"""Parser selection utilities.

`detect_bank` keeps the existing heuristic ordering because several issuers
mention other banks in fine print. In particular: IndusInd must be checked
before ICICI (`ICICI Lombard` appears on some IndusInd statements), `AXIS
BANK` should be more specific than bare `AXIS`, and HSBC / Jupiter must be
detected before SBI.

`get_parser` keeps auto-detection in this module, but parser instantiation and
user-facing bank ordering now come from `registry.py`.
"""

from pathlib import Path
from typing import Any

from cc_parser.parsers.base import StatementParser
from cc_parser.parsers.registry import PARSER_REGISTRY, list_registered_banks

type BankChoice = str


def list_bank_choices() -> list[str]:
    """Return stable user-facing bank choices, including `auto`."""
    return list_registered_banks(include_auto=True)


def detect_bank(raw_data: dict[str, Any]) -> str:
    """Infer bank profile from first pages and input file name.

    Args:
        raw_data: Raw extraction payload.

    Returns:
        One of: `icici`, `hdfc`, `sbi`, `idfc`, `indusind`, `hsbc`, `axis`, `jupiter`, `slice`, `ssfb`, `bob`, `yesbank`, or `generic`.
    """
    pages = raw_data.get("pages", [])
    page_texts = []
    if isinstance(pages, list):
        for page in pages[:3]:
            if isinstance(page, dict):
                page_texts.append(str(page.get("text", "")))
    joined = "\n".join(page_texts).upper()
    # Use basename only for filename checks to avoid matching directory names
    # (e.g. /statements/hsbc/some_other_bank.pdf).
    file_name = Path(raw_data.get("file", "")).name.upper()

    # Check INDUSIND before ICICI because IndusInd statements mention
    # "ICICI Lombard" (insurance provider) in the fine print.
    if "INDUSIND" in joined or "INDUSIND" in file_name:
        return "indusind"
    # Check AXIS BANK before ICICI. Use "AXIS BANK" in text to avoid
    # matching unrelated words containing "AXIS" (e.g. "TAXATION").
    if "AXIS BANK" in joined or "AXIS" in file_name:
        return "axis"
    if "ICICI" in joined or "ICICI" in file_name:
        return "icici"
    if "HDFC" in joined or "HDFC" in file_name:
        return "hdfc"
    # Check HSBC before SBI — HSBC page text can contain "SBI" substrings
    # in compound words or payee references.
    if "HSBC" in joined or "HSBC" in file_name:
        return "hsbc"
    # Check Jupiter/CSB before SBI to avoid false matches.
    if (
        "JUPITER" in joined
        or "CSB BANK" in joined
        or "EDGE CSB" in joined
        or "JUPITER" in file_name
    ):
        return "jupiter"
    if "SBI" in joined or "SBI" in file_name:
        return "sbi"
    if "IDFC" in joined or "IDFC" in file_name:
        return "idfc"
    if "SLICE" in joined or "SLICE" in file_name:
        return "slice"
    if (
        "SURYODAY SMALL FINANCE BANK" in joined
        or "SURYODAY SFB" in joined
        or "SSFB RUPAY" in joined
        or "SSFB" in file_name
        or "SURYODAY" in file_name
    ):
        return "ssfb"
    if "BOBCARD" in joined or "BOBCARD" in file_name:
        return "bob"
    # Check YES BANK before generic — YES BANK statements contain
    # "YES BANK" prominently in the header.
    if "YES BANK" in joined or "YESBANK" in file_name:
        return "yesbank"
    return "generic"


def get_parser(choice: BankChoice, raw_data: dict[str, Any]) -> StatementParser:
    """Return parser instance for explicit or auto-detected bank choice.

    Args:
        choice: User-selected parser profile (`auto` or explicit bank).
        raw_data: Raw extraction payload used for auto-detection.

    Returns:
        Parser implementation instance for the selected bank.
    """
    effective = detect_bank(raw_data) if choice == "auto" else choice
    parser_cls = PARSER_REGISTRY.get(effective, PARSER_REGISTRY["generic"])
    return parser_cls()
