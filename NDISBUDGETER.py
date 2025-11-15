import os
import re
from pathlib import Path
import pandas as pd

try:
    from purger_state import DEFAULT_START_ID, get_purge_statistics
except ImportError:
    DEFAULT_START_ID = 100001
    get_purge_statistics = None


def _get_purger_stats():
    if not get_purge_statistics:
        return None
    try:
        return get_purge_statistics()
    except Exception:
        return None


def auto_detect_excel_file():
    """Return the most relevant Excel export for the universal client, if present."""
    root = Path.cwd()
    stats = _get_purger_stats()
    preferred_prefixes = []

    candidate_dirs = []
    if stats:
        last_sequence = max(DEFAULT_START_ID, stats["next_universal_id"] - 1)
        if last_sequence >= DEFAULT_START_ID:
            preferred_prefixes.append(f"{last_sequence} ")
            candidate_dirs.extend(root.glob(f"{last_sequence}*"))

    if not candidate_dirs:
        candidate_dirs = [
            d for d in root.iterdir()
            if d.is_dir() and d.name[: len(str(DEFAULT_START_ID))].isdigit()
        ]

    candidate_dirs = sorted(
        {d for d in candidate_dirs if d.is_dir()},
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    search_dirs = candidate_dirs or [root]

    scoring = []
    for directory in search_dirs:
        for ext in ("*.xlsx", "*.xls"):
            for path in directory.glob(ext):
                if path.name.startswith("~$"):
                    continue  # skip temporary Excel locks
                name_lower = path.name.lower()
                score = 0
                if any(path.name.startswith(prefix) for prefix in preferred_prefixes):
                    score += 2
                if "budget" in name_lower:
                    score += 1
                scoring.append((score, path.stat().st_mtime, path))

    if not scoring:
        return None

    scoring.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scoring[0][2]


def process_budget_excel(excel_path, sheet_name=None, export_folder=None, quiet=False):
    """
    Parse a TurnPoint budget export into CSVs.

    Returns a dict containing summary information so callers can react programmatically.
    """

    def log(message):
        if not quiet:
            print(message)

    excel_path = Path(excel_path).expanduser().resolve()
    if not excel_path.exists():
        raise FileNotFoundError(f"Excel file not found: {excel_path}")

    log("\n📄 Using Excel file: {}".format(excel_path))

    # --- Load Excel workbook ---
    log("🔍 Reading workbook and listing sheets...")
    excel_file = pd.ExcelFile(excel_path)
    log(f"   ✅ Sheets found: {excel_file.sheet_names}")

    if sheet_name:
        selected_sheet = sheet_name
    else:
        selected_sheet = excel_file.sheet_names[0]

    log(f"\n📖 Reading sheet: '{selected_sheet}' ...")
    df_raw = pd.read_excel(excel_path, sheet_name=selected_sheet, header=None, dtype=str)
    df_raw = df_raw.fillna("").astype(str).apply(lambda col: col.map(lambda x: x.strip()))
    log("   ✅ Sheet loaded successfully.")

    # --- Create export folder ---
    if export_folder:
        export_path = Path(export_folder).expanduser().resolve()
    else:
        export_path = excel_path.parent / "NDIS_Budget_Exports"
    export_path.mkdir(parents=True, exist_ok=True)
    log(f"\n📂 Export folder (created/used): {export_path}")

    # --- Save full raw sheet as backup ---
    main_path = export_path / "Main_Agreement.csv"
    df_raw.to_csv(main_path, index=False, header=False)
    log("   ✅ Saved full raw sheet backup to:")
    log(f"      {main_path}")

    # --- Prepare entry folder ---
    entry_folder = export_path / "Entries"
    entry_folder.mkdir(parents=True, exist_ok=True)
    log(f"\n📁 Entry CSVs will be saved in:\n   {entry_folder}")

    # --- Parse and export each “Agreement entry” ---
    log("\n🧩 Parsing agreement entries and day rows...")

    entries_exported = 0
    current_name = None
    header = None
    entry_rows = []
    day_names = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}

    def flush_entry():
        nonlocal entries_exported, entry_rows, header, current_name
        if current_name and entry_rows:
            df_entry = pd.DataFrame(entry_rows, columns=header)
            safe_name = re.sub(r"[^A-Za-z0-9 _-]", "_", current_name)
            file_path = entry_folder / f"{safe_name}.csv"
            df_entry.to_csv(file_path, index=False)
            log(f"   ✅ Exported entry '{current_name}' to:\n      {file_path}")
            entries_exported += 1

    for _, row in df_raw.iterrows():
        first_col = row[0].strip()

        if first_col.lower().startswith("agreement entry"):
            flush_entry()
            parts = first_col.split(":", 1)
            current_name = parts[1].strip() if len(parts) > 1 else first_col.strip()
            log(f"\n🔎 Found new agreement entry: {current_name}")
            header = None
            entry_rows = []
            continue

        if first_col == "Day":
            header = list(row)
            log("   🧱 Found header row for day entries.")
            continue

        if header and first_col in day_names:
            entry_rows.append(list(row)[: len(header)])
            continue

        if first_col.startswith("Monthly Total"):
            continue

    flush_entry()

    log("\n✅ Parsing complete.")
    log(f"📊 Total agreement entry CSVs created: {entries_exported}")
    log(f"📌 Backup of full sheet: {main_path}")
    log(f"📌 Individual entries folder: {entry_folder}")

    if entries_exported == 0:
        log("\n⚠️ No agreement entries were detected. Check that the sheet contains lines starting with 'Agreement entry'.")
    else:
        log("\n🎉 All done! NDIS Budget exports were generated successfully.")
        log(f"   You can open the export folder in Finder with:\n   open '{export_path}'")

    return {
        "entries_exported": entries_exported,
        "export_folder": export_path,
        "entry_folder": entry_folder,
        "sheet_name": selected_sheet,
        "excel_path": excel_path,
    }


def generate_budget_exports():
    print("🧾 NDIS Budget Export Tool")
    print("--------------------------")

    detected_file = auto_detect_excel_file()
    if detected_file:
        prompt = (
            f"👉 Press Enter to use detected file '{detected_file.name}' "
            "or provide a different spreadsheet path: "
        )
    else:
        prompt = "👉 Enter the spreadsheet file name OR full path (you can drag the file here): "

    excel_path = input(prompt).strip()
    excel_path = excel_path.strip('"').strip("'")

    if excel_path == "" and detected_file:
        excel_path = str(detected_file)
        print(f"\n📄 Auto-selected Excel file: {excel_path}")

    if excel_path == "":
        print("❌ No spreadsheet provided. Please rerun the tool with a valid file.")
        return

    if not os.path.exists(excel_path):
        print(f"❌ File not found: {excel_path}")
        print("   Tip: Try dragging the Excel file into the terminal so the full path is used.")
        return

    result = process_budget_excel(excel_path)
    entries = result.get("entries_exported", 0)
    entry_folder = result.get("entry_folder")
    if entries:
        print(f"\n✅ {entries} entry CSVs written to {entry_folder}")
    else:
        print("\n⚠️ No entries detected; please double-check the spreadsheet.")


if __name__ == "__main__":
    generate_budget_exports()
