# Output Schema

This document describes the normalized parser output.

## Top-Level Fields

- `file` (`string`): input PDF file name only (basename, not full local path).
- `bank` (`string`): parser profile (`hdfc`, `icici`, `sbi`, `idfc`, `indusind`, `hsbc`, `axis`, `jupiter`, `slice`, `ssfb`, `bob`, `yesbank`, or `generic`).
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
- `overall_reward_points` (`string`): reward points earned this cycle. Prefers the statement-declared total when available (e.g. ICICI "Total Points earned*", HSBC "Earned"); otherwise falls back to the per-transaction sum.
- `reward_points_balance` (`string | null`): cumulative reward points balance (when available, e.g. Axis eDGE, HSBC closing balance).
- `reward_points_line_total` (`string | null`): sum of per-transaction reward points only. Populated when the parser used a statement-declared figure for `overall_reward_points`, so the per-line sum remains visible for reconciliation.
- `reward_points_bonus` (`string | null`): accelerated/bonus points credited this cycle that aren't visible in per-transaction rows (e.g. ICICI iShop 5× bonus, HDFC's "Rewards Program Points Summary" declared Total). Already included in `overall_reward_points`; exposed separately for transparency. Sourced from the statement's declared figure where available.
- `reward_points_bonus_breakdown` (`array | null`): itemized list of bonus programs when the statement exposes one (e.g. HDFC's "Rewards Program Points Summary" table). Each entry is `{ program: string, points: string }`. The sum of entries is typically equal to `reward_points_bonus`, but can diverge when the statement's declared Total already nets out reversal rows (e.g. HDFC March `SmartBuy_Reversal` -150 pts).
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

Fields:

- `statement_total_amount_due` (`string | null`)
- `header_previous_balance` (`string`): previous balance from account summary.
- `parsed_debit_total` (`string`)
- `parsed_credit_total` (`string`)
- `parsed_net_due_estimate` (`string`): debits minus credits.
- `smart_expected_total` (`string`): `previous_balance + debits + fees - credits`.
- `smart_delta` (`string`): `statement_total - smart_expected`. Near zero when all transactions are captured.
- `prev_balance_cleared_date` (`string | null`): date when cumulative credits first exceeded previous balance.
- `excess_paid_after_clearing` (`string | null`): `total_credits - previous_balance` (portion toward current-cycle charges).
- `header_purchases_debit`, `header_finance_charges`, `header_payments_credits_received`, `header_computed_due_estimate`: raw summary fields.
- `delta_statement_vs_parsed_debit`, `delta_statement_vs_parsed_net`, `delta_statement_vs_header_estimate`: legacy deltas.
- `summary_amount_candidates` (`array`): raw amounts found in summary area.

## Verbosity Modes

- `-v`: writes compact parsed output.
- `-vv`: writes `{ parsed, debug }`.
- `-vvv`: writes `{ parsed, debug, raw }`.

Privacy note:

- `-vvv` and `--export-raw-json` include raw page text, token coordinates, and metadata from the source PDF.
- Treat exported JSON/CSV as sensitive statement data even though card numbers are masked.

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
