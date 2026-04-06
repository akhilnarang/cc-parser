"""Unit tests for adjustment pair detection system."""

import pytest
from decimal import Decimal
from cc_parser.parsers.models import Transaction, AdjustmentPair
from cc_parser.parsers.transaction_id_generator import assign_transaction_ids
from cc_parser.parsers.reconciliation import detect_adjustment_pairs
from cc_parser.parsers.scoring_engine import (
    score_candidate_pair,
    determine_confidence,
    determine_kind,
    calculate_amount_delta,
)
from cc_parser.parsers.candidate_generation import (
    is_normal_payment_credit,
    is_malformed,
    has_refund_keyword,
    should_hard_reject,
    should_early_prune,
)
from cc_parser.parsers.match_selection import select_best_non_overlapping_pairs
from cc_parser.parsers.narration import normalize_merchant_name
from cc_parser.parsers.similarity_metrics import merchant_similarity


class TestTransactionIdGeneration:
    """Test transaction ID generation stability."""

    def test_transaction_id_stability(self):
        """Transaction IDs should be stable across re-parses."""
        transactions = [
            Transaction(
                date="01/01/2024",
                narration="SWIGGY BANGALORE",
                amount="500.00",
                transaction_type="debit",
            ),
            Transaction(
                date="02/01/2024",
                narration="REFUND SWIGGY",
                amount="500.00",
                transaction_type="credit",
            ),
        ]

        # Assign IDs twice
        first_pass = assign_transaction_ids(transactions, "axis")
        second_pass = assign_transaction_ids(transactions, "axis")

        # IDs should be identical
        assert first_pass[0].transaction_id == second_pass[0].transaction_id
        assert first_pass[1].transaction_id == second_pass[1].transaction_id

    def test_transaction_id_uniqueness(self):
        """Different transactions should get different IDs."""
        transactions = [
            Transaction(
                date="01/01/2024",
                narration="MERCHANT A",
                amount="100.00",
                transaction_type="debit",
            ),
            Transaction(
                date="02/01/2024",
                narration="MERCHANT B",
                amount="200.00",
                transaction_type="debit",
            ),
        ]

        with_ids = assign_transaction_ids(transactions, "sbi")
        assert with_ids[0].transaction_id != with_ids[1].transaction_id


class TestNarrationNormalization:
    """Test narration normalization for merchant extraction."""

    def test_strip_reference_numbers(self):
        """Should strip reference numbers."""
        narration = "SWIGGY BANGALORE (Ref#123456789)"
        normalized = normalize_merchant_name(narration)
        assert "REF" not in normalized
        assert "123456789" not in normalized
        assert "SWIGGY" in normalized

    def test_strip_processor_wrappers(self):
        """Should strip payment processor wrappers."""
        narration = "REFUND FRM RAZORPAY PAYMENTS ZEPTO"
        normalized = normalize_merchant_name(narration)
        assert "RAZORPAY" not in normalized
        assert "REFUND FRM" not in normalized
        assert "ZEPTO" in normalized

    def test_strip_cosmetic_wording(self):
        """Should strip cosmetic refund/reversal wording."""
        narration = "SWIGGY REFUND"
        normalized = normalize_merchant_name(narration)
        assert "SWIGGY" in normalized
        assert normalized == "SWIGGY"

    def test_axis_bank_specific(self):
        """Should handle Axis-specific patterns."""
        narration = "VISA POS TXN AT IN/SWIGGY BANGALORE"
        normalized = normalize_merchant_name(narration, "axis")
        assert "VISA POS TXN" not in normalized
        assert "SWIGGY" in normalized

    def test_location_suffix_requires_slash(self):
        """'MERCHANT IN KARNATAKA' preserved, 'MERCHANT IN/KA' stripped."""
        result_no_slash = normalize_merchant_name("MERCHANT IN KARNATAKA")
        assert "KARNATAKA" in result_no_slash

        result_with_slash = normalize_merchant_name("MERCHANT IN/KA")
        assert "KA" not in result_with_slash
        assert "MERCHANT" in result_with_slash


