# Risks, Gaps, and Recommendations

## High-priority observations

### 1) Historical UI import issue has been resolved
- `turnpoint_purger_ui.py` now includes `import os` while using `os.getenv(...)`.
- There is no active `NameError` risk from this item at present.

### 2) Version metadata is inconsistent
- `pyproject.toml` declares `3.0.1`.
- `README.md` references `3.0.1`.
- `importcsv.py` sets `APP_VERSION = "2.0.2"` used by the UI badge.
- Risk: operators and support staff see conflicting version info.

### 3) Hardcoded Nexis default password in mapper
- `nexis_uploader.py` defines `DEFAULT_PASSWORD = "Circle@2024"`.
- Risk: insecure default credential behavior and policy mismatch with secure environments.

## Medium-priority observations

### 4) Extensive global mutable state in `importcsv.py`
- Globals track client id, output path, sequence prefix, runtime credentials, etc.
- Risk: brittle behavior under concurrent use, hard-to-test logic boundaries.

### 5) Broad exception handling in extraction loops
- Per-page extraction errors are logged and suppressed.
- Risk: partial archives can look successful without explicit completeness metrics.

### 6) Selector and DOM-coupling fragility
- Selenium selectors are tightly coupled to exact page shape/text.
- Risk: minor portal UI changes can silently break one path while others still run.

### 7) Limited automated coverage for integration-critical paths
- Tests focus on helper functions.
- Risk: regressions in extraction, state mutation, and upload flow may go undetected.

## Lower-priority observations

### 8) Budget auto-detection depends on current working directory
- `NDISBUDGETER.auto_detect_excel_file()` begins from `Path.cwd()`.
- Risk: running from unexpected folders may miss latest archive exports.

### 9) Duplicate behavior differs by domain
- Clients: duplicate can be overridden.
- Workers: duplicate is always skipped via runtime error.
- Risk: inconsistent operator expectations across tabs.

## Recommendations by implementation horizon

### Immediate
1. Keep import-sanity checks in CI/lint so missing-import regressions are caught early.
2. Align version string source so UI, package metadata, and docs match.
3. Remove hardcoded default password and source it from secure config.

### Near-term
1. Introduce run summary reports (pages succeeded/failed, download counts).
2. Add structured logging mode for easier support diagnostics.
3. Expand unit tests for state, manifests, payload mapping, and worker extraction helpers.

### Mid-term
1. Refactor large procedural modules toward explicit context objects.
2. Centralize selectors and constants to reduce maintenance cost.
3. Add smoke-test harnesses for TurnPoint and Nexis DOM health checks.

## Operator-facing caution
Until completeness metrics and stronger integration tests are in place, treat each run as “needs spot-check” rather than “fully guaranteed complete.” The current system is effective in practice but still sensitive to external UI drift and credential/session conditions.
