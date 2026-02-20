# Output Schema

This document describes the normalized parser output.

## Top-Level Fields

- `file` (`string`): input PDF path.
- `bank` (`string`): parser profile (`hdfc`, `icici`, or `generic`).
- `name` (`string | null`): primary cardholder name.
- `card_number` (`string | null`): primary masked card number.
- `due_date` (`string | null`): detected due date.
- `statement_total_amount_due` (`string | null`): statement-level due amount.
- `transactions` (`array`): debit-side transactions.
- `payments_refunds` (`array`): credit-side transactions.
- `payments_refunds_total` (`string`): sum of credit-side transactions.
- `adjustments` (`array`): offsetting debit/credit pairs excluded from spend/payment totals.
- `adjustments_debit_total` (`string`): sum of debit-side adjustments.
- `adjustments_credit_total` (`string`): sum of credit-side adjustments.
- `card_summaries` (`array`): aggregated totals by `person + card_number`.
- `person_groups` (`array`): grouped debit transactions by person.
- `overall_total` (`string`): debit total only.
- `overall_reward_points` (`string`): debit-side reward points total.
- `reconciliation` (`object`): audit metrics and deltas.

## Transaction Object

Each row in `transactions` and `payments_refunds` includes:

- `date` (`string`)
- `time` (`string | null`)
- `narration` (`string`)
- `reward_points` (`string | null`)
- `amount` (`string`)
- `card_number` (`string | null`)
- `person` (`string | null`)
- `transaction_type` (`"debit" | "credit"`)
- `credit_reasons` (`string`, optional for credits)

## Aggregates

### `card_summaries[]`

- `card_number`
- `person`
- `transaction_count`
- `total_amount`
- `reward_points_total`

### `person_groups[]`

- `person`
- `transaction_count`
- `total_amount`
- `reward_points_total`
- `transactions` (debit rows for that person)

## Reconciliation

`reconciliation` is intended for diagnostics, not hard validation.
Current fields include parsed totals and deltas against statement-level fields.

## Verbosity Modes

- `-v`: writes compact parsed output.
- `-vv`: writes `{ parsed, debug }`.
- `-vvv`: writes `{ parsed, debug, raw }`.

## CSV Export

`--export-csv` writes flattened rows suitable for Google Sheets with fields:

- `source` (`transactions`, `payments_refunds`, `adjustments`)
- `person`, `card_number`, `date`, `time`, `narration`, `reward_points`
- `amount`, `amount_numeric`, `signed_amount`
- `spend_amount` (debit-only spend)
- `credit_amount` (credit-side values)

Notes for Sheets:

- Use `source = transactions` for per-person spend totals.
- `source = payments_refunds` contains credit rows.
- `source = adjustments` contains offsetting non-spend adjustment rows.
