"""HSBC total-payment-due extraction regressions."""

from cc_parser.parsers.hsbc import (
    _apply_hsbc_reward_rollups,
    _extract_hsbc_reward_points,
    _extract_hsbc_total_amount_due,
    _extract_hsbc_transactions,
    _split_hsbc_reconciliation_credits,
)
from cc_parser.parsers.models import CardSummary, PersonGroup, StatementSummary
from cc_parser.parsers.summary.reconciliation import build_reconciliation


def _line_words(tokens: list[str], top: float) -> list[dict]:
    """Build one minimal pdfplumber-style visual line."""
    return [
        {
            "text": token,
            "x0": float(index * 45),
            "x1": float(index * 45 + 40),
            "top": top,
            "doctop": top,
            "bottom": top + 8,
        }
        for index, token in enumerate(tokens)
    ]


def test_payment_summary_period_row_beats_net_outstanding_balance():
    """An unlabeled payment-summary row still carries the actual total due."""
    words = [
        *_line_words(
            [
                "12",
                "MAY",
                "2026",
                "To",
                "11",
                "JUN",
                "2026",
                "12,345.67",
            ],
            80.0,
        ),
        *_line_words(
            ["NET", "OUTSTANDING", "BALANCE", "98,765.43"],
            600.0,
        ),
    ]
    pages = [{"page_number": 1, "words": words, "text": ""}]

    assert _extract_hsbc_total_amount_due("", pages) == "12,345.67"


def test_outstanding_balances_are_not_used_as_total_payment_due():
    """Future loan and net balances are not substitutes for payment-summary data."""
    words = [
        *_line_words(
            ["TOTAL", "PURCHASE", "OUTSTANDING", "1,200.00"],
            500.0,
        ),
        *_line_words(["TOTAL", "LOAN", "OUTSTANDING", "9,000.00"], 520.0),
        *_line_words(["NET", "OUTSTANDING", "BALANCE", "10,200.00"], 540.0),
    ]
    pages = [{"page_number": 1, "words": words, "text": ""}]

    assert _extract_hsbc_total_amount_due("", pages) is None


def test_explicit_total_payment_due_works_without_word_coordinates():
    """Text-only extraction remains supported when the label is available."""
    pages = [
        {
            "page_number": 1,
            "words": [],
            "text": "Total Payment Due: 7,654.32\nNet Outstanding Balance 99,999.99",
        }
    ]

    assert _extract_hsbc_total_amount_due("", pages) == "7,654.32"


def test_total_due_text_fallback_never_reads_later_pages():
    """A later tariff example cannot replace missing page-1 summary evidence."""
    pages = [
        {"page_number": 1, "words": [], "text": ""},
        {
            "page_number": 2,
            "words": [],
            "text": "Illustration\nTotal Payment Due: 8,888.88",
        },
    ]
    full_text = "\n".join(page["text"] for page in pages)

    assert _extract_hsbc_total_amount_due(full_text, pages) is None


def test_reward_summary_extracts_earned_and_closing_as_points():
    """Reward decimals are normalized as points, not monetary amounts."""
    words = [
        *_line_words(["10,000.00", "20,000.00", "5,000.00", "25,000.00"], 600.0),
        *_line_words(["120.00", "35.00", "10.00", "145.00"], 700.0),
    ]
    pages = [{"page_number": 1, "words": words, "text": ""}]

    assert _extract_hsbc_reward_points(pages) == ("35", "145")


def test_single_card_reward_points_populate_card_and_person_rollups():
    """Statement-only earnings are attributable when there is one owner."""
    cards = [
        CardSummary(
            card_number="40XX XXXX XXXX 0001",
            person="TEST CARDHOLDER",
            transaction_count=1,
            total_amount="100.00",
            reward_points_total="0",
        )
    ]
    groups = [
        PersonGroup(
            person="TEST CARDHOLDER",
            transaction_count=1,
            total_amount="100.00",
            reward_points_total="0",
            transactions=[],
        )
    ]

    _apply_hsbc_reward_rollups(cards, groups, "35")

    assert cards[0].reward_points_total == "35"
    assert groups[0].reward_points_total == "35"


def test_emi_detail_is_retained_and_transfer_credit_reconciles():
    """EMI sublines identify internal credits while remaining in narration."""
    words = [
        *_line_words(["PURCHASES", "&", "INSTALLMENTS"], 100.0),
        *_line_words(["15JUL", "TEST", "MERCHANT", "300.00", "CR"], 120.0),
        *_line_words(["1ST", "OF", "3", "INSTALLMENTS", "PRINCIPAL"], 130.0),
        *_line_words(["15JUL", "TEST", "MERCHANT", "300.00"], 150.0),
        *_line_words(["1ST", "OF", "3", "INSTALLMENTS", "PRINCIPAL"], 160.0),
        *_line_words(["TOTAL", "PURCHASE", "OUTSTANDING", "300.00"], 180.0),
    ]
    pages = [{"page_number": 1, "words": words, "text": ""}]

    transactions, _ = _extract_hsbc_transactions(pages, "2026", None)

    assert len(transactions) == 2
    assert all(
        txn.narration == "TEST MERCHANT 1ST OF 3 INSTALLMENTS PRINCIPAL"
        for txn in transactions
    )
    debits = [txn for txn in transactions if txn.transaction_type == "debit"]
    credits = [txn for txn in transactions if txn.transaction_type == "credit"]
    assert credits[0].credit_reasons == "emi_installment_transfer"

    payable_credits, emi_transfers = _split_hsbc_reconciliation_credits(debits, credits)
    reconciliation = build_reconciliation(
        "300.00",
        debits,
        credits,
        StatementSummary(),
        smart_credit_transactions=payable_credits,
    )

    assert emi_transfers == credits
    assert reconciliation.parsed_credit_total == "300.00"
    assert reconciliation.smart_excluded_credit_total == "300.00"
    assert reconciliation.smart_credit_total == "0.00"
    assert reconciliation.smart_expected_total == "300.00"
    assert reconciliation.smart_delta == "0.00"


def test_multi_card_reward_points_are_not_fabricated():
    """An aggregate reward value cannot be split across multiple cards."""
    cards = [
        CardSummary(
            card_number=f"40XX XXXX XXXX 000{index}",
            person=f"CARDHOLDER {index}",
            transaction_count=1,
            total_amount="100.00",
            reward_points_total="0",
        )
        for index in (1, 2)
    ]
    groups = [
        PersonGroup(
            person=f"CARDHOLDER {index}",
            transaction_count=1,
            total_amount="100.00",
            reward_points_total="0",
            transactions=[],
        )
        for index in (1, 2)
    ]

    _apply_hsbc_reward_rollups(cards, groups, "35")

    assert [card.reward_points_total for card in cards] == ["0", "0"]
    assert [group.reward_points_total for group in groups] == ["0", "0"]
