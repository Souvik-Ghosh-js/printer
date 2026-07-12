"""
Mohini Print Worker — desktop app.

A window the shop keeps open. It polls the server for confirmed print jobs,
downloads each PDF, prints it on the default printer, and marks it printed.
Shows a live records table + log. Package to .exe with PyInstaller (see build_exe.bat).

Printing logic is identical to worker.py — this only adds the UI + threading.
"""
import os
import time
import threading
import queue
import tempfile
import mimetypes

import requests
import tkinter as tk
from tkinter import ttk, scrolledtext

import win32print
import win32api

# ---------------------------------------------------------------------------
# Config  (edit these two before building the .exe)
# ---------------------------------------------------------------------------
BASE_URL = "https://print.mohiniprintshop.org"
FILE_TOKEN = "CHANGE_ME_random_token"   # must match FILE_TOKEN in db.py on the server

POLL_SECONDS = 10
PRINTER_NAME = None  # None = system default printer


# ---------------------------------------------------------------------------
# Printing logic
# ---------------------------------------------------------------------------
# The printer the user picks in the UI. Shared with the worker thread.
# None = fall back to the system default printer.
_selected_printer = None
_printer_lock = threading.Lock()


def set_selected_printer(name):
    global _selected_printer
    with _printer_lock:
        _selected_printer = name


def list_printers():
    """All installed printers on this PC (local + network connections)."""
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return [p[2] for p in win32print.EnumPrinters(flags, None, 1)]


def get_printer_name():
    with _printer_lock:
        if _selected_printer:
            return _selected_printer
    return PRINTER_NAME or win32print.GetDefaultPrinter()


def configure_printer_settings(printer, job):
    hprinter = win32print.OpenPrinter(
        printer, {"DesiredAccess": win32print.PRINTER_ALL_ACCESS}
    )
    properties = win32print.GetPrinter(hprinter, 2)
    devmode = properties["pDevMode"]

    devmode.Color = 2 if job.get("color_mode") == "color" else 1
    devmode.Duplex = 2 if job.get("sides") == "double" else 1
    devmode.Orientation = 2 if job.get("orientation") == "landscape" else 1

    paper_map = {"A4": 9, "A3": 8, "Letter": 1}
    if job.get("paper_size") in paper_map:
        devmode.PaperSize = paper_map[job["paper_size"]]

    properties["pDevMode"] = devmode
    win32print.SetPrinter(hprinter, 2, properties, 0)
    win32print.ClosePrinter(hprinter)


def print_file(file_path, job):
    printer = get_printer_name()
    configure_printer_settings(printer, job)
    win32api.ShellExecute(0, "print", file_path, f'/d:"{printer}"', ".", 0)
    return True


