import csv
import os
import queue
import threading
import time
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
    DATASET_COLUMNS,
    capture_service_type_rates,
    default_rate_numeric,
    normalize_external_row,
)


ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ART_FILENAME = "turnpoint_purger_art.png"
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
        self.title("TurnpointPurger // Purging Control Surface")
        self.geometry("1380x820")
        self.configure(bg="#03060f")
        self.minsize(1200, 800)

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
        self.nexis_root_var = tk.StringVar(value=str(Path.home() / "PurgedWorker"))
        self.nexis_table = None
        self.nexis_preview = None
        self.nexis_count_var = tk.StringVar(value="No workers scanned yet.")
        self.nexis_user_var = tk.StringVar(value=os.getenv("NEXIS_USERNAME", ""))
        self.nexis_pass_var = tk.StringVar(value=os.getenv("NEXIS_PASSWORD", ""))
        self.cleaned_root_var = tk.StringVar(value=str(Path.home() / "CLEANEDFORNEXIS"))
        self.clients_root_var = tk.StringVar(value=str(Path.home() / "PurgedClients"))
        self.clients_out_var = tk.StringVar(
            value=str(Path(__file__).resolve().parent / "FormatforClient(Nexis)" / "clients-data.csv")
        )

        # ServiceType -> Rate Extractor state
        self.rate_status_var = tk.StringVar(
            value="Idle // ServiceType rate extractor ready."
        )
        self.rate_table = None
        self.rate_running = False
        self.rate_capture_button = None
        self.rate_export_button = None
        self.rate_apply_button = None
        self.rate_log_view = None
        self.rate_all_rows = []
        self.rate_visible_rows = []
        self.rate_sort_var = tk.StringVar(value="Service Type (A→Z)")
        self.rate_deleted_no_var = tk.BooleanVar(value=True)
        self.rate_positive_var = tk.BooleanVar(value=False)
        self.rate_service_code_var = tk.BooleanVar(value=False)
        self.rate_sil_var = tk.BooleanVar(value=False)
        self.rate_search_var = tk.StringVar(value="")

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
            padding=8,
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
            padding=6,
        )
        style.map(
            "Cyber.TCheckbutton",
            background=[("active", "#0b1831")],
            foreground=[("disabled", "#5c6c87")],
        )
        style.configure(
            "Danger.TButton",
            font=("SF Pro Display", 13, "bold"),
            padding=6,
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
        canvas.create_window((0, 0), window=inner, anchor="nw")

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

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
                self.attributes("-fullscreen", False)

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
        container.pack(fill="both", expand=True, padx=24, pady=20)

        notebook = ttk.Notebook(container)
        client_tab = tk.Frame(notebook, bg="#050b16")
        worker_tab = tk.Frame(notebook, bg="#050b16")
        nexis_tab = tk.Frame(notebook, bg="#050b16")
        rate_tab = tk.Frame(notebook, bg="#050b16")
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
        parent.columnconfigure(0, weight=3)
        parent.columnconfigure(1, weight=2)

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
        parent.columnconfigure(0, weight=3)
        parent.columnconfigure(1, weight=2)

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
        parent.columnconfigure(0, weight=3)
        parent.columnconfigure(1, weight=2)

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
        parent.rowconfigure(2, weight=1)

        header = tk.Frame(parent, bg="#050b16")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 8))

        tk.Label(
            header,
            text="ServiceType → Rate Extractor",
            fg="#f5fbff",
            bg="#050b16",
            font=("Orbitron", 24, "bold"),
        ).pack(anchor="w")

        tk.Label(
            header,
            text=(
                "Global Service Types capture (read-only, non-purge, non-destructive). "
                "Source: service-types.asp with export-first + HTML fallback."
            ),
            fg="#7cc3ff",
            bg="#050b16",
            font=("Space Mono", 11),
            wraplength=1020,
            justify="left",
        ).pack(anchor="w", pady=(4, 10))

        actions = tk.Frame(header, bg="#050b16")
        actions.pack(anchor="w", fill="x")

        self.rate_capture_button = ttk.Button(
            actions,
            text="Capture Live Rates",
            style="Cyber.TButton",
            command=self._handle_capture_live_rates,
        )
        self.rate_capture_button.pack(side="left", padx=(0, 10))

        self.rate_export_button = ttk.Button(
            actions,
            text="ImportCSV",
            style="Cyber.TButton",
            command=self._handle_rate_import_csv,
        )
        self.rate_export_button.pack(side="left")

        tk.Label(
            header,
            textvariable=self.rate_status_var,
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 10),
            wraplength=1020,
            justify="left",
        ).pack(anchor="w", pady=(10, 0))

        tk.Label(
            header,
            text="Status",
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 10, "bold"),
        ).pack(anchor="w", pady=(8, 4))

        status_view = scrolledtext.ScrolledText(
            header,
            height=6,
            wrap="word",
            font=("JetBrains Mono", 10),
            bg="#030611",
            fg="#c2f1ff",
            insertbackground="#1de5ff",
            relief="flat",
        )
        status_view.pack(fill="x", expand=False)
        status_view.configure(state="disabled")
        self.rate_log_view = status_view

        filters = tk.Frame(parent, bg="#050b16")
        filters.grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 10))

        tk.Label(
            filters,
            text="Sort / Filter",
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 11, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 10))

        sort_options = [
            "Service Type (A→Z)",
            "Service Type (Z→A)",
            "ID (Low→High)",
            "ID (High→Low)",
            "Def. Rate (Low→High)",
            "Def. Rate (High→Low)",
            "Service Code (A→Z)",
            "Package (A→Z)",
            "Billing Type (A→Z)",
        ]
        sort_combo = ttk.Combobox(
            filters,
            textvariable=self.rate_sort_var,
            values=sort_options,
            state="readonly",
            width=28,
        )
        sort_combo.grid(row=0, column=1, sticky="w", padx=(0, 12))

        tk.Checkbutton(
            filters,
            text="Deleted = No",
            variable=self.rate_deleted_no_var,
            bg="#050b16",
            fg="#d8e5ff",
            selectcolor="#0b1322",
            activebackground="#050b16",
            activeforeground="#d8e5ff",
        ).grid(row=0, column=2, sticky="w", padx=(0, 10))

        tk.Checkbutton(
            filters,
            text="Def. Rate > 0",
            variable=self.rate_positive_var,
            bg="#050b16",
            fg="#d8e5ff",
            selectcolor="#0b1322",
            activebackground="#050b16",
            activeforeground="#d8e5ff",
        ).grid(row=0, column=3, sticky="w", padx=(0, 10))

        tk.Checkbutton(
            filters,
            text="Service Code not empty",
            variable=self.rate_service_code_var,
            bg="#050b16",
            fg="#d8e5ff",
            selectcolor="#0b1322",
            activebackground="#050b16",
            activeforeground="#d8e5ff",
        ).grid(row=0, column=4, sticky="w", padx=(0, 10))

        tk.Checkbutton(
            filters,
            text="Service Type contains SIL",
            variable=self.rate_sil_var,
            bg="#050b16",
            fg="#d8e5ff",
            selectcolor="#0b1322",
            activebackground="#050b16",
            activeforeground="#d8e5ff",
        ).grid(row=0, column=5, sticky="w", padx=(0, 12))

        tk.Label(
            filters,
            text="Search",
            fg="#9fe3ff",
            bg="#050b16",
            font=("Space Mono", 10),
        ).grid(row=0, column=6, sticky="w", padx=(0, 6))

        search_entry = tk.Entry(
            filters,
            textvariable=self.rate_search_var,
            width=28,
            font=("JetBrains Mono", 11),
            bg="#0a1324",
            fg="#e9f2ff",
            insertbackground="#18e0ff",
            relief="flat",
        )
        search_entry.grid(row=0, column=7, sticky="w", padx=(0, 10))
        search_entry.bind("<Return>", lambda _event: self._apply_rate_filters())

        self.rate_apply_button = ttk.Button(
            filters,
            text="Apply",
            style="Cyber.TButton",
            command=self._apply_rate_filters,
        )
        self.rate_apply_button.grid(row=0, column=8, sticky="w")
        sort_combo.bind("<<ComboboxSelected>>", lambda _event: self._apply_rate_filters())

        table_frame = tk.Frame(parent, bg="#050b16")
        table_frame.grid(row=2, column=0, sticky="nsew", padx=24, pady=(0, 16))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        columns = (
            "service_type",
            "service_type_id",
            "default_rate",
            "service_code",
            "service_type_link",
        )
        table = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            height=20,
            style="Atlas.Treeview",
        )
        table.heading("service_type", text="Service Type", anchor="w")
        table.heading("service_type_id", text="ID", anchor="center")
        table.heading("default_rate", text="Def. Rate", anchor="center")
        table.heading("service_code", text="Service Code", anchor="w")
        table.heading("service_type_link", text="ServiceTypeLink", anchor="w")
        table.column("service_type", width=330, anchor="w")
        table.column("service_type_id", width=130, anchor="center")
        table.column("default_rate", width=130, anchor="center")
        table.column("service_code", width=180, anchor="w")
        table.column("service_type_link", width=420, anchor="w")

        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=scroll.set)
        table.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.rate_table = table

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
        if self.rate_capture_button:
            self.rate_capture_button.configure(
                state="disabled" if running else "normal"
            )
        if self.rate_export_button:
            self.rate_export_button.configure(
                state="disabled" if running else "normal"
            )
        if self.rate_apply_button:
            self.rate_apply_button.configure(
                state="disabled" if running else "normal"
            )

    def _append_rate_status(self, text):
        if not self.rate_log_view:
            return
        self.rate_log_view.configure(state="normal")
        self.rate_log_view.insert("end", text + "\n")
        self.rate_log_view.see("end")
        self.rate_log_view.configure(state="disabled")

    def _clear_rate_table(self):
        if not self.rate_table:
            return
        self.rate_table.delete(*self.rate_table.get_children())

    def _insert_rate_preview_row(self, row):
        if not self.rate_table:
            return
        values = (
            row.get("Service Type", ""),
            row.get("ID", ""),
            row.get("Def. Rate", ""),
            row.get("Service Code", ""),
            row.get("ServiceTypeLink", ""),
        )
        self.rate_table.insert("", "end", values=values)

    def _rate_row_matches_filters(self, row):
        deleted = (row.get("Deleted") or "").strip().lower()
        if self.rate_deleted_no_var.get() and deleted in {"yes", "y", "true", "1", "deleted"}:
            return False

        if self.rate_positive_var.get() and default_rate_numeric(row.get("Def. Rate", "")) <= 0:
            return False

        if self.rate_service_code_var.get() and not (row.get("Service Code") or "").strip():
            return False

        if self.rate_sil_var.get() and "sil" not in (row.get("Service Type") or "").lower():
            return False

        search = self.rate_search_var.get().strip().lower()
        if search:
            text = (
                f"{row.get('Service Type', '')} "
                f"{row.get('Service Code', '')} "
                f"{row.get('Package', '')}"
            ).lower()
            if search not in text:
                return False
        return True

    def _sorted_rate_rows(self, rows):
        def _id_value(row):
            raw = (row.get("ID") or "").strip()
            try:
                return int(raw)
            except Exception:
                return 0

        option = self.rate_sort_var.get().strip()
        if option == "Service Type (Z→A)":
            return sorted(rows, key=lambda r: (r.get("Service Type", "").lower()), reverse=True)
        if option == "ID (Low→High)":
            return sorted(rows, key=_id_value)
        if option == "ID (High→Low)":
            return sorted(rows, key=_id_value, reverse=True)
        if option == "Def. Rate (Low→High)":
            return sorted(rows, key=lambda r: default_rate_numeric(r.get("Def. Rate", "")))
        if option == "Def. Rate (High→Low)":
            return sorted(rows, key=lambda r: default_rate_numeric(r.get("Def. Rate", "")), reverse=True)
        if option == "Service Code (A→Z)":
            return sorted(rows, key=lambda r: (r.get("Service Code", "").lower()))
        if option == "Package (A→Z)":
            return sorted(rows, key=lambda r: (r.get("Package", "").lower()))
        if option == "Billing Type (A→Z)":
            return sorted(rows, key=lambda r: (r.get("Billing Type", "").lower()))
        return sorted(rows, key=lambda r: (r.get("Service Type", "").lower()))

    def _apply_rate_filters(self):
        rows = [row for row in self.rate_all_rows if self._rate_row_matches_filters(row)]
        rows = self._sorted_rate_rows(rows)
        self.rate_visible_rows = rows
        self._clear_rate_table()
        for row in rows:
            self._insert_rate_preview_row(row)
        self.rate_status_var.set(
            f"ServiceType dataset: {len(rows)} shown / {len(self.rate_all_rows)} total."
        )

    def _load_rate_dataset(self, rows):
        self.rate_all_rows = []
        for row in rows:
            normalized = {column: str(row.get(column, "")) for column in DATASET_COLUMNS}
            self.rate_all_rows.append(normalized)
        self._apply_rate_filters()

    def _handle_capture_live_rates(self):
        if self.rate_running:
            return
        self._clear_rate_table()
        self.rate_all_rows = []
        self.rate_visible_rows = []
        self._set_rate_running(True)
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.rate_status_var.set(f"ServiceType rate capture in progress (started {started_at})...")
        self._append_rate_status(self._timestamp(f"Capture started at {started_at}"))

        def on_row(row):
            def add_row():
                normalized = {column: str(row.get(column, "")) for column in DATASET_COLUMNS}
                self.rate_all_rows.append(normalized)
                if self._rate_row_matches_filters(normalized):
                    self._insert_rate_preview_row(normalized)
            self.after(0, add_row)

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
                self.after(0, self._apply_rate_filters)
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

        imported_rows = []
        try:
            with source.open("r", newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                for raw in reader:
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
                "Imported CSV has no valid ServiceType rows.",
            )
            return
        self._load_rate_dataset(imported_rows)
        self._enqueue_log(
            self._timestamp(
                f"[ServiceType→Rate] ImportCSV loaded {len(imported_rows)} row(s) from {source}"
            )
        )
        self._append_rate_status(
            self._timestamp(
                f"ImportCSV loaded {len(imported_rows)} row(s) from {source}"
            )
        )

        dialog = tk.Toplevel(self)
        dialog.title("Import CSV")
        dialog.configure(bg="#03060f")
        dialog.resizable(False, False)
        dialog.grab_set()

        tk.Label(
            dialog,
            text="Choose export format",
            fg="#a8d8ff",
            bg="#03060f",
            font=("Space Mono", 12, "bold"),
        ).pack(padx=22, pady=(16, 10))

        ttk.Button(
            dialog,
            text="Want in CSV?",
            style="Cyber.TButton",
            command=lambda: (dialog.destroy(), self._export_service_types_csv()),
        ).pack(fill="x", padx=20, pady=(0, 8))

        ttk.Button(
            dialog,
            text="Want in Excel?",
            style="Cyber.TButton",
            command=lambda: (dialog.destroy(), self._export_service_types_xlsx()),
        ).pack(fill="x", padx=20, pady=(0, 12))

        ttk.Button(
            dialog,
            text="Cancel",
            style="Danger.TButton",
            command=dialog.destroy,
        ).pack(fill="x", padx=20, pady=(0, 16))

    def _export_service_types_csv(self):
        rows = list(self.rate_visible_rows or self.rate_all_rows)
        if not rows:
            messagebox.showinfo(
                "ServiceType → Rate Extractor",
                "No rows to export.",
            )
            return
        target = filedialog.asksaveasfilename(
            title="Save CSV",
            parent=self,
            defaultextension=".csv",
            initialfile="ServiceTypes_imported.csv",
            filetypes=[("CSV files", "*.csv")],
        )
        if not target:
            return
        output_path = Path(target)
        with output_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=DATASET_COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column, "") for column in DATASET_COLUMNS})
        self._enqueue_log(
            self._timestamp(f"[ServiceType→Rate] CSV exported -> {output_path}")
        )
        messagebox.showinfo(
            "ServiceType → Rate Extractor",
            f"Exported CSV to:\n{output_path}",
        )

    def _export_service_types_xlsx(self):
        rows = list(self.rate_visible_rows or self.rate_all_rows)
        if not rows:
            messagebox.showinfo(
                "ServiceType → Rate Extractor",
                "No rows to export.",
            )
            return
        target = filedialog.asksaveasfilename(
            title="Save Excel",
            parent=self,
            defaultextension=".xlsx",
            initialfile="ServiceTypes_imported.xlsx",
            filetypes=[("Excel files", "*.xlsx")],
        )
        if not target:
            return
        output_path = Path(target)
        try:
            from openpyxl import Workbook
        except Exception as exc:
            messagebox.showerror(
                "ServiceType → Rate Extractor",
                f"Excel export requires openpyxl:\n{exc}",
            )
            return

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "ServiceTypes"
        sheet.append(DATASET_COLUMNS)
        for row in rows:
            sheet.append([row.get(column, "") for column in DATASET_COLUMNS])
        workbook.save(output_path)

        self._enqueue_log(
            self._timestamp(f"[ServiceType→Rate] Excel exported -> {output_path}")
        )
        messagebox.showinfo(
            "ServiceType → Rate Extractor",
            f"Exported Excel to:\n{output_path}",
        )

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
