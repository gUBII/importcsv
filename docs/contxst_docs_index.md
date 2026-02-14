# Repository Context Documentation Index

## Why this document set exists
This repository is a Selenium-based operations toolkit for TurnPoint and Nexis workflows. The docs in this folder are the current baseline for engineering and operations.

## Scope of the current baseline
This set reflects the implemented code as of today, including:
- Client and worker purge pipelines.
- PDCC purgeable/bundle/manifest workflows.
- Service Type reference extraction (`service_type_rate_extractor.py`).
- Service Type variants truth extraction from Assist appointment editor (`appointment_item_discovery.py`).
- CLI and build/distribution behavior.

## Reading order
1. `docs/contxst_repository_overview.md`
2. `docs/contxst_architecture_and_dataflow.md`
3. `docs/contxst_module_reference.md`
4. `docs/contxst_configuration_and_paths.md`
5. `docs/contxst_cli_and_gui_workflows.md`
6. `docs/contxst_build_and_release.md`
7. `docs/contxst_testing_and_quality.md`
8. `docs/contxst_risks_and_recommendations.md`

## Fast orientation
- CLI entrypoint: `importcsv.py` (`turnpoint-purger-cli`).
- GUI entrypoint: `turnpoint_purger_ui.py` (`turnpoint-purger-gui`).
- New discovery module: `appointment_item_discovery.py`.
- Variants extraction runbook: `docs/service_type_variants_runbook.md`.
- Variants extraction route contract: TP1 `Add Appointment` nested iframe path is required; direct Assist URL context is unsupported.
- Core archives: `~/PurgedClients`, `~/PurgedWorker`, PDCC root, and `~/LineItemRates`.
- Variant extraction diagnostics:
  - `~/LineItemRates/ServiceTypeTruth/variants/diagnostics/<run_id>/events.jsonl`
  - `~/LineItemRates/ServiceTypeTruth/variants/diagnostics/<run_id>/checkers.csv`
  - HTML/PNG/console artifacts in the same run directory

## Legacy docs note
These files still exist and are kept aligned at a high level:
- `docs/TurnpointPurger_Notes.md`
- `docs/TurnpointPurger_Technical_Documentation.md`
- `docs/TurnpointPurger Automation Suite – Technical Documentation.docx`
