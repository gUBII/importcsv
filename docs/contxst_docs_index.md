# Repository Context Documentation Index

## Why this document set exists
This repository has grown from a single client purge script into a multi-surface automation suite:
- Client data extraction and archival from TurnPoint.
- Worker data extraction and archival from TurnPoint.
- Nexis payload generation and upload assistance.
- Packaging scripts for distributable GUI/CLI builds.

This index is the entry point for the full context pack. Every document in this set uses the requested naming format (`contxst_{docname}.md`) and lives in `docs/`.

## Scope of this context refresh
This documentation was assembled by reading source-of-truth code and runtime files in the repository, not only the previous markdown docs. It covers:
- Core runtime modules.
- GUI and CLI flows.
- State persistence model.
- Build and packaging flow.
- Testing footprint and known gaps.
- Risks and practical recommendations.

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
If you only need the short version:
- Entry points are `importcsv.py` (CLI), `turnpoint_purger_ui.py` (GUI), `worker_purger.py` (worker flow), and `nexis_uploader.py`/`nexis_submitter.py` (Nexis mapping + submission).
- Primary persistent state is JSON under `~/.turnpoint_purger/`.
- Primary archive roots are `~/PurgedClients` and `~/PurgedWorker` unless overridden by env vars.
- Package discovery and bundle exports are stored under the PDCC directory (`Purged Client/Package Divided Client Credential (PDCC)`).

## Notes on legacy docs
This repository already contains prior documentation artifacts:
- `docs/TurnpointPurger_Notes.md`
- `docs/TurnpointPurger_Technical_Documentation.md`
- `docs/TurnpointPurger Automation Suite – Technical Documentation.docx`

Those files remain useful historical context, but this `contxst_*.md` set is intended to be the current working baseline for engineering and operations.
