"""Equitas Small Finance Bank credit card statement parser.

Equitas statements have these distinctive features:

- Page-1 summary block with ``Total Due`` / ``Due Date`` and
  ``Opening Balance`` / ``Payments/Credits`` / ``Spends/Charges``
- Transaction rows ending with explicit ``Dr.`` / ``Cr.`` markers
- Reward points inline as ``+500 B``, ``+2 E``, ``-322 B``, or ``0``
- A page-1 rewards summary with earned/bonus/closing balances
- Member header rows like ``FULL NAME : (123456XXXXXX7890)``
"""

from __future__ import annotations

import re
from typing import Any

from cc_parser.parsers.generic import GenericParser
from cc_parser.parsers.models import ParsedStatement, StatementSummary, Transaction
from cc_parser.parsers.tokens import clean_space, normalize_amount, parse_date

_NAME_RE = re.compile(r"\n([A-Za-z][A-Za-z .'-]+?)\s+RUPAY\b", re.IGNORECASE)
_CARD_RE = re.compile(r"Card\s+No:\s*([0-9*Xx]{12,19})", re.IGNORECASE)
_MEMBER_HEADER_RE = re.compile(
    r"^(?P<name>[A-Z][A-Z .'-]+?)\s*:\s*\((?P<card>[0-9*Xx]{12,19})\)$"
)
_TOTAL_DUE_RE = re.compile(
    r"Total\s+Due:\s*Due\s+Date:\s*₹\s*([\d,]+\.\d{2})\s+([0-9]{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
    re.IGNORECASE | re.DOTALL,
)
_REWARD_SUMMARY_RE = re.compile(
    r"Reward\s+Points\s+Summary\s+"
    r"Opening\s+Balance\s+Reward\s+Points\s+Earned_E\s+"
    r"Bonus\s+Points\s+Earned_B\s+Redeemed\s+Adjusted\s+Lapsed\s+Closing\s+Balance\s+"
    r"(-?\d[\d,]*)\s+(-?\d[\d,]*)\s+(-?\d[\d,]*)\s+(-?\d[\d,]*)\s+"
    r"(-?\d[\d,]*)\s+(-?\d[\d,]*)\s+(-?\d[\d,]*)",
    re.IGNORECASE | re.DOTALL,
)
_TXN_LINE_RE = re.compile(
    r"^(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<body>.+?)\s+₹\s*(?P<amount>[\d,]+\.\d{2})\s+(?P<marker>Dr\.|Cr\.)$",
    re.IGNORECASE,
)
_CURRENCY_AMOUNT_RE = re.compile(r"₹\s*([\d,]+\.\d{2})")


def _normalize_card(raw: str) -> str:
    """Normalize Equitas card masks to the repo's ``X``-masked format."""
    return re.sub(r"[*x]", "X", raw.strip()).replace(" ", "").upper()


def _extract_page_one_text(pages: list[dict[str, Any]]) -> str:
    """Return first-page text or an empty string."""
    if not pages:
        return ""
    return str(pages[0].get("text", ""))


def _extract_equitas_name(full_text: str, pages: list[dict[str, Any]]) -> str | None:
    """Extract the primary cardholder name from page 1 or member headers."""
    page_text = _extract_page_one_text(pages)
    name_match = _NAME_RE.search(page_text)
    if name_match:
        return clean_space(name_match.group(1)).upper()

    for page in pages[:2]:
        for raw_line in str(page.get("text", "")).splitlines():
            line = clean_space(raw_line).upper()
            member_match = _MEMBER_HEADER_RE.match(line)
            if member_match:
                return clean_space(member_match.group("name")).upper()

    return None