class TestSimilarityMetrics:
    """Test merchant similarity calculations."""

    def test_exact_match(self):
        """Exact matches should have high similarity."""
        sim = merchant_similarity("SWIGGY BANGALORE", "SWIGGY BANGALORE")
        assert sim >= 0.9

    def test_normalized_match(self):
        """Normalized matches should have high similarity."""
        sim = merchant_similarity("VISA POS TXN AT SWIGGY", "REFUND SWIGGY")
        # After normalization, both should contain "SWIGGY"
        assert sim > 0.0

    def test_unrelated_merchants(self):
        """Unrelated merchants should have low similarity."""
        sim = merchant_similarity("AMAZON", "FLIPKART")
        assert sim < 0.3


class TestCandidateGeneration:
    """Test candidate generation and filtering."""

    def test_normal_payment_exclusion(self):
        """Normal payment credits should be excluded."""
        payment = Transaction(
            date="01/01/2024",
            narration="PAYMENT RECEIVED - THANK YOU",
            amount="1000.00",
            transaction_type="credit",
        )
        assert is_normal_payment_credit(payment) is True

    def test_refund_keyword_detection(self):
        """Should detect refund keywords."""
        refund = Transaction(
            date="01/01/2024",
            narration="REFUND SWIGGY",
            amount="500.00",
            transaction_type="credit",
        )
        assert has_refund_keyword(refund) is True

    def test_card_conflict_rejection(self):
        """Different cards should be rejected."""
        debit = Transaction(
            date="01/01/2024",
            narration="SWIGGY",
            amount="500.00",
            card_number="1234",
            transaction_type="debit",
        )
        credit = Transaction(
            date="02/01/2024",
            narration="REFUND",
            amount="500.00",
            card_number="5678",
            transaction_type="credit",
        )

        should_reject, reason = should_hard_reject(debit, credit)
        assert should_reject is True
        assert reason == "card_conflict"

    def test_refund_keyword_word_boundary(self):
        """'NONREFUNDABLE CHARGE' should NOT match refund keywords."""
        txn = Transaction(
            date="01/01/2024",
            narration="NONREFUNDABLE CHARGE",
            amount="500.00",
            transaction_type="credit",
        )
        assert has_refund_keyword(txn) is False

    def test_cashback_not_refund_keyword(self):
        """'CASHBACK 5% ON GROCERIES' should NOT match refund keywords."""
        txn = Transaction(
            date="01/01/2024",
            narration="CASHBACK 5% ON GROCERIES",
            amount="50.00",
            transaction_type="credit",
        )
        assert has_refund_keyword(txn) is False


class TestEarlyPruning:
    """Tests for early pruning logic."""

    def test_should_early_prune_large_delta(self):
        """Large delta (>50%) with no keywords/card should be pruned."""
        debit = Transaction(
            date="01/01/2024",
            narration="AMAZON",
            amount="1000.00",
            transaction_type="debit",
        )
        credit = Transaction(
            date="02/01/2024",
            narration="SOME CREDIT",
            amount="200.00",
            transaction_type="credit",
        )
        assert should_early_prune(debit, credit) is True

    def test_should_early_prune_kept_with_keyword(self):
        """Large delta but refund keyword should NOT be pruned."""
        debit = Transaction(
            date="01/01/2024",
            narration="AMAZON",
            amount="1000.00",
            transaction_type="debit",
        )
        credit = Transaction(
            date="02/01/2024",
            narration="REFUND AMAZON",
            amount="200.00",
            transaction_type="credit",
        )
        assert should_early_prune(debit, credit) is False


