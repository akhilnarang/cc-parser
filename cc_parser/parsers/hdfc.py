"""HDFC parser profile.

Currently extends generic parser behavior. Keep HDFC-specific tweaks here.
"""

from typing import Any

from cc_parser.parsers.generic import GenericParser
from cc_parser.parsers.models import ParsedStatement


class HdfcParser(GenericParser):
    """Parser entrypoint for HDFC statements."""

    bank = "hdfc"

    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        """Parse HDFC statement payload using shared generic logic.

        Args:
            raw_data: Raw extraction payload from extractor.

        Returns:
            Normalized statement output for HDFC profile.
        """
        parsed = super().parse(raw_data)
        parsed.bank = self.bank
        return parsed
