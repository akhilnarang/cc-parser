# Parsing Notes

This document explains the statement structures the parser handles and the normalization logic used by this project.

## Scope

- Input: password-protected or plain PDF credit card statements.
- Supported parsing profiles: `hdfc`, `icici`, `sbi`, `idfc`, `generic`.
- Output: normalized JSON for transactions, totals, and reconciliation.

## Common PDF Patterns

Credit card statement PDFs typically contain a mix of:

- Header summary region (`Total Amount Due`, `Due Date`, limits).
- Transaction tables (date, reference, narration, points, amount).
- Supplementary/add-on sections separated by card number headings.
- Informational sections that look tabular but are not transactions.

Extraction caveats that parser accounts for:

- Wrapped rows where date/amount and narration may split across lines.
- OCR/font artifacts (`(cid:...)`, duplicated characters, broken spacing).
- Mixed separators (`|`, irregular spaces, merged tokens).
- Credit markers (for example, `CR` or bare `C`/`D` for SBI, `DR`/`CR` for IDFC) that indicate refunds/payments.

## Normalized Output Model

Key top-level fields in compact output:

- `name`
- `card_number`
- `due_date`
- `statement_total_amount_due`
- `transactions` (debit side)
- `payments_refunds` (credit side)
- `card_summaries`
- `person_groups`
- `overall_total` (debits only)
- `overall_reward_points` (debits only)
- `reconciliation`

## Transaction Parsing Strategy

1. Extract words with coordinates from each page.
2. Group words into visual lines by y-position tolerance.
3. Detect transaction candidates by date + amount signatures.
4. Parse optional time, rewards, narration, amount.
5. Classify rows as debit or credit using table/context signals.
6. Attach card/person context based on nearby section headers.

## Add-on Handling

- Card switches are detected from masked card rows in statement tables.
- If add-on names are unavailable or low-confidence, labels are generated as:
  - `ADDON <last 4 digits of addon>`

## Reconciliation

`reconciliation` provides quick sanity checks between:

- statement-level due amount,
- parsed debit/credit totals,
- and derived deltas.

The **smart delta** (`smart_expected_total`) accounts for previous balance:

    expected = previous_balance + parsed_debits + fees - parsed_credits

A near-zero smart delta confirms all transactions are captured.

When previous balance exists and credits cover it, the reconciliation also reports:

- **Previous Balance Cleared On**: the date cumulative credits first exceeded the previous balance.
- **Excess Paid After Clearing**: `total_credits - previous_balance` — the portion that went toward current-cycle charges.

This is meant for auditing parser behavior and identifying edge cases.

## Design Rule

Parsing logic avoids dependence on account-specific values and does not require embedding any real statement content.
