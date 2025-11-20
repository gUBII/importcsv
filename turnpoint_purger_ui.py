import csv
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, simpledialog

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
    run_client_batch,
    run_turnpoint_purge,
    set_log_sink,
    set_operator_name,
    reset_purge_data,
    configure_credentials,
    ensure_credentials,
    RUNTIME_USERNAME,
    RUNTIME_PASSWORD,
)
from purger_state import get_purge_statistics


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

        configure_credentials(self.credential_username, self.credential_password)

        self._setup_styles()
        self._build_scrollable_root()
        self._build_layout(self.scroll_frame)
        self._refresh_sequence_stats()
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
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
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
        container.columnconfigure(0, weight=3)
        container.columnconfigure(1, weight=2)
        container.rowconfigure(2, weight=1)

        visual_panel = tk.Frame(container, bg="#050b16", bd=0, relief="flat")
        visual_panel.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 24))

        controls_panel = tk.Frame(container, bg="#050b16", bd=0, relief="flat")
        controls_panel.grid(row=0, column=1, sticky="nsew")

        log_panel = tk.Frame(container, bg="#050b16", bd=0, relief="flat")
        log_panel.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(24, 0))

        version_badge = tk.Label(
            self,
            text=f"TurnpointPurger v{APP_VERSION}",
            fg="#5de4ff",
            bg=self["bg"],
            font=("Space Mono", 12, "bold"),
        )
        version_badge.place(relx=1.0, x=-32, y=16, anchor="ne")

        # Visual panel content
        headline = tk.Label(
            visual_panel,
            text="TurnpointPurger",
            fg="#f5fbff",
            bg="#050b16",
            font=("Orbitron", 28, "bold"),
        )
        headline.pack(anchor="w", padx=30, pady=(28, 0))

        subline = tk.Label(
            visual_panel,
            text="Zero-trace purging system // Codename: (Far)H4n_SOLO",
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

        # Controls panel content
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

        # // cooldown entry promotes safer pacing between purges (min 20s)
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
        watermark.pack(anchor="e", padx=20, pady=(140, 4))

        email_label = tk.Label(
            controls_panel,
            text=f"Contact: {CONTACT_EMAIL}",
            fg="#6bdcff",
            bg="#050b16",
            font=("Space Mono", 11),
        )
        email_label.pack(anchor="e", padx=20, pady=(0, 12))

        # Log panel
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

        global_watermark = tk.Label(
            self,
            text="(Far)H4n_SOLO • TurnpointPurger // Purging System",
            fg="#0e1c33",
            bg=self["bg"],
            font=("Space Mono", 12, "bold"),
        )
        global_watermark.place(relx=1.0, rely=1.0, anchor="se", x=-18, y=-10)

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

    def _update_manifest_timestamp(self):
        manifest = Path(self.manifest_path)
        if manifest.exists():
            ts = datetime.fromtimestamp(manifest.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            self.manifest_timestamp_var.set(f"Manifest updated: {ts}")
        else:
            self.manifest_timestamp_var.set("Manifest not generated")

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

    def _refresh_credential_display(self):
        username = self.credential_username or "(not set)"
        masked = "*" * len(self.credential_password) if self.credential_password else "(none)"
        self.credential_display_var.set(f"Purging account: {username} / {masked}")


def launch_ui():
    ui = TurnpointPurgerUI()
    ui.mainloop()


if __name__ == "__main__":
    launch_ui()
