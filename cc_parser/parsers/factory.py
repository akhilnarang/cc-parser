"""Parser selection utilities.

`detect_bank` uses simple text/file-name heuristics.
`get_parser` returns the parser implementation for the selected bank.
"""

from pathlib import Path
from typing import Any, Literal

from cc_parser.parsers.axis import AxisParser
from cc_parser.parsers.base import StatementParser
from cc_parser.parsers.bob import BobParser
from cc_parser.parsers.generic import GenericParser
from cc_parser.parsers.hdfc import HdfcParser
from cc_parser.parsers.hsbc import HsbcParser
from cc_parser.parsers.icici import IciciParser
from cc_parser.parsers.idfc import IdfcParser
from cc_parser.parsers.indusind import IndusindParser
from cc_parser.parsers.jupiter import JupiterParser
from cc_parser.parsers.sbi import SbiParser
from cc_parser.parsers.slice import SliceParser
from cc_parser.parsers.ssfb import SsfbParser
from cc_parser.parsers.yesbank import YesbankParser

type BankChoice = Literal[
    "auto",
    "icici",
    "hdfc",
    "sbi",
    "idfc",
    "indusind",
    "hsbc",
    "axis",
    "jupiter",
    "slice",
    "ssfb",
    "bob",
    "yesbank",
    "generic",
]


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
    match effective:
        case "icici":
            return IciciParser()
        case "hdfc":
            return HdfcParser()
        case "sbi":
            return SbiParser()
        case "idfc":
            return IdfcParser()
        case "indusind":
            return IndusindParser()
        case "hsbc":
            return HsbcParser()
        case "axis":
            return AxisParser()
        case "jupiter":
            return JupiterParser()
        case "slice":
            return SliceParser()
        case "ssfb":
            return SsfbParser()
        case "bob":
            return BobParser()
        case "yesbank":
            return YesbankParser()
        case _:
            return GenericParser()