# ---------------------------------------------------------------------------
# Server API
# ---------------------------------------------------------------------------
def fetch_confirmed_jobs():
    resp = requests.get(f"{BASE_URL}/worker/jobs", params={"token": FILE_TOKEN}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("jobs", [])


def mark_printed(job_id):
    resp = requests.post(
        f"{BASE_URL}/worker/jobs/{job_id}/printed",
        params={"token": FILE_TOKEN}, timeout=30,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Background worker thread — talks to the GUI via a thread-safe queue
# ---------------------------------------------------------------------------
class WorkerThread(threading.Thread):
    def __init__(self, ui_queue, stop_event):
        super().__init__(daemon=True)
        self.ui_queue = ui_queue
        self.stop_event = stop_event

    def log(self, msg):
        self.ui_queue.put(("log", msg))

    def record(self, job, status):
        self.ui_queue.put(("record", (job, status)))

    def run(self):
        self.log(f"Connected to {BASE_URL}")
        self.log("Watching for jobs...")
        while not self.stop_event.is_set():
            try:
                jobs = fetch_confirmed_jobs()
            except Exception as e:
                self.log(f"⚠️ Could not fetch jobs: {e}")
                self._sleep()
                continue

            if not jobs:
                self._sleep()
                continue

            for job in jobs:
                if self.stop_event.is_set():
                    break
                self._handle_job(job)

            self._sleep()
        self.log("Stopped.")

    def _handle_job(self, job):
        job_id = job["id"]
        filename = job.get("original_filename", "?")
        self.log(f"📥 Job {job_id}: {filename}")
        self.record(job, "printing")
        try:
            resp = requests.get(job["file_url"], stream=True, timeout=60)
            resp.raise_for_status()

            mime_type, _ = mimetypes.guess_type(filename)
            ext = ".pdf" if "pdf" in (mime_type or "") else os.path.splitext(filename)[1] or ".pdf"
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                tmp.write(resp.content)
                tmp_path = tmp.name

            print_file(tmp_path, job)
            mark_printed(job_id)
            self.log(f"✅ Job {job_id} printed.")
            self.record(job, "printed")
        except Exception as e:
            self.log(f"❌ Job {job_id} failed: {e}")
            self.record(job, "failed")

    def _sleep(self):
        # sleep in small chunks so Stop is responsive
        for _ in range(POLL_SECONDS * 2):
            if self.stop_event.is_set():
                return
            time.sleep(0.5)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Mohini Print Worker")
        self.geometry("820x560")

        self.ui_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None

        self._build_ui()
        self._refresh_printers()  # populate dropdown + pick default (after log box exists)
        self.after(200, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.start()  # auto-start on launch

    def _build_ui(self):
        # Top bar
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Mohini Print Worker", font=("Segoe UI", 14, "bold")).pack(side="left")
        self.status_lbl = ttk.Label(top, text="●  starting", foreground="orange")
        self.status_lbl.pack(side="left", padx=15)
        self.start_btn = ttk.Button(top, text="Stop", command=self.toggle)
        self.start_btn.pack(side="right")

        # Printer selector row
        prow = ttk.Frame(self, padding=(10, 0, 10, 8))
        prow.pack(fill="x")
        ttk.Label(prow, text="Printer:").pack(side="left")
        self.printer_var = tk.StringVar()
        self.printer_combo = ttk.Combobox(prow, textvariable=self.printer_var,
                                          state="readonly", width=45)
        self.printer_combo.pack(side="left", padx=8)
        self.printer_combo.bind("<<ComboboxSelected>>", self._on_printer_change)
        ttk.Button(prow, text="Refresh", command=self._refresh_printers).pack(side="left")

        # Records table
        cols = ("id", "file", "color", "copies", "status", "time")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=12)
        widths = {"id": 50, "file": 300, "color": 80, "copies": 60, "status": 90, "time": 130}
        for c in cols:
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        # Log pane
        ttk.Label(self, text="Activity log", padding=(10, 0)).pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(self, height=8, state="disabled",
                                                 font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=False, padx=10, pady=(0, 10))

    # --- printer selection ---
    def _refresh_printers(self):
        try:
            printers = list_printers()
        except Exception as e:
            printers = []
            self._append_log(f"⚠️ Could not list printers: {e}")

        self.printer_combo["values"] = printers

        # Keep the current pick if still available, else use system default.
        current = self.printer_var.get()
        if current and current in printers:
            chosen = current
        else:
            try:
                chosen = win32print.GetDefaultPrinter()
            except Exception:
                chosen = printers[0] if printers else ""
        self.printer_var.set(chosen)
        set_selected_printer(chosen or None)
        if chosen:
            self._append_log(f"🖨 Using printer: {chosen}")

    def _on_printer_change(self, _event=None):
        chosen = self.printer_var.get()
        set_selected_printer(chosen or None)
        self._append_log(f"🖨 Printer switched to: {chosen}")

    # --- start/stop ---
    def start(self):
        self.stop_event.clear()
        self.worker = WorkerThread(self.ui_queue, self.stop_event)
        self.worker.start()
        self.status_lbl.config(text="●  running", foreground="green")
        self.start_btn.config(text="Stop")

    def stop(self):
        self.stop_event.set()
        self.status_lbl.config(text="●  stopped", foreground="red")
        self.start_btn.config(text="Start")

    def toggle(self):
        if self.stop_event.is_set() or self.worker is None or not self.worker.is_alive():
            self.start()
        else:
            self.stop()

    # --- queue draining (runs on the UI thread) ---
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "record":
                    self._add_record(*payload)
        except queue.Empty:
            pass
        self.after(200, self._drain_queue)

    def _append_log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log_box.config(state="normal")
        self.log_box.insert("end", f"[{ts}] {msg}\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _add_record(self, job, status):
        ts = time.strftime("%H:%M:%S")
        self.tree.insert("", 0, values=(
            job.get("id"),
            job.get("original_filename", ""),
            job.get("color_mode", ""),
            job.get("copies", 1),
            status,
            ts,
        ))

    def _on_close(self):
        self.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
