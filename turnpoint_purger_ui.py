import queue
import threading
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
        self.geometry("1320x780")
        self.configure(bg="#03060f")
        self.resizable(False, False)

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

        configure_credentials(self.credential_username, self.credential_password)

        self._setup_styles()
        self._build_layout()
        self._refresh_sequence_stats()
        self._refresh_credential_display()

        set_log_sink(self._enqueue_log)
        self.after(120, self._drain_log_queue)
        self.after(400, self._prompt_operator_name)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------- UI Construction ---------------------- #
    def _setup_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

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

    def _build_layout(self):
        container = tk.Frame(self, bg=self["bg"])
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

        discovery_label = tk.Label(
            controls_panel,
            text="Client Discovery",
            fg="#93b5ff",
            bg="#050b16",
            font=("Space Mono", 11, "bold"),
        )
        discovery_label.pack(anchor="w", padx=20, pady=(12, 4))

        self.find_button = ttk.Button(
            controls_panel,
            text="Find Purgeable Clients",
            style="Cyber.TButton",
            command=self._handle_find_purgeable_clients,
        )
        self.find_button.pack(anchor="w", padx=20, pady=(4, 6), fill="x")

        self.bundle_button = ttk.Button(
            controls_panel,
            text="Bundle Download (All Packages)",
            style="Cyber.TButton",
            command=lambda: self._handle_bundle_download(update=False),
        )
        self.bundle_button.pack(anchor="w", padx=20, pady=(0, 6), fill="x")

        self.update_bundle_button = ttk.Button(
            controls_panel,
            text="Update package bundle to latest",
            style="Cyber.TButton",
            command=lambda: self._handle_bundle_download(update=True),
        )
        self.update_bundle_button.pack(anchor="w", padx=20, pady=(0, 12), fill="x")

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

        def task():
            try:
                result = bundle_package_download(
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
                self.after(0, lambda: messagebox.showinfo("TurnpointPurger", summary))
            except Exception as exc:
                error = f"Bundle download failed: {exc}"
                self._enqueue_log(self._timestamp(error))
                self.after(0, lambda: messagebox.showerror("TurnpointPurger", error))

        self._run_button_task(button, task)

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
