"""HDFC parser profile.

Extends generic parsing with HDFC-specific rewards extraction:
page-1 "Reward Points" header row (opening/feature+bonus/disbursed/lapsed)
plus the later "Rewards Program Points Summary" itemized bonus table.
"""

import re
from typing import Any

from cc_parser.parsers.generic import GenericParser
from cc_parser.parsers.models import BonusProgram, ParsedStatement


# Page 1 has a "Reward Points  Opening Balance  Feature + Bonus Reward  Disbursed  Adjusted/Lapsed"
# header. The next text line looks like "<closing> Points Earned" (the "Points Earned"
# label is misleading — that number is actually the closing balance). The line after
# that holds the four column values: "<opening> <feat+bonus> <disbursed> <lapsed>".
_REWARDS_HEADER_RE = re.compile(
    r"Reward Points\s+Opening Balance\s+Feature\s*\+\s*Bonus Reward\s+Disbursed\s+Adjusted/Lapsed",
    re.IGNORECASE,
)
_POINTS_EARNED_RE = re.compile(r"(-?\d[\d,]*)\s+Points Earned", re.IGNORECASE)
_FOUR_NUMS_RE = re.compile(
    r"^\s*(-?\d[\d,]*)\s+(-?\d[\d,]*)\s+(-?\d[\d,]*)\s+(-?\d[\d,]*)\s*$",
    re.MULTILINE,
)

# The "Rewards Program Points Summary" table may span multiple pages; each page
# repeats the "SR NO. PROGRAMS BONUS POINTS" header. Row format:
#   "<n> <program name> <points> pts"
# Total row:
#   "Total <points> pts"
_PROGRAM_TABLE_HEADER_RE = re.compile(r"Rewards Program Points Summary", re.IGNORECASE)
_PROGRAM_ROW_RE = re.compile(r"^\s*\d+\s+(.+?)\s+(-?\d[\d,]*)\s+pts\s*$", re.IGNORECASE)
_PROGRAM_TOTAL_RE = re.compile(r"^\s*Total\s+(-?\d[\d,]*)\s+pts\s*$", re.IGNORECASE)


def _clean_num(value: str) -> str:
    """Strip commas from a numeric string."""
    return value.replace(",", "")


def _extract_rewards_header(page_text: str) -> dict[str, str] | None:
    """Extract the 4 column values and closing balance from page-1 rewards row.

    Returns dict with keys: opening, feature_bonus, disbursed, lapsed, closing.
    Returns None if the header isn't found or values can't be parsed.
    """
    header = _REWARDS_HEADER_RE.search(page_text)
    if not header:
        return None
    tail = page_text[header.end() : header.end() + 400]
    earned = _POINTS_EARNED_RE.search(tail)
    if not earned:
        return None
    # Anchor the four-column row lookup to after the "Points Earned" line so a
    # stray 4-number run in the header tail can't shadow it.
    four = _FOUR_NUMS_RE.search(tail, pos=earned.end())
    if not four:
        return None
    return {
        "closing": _clean_num(earned.group(1)),
        "opening": _clean_num(four.group(1)),
        "feature_bonus": _clean_num(four.group(2)),
        "disbursed": _clean_num(four.group(3)),
        "lapsed": _clean_num(four.group(4)),
    }


def _extract_bonus_programs(
    full_text: str,
) -> tuple[list[BonusProgram], str | None]:
    """Extract itemized bonus programs + declared Total from the summary table.

    The declared "Total N pts" row is authoritative: it can disagree with the
    sum of the itemized rows (e.g., March 2026 includes a -150 pt SmartBuy
    reversal whose effect is already netted elsewhere). Returns both so the
    caller can surface the declared total while keeping the breakdown intact.
    """
    if not _PROGRAM_TABLE_HEADER_RE.search(full_text):
        return [], None

    programs: list[BonusProgram] = []
    declared_total: str | None = None
    in_table = False
    for raw_line in full_text.split("\n"):
        line = raw_line.rstrip()
        if _PROGRAM_TABLE_HEADER_RE.search(line):
            in_table = True
            continue
        if not in_table:
            continue
        total_match = _PROGRAM_TOTAL_RE.match(line)
        if total_match:
            declared_total = _clean_num(total_match.group(1))
            in_table = False
            continue
        row_match = _PROGRAM_ROW_RE.match(line)
        if row_match:
            programs.append(
                BonusProgram(
                    program=row_match.group(1).strip(),
                    points=_clean_num(row_match.group(2)),
                )
            )
    return programs, declared_total


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

        pages = raw_data.get("pages", [])
        page_text = str(pages[0].get("text", "")) if pages else ""
        full_text = "\n".join(str(p.get("text", "")) for p in pages)

        header = _extract_rewards_header(page_text)
        programs, declared_bonus_total = _extract_bonus_programs(full_text)

        if header is not None:
            parsed.reward_points_line_total = parsed.overall_reward_points
            parsed.overall_reward_points = header["feature_bonus"]
            parsed.reward_points_balance = header["closing"]

        if programs:
            parsed.reward_points_bonus_breakdown = programs
            # Prefer the statement's declared Total row over the sum of
            # itemized rows: reversal rows can make the two disagree.
            parsed.reward_points_bonus = declared_bonus_total or str(
                sum(int(p.points) for p in programs)
            )

        return parsed
