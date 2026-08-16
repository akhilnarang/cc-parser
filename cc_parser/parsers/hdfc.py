"""HDFC parser profile.

Extends generic parsing with HDFC-specific rewards extraction:
page-1 "Reward Points" header row (opening/feature+bonus/disbursed/lapsed)
plus the later "Rewards Program Points Summary" itemized bonus table.
"""

import re
from typing import Any

from cc_parser.parsers.generic import GenericParser
from cc_parser.parsers.models import BonusProgram, ParsedStatement, StatementSummary
from cc_parser.parsers.tokens import normalize_amount

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

# HDFC prints the summary as a single equation row below the labels:
#   "C<prev> C<payments> + C<purchases> + C<finance> ="
# The values map to previous dues, payments/credits received, purchases, and
# finance charges, in that order. Each value can be negative when a prior cycle
# ended in credit. The shared positional heuristic mis-reads this layout because
# the total amount due appears first in the block, so HDFC parses the row here.
# The row is one line, so horizontal whitespace only. This stops a match from
# stitching a value off the line above.
_SUMMARY_EQUATION_RE = re.compile(
    r"C[ \t]*(-?\d[\d,]*\.\d{2})[ \t]+C[ \t]*(-?\d[\d,]*\.\d{2})[ \t]*\+[ \t]*"
    r"C[ \t]*(-?\d[\d,]*\.\d{2})[ \t]*\+[ \t]*C[ \t]*(-?\d[\d,]*\.\d{2})[ \t]*="
)


def _extract_summary_equation(page_text: str) -> tuple[str, str, str, str] | None:
    """Read the HDFC summary equation row from page-1 text.

    The search stays inside the summary block so a later illustration that
    repeats the equation shape cannot overwrite the real values.

    Args:
        page_text: Page-1 statement text.

    Returns:
        Normalized ``(previous_dues, payments_received, purchases, finance)``
        or ``None`` when the equation row is absent.
    """
    upper = page_text.upper()
    start = upper.find("PREVIOUS STATEMENT DUES")
    if start == -1:
        start = upper.find("TOTAL AMOUNT DUE")
    if start == -1:
        start = 0
    end = upper.find("TOTAL CREDIT LIMIT", start)
    region = page_text[start:end] if end != -1 else page_text[start : start + 600]

    match = _SUMMARY_EQUATION_RE.search(region)
    if not match:
        return None
    return (
        normalize_amount(match.group(1)),
        normalize_amount(match.group(2)),
        normalize_amount(match.group(3)),
        normalize_amount(match.group(4)),
    )


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

    # HDFC renders a convert-to-instalment control as a text run on the
    # transaction's own baseline, in a lane between the time column and the
    # description column, and repeats the table's column header on every page
    # that carries rows. Both were measured on the HDFC layouts, so the
    # header-anchored removal is enabled for this profile.
    strip_action_lane = True

    def _extract_summary(
        self, full_text: str, pages: list[dict[str, Any]]
    ) -> StatementSummary:
        """Map the HDFC summary equation row onto the summary fields.

        Args:
            full_text: Full statement text across pages.
            pages: Raw per-page extraction payloads.

        Returns:
            Summary with the equation values applied when the row is present,
            otherwise the shared heuristic result.
        """
        summary = super()._extract_summary(full_text, pages)
        page_text = str(pages[0].get("text", "")) if pages else full_text
        equation = _extract_summary_equation(page_text)
        if equation is not None:
            (
                summary.previous_statement_dues,
                summary.payments_credits_received,
                summary.purchases_debit,
                summary.finance_charges,
            ) = equation
            # The heuristic equation tail is meaningless once the row is parsed.
            summary.equation_tail = None
        return summary

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
