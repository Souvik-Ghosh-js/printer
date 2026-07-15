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
import subprocess

import requests
import tkinter as tk
from tkinter import ttk, scrolledtext

import win32print
import win32api
import win32con

# ---------------------------------------------------------------------------
# Config  (edit these two before building the .exe)
# ---------------------------------------------------------------------------
BASE_URL = "https://print.mohiniprintshop.org"
FILE_TOKEN = "CHANGE_ME_random_token"   # must match FILE_TOKEN in db.py on the server

POLL_SECONDS = 10
PRINTER_NAME = None  # None = system default printer

# How long (seconds) to wait for a spooled job to actually finish printing
PRINT_CONFIRM_TIMEOUT = 120


# ---------------------------------------------------------------------------
# Printing logic
# ---------------------------------------------------------------------------
# The printer + tray the user picks in the UI. Shared with the worker thread.
# None = fall back to the system default printer / printer's default tray.
_selected_printer = None
_selected_tray = None          # a DMBIN_* integer, or None for "auto"
_printer_lock = threading.Lock()

AUTO_TRAY_LABEL = "Auto (printer default)"


def set_selected_printer(name):
    global _selected_printer
    with _printer_lock:
        _selected_printer = name


def set_selected_tray(bin_id):
    global _selected_tray
    with _printer_lock:
        _selected_tray = bin_id


def list_printers():
    """All installed printers on this PC (local + network connections)."""
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return [p[2] for p in win32print.EnumPrinters(flags, None, 1)]


def list_trays(printer):
    """Return the printer's paper trays as [(label, bin_id), ...].

    Queries the driver for its bin names + IDs. Returns [] if the driver
    reports none (the UI hides the tray dropdown in that case).
    """
    try:
        names = win32print.DeviceCapabilities(
            printer, "", win32con.DC_BINNAMES) or []
        ids = win32print.DeviceCapabilities(
            printer, "", win32con.DC_BINS) or []
    except Exception:
        return []
    # Pair names with ids; some drivers return mismatched lengths.
    trays = []
    for i, name in enumerate(names):
        bin_id = ids[i] if i < len(ids) else None
        if bin_id is not None:
            trays.append((str(name).strip(), int(bin_id)))
    return trays


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

    # Paper tray (DefaultSource) — only if the user picked a specific one.
    with _printer_lock:
        tray = _selected_tray
    if tray is not None:
        devmode.DefaultSource = tray

    properties["pDevMode"] = devmode
    win32print.SetPrinter(hprinter, 2, properties, 0)
    win32print.ClosePrinter(hprinter)


def printer_is_ready(printer):
    """Return (ok, reason). ok=False if the printer is offline/error/paused."""
    bad = {
        win32print.PRINTER_STATUS_OFFLINE: "offline",
        win32print.PRINTER_STATUS_ERROR: "error",
        win32print.PRINTER_STATUS_PAPER_JAM: "paper jam",
        win32print.PRINTER_STATUS_PAPER_OUT: "out of paper",
        win32print.PRINTER_STATUS_PAUSED: "paused",
        win32print.PRINTER_STATUS_NOT_AVAILABLE: "not available",
        win32print.PRINTER_STATUS_NO_TONER: "no toner",
        win32print.PRINTER_STATUS_DOOR_OPEN: "door open",
    }
    try:
        h = win32print.OpenPrinter(printer)
        try:
            info = win32print.GetPrinter(h, 2)
        finally:
            win32print.ClosePrinter(h)
    except Exception as e:
        return False, f"cannot open printer ({e})"

    status = info.get("Status", 0)
    for bit, reason in bad.items():
        if status & bit:
            return False, reason
    return True, "ready"


def _get_job_ids(printer):
    """Current spooler job IDs for a printer."""
    h = win32print.OpenPrinter(printer)
    try:
        jobs = win32print.EnumJobs(h, 0, 999, 1)
        return {j["JobId"] for j in jobs}, {j["JobId"]: j for j in jobs}
    finally:
        win32print.ClosePrinter(h)


