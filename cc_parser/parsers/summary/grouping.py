"""Grouping and aggregation helpers for parsed transactions."""

from decimal import Decimal
from typing import Any

from cc_parser.parsers.models import CardSummary, PersonGroup, Transaction
from cc_parser.parsers.tokens import (
    format_amount,
    parse_amount,
    parse_points,
    sum_amounts,
    sum_points,
)


def group_transactions_by_person(
    transactions: list[Transaction], fallback_name: str | None
) -> list[PersonGroup]:
    """Group transactions by person and compute totals/points."""
    grouped: dict[str, list[Transaction]] = {}
    for txn in transactions:
        person = txn.person or fallback_name or "UNKNOWN"
        grouped.setdefault(person, []).append(txn)

    grouped_rows: list[PersonGroup] = []
    for person, rows in grouped.items():
        total = sum_amounts(rows)
        points_total = sum_points(rows)
        grouped_rows.append(
            PersonGroup(
                person=person,
                transaction_count=len(rows),
                total_amount=format_amount(total),
                reward_points_total=str(int(points_total)),
                transactions=rows,
            )
        )

    grouped_rows.sort(key=lambda item: item.person)
    return grouped_rows


def build_card_summaries(
    transactions: list[Transaction], fallback_name: str | None
) -> tuple[list[CardSummary], str]:
    """Build person/card summary totals for parsed transactions."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}

    for txn in transactions:
        card_number = txn.card_number or "UNKNOWN"
        person = txn.person or fallback_name or "UNKNOWN"
        key = (card_number, person)

        if key not in grouped:
            grouped[key] = {
                "card_number": card_number,
                "person": person,
                "total": Decimal(0),
                "points_total": Decimal(0),
                "transaction_count": 0,
            }

        grouped[key]["total"] += parse_amount(str(txn.amount or "0"))
        grouped[key]["points_total"] += parse_points(txn.reward_points)
        grouped[key]["transaction_count"] += 1

    summaries: list[CardSummary] = []
    overall_total = Decimal(0)
    for item in grouped.values():
        total = item["total"]
        overall_total += total
        summaries.append(
            CardSummary(
                card_number=str(item["card_number"]),
                person=str(item["person"]),
                transaction_count=int(item["transaction_count"]),
                total_amount=format_amount(total),
                reward_points_total=str(int(item["points_total"])),
            )
        )

    summaries.sort(key=lambda row: (row.person, row.card_number))
    return summaries, format_amount(overall_total)


__all__ = ["build_card_summaries", "group_transactions_by_person"]