class TestScoring:
    """Test scoring engine."""

    def test_exact_refund_scoring(self):
        """Exact refund should score highly."""
        debit = Transaction(
            date="01/01/2024",
            narration="SWIGGY BANGALORE",
            amount="500.00",
            card_number="1234",
            person="JOHN DOE",
            transaction_type="debit",
            transaction_id="txn_001",
        )
        credit = Transaction(
            date="03/01/2024",
            narration="REFUND SWIGGY BANGALORE",
            amount="500.00",
            card_number="1234",
            person="JOHN DOE",
            transaction_type="credit",
            transaction_id="txn_002",
        )

        score, reasons, _, _ = score_candidate_pair(debit, credit, "axis")

        # Should have high score
        assert score >= 70
        assert "exact_amount_match" in reasons
        assert any("same_card" in r for r in reasons)
        assert any("same_person" in r for r in reasons)
        assert "refund_keyword_present" in reasons

    def test_partial_refund_scoring(self):
        """Partial refund should be detected."""
        debit = Transaction(
            date="01/01/2024",
            narration="ZEPTO MARKETPLACE",
            amount="9160.00",
            transaction_type="debit",
            transaction_id="txn_001",
        )
        credit = Transaction(
            date="03/01/2024",
            narration="REFUND ZEPTO",
            amount="9110.00",
            transaction_type="credit",
            transaction_id="txn_002",
        )

        delta_decimal, delta_str, delta_pct = calculate_amount_delta(debit, credit)

        # Delta should be 50.00
        assert delta_decimal == Decimal("50.00")
        # Percentage should be ~0.55%
        assert delta_pct is not None
        delta_val = float(delta_pct.rstrip("%"))
        assert delta_val < 1.0

    def test_person_conflict_penalty(self):
        """Person conflict should reduce score."""
        debit = Transaction(
            date="01/01/2024",
            narration="SWIGGY",
            amount="500.00",
            person="JOHN DOE",
            transaction_type="debit",
            transaction_id="txn_001",
        )
        credit = Transaction(
            date="02/01/2024",
            narration="REFUND SWIGGY",
            amount="500.00",
            person="JANE SMITH",
            transaction_type="credit",
            transaction_id="txn_002",
        )

        score, reasons, _, _ = score_candidate_pair(debit, credit)

        # Should have person conflict penalty
        assert any("person_conflict" in r for r in reasons)


class TestKindDetermination:
    """Test pair kind classification."""

    def test_exact_refund_kind(self):
        """Zero delta should be exact_refund."""
        debit = Transaction(
            date="01/01/2024",
            narration="SWIGGY",
            amount="500.00",
            transaction_type="debit",
            transaction_id="txn_001",
        )
        credit = Transaction(
            date="02/01/2024",
            narration="REFUND SWIGGY",
            amount="500.00",
            transaction_type="credit",
            transaction_id="txn_002",
        )

        delta_decimal, _, delta_pct = calculate_amount_delta(debit, credit)
        kind = determine_kind(debit, credit, delta_decimal, delta_pct, 0.8, 80)

        assert kind == "exact_refund"

    def test_reversal_kind(self):
        """Reversal keyword should give reversal kind."""
        debit = Transaction(
            date="01/01/2024",
            narration="SWIGGY",
            amount="500.00",
            transaction_type="debit",
            transaction_id="txn_001",
        )
        credit = Transaction(
            date="02/01/2024",
            narration="REVERSAL SWIGGY",
            amount="500.00",
            transaction_type="credit",
            transaction_id="txn_002",
        )

        delta_decimal, _, delta_pct = calculate_amount_delta(debit, credit)
        kind = determine_kind(debit, credit, delta_decimal, delta_pct, 0.8, 80)

        assert kind == "reversal"

    def test_partial_refund_kind(self):
        """Small delta with good similarity should be partial_refund."""
        debit = Transaction(
            date="01/01/2024",
            narration="SWIGGY",
            amount="1000.00",
            transaction_type="debit",
            transaction_id="txn_001",
        )
        credit = Transaction(
            date="02/01/2024",
            narration="REFUND SWIGGY",
            amount="950.00",
            transaction_type="credit",
            transaction_id="txn_002",
        )

        delta_decimal, _, delta_pct = calculate_amount_delta(debit, credit)
        # 50/1000 = 5% delta, 0.8 similarity
        kind = determine_kind(debit, credit, delta_decimal, delta_pct, 0.8, 60)

        assert kind == "partial_refund"

    def test_possible_refund_kind(self):
        """Large delta + low similarity should give possible_refund."""
        debit = Transaction(
            date="01/01/2024",
            narration="AMAZON",
            amount="1000.00",
            transaction_type="debit",
            transaction_id="txn_001",
        )
        credit = Transaction(
            date="02/01/2024",
            narration="REFUND FLIPKART",
            amount="200.00",
            transaction_type="credit",
            transaction_id="txn_002",
        )
        delta_decimal, _, delta_pct = calculate_amount_delta(debit, credit)
        kind = determine_kind(debit, credit, delta_decimal, delta_pct, 0.1, 30)
        assert kind == "possible_refund"


