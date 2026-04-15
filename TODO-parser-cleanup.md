# Parser cleanup backlog

## axis
- Current state: custom StatementParser subclass, 875 lines.
- Why didn't migrate to GenericParser: transaction parsing is tightly coupled to Axis-specific member headers, merchant-category stripping, and Cr/Dr suffix handling in one pass.
- Sketch to finish later: extract Axis line tokenization + merchant-category handling behind GenericParser transaction hooks before switching the class hierarchy.

## bob
- Current state: custom StatementParser subclass, 615 lines.
- Why didn't migrate to GenericParser: the parser is table-first rather than line-first, with newline-packed cells and member headers embedded inside a single particulars column.
- Sketch to finish later: add a GenericParser table-row hook or a sibling table-oriented base for columnar statements before migrating BOB.

## hsbc
- Current state: custom StatementParser subclass, 954 lines.
- Why didn't migrate to GenericParser: credit detection, summary extraction, and transaction segmentation remain intertwined with HSBC-specific column quirks.
- Sketch to finish later: isolate HSBC section detection and CR-suffix classification into reusable override points, then port incrementally.

## idfc
- Current state: custom StatementParser subclass, 734 lines.
- Why didn't migrate to GenericParser: it needs three-token dates, r-prefixed amounts, and summary extraction keyed by x-position, all inside one custom pipeline.
- Sketch to finish later: route GenericParser amount/date hooks through configurable token strategies, then lift IDFC's summary extractor over as an override.

## indusind
- Current state: custom StatementParser subclass, 749 lines.
- Why didn't migrate to GenericParser: statement-specific credits, summary filtering, and card/member context are still entangled with its transaction scan.
- Sketch to finish later: split IndusInd's transaction scan into line-context, amount/date parsing, and summary filtering hooks before swapping the base class.

## jupiter
- Current state: custom StatementParser subclass, 708 lines.
- Why didn't migrate to GenericParser: Jupiter's separate time lines, amount tokens without decimals, and narration-keyword credit detection need broader transaction-shape hooks.
- Sketch to finish later: add configurable amount/time extraction stages to GenericParser, then move Jupiter over once the hooks exist.

## slice
- Current state: custom StatementParser subclass, 683 lines.
- Why didn't migrate to GenericParser: Slice depends on section-bounded parsing with bank-specific summary and credit heuristics that do not map cleanly to the shared line parser yet.
- Sketch to finish later: split section discovery from row parsing so Slice can override only the section boundaries and summary extraction.

## ssfb
- Current state: custom StatementParser subclass, 234 lines.
- Why didn't migrate to GenericParser: SSFB is short, but it relies on page-bounded summary parsing and MITC/example-page isolation that would need extra GenericParser hooks first.
- Sketch to finish later: add page-filter and summary-source hooks, then consider migrating SSFB if that still simplifies the code.

## yesbank
- Current state: custom StatementParser subclass, 906 lines.
- Why didn't migrate to GenericParser: YES BANK has statement-bounded transaction sections, merchant-category columns, and intertwined summary parsing that would be brittle to force into the current shared pipeline.
- Sketch to finish later: extract YES BANK sectioning and category-column handling into dedicated hooks before attempting the base-class change.
