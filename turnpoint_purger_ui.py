import csv
import json
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, simpledialog, filedialog

from PIL import Image, ImageDraw, ImageOps, ImageSequence, ImageTk

from importcsv import (
    CLIENT_ID as DEFAULT_CLIENT_ID,
    APP_VERSION,
    CONTACT_EMAIL,
    DuplicateClientError,
    format_timestamp,
    find_purgeable_clients,
    bundle_package_download,
    collect_clients_by_package,
    PACKAGE_MANIFEST_PATH,
    PACKAGE_FALLBACK_NAMES,
    run_turnpoint_purge,
    set_log_sink,
    set_operator_name,
    reset_purge_data,
    configure_credentials,
    RUNTIME_USERNAME,
    RUNTIME_PASSWORD,
)
from purger_state import get_purge_statistics
from nexis_uploader import discover_workers, preview_payload, build_nexis_employee
from nexis_submitter import submit_employee
from worker_purger import (
    WORKER_MANIFEST_PATH,
    collect_workers,
    download_worker_excel,
    run_worker_batch as run_worker_batch,
    run_worker_purge,
    reset_worker_data,
)
from worker_state import get_worker_statistics
from service_type_rate_extractor import (
    capture_service_type_rates,
    normalize_external_row,
)
from appointment_item_discovery import (
    extract_service_type_variants,
    discover_appointment_item_numbers,
    load_discovery_latest,
    run_service_type_merge,
)
import line_item_paths
import storage_paths
from truth_store import TruthStore
from tksheet import Sheet


ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ART_FILENAME = "turnpoint_purger_art.png"
BASE_SCREEN_WIDTH = 1920
BASE_SCREEN_HEIGHT = 1080
UI_SCALE_MIN = 0.80
UI_SCALE_AUTO_MAX = 1.25
UI_SCALE_MANUAL_MAX = 1.40
UI_STATE_FILENAME = "ui_state.json"
PAD_X = 24
PAD_Y = 20
RIGHT_PANEL_BASE_MIN_WIDTH = 440
INSPECTOR_BASE_MIN_WIDTH = 360
HEADER_FONT = ("Orbitron", 24, "bold")
BODY_FONT = ("Space Mono", 10)
BODY_BOLD_FONT = ("Space Mono", 10, "bold")
CODE_FONT = ("JetBrains Mono", 10)
ASCII_SIGNATURE = (
    "_____ _    ____   ___   _ _  _   _   ___      ____   ___  _     ___  \n"
    "|  ___/ \\  |  _ \\ / / | | | || | | \\ | \\ \\    / ___| / _ \\| |   / _ \\ \n"
    "| |_ / _ \\ | |_) | || |_| | || |_|  \\| || |   \\___ \\| | | | |  | | | |\n"
    "|  _/ ___ \\|  _ <| ||  _  |__   _| |\\  || |    ___) | |_| | |__| |_| |\n"
    "|_|/_/   \\_\\_| \\_\\ ||_| |_|  |_| |_| \\_|| |___|____/ \\___/|_____\\___/ \n"
    "                 \\_\\                  /_/_____|\n"
)