def _extract_equitas_card_number(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract the masked card number from the header or member section."""
    page_text = _extract_page_one_text(pages)
    card_match = _CARD_RE.search(page_text)
    if card_match:
        return _normalize_card(card_match.group(1))

    for page in pages[:2]:
        for raw_line in str(page.get("text", "")).splitlines():
            line = clean_space(raw_line).upper()
            member_match = _MEMBER_HEADER_RE.match(line)
            if member_match:
                return _normalize_card(member_match.group("card"))

    return None


def _extract_equitas_summary(
    page_text: str,
) -> tuple[str | None, str | None, StatementSummary]:
    """Extract page-1 summary amounts and due date.

    Args:
        page_text: First-page extracted text.

    Returns:
        Tuple of ``(total_due, due_date, summary_fields)``.
    """
    total_due: str | None = None
    due_date: str | None = None
    total_match = _TOTAL_DUE_RE.search(page_text)
    if total_match:
        total_due = normalize_amount(total_match.group(1))
        due_date = parse_date(total_match.group(2))

    summary_lines = [
        clean_space(line) for line in page_text.splitlines() if line.strip()
    ]
    amount_line: str | None = None
    for idx, line in enumerate(summary_lines):
        upper = line.upper()
        if "OPENING BALANCE" not in upper or "PAYMENTS/CREDITS" not in upper:
            continue
        for candidate in summary_lines[idx + 1 : idx + 7]:
            amounts = _CURRENCY_AMOUNT_RE.findall(candidate)
            if len(amounts) >= 3:
                amount_line = candidate
                break
        if amount_line:
            break

    if not amount_line:
        return total_due, due_date, StatementSummary()

    amounts = _CURRENCY_AMOUNT_RE.findall(amount_line)
    opening_balance = normalize_amount(amounts[0])
    payments_credits = normalize_amount(amounts[1])
    spends_charges = normalize_amount(amounts[2])

    return (
        total_due,
        due_date,
        StatementSummary(
            summary_amount_candidates=[
                opening_balance,
                payments_credits,
                spends_charges,
                *([total_due] if total_due else []),
            ],
            previous_statement_dues=opening_balance,
            payments_credits_received=payments_credits,
            purchases_debit=spends_charges,
        ),
    )


def _extract_equitas_rewards(page_text: str) -> dict[str, str] | None:
    """Extract page-1 rewards summary numbers."""
    match = _REWARD_SUMMARY_RE.search(page_text)
    if not match:
        return None

    opening, earned, bonus, redeemed, adjusted, lapsed, closing = (
        value.replace(",", "") for value in match.groups()
    )
    return {
        "opening": opening,
        "earned": earned,
        "bonus": bonus,
        "redeemed": redeemed,
        "adjusted": adjusted,
        "lapsed": lapsed,
        "closing": closing,
    }


def _looks_like_reference(token: str) -> bool:
    """Return True for Equitas reference-number tokens."""
    upper = token.upper()
    return bool(upper.startswith("RT") or re.fullmatch(r"\d{12,}", upper))


def _parse_reward_tail(tokens: list[str]) -> tuple[str | None, list[str]]:
    """Extract trailing reward points tokens from a transaction body."""
    if not tokens:
        return None, tokens

    if tokens[-1] == "0":
        return "0", tokens[:-1]

    if len(tokens) >= 2 and re.fullmatch(r"[BE]", tokens[-1], re.IGNORECASE):
        raw_value = tokens[-2]
        if re.fullmatch(r"[+-]?\d{1,6}", raw_value):
            reward = raw_value if raw_value.startswith("-") else raw_value.lstrip("+")
            return reward, tokens[:-2]

    return None, tokens


def _parse_equitas_transaction_body(body: str) -> tuple[str | None, str]:
    """Split a transaction body into reward points and narration."""
    tokens = clean_space(body).split()

    if (
        len(tokens) >= 3
        and re.fullmatch(r"\d{8,}", tokens[0])
        and any(_looks_like_reference(token) for token in tokens[1:])
        and any(any(ch.isalpha() for ch in token) for token in tokens[1:])
    ):
        tokens = tokens[1:]

    reward_points, narration_tokens = _parse_reward_tail(tokens)
    narration = clean_space(" ".join(narration_tokens))
    return reward_points, narration


def _parse_signed_points(value: str | None) -> int:
    """Parse reward points with sign preserved."""
    if not value:
        return 0
    match = re.search(r"[+-]?\d+", str(value))
    return int(match.group(0)) if match else 0


class EquitasParser(GenericParser):
    """Parser profile for Equitas Small Finance Bank credit card statements."""

    bank = "equitas"

    def _extract_name(self, full_text: str, pages: list[dict[str, Any]]) -> str | None:
        return _extract_equitas_name(full_text, pages) or super()._extract_name(
            full_text, pages
        )

    def _extract_card_number(
        self, full_text: str, pages: list[dict[str, Any]], file_name: str
    ) -> str | None:
        return _extract_equitas_card_number(
            full_text, pages
        ) or super()._extract_card_number(full_text, pages, file_name)

    def _extract_due_date(
        self, full_text: str, pages: list[dict[str, Any]]
    ) -> str | None:
        _total_due, due_date, _summary = _extract_equitas_summary(
            _extract_page_one_text(pages)
        )
        return due_date or super()._extract_due_date(full_text, pages)

    def _extract_total_amount_due(
        self, full_text: str, pages: list[dict[str, Any]]
    ) -> str | None:
        total_due, _due_date, _summary = _extract_equitas_summary(
            _extract_page_one_text(pages)
        )
        return total_due or super()._extract_total_amount_due(full_text, pages)

    def _extract_summary(
        self, full_text: str, pages: list[dict[str, Any]]
    ) -> StatementSummary:
        _total_due, _due_date, summary = _extract_equitas_summary(
            _extract_page_one_text(pages)
        )
        return summary

    def _extract_transactions_with_debug(
        self, pages: list[dict[str, Any]]
    ) -> tuple[list[Transaction], dict[str, Any]]:
        """Parse Equitas transactions from page text rows with ``Dr.`` / ``Cr.`` markers."""
        transactions: list[Transaction] = []
        date_lines: list[dict[str, Any]] = []
        rejected_date_lines: list[dict[str, Any]] = []
        detected_members: list[dict[str, Any]] = []
        current_member: str | None = None
        current_card: str | None = None

        for page in pages:
            page_number = int(page.get("page_number", 0) or 0)
            for line_index, raw_line in enumerate(
                str(page.get("text", "")).splitlines()
            ):
                line = clean_space(raw_line)
                if not line:
                    continue

                member_match = _MEMBER_HEADER_RE.match(line.upper())
                if member_match:
                    current_member = clean_space(member_match.group("name")).upper()
                    current_card = _normalize_card(member_match.group("card"))
                    detected_members.append(
                        {
                            "page": page_number,
                            "line_index": line_index,
                            "member": current_member,
                        }
                    )
                    continue

                txn_match = _TXN_LINE_RE.match(line)
                if not txn_match:
                    continue

                tokens = line.split()
                date_lines.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "tokens": tokens,
                        "current_member": current_member,
                        "current_card": current_card,
                    }
                )

                reward_points, narration = _parse_equitas_transaction_body(
                    txn_match.group("body")
                )
                if not narration:
                    rejected_date_lines.append(
                        {
                            "page": page_number,
                            "line_index": line_index,
                            "reason": "empty_narration",
                            "tokens": tokens,
                        }
                    )
                    continue

                is_credit = txn_match.group("marker").upper().startswith("CR")
                transactions.append(
                    Transaction(
                        date=txn_match.group("date"),
                        time=None,
                        narration=narration,
                        reward_points=reward_points,
                        amount=normalize_amount(txn_match.group("amount")),
                        card_number=current_card,
                        person=current_member,
                        transaction_type="credit" if is_credit else "debit",
                        credit_reasons="cr_marker" if is_credit else None,
                    )
                )

        return (
            transactions,
            {
                "date_lines": date_lines,
                "rejected_date_lines": rejected_date_lines,
                "detected_members": detected_members,
            },
        )

    def _normalize_transactions(
        self,
        transactions: list[Transaction],
        name: str | None,
        card_number: str | None,
    ) -> None:
        """Normalize primary-card masks when the statement only exposes one card."""
        super()._normalize_transactions(transactions, name, card_number)

        if not card_number:
            return

        seen_cards = {txn.card_number for txn in transactions if txn.card_number}
        if len(seen_cards) != 1:
            return

        seen_card = next(iter(seen_cards))
        if not seen_card or not seen_card.endswith(card_number[-4:]):
            return

        for txn in transactions:
            txn.card_number = card_number

    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        """Parse Equitas statement payload and apply rewards-summary overrides."""
        parsed = super().parse(raw_data)
        parsed.bank = self.bank

        page_text = _extract_page_one_text(raw_data.get("pages", []))
        rewards = _extract_equitas_rewards(page_text)
        line_total = sum(
            _parse_signed_points(txn.reward_points)
            for txn in [*parsed.transactions, *parsed.payments_refunds]
        )
        if line_total:
            parsed.reward_points_line_total = str(line_total)

        if rewards is not None:
            parsed.overall_reward_points = str(
                int(rewards["earned"]) + int(rewards["bonus"])
            )
            parsed.reward_points_bonus = rewards["bonus"]
            parsed.reward_points_balance = rewards["closing"]

        return parsed
