# Service Type Variants Runbook

## Purpose

This runbook covers reliable extraction of Service Type variants from Assist / appointment editor pages, including diagnostics, selector maintenance, smoke checks, and validation.

## Preconditions

- TurnPoint credentials are configured and valid.
- Reference Service Type export exists at:
  - `~/LineItemRates/ServiceTypeTruth/reference/latest/ServiceTypes_latest.csv`, or
  - `~/LineItemRates/ServiceTypeTruth/reference/latest/ServiceTypes_latest.xlsx`
- Probe client ID is known (required for TP1 Add Appointment nested-iframe route).

## What "success" means

- Route is only marked `PASS` when the TP1 Add Appointment nested iframe editor is active:
  - outer iframe: `iframe[src*='appointment-edit.asp']`
  - inner iframe: `iframe[src*='assist.turnpoint.co/appointments/new'][src*='has_parent=true']`
- Direct Assist URL (`assist.turnpoint.co/appointments/new` without `has_parent=true` + client context) is unsupported for variant extraction.
- Capability gate must pass:
  - reload anchor exists: `div[data-cy='service_type_id-reload']`
  - service input exists via primary XPath:
    - `//div[@data-cy='service_type_id-reload']/preceding::input[@role='combobox'][1]`
  - at least `day_rate-input` + `day_code-input` exist after sentinel selection.
- Each Service Type outputs:
  - `Parent Service Type ID`
  - `Parent Service Type Label`
  - `Parent Service Type` (alias)
  - `Service Type ID` (alias)
  - `Service Variant Label`
  - `Rate` + `Rate (Raw)`
  - `Code` + `Code (Raw)`
  - `Item Number` (alias)
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

## Resume semantics

- `resume=True` + `force_refresh=False`:
  - auto-resumes the latest `status=in_progress` checkpoint for matching probe clients/mode
  - treats `processed_service_type_ids` as "attempted" (both previous PASS and FAIL), so resume does not re-attempt prior failures
- `force_refresh=True`:
  - bypasses auto-resume and starts a fresh run id
  - re-attempts all queued Service Types (use this when you intentionally want to retry failures)

## Selector maintenance (using ATLAS facts)

Current selector priorities:

1. Service Type combobox:
   - `//div[@data-cy='service_type_id-reload']/preceding::input[@role='combobox'][1]`
   - workflow: click -> clear -> type -> listbox wait -> Enter -> `aria-expanded=false`
   - fallback: click exact `div[role='option']` label
2. Variants grid:
   - fixed pairs via `input[data-cy='{prefix}_rate-input']` + `input[data-cy='{prefix}_code-input']`
   - prefixes: `day`, `eve`, `night`, `saturday`, `sunday`, `ph`

When ATLAS reports DOM drift:

1. Update selector constants in `appointment_item_discovery.py`:
   - `SERVICE_TYPE_INPUT_XPATH`
   - `OUTER_APPOINTMENT_IFRAME_CSS`
   - `INNER_ASSIST_IFRAME_CSS`
   - fixed variant input `data-cy` prefixes
2. Keep at least 3 fallbacks for critical elements.
3. Re-run smoke mode and confirm artifacts/checkers.

## Diagnostics and artifacts

Per run directory:

- `~/LineItemRates/ServiceTypeTruth/variants/diagnostics/<run_id>/events.jsonl`
- `~/LineItemRates/ServiceTypeTruth/variants/diagnostics/<run_id>/checkers.csv`
- HTML / PNG artifacts saved for:
  - TP1 Add Appointment route and iframe-switch failures
  - per-Service-Type selection failures
  - stale fingerprint / missing variant input pairs
  - unhandled run failures

No early-failure path is allowed to skip diagnostics persistence.

## Validation against UI

For a known service type with variants (example: `Active Night-Time`):

1. Open appointment editor and select the same Service Type manually.
2. Compare the six expected variant rows (`day/eve/night/saturday/sunday/ph`) to extracted PASS rows for that parent service type.
3. Spot-check at least 3 rows:
   - `Service Variant Label`
   - `Rate (Raw)` and normalized `Rate`
   - `Code (Raw)` and normalized `Code`

## Golden output example

Expected style (example values):

```text
Parent Service Type ID: 7358
Parent Service Type Label: (SIL) Active Night-Time (12 am to 6 am)
Parent Service Type: (SIL) Active Night-Time (12 am to 6 am)
Service Type ID: 7358
Service Variant Label: Weekday Daytime/Individual Code
Rate: 78.81
Rate (Raw): $ 78.81 / hour
Code: 01_803_0115_1_1
Code (Raw): 01_803_0115_1_1
Item Number: 01_803_0115_1_1
Unit: / hour
Status: PASS
Error Reason:
```