class TestDetectAdjustmentPairs:
    """Integration tests for detect_adjustment_pairs."""

    def test_exact_refund_detection(self):
        """Should detect exact refund pairs."""
        debits = assign_transaction_ids(
            [
                Transaction(
                    date="01/01/2024",
                    narration="SWIGGY BANGALORE",
                    amount="500.00",
                    card_number="1234",
                    transaction_type="debit",
                ),
            ],
            "axis",
        )

        credits = assign_transaction_ids(
            [
                Transaction(
                    date="03/01/2024",
                    narration="REFUND SWIGGY",
                    amount="500.00",
                    card_number="1234",
                    transaction_type="credit",
                ),
            ],
            "axis",
        )

        pairs = detect_adjustment_pairs(debits, credits, "axis")

        # Should find at least one pair
        assert len(pairs) > 0
        # First pair should be high confidence
        assert pairs[0].confidence == "high"
        assert pairs[0].kind in ["exact_refund", "reversal"]

    def test_processor_mediated_refund(self):
        """Should handle processor-mediated refunds."""
        debits = assign_transaction_ids(
            [
                Transaction(
                    date="01/01/2024",
                    narration="VISA POS TXN AT IN/ZEPTO MARKETPLACE",
                    amount="1305.00",
                    transaction_type="debit",
                ),
            ],
            "axis",
        )

        credits = assign_transaction_ids(
            [
                Transaction(
                    date="05/01/2024",
                    narration="Refund Frm Razorpay Payments",
                    amount="1305.00",
                    transaction_type="credit",
                ),
            ],
            "axis",
        )

        pairs = detect_adjustment_pairs(debits, credits, "axis")

        # Should find pair despite low merchant similarity
        assert len(pairs) > 0
        # Exact amount + refund keyword should drive matching
        assert pairs[0].score > 0

    def test_missing_person_no_block(self):
        """Missing person should not block matching."""
        debits = assign_transaction_ids(
            [
                Transaction(
                    date="01/01/2024",
                    narration="SWIGGY",
                    amount="500.00",
                    card_number="1234",
                    person="JOHN DOE",
                    transaction_type="debit",
                ),
            ],
            "axis",
        )

        credits = assign_transaction_ids(
            [
                Transaction(
                    date="02/01/2024",
                    narration="REFUND SWIGGY",
                    amount="500.00",
                    card_number="1234",
                    # person missing
                    transaction_type="credit",
                ),
            ],
            "axis",
        )

        pairs = detect_adjustment_pairs(debits, credits, "axis")

        # Should still match on card + amount + keyword
        assert len(pairs) > 0
        assert pairs[0].score > 60

    def test_credit_balance_refund_onesided(self):
        """Should handle one-sided credit balance refund."""
        debits = assign_transaction_ids(
            [
                Transaction(
                    date="01/01/2024",
                    narration="CREDIT BALANCE REFUND",
                    amount="100.00",
                    reward_points="0",
                    transaction_type="debit",
                ),
            ],
            "axis",
        )

        credits = assign_transaction_ids([], "axis")

        pairs = detect_adjustment_pairs(debits, credits, "axis")

        # Should create one-sided pair
        assert len(pairs) > 0
        assert pairs[0].kind == "credit_balance_refund"
        assert pairs[0].credit is None
        assert pairs[0].debit is not None

    def test_normal_payment_exclusion_integration(self):
        """Normal payment credits should not create pairs."""
        debits = assign_transaction_ids(
            [
                Transaction(
                    date="01/01/2024",
                    narration="SWIGGY",
                    amount="1000.00",
                    transaction_type="debit",
                ),
            ],
            "axis",
        )

        credits = assign_transaction_ids(
            [
                Transaction(
                    date="02/01/2024",
                    narration="PAYMENT RECEIVED - THANK YOU",
                    amount="1000.00",
                    transaction_type="credit",
                ),
            ],
            "axis",
        )

        pairs = detect_adjustment_pairs(debits, credits, "axis")

        # Should not create pairs with payment credits
        assert len(pairs) == 0

    def test_no_refund_evidence_filtered(self):
        """A credit with no refund keyword and no merchant match should be filtered."""
        debits = assign_transaction_ids(
            [
                Transaction(
                    date="01/01/2024",
                    narration="AMAZON MARKETPLACE",
                    amount="1000.00",
                    card_number="1234",
                    person="JOHN DOE",
                    transaction_type="debit",
                ),
            ],
            "axis",
        )
        # A non-payment credit that happens to match on amount/card/person/date
        # but has no refund keyword and completely different merchant.
        credits = assign_transaction_ids(
            [
                Transaction(
                    date="02/01/2024",
                    narration="ONLINE PAYMENT HDFC",
                    amount="1000.00",
                    card_number="1234",
                    person="JOHN DOE",
                    transaction_type="credit",
                ),
            ],
            "axis",
        )

        pairs = detect_adjustment_pairs(debits, credits, "axis")

        # Should be filtered: no refund keyword, no merchant similarity
        assert len(pairs) == 0