class TurnpointPurgerUI(tk.Tk):
    def __init__(self):
        super().__init__()
        try:
            storage_paths.ensure_storage_structure()
            storage_paths.auto_migrate_legacy_outputs()
        except Exception:
            pass
        self.title("TurnpointPurger // Purging Control Surface")
        self.ui_scale = 1.0
        self.ui_scale_mode = "auto"
        self.ui_scale_var = tk.DoubleVar(value=100.0)
        self.ui_scale_percent_var = tk.StringVar(value="100%")
        self.ui_scale_slider = None
        self._suspend_ui_scale_callback = False
        self._ui_scale_config_path = self._ui_scale_state_path()
        self._styles_initialized = False
        # Scaling is computed once at startup (auto/env/manual override) and can be changed live.
        self._apply_ui_scale(self._load_initial_ui_scale(), persist=False)
        self.geometry(f"{self._scaled_px(1380)}x{self._scaled_px(820)}")
        self.configure(bg="#03060f")
        self.minsize(self._scaled_px(1080), self._scaled_px(760))

        self.log_queue = queue.Queue()
        self.status_var = tk.StringVar(
            value="Idle // Purging system primed. Awaiting directive."
        )
        self.client_id_var = tk.StringVar(value=str(DEFAULT_CLIENT_ID))
        self.headless_var = tk.BooleanVar(value=False)
        self.sequence_var = tk.StringVar(value="Sequence tracker offline")
        self.credential_display_var = tk.StringVar(value="Purging account: (not set)")
        self.operator_name = None
        self.credential_username = RUNTIME_USERNAME or ""
        self.credential_password = RUNTIME_PASSWORD or ""
        self.run_thread = None
        self.is_running = False
        self.last_dataset_path = None
        self.art_image = None
        self.scroll_canvas = None
        self.scroll_frame = None
        self.discovery_frame = None
        self.atlas_tree = None
        self.atlas_status_var = tk.StringVar(value="Client atlas awaiting manifest.")
        self.manifest_path = PACKAGE_MANIFEST_PATH
        self.cooldown_seconds = 120  # // default cooldown in seconds
        self.cooldown_seconds_var = tk.StringVar(value="120")
        self.cooldown_bar = None
        self.cooldown_label_var = tk.StringVar(value="Cooldown idle")
        self._cooldown_job = None
        self.cooldown_override = False
        self.force_button = None
        self.bundle_progress = None
        self.bundle_timestamp_var = tk.StringVar(value="Bundle not run yet")
        self.manifest_timestamp_var = tk.StringVar(value="Manifest not generated")

        # Worker branch state
        self.worker_status_var = tk.StringVar(
            value="Idle // Worker purging system primed. Awaiting directive."
        )
        self.worker_id_var = tk.StringVar(value="")
        self.worker_sequence_var = tk.StringVar(value="Worker sequence tracker offline")
        self.worker_manifest_path = WORKER_MANIFEST_PATH
        self.worker_atlas_tree = None
        self.worker_atlas_status_var = tk.StringVar(
            value="Worker atlas awaiting manifest."
        )
        self.worker_cooldown_seconds = 120
        self.worker_cooldown_seconds_var = tk.StringVar(value="120")
        self.worker_cooldown_bar = None
        self.worker_cooldown_label_var = tk.StringVar(value="Cooldown idle")
        self._worker_cooldown_job = None
        self.worker_cooldown_override = False
        self.worker_force_button = None
        self.worker_manifest_timestamp_var = tk.StringVar(
            value="Worker manifest not generated"
        )

        # Nexis uploader state
        self.nexis_root_var = tk.StringVar(value=str(storage_paths.purged_worker_root()))
        self.nexis_table = None
        self.nexis_preview = None
        self.nexis_count_var = tk.StringVar(value="No workers scanned yet.")
        self.nexis_user_var = tk.StringVar(value=os.getenv("NEXIS_USERNAME", ""))
        self.nexis_pass_var = tk.StringVar(value=os.getenv("NEXIS_PASSWORD", ""))
        self.cleaned_root_var = tk.StringVar(value=str(storage_paths.cleaned_nexis_root()))
        self.clients_root_var = tk.StringVar(value=str(storage_paths.purged_clients_root()))
        self.clients_out_var = tk.StringVar(value=str(storage_paths.clients_export_path()))

        # ServiceType -> Rate Extractor state (Truth Table)
        self.rate_status_var = tk.StringVar(
            value="Idle // ServiceType rate extractor ready."
        )
        self.truth_store = TruthStore()
        self.truth_store.on_change = self._on_truth_store_changed
        self.truth_grid = None
        self.rate_running = False
        self.rate_capture_button = None
        self.rate_import_button = None
        self.rate_export_csv_button = None
        self.rate_export_xlsx_button = None
        self.rate_cleanup_button = None
        self.rate_log_view = None
        self.rate_search_var = tk.StringVar(value="")
        self.rate_search_entry = None
        self.rate_apply_button = None
        self.rate_group_var = tk.StringVar(value="All Groups")
        self.rate_group_combo = None
        self.rate_variant_count_var = tk.StringVar(value="0 variants shown")
        self.rate_status_strip = None
        self.rate_inspector_frame = None
        self.rate_inspector_text = None
        self._inspector_link_frame = None
        self.rate_autofit_button = None
        self.rate_help_button = None
        self._visible_truth_records = []
        self._selected_truth_record = None
        self.rate_freeze_var = tk.BooleanVar(value=False)
        self.rate_pending_refresh = False
        self.rate_refresh_job_id = None
        self.discovery_probe_client_var = tk.StringVar(value="")
        self.discovery_headless_var = tk.BooleanVar(value=True)
        self.discovery_debug_var = tk.BooleanVar(value=False)
        self.discovery_running = False
        self.discovery_run_button = None
        self.discovery_merge_button = None
        self.discovery_open_diagnostics_button = None
        self.discovery_open_output_button = None
        self.discovery_probe_entry = None
        self.discovery_headless_checkbox = None
        self.discovery_debug_checkbox = None
        self.discovery_last_result = {}
        self.discovery_last_diagnostics_folder = ""
        self.discovery_last_output_root = ""

        configure_credentials(self.credential_username, self.credential_password)

        self._setup_styles()
        self._build_scrollable_root()
        self._build_layout(self.scroll_frame)
        self._refresh_sequence_stats()
        self._refresh_worker_sequence_stats()
        self._refresh_credential_display()
        self._toggle_discovery_section(self._bundle_buttons_ready())

        set_log_sink(self._enqueue_log)
        self.after(120, self._drain_log_queue)
        self.after(400, self._prompt_operator_name)
        self.after(200, self._maximize_window)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------- UI Construction ---------------------- #
    def _ui_scale_state_path(self) -> Path:
        return line_item_paths.get_truth_root() / "_config" / UI_STATE_FILENAME

    def _scaled_px(self, value: int) -> int:
        return max(1, int(round(float(value) * float(self.ui_scale))))

    def _clamp_ui_scale(self, value: float, max_scale: float = UI_SCALE_MANUAL_MAX) -> float:
        return max(UI_SCALE_MIN, min(max_scale, float(value)))

    def _compute_auto_ui_scale(self) -> float:
        screen_w = max(1, int(self.winfo_screenwidth()))
        screen_h = max(1, int(self.winfo_screenheight()))
        ratio = min(screen_w / BASE_SCREEN_WIDTH, screen_h / BASE_SCREEN_HEIGHT)
        return self._clamp_ui_scale(ratio, max_scale=UI_SCALE_AUTO_MAX)

    def _load_ui_scale_state(self) -> dict:
        path = self._ui_scale_config_path
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass
        return {}

    def _save_ui_scale_state(self, manual_scale: float) -> None:
        path = self._ui_scale_config_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "manual_scale": round(float(manual_scale), 4),
                        "saved_at_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                    fh,
                    indent=2,
                )
        except Exception:
            pass

    def _clear_ui_scale_state(self) -> None:
        try:
            if self._ui_scale_config_path.exists():
                self._ui_scale_config_path.unlink()
        except Exception:
            pass

    def _load_initial_ui_scale(self) -> float:
        auto_scale = self._compute_auto_ui_scale()
        # TURNPOINTPURGER_UI_SCALE can force a startup scale (e.g. 1.25).
        env_raw = (os.getenv("TURNPOINTPURGER_UI_SCALE", "") or "").strip()
        if env_raw:
            try:
                env_scale = self._clamp_ui_scale(float(env_raw))
                self.ui_scale_mode = "env"
                self.ui_scale_var.set(env_scale * 100.0)
                self.ui_scale_percent_var.set(f"{int(round(env_scale * 100.0))}%")
                return env_scale
            except Exception:
                pass

        state = self._load_ui_scale_state()
        manual = state.get("manual_scale")
        if isinstance(manual, (int, float)):
            manual_scale = self._clamp_ui_scale(float(manual))
            self.ui_scale_mode = "manual"
            self.ui_scale_var.set(manual_scale * 100.0)
            self.ui_scale_percent_var.set(f"{int(round(manual_scale * 100.0))}%")
            return manual_scale

        self.ui_scale_mode = "auto"
        self.ui_scale_var.set(auto_scale * 100.0)
        self.ui_scale_percent_var.set(f"{int(round(auto_scale * 100.0))}%")
        return auto_scale

    def _apply_ui_scale(self, scale: float, *, persist: bool = True) -> None:
        scale = self._clamp_ui_scale(scale)
        self.ui_scale = scale
        # Global Tk scaling handles DPI/monitor differences for fonts/widgets.
        try:
            self.tk.call("tk", "scaling", scale)
        except Exception:
            pass
        try:
            self.minsize(self._scaled_px(1080), self._scaled_px(760))
        except Exception:
            pass
        percent = int(round(scale * 100.0))
        self.ui_scale_var.set(float(percent))
        self.ui_scale_percent_var.set(f"{percent}%")
        if self.ui_scale_slider:
            try:
                self._suspend_ui_scale_callback = True
                self.ui_scale_slider.set(percent)
                self._suspend_ui_scale_callback = False
                self.ui_scale_slider.configure(length=self._scaled_px(220))
            except Exception:
                self._suspend_ui_scale_callback = False
                pass
        for tab_attr in ("client_tab", "worker_tab", "nexis_tab"):
            tab = getattr(self, tab_attr, None)
            if tab:
                try:
                    tab.columnconfigure(
                        1, weight=0, minsize=self._scaled_px(RIGHT_PANEL_BASE_MIN_WIDTH)
                    )
                except Exception:
                    pass
        if getattr(self, "rate_tab", None):
            try:
                self.rate_tab.columnconfigure(
                    1, weight=0, minsize=self._scaled_px(INSPECTOR_BASE_MIN_WIDTH)
                )
            except Exception:
                pass
        if getattr(self, "truth_grid", None):
            self._configure_truth_grid_columns()
        if self._styles_initialized:
            self._setup_styles()
        if persist:
            if self.ui_scale_mode == "manual":
                self._save_ui_scale_state(scale)
            elif self.ui_scale_mode == "auto":
                self._clear_ui_scale_state()

    def _handle_ui_scale_changed(self, value: str) -> None:
        if self._suspend_ui_scale_callback:
            return
        try:
            scale = self._clamp_ui_scale(float(value) / 100.0)
        except Exception:
            return
        self.ui_scale_mode = "manual"
        self._apply_ui_scale(scale, persist=True)

    def _handle_ui_scale_reset_auto(self) -> None:
        env_raw = (os.getenv("TURNPOINTPURGER_UI_SCALE", "") or "").strip()
        if env_raw:
            try:
                self.ui_scale_mode = "env"
                self._apply_ui_scale(float(env_raw), persist=False)
                return
            except Exception:
                pass
        self.ui_scale_mode = "auto"
        self._apply_ui_scale(self._compute_auto_ui_scale(), persist=True)

    def _setup_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        # // shared progress bar and button styles for the neon UI

        style.configure(
            "Neon.Horizontal.TProgressbar",
            troughcolor="#050b16",
            background="#18e0ff",
            lightcolor="#5fffff",
            darkcolor="#0aa4ff",
            bordercolor="#050b16",
        )
        style.configure(
            "Ambient.Horizontal.TProgressbar",
            troughcolor="#050b16",
            background="#1f4dff",
            lightcolor="#355fff",
            darkcolor="#0f2da8",
            bordercolor="#050b16",
        )
        style.configure(
            "Cyber.TButton",
            font=("SF Pro Display", 15, "bold"),
            padding=self._scaled_px(8),
            background="#0f172a",
            foreground="#f7fbff",
        )
        style.map(
            "Cyber.TButton",
            background=[
                ("active", "#102f5f"),
                ("disabled", "#0a0d18"),
            ],
            foreground=[
                ("disabled", "#5c6c87"),
            ],
        )
        style.configure(
            "Cyber.TCheckbutton",
            background="#050b16",
            foreground="#d8e5ff",
            font=("Space Mono", 11),
            padding=self._scaled_px(6),
        )
        style.map(
            "Cyber.TCheckbutton",
            background=[("active", "#0b1831")],
            foreground=[("disabled", "#5c6c87")],
        )
        style.configure(
            "Danger.TButton",
            font=("SF Pro Display", 13, "bold"),
            padding=self._scaled_px(6),
            background="#2a0a10",
            foreground="#ffdfe5",
        )
        style.map(
            "Danger.TButton",
            background=[
                ("active", "#5c0f1f"),
                ("disabled", "#1a070c"),
            ],
            foreground=[
                ("disabled", "#6f4b54"),
            ],
        )
        self._styles_initialized = True

    def _build_scrollable_root(self):
        container = tk.Frame(self, bg=self["bg"])
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, bg=self["bg"], highlightthickness=0)
        scrollbar = tk.Scrollbar(
            container, orient="vertical", command=canvas.yview, width=16
        )
        canvas.configure(yscrollcommand=scrollbar.set)

        inner = tk.Frame(canvas, bg=self["bg"])
        inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        canvas.bind(
            "<Configure>",
            lambda event, cid=window_id: canvas.itemconfigure(cid, width=event.width),
        )

        canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        self.scroll_canvas = canvas
        self.scroll_frame = inner

    def _on_mousewheel(self, event):
        if self.scroll_canvas:
            self.scroll_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _maximize_window(self):
        try:
            self.state("zoomed")
        except tk.TclError:
            try:
                self.attributes("-zoomed", True)
            except tk.TclError:
                pass

    def _bundle_buttons_ready(self):
        return bool(self.credential_username and self.credential_password)

    def _toggle_discovery_section(self, visible=None):
        if visible is None:
            visible = self._bundle_buttons_ready()
        if not self.discovery_frame:
            return
        if visible:
            if not self.discovery_frame.winfo_manager():
                self.discovery_frame.pack(anchor="w", padx=20, pady=(12, 12), fill="x")
        else:
            self.discovery_frame.pack_forget()

    def _build_layout(self, parent):
        container = tk.Frame(parent, bg=self["bg"])
        container.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)

        top_bar = tk.Frame(container, bg=self["bg"])
        top_bar.pack(fill="x", pady=(0, 8))
        tk.Label(
            top_bar,
            text="UI Scale",
            fg="#9fe3ff",
            bg=self["bg"],
            font=("Space Mono", 10, "bold"),
        ).pack(side="left")
        self.ui_scale_slider = tk.Scale(
            top_bar,
            from_=80,
            to=140,
            orient="horizontal",
            showvalue=False,
            resolution=1,
            length=self._scaled_px(220),
            bg=self["bg"],
            fg="#d8f1ff",
            troughcolor="#0a1324",
            highlightthickness=0,
            activebackground="#2266cc",
            command=self._handle_ui_scale_changed,
        )
        self._suspend_ui_scale_callback = True
        self.ui_scale_slider.set(self.ui_scale_var.get())
        self._suspend_ui_scale_callback = False
        self.ui_scale_slider.pack(side="left", padx=(10, 8))
        tk.Label(
            top_bar,
            textvariable=self.ui_scale_percent_var,
            fg="#d8f1ff",
            bg=self["bg"],
            font=("Space Mono", 10),
            width=6,
            anchor="w",
        ).pack(side="left")
        ttk.Button(
            top_bar,
            text="Reset Auto",
            style="Cyber.TButton",
            command=self._handle_ui_scale_reset_auto,
        ).pack(side="left", padx=(8, 0))

        notebook = ttk.Notebook(container)
        client_tab = tk.Frame(notebook, bg="#050b16")
        worker_tab = tk.Frame(notebook, bg="#050b16")
        nexis_tab = tk.Frame(notebook, bg="#050b16")
        rate_tab = tk.Frame(notebook, bg="#050b16")
        self.client_tab = client_tab
        self.worker_tab = worker_tab
        self.nexis_tab = nexis_tab
        self.rate_tab = rate_tab
        notebook.add(client_tab, text="Client Purger")
        notebook.add(worker_tab, text="Worker Purger")
        notebook.add(nexis_tab, text="NexisUploader (Employees)")
        notebook.add(rate_tab, text="ServiceType → Rate Extractor")
        notebook.pack(fill="both", expand=True)

        self._build_client_layout(client_tab)
        self._build_worker_layout(worker_tab)
        self._build_nexis_layout(nexis_tab)
        self._build_service_rate_layout(rate_tab)
        self._build_log_panel(container)

        version_badge = tk.Label(
            self,
            text=f"TurnpointPurger v{APP_VERSION}",
            fg="#5de4ff",
            bg=self["bg"],
            font=("Space Mono", 12, "bold"),
        )
        version_badge.place(relx=1.0, x=-32, y=16, anchor="ne")

        global_watermark = tk.Label(
            self,
            text="(Far)H4n_SOLO • TurnpointPurger // Purging System",
            fg="#0e1c33",
            bg=self["bg"],
            font=("Space Mono", 12, "bold"),
        )
        global_watermark.place(relx=1.0, rely=1.0, anchor="se", x=-18, y=-10)

    def _build_client_layout(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0, minsize=self._scaled_px(RIGHT_PANEL_BASE_MIN_WIDTH))

        visual_panel = tk.Frame(parent, bg="#050b16", bd=0, relief="flat")
        visual_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 24), pady=(10, 0))

        controls_panel = tk.Frame(parent, bg="#050b16", bd=0, relief="flat")
        controls_panel.grid(row=0, column=1, sticky="nsew", pady=(10, 0))

        headline = tk.Label(
            visual_panel,
            text="TurnpointPurger — Clients",
            fg="#f5fbff",
            bg="#050b16",
            font=("Orbitron", 28, "bold"),
        )
        headline.pack(anchor="w", padx=30, pady=(28, 0))

        subline = tk.Label(
            visual_panel,
            text="Zero-trace client purging system // Codename: (Far)H4n_SOLO",
            fg="#7cc3ff",
            bg="#050b16",
            font=("Space Mono", 13),
        )
        subline.pack(anchor="w", padx=30, pady=(4, 20))

        self.primary_bar = ttk.Progressbar(
            visual_panel,
            style="Neon.Horizontal.TProgressbar",
            mode="indeterminate",
            length=420,
        )
        self.primary_bar.pack(padx=30, pady=10, anchor="w")

        self.secondary_bar = ttk.Progressbar(
            visual_panel,
            style="Ambient.Horizontal.TProgressbar",
            mode="indeterminate",
            length=420,
        )
        self.secondary_bar.pack(padx=30, pady=6, anchor="w")
        self.secondary_bar.start(65)

        self._build_artwork_section(visual_panel)
        self._build_client_atlas(visual_panel)

        status_label = tk.Label(
            visual_panel,
            textvariable=self.status_var,
            fg="#8fc7ff",
            bg="#050b16",
            font=("Space Mono", 12),
            wraplength=460,
            justify="left",
        )
        status_label.pack(anchor="w", padx=30, pady=(20, 24))

        controls_title = tk.Label(
            controls_panel,
            text="Directive Console",
            fg="#f5fbff",
            bg="#050b16",
            font=("Space Grotesk", 18, "bold"),
        )
        controls_title.pack(anchor="w", padx=20, pady=(24, 4))

        stats_label = tk.Label(
            controls_panel,
            textvariable=self.sequence_var,
            fg="#6bdcff",
            bg="#050b16",
            font=("Space Mono", 11),
        )
        stats_label.pack(anchor="w", padx=20, pady=(0, 12))

        cred_label = tk.Label(
            controls_panel,
            textvariable=self.credential_display_var,
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 11),
            wraplength=360,
            justify="left",
        )
        cred_label.pack(anchor="w", padx=20, pady=(0, 6))

        cred_btn = ttk.Button(
            controls_panel,
            text="Set Purging Credentials",
            style="Cyber.TButton",
            command=self._handle_set_credentials,
        )
        cred_btn.pack(anchor="w", padx=20, pady=(4, 14), fill="x")

        tk.Label(
            controls_panel,
            text="Client ID",
            fg="#93b5ff",
            bg="#050b16",
            font=("Space Mono", 11),
        ).pack(anchor="w", padx=20)

        client_entry = tk.Entry(
            controls_panel,
            textvariable=self.client_id_var,
            font=("Helvetica", 16, "bold"),
            fg="#ffffff",
            bg="#091021",
            insertbackground="#1de5ff",
            relief="flat",
            justify="center",
            width=18,
        )
        client_entry.pack(anchor="w", padx=20, pady=(4, 18))

        self.headless_check = ttk.Checkbutton(
            controls_panel,
            text="Stealth Chrome (headless)",
            variable=self.headless_var,
            style="Cyber.TCheckbutton",
        )
        self.headless_check.pack(anchor="w", padx=20, pady=(0, 18))

        self.launch_button = ttk.Button(
            controls_panel,
            text="Engage Purge",
            style="Cyber.TButton",
            command=self._handle_engage,
        )
        self.launch_button.pack(anchor="w", padx=20, pady=(12, 10), fill="x")

        self.reset_button = ttk.Button(
            controls_panel,
            text="Reset Purge",
            style="Danger.TButton",
            command=self._handle_reset_purge,
        )
        self.reset_button.pack(anchor="w", padx=20, pady=(0, 10), fill="x")

        self.discovery_frame = tk.Frame(controls_panel, bg="#050b16")
        discovery_label = tk.Label(
            self.discovery_frame,
            text="Client Discovery",
            fg="#93b5ff",
            bg="#050b16",
            font=("Space Mono", 11, "bold"),
        )
        discovery_label.pack(anchor="w", pady=(4, 6))

        tk.Label(
            self.discovery_frame,
            text="Cooldown (sec) – set >=20 to bypass server lockout",
            fg="#b3c4ff",
            bg="#050b16",
            font=("Space Mono", 10),
        ).pack(anchor="w", pady=(0, 2))
        tk.Entry(
            self.discovery_frame,
            textvariable=self.cooldown_seconds_var,
            font=("JetBrains Mono", 12),
            bg="#0a1324",
            fg="#e9f2ff",
            insertbackground="#18e0ff",
            relief="flat",
            width=10,
        ).pack(anchor="w", pady=(0, 6))

        self.collect_packages_button = ttk.Button(
            self.discovery_frame,
            text="Collect Package Manifest",
            style="Cyber.TButton",
            command=self._handle_collect_packages,
        )
        self.collect_packages_button.pack(anchor="w", pady=(0, 6), fill="x")

        tk.Label(
            self.discovery_frame,
            textvariable=self.manifest_timestamp_var,
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 9),
        ).pack(anchor="w", pady=(0, 6))

        self.find_button = ttk.Button(
            self.discovery_frame,
            text="Find Purgeable Clients",
            style="Cyber.TButton",
            command=self._handle_find_purgeable_clients,
        )
        self.find_button.pack(anchor="w", pady=(0, 6), fill="x")

        self.bundle_button = ttk.Button(
            self.discovery_frame,
            text="Bundle Download (All Packages)",
            style="Cyber.TButton",
            command=lambda: self._handle_bundle_download(update=False),
        )
        self.bundle_button.pack(anchor="w", pady=(0, 6), fill="x")

        self.update_bundle_button = ttk.Button(
            self.discovery_frame,
            text="Update package bundle to latest",
            style="Cyber.TButton",
            command=lambda: self._handle_bundle_download(update=True),
        )
        self.update_bundle_button.pack(anchor="w", pady=(0, 6), fill="x")

        self.bundle_progress = ttk.Progressbar(
            self.discovery_frame,
            mode="indeterminate",
            length=320,
            style="Ambient.Horizontal.TProgressbar",
        )
        self.bundle_progress.pack(anchor="w", pady=(0, 4), fill="x")

        tk.Label(
            self.discovery_frame,
            textvariable=self.bundle_timestamp_var,
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 9),
        ).pack(anchor="w", pady=(0, 10))

        self.purge_all_button = ttk.Button(
            self.discovery_frame,
            text="Purge All Clients",
            style="Danger.TButton",
            command=self._handle_purge_all_clients,
        )
        self.purge_all_button.pack(anchor="w", pady=(0, 6), fill="x")

        cooldown_label = tk.Label(
            self.discovery_frame,
            textvariable=self.cooldown_label_var,
            fg="#f7c6c6",
            bg="#050b16",
            font=("Space Mono", 10),
        )
        cooldown_label.pack(anchor="w", pady=(0, 2))

        self.cooldown_bar = ttk.Progressbar(
            self.discovery_frame,
            mode="determinate",
            length=320,
            maximum=self.cooldown_seconds,
            style="Ambient.Horizontal.TProgressbar",
        )
        self.cooldown_bar.pack(anchor="w", pady=(0, 6), fill="x")

        self.force_button = ttk.Button(
            self.discovery_frame,
            text="Override cooldown / Force next client",
            style="Danger.TButton",
            command=self._force_cooldown,
        )
        self.force_button.pack(anchor="w", pady=(0, 6), fill="x")
        self.force_button.configure(state="disabled")

        self.refresh_table_button = ttk.Button(
            self.discovery_frame,
            text="Refresh Client Atlas",
            style="Cyber.TButton",
            command=self._load_manifest_table,
        )
        self.refresh_table_button.pack(anchor="w", pady=(0, 6), fill="x")

        self.discovery_frame.pack(anchor="w", padx=20, pady=(12, 12), fill="x")
        self._toggle_discovery_section(self._bundle_buttons_ready())

        notes = tk.Label(
            controls_panel,
            text=(
                "This will authenticate with TurnPoint, capture all client artefacts, "
                "download linked documents, and rebrand outputs under the universal "
                "TurnpointPurger file tree."
            ),
            fg="#7e8fb8",
            bg="#050b16",
            font=("Space Mono", 10),
            wraplength=340,
            justify="left",
        )
        notes.pack(anchor="w", padx=20, pady=(8, 16))

        watermark = tk.Label(
            controls_panel,
            text="(Far)H4n_SOLO // Creator",
            fg="#182544",
            bg="#050b16",
            font=("Segoe UI", 12, "bold"),
        )
        watermark.pack(anchor="e", padx=20, pady=(20, 4))

        email_label = tk.Label(
            controls_panel,
            text=f"Contact: {CONTACT_EMAIL}",
            fg="#6bdcff",
            bg="#050b16",
            font=("Space Mono", 11),
        )
        email_label.pack(anchor="e", padx=20, pady=(0, 12))

    def _build_worker_layout(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0, minsize=self._scaled_px(RIGHT_PANEL_BASE_MIN_WIDTH))

        visual_panel = tk.Frame(parent, bg="#050b16", bd=0, relief="flat")
        visual_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 24), pady=(10, 0))

        controls_panel = tk.Frame(parent, bg="#050b16", bd=0, relief="flat")
        controls_panel.grid(row=0, column=1, sticky="nsew", pady=(10, 0))

        headline = tk.Label(
            visual_panel,
            text="TurnpointPurger — Workers",
            fg="#f5fbff",
            bg="#050b16",
            font=("Orbitron", 28, "bold"),
        )
        headline.pack(anchor="w", padx=30, pady=(28, 0))

        subline = tk.Label(
            visual_panel,
            text="Care worker purging system // Atlas-driven workflow",
            fg="#7cc3ff",
            bg="#050b16",
            font=("Space Mono", 13),
        )
        subline.pack(anchor="w", padx=30, pady=(4, 20))

        self.worker_primary_bar = ttk.Progressbar(
            visual_panel,
            style="Neon.Horizontal.TProgressbar",
            mode="indeterminate",
            length=420,
        )
        self.worker_primary_bar.pack(padx=30, pady=10, anchor="w")

        self.worker_secondary_bar = ttk.Progressbar(
            visual_panel,
            style="Ambient.Horizontal.TProgressbar",
            mode="indeterminate",
            length=420,
        )
        self.worker_secondary_bar.pack(padx=30, pady=6, anchor="w")
        self.worker_secondary_bar.start(65)

        tk.Label(
            visual_panel,
            text="Worker Atlas + purge controls",
            fg="#6bdcff",
            bg="#050b16",
            font=("Space Mono", 12, "bold"),
        ).pack(anchor="w", padx=30, pady=(10, 6))
        self._build_worker_atlas(visual_panel)

        status_label = tk.Label(
            visual_panel,
            textvariable=self.worker_status_var,
            fg="#8fc7ff",
            bg="#050b16",
            font=("Space Mono", 12),
            wraplength=460,
            justify="left",
        )
        status_label.pack(anchor="w", padx=30, pady=(20, 24))

        controls_title = tk.Label(
            controls_panel,
            text="Worker Console",
            fg="#f5fbff",
            bg="#050b16",
            font=("Space Grotesk", 18, "bold"),
        )
        controls_title.pack(anchor="w", padx=20, pady=(24, 4))

        stats_label = tk.Label(
            controls_panel,
            textvariable=self.worker_sequence_var,
            fg="#6bdcff",
            bg="#050b16",
            font=("Space Mono", 11),
        )
        stats_label.pack(anchor="w", padx=20, pady=(0, 8))

        tk.Label(
            controls_panel,
            textvariable=self.credential_display_var,
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 11),
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 6))

        worker_headless = ttk.Checkbutton(
            controls_panel,
            text="Stealth Chrome (headless)",
            variable=self.headless_var,
            style="Cyber.TCheckbutton",
        )
        worker_headless.pack(anchor="w", padx=20, pady=(4, 12))

        tk.Label(
            controls_panel,
            text="Worker ID",
            fg="#93b5ff",
            bg="#050b16",
            font=("Space Mono", 11),
        ).pack(anchor="w", padx=20)
        tk.Entry(
            controls_panel,
            textvariable=self.worker_id_var,
            font=("Helvetica", 16, "bold"),
            fg="#ffffff",
            bg="#091021",
            insertbackground="#1de5ff",
            relief="flat",
            justify="center",
            width=18,
        ).pack(anchor="w", padx=20, pady=(4, 14))

        self.worker_launch_button = ttk.Button(
            controls_panel,
            text="Engage Worker Purge",
            style="Cyber.TButton",
            command=self._handle_worker_engage,
        )
        self.worker_launch_button.pack(anchor="w", padx=20, pady=(4, 10), fill="x")

        self.worker_reset_button = ttk.Button(
            controls_panel,
            text="Reset Worker Purge",
            style="Danger.TButton",
            command=self._handle_worker_reset,
        )
        self.worker_reset_button.pack(anchor="w", padx=20, pady=(0, 10), fill="x")

        discovery = tk.Frame(controls_panel, bg="#050b16")
        tk.Label(
            discovery,
            text="Worker Discovery",
            fg="#93b5ff",
            bg="#050b16",
            font=("Space Mono", 11, "bold"),
        ).pack(anchor="w", pady=(4, 6))

        self.worker_collect_button = ttk.Button(
            discovery,
            text="Collect Worker Manifest",
            style="Cyber.TButton",
            command=self._handle_collect_workers,
        )
        self.worker_collect_button.pack(anchor="w", pady=(0, 6), fill="x")

        tk.Label(
            discovery,
            textvariable=self.worker_manifest_timestamp_var,
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 9),
        ).pack(anchor="w", pady=(0, 6))

        self.worker_excel_button = ttk.Button(
            discovery,
            text="Download Worker Excel",
            style="Cyber.TButton",
            command=self._handle_download_workers_excel,
        )
        self.worker_excel_button.pack(anchor="w", pady=(0, 6), fill="x")

        ttk.Button(
            discovery,
            text="Refresh Worker Atlas",
            style="Cyber.TButton",
            command=self._load_worker_manifest_table,
        ).pack(anchor="w", pady=(0, 6), fill="x")

        self.worker_purge_all_button = ttk.Button(
            discovery,
            text="Purge All Workers",
            style="Danger.TButton",
            command=self._handle_worker_purge_all,
        )
        self.worker_purge_all_button.pack(anchor="w", pady=(0, 6), fill="x")

        tk.Label(
            discovery,
            text="Cooldown (sec) – set >=20 for purge-all pacing",
            fg="#b3c4ff",
            bg="#050b16",
            font=("Space Mono", 10),
        ).pack(anchor="w", pady=(2, 2))
        tk.Entry(
            discovery,
            textvariable=self.worker_cooldown_seconds_var,
            font=("JetBrains Mono", 12),
            bg="#0a1324",
            fg="#e9f2ff",
            insertbackground="#18e0ff",
            relief="flat",
            width=10,
        ).pack(anchor="w", pady=(0, 6))

        self.worker_cooldown_bar = ttk.Progressbar(
            discovery,
            mode="determinate",
            length=320,
            maximum=self.worker_cooldown_seconds,
            style="Ambient.Horizontal.TProgressbar",
        )
        self.worker_cooldown_bar.pack(anchor="w", pady=(0, 6), fill="x")

        tk.Label(
            discovery,
            textvariable=self.worker_cooldown_label_var,
            fg="#f7c6c6",
            bg="#050b16",
            font=("Space Mono", 10),
        ).pack(anchor="w", pady=(0, 2))

        self.worker_force_button = ttk.Button(
            discovery,
            text="Override cooldown / Force next worker",
            style="Danger.TButton",
            command=self._force_worker_cooldown,
        )
        self.worker_force_button.pack(anchor="w", pady=(0, 6), fill="x")
        self.worker_force_button.configure(state="disabled")

        discovery.pack(anchor="w", padx=20, pady=(8, 12), fill="x")

        notes = tk.Label(
            controls_panel,
            text=(
                "Worker purging uses the same credentials and headless flag. "
                "The Worker Atlas mirrors the manifest and marks purged IDs in red."
            ),
            fg="#7e8fb8",
            bg="#050b16",
            font=("Space Mono", 10),
            wraplength=340,
            justify="left",
        )
        notes.pack(anchor="w", padx=20, pady=(8, 12))

    def _build_nexis_layout(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0, minsize=self._scaled_px(RIGHT_PANEL_BASE_MIN_WIDTH))

        left = tk.Frame(parent, bg="#050b16", bd=0, relief="flat")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 24), pady=(10, 0))
        right = tk.Frame(parent, bg="#050b16", bd=0, relief="flat")
        right.grid(row=0, column=1, sticky="nsew", pady=(10, 0))

        headline = tk.Label(
            left,
            text="NexisUploader",
            fg="#f5fbff",
            bg="#050b16",
            font=("Orbitron", 26, "bold"),
        )
        headline.pack(anchor="w", padx=30, pady=(24, 6))

        subline = tk.Label(
            left,
            text="Map PurgedWorker CSVs into Nexis-ready payloads",
            fg="#7cc3ff",
            bg="#050b16",
            font=("Space Mono", 12),
        )
        subline.pack(anchor="w", padx=30, pady=(0, 12))

        controls = tk.Frame(left, bg="#050b16")
        controls.pack(anchor="w", padx=30, pady=(0, 8))

        row_idx = 0
        tk.Label(
            controls,
            text="PurgedWorker root",
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 11),
        ).grid(row=row_idx, column=0, sticky="w")
        tk.Entry(
            controls,
            textvariable=self.nexis_root_var,
            width=46,
            font=("JetBrains Mono", 12),
            bg="#0a1324",
            fg="#e9f2ff",
            insertbackground="#18e0ff",
            relief="flat",
        ).grid(row=row_idx + 1, column=0, sticky="w", pady=(2, 8))
        ttk.Button(
            controls,
            text="Scan workers",
            style="Cyber.TButton",
            command=self._handle_nexis_scan,
        ).grid(row=row_idx + 1, column=1, padx=(10, 0), sticky="w")

        row_idx += 2
        tk.Label(
            controls,
            text="CLEANEDFORNEXIS output",
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 11),
        ).grid(row=row_idx, column=0, sticky="w")
        tk.Entry(
            controls,
            textvariable=self.cleaned_root_var,
            width=46,
            font=("JetBrains Mono", 12),
            bg="#0a1324",
            fg="#e9f2ff",
            insertbackground="#18e0ff",
            relief="flat",
        ).grid(row=row_idx + 1, column=0, sticky="w", pady=(2, 4))
        ttk.Button(
            controls,
            text="Combine Nexis worker CSV/JSON",
            style="Cyber.TButton",
            command=self._handle_combine_nexis,
        ).grid(row=row_idx + 1, column=1, padx=(10, 0), sticky="w")

        row_idx += 2
        tk.Label(
            controls,
            text="PurgedClients root",
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 11),
        ).grid(row=row_idx, column=0, sticky="w")
        tk.Entry(
            controls,
            textvariable=self.clients_root_var,
            width=46,
            font=("JetBrains Mono", 12),
            bg="#0a1324",
            fg="#e9f2ff",
            insertbackground="#18e0ff",
            relief="flat",
        ).grid(row=row_idx + 1, column=0, sticky="w", pady=(2, 4))
        tk.Label(
            controls,
            text="Output clients-data.csv",
            fg="#7cc3ff",
            bg="#050b16",
            font=("Space Mono", 10),
        ).grid(row=row_idx, column=1, sticky="w", padx=(10, 0))
        tk.Entry(
            controls,
            textvariable=self.clients_out_var,
            width=46,
            font=("JetBrains Mono", 12),
            bg="#0a1324",
            fg="#e9f2ff",
            insertbackground="#18e0ff",
            relief="flat",
        ).grid(row=row_idx + 1, column=1, sticky="w", padx=(10, 0), pady=(2, 4))
        ttk.Button(
            controls,
            text="Export Client CSV",
            style="Cyber.TButton",
            command=self._handle_combine_clients,
        ).grid(row=row_idx + 1, column=2, padx=(10, 0), sticky="w")


        creds = tk.Frame(left, bg="#050b16")
        creds.pack(anchor="w", padx=30, pady=(4, 6))
        tk.Label(
            creds,
            text="Nexis credentials",
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 11),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        tk.Label(
            creds,
            text="Username",
            fg="#7cc3ff",
            bg="#050b16",
            font=("Space Mono", 10),
        ).grid(row=1, column=0, sticky="w")
        tk.Entry(
            creds,
            textvariable=self.nexis_user_var,
            width=30,
            font=("JetBrains Mono", 11),
            bg="#0a1324",
            fg="#e9f2ff",
            insertbackground="#18e0ff",
            relief="flat",
        ).grid(row=2, column=0, sticky="w", pady=(0, 4))
        tk.Label(
            creds,
            text="Password",
            fg="#7cc3ff",
            bg="#050b16",
            font=("Space Mono", 10),
        ).grid(row=1, column=1, sticky="w", padx=(10, 0))
        tk.Entry(
            creds,
            textvariable=self.nexis_pass_var,
            width=30,
            font=("JetBrains Mono", 11),
            bg="#0a1324",
            fg="#e9f2ff",
            insertbackground="#18e0ff",
            relief="flat",
            show="*",
        ).grid(row=2, column=1, sticky="w", padx=(10, 0), pady=(0, 4))

        tk.Label(
            left,
            textvariable=self.nexis_count_var,
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 10),
        ).pack(anchor="w", padx=30, pady=(4, 8))

        table_frame = tk.Frame(left, bg="#050b16")
        table_frame.pack(fill="both", expand=True, padx=30, pady=(4, 6))

        columns = ("order", "worker_id", "full_name", "team", "email")
        table = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            height=10,
            style="Atlas.Treeview",
        )
        table.heading("order", text="#", anchor="center")
        table.heading("worker_id", text="Worker ID", anchor="center")
        table.heading("full_name", text="Full Name", anchor="w")
        table.heading("team", text="Team", anchor="w")
        table.heading("email", text="Email (mapped)", anchor="w")
        table.column("order", width=60, anchor="center")
        table.column("worker_id", width=120, anchor="center")
        table.column("full_name", width=260, anchor="w")
        table.column("team", width=200, anchor="w")
        table.column("email", width=260, anchor="w")

        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=scroll.set)
        table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        table.bind("<<TreeviewSelect>>", self._handle_nexis_select)
        self.nexis_table = table

        # Client discover table
        client_frame = tk.Frame(left, bg="#050b16")
        client_frame.pack(fill="both", expand=True, padx=30, pady=(6, 10))
        tk.Label(
            client_frame,
            text="Clients discovered",
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 10, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        client_columns = ("order", "client_id", "client_name", "package")
        client_table = ttk.Treeview(
            client_frame,
            columns=client_columns,
            show="headings",
            height=8,
            style="Atlas.Treeview",
        )
        client_table.heading("order", text="#", anchor="center")
        client_table.heading("client_id", text="Client ID", anchor="center")
        client_table.heading("client_name", text="Client Name", anchor="w")
        client_table.heading("package", text="Package", anchor="w")
        client_table.column("order", width=60, anchor="center")
        client_table.column("client_id", width=120, anchor="center")
        client_table.column("client_name", width=260, anchor="w")
        client_table.column("package", width=200, anchor="w")

        cscroll = ttk.Scrollbar(client_frame, orient="vertical", command=client_table.yview)
        client_table.configure(yscrollcommand=cscroll.set)
        client_table.pack(side="left", fill="both", expand=True)
        cscroll.pack(side="right", fill="y")
        client_table.bind("<<TreeviewSelect>>", self._handle_client_select)
        self.client_table = client_table

        preview_label = tk.Label(
            right,
            text="Mapped Nexis payload (preview)",
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 11, "bold"),
        )
        preview_label.pack(anchor="w", padx=20, pady=(12, 6))
        ttk.Button(
            right,
            text="Upload selected to Nexis",
            style="Cyber.TButton",
            command=self._handle_nexis_upload,
        ).pack(anchor="w", padx=20, pady=(0, 8))
        preview = scrolledtext.ScrolledText(
            right,
            height=28,
            wrap="word",
            font=("JetBrains Mono", 11),
            bg="#0b1322",
            fg="#dbe7ff",
            relief="flat",
        )
        preview.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        preview.configure(state="disabled")
        self.nexis_preview = preview

    def _build_service_rate_layout(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(
            1, weight=0, minsize=self._scaled_px(INSPECTOR_BASE_MIN_WIDTH)
        )
        parent.rowconfigure(4, weight=1)

        # ---- HEADER ----
        header = tk.Frame(parent, bg="#050b16")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=PAD_X, pady=(20, 8))

        tk.Label(
            header,
            text="ServiceType \u2192 Rate Extractor",
            fg="#f5fbff",
            bg="#050b16",
            font=HEADER_FONT,
        ).pack(anchor="w")

        tk.Label(
            header,
            text=(
                "Truth Table: real-time conflict detection + multi-source rate resolution. "
                f"Outputs: {line_item_paths.get_truth_root()}/"
            ),
            fg="#7cc3ff",
            bg="#050b16",
            font=("Space Mono", 11),
            wraplength=1020,
            justify="left",
        ).pack(anchor="w", pady=(4, 10))

        # ---- ACTION BUTTONS ----
        actions = tk.Frame(header, bg="#050b16")
        actions.pack(anchor="w", fill="x")

        self.rate_capture_button = ttk.Button(
            actions,
            text="Capture Live Rates",
            style="Cyber.TButton",
            command=self._handle_capture_live_rates,
        )
        self.rate_capture_button.pack(side="left", padx=(0, 10))

        self.rate_import_button = ttk.Button(
            actions,
            text="ImportCSV",
            style="Cyber.TButton",
            command=self._handle_rate_import_csv,
        )
        self.rate_import_button.pack(side="left", padx=(0, 10))

        self.rate_export_csv_button = ttk.Button(
            actions,
            text="Export \u2192 CSV",
            style="Cyber.TButton",
            command=self._handle_export_truth_csv,
        )
        self.rate_export_csv_button.pack(side="left", padx=(0, 10))

        self.rate_export_xlsx_button = ttk.Button(
            actions,
            text="Export \u2192 XLSX",
            style="Cyber.TButton",
            command=self._handle_export_truth_xlsx,
        )
        self.rate_export_xlsx_button.pack(side="left", padx=(0, 10))

        self.rate_cleanup_button = ttk.Button(
            actions,
            text="Clean Rate Clutter",
            style="Cyber.TButton",
            command=self._handle_clean_rate_clutter,
        )
        self.rate_cleanup_button.pack(side="left")

        # ---- DISCOVERY SECTION (preserved) ----
        discovery_section = tk.Frame(
            header,
            bg="#0a1324",
            highlightthickness=1,
            highlightbackground="#1f3e66",
            highlightcolor="#1f3e66",
        )
        discovery_section.pack(anchor="w", fill="x", pady=(12, 6))
        discovery_section.columnconfigure(1, weight=1)

        tk.Label(
            discovery_section,
            text="Appointment Discovery + Enrichment",
            fg="#d8f1ff",
            bg="#0a1324",
            font=("Space Mono", 11, "bold"),
        ).grid(row=0, column=0, columnspan=6, sticky="w", padx=10, pady=(8, 2))

        tk.Label(
            discovery_section,
            text=(
                "Probe Client ID is required for Assist discovery context. "
                "Run discovery first, then merge explicitly."
            ),
            fg="#83c5f3",
            bg="#0a1324",
            font=("Space Mono", 9),
            justify="left",
        ).grid(row=1, column=0, columnspan=6, sticky="w", padx=10, pady=(0, 8))

        tk.Label(
            discovery_section,
            text="Probe Client ID",
            fg="#9fe3ff",
            bg="#0a1324",
            font=("Space Mono", 10),
        ).grid(row=2, column=0, sticky="w", padx=(10, 6), pady=(0, 8))

        probe_entry = tk.Entry(
            discovery_section,
            textvariable=self.discovery_probe_client_var,
            width=20,
            font=("JetBrains Mono", 11),
            bg="#0f1a31",
            fg="#e9f2ff",
            insertbackground="#18e0ff",
            relief="flat",
        )
        probe_entry.grid(row=2, column=1, sticky="w", padx=(0, 10), pady=(0, 8))
        self.discovery_probe_entry = probe_entry

        headless_checkbox = tk.Checkbutton(
            discovery_section,
            text="Headless",
            variable=self.discovery_headless_var,
            bg="#0a1324",
            fg="#d8e5ff",
            selectcolor="#0f1a31",
            activebackground="#0a1324",
            activeforeground="#d8e5ff",
        )
        headless_checkbox.grid(row=2, column=2, sticky="w", padx=(0, 10), pady=(0, 8))
        self.discovery_headless_checkbox = headless_checkbox

        debug_checkbox = tk.Checkbutton(
            discovery_section,
            text="Discovery Debug",
            variable=self.discovery_debug_var,
            bg="#0a1324",
            fg="#d8e5ff",
            selectcolor="#0f1a31",
            activebackground="#0a1324",
            activeforeground="#d8e5ff",
        )
        debug_checkbox.grid(row=2, column=3, sticky="w", padx=(0, 10), pady=(0, 8))
        self.discovery_debug_checkbox = debug_checkbox

        self.discovery_run_button = ttk.Button(
            discovery_section,
            text="Run Appointment Discovery",
            style="Cyber.TButton",
            command=self._handle_run_appointment_discovery,
        )
        self.discovery_run_button.grid(row=3, column=0, sticky="w", padx=(10, 8), pady=(0, 10))

        self.discovery_merge_button = ttk.Button(
            discovery_section,
            text="Merge with Service Types",
            style="Cyber.TButton",
            command=self._handle_merge_service_types_from_discovery,
        )
        self.discovery_merge_button.grid(row=3, column=1, sticky="w", padx=(0, 8), pady=(0, 10))

        self.discovery_open_diagnostics_button = ttk.Button(
            discovery_section,
            text="Open Diagnostics Folder",
            style="Cyber.TButton",
            command=self._handle_open_diagnostics_folder,
        )
        self.discovery_open_diagnostics_button.grid(
            row=3, column=2, sticky="w", padx=(0, 8), pady=(0, 10)
        )

        self.discovery_open_output_button = ttk.Button(
            discovery_section,
            text="Open Output Folder",
            style="Cyber.TButton",
            command=self._handle_open_output_folder,
        )
        self.discovery_open_output_button.grid(
            row=3, column=3, sticky="w", padx=(0, 10), pady=(0, 10)
        )

        # ---- STATUS LOG ----
        tk.Label(
            header,
            textvariable=self.rate_status_var,
            fg="#9fe3ff",
            bg="#050b16",
            font=BODY_FONT,
            wraplength=1020,
            justify="left",
        ).pack(anchor="w", pady=(10, 0))

        tk.Label(
            header,
            text="Status",
            fg="#9fe3ff",
            bg="#050b16",
            font=BODY_BOLD_FONT,
        ).pack(anchor="w", pady=(8, 4))

        status_view = scrolledtext.ScrolledText(
            header,
            height=6,
            wrap="word",
            font=CODE_FONT,
            bg="#030611",
            fg="#c2f1ff",
            insertbackground="#1de5ff",
            relief="flat",
        )
        status_view.pack(fill="x", expand=False)
        status_view.configure(state="disabled")
        self.rate_log_view = status_view

        # ---- STATUS STRIP ----
        strip_frame = tk.Frame(
            parent,
            bg="#0a1324",
            highlightthickness=1,
            highlightbackground="#1f3e66",
            highlightcolor="#1f3e66",
        )
        strip_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=PAD_X, pady=(10, 0))

        self.rate_status_strip = tk.Label(
            strip_frame,
            text="RED: 0 | YELLOW: 0 | BLUE: 0 | Conflicts: 0 | Last: --:--:--",
            fg="#d8f1ff",
            bg="#0a1324",
            font=BODY_BOLD_FONT,
            anchor="w",
        )
        self.rate_status_strip.pack(fill="x", padx=10, pady=6)

        # ---- LEGEND ----
        legend_frame = tk.Frame(
            parent,
            bg="#0a1324",
            highlightthickness=1,
            highlightbackground="#1f3e66",
            highlightcolor="#1f3e66",
        )
        legend_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=PAD_X, pady=(10, 0))
        tk.Label(
            legend_frame,
            text=self._rate_help_text(inline=True),
            fg="#a8dcff",
            bg="#0a1324",
            font=("Space Mono", 9),
            justify="left",
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=10, pady=6)
        self.rate_help_button = ttk.Button(
            legend_frame,
            text="?",
            style="Cyber.TButton",
            command=self._show_rate_help_dialog,
            width=3,
        )
        self.rate_help_button.pack(side="right", padx=(0, 8), pady=6)

        # ---- FILTERS ----
        filters = tk.Frame(parent, bg="#050b16")
        filters.grid(row=3, column=0, columnspan=2, sticky="ew", padx=PAD_X, pady=(10, 6))
        filters.columnconfigure(8, weight=1)

        tk.Label(
            filters,
            text="Service Type:",
            fg="#9fe3ff",
            bg="#050b16",
            font=BODY_FONT,
        ).grid(row=0, column=0, sticky="w", padx=(0, 6))

        self.rate_group_combo = ttk.Combobox(
            filters,
            textvariable=self.rate_group_var,
            values=["All Groups"],
            state="readonly",
            width=36,
        )
        self.rate_group_combo.grid(row=0, column=1, sticky="w", padx=(0, 12))
        self.rate_group_combo.bind(
            "<<ComboboxSelected>>", lambda _e: self._refresh_truth_grid()
        )

        tk.Label(
            filters,
            text="Search:",
            fg="#9fe3ff",
            bg="#050b16",
            font=BODY_FONT,
        ).grid(row=0, column=2, sticky="w", padx=(0, 6))

        search_entry = tk.Entry(
            filters,
            textvariable=self.rate_search_var,
            width=24,
            font=("JetBrains Mono", 11),
            bg="#0a1324",
            fg="#e9f2ff",
            insertbackground="#18e0ff",
            relief="flat",
        )
        search_entry.grid(row=0, column=3, sticky="w", padx=(0, 10))
        search_entry.bind("<Return>", lambda _e: self._refresh_truth_grid())
        self.rate_search_entry = search_entry

        self.rate_apply_button = ttk.Button(
            filters,
            text="Apply",
            style="Cyber.TButton",
            command=self._refresh_truth_grid,
        )
        self.rate_apply_button.grid(row=0, column=4, sticky="w")

        self.rate_autofit_button = ttk.Button(
            filters,
            text="Auto-fit Columns",
            style="Cyber.TButton",
            command=self._handle_auto_fit_truth_columns,
        )
        self.rate_autofit_button.grid(row=0, column=5, sticky="w", padx=(8, 0))

        freeze_cb = tk.Checkbutton(
            filters,
            text="Freeze View",
            variable=self.rate_freeze_var,
            bg="#050b16",
            fg="#9fe3ff",
            selectcolor="#0a1324",
            activebackground="#050b16",
            activeforeground="#18e0ff",
            font=BODY_FONT,
            command=self._on_freeze_toggled,
        )
        freeze_cb.grid(row=0, column=6, sticky="w", padx=(12, 0))

        tk.Label(
            filters,
            textvariable=self.rate_variant_count_var,
            fg="#9fe3ff",
            bg="#050b16",
            font=BODY_BOLD_FONT,
        ).grid(row=0, column=7, sticky="w", padx=(16, 0))

        # ---- TRUTH GRID (left) + INSPECTOR (right) ----
        grid_frame = tk.Frame(parent, bg="#050b16")
        grid_frame.grid(row=4, column=0, sticky="nsew", padx=(PAD_X, 8), pady=(0, 16))
        grid_frame.columnconfigure(0, weight=1)
        grid_frame.rowconfigure(0, weight=1)

        self.truth_grid = Sheet(
            grid_frame,
            headers=[
                "Status",
                "Parent Service Type",
                "Variant Prefix",
                "Service Variant",
                "Variant ID",
                "Rate",
                "Item Number",
                "Rate Source",
                "Item Source",
                "Updated (UTC)",
            ],
            show_x_scrollbar=True,
            show_y_scrollbar=True,
            tooltips=True,
            tooltip_hover_delay=800,
        )
        self.truth_grid.enable_bindings(
            "single_select",
            "row_select",
            "column_width_resize",
            "arrowkeys",
            "right_click_popup_menu",
            "rc_select",
            "copy",
        )
        self.truth_grid.set_options(
            font=("JetBrains Mono", 10, "normal"),
            header_font=BODY_BOLD_FONT,
            table_bg="#0a1324",
            table_fg="#e9f2ff",
            header_bg="#1f3e66",
            header_fg="#d8f1ff",
            top_left_bg="#0a1324",
            top_left_fg="#d8f1ff",
            frame_bg="#050b16",
        )
        self.truth_grid.grid(row=0, column=0, sticky="nsew")
        self.truth_grid.extra_bindings("cell_select", self._on_truth_row_selected)
        self._configure_truth_grid_columns()

        # ---- INSPECTOR PANEL (right column) ----
        inspector_frame = tk.Frame(
            parent,
            bg="#0a1324",
            highlightthickness=1,
            highlightbackground="#1f3e66",
            highlightcolor="#1f3e66",
        )
        inspector_frame.grid(row=4, column=1, sticky="nsew", padx=(8, PAD_X), pady=(0, 16))
        self.rate_inspector_frame = inspector_frame

        tk.Label(
            inspector_frame,
            text="Inspector",
            fg="#d8f1ff",
            bg="#0a1324",
            font=("Space Mono", 11, "bold"),
        ).pack(anchor="w", padx=10, pady=(8, 4))

        inspector_text = scrolledtext.ScrolledText(
            inspector_frame,
            wrap="word",
            font=("JetBrains Mono", 9),
            bg="#030611",
            fg="#c2f1ff",
            relief="flat",
            height=35,
            width=38,
        )
        inspector_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        inspector_text.configure(state="disabled")
        self.rate_inspector_text = inspector_text

        # Inspector text tags
        inspector_text.tag_config("heading", font=("Space Mono", 11, "bold"), foreground="#18e0ff")
        inspector_text.tag_config("subheading", font=("Space Mono", 10), foreground="#9fe3ff")
        inspector_text.tag_config("label", font=("Space Mono", 10, "bold"), foreground="#d8f1ff")
        inspector_text.tag_config("conflict", font=("Space Mono", 10, "bold"), foreground="#ff6b6b")
        inspector_text.tag_config("link", font=("JetBrains Mono", 9), foreground="#7cc3ff")

    def _build_log_panel(self, parent):
        log_panel = tk.Frame(parent, bg="#050b16", bd=0, relief="flat")
        log_panel.pack(fill="both", expand=True, pady=(24, 0))

        log_title = tk.Label(
            log_panel,
            text="Purge Feed // Live Ops Log",
            fg="#f5fbff",
            bg="#050b16",
            font=("Space Grotesk", 16, "bold"),
        )
        log_title.pack(anchor="w", padx=20, pady=(20, 6))

        self.log_view = scrolledtext.ScrolledText(
            log_panel,
            height=12,
            wrap="word",
            font=("JetBrains Mono", 13),
            bg="#030611",
            fg="#c2f1ff",
            insertbackground="#1de5ff",
            relief="flat",
        )
        self.log_view.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        self.log_view.configure(state="disabled")

        signature = tk.Label(
            log_panel,
            text=ASCII_SIGNATURE,
            fg="#23445f",
            bg="#050b16",
            font=("Courier New", 8),
            justify="left",
        )
        signature.pack(anchor="w", padx=20, pady=(0, 12))

    def _build_artwork_section(self, parent):
        art_frame = tk.Frame(parent, bg="#050b16")
        art_frame.pack(fill="x", padx=20, pady=(10, 0))

        badge_frame = tk.Frame(art_frame, bg="#050b16")
        badge_frame.pack(anchor="w", pady=(0, 10), fill="x")

        badge_label = tk.Label(
            badge_frame,
            text="Powered by Nexix365",
            fg="#8cf0ff",
            bg="#050b16",
            font=("Orbitron", 16, "bold"),
        )
        badge_label.pack(side="left")

        mascot_path = ASSETS_DIR / "nexismascot.png"
        if mascot_path.exists():
            try:
                mascot_img = Image.open(mascot_path).resize((64, 64))
                self.mascot_image = ImageTk.PhotoImage(mascot_img)
                tk.Label(
                    badge_frame,
                    image=self.mascot_image,
                    bg="#050b16",
                ).pack(side="left", padx=14)
            except Exception:
                pass

        self.gif_canvas = tk.Canvas(
            art_frame,
            width=320,
            height=320,
            bg="#050b16",
            highlightthickness=0,
        )
        self.gif_canvas.pack()
        self.gif_canvas.create_oval(20, 20, 300, 300, outline="#081327", width=4)
        self.gif_canvas.create_oval(26, 26, 294, 294, outline="#17e0ff", width=4)

        self.profile_frames = []
        self.profile_frame_index = 0
        self.gif_canvas_image = None
        self._load_profile_animation()
        if self.profile_frames:
            self._animate_profile_gif()
        else:
            self.gif_canvas.create_text(
                160,
                160,
                text="maindp.gif missing",
                fill="#7cc3ff",
                font=("Space Mono", 12),
            )

    def _set_atlas_status(self, message):
        self.atlas_status_var.set(message)

    def _clear_atlas_tree(self):
        if not self.atlas_tree:
            return
        for item in self.atlas_tree.get_children():
            self.atlas_tree.delete(item)

    def _cancel_cooldown_timer(self):
        if self._cooldown_job:
            self.after_cancel(self._cooldown_job)
            self._cooldown_job = None
        if self.force_button:
            self.force_button.configure(state="disabled")

    def _start_cooldown_timer(self, seconds):
        if not self.cooldown_bar:
            return
        self.cooldown_seconds = seconds
        self.cooldown_override = False
        self._cancel_cooldown_timer()
        self.cooldown_bar["maximum"] = seconds
        self.cooldown_bar["value"] = 0
        self.cooldown_label_var.set(f"Cooldown: {seconds}s remaining")
        if self.force_button:
            self.force_button.configure(state="normal")

        def tick(elapsed):
            if self.cooldown_override:
                self.cooldown_bar["value"] = seconds
                self.cooldown_label_var.set("Cooldown overridden")
                self._cancel_cooldown_timer()
                return
            if elapsed >= seconds:
                self.cooldown_bar["value"] = seconds
                self.cooldown_label_var.set("Cooldown complete")
                self._cancel_cooldown_timer()
                return
            self.cooldown_bar["value"] = elapsed
            remaining = seconds - elapsed
            self.cooldown_label_var.set(f"Cooldown: {remaining}s remaining")
            self._cooldown_job = self.after(1000, tick, elapsed + 1)

        self._cooldown_job = self.after(1000, tick, 1)

    def _resolve_cooldown_seconds(self):
        try:
            value = int(self.cooldown_seconds_var.get())
        except ValueError:
            value = 120
        if value < 20:
            value = 20
            self.cooldown_seconds_var.set(str(value))
        self.cooldown_seconds = value
        return value

    def _sleep_with_override(self, seconds):
        self.cooldown_override = False
        for _ in range(seconds):
            if self.cooldown_override:
                break
            time.sleep(1)
        self.cooldown_override = False

    def _force_cooldown(self):
        if not self._cooldown_job:
            return
        self.cooldown_override = True
        self.cooldown_label_var.set("Cooldown overridden - forcing next client")
        self._cancel_cooldown_timer()

    def _cancel_worker_cooldown_timer(self):
        if self._worker_cooldown_job:
            self.after_cancel(self._worker_cooldown_job)
            self._worker_cooldown_job = None
        if self.worker_force_button:
            self.worker_force_button.configure(state="disabled")

    def _start_worker_cooldown_timer(self, seconds):
        if not self.worker_cooldown_bar:
            return
        self.worker_cooldown_seconds = seconds
        self.worker_cooldown_override = False
        self._cancel_worker_cooldown_timer()
        self.worker_cooldown_bar["maximum"] = seconds
        self.worker_cooldown_bar["value"] = 0
        self.worker_cooldown_label_var.set(f"Cooldown: {seconds}s remaining")
        if self.worker_force_button:
            self.worker_force_button.configure(state="normal")

        def tick(elapsed):
            if self.worker_cooldown_override:
                self.worker_cooldown_bar["value"] = seconds
                self.worker_cooldown_label_var.set("Cooldown overridden")
                self._cancel_worker_cooldown_timer()
                return
            if elapsed >= seconds:
                self.worker_cooldown_bar["value"] = seconds
                self.worker_cooldown_label_var.set("Cooldown complete")
                self._cancel_worker_cooldown_timer()
                return
            self.worker_cooldown_bar["value"] = elapsed
            remaining = seconds - elapsed
            self.worker_cooldown_label_var.set(f"Cooldown: {remaining}s remaining")
            self._worker_cooldown_job = self.after(1000, tick, elapsed + 1)

        self._worker_cooldown_job = self.after(1000, tick, 1)

    def _resolve_worker_cooldown_seconds(self):
        try:
            value = int(self.worker_cooldown_seconds_var.get())
        except ValueError:
            value = 120
        if value < 20:
            value = 20
            self.worker_cooldown_seconds_var.set(str(value))
        self.worker_cooldown_seconds = value
        return value

    def _sleep_with_worker_override(self, seconds):
        self.worker_cooldown_override = False
        for _ in range(seconds):
            if self.worker_cooldown_override:
                break
            time.sleep(1)
        self.worker_cooldown_override = False

    def _force_worker_cooldown(self):
        if not self._worker_cooldown_job:
            return
        self.worker_cooldown_override = True
        self.worker_cooldown_label_var.set("Cooldown overridden - forcing next worker")
        self._cancel_worker_cooldown_timer()

    def _update_manifest_timestamp(self):
        manifest = Path(self.manifest_path)
        if manifest.exists():
            ts = datetime.fromtimestamp(manifest.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            self.manifest_timestamp_var.set(f"Manifest updated: {ts}")
        else:
            self.manifest_timestamp_var.set("Manifest not generated")

    def _update_worker_manifest_timestamp(self):
        manifest = Path(self.worker_manifest_path)
        if manifest.exists():
            ts = datetime.fromtimestamp(manifest.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            self.worker_manifest_timestamp_var.set(f"Worker manifest updated: {ts}")
        else:
            self.worker_manifest_timestamp_var.set("Worker manifest not generated")

    def _build_client_atlas(self, parent):
        atlas_frame = tk.Frame(parent, bg="#050b16")
        atlas_frame.pack(fill="both", expand=True, padx=20, pady=(10, 10))
        title = tk.Label(
            atlas_frame,
            text="Client Atlas",
            fg="#f5fbff",
            bg="#050b16",
            font=("Space Grotesk", 16, "bold"),
        )
        title.pack(anchor="w", pady=(0, 4))

        status = tk.Label(
            atlas_frame,
            textvariable=self.atlas_status_var,
            fg="#7ecdf3",
            bg="#050b16",
            font=("Space Mono", 10),
        )
        status.pack(anchor="w", pady=(0, 6))

        tree_container = tk.Frame(atlas_frame, bg="#050b16")
        tree_container.pack(fill="both", expand=True)

        columns = ("order", "client_id", "client_name", "package")
        atlas_style = ttk.Style(self)
        atlas_style.configure(
            "Atlas.Treeview",
            background="#0d1424",
            fieldbackground="#0d1424",
            foreground="#dbe7ff",
            rowheight=26,
            bordercolor="#0d1424",
            borderwidth=0,
        )
        atlas_style.map(
            "Atlas.Treeview",
            background=[("selected", "#1f3554")],
            foreground=[("selected", "#fefcf5")],
        )
        tree = ttk.Treeview(
            tree_container,
            columns=columns,
            show="headings",
            height=12,
            style="Atlas.Treeview",
        )
        tree.heading("order", text="#", anchor="center")
        tree.heading("client_id", text="Client ID", anchor="center")
        tree.heading("client_name", text="Client Name", anchor="w")
        tree.heading("package", text="Package", anchor="w")
        tree.column("order", width=60, anchor="center")
        tree.column("client_id", width=120, anchor="center")
        tree.column("client_name", width=280, anchor="w")
        tree.column("package", width=260, anchor="w")

        scrollbar = ttk.Scrollbar(tree_container, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        tree.tag_configure("pending", background="#1b2d16", foreground="#f9f5c8")
        tree.tag_configure("purged", background="#471524", foreground="#ffc2d3")

        self.atlas_tree = tree
        self._load_manifest_table()

    def _build_worker_atlas(self, parent):
        atlas_frame = tk.Frame(parent, bg="#050b16")
        atlas_frame.pack(fill="both", expand=True, padx=20, pady=(10, 10))
        title = tk.Label(
            atlas_frame,
            text="Worker Atlas",
            fg="#f5fbff",
            bg="#050b16",
            font=("Space Grotesk", 16, "bold"),
        )
        title.pack(anchor="w", pady=(0, 4))

        status = tk.Label(
            atlas_frame,
            textvariable=self.worker_atlas_status_var,
            fg="#7ecdf3",
            bg="#050b16",
            font=("Space Mono", 10),
        )
        status.pack(anchor="w", pady=(0, 6))

        tree_container = tk.Frame(atlas_frame, bg="#050b16")
        tree_container.pack(fill="both", expand=True)

        columns = ("order", "worker_id", "full_name", "team")
        atlas_style = ttk.Style(self)
        atlas_style.configure(
            "WorkerAtlas.Treeview",
            background="#0d1424",
            fieldbackground="#0d1424",
            foreground="#dbe7ff",
            rowheight=26,
            bordercolor="#0d1424",
            borderwidth=0,
        )
        atlas_style.map(
            "WorkerAtlas.Treeview",
            background=[("selected", "#1f3554")],
            foreground=[("selected", "#fefcf5")],
        )
        tree = ttk.Treeview(
            tree_container,
            columns=columns,
            show="headings",
            height=12,
            style="WorkerAtlas.Treeview",
        )
        tree.heading("order", text="#", anchor="center")
        tree.heading("worker_id", text="Worker ID", anchor="center")
        tree.heading("full_name", text="Full Name", anchor="w")
        tree.heading("team", text="Team", anchor="w")
        tree.column("order", width=60, anchor="center")
        tree.column("worker_id", width=120, anchor="center")
        tree.column("full_name", width=280, anchor="w")
        tree.column("team", width=260, anchor="w")

        scrollbar = ttk.Scrollbar(tree_container, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        tree.tag_configure("pending", background="#1b2d16", foreground="#f9f5c8")
        tree.tag_configure("purged", background="#471524", foreground="#ffc2d3")

        self.worker_atlas_tree = tree
        self._load_worker_manifest_table()

    def _load_manifest_table(self):
        if not self.atlas_tree:
            return
        manifest = Path(self.manifest_path)
        if not manifest.exists():
            self._clear_atlas_tree()
            self._set_atlas_status("Manifest not found. Collect package manifest first.")
            self._update_manifest_timestamp()
            return
        try:
            with manifest.open("r", newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                rows = list(reader)
        except Exception as exc:
            self._clear_atlas_tree()
            self._set_atlas_status(f"Failed to read manifest: {exc}")
            return

        self._clear_atlas_tree()
        manifest_clients = 0
        purged_ids = set()
        purged_count = 0
        try:
            stats = get_purge_statistics()
            clients = (stats or {}).get("clients") or {}
            purged_ids = {str(cid) for cid in clients.keys()}
        except Exception:
            purged_ids = set()

        for row in rows:
            order = row.get("Order") or row.get("order") or ""
            client_id = (row.get("Client ID") or row.get("client_id") or "").strip()
            client_name = row.get("Client Name") or row.get("client_name") or ""
            package = row.get("Package") or row.get("package") or ""
            tag = "purged" if client_id and client_id in purged_ids else "pending"
            if tag == "purged":
                purged_count += 1
            values = (order, client_id, client_name, package)
            self.atlas_tree.insert("", "end", values=values, tags=(tag,))
            manifest_clients += 1

        self._set_atlas_status(
            f"Atlas loaded: {manifest_clients} client(s). Purged: {purged_count}."
        )
        self._update_manifest_timestamp()

    def _load_worker_manifest_table(self):
        if not self.worker_atlas_tree:
            return
        manifest = Path(self.worker_manifest_path)
        if not manifest.exists():
            for item in self.worker_atlas_tree.get_children():
                self.worker_atlas_tree.delete(item)
            self.worker_atlas_status_var.set(
                "Worker manifest not found. Collect worker manifest first."
            )
            self._update_worker_manifest_timestamp()
            return
        try:
            with manifest.open("r", newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                rows = list(reader)
        except Exception as exc:
            for item in self.worker_atlas_tree.get_children():
                self.worker_atlas_tree.delete(item)
            self.worker_atlas_status_var.set(f"Failed to read worker manifest: {exc}")
            return

        for item in self.worker_atlas_tree.get_children():
            self.worker_atlas_tree.delete(item)
        manifest_workers = 0
        purged_ids = set()
        purged_count = 0
        try:
            stats = get_worker_statistics()
            workers = (stats or {}).get("workers") or {}
            purged_ids = {str(cid) for cid in workers.keys()}
        except Exception:
            purged_ids = set()

        # sort by team then name for atlas clarity and purge-all ordering
        rows = sorted(
            rows,
            key=lambda r: (
                (r.get("Team") or r.get("team") or "").lower(),
                (r.get("Full Name") or r.get("full_name") or "").lower(),
            ),
        )

        for display_index, row in enumerate(rows, start=1):
            order = str(display_index)
            worker_id = (row.get("Worker ID") or row.get("worker_id") or "").strip()
            full_name = row.get("Full Name") or row.get("full_name") or ""
            team = row.get("Team") or row.get("team") or ""
            tag = "purged" if worker_id and worker_id in purged_ids else "pending"
            if tag == "purged":
                purged_count += 1
            values = (order, worker_id, full_name, team)
            self.worker_atlas_tree.insert("", "end", values=values, tags=(tag,))
            manifest_workers += 1

        self.worker_atlas_status_var.set(
            f"Worker atlas loaded: {manifest_workers} worker(s). Purged: {purged_count}."
        )
        self._update_worker_manifest_timestamp()

    def _load_profile_animation(self):
        gif_path = ASSETS_DIR / "maindp.gif"
        if not gif_path.exists():
            return
        diameter = 248
        try:
            gif = Image.open(gif_path)
        except Exception:
            return

        mask = Image.new("L", (diameter, diameter), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, diameter, diameter), fill=255)
        frames = []
        try:
            for frame in ImageSequence.Iterator(gif):
                frame = frame.convert("RGBA")
                trimmed = ImageOps.fit(frame, (diameter, diameter), centering=(0.5, 0.5))
                trimmed.putalpha(mask)
                frames.append(ImageTk.PhotoImage(trimmed))
        except Exception:
            frames = []
        finally:
            gif.close()
        self.profile_frames = frames

    def _animate_profile_gif(self):
        if not self.profile_frames or not self.gif_canvas:
            return
        frame = self.profile_frames[self.profile_frame_index]
        if self.gif_canvas_image is None:
            self.gif_canvas_image = self.gif_canvas.create_image(160, 160, image=frame)
        else:
            self.gif_canvas.itemconfig(self.gif_canvas_image, image=frame)
        self.profile_frame_index = (self.profile_frame_index + 1) % len(self.profile_frames)
        self.after(120, self._animate_profile_gif)

    # ---------------------- Logging & Threads ---------------------- #
    def _enqueue_log(self, message):
        self.log_queue.put(message)

    def _drain_log_queue(self):
        while not self.log_queue.empty():
            entry = self.log_queue.get()
            self._append_log(entry)
        self.after(120, self._drain_log_queue)

    def _prompt_operator_name(self):
        default_name = self.operator_name or "Operator Zero"
        try:
            response = simpledialog.askstring(
                "Operator Identification",
                "Enter your codename:",
                parent=self,
                initialvalue=default_name,
            )
        except Exception:
            response = default_name
        name = (response or "").strip() or default_name
        self.operator_name = name
        set_operator_name(name)
        greeting = f"Thanks for using my Middleware, {name}; This time I'm not charging you ;)"
        self.status_var.set(greeting)
        self._append_log(self._timestamp(greeting))

    def _append_log(self, text):
        self.log_view.configure(state="normal")
        self.log_view.insert("end", text + "\n")
        self.log_view.see("end")
        self.log_view.configure(state="disabled")

    def _handle_engage(self):
        if self.is_running:
            return
        client_id = self.client_id_var.get().strip()
        if not client_id:
            messagebox.showerror("TurnpointPurger", "Client ID is required to engage the purge.")
            return
        self._append_log(self._timestamp("Directive accepted. Spinning up purge chamber..."))
        self.status_var.set(f"Purging system engaged for CID {client_id}.")
        self._set_running(True)
        self.run_thread = threading.Thread(
            target=self._execute_purge, args=(client_id,), daemon=True
        )
        self.run_thread.start()

    def _handle_reset_purge(self):
        if self.is_running:
            messagebox.showwarning(
                "TurnpointPurger",
                "Pause the active purge before resetting the archives.",
            )
            return
        confirm = messagebox.askyesno(
            "Reset Purge",
            "This will delete every PurgedClients archive and reset counters.\n"
            "Proceed?",
            icon="warning",
        )
        if not confirm:
            return
        try:
            reset_purge_data()
            if self.manifest_path and Path(self.manifest_path).exists():
                Path(self.manifest_path).unlink(missing_ok=True)
            self._load_manifest_table()
            notice = self._timestamp("Purge archives wiped. Counters restored to zero.")
            self._append_log(notice)
            self.status_var.set("Purge archive reset. Awaiting new directives.")
            messagebox.showinfo(
                "TurnpointPurger",
                "Purge archives and counters have been reset.",
            )
            self._refresh_sequence_stats()
        except Exception as exc:
            messagebox.showerror(
                "TurnpointPurger",
                f"Reset failed:\n{exc}",
            )
    def _handle_set_credentials(self):
        dialog = tk.Toplevel(self)
        dialog.title("Purging Credentials")
        dialog.configure(bg="#03060f")
        dialog.resizable(False, False)
        dialog.grab_set()

        tk.Label(
            dialog,
            text="TurnPoint Email",
            fg="#a8d8ff",
            bg="#03060f",
            font=("Space Mono", 11),
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(18, 4))
        email_var = tk.StringVar(value=self.credential_username)
        email_entry = tk.Entry(
            dialog,
            textvariable=email_var,
            width=32,
            font=("JetBrains Mono", 12),
            bg="#0a1324",
            fg="#e9f2ff",
            insertbackground="#18e0ff",
            relief="flat",
        )
        email_entry.grid(row=1, column=0, sticky="we", padx=16)

        tk.Label(
            dialog,
            text="TurnPoint Password",
            fg="#a8d8ff",
            bg="#03060f",
            font=("Space Mono", 11),
        ).grid(row=2, column=0, sticky="w", padx=16, pady=(16, 4))
        password_var = tk.StringVar(value=self.credential_password)
        password_entry = tk.Entry(
            dialog,
            textvariable=password_var,
            width=32,
            font=("JetBrains Mono", 12),
            bg="#0a1324",
            fg="#e9f2ff",
            insertbackground="#18e0ff",
            relief="flat",
            show="*",
        )
        password_entry.grid(row=3, column=0, sticky="we", padx=16)

        def submit():
            email = email_var.get().strip()
            if not email:
                messagebox.showerror(
                    "TurnpointPurger",
                    "Can't do much without credentials bro ...",
                    parent=dialog,
                )
                return
            password = password_var.get()
            if not password:
                messagebox.showerror(
                    "TurnpointPurger",
                    "Password is required for the purge account.",
                    parent=dialog,
                )
                return
            configure_credentials(email, password)
            self.credential_username = email
            self.credential_password = password
            self._refresh_credential_display()
            self._append_log(
                self._timestamp(f"Purging account updated for {self.credential_username}")
            )
            self._toggle_discovery_section()
            dialog.destroy()

        action_row = tk.Frame(dialog, bg="#03060f")
        action_row.grid(row=4, column=0, pady=20)
        ttk.Button(action_row, text="Save", style="Cyber.TButton", command=submit).pack(
            side="left", padx=8
        )
        ttk.Button(
            action_row,
            text="Cancel",
            style="Danger.TButton",
            command=dialog.destroy,
        ).pack(side="left", padx=8)

        email_entry.focus_set()

    def _run_button_task(self, button, worker):
        if button is None:
            threading.Thread(target=worker, daemon=True).start()
            return
        button.configure(state="disabled")

        def runner():
            try:
                worker()
            finally:
                self.after(0, lambda: button.configure(state="normal"))

        threading.Thread(target=runner, daemon=True).start()

    def _get_manifest_packages(self):
        manifest = Path(self.manifest_path)
        packages = []
        if manifest.exists():
            try:
                with manifest.open("r", newline="", encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    seen = set()
                    for row in reader:
                        pkg = (row.get("Package") or row.get("package") or "").strip()
                        if pkg and pkg not in seen:
                            seen.add(pkg)
                            packages.append(pkg)
            except Exception:
                packages = []
        if not packages:
            packages = PACKAGE_FALLBACK_NAMES
        return packages

    def _start_bundle_download(self, packages, update, button):
        if self.bundle_progress:
            self.bundle_progress.start(14)
        self.bundle_timestamp_var.set("Bundle download in progress...")

        def task():
            try:
                result = bundle_package_download(
                    packages=packages,
                    headless=self.headless_var.get(),
                    refresh=update,
                    overwrite=update,
                )
                self.last_dataset_path = result.get("excel_path")
                exports = result.get("exports", [])
                completed = [e for e in exports if e and not e.get("skipped")]
                skipped = [e for e in exports if e and e.get("skipped")]
                summary = (
                    f"Package bundle {'updated' if update else 'created'}: "
                    f"{len(completed)} package(s) exported, {len(skipped)} skipped. "
                    f"Source workbook: {self.last_dataset_path}"
                )
                self._enqueue_log(self._timestamp(summary))
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.after(0, lambda: self.bundle_timestamp_var.set(f"Bundle last run: {ts}"))
                self.after(0, lambda: messagebox.showinfo("TurnpointPurger", summary))
            except Exception as exc:
                error = f"Bundle download failed: {exc}"
                self._enqueue_log(self._timestamp(error))
                self.after(0, lambda: self.bundle_timestamp_var.set("Bundle failed. See log for details."))
                self.after(0, lambda: messagebox.showerror("TurnpointPurger", error))
            finally:
                if self.bundle_progress:
                    self.after(0, self.bundle_progress.stop)

        self._run_button_task(button, task)

    def _open_package_picker(self, button, update=False):
        if button["state"] == "disabled":
            return
        button.configure(state="disabled")
        picker = tk.Toplevel(self)
        picker.title("Select Packages")
        picker.configure(bg="#03060f")
        picker.grab_set()

        def on_close():
            if button:
                button.configure(state="normal")
            picker.destroy()

        picker.protocol("WM_DELETE_WINDOW", on_close)

        tk.Label(
            picker,
            text="Choose package(s) for bundle download",
            fg="#a8d8ff",
            bg="#03060f",
            font=("Space Mono", 11, "bold"),
        ).pack(padx=20, pady=(16, 8))

        body = tk.Frame(picker, bg="#03060f")
        body.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        canvas = tk.Canvas(body, bg="#03060f", highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg="#03060f")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        def queue_all():
            self._start_bundle_download(None, update, button)
            picker.destroy()

        ttk.Button(
            inner,
            text="All Packages",
            style="Cyber.TButton",
            command=queue_all,
        ).pack(fill="x", pady=4)

        for pkg in self._get_manifest_packages():
            ttk.Button(
                inner,
                text=pkg,
                style="Cyber.TButton",
                command=lambda p=pkg: (self._start_bundle_download([p], update, button), picker.destroy()),
            ).pack(fill="x", pady=2)

    def _handle_collect_packages(self):
        def task():
            try:
                result = collect_clients_by_package(headless=self.headless_var.get())
                manifest_path = Path(result.get("manifest_path", PACKAGE_MANIFEST_PATH))
                self.manifest_path = manifest_path
                count = result.get("count", 0)
                packages = result.get("packages") or []
                message = (
                    f"Package collection complete: {count} unique client(s) across "
                    f"{len(packages)} package(s).\nManifest saved at:\n{manifest_path}"
                )
                self._enqueue_log(self._timestamp(message))
                self.after(0, self._load_manifest_table)
                self.after(0, self._update_manifest_timestamp)
                self.after(0, lambda: messagebox.showinfo("TurnpointPurger", message))
            except Exception as exc:
                error = f"Package collection failed: {exc}"
                self._enqueue_log(self._timestamp(error))
                self.after(0, lambda: messagebox.showerror("TurnpointPurger", error))

        self._run_button_task(self.collect_packages_button, task)

    def _read_manifest_entries(self):
        manifest = Path(self.manifest_path)
        if not manifest.exists():
            raise FileNotFoundError(
                f"Manifest not found at {manifest}. Collect package manifest first."
            )
        entries = []
        with manifest.open("r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                client_id = (row.get("Client ID") or row.get("client_id") or "").strip()
                if not client_id:
                    continue
                entries.append(
                    {
                        "client_id": client_id,
                        "client_name": row.get("Client Name") or row.get("client_name") or "",
                        "package": row.get("Package") or row.get("package") or "",
                    }
                )
        return entries

    def _read_worker_manifest_entries(self):
        manifest = Path(self.worker_manifest_path)
        if not manifest.exists():
            raise FileNotFoundError(
                f"Worker manifest not found at {manifest}. Collect worker manifest first."
            )
        entries = []
        with manifest.open("r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                worker_id = (row.get("Worker ID") or row.get("worker_id") or "").strip()
                if not worker_id:
                    continue
                entries.append(
                    {
                        "worker_id": worker_id,
                        "full_name": row.get("Full Name") or row.get("full_name") or "",
                        "team": row.get("Team") or row.get("team") or "",
                    }
                )
        return entries

    def _lookup_worker_manifest_entry(self, worker_id):
        manifest = Path(self.worker_manifest_path)
        if not manifest.exists():
            return {}
        try:
            with manifest.open("r", newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    rid = (row.get("Worker ID") or row.get("worker_id") or "").strip()
                    if rid == str(worker_id):
                        return {
                            "worker_id": rid,
                            "full_name": row.get("Full Name") or row.get("full_name") or "",
                            "team": row.get("Team") or row.get("team") or "",
                        }
        except Exception:
            return {}
        return {}

    def _handle_purge_all_clients(self):
        if self.is_running:
            messagebox.showwarning(
                "TurnpointPurger",
                "Finish the current purge before running the purge-all cycle.",
            )
            return

        confirm = messagebox.askyesno(
            "Purge All Clients",
            "This will sequentially purge every client in the manifest. Continue?",
            icon="warning",
        )
        if not confirm:
            return

        try:
            entries = self._read_manifest_entries()
        except Exception as exc:
            messagebox.showerror("TurnpointPurger", str(exc))
            return
        if not entries:
            messagebox.showinfo(
                "TurnpointPurger",
                "Manifest is empty. Generate the manifest before purging.",
            )
            return

        cooldown_seconds = self._resolve_cooldown_seconds()
        self.purge_all_button.configure(state="disabled")

        total = len(entries)

        def task():
            self._set_running(True)
            completed = duplicates = failed = 0
            for index, entry in enumerate(entries, start=1):
                client_id = entry["client_id"]
                client_name = entry.get("client_name") or None
                try:
                    self._enqueue_log(
                        self._timestamp(
                            f"[Purge All] ({index}/{total}) Engaging client {client_id}"
                        )
                    )
                    run_turnpoint_purge(
                        client_id,
                        client_name=client_name,
                        headless=self.headless_var.get(),
                        allow_duplicate=False,
                        prompt_on_duplicate=False,
                    )
                    completed += 1
                except DuplicateClientError as exc:
                    duplicates += 1
                    self._enqueue_log(self._timestamp(f"[Purge All] Duplicate skipped: {exc}"))
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    self._enqueue_log(
                        self._timestamp(f"[Purge All] Error on {client_id}: {exc}")
                    )
                finally:
                    self.after(0, self._load_manifest_table)
                    self.after(0, self._refresh_sequence_stats)

                if index < total and cooldown_seconds > 0:
                    self.after(0, lambda s=cooldown_seconds: self._start_cooldown_timer(s))
                    self._sleep_with_override(cooldown_seconds)
                    self.after(0, self._cancel_cooldown_timer)
                    self.after(0, lambda: self.cooldown_label_var.set("Cooldown idle"))

            self.after(0, self._cancel_cooldown_timer)
            self.after(0, lambda: self.cooldown_bar.configure(value=0))
            self.after(0, lambda: self.cooldown_label_var.set("Cooldown idle"))
            summary = (
                f"Purge-all finished: {completed} completed, {duplicates} duplicates, {failed} failed."
            )
            self._enqueue_log(self._timestamp(summary))
            self.after(0, self._load_manifest_table)
            self.after(0, self._refresh_sequence_stats)
            self.after(0, lambda: messagebox.showinfo("TurnpointPurger", summary))
            self.after(0, lambda: self._set_running(False))
            self.after(0, lambda: self.purge_all_button.configure(state="normal"))

        threading.Thread(target=task, daemon=True).start()

    def _handle_collect_workers(self):
        def task():
            try:
                result = collect_workers(headless=self.headless_var.get())
                manifest_path = Path(result.get("manifest_path", WORKER_MANIFEST_PATH))
                self.worker_manifest_path = manifest_path
                count = result.get("count", 0)
                message = (
                    f"Worker manifest collected: {count} worker(s).\n"
                    f"Manifest saved at:\n{manifest_path}"
                )
                self._enqueue_log(self._timestamp(message))
                self.after(0, self._load_worker_manifest_table)
                self.after(0, self._update_worker_manifest_timestamp)
                self.after(0, lambda: messagebox.showinfo("TurnpointPurger", message))
            except Exception as exc:
                error = f"Worker manifest collection failed: {exc}"
                self._enqueue_log(self._timestamp(error))
                self.after(0, lambda: messagebox.showerror("TurnpointPurger", error))

        self._run_button_task(self.worker_collect_button, task)

    def _handle_download_workers_excel(self):
        def task():
            try:
                path = download_worker_excel(headless=self.headless_var.get())
                message = (
                    f"Worker Excel snapshot downloaded:\n{path}\n"
                    "Use Refresh Worker Atlas after generating the manifest."
                )
                self._enqueue_log(self._timestamp(message))
                self.after(0, lambda: messagebox.showinfo("TurnpointPurger", message))
            except Exception as exc:
                error = f"Worker Excel download failed: {exc}"
                self._enqueue_log(self._timestamp(error))
                self.after(0, lambda: messagebox.showerror("TurnpointPurger", error))

        self._run_button_task(self.worker_excel_button, task)

    def _handle_worker_engage(self):
        if self.is_running:
            return
        worker_id = self.worker_id_var.get().strip()
        if not worker_id:
            messagebox.showerror(
                "TurnpointPurger", "Worker ID is required to engage the purge."
            )
            return
        self._enqueue_log(self._timestamp("Worker directive accepted. Spooling up..."))
        self.worker_status_var.set(f"Purging system engaged for worker {worker_id}.")
        self._set_worker_running(True)
        threading.Thread(
            target=self._execute_worker_purge, args=(worker_id,), daemon=True
        ).start()

    def _handle_worker_reset(self):
        if self.is_running:
            messagebox.showwarning(
                "TurnpointPurger",
                "Pause the active purge before resetting the worker archives.",
            )
            return
        confirm = messagebox.askyesno(
            "Reset Worker Purge",
            "This will delete every PurgedWorker archive and reset worker counters.\nProceed?",
            icon="warning",
        )
        if not confirm:
            return
        try:
            reset_worker_data()
            if self.worker_manifest_path and Path(self.worker_manifest_path).exists():
                Path(self.worker_manifest_path).unlink(missing_ok=True)
            self._load_worker_manifest_table()
            notice = self._timestamp("Worker purge archives wiped. Counters restored.")
            self._enqueue_log(notice)
            self.worker_status_var.set("Worker archive reset. Awaiting new directives.")
            messagebox.showinfo(
                "TurnpointPurger",
                "Worker archives and counters have been reset.",
            )
            self._refresh_worker_sequence_stats()
        except Exception as exc:
            messagebox.showerror(
                "TurnpointPurger",
                f"Reset failed:\n{exc}",
            )

    def _handle_worker_purge_all(self):
        if self.is_running:
            messagebox.showwarning(
                "TurnpointPurger",
                "Finish the current purge before running the worker purge-all cycle.",
            )
            return

        confirm = messagebox.askyesno(
            "Purge All Workers",
            "This will sequentially purge every worker in the manifest. Continue?",
            icon="warning",
        )
        if not confirm:
            return

        try:
            entries = self._read_worker_manifest_entries()
        except Exception as exc:
            messagebox.showerror("TurnpointPurger", str(exc))
            return
        if not entries:
            messagebox.showinfo(
                "TurnpointPurger",
                "Worker manifest is empty. Generate the manifest before purging.",
            )
            return

        cooldown_seconds = self._resolve_worker_cooldown_seconds()

        def task():
            self._set_worker_running(True)
            completed = failed = 0
            total = len(entries)
            for index, entry in enumerate(entries, start=1):
                worker_id = entry["worker_id"]
                worker_name = entry.get("full_name") or None
                try:
                    self._enqueue_log(
                        self._timestamp(
                            f"[Worker Purge All] ({index}/{total}) Engaging worker {worker_id}"
                        )
                    )
                    run_worker_purge(
                        worker_id,
                        worker_name=worker_name,
                        worker_team=entry.get("team"),
                        headless=self.headless_var.get(),
                    )
                    completed += 1
                except RuntimeError as exc:
                    self._enqueue_log(self._timestamp(f"[Worker Purge All] Skipped: {exc}"))
                except Exception as exc:
                    failed += 1
                    self._enqueue_log(
                        self._timestamp(f"[Worker Purge All] Error on {worker_id}: {exc}")
                    )
                finally:
                    self.after(0, self._load_worker_manifest_table)
                    self.after(0, self._refresh_worker_sequence_stats)

                if index < total and cooldown_seconds > 0:
                    self.after(
                        0, lambda s=cooldown_seconds: self._start_worker_cooldown_timer(s)
                    )
                    self._sleep_with_worker_override(cooldown_seconds)
                    self.after(0, self._cancel_worker_cooldown_timer)
                    self.after(0, lambda: self.worker_cooldown_label_var.set("Cooldown idle"))

            self.after(0, self._cancel_worker_cooldown_timer)
            self.after(0, lambda: self.worker_cooldown_bar.configure(value=0))
            self.after(0, lambda: self.worker_cooldown_label_var.set("Cooldown idle"))
            summary = (
                f"Worker purge-all finished: {completed} completed, {failed} failed/duplicates."
            )
            self._enqueue_log(self._timestamp(summary))
            self.after(0, self._load_worker_manifest_table)
            self.after(0, self._refresh_worker_sequence_stats)
            self.after(0, lambda: messagebox.showinfo("TurnpointPurger", summary))
            self.after(0, lambda: self._set_worker_running(False))

        self._run_button_task(self.worker_purge_all_button, task)

    def _handle_find_purgeable_clients(self):
        def task():
            try:
                result = find_purgeable_clients(headless=self.headless_var.get())
                self.last_dataset_path = result.get("excel_path")
                packages = result.get("packages", [])
                count = result.get("record_count", 0)
                message = (
                    f"Purgeable discovery complete: {count} client(s) across {len(packages)} package(s).\n"
                    f"Snapshot stored at:\n{self.last_dataset_path}"
                )
                self._enqueue_log(self._timestamp(message))
                self.after(0, lambda: messagebox.showinfo("TurnpointPurger", message))
            except Exception as exc:
                error = f"Purgeable client discovery failed: {exc}"
                self._enqueue_log(self._timestamp(error))
                self.after(0, lambda: messagebox.showerror("TurnpointPurger", error))

        self._run_button_task(self.find_button, task)

    def _handle_bundle_download(self, update=False):
        button = self.update_bundle_button if update else self.bundle_button
        self._open_package_picker(button, update=update)

    def _execute_purge(self, client_id):
        try:
            output_dir = run_turnpoint_purge(client_id, headless=self.headless_var.get())
            self._enqueue_log(
                self._timestamp(f"Purging sweep finished. Output archived @ {output_dir}")
            )
            self._notify_completion(success=True, output=str(output_dir))
        except DuplicateClientError as exc:
            last_purge = format_timestamp((exc.record or {}).get("timestamp"))
            message = (
                f"Client {exc.client_id} already has a purge from {last_purge}."
            )
            if exc.report_path:
                message += f" Duplicate notice: {exc.report_path}"
            self._enqueue_log(self._timestamp(message))
            self._notify_completion(success=False, error=message)
        except Exception as exc:
            self._enqueue_log(self._timestamp(f"Purging failure: {exc}"))
            self._notify_completion(success=False, error=str(exc))

    def _execute_worker_purge(self, worker_id):
        try:
            metadata = self._lookup_worker_manifest_entry(worker_id)
            worker_name = metadata.get("full_name") or None
            worker_team = metadata.get("team") or None
            output_dir = run_worker_purge(
                worker_id,
                worker_name=worker_name,
                worker_team=worker_team,
                headless=self.headless_var.get(),
            )
            self._enqueue_log(
                self._timestamp(
                    f"Worker purge finished. Output archived @ {output_dir}"
                )
            )
            self._notify_worker_completion(success=True, output=str(output_dir))
        except Exception as exc:
            self._enqueue_log(self._timestamp(f"Worker purge failure: {exc}"))
            self._notify_worker_completion(success=False, error=str(exc))

    def _handle_nexis_scan(self):
        root = Path(self.nexis_root_var.get()).expanduser()
        if not root.exists():
            messagebox.showerror("NexisUploader", f"Path not found:\n{root}")
            return
        try:
            workers = discover_workers(root)
        except Exception as exc:
            messagebox.showerror("NexisUploader", f"Scan failed:\n{exc}")
            return
        self._cached_workers = workers
        if not self.nexis_table:
            return
        self.nexis_table.delete(*self.nexis_table.get_children())
        for idx, worker in enumerate(workers, start=1):
            email = worker.data.get("Email") or worker.data.get("Mobile") or ""
            values = (idx, worker.worker_id, worker.full_name, worker.team, email)
            self.nexis_table.insert(
                "", "end", values=values, tags=("pending",), iid=worker.path.as_posix()
            )
        self.nexis_count_var.set(f"Workers discovered: {len(workers)} (sorted by team/name)")

        # scan clients for display
        headers, client_rows = self._collect_client_rows(
            Path(self.clients_root_var.get()).expanduser(), None
        )
        if hasattr(self, "client_table") and self.client_table:
            self.client_table.delete(*self.client_table.get_children())
            for idx, row in enumerate(client_rows, start=1):
                values = (
                    idx,
                    row.get("Client ID") or row.get("System ID") or "",
                    row.get("Client Name") or "",
                    row.get("Default Package") or "",
                )
                self.client_table.insert("", "end", values=values)

    def _handle_nexis_select(self, event):
        if not self.nexis_table or not self.nexis_preview:
            return
        sel = self.nexis_table.selection()
        if not sel:
            return
        path_str = sel[0]
        try:
            root = Path(self.nexis_root_var.get()).expanduser()
            records = self._load_discovered_workers(root)
            record = next((w for w in records if w.path.as_posix() == path_str), None)
            if not record:
                return
            preview_text = preview_payload(record)
        except Exception as exc:
            preview_text = f"Error building payload:\n{exc}"
        self.nexis_preview.configure(state="normal")
        self.nexis_preview.delete("1.0", "end")
        self.nexis_preview.insert("end", preview_text)
        self.nexis_preview.configure(state="disabled")

    def _handle_client_select(self, event):
        if not self.client_table or not self.nexis_preview:
            return
        sel = self.client_table.selection()
        if not sel:
            return
        # For clients, just preview the raw row as JSON
        item = self.client_table.item(sel[0])
        values = item.get("values") or []
        payload = {
            "ClientID": values[1] if len(values) > 1 else "",
            "ClientName": values[2] if len(values) > 2 else "",
            "Package": values[3] if len(values) > 3 else "",
        }
        import json

        preview_text = json.dumps(payload, indent=2)
        self.nexis_preview.configure(state="normal")
        self.nexis_preview.delete("1.0", "end")
        self.nexis_preview.insert("end", preview_text)
        self.nexis_preview.configure(state="disabled")
    def _handle_export_cleaned(self):
        if not self.nexis_table:
            return
        sel = self.nexis_table.selection()
        if not sel:
            messagebox.showinfo("NexisUploader", "Select a worker row first.")
            return
        path_str = sel[0]
        root = Path(self.nexis_root_var.get()).expanduser()
        cleaned_root = Path(self.cleaned_root_var.get()).expanduser()
        records = self._load_discovered_workers(root)
        record = next((w for w in records if w.path.as_posix() == path_str), None)
        if not record:
            messagebox.showerror("NexisUploader", "Unable to locate selected worker record.")
            return
        cleaned_root.mkdir(parents=True, exist_ok=True)
        # determine next 10000x sequence
        next_id = self._next_clean_id(cleaned_root)
        target = cleaned_root / self._build_clean_filename(next_id, record.full_name)
        try:
            import shutil

            shutil.copy2(record.path, target)
            self._enqueue_log(self._timestamp(f"Exported {record.full_name} -> {target}"))
            messagebox.showinfo("NexisUploader", f"Exported to {target}")
        except Exception as exc:
            messagebox.showerror("NexisUploader", f"Export failed:\n{exc}")

    def _handle_export_all_cleaned(self):
        root = Path(self.nexis_root_var.get()).expanduser()
        cleaned_root = Path(self.cleaned_root_var.get()).expanduser()
        records = self._load_discovered_workers(root)
        if not records:
            messagebox.showinfo("NexisUploader", "No workers to export. Scan first.")
            return
        cleaned_root.mkdir(parents=True, exist_ok=True)
        next_id = self._next_clean_id(cleaned_root)
        count = 0
        try:
            import shutil
        except ImportError:
            shutil = None
        for record in records:
            target = cleaned_root / self._build_clean_filename(next_id, record.full_name)
            try:
                if shutil:
                    shutil.copy2(record.path, target)
                else:
                    with record.path.open("rb") as src, target.open("wb") as dst:
                        dst.write(src.read())
                self._enqueue_log(self._timestamp(f"Exported {record.full_name} -> {target}"))
                next_id += 1
                count += 1
            except Exception as exc:
                self._enqueue_log(self._timestamp(f"Export failed for {record.full_name}: {exc}"))
                continue
        messagebox.showinfo("NexisUploader", f"Exported {count} worker CSVs to {cleaned_root}")

    def _handle_combine_nexis(self):
        root = Path(self.nexis_root_var.get()).expanduser()
        cleaned_root = Path(self.cleaned_root_var.get()).expanduser()
        records = self._load_discovered_workers(root)
        if not records:
            messagebox.showinfo("NexisUploader", "No workers to combine. Scan first.")
            return
        cleaned_root.mkdir(parents=True, exist_ok=True)
        # build nexis payloads
        nexis_records = [build_nexis_employee(r) for r in records]
        headers = []
        for rec in nexis_records:
            for key in rec.keys():
                if key not in headers:
                    headers.append(key)
        csv_path = cleaned_root / "combined_workers_nexis.csv"
        json_path = cleaned_root / "combined_workers_nexis.json"
        try:
            import csv
            import json
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=headers)
                writer.writeheader()
                for rec in nexis_records:
                    writer.writerow({h: rec.get(h, "") for h in headers})
            with json_path.open("w", encoding="utf-8") as fh:
                json.dump(nexis_records, fh, indent=2, ensure_ascii=False)
            self._enqueue_log(self._timestamp(f"Combined Nexis payloads -> {csv_path} / {json_path}"))
            messagebox.showinfo("NexisUploader", f"Nexis combined CSV/JSON written to:\n{csv_path}\n{json_path}")
        except Exception as exc:
            messagebox.showerror("NexisUploader", f"Combine failed:\n{exc}")

    def _handle_nexis_upload(self):
        if not self.nexis_table:
            return
        sel = self.nexis_table.selection()
        if not sel:
            messagebox.showinfo("NexisUploader", "Select a worker row first.")
            return
        path_str = sel[0]
        root = Path(self.nexis_root_var.get()).expanduser()
        records = self._load_discovered_workers(root)
        record = next((w for w in records if w.path.as_posix() == path_str), None)
        if not record:
            messagebox.showerror("NexisUploader", "Unable to locate selected worker record.")
            return
        user = self.nexis_user_var.get().strip()
        pwd = self.nexis_pass_var.get().strip()
        if not user or not pwd:
            messagebox.showerror("NexisUploader", "Set Nexis credentials before uploading.")
            return

        def task():
            try:
                payload_json = preview_payload(record)
                import json
                data = json.loads(payload_json)
                submit_employee(data, headless=self.headless_var.get(), username=user, password=pwd)
                self._enqueue_log(self._timestamp(f"Nexis upload complete for {record.full_name} ({record.worker_id})"))
                self.after(0, lambda: messagebox.showinfo("NexisUploader", f"Uploaded {record.full_name} to Nexis."))
            except Exception as exc:
                self._enqueue_log(self._timestamp(f"Nexis upload failed: {exc}"))
                self.after(
                    0,
                    lambda err=str(exc): messagebox.showerror(
                        "NexisUploader", f"Upload failed:\n{err}"
                    ),
                )

        self._run_button_task(None, task)

    def _set_rate_running(self, running):
        self.rate_running = running
        blocked = running or self.discovery_running
        for btn in (
            self.rate_capture_button,
            self.rate_import_button,
            self.rate_export_csv_button,
            self.rate_export_xlsx_button,
            self.rate_cleanup_button,
            self.rate_apply_button,
            self.rate_autofit_button,
        ):
            if btn:
                btn.configure(state="disabled" if blocked else "normal")
        if self.rate_group_combo:
            self.rate_group_combo.configure(state="disabled" if blocked else "readonly")
        if self.rate_search_entry:
            self.rate_search_entry.configure(state="disabled" if blocked else "normal")

    def _set_discovery_running(self, running):
        self.discovery_running = running
        blocked = running or self.rate_running
        if self.discovery_run_button:
            self.discovery_run_button.configure(
                state="disabled" if blocked else "normal"
            )
        if self.discovery_merge_button:
            self.discovery_merge_button.configure(
                state="disabled" if blocked else "normal"
            )
        if self.discovery_open_diagnostics_button:
            self.discovery_open_diagnostics_button.configure(
                state="disabled" if blocked else "normal"
            )
        if self.discovery_open_output_button:
            self.discovery_open_output_button.configure(
                state="disabled" if blocked else "normal"
            )
        if self.discovery_probe_entry:
            self.discovery_probe_entry.configure(
                state="disabled" if blocked else "normal"
            )
        for checkbox in (
            self.discovery_headless_checkbox,
            self.discovery_debug_checkbox,
        ):
            if checkbox:
                checkbox.configure(state="disabled" if blocked else "normal")
        self._set_rate_running(self.rate_running)

    def _append_rate_status(self, text):
        if not self.rate_log_view:
            return
        self.rate_log_view.configure(state="normal")
        self.rate_log_view.insert("end", text + "\n")
        self.rate_log_view.see("end")
        self.rate_log_view.configure(state="disabled")

    def _open_path_in_system(self, path):
        target = Path(path).expanduser()
        if not target.exists():
            raise FileNotFoundError(f"Path not found: {target}")
        if sys.platform.startswith("darwin"):
            subprocess.Popen(["open", str(target)])
            return
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
            return
        subprocess.Popen(["xdg-open", str(target)])

    def _handle_open_diagnostics_folder(self):
        folder = self.discovery_last_diagnostics_folder
        if not folder:
            messagebox.showwarning(
                "ServiceType → Rate Extractor",
                "No diagnostics folder recorded yet. Run discovery first.",
            )
            return
        try:
            self._open_path_in_system(folder)
        except Exception as exc:
            messagebox.showerror(
                "ServiceType → Rate Extractor",
                f"Unable to open diagnostics folder:\n{exc}",
            )

    def _handle_open_output_folder(self):
        folder = self.discovery_last_output_root
        if not folder:
            messagebox.showwarning(
                "ServiceType → Rate Extractor",
                "No output folder recorded yet. Run discovery or merge first.",
            )
            return
        try:
            self._open_path_in_system(folder)
        except Exception as exc:
            messagebox.showerror(
                "ServiceType → Rate Extractor",
                f"Unable to open output folder:\n{exc}",
            )

    def _latest_variant_diagnostics_dir(self) -> str:
        """Return the most recently modified variant diagnostics run folder."""
        base = line_item_paths.variants_diagnostics_dir()
        if not base.exists():
            return ""
        latest = None
        for child in base.iterdir():
            if not child.is_dir():
                continue
            try:
                mtime = child.stat().st_mtime
            except Exception:
                continue
            if latest is None or mtime > latest[0]:
                latest = (mtime, child)
        return str(latest[1]) if latest else ""

    def _handle_run_appointment_discovery(self):
        if self.discovery_running:
            return
        if self.rate_running:
            messagebox.showwarning(
                "ServiceType → Rate Extractor",
                "Wait for the metadata capture/import task to complete before running discovery.",
            )
            return

        probe_client_id = self.discovery_probe_client_var.get().strip()
        if not probe_client_id:
            messagebox.showerror(
                "ServiceType → Rate Extractor",
                "Probe Client ID is required for appointment discovery.",
            )
            return

        self._set_discovery_running(True)
        self.truth_store.clear()
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.rate_status_var.set(
            f"Appointment discovery in progress (started {started_at})..."
        )
        self._append_rate_status(
            self._timestamp(
                f"[AppointmentDiscovery] Started at {started_at} with probe {probe_client_id}"
            )
        )

        def on_progress(message):
            plain = str(message or "").strip()
            if not plain:
                return
            if plain.startswith("["):
                line = plain
            else:
                line = f"[AppointmentDiscovery] {plain}"
            stamped = self._timestamp(line)
            self._enqueue_log(stamped)
            self.after(0, lambda t=stamped: self._append_rate_status(t))

        def on_event(event):
            level = str(event.get("level", "INFO")).upper()
            if level not in {"WARN", "ERROR"}:
                return
            code = str(event.get("code", "EVENT")).strip() or "EVENT"
            message = str(event.get("message", "")).strip()
            line = f"[AppointmentDiscovery][{level}][{code}] {message}"
            stamped = self._timestamp(line)
            self._enqueue_log(stamped)
            self.after(0, lambda t=stamped: self._append_rate_status(t))

        def on_row(row):
            self.after(0, lambda r=dict(row): self._truth_store_ingest("discovery", r))

        def task():
            try:
                result = extract_service_type_variants(
                    headless=self.discovery_headless_var.get(),
                    probe_client_ids=probe_client_id,
                    on_progress=on_progress,
                    on_event=on_event,
                    on_row=on_row,
                    resume=True,
                    force_refresh=False,
                )
                self.discovery_last_result = dict(result)
                output_paths = dict(result.get("output_paths", {}))
                self.discovery_last_diagnostics_folder = str(output_paths.get("diagnostics_dir", ""))
                self.discovery_last_output_root = str(Path(output_paths.get("latest_xlsx", "")).parent)

                total_rows = result.get("total_variant_rows", 0)
                summary = (
                    f"Service Type variant extraction complete: {total_rows} variant row(s).\n"
                    f"Service Types processed: {result.get('processed_service_types', 0)}/{result.get('total_service_types', 0)}\n"
                    f"Clean variants: {result.get('clean_variants', 0)} | Conflicts: {result.get('conflict_variants', 0)}\n"
                    f"Diagnostics: {self.discovery_last_diagnostics_folder}\n"
                    f"CSV: {output_paths.get('latest_csv', '')}\n"
                    f"XLSX: {output_paths.get('latest_xlsx', '')}"
                )
                self._enqueue_log(
                    self._timestamp(f"[ServiceTypeVariants] {summary}")
                )
                self.after(0, lambda: self.rate_status_var.set(summary))
                self.after(
                    0,
                    lambda s=summary: self._append_rate_status(self._timestamp(s)),
                )
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        "ServiceType → Rate Extractor", summary
                    ),
                )
            except Exception as exc:
                error = f"Service Type variant extraction failed: {exc}"
                self._enqueue_log(self._timestamp(f"[ServiceTypeVariants] {error}"))
                diag_folder = self.discovery_last_diagnostics_folder or self._latest_variant_diagnostics_dir()
                if diag_folder:
                    self.discovery_last_diagnostics_folder = diag_folder
                    self._enqueue_log(
                        self._timestamp(
                            f"[ServiceTypeVariants] Diagnostics folder: {diag_folder}"
                        )
                    )
                self.after(
                    0,
                    lambda: self.rate_status_var.set(
                        "Service Type variant extraction failed. Full details saved in diagnostics."
                    ),
                )
                self.after(
                    0,
                    lambda: messagebox.showerror(
                        "ServiceType → Rate Extractor",
                        "Variant extraction failed.\n\n"
                        "Full details were saved in diagnostics.\n"
                        "Use 'Open Diagnostics Folder'.",
                    ),
                )
            finally:
                self.after(0, lambda: self._set_discovery_running(False))

        threading.Thread(target=task, daemon=True).start()

    def _handle_merge_service_types_from_discovery(self):
        if self.discovery_running:
            return
        if self.rate_running:
            messagebox.showwarning(
                "ServiceType → Rate Extractor",
                "Wait for the metadata capture/import task to complete before running merge.",
            )
            return

        self._set_discovery_running(True)
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.rate_status_var.set(f"Service type merge in progress (started {started_at})...")
        self._append_rate_status(
            self._timestamp(f"[AppointmentDiscovery] Merge started at {started_at}")
        )

        def on_progress(message):
            plain = str(message or "").strip()
            if not plain:
                return
            line = plain if plain.startswith("[") else f"[AppointmentDiscovery] {plain}"
            stamped = self._timestamp(line)
            self._enqueue_log(stamped)
            self.after(0, lambda t=stamped: self._append_rate_status(t))

        def task():
            try:
                discovered_rows = list(self.discovery_last_result.get("rows", []) or [])
                if not discovered_rows:
                    discovered_rows = load_discovery_latest()
                if not discovered_rows:
                    raise RuntimeError(
                        "No discovery rows available. Run appointment discovery first."
                    )

                merged = run_service_type_merge(discovered_rows, progress=on_progress)
                enriched_path = str(merged.get("enriched_latest_csv", "") or "")
                if enriched_path:
                    self.discovery_last_output_root = str(Path(enriched_path).expanduser().parent)

                # Load enriched rows into truth store (main thread)
                enriched_rows = merged.get("enriched_rows", []) or []
                for row in enriched_rows:
                    self.after(0, lambda r=dict(row): self._truth_store_ingest("discovery", r))

                summary = (
                    f"Merge complete: enriched={merged.get('enriched_count', 0)} "
                    f"| unmatched={merged.get('unmatched_count', 0)}\n"
                    f"Enriched CSV: {merged.get('enriched_latest_csv', '')}\n"
                    f"Unmatched CSV: {merged.get('unmatched_latest_csv', '')}"
                )
                self._enqueue_log(
                    self._timestamp(f"[AppointmentDiscovery] {summary}")
                )
                self.after(0, lambda: self.rate_status_var.set(summary))
                self.after(
                    0,
                    lambda s=summary: self._append_rate_status(self._timestamp(s)),
                )
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        "ServiceType → Rate Extractor", summary
                    ),
                )
            except Exception as exc:
                error = f"Service type merge failed: {exc}"
                self._enqueue_log(self._timestamp(f"[AppointmentDiscovery] {error}"))
                self.after(
                    0,
                    lambda: self.rate_status_var.set(
                        "Service type merge failed. Inspect logs for details."
                    ),
                )
                self.after(
                    0,
                    lambda err=str(exc): messagebox.showerror(
                        "ServiceType → Rate Extractor",
                        f"Service type merge failed:\n{err}",
                    ),
                )
            finally:
                self.after(0, lambda: self._set_discovery_running(False))

        threading.Thread(target=task, daemon=True).start()

    # -----------------------------------------------------------------------
    # Truth Grid helpers
    # -----------------------------------------------------------------------

    def _on_truth_store_changed(self):
        """Called by truth_store when data changes. Triggers debounced grid refresh."""
        if self.rate_freeze_var.get():
            return
        if not self.rate_pending_refresh:
            self.rate_pending_refresh = True
            self.rate_refresh_job_id = self.after(50, self._refresh_truth_grid)

    def _rate_help_text(self, inline: bool = False) -> str:
        if inline:
            return (
                "BLUE: resolved (Rate + Item Number). "
                "YELLOW: partially resolved. "
                "RED: missing values. "
                "Rate Source / Item Source = source chosen for each truth field. "
                "Updated (UTC) = last truth refresh timestamp."
            )
        return (
            "Legend:\n"
            "- BLUE rows: both Rate and Item Number resolved.\n"
            "- YELLOW rows: partially resolved (only one of Rate/Item Number).\n"
            "- RED rows: unresolved or missing required values.\n\n"
            "Columns:\n"
            "- Rate Source: source currently selected for the truth Rate.\n"
            "- Item Source: source currently selected for the truth Item Number.\n"
            "- Updated (UTC): when that record was last resolved in UTC.\n"
        )

    def _show_rate_help_dialog(self):
        messagebox.showinfo("Rate Extractor Help", self._rate_help_text(inline=False))

    def _configure_truth_grid_columns(self):
        if not self.truth_grid:
            return
        widths = [
            self._scaled_px(88),   # Status
            self._scaled_px(300),  # Parent Service Type
            self._scaled_px(120),  # Variant Prefix
            self._scaled_px(260),  # Service Variant
            self._scaled_px(230),  # Variant ID
            self._scaled_px(110),  # Rate
            self._scaled_px(220),  # Item Number
            self._scaled_px(120),  # Rate Source
            self._scaled_px(120),  # Item Source
            self._scaled_px(165),  # Updated (UTC)
        ]
        self.truth_grid.set_column_widths(column_widths=widths)
        self.truth_grid.align_columns(columns=[5], align="e", redraw=False)

    def _handle_auto_fit_truth_columns(self):
        if not self.truth_grid:
            return
        widths = []
        for col_idx in range(10):
            auto_width = self.truth_grid.get_column_text_width(
                col_idx, visible_only=True, only_if_too_small=False
            )
            min_width = self._scaled_px(80)
            max_width = self._scaled_px(500)
            widths.append(max(min_width, min(max_width, int(auto_width))))
        self.truth_grid.set_column_widths(column_widths=widths)
        self.truth_grid.align_columns(columns=[5], align="e", redraw=True)

    def _get_filtered_truth_records(self):
        group = self.rate_group_var.get()
        if group == "All Groups":
            records = self.truth_store.get_all_records()
        else:
            records = self.truth_store.get_records_for_parent(group)

        search = self.rate_search_var.get().strip().lower()
        if search:
            filtered = []
            for rec in records:
                text = (
                    f"{rec.parent_service_type} "
                    f"{rec.service_variant_prefix} "
                    f"{rec.service_variant_label} "
                    f"{rec.service_type_id} {rec.truth_rate} {rec.truth_item_number}"
                ).lower()
                if search in text:
                    filtered.append(rec)
            records = filtered

        return sorted(
            records,
            key=lambda r: (
                r.parent_service_type.lower(),
                r.service_variant_prefix.lower(),
                r.service_variant_label.lower(),
                r.service_type_id,
            ),
        )

    def _refresh_truth_grid(self):
        """Refresh the truth grid from the truth store."""
        self.rate_pending_refresh = False
        if not self.truth_grid:
            return

        records = self._get_filtered_truth_records()
        self._visible_truth_records = records

        # Build grid data
        data = []
        for rec in records:
            row = [
                rec.status.upper(),
                rec.parent_service_type,
                rec.service_variant_prefix,
                rec.service_variant_label,
                rec.service_type_id,
                rec.truth_rate,
                rec.truth_item_number,
                rec.truth_rate_source,
                rec.truth_item_source,
                rec.updated_utc,
            ]
            data.append(row)

        self.truth_grid.set_sheet_data(data)
        self.truth_grid.dehighlight_all()

        # Apply row colors
        for idx, rec in enumerate(records):
            if rec.status == "red":
                self.truth_grid.highlight_rows([idx], bg="#cc3333", fg="#ffffff")
            elif rec.status == "yellow":
                self.truth_grid.highlight_rows([idx], bg="#ccaa00", fg="#1a1a1a")
            elif rec.status == "blue":
                self.truth_grid.highlight_rows([idx], bg="#2266cc", fg="#ffffff")

            # Per-cell conflict highlighting (mandatory)
            if rec.rate_conflict:
                self.truth_grid.highlight_cells(row=idx, column=5, bg="#cc0000", fg="#ffffff")
                parts = [f"{src}: {c.value}" for src, c in rec.rate_candidates.items() if c.value]
                self.truth_grid.note((idx, 5), note="CONFLICT\n" + " vs ".join(parts), readonly=True)
            if rec.item_conflict:
                self.truth_grid.highlight_cells(row=idx, column=6, bg="#cc0000", fg="#ffffff")
                parts = [f"{src}: {c.value}" for src, c in rec.item_candidates.items() if c.value]
                self.truth_grid.note((idx, 6), note="CONFLICT\n" + " vs ".join(parts), readonly=True)

        self._update_status_strip()
        self._refresh_group_selector()
        self._update_rate_variant_count(records)

    def _update_status_strip(self):
        """Update the status strip with RED/YELLOW/BLUE counts."""
        if not self.rate_status_strip:
            return
        counts = self.truth_store.get_status_counts()
        now = datetime.now().strftime("%H:%M:%S")
        text = f"RED: {counts['red']} | YELLOW: {counts['yellow']} | BLUE: {counts['blue']} | Conflicts: {counts['conflicts']} | Last: {now}"
        self.rate_status_strip.config(text=text)

    def _refresh_group_selector(self):
        """Refresh the Service Group combobox with current parent groups."""
        if not self.rate_group_combo:
            return
        selected = self.rate_group_var.get()
        groups = self.truth_store.get_parent_groups()
        all_groups = ["All Groups"] + groups
        self.rate_group_combo["values"] = all_groups
        if selected not in all_groups:
            self.rate_group_var.set("All Groups")

    def _update_rate_variant_count(self, records):
        group = self.rate_group_var.get()
        if group == "All Groups":
            self.rate_variant_count_var.set(f"{len(records)} variants shown")
        else:
            self.rate_variant_count_var.set(f"{len(records)} variants shown for '{group}'")

    def _on_freeze_toggled(self):
        """Handle Freeze View checkbox toggle."""
        if not self.rate_freeze_var.get():
            self._refresh_truth_grid()

    def _on_truth_row_selected(self, event):
        """Show inspector panel when a truth grid row is clicked."""
        if not self.truth_grid or not self.rate_inspector_text:
            return

        selected = self.truth_grid.get_currently_selected()
        if not selected or not selected.rows:
            return

        row_idx = list(selected.rows)[0]
        records = self._visible_truth_records

        if row_idx >= len(records):
            return

        rec = records[row_idx]
        self._selected_truth_record = rec
        self._show_inspector(rec)

    def _record_to_json_payload(self, rec):
        return {
            "status": rec.status,
            "parent_service_type": rec.parent_service_type,
            "service_variant_prefix": rec.service_variant_prefix,
            "service_variant_label": rec.service_variant_label,
            "service_variant_id": rec.service_type_id,
            "rate": rec.truth_rate,
            "item_number": rec.truth_item_number,
            "rate_source": rec.truth_rate_source,
            "item_source": rec.truth_item_source,
            "updated_utc": rec.updated_utc,
            "rate_conflict": rec.rate_conflict,
            "item_conflict": rec.item_conflict,
            "service_type_link": rec.service_type_link,
            "rate_candidates": {
                src: {"value": cand.value, "updated_utc": cand.updated_utc}
                for src, cand in rec.rate_candidates.items()
            },
            "item_candidates": {
                src: {"value": cand.value, "updated_utc": cand.updated_utc}
                for src, cand in rec.item_candidates.items()
            },
        }

    def _copy_to_clipboard(self, value: str):
        self.clipboard_clear()
        self.clipboard_append(str(value or ""))
        self.update_idletasks()

    def _copy_truth_row_json(self, rec):
        payload = self._record_to_json_payload(rec)
        self._copy_to_clipboard(json.dumps(payload, indent=2, ensure_ascii=False))

    def _show_inspector(self, rec):
        """Populate the inspector panel with truth record details."""
        if not self.rate_inspector_text:
            return

        self.rate_inspector_text.config(state="normal")
        self.rate_inspector_text.delete("1.0", "end")

        title = rec.service_variant_label or "(Unlabeled Variant)"
        self.rate_inspector_text.insert("end", f"{title}\n", "heading")
        self.rate_inspector_text.insert(
            "end",
            f"Parent: {rec.parent_service_type or '(unknown)'} | Prefix: {rec.service_variant_prefix or '(n/a)'}\n",
            "subheading",
        )
        self.rate_inspector_text.insert(
            "end", f"Variant ID: {rec.service_type_id or '(missing)'}\n\n", "subheading"
        )

        self.rate_inspector_text.insert("end", "Status\n", "label")
        self.rate_inspector_text.insert("end", f"  {rec.status.upper()}\n")
        self.rate_inspector_text.insert("end", "Rate\n", "label")
        self.rate_inspector_text.insert(
            "end", f"  {rec.truth_rate or '(missing)'}  [{rec.truth_rate_source or 'n/a'}]\n"
        )
        self.rate_inspector_text.insert("end", "Item Number\n", "label")
        self.rate_inspector_text.insert(
            "end", f"  {rec.truth_item_number or '(missing)'}  [{rec.truth_item_source or 'n/a'}]\n"
        )
        self.rate_inspector_text.insert("end", "Updated (UTC)\n", "label")
        self.rate_inspector_text.insert("end", f"  {rec.updated_utc or '(unknown)'}\n")

        self.rate_inspector_text.insert("end", "\nRate Candidates:\n", "label")
        if rec.rate_candidates:
            for src, cand in rec.rate_candidates.items():
                self.rate_inspector_text.insert("end", f"  {src}: {cand.value} (updated: {cand.updated_utc})\n")
        else:
            self.rate_inspector_text.insert("end", "  (none)\n")

        self.rate_inspector_text.insert("end", "\nItem Number Candidates:\n", "label")
        if rec.item_candidates:
            for src, cand in rec.item_candidates.items():
                self.rate_inspector_text.insert("end", f"  {src}: {cand.value} (updated: {cand.updated_utc})\n")
        else:
            self.rate_inspector_text.insert("end", "  (none)\n")

        if rec.rate_conflict:
            self.rate_inspector_text.insert("end", "\nRate Conflict:\n", "conflict")
            vals = set(c.value for c in rec.rate_candidates.values() if c.value)
            self.rate_inspector_text.insert("end", f"  Disagreeing values: {', '.join(vals)}\n")

        if rec.item_conflict:
            self.rate_inspector_text.insert("end", "\nItem Number Conflict:\n", "conflict")
            vals = set(c.value for c in rec.item_candidates.values() if c.value)
            self.rate_inspector_text.insert("end", f"  Disagreeing values: {', '.join(vals)}\n")

        if rec.service_type_link:
            self.rate_inspector_text.insert("end", f"\nServiceTypeLink:\n{rec.service_type_link}\n", "link")

        self.rate_inspector_text.config(state="disabled")

        # Link action buttons
        if self._inspector_link_frame:
            self._inspector_link_frame.destroy()
            self._inspector_link_frame = None

        link_url = rec.service_type_link
        btn_frame = tk.Frame(self.rate_inspector_frame, bg="#0a1324")
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))

        tk.Button(
            btn_frame,
            text="Copy Rate",
            font=("Space Mono", 9),
            bg="#1f3e66",
            fg="#d8f1ff",
            activebackground="#2a5080",
            activeforeground="#ffffff",
            relief="flat",
            command=lambda v=rec.truth_rate: self._copy_to_clipboard(v),
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            btn_frame,
            text="Copy Item Number",
            font=("Space Mono", 9),
            bg="#1f3e66",
            fg="#d8f1ff",
            activebackground="#2a5080",
            activeforeground="#ffffff",
            relief="flat",
            command=lambda v=rec.truth_item_number: self._copy_to_clipboard(v),
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            btn_frame,
            text="Copy Row JSON",
            font=("Space Mono", 9),
            bg="#1f3e66",
            fg="#d8f1ff",
            activebackground="#2a5080",
            activeforeground="#ffffff",
            relief="flat",
            command=lambda r=rec: self._copy_truth_row_json(r),
        ).pack(side="left", padx=(0, 6))

        if link_url:
            tk.Button(
                btn_frame,
                text="Copy Link",
                font=("Space Mono", 9),
                bg="#1f3e66",
                fg="#d8f1ff",
                activebackground="#2a5080",
                activeforeground="#ffffff",
                relief="flat",
                command=lambda u=link_url: self._copy_to_clipboard(u),
            ).pack(side="left", padx=(0, 6))

            tk.Button(
                btn_frame,
                text="Open in Browser",
                font=("Space Mono", 9),
                bg="#1f3e66",
                fg="#d8f1ff",
                activebackground="#2a5080",
                activeforeground="#ffffff",
                relief="flat",
                command=lambda u=link_url: webbrowser.open(u),
            ).pack(side="left")

        self._inspector_link_frame = btn_frame

    def _truth_store_ingest(self, source, row):
        """Thread-safe ingestion into truth store. Called from on_row callbacks."""
        if source == "reference":
            self.truth_store.upsert_reference(row)
        elif source == "discovery":
            self.truth_store.upsert_discovery(row)

    # -----------------------------------------------------------------------
    # Export + Cleanup handlers
    # -----------------------------------------------------------------------

    def _open_in_file_manager(self, path):
        """Open path in system file manager."""
        target = Path(path).expanduser()
        if not target.exists():
            return
        try:
            if sys.platform.startswith("darwin"):
                subprocess.Popen(["open", str(target)])
            elif os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except Exception:
            pass

    def _handle_export_truth_csv(self):
        """Export current truth view to CSV."""
        if self.rate_running or self.discovery_running:
            return

        records = self._get_filtered_truth_records()

        if not records:
            messagebox.showinfo("ServiceType → Rate Extractor", "No rows to export.")
            return

        run_id = line_item_paths.make_run_id("EXPORT")
        path = line_item_paths.get_export_path(run_id, "csv")
        path.parent.mkdir(parents=True, exist_ok=True)

        fieldnames = [
            "Parent Service Type",
            "Service Variant Prefix",
            "Service Variant Label",
            "Service Type ID",
            "Status",
            "Rate (Truth)",
            "Rate Source",
            "Item Number (Truth)",
            "Item Source",
            "Rate Conflict",
            "Item Conflict",
            "Updated (UTC)",
        ]

        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for rec in records:
                writer.writerow({
                    "Parent Service Type": rec.parent_service_type,
                    "Service Variant Prefix": rec.service_variant_prefix,
                    "Service Variant Label": rec.service_variant_label,
                    "Service Type ID": rec.service_type_id,
                    "Status": rec.status.upper(),
                    "Rate (Truth)": rec.truth_rate,
                    "Rate Source": rec.truth_rate_source,
                    "Item Number (Truth)": rec.truth_item_number,
                    "Item Source": rec.truth_item_source,
                    "Rate Conflict": "Yes" if rec.rate_conflict else "No",
                    "Item Conflict": "Yes" if rec.item_conflict else "No",
                    "Updated (UTC)": rec.updated_utc,
                })

        self._enqueue_log(self._timestamp(f"[TruthView] CSV exported -> {path}"))
        messagebox.showinfo("ServiceType → Rate Extractor", f"Exported CSV to:\n{path}")
        self._open_in_file_manager(path.parent)

    def _handle_export_truth_xlsx(self):
        """Export current truth view to XLSX."""
        if self.rate_running or self.discovery_running:
            return

        records = self._get_filtered_truth_records()

        if not records:
            messagebox.showinfo("ServiceType → Rate Extractor", "No rows to export.")
            return

        run_id = line_item_paths.make_run_id("EXPORT")
        path = line_item_paths.get_export_path(run_id, "xlsx")
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            from openpyxl import Workbook
        except Exception as exc:
            messagebox.showerror("ServiceType → Rate Extractor", f"Excel export requires openpyxl:\n{exc}")
            return

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "TruthView"
        sheet.append([
            "Parent Service Type",
            "Service Variant Prefix",
            "Service Variant Label",
            "Service Type ID",
            "Status",
            "Rate (Truth)",
            "Rate Source",
            "Item Number (Truth)",
            "Item Source",
            "Rate Conflict",
            "Item Conflict",
            "Updated (UTC)",
        ])
        for rec in records:
            sheet.append([
                rec.parent_service_type,
                rec.service_variant_prefix,
                rec.service_variant_label,
                rec.service_type_id,
                rec.status.upper(),
                rec.truth_rate,
                rec.truth_rate_source,
                rec.truth_item_number,
                rec.truth_item_source,
                "Yes" if rec.rate_conflict else "No",
                "Yes" if rec.item_conflict else "No",
                rec.updated_utc,
            ])
        workbook.save(path)

        self._enqueue_log(self._timestamp(f"[TruthView] XLSX exported -> {path}"))
        messagebox.showinfo("ServiceType → Rate Extractor", f"Exported XLSX to:\n{path}")
        self._open_in_file_manager(path.parent)

    def _handle_clean_rate_clutter(self):
        """Clean everything - full ServiceTypeTruth wipe for fresh start."""
        if self.rate_running or self.discovery_running:
            messagebox.showwarning("ServiceType → Rate Extractor", "Cannot clean while tasks are running.")
            return

        # Confirm before wiping
        confirm = messagebox.askyesno(
            "Clean Rate Clutter - Fresh Start",
            "This will DELETE EVERYTHING in ServiceTypeTruth and clear all color coding.\n\n"
            "You will start completely fresh. Continue?"
        )
        if not confirm:
            return

        try:
            import shutil

            truth_root = line_item_paths.get_truth_root()
            run_id = line_item_paths.make_run_id("FRESH_START")
            log_dir = line_item_paths.cleanup_logs_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"cleanup_{run_id}.txt"

            deleted_count = 0
            bytes_reclaimed = 0

            with log_path.open("w", encoding="utf-8") as log:
                log.write(f"Fresh Start Cleanup: {run_id}\n")
                log.write(f"Action: Full wipe of {truth_root}\n\n")

                # Count files before deletion
                if truth_root.exists():
                    for item in truth_root.rglob("*"):
                        if item.is_file():
                            deleted_count += 1
                            bytes_reclaimed += item.stat().st_size

                # Delete the entire tree
                if truth_root.exists():
                    shutil.rmtree(truth_root)
                    log.write(f"Deleted: {truth_root}\n")

                # Recreate empty directory structure
                line_item_paths.ensure_structure()
                log.write(f"Recreated: empty directory structure\n")

                log.write(f"\nSummary:\n")
                log.write(f"  Deleted: {deleted_count} files\n")
                log.write(f"  Reclaimed: {bytes_reclaimed} bytes ({bytes_reclaimed / 1024 / 1024:.2f} MB)\n")
                log.write(f"  Status: Fresh start ready\n")

            # Clear UI state
            self.truth_store.clear()
            self._refresh_truth_grid()

            summary = (
                f"Fresh Start Complete:\n\n"
                f"Deleted: {deleted_count} files\n"
                f"Reclaimed: {bytes_reclaimed / 1024 / 1024:.2f} MB\n\n"
                f"Ready for new capture/discovery.\n"
                f"Log: {log_path}"
            )
            self._enqueue_log(self._timestamp(f"[Cleanup] {summary}"))
            messagebox.showinfo("Clean Rate Clutter - Fresh Start", summary)

        except Exception as exc:
            error = f"Clean Rate Clutter failed: {exc}"
            self._enqueue_log(self._timestamp(f"[Cleanup] {error}"))
            messagebox.showerror("Clean Rate Clutter Error", error)

    # -----------------------------------------------------------------------
    # Original rate extractor handlers (updated for truth store)
    # -----------------------------------------------------------------------

    def _handle_capture_live_rates(self):
        if self.rate_running:
            return
        if self.discovery_running:
            messagebox.showwarning(
                "ServiceType → Rate Extractor",
                "Wait for appointment discovery/merge to finish before running metadata capture.",
            )
            return

        self.truth_store.clear()
        self._set_rate_running(True)
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.rate_status_var.set(f"ServiceType rate capture in progress (started {started_at})...")
        self._append_rate_status(self._timestamp(f"Capture started at {started_at}"))

        def on_row(row):
            self.after(0, lambda r=dict(row): self._truth_store_ingest("reference", r))

        def on_progress(message):
            plain = str(message or "").strip()
            if plain.startswith("[") or plain.startswith("ServiceType→Rate"):
                line = plain
            else:
                line = f"[ServiceType→Rate] {plain}"
            stamped = self._timestamp(line)
            self.after(0, lambda m=message: self.rate_status_var.set(m))
            self.after(0, lambda t=stamped: self._append_rate_status(t))

        def task():
            try:
                result = capture_service_type_rates(
                    headless=True,
                    on_row=on_row,
                    on_progress=on_progress,
                )
                rows = result.get("rows", []) or []
                method = result.get("method", "unknown")
                csv_path = result.get("csv_path", "")
                xlsx_path = result.get("xlsx_path", "")
                selected_page_size = result.get("selected_page_size", "")
                summary = (
                    f"Capture complete ({method}, psize={selected_page_size}): {len(rows)} row(s).\n"
                    f"CSV: {csv_path}\nXLSX: {xlsx_path}"
                )
                self._enqueue_log(self._timestamp(f"[ServiceType→Rate] {summary}"))
                self.after(0, lambda: self.rate_status_var.set(summary))
                self.after(
                    0,
                    lambda s=summary: self._append_rate_status(self._timestamp(s)),
                )
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        "ServiceType → Rate Extractor", summary
                    ),
                )
            except Exception as exc:
                error = f"ServiceType rate capture failed: {exc}"
                self._enqueue_log(self._timestamp(f"[ServiceType→Rate] {error}"))
                self.after(
                    0,
                    lambda: self.rate_status_var.set(
                        "Capture failed. Inspect logs for details."
                    ),
                )
                self.after(
                    0,
                    lambda: messagebox.showerror(
                        "ServiceType → Rate Extractor", error
                    ),
                )
            finally:
                self.after(0, lambda: self._set_rate_running(False))

        threading.Thread(target=task, daemon=True).start()

    def _handle_rate_import_csv(self):
        if self.rate_running:
            messagebox.showwarning(
                "ServiceType → Rate Extractor",
                "Wait for capture to finish before importing.",
            )
            return
        if self.discovery_running:
            messagebox.showwarning(
                "ServiceType → Rate Extractor",
                "Wait for appointment discovery/merge to finish before importing metadata CSV.",
            )
            return
        selected = filedialog.askopenfilename(
            title="ImportCSV - select source CSV",
            parent=self,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not selected:
            return
        source = Path(selected).expanduser()
        if not source.exists():
            messagebox.showerror(
                "ServiceType → Rate Extractor",
                f"File not found:\n{source}",
            )
            return

        # Detect CSV format (reference vs discovery)
        imported_rows = []
        detected_format = None
        try:
            with source.open("r", newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                headers = reader.fieldnames or []

                # Check if it's a discovery CSV (has "Parent Service Type" or "Service Variant Label")
                if "Parent Service Type" in headers or "Service Variant Label" in headers:
                    detected_format = "discovery"
                elif "Service Type" in headers or "ID" in headers:
                    detected_format = "reference"

                for raw in reader:
                    if detected_format == "discovery":
                        # Discovery format - feed into truth store as discovery source
                        if raw.get("Service Variant ID") or raw.get("Service Type ID"):
                            imported_rows.append(dict(raw))
                    else:
                        # Reference format - normalize and feed as reference source
                        normalized = normalize_external_row(raw)
                        if normalized.get("Service Type"):
                            imported_rows.append(normalized)
        except Exception as exc:
            messagebox.showerror(
                "ServiceType → Rate Extractor",
                f"Failed to import CSV:\n{exc}",
            )
            return

        if not imported_rows:
            messagebox.showinfo(
                "ServiceType → Rate Extractor",
                "Imported CSV has no valid rows.",
            )
            return

        # Feed rows into truth store
        self.truth_store.clear()
        for row in imported_rows:
            if detected_format == "discovery":
                self._truth_store_ingest("discovery", row)
            else:
                self._truth_store_ingest("reference", row)

        summary = f"Imported {len(imported_rows)} rows from {source} (detected as {detected_format} format)"
        self._enqueue_log(self._timestamp(f"[ServiceType→Rate] {summary}"))
        self._append_rate_status(self._timestamp(summary))
        messagebox.showinfo("ServiceType → Rate Extractor", summary)

    def _notify_completion(self, success, output=None, error=None):
        def finalize():
            self._set_running(False)
            if success:
                self.status_var.set(f"Purging complete. Payload stored at {output}")
                messagebox.showinfo(
                    "TurnpointPurger",
                    f"Purging cycle complete.\n\nFiles stored at:\n{output}",
                )
            else:
                self.status_var.set("Purging aborted. Inspect logs for anomalies.")
                messagebox.showerror(
                    "TurnpointPurger",
                    f"Purging failed.\n\n{error}",
                )
            self._refresh_sequence_stats()

        self.after(0, finalize)

    def _notify_worker_completion(self, success, output=None, error=None):
        def finalize():
            self._set_worker_running(False)
            if success:
                self.worker_status_var.set(f"Worker purging complete. Payload stored at {output}")
                messagebox.showinfo(
                    "TurnpointPurger",
                    f"Worker purge complete.\n\nFiles stored at:\n{output}",
                )
            else:
                self.worker_status_var.set("Worker purging aborted. Inspect logs for anomalies.")
                messagebox.showerror(
                    "TurnpointPurger",
                    f"Worker purging failed.\n\n{error}",
                )
            self._refresh_worker_sequence_stats()

        self.after(0, finalize)

    def _set_running(self, running):
        self.is_running = running
        if running:
            self.primary_bar.start(12)
            self.secondary_bar.start(40)
            self.launch_button.configure(text="Purging…", state="disabled")
        else:
            self.primary_bar.stop()
            self.secondary_bar.stop()
        self.secondary_bar.start(65)
        self.launch_button.configure(text="Engage Purge", state="normal")

    def _load_discovered_workers(self, root: Path):
        if hasattr(self, "_cached_workers"):
            cached = getattr(self, "_cached_workers")
            if cached:
                return cached
        try:
            workers = discover_workers(root)
        except Exception:
            workers = []
        self._cached_workers = workers
        return workers

    def _next_clean_id(self, cleaned_root: Path) -> int:
        existing = []
        for f in cleaned_root.glob("*.csv"):
            try:
                prefix = f.name.split()[0]
                existing.append(int(prefix))
            except Exception:
                continue
        return max(existing) + 1 if existing else 100001

    def _build_clean_filename(self, prefix: int, full_name: str) -> str:
        safe_name = (full_name or "Worker").strip().replace(" ", "_") or "Worker"
        return f"{prefix} {safe_name}.csv"

    def _collect_client_rows(self, root: Path, template_path: Path | None):
        def _sanitize(value):
            if value is None:
                return ""
            return str(value).replace("\r", " ").replace("\n", " ").strip()

        template_headers: list[str] = []
        if template_path and template_path.exists():
            try:
                with template_path.open("r", newline="", encoding="utf-8-sig") as fh:
                    reader = csv.reader(fh)
                    template_headers = next(reader, [])
            except Exception:
                template_headers = []

        rows: list[dict[str, str]] = []
        headers: list[str] = list(template_headers)

        for path in root.glob("**/*Client-Details.csv"):
            try:
                with path.open("r", newline="", encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    first = next(reader, None)
                    if not first:
                        continue
                    for key in reader.fieldnames or []:
                        if key not in headers:
                            headers.append(key)
                    cleaned = {k: _sanitize(v) for k, v in first.items()}
                    rows.append(cleaned)
            except Exception as exc:
                self._enqueue_log(self._timestamp(f"Client combine skip {path}: {exc}"))
                continue

        # prefer template header order when present
        if template_headers:
            headers = template_headers
        return headers, rows

    def _handle_combine_clients(self):
        root = Path(self.clients_root_var.get()).expanduser()
        out_path = Path(self.clients_out_var.get()).expanduser()
        if not root.exists():
            messagebox.showerror("Client Combine", f"PurgedClients path not found:\n{root}")
            return
        template_path = out_path if out_path.exists() else None
        headers, rows = self._collect_client_rows(root, template_path)
        if not rows:
            messagebox.showinfo("Client Combine", "No client detail rows found.")
            return
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with out_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=headers)
                writer.writeheader()
                for row in rows:
                    writer.writerow({h: row.get(h, "") for h in headers})
            self._enqueue_log(self._timestamp(f"Combined {len(rows)} clients -> {out_path}"))
            messagebox.showinfo("Client Combine", f"Combined {len(rows)} clients to {out_path}")
        except Exception as exc:
            messagebox.showerror("Client Combine", f"Combine failed:\n{exc}")

    def _set_worker_running(self, running):
        self.is_running = running
        if running:
            self.worker_primary_bar.start(12)
            self.worker_secondary_bar.start(40)
            self.worker_launch_button.configure(text="Purging…", state="disabled")
        else:
            self.worker_primary_bar.stop()
            self.worker_secondary_bar.stop()
            self.worker_secondary_bar.start(65)
            self.worker_launch_button.configure(text="Engage Worker Purge", state="normal")

    def _timestamp(self, text):
        stamp = datetime.now().strftime("[%H:%M:%S]")
        return f"{stamp} {text}"

    def _on_close(self):
        set_log_sink(None)
        if self.scroll_canvas:
            self.scroll_canvas.unbind_all("<MouseWheel>")
        self.destroy()

    def _refresh_sequence_stats(self):
        try:
            stats = get_purge_statistics()
        except Exception:
            stats = None
        if stats:
            text = (
                f"Next Sequence: {stats['next_universal_id']}    "
                f"Purged: {stats['purged_count']}"
            )
        else:
            text = "Sequence tracker offline"
        self.sequence_var.set(text)

    def _refresh_worker_sequence_stats(self):
        try:
            stats = get_worker_statistics()
        except Exception:
            stats = None
        if stats:
            text = (
                f"Next Worker Sequence: {stats['next_universal_id']}    "
                f"Purged: {stats['purged_count']}"
            )
        else:
            text = "Worker sequence tracker offline"
        self.worker_sequence_var.set(text)

    def _refresh_credential_display(self):
        username = self.credential_username or "(not set)"
        masked = "*" * len(self.credential_password) if self.credential_password else "(none)"
        self.credential_display_var.set(f"Purging account: {username} / {masked}")


def launch_ui():
    ui = TurnpointPurgerUI()
    ui.mainloop()


if __name__ == "__main__":
    launch_ui()
