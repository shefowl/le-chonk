#!/usr/bin/env python3
"""Le Chonk Control Desk: Graphical Launcher to start and stop campaign agents.

Run from desktop or command line:
    python3 tools/launcher.py
"""
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, scrolledtext

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOG_FILE = ROOT / "logs" / "campaign-u1.out"
CAMPAIGN_FILE = ROOT / "logs" / "campaign.json"


def get_hoi4_pid():
    try:
        out = subprocess.check_output(["pgrep", "-x", "hoi4"], text=True).strip()
        pids = [int(p) for p in out.splitlines() if p.isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def get_harness_pid():
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", r"^(/[^ ]*/)?python3 -u -m harness[.]run"], text=True
        ).strip()
        pids = [int(p) for p in out.splitlines() if p.isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def read_hoi4_info(pid):
    if not pid:
        return None
    try:
        from harness.bridge import module as B
        g = B("game").Game(pid)
        tag = g.player_tag()
        date_dict = g.date()
        return {"tag": tag, "date": date_dict.get("text", "?")}
    except Exception:
        return {"tag": "?", "date": "?"}


class LauncherApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Le Chonk · Headquarters Control Desk")
        self.root.geometry("860x680")
        self.root.minsize(780, 580)
        self.root.configure(bg="#22241f")

        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.style.configure(".", background="#22241f", foreground="#dedacf", fieldbackground="#161814")
        self.style.configure("Title.TLabel", font=("Sans", 16, "bold"), foreground="#e5c158")
        self.style.configure("Sub.TLabel", font=("Sans", 10), foreground="#9a9688")
        self.style.configure("Status.TLabel", font=("Sans", 11, "bold"))
        self.style.configure("Action.TButton", font=("Sans", 12, "bold"), padding=10)
        self.style.configure("Small.TButton", font=("Sans", 9), padding=5)

        self.selected_model = tk.StringVar(value="deepseek")
        self.opt_resume = tk.BooleanVar(value=True)
        self.opt_rewound = tk.BooleanVar(value=False)
        self.opt_speed = tk.StringVar(value="2")
        self.opt_days = tk.StringVar(value="3650")

        if LOG_FILE.exists():
            try:
                sz = LOG_FILE.stat().st_size
                self.log_pos = max(0, sz - 32768)
            except Exception:
                self.log_pos = 0
        else:
            self.log_pos = 0
        self.stopping = False

        self._build_ui()
        self._poll_status()
        self._tail_log()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill="both", expand=True)

        # Header
        header = ttk.Frame(main)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="LE CHONK  ·  HEADQUARTERS CONTROL", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="Autonomous LLM agents playing Hearts of Iron IV through in-memory bridge", style="Sub.TLabel").pack(anchor="w")

        # Status Cards Frame
        cards = ttk.Frame(main)
        cards.pack(fill="x", pady=(0, 12))

        # Game Card
        self.game_card = ttk.LabelFrame(cards, text=" Game (Hearts of Iron IV) ", padding=10)
        self.game_card.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.lbl_game_status = ttk.Label(self.game_card, text="⚪ Checking...", style="Status.TLabel")
        self.lbl_game_status.pack(anchor="w", pady=2)
        self.lbl_game_details = ttk.Label(self.game_card, text="", style="Sub.TLabel")
        self.lbl_game_details.pack(anchor="w", pady=2)
        self.btn_launch_game = ttk.Button(
            self.game_card,
            text="🚀 Launch via Steam",
            style="Small.TButton",
            command=self._launch_hoi4,
        )

        # Harness Card
        self.harness_card = ttk.LabelFrame(cards, text=" Headquarters (AI Harness) ", padding=10)
        self.harness_card.pack(side="right", fill="both", expand=True, padx=(6, 0))
        self.lbl_harness_status = ttk.Label(self.harness_card, text="⚪ Checking...", style="Status.TLabel")
        self.lbl_harness_status.pack(anchor="w", pady=2)
        self.lbl_harness_details = ttk.Label(self.harness_card, text="", style="Sub.TLabel")
        self.lbl_harness_details.pack(anchor="w", pady=2)

        # Controls & Settings Frame
        ctrl = ttk.LabelFrame(main, text=" Campaign Configuration & Launch ", padding=12)
        ctrl.pack(fill="x", pady=(0, 12))

        # Row 1: Big Action Button + Model selector
        row1 = ttk.Frame(ctrl)
        row1.pack(fill="x", pady=(0, 8))

        self.btn_action = tk.Button(
            row1,
            text="▶  START AGENTS",
            font=("Sans", 13, "bold"),
            bg="#2e7d32",
            fg="#ffffff",
            activebackground="#388e3c",
            activeforeground="#ffffff",
            padx=18,
            pady=10,
            relief="raised",
            cursor="hand2",
            command=self._on_action_click,
        )
        self.btn_action.pack(side="left", padx=(0, 16))

        # Model radio/combo
        m_frame = ttk.Frame(row1)
        m_frame.pack(side="left", fill="x", expand=True)
        ttk.Label(m_frame, text="Model Profile:").pack(anchor="w")
        models = [
            ("DeepSeek V3 / Flash (OpenRouter)", "deepseek"),
            ("Mistral Large 4 (Mistral Studio)", "lechonk"),
            ("Claude Haiku (Anthropic subscription)", "haiku-sub"),
            ("Custom BYOK (from API Setup)", "configured"),
        ]
        combo_models = ttk.Combobox(
            m_frame,
            values=[m[0] for m in models],
            state="readonly",
            width=36,
        )
        # map name to code
        self.model_map = {m[0]: m[1] for m in models}
        self.rev_map = {m[1]: m[0] for m in models}
        combo_models.set(self.rev_map.get(self.selected_model.get(), models[0][0]))

        def on_model_change(_=None):
            self.selected_model.set(self.model_map[combo_models.get()])

        combo_models.bind("<<ComboboxSelected>>", on_model_change)
        combo_models.pack(anchor="w", pady=2)

        # API Setup button
        btn_api = ttk.Button(row1, text="🔑 API Setup...", style="Small.TButton", command=self._open_api_setup)
        btn_api.pack(side="right", padx=(8, 0))

        # Row 2: Options
        row2 = ttk.Frame(ctrl)
        row2.pack(fill="x", pady=(4, 0))

        ttk.Checkbutton(row2, text="Resume campaign (--resume)", variable=self.opt_resume).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(row2, text="Loaded older save (--rewound)", variable=self.opt_rewound).pack(side="left", padx=(0, 14))

        ttk.Label(row2, text="Speed:").pack(side="left", padx=(10, 4))
        combo_speed = ttk.Combobox(row2, values=["1", "2", "3", "4"], textvariable=self.opt_speed, width=3, state="readonly")
        combo_speed.pack(side="left", padx=(0, 14))

        ttk.Label(row2, text="Watch: GER ITA FRA").pack(side="left", padx=(10, 0))

        # Logs Frame
        log_frame = ttk.LabelFrame(main, text=" Live Operations Feed (logs/campaign-u1.out) ", padding=8)
        log_frame.pack(fill="both", expand=True)

        log_top = ttk.Frame(log_frame)
        log_top.pack(fill="x", pady=(0, 4))
        ttk.Label(log_top, text="Recent harness output (auto-scrolls):", style="Sub.TLabel").pack(side="left")
        ttk.Button(log_top, text="Clear View", style="Small.TButton", command=self._clear_log_view).pack(side="right")

        self.txt_log = scrolledtext.ScrolledText(
            log_frame,
            wrap="word",
            bg="#141512",
            fg="#c8c4b7",
            insertbackground="#e5c158",
            font=("Monospace", 9),
            padx=8,
            pady=8,
        )
        self.txt_log.pack(fill="both", expand=True)

    def _clear_log_view(self):
        self.txt_log.delete("1.0", tk.END)

    def _launch_hoi4(self):
        try:
            subprocess.Popen(["steam", "steam://rungameid/394360"])
        except Exception as e:
            self.lbl_game_details.configure(text=f"Failed to launch via Steam: {e}")

    def _open_api_setup(self):
        script = ROOT / "tools" / "configure_model.py"
        subprocess.Popen([sys.executable, str(script)], cwd=ROOT)

    def _on_action_click(self):
        h_pid = get_harness_pid()
        if h_pid:
            # STOP
            self._stop_harness(h_pid)
        else:
            # START
            self._start_harness()

    def _start_harness(self):
        hoi4_pid = get_hoi4_pid()
        if not hoi4_pid:
            self.lbl_harness_status.configure(text="❌ Cannot start: Hearts of Iron IV is not running", foreground="#e57373")
            return

        cmd = [
            str(ROOT / "tools" / "run_campaign.sh"),
            "--models", self.selected_model.get(),
            "--days", self.opt_days.get(),
            "--speed", self.opt_speed.get(),
            "--history", "on",
            "--pause", "fires",
            "--watch", "GER", "ITA", "FRA",
        ]
        if self.opt_resume.get():
            cmd.append("--resume")
        if self.opt_rewound.get():
            cmd.append("--rewound")

        self.lbl_harness_status.configure(text="⏳ Launching...", foreground="#ffb74d")
        self.btn_action.configure(state="disabled")

        def launch_thread():
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a") as f_out:
                f_out.write(f"\n--- Launcher started campaign at {datetime.now().isoformat()} ---\n")
                f_out.flush()
                # Run detached in own process group so closing launcher won't terminate harness
                subprocess.Popen(cmd, cwd=ROOT, stdout=f_out, stderr=subprocess.STDOUT, start_new_session=True)
            time.sleep(1.0)
            self.root.after(0, lambda: self.btn_action.configure(state="normal"))

        threading.Thread(target=launch_thread, daemon=True).start()

    def _stop_harness(self, pid):
        self.stopping = True
        self.lbl_harness_status.configure(text="⏳ Stopping (saving turn & campaign)...", foreground="#ffb74d")
        self.btn_action.configure(state="disabled")

        def stop_thread():
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # Wait up to 180 seconds for turn to complete and campaign to save
            for i in range(360):
                if not get_harness_pid():
                    break
                if i % 4 == 0:
                    elapsed = i // 2
                    self.root.after(0, lambda e=elapsed: self.lbl_harness_status.configure(
                        text=f"⏳ Stopping... ({e}s / saving campaign)",
                        foreground="#ffb74d"
                    ))
                time.sleep(0.5)
            self.stopping = False
            self.root.after(0, lambda: self.btn_action.configure(state="normal"))

        threading.Thread(target=stop_thread, daemon=True).start()

    def _poll_status(self):
        hoi4_pid = get_hoi4_pid()
        h_pid = get_harness_pid()

        # Update Game card
        if hoi4_pid:
            self.lbl_game_status.configure(text=f"🟢 Running (PID: {hoi4_pid})", foreground="#81c784")
            self.lbl_game_details.configure(text="Hearts of Iron IV process active")
            if hasattr(self, "btn_launch_game"):
                self.btn_launch_game.pack_forget()
        else:
            self.lbl_game_status.configure(text="⚪ Not running", foreground="#b0bec5")
            self.lbl_game_details.configure(text="Start HoI4 and load a singleplayer game")
            if hasattr(self, "btn_launch_game"):
                self.btn_launch_game.pack(anchor="w", pady=(4, 0))

        # Update Harness card
        if h_pid:
            if not self.stopping:
                self.lbl_harness_status.configure(text=f"🟢 Active (PID: {h_pid})", foreground="#81c784")
                self.lbl_harness_details.configure(text="Executing campaign turn loop")
                self.btn_action.configure(
                    text="⏹  STOP AGENTS",
                    bg="#c62828",
                    activebackground="#d32f2f",
                    state="normal",
                )
        else:
            if not self.stopping:
                self.lbl_harness_status.configure(text="⚪ Stopped", foreground="#b0bec5")
                self.lbl_harness_details.configure(text="Click 'Start Agents' when game is loaded")
                self.btn_action.configure(
                    text="▶  START AGENTS",
                    bg="#2e7d32",
                    activebackground="#388e3c",
                    state="normal" if hoi4_pid else "disabled",
                )

        self.root.after(1000, self._poll_status)

    def _tail_log(self):
        if LOG_FILE.exists():
            try:
                with open(LOG_FILE, "r", errors="replace") as f:
                    f.seek(self.log_pos)
                    chunk = f.read()
                    if chunk:
                        self.log_pos = f.tell()
                        self.txt_log.insert(tk.END, chunk)
                        lines = int(self.txt_log.index("end-1c").split(".")[0])
                        if lines > 3000:
                            self.txt_log.delete("1.0", f"{lines - 2500}.0")
                        self.txt_log.see(tk.END)
            except Exception:
                pass
        self.root.after(500, self._tail_log)


def main():
    root = tk.Tk()
    app = LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
