import os
import time
import requests
import tempfile
import mimetypes
import win32print
import win32api
from supabase import create_client, Client

# --- Supabase Setup ---
SUPABASE_URL = "https://fgksbxrxskwchjyqxpvx.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZna3NieHJ4c2t3Y2hqeXF4cHZ4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc1NjgxODM5MSwiZXhwIjoyMDcyMzk0MzkxfQ.l5Uujx1rpnVMGCukQtrYDP2n_RcCDMC5mlcCES8rBTc"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

BUCKET_NAME = "pdfs"

# --- Printer Config ---
PRINTER_NAME = None  # None = system default printer

def get_printer_name():
    return PRINTER_NAME or win32print.GetDefaultPrinter()

def configure_printer_settings(printer, job):
    """Apply DB fields as printer DEVMODE settings."""
    hprinter = win32print.OpenPrinter(printer)
    properties = win32print.GetPrinter(hprinter, 2)
    devmode = properties["pDevMode"]

    # --- Color ---
    if job["color_mode"] == "color":
        devmode.Color = 2  # DMCOLOR_COLOR
    else:
        devmode.Color = 1  # DMCOLOR_MONOCHROME

    # --- Duplex / Sides ---
    if job["sides"] == "double":
        devmode.Duplex = 2  # DMDUP_VERTICAL
    else:
        devmode.Duplex = 1  # DMDUP_SIMPLEX

    # --- Orientation ---
    if job["orientation"] == "landscape":
        devmode.Orientation = 2  # DMORIENT_LANDSCAPE
    else:
        devmode.Orientation = 1  # DMORIENT_PORTRAIT

    # --- Paper size ---
    paper_map = {
        "A4": 9,       # DMPAPER_A4
        "A3": 8,       # DMPAPER_A3
        "Letter": 1,   # DMPAPER_LETTER
    }
    if job["paper_size"] and job["paper_size"] in paper_map:
        devmode.PaperSize = paper_map[job["paper_size"]]

    # --- Page scaling (like 1-3, 4, 6-8 per sheet) ---
    # NOTE: Native win32 doesn’t directly support N-up printing
    # Some drivers support it via dmFields but it’s vendor-specific
    # Here we just log it for now:

    # Apply updated devmode
    properties["pDevMode"] = devmode
    win32print.SetPrinter(hprinter, 2, properties, 0)
    win32print.ClosePrinter(hprinter)

def print_file(file_path, job):
    """Send file to printer with settings."""
    printer = get_printer_name()
    print(f"🖨 Printing {file_path} on {printer} ...")
    try:
        configure_printer_settings(printer, job)
        win32api.ShellExecute(
            0,
            "print",
            file_path,
            f'/d:"{printer}"',
            ".",
            0
        )
        return True
    except Exception as e:
        print("Print error:", e)
        return False

def process_jobs():
    jobs = supabase.table("print_jobs").select("*").eq("status", "confirmed").execute()

    if not jobs.data:
        print("No jobs to process.")
        return

    for job in jobs.data:
        job_id = job["id"]
        file_url = job["file_url"]
        filename = job["original_filename"]

        print(f"📥 Found job {job_id}: {filename}")

        try:
            # Download file
            resp = requests.get(file_url, stream=True)
            resp.raise_for_status()

            mime_type, _ = mimetypes.guess_type(filename)
            ext = ".pdf" if "pdf" in (mime_type or "") else os.path.splitext(filename)[1] or ".pdf"

            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                tmp.write(resp.content)
                tmp_path = tmp.name

            # Print
            if print_file(tmp_path, job):
                supabase.table("print_jobs").update({"status": "printed"}).eq("id", job_id).execute()

                file_key = os.path.basename(file_url.split("?")[0])
                supabase.storage.from_(BUCKET_NAME).remove([file_key])

                print(f"✅ Job {job_id} printed & cleaned up.")
            else:
                print(f"❌ Job {job_id} failed to print.")

        except Exception as e:
            print(f"⚠️ Error processing job {job_id}: {e}")

def main():
    print("📡 Print worker started. Watching for jobs...")
    while True:
        process_jobs()
        time.sleep(10)

if __name__ == "__main__":
    main()