def _find_acrobat():
    """Locate Adobe Acrobat/Reader, which can print to a NAMED printer via /t.
    Returns the exe path, or None if not installed."""
    candidates = [
        r"C:\Program Files\Adobe\Acrobat DC\Acrobat\Acrobat.exe",
        r"C:\Program Files (x86)\Adobe\Acrobat DC\Acrobat\Acrobat.exe",
        r"C:\Program Files (x86)\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe",
        r"C:\Program Files\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe",
        r"C:\Program Files (x86)\Adobe\Reader 11.0\Reader\AcroRd32.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _submit_print(file_path, printer):
    """Submit the PDF to the EXACT named printer.

    Prefers Adobe Acrobat/Reader (/t prints to a named printer and quits, so
    the printer choice is honored). Falls back to ShellExecute if Acrobat isn't
    installed — that path prints to the default viewer's printer and cannot
    guarantee switching. Returns the method used ('acrobat' or 'shell').
    """
    acro = _find_acrobat()
    if acro:
        # AcroRd32.exe /t "file" "printer"  -> print to named printer, then exit
        subprocess.Popen([acro, "/t", file_path, printer],
                         creationflags=subprocess.CREATE_NO_WINDOW)
        return "acrobat"
    win32api.ShellExecute(0, "print", file_path, f'/d:"{printer}"', ".", 0)
    return "shell"


def print_file(file_path, job):
    """Print with confirmation. Returns (status, message).

    status is 'printed' only if the spooler reports the job completed;
    otherwise 'failed' with a reason (offline, timed out, error, etc.).
    """
    printer = get_printer_name()

    # 1) Pre-check: is the printer actually ready?
    ok, reason = printer_is_ready(printer)
    if not ok:
        return "failed", f"printer not ready: {reason}"

    # 2) Apply per-job settings (color/duplex/etc.)
    try:
        configure_printer_settings(printer, job)
    except Exception as e:
        return "failed", f"could not apply settings: {e}"

    # 3) Snapshot existing jobs, submit ONCE to the exact printer, then
    #    find the new job the spooler created for this printer.
    before, _ = _get_job_ids(printer)
    try:
        method = _submit_print(file_path, printer)
    except Exception as e:
        return "failed", f"submit error: {e}"

    # Give the PDF app + spooler time to register the new job.
    new_id = None
    for _ in range(60):  # up to ~30s
        time.sleep(0.5)
        after, jobmap = _get_job_ids(printer)
        added = after - before
        if added:
            new_id = max(added)
            break

    if new_id is None:
        # Job never reached the queue — nothing was printed.
        return "failed", "job did not reach the print queue (no PDF handler?)"

    # 4) Track the job until it leaves the queue (completed) or errors out.
    deadline = time.time() + PRINT_CONFIRM_TIMEOUT
    error_bits = {
        win32print.JOB_STATUS_ERROR: "job error",
        win32print.JOB_STATUS_OFFLINE: "printer offline",
        win32print.JOB_STATUS_PAPEROUT: "out of paper",
        win32print.JOB_STATUS_BLOCKED_DEVQ: "queue blocked",
        win32print.JOB_STATUS_DELETED: "job deleted",
    }
    while time.time() < deadline:
        ids, jobmap = _get_job_ids(printer)
        if new_id not in ids:
            # Job left the queue = printed/spooled successfully.
            return "printed", "completed"
        jstatus = jobmap[new_id].get("Status", 0)
        for bit, reason in error_bits.items():
            if jstatus & bit:
                return "failed", reason
        time.sleep(1)

    return "failed", "timed out waiting for the printer"


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
        # Job ids already submitted this session — guards against submitting the
        # SAME job to the printer twice (root cause of duplicate spooler jobs).
        # We only retry a job if the printer itself reported a failure.
        self.submitted = set()

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
                # Skip any job we already submitted this session. It's either
                # printed (server will drop it soon) or awaiting confirmation —
                # resubmitting would create a duplicate spooler job.
                if job["id"] in self.submitted:
                    continue
                self._handle_job(job)

            self._sleep()
        self.log("Stopped.")

    def _handle_job(self, job):
        job_id = job["id"]
        filename = job.get("original_filename", "?")
        self.log(f"📥 Job {job_id}: {filename}")
        self.record(job, "printing")
        # Mark as attempted immediately so a slow print can't be double-submitted
        # by the next poll while this one is still in flight.
        self.submitted.add(job_id)

        # --- Download the file ---
        try:
            resp = requests.get(job["file_url"], stream=True, timeout=60)
            resp.raise_for_status()
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            if code == 404:
                # File is gone on the server — it will NEVER succeed, so mark it
                # done to stop retrying it forever (prevents the infinite loop).
                self.log(f"⚠️ Job {job_id}: file missing (404) — skipping.")
                try:
                    mark_printed(job_id)
                except Exception:
                    pass
                self.record(job, "skipped (missing file)")
            else:
                self.submitted.discard(job_id)  # transient — allow retry
                self.log(f"❌ Job {job_id}: download failed ({code}).")
                self.record(job, "failed")
            return
        except Exception as e:
            self.submitted.discard(job_id)  # network error — allow retry
            self.log(f"❌ Job {job_id}: download error: {e}")
            self.record(job, "failed")
            return

        # --- Save to temp ---
        mime_type, _ = mimetypes.guess_type(filename)
        ext = ".pdf" if "pdf" in (mime_type or "") else os.path.splitext(filename)[1] or ".pdf"
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(resp.content)
            tmp_path = tmp.name

        # --- Print WITH confirmation ---
        try:
            status, message = print_file(tmp_path, job)
        except Exception as e:
            self.submitted.discard(job_id)  # allow retry
            self.log(f"❌ Job {job_id}: print error: {e}")
            self.record(job, "failed")
            return
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        if status == "printed":
            # Success: keep it in `submitted` so we never resend it. Tell the
            # server it's done (updates DB + deletes the file).
            try:
                mark_printed(job_id)
                self.log(f"✅ Job {job_id} printed ({message}).")
                self.record(job, "printed")
            except Exception as e:
                self.log(f"⚠️ Job {job_id} printed but could not update server: {e}")
                self.record(job, "printed (unsynced)")
        else:
            # Printer REPORTED a failure (offline/paper/etc.). Allow a retry by
            # forgetting it — but only after a cooldown so we don't hammer the
            # printer. It stays 'confirmed' on the server until it truly prints.
            self.submitted.discard(job_id)
            self.log(f"❌ Job {job_id} NOT printed: {message}. Will retry later.")
            self.record(job, f"failed: {message}")

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

        # Do NOT auto-start. Wait for the user to click Start.
        self.status_lbl.config(text="●  stopped (click Start)", foreground="red")
        self._append_log("Ready. Click Start to begin printing.")

    def _build_ui(self):
        # Top bar
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Mohini Print Worker", font=("Segoe UI", 14, "bold")).pack(side="left")
        self.status_lbl = ttk.Label(top, text="●  stopped", foreground="red")
        self.status_lbl.pack(side="left", padx=15)
        self.start_btn = ttk.Button(top, text="Start", command=self.toggle)
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

        # Tray selector (hidden when the printer reports no trays)
        self.tray_label = ttk.Label(prow, text="Tray:")
        self.tray_var = tk.StringVar()
        self.tray_combo = ttk.Combobox(prow, textvariable=self.tray_var,
                                       state="readonly", width=25)
        self.tray_combo.bind("<<ComboboxSelected>>", self._on_tray_change)
        self._trays = []  # list of (label, bin_id) for the current printer

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
        self._refresh_trays(chosen)

    def _on_printer_change(self, _event=None):
        chosen = self.printer_var.get()
        set_selected_printer(chosen or None)
        self._append_log(f"🖨 Printer switched to: {chosen}")
        self._refresh_trays(chosen)

    # --- tray selection ---
    def _refresh_trays(self, printer):
        """Populate the tray dropdown for a printer, or hide it if none."""
        self._trays = list_trays(printer) if printer else []

        if not self._trays:
            # No trays reported — hide the dropdown and use printer default.
            self.tray_label.pack_forget()
            self.tray_combo.pack_forget()
            set_selected_tray(None)
            return

        # Show it, with an Auto option first.
        labels = [AUTO_TRAY_LABEL] + [t[0] for t in self._trays]
        self.tray_combo["values"] = labels
        self.tray_var.set(AUTO_TRAY_LABEL)
        set_selected_tray(None)  # default to Auto
        self.tray_label.pack(side="left", padx=(12, 0))
        self.tray_combo.pack(side="left", padx=8)

    def _on_tray_change(self, _event=None):
        label = self.tray_var.get()
        if label == AUTO_TRAY_LABEL:
            set_selected_tray(None)
            self._append_log("📥 Tray: auto (printer default)")
            return
        for name, bin_id in self._trays:
            if name == label:
                set_selected_tray(bin_id)
                self._append_log(f"📥 Tray switched to: {name}")
                return

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