class TestConfidenceLevels:
    """Test confidence level assignment."""

    def test_high_confidence(self):
        """Score >= 70 should be high confidence."""
        assert determine_confidence(80) == "high"
        assert determine_confidence(70) == "high"

    def test_medium_confidence(self):
        """Score >= 45 should be medium confidence."""
        assert determine_confidence(60) == "medium"
        assert determine_confidence(45) == "medium"

    def test_low_confidence(self):
        """Score < 45 should be low confidence."""
        assert determine_confidence(40) == "low"
        assert determine_confidence(20) == "low"


class TestMatchSelection:
    """Tests for non-overlapping pair selection."""

    def test_non_overlapping_selection(self):
        """One debit should appear in at most one selected pair."""
        debit = Transaction(
            date="01/01/2024",
            narration="SWIGGY",
            amount="500.00",
            card_number="1234",
            transaction_type="debit",
            transaction_id="d1",
        )
        credit1 = Transaction(
            date="02/01/2024",
            narration="REFUND SWIGGY",
            amount="500.00",
            card_number="1234",
            transaction_type="credit",
            transaction_id="c1",
        )
        credit2 = Transaction(
            date="03/01/2024",
            narration="REFUND SWIGGY",
            amount="500.00",
            card_number="1234",
            transaction_type="credit",
            transaction_id="c2",
        )

        pair1 = AdjustmentPair(
            pair_id="p1",
            debit_transaction_id="d1",
            credit_transaction_id="c1",
            debit=debit,
            credit=credit1,
            score=90,
            confidence="high",
            kind="exact_refund",
            amount_delta="0.00",
            reasons=["exact"],
        )
        pair2 = AdjustmentPair(
            pair_id="p2",
            debit_transaction_id="d1",
            credit_transaction_id="c2",
            debit=debit,
            credit=credit2,
            score=80,
            confidence="high",
            kind="exact_refund",
            amount_delta="0.00",
            reasons=["exact"],
        )

        selected = select_best_non_overlapping_pairs([pair1, pair2])
        assert len(selected) == 1
        assert selected[0].pair_id == "p1"

    def test_empty_input(self):
        """Empty input returns empty output."""
        assert select_best_non_overlapping_pairs([]) == []

    def test_integration_debit_not_duplicated(self):
        """detect_adjustment_pairs should not return overlapping pairs."""
        debits = assign_transaction_ids(
            [
                Transaction(
                    date="01/01/2024",
                    narration="SWIGGY BANGALORE",
                    amount="500.00",
                    card_number="1234",
                    transaction_type="debit",
                ),
            ],
            "axis",
        )
        credits = assign_transaction_ids(
            [
                Transaction(
                    date="02/01/2024",
                    narration="REFUND SWIGGY",
                    amount="500.00",
                    card_number="1234",
                    transaction_type="credit",
                ),
                Transaction(
                    date="03/01/2024",
                    narration="REFUND SWIGGY",
                    amount="500.00",
                    card_number="1234",
                    transaction_type="credit",
                ),
            ],
            "axis",
        )

        pairs = detect_adjustment_pairs(debits, credits, "axis")
        debit_ids = [p.debit_transaction_id for p in pairs if p.debit_transaction_id]
        # Each debit should appear at most once
        assert len(debit_ids) == len(set(debit_ids))

    def test_negative_score_filtered(self):
        """Pairs with score <= 0 should not be selected."""
        debit = Transaction(
            date="01/01/2024",
            narration="AMAZON",
            amount="500.00",
            transaction_type="debit",
            transaction_id="d1",
        )
        credit = Transaction(
            date="01/06/2024",
            narration="FLIPKART",
            amount="200.00",
            transaction_type="credit",
            transaction_id="c1",
        )
        pair = AdjustmentPair(
            pair_id="p1",
            debit_transaction_id="d1",
            credit_transaction_id="c1",
            debit=debit,
            credit=credit,
            score=-10,
            confidence="low",
            kind="possible_refund",
            amount_delta="300.00",
            reasons=["merchant_mismatch"],
        )
        selected = select_best_non_overlapping_pairs([pair])
        assert len(selected) == 0

    def test_empty_transaction_id_overlap(self):
        """Empty-string IDs don't participate in overlap tracking."""
        debit = Transaction(
            date="01/01/2024",
            narration="SWIGGY",
            amount="500.00",
            transaction_type="debit",
            transaction_id="",
        )
        credit1 = Transaction(
            date="02/01/2024",
            narration="REFUND SWIGGY",
            amount="500.00",
            transaction_type="credit",
            transaction_id="c1",
        )
        credit2 = Transaction(
            date="03/01/2024",
            narration="REFUND SWIGGY",
            amount="500.00",
            transaction_type="credit",
            transaction_id="c2",
        )
        pair1 = AdjustmentPair(
            pair_id="p1",
            debit_transaction_id="",
            credit_transaction_id="c1",
            debit=debit,
            credit=credit1,
            score=80,
            confidence="high",
            kind="exact_refund",
            amount_delta="0.00",
            reasons=["exact"],
        )
        pair2 = AdjustmentPair(
            pair_id="p2",
            debit_transaction_id="",
            credit_transaction_id="c2",
            debit=debit,
            credit=credit2,
            score=70,
            confidence="high",
            kind="exact_refund",
            amount_delta="0.00",
            reasons=["exact"],
        )
        selected = select_best_non_overlapping_pairs([pair1, pair2])
        # Both selected — empty-string IDs treated as "no ID"
        assert len(selected) == 2

    def test_multi_debit_multi_credit_selection(self):
        """3 debits, 3 credits: verify greedy non-overlapping selection."""
        pairs = [
            AdjustmentPair(
                pair_id="p1",
                debit_transaction_id="d1",
                credit_transaction_id="c1",
                debit=None,
                credit=None,
                score=90,
                confidence="high",
                kind="exact_refund",
                amount_delta="0.00",
                reasons=["exact"],
            ),
            AdjustmentPair(
                pair_id="p2",
                debit_transaction_id="d1",
                credit_transaction_id="c2",
                debit=None,
                credit=None,
                score=85,
                confidence="high",
                kind="exact_refund",
                amount_delta="0.00",
                reasons=["exact"],
            ),
            AdjustmentPair(
                pair_id="p3",
                debit_transaction_id="d2",
                credit_transaction_id="c2",
                debit=None,
                credit=None,
                score=80,
                confidence="high",
                kind="exact_refund",
                amount_delta="0.00",
                reasons=["exact"],
            ),
            AdjustmentPair(
                pair_id="p4",
                debit_transaction_id="d2",
                credit_transaction_id="c3",
                debit=None,
                credit=None,
                score=70,
                confidence="high",
                kind="exact_refund",
                amount_delta="0.00",
                reasons=["exact"],
            ),
            AdjustmentPair(
                pair_id="p5",
                debit_transaction_id="d3",
                credit_transaction_id="c3",
                debit=None,
                credit=None,
                score=60,
                confidence="medium",
                kind="exact_refund",
                amount_delta="0.00",
                reasons=["exact"],
            ),
        ]
        selected = select_best_non_overlapping_pairs(pairs)
        selected_ids = {p.pair_id for p in selected}
        # Greedy: p1(d1,c1) -> p3(d2,c2) -> p5(d3,c3)
        assert len(selected) == 3
        assert selected_ids == {"p1", "p3", "p5"}


