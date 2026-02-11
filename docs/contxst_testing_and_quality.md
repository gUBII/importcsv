# Testing and Quality Context

## Current automated tests
The repository includes focused unit-style test modules:

1. `tests/test_appointment_item_discovery.py`
- Verifies checker transitions (success/missing/empty paths).
- Verifies Assist route handling and option parsing helpers.
- Verifies details-page enrichment mapping (`ef581`, `ef592`).
- Verifies merge precedence (label-first, ID fallback).

2. `tests/test_package_collector.py`
- Verifies `_extract_client_rows_from_elements` behavior.
- Verifies `_write_package_manifest` output shape and ordering.

3. `tests/test_purgeable_helpers.py`
- Verifies purgeable URL override/default precedence.
- Verifies purgeable page 404 detection helper behavior.

## What is currently covered well
- Appointment discovery parsing, checker semantics, and merge rules.
- Pure helper logic with deterministic inputs.
- CSV writer structure for package manifest.
- Basic URL and error-checking helpers.

## What is currently not covered
- Full Selenium workflows against TurnPoint.
- Full Selenium workflows against Nexis.
- End-to-end client purge output contract.
- End-to-end worker purge output contract.
- GUI interactions and threading behavior.
- State mutation semantics under concurrent operations.

## Practical testing constraints
- Browser automation tests require real credentials and predictable remote DOMs.
- Portal UI changes can invalidate selectors quickly.
- Local execution environments need Chrome/driver compatibility.

## Suggested quality gates for operational confidence

### Lightweight gates (safe for CI)
- `pytest` for helper coverage.
- Basic import checks for all major modules.
- Lint/type checks (if adopted).

### Environment-aware gates (manual or secured CI)
- Smoke test: login + one page fetch (TurnPoint).
- Smoke test: Nexis login + form availability.
- Dry-run report mode for selectors and page anchors.

## Reliability characteristics from code review
- Many flows recover from individual failures and continue processing.
- Discovery workflow has structured diagnostics (`events.jsonl`, `checkers.csv`, `summary.json`).
- Other legacy flows still rely primarily on operator-oriented text logs.
- Global mutable state can become fragile in concurrent or long multi-step sessions.
- Duplicate handling is stronger in client flow than worker flow (which hard-skips duplicates).

## Suggested near-term test additions
1. State file tests:
- monotonic sequence progression
- history capping logic
- duplicate metadata retrieval

2. Manifest parser tests:
- malformed headers
- mixed-case aliases
- empty rows and whitespace normalization

3. Worker payload tests:
- field alias resolution
- checkbox/radio normalization
- date parsing edge cases

4. Nexis mapper tests:
- department mapping fallback behavior
- missing required field fallbacks
- account type and designation mapping
