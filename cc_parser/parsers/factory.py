"""Parser selection utilities.

`detect_bank` prefers the first-page header region and filename over the full
statement body so transaction merchant text does not dominate bank detection.
It still keeps the existing heuristic ordering because several issuers mention
other banks in fine print. In particular: IndusInd must be checked before
ICICI (`ICICI Lombard` appears on some IndusInd statements), `AXIS BANK`
should be more specific than bare `AXIS`, and HSBC / Jupiter must be detected
before SBI.

`get_parser` keeps auto-detection in this module, but parser instantiation and
user-facing bank ordering now come from `registry.py`.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cc_parser.parsers.base import StatementParser
from cc_parser.parsers.registry import PARSER_REGISTRY, list_registered_banks

type BankChoice = str


@dataclass(frozen=True, slots=True)
class DetectionRule:
    """Ordered bank-detection rule.

    Args:
        bank: Parser slug returned on match.
        header_tokens: Tokens searched in the first-page header region.
        file_tokens: Optional filename-only tokens. Defaults to `header_tokens`.
    """

    bank: str
    header_tokens: tuple[str, ...]
    file_tokens: tuple[str, ...] | None = None

    def header_matches(self, header: str) -> bool:
        """Return True when the header region matches this rule."""
        return _contains_any(header, self.header_tokens)

    def file_matches(self, file_name: str) -> bool:
        """Return True when the filename matches this rule."""
        return _contains_any(file_name, self.file_tokens or self.header_tokens)


_HEADER_STOP_MARKERS = (
    "TRANSACTION HISTORY",
    "STATEMENT DETAILS",
    "DOMESTIC TRANSACTIONS",
    "INTERNATIONAL TRANSACTIONS",
    "DATE TRANSACTION DETAILS",
    "DATE DESCRIPTION",
    "CARDHOLDER TRANSACTIONS",
)

# Ordered detection rules. Keep the order aligned with the comments below and
# with AGENTS.md because mis-ordering can change parser selection.
_BANK_DETECTION_RULES: tuple[DetectionRule, ...] = (
    DetectionRule("indusind", ("INDUSIND",)),
    DetectionRule("axis", ("AXIS BANK",), ("AXIS",)),
    DetectionRule("equitas", ("EQUITAS",)),
    DetectionRule("icici", ("ICICI",)),
    DetectionRule("hdfc", ("HDFC",)),
    DetectionRule("hsbc", ("HSBC",)),
    DetectionRule("jupiter", ("JUPITER", "CSB BANK", "EDGE CSB"), ("JUPITER",)),
    DetectionRule("sbi", ("SBI",)),
    DetectionRule("idfc", ("IDFC",)),
    DetectionRule("slice", ("SLICE",)),
    DetectionRule(
        "ssfb",
        ("SURYODAY SMALL FINANCE BANK", "SURYODAY SFB", "SSFB RUPAY"),
        ("SSFB", "SURYODAY"),
    ),
    DetectionRule("bob", ("BOBCARD",)),
    DetectionRule("yesbank", ("YES BANK",), ("YESBANK",)),
)


def list_bank_choices() -> list[str]:
    """Return stable user-facing bank choices, including `auto`."""
    return list_registered_banks(include_auto=True)


def _extract_detection_header(raw_data: dict[str, Any]) -> str:
    """Return the first-page header region used for bank detection."""
    pages = raw_data.get("pages", [])
    first_page_text = ""
    if isinstance(pages, list) and pages and isinstance(pages[0], dict):
        first_page_text = str(pages[0].get("text", ""))

    header_lines: list[str] = []
    for raw_line in first_page_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        upper_line = line.upper()
        if any(marker in upper_line for marker in _HEADER_STOP_MARKERS):
            break
        header_lines.append(line)
        if len(header_lines) >= 25:
            break
    return "\n".join(header_lines).upper()


def _contains_any(value: str, candidates: tuple[str, ...]) -> bool:
    """Return True when any candidate substring appears in the value."""
    return any(candidate in value for candidate in candidates)


def detect_bank(raw_data: dict[str, Any]) -> str:
    """Infer bank profile from first pages and input file name.

    Args:
        raw_data: Raw extraction payload.

    Returns:
        One of: `icici`, `hdfc`, `sbi`, `idfc`, `indusind`, `hsbc`, `axis`, `jupiter`, `slice`, `ssfb`, `bob`, `yesbank`, `equitas`, or `generic`.
    """
    header = _extract_detection_header(raw_data)
    # Use basename only for filename checks to avoid matching directory names
    # (e.g. /statements/hsbc/some_other_bank.pdf).
    file_name = Path(raw_data.get("file", "")).name.upper()

    # Check INDUSIND before ICICI because IndusInd statements mention
    # "ICICI Lombard" (insurance provider) in the fine print.
    # Check AXIS BANK before ICICI. Use "AXIS BANK" in text to avoid
    # matching unrelated words containing "AXIS" (e.g. "TAXATION").
    # Check HSBC before SBI — HSBC page text can contain "SBI" substrings
    # in compound words or payee references.
    # Check Jupiter/CSB before SBI to avoid false matches.
    # Check YES BANK before generic — YES BANK statements contain
    # "YES BANK" prominently in the header.
    for rule in _BANK_DETECTION_RULES:
        if rule.header_matches(header) or rule.file_matches(file_name):
            return rule.bank
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