class TestIsMalformed:
    """Tests for malformed transaction detection."""

    def test_zero_point_zero_amount(self):
        """Amount '0.00' should be treated as malformed."""
        txn = Transaction(
            date="01/01/2024",
            narration="X",
            amount="0.00",
            transaction_type="debit",
        )
        assert is_malformed(txn) is True

    def test_normal_amount(self):
        """Normal amount should not be malformed."""
        txn = Transaction(
            date="01/01/2024",
            narration="X",
            amount="500.00",
            transaction_type="debit",
        )
        assert is_malformed(txn) is False

    def test_empty_date(self):
        """Empty date should be malformed."""
        txn = Transaction(
            date="",
            narration="X",
            amount="500.00",
            transaction_type="debit",
        )
        assert is_malformed(txn) is True


class TestPaymentKeywordWordBoundary:
    """Test that payment keyword matching uses word boundaries."""

    def test_upi_as_word(self):
        """'UPI' as standalone word should be detected."""
        txn = Transaction(
            date="01/01/2024",
            narration="UPI PAYMENT RECEIVED",
            amount="1000.00",
            transaction_type="credit",
        )
        assert is_normal_payment_credit(txn) is True

    def test_upi_in_merchant_name(self):
        """'UPI' as substring of merchant should NOT be detected."""
        txn = Transaction(
            date="01/01/2024",
            narration="JUPITERCARD REFUND",
            amount="1000.00",
            transaction_type="credit",
        )
        assert is_normal_payment_credit(txn) is False

    def test_neft_as_word(self):
        """'NEFT' as standalone word should be detected."""
        txn = Transaction(
            date="01/01/2024",
            narration="NEFT PAYMENT",
            amount="1000.00",
            transaction_type="credit",
        )
        assert is_normal_payment_credit(txn) is True


