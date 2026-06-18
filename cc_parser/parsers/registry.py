"""Stable parser registry shared by the CLI, browser, and factory."""

from cc_parser.parsers.axis import AxisParser
from cc_parser.parsers.base import StatementParser
from cc_parser.parsers.bob import BobParser
from cc_parser.parsers.equitas import EquitasParser
from cc_parser.parsers.generic import GenericParser
from cc_parser.parsers.hdfc import HdfcParser
from cc_parser.parsers.hsbc import HsbcParser
from cc_parser.parsers.icici import IciciParser
from cc_parser.parsers.idfc import IdfcParser
from cc_parser.parsers.indusind import IndusindParser
from cc_parser.parsers.jupiter import JupiterParser
from cc_parser.parsers.kotak import KotakParser
from cc_parser.parsers.sbi import SbiParser
from cc_parser.parsers.slice import SliceParser
from cc_parser.parsers.ssfb import SsfbParser
from cc_parser.parsers.yesbank import YesbankParser

PARSER_REGISTRY: dict[str, type[StatementParser]] = {
    "icici": IciciParser,
    "hdfc": HdfcParser,
    "sbi": SbiParser,
    "idfc": IdfcParser,
    "indusind": IndusindParser,
    "hsbc": HsbcParser,
    "axis": AxisParser,
    "jupiter": JupiterParser,
    "slice": SliceParser,
    "kotak": KotakParser,
    "ssfb": SsfbParser,
    "bob": BobParser,
    "yesbank": YesbankParser,
    "equitas": EquitasParser,
    "generic": GenericParser,
}


def list_registered_banks(*, include_auto: bool = False) -> list[str]:
    """Return user-facing parser names in stable order."""
    banks = list(PARSER_REGISTRY)
    if include_auto:
        return ["auto", *banks]
    return banks


__all__ = ["PARSER_REGISTRY", "list_registered_banks"]
