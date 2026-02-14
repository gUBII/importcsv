# Service Type Variants Runbook

## Purpose

This runbook covers reliable extraction of Service Type variants from Assist / appointment editor pages, including diagnostics, selector maintenance, smoke checks, and validation.

## Preconditions

- TurnPoint credentials are configured and valid.
- Reference Service Type export exists at:
  - `~/LineItemRates/ServiceTypeTruth/reference/latest/ServiceTypes_latest.csv`, or
  - `~/LineItemRates/ServiceTypeTruth/reference/latest/ServiceTypes_latest.xlsx`
- Probe client ID is known (recommended for TP1 bridge route).

## What "success" means

- Assist open is only marked `PASS` when Service Type combobox is present and interactable.
- Each Service Type outputs:
  - `Parent Service Type ID`
  - `Parent Service Type Label`
  - `Service Variant Label`
  - `Rate` + `Rate (Raw)`
  - `Code` + `Code (Raw)`
  - `Unit`
  - `Status` + `Error Reason`
- XLSX merges parent columns (`A` + `B`) across contiguous variant rows.

## Smoke mode (single Service Type)

Run a one-item smoke probe:

```bash
cd /Users/moofasa/importcsv
python - <<'PY'
from appointment_item_discovery import extract_service_type_variants
result = extract_service_type_variants(
    headless=True,
    probe_client_ids="92108",
    smoke_mode=True,
    smoke_service_type_label="Active Night-Time",
)
print(result["output_paths"])
PY
```

If the label is omitted or not found, smoke mode uses the first Service Type from the reference index.

## Selector maintenance (using ATLAS facts)

Current selector priorities:

1. Service Type combobox:
   - `data-testid`
   - `name`
   - `aria-label`
   - `input[role="combobox"][aria-haspopup="true"]`
   - label-adjacent XPath fallback
2. Variant table:
   - `data-testid`
   - named table
   - table with `Service` header
   - first table after `Service Type`
   - fallback any table

When ATLAS reports DOM drift:

1. Update selector constants in `appointment_item_discovery.py`:
   - `SERVICE_TYPE_LOCATOR_STRATEGIES`
   - `SERVICE_TYPE_OPTION_LOCATOR_STRATEGIES`
   - `VARIANT_TABLE_LOCATOR_STRATEGIES`
2. Keep at least 3 fallbacks for critical elements.
3. Re-run smoke mode and confirm artifacts/checkers.

## Diagnostics and artifacts

Per run directory:

- `~/LineItemRates/ServiceTypeTruth/variants/diagnostics/<run_id>/events.jsonl`
- `~/LineItemRates/ServiceTypeTruth/variants/diagnostics/<run_id>/checkers.csv`
- HTML / PNG artifacts saved for:
  - Assist-open failures
  - per-Service-Type selection failures
  - missing/empty variant blocks
  - unhandled run failures

No early-failure path is allowed to skip diagnostics persistence.

## Validation against UI

For a known service type with variants (example: `Active Night-Time`):

1. Open appointment editor and select the same Service Type manually.
2. Compare table row count to extracted PASS rows for that parent service type.
3. Spot-check at least 3 rows:
   - `Service Variant Label`
   - `Rate (Raw)` and normalized `Rate`
   - `Code (Raw)` and normalized `Code`

## Golden output example

Expected style (example values):

```text
Parent Service Type ID: 7358
Parent Service Type Label: (SIL) Active Night-Time (12 am to 6 am)
Service Variant Label: Weekday Daytime/Individual Code
Rate: 78.81
Rate (Raw): $ 78.81 / hour
Code: 01_803_0115_1_1
Code (Raw): 01_803_0115_1_1
Unit: / hour
Status: PASS
Error Reason:
```