class TestMerchantSimilarityEmptyNormalization:
    """Test similarity when normalization strips narration to empty."""

    def test_empty_after_normalization_neutral(self):
        """When one narration normalizes to empty, similarity should be neutral."""
        sim = merchant_similarity("", "SWIGGY BANGALORE")
        assert sim >= 0.3  # neutral, not penalized to 0.0

    def test_both_empty_neutral(self):
        """Both empty narrations should return neutral."""
        sim = merchant_similarity("", "")
        assert sim >= 0.3


class TestCreditBalanceRefundExclusion:
    """Test that credit balance refund debits don't double-match."""

    def test_onesided_debit_excluded_from_regular_pairing(self):
        """A CREDIT BALANCE REFUND debit should only appear in one pair."""
        debits = assign_transaction_ids(
            [
                Transaction(
                    date="01/01/2024",
                    narration="CREDIT BALANCE REFUND",
                    amount="500.00",
                    reward_points="0",
                    transaction_type="debit",
                ),
            ],
            "axis",
        )
        credits = assign_transaction_ids(
            [
                Transaction(
                    date="02/01/2024",
                    narration="SOME REFUND",
                    amount="500.00",
                    transaction_type="credit",
                ),
            ],
            "axis",
        )

        pairs = detect_adjustment_pairs(debits, credits, "axis")
        debit_ids = [p.debit_transaction_id for p in pairs if p.debit_transaction_id]
        # The debit should appear at most once (only in the one-sided pair)
        assert len(debit_ids) == len(set(debit_ids))
        # Should have exactly the one-sided pair
        onesided = [p for p in pairs if p.kind == "credit_balance_refund"]
        assert len(onesided) == 1

    def test_credit_balance_refund_zero_point_zero_rewards(self):
        """reward_points='0.00' should still trigger one-sided pair."""
        debits = assign_transaction_ids(
            [
                Transaction(
                    date="01/01/2024",
                    narration="CREDIT BALANCE REFUND",
                    amount="100.00",
                    reward_points="0.00",
                    transaction_type="debit",
                ),
            ],
            "axis",
        )
        credits = assign_transaction_ids([], "axis")
        pairs = detect_adjustment_pairs(debits, credits, "axis")
        assert len(pairs) > 0
        assert pairs[0].kind == "credit_balance_refund"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
