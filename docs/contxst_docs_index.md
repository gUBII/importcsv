# Repository Context Documentation Index

## Why this document set exists
This repository is a Selenium-based operations toolkit for TurnPoint and Nexis workflows. The docs in this folder are the current baseline for engineering and operations.

## Scope of the current baseline
This set reflects the implemented code as of today, including:
- Client and worker purge pipelines.
- PDCC purgeable/bundle/manifest workflows.
- Service Type reference extraction (`service_type_rate_extractor.py`).
- Appointment-driven item-number discovery + service-type enrichment (`appointment_item_discovery.py`).
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
- Core archives: `~/PurgedClients`, `~/PurgedWorker`, and PDCC root.
- New diagnostics for item discovery:
  - `ServiceTypeRateExtractor/diagnostics/<run_id>/events.jsonl`
  - `ServiceTypeRateExtractor/diagnostics/<run_id>/checkers.csv`
  - `ServiceTypeRateExtractor/diagnostics/<run_id>/summary.json`

## Legacy docs note
These files still exist and are kept aligned at a high level:
- `docs/TurnpointPurger_Notes.md`
- `docs/TurnpointPurger_Technical_Documentation.md`
- `docs/TurnpointPurger Automation Suite – Technical Documentation.docx`
