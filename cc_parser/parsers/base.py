"""Parser interface for bank-specific statement normalization."""

from abc import ABC, abstractmethod
from typing import Any

from cc_parser.parsers.models import ParsedStatement


class StatementParser(ABC):
    """Base contract for all bank parser implementations.

    Implementations must return a consistent output schema from `parse`.
    Optional `build_debug` can expose parser diagnostics for `--very-raw` mode.
    """

    bank: str = "generic"

    @abstractmethod
    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        """Convert raw extractor payload into normalized statement output.

        Args:
            raw_data: Raw extraction payload from extractor module.

        Returns:
            Normalized parser output as a ParsedStatement model.
        """
        raise NotImplementedError

    def build_debug(self, raw_data: dict[str, Any]) -> dict[str, Any]:
        """Return lightweight debug details; subclasses may extend.

        Args:
            raw_data: Raw extraction payload from extractor module.

        Returns:
            Debug dictionary for troubleshooting output.
        """
        return {
            "bank": self.bank,
            "page_count": raw_data.get("page_count", 0),
        }
