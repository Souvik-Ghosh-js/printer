from flask import Flask, request, jsonify, render_template, send_file
from werkzeug.utils import secure_filename
from supabase import create_client
import uuid
import mimetypes
from PyPDF2 import PdfReader, PdfWriter
import io

# --- Supabase Config ---
SUPABASE_URL = "https://fgksbxrxskwchjyqxpvx.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZna3NieHJ4c2t3Y2hqeXF4cHZ4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc1NjgxODM5MSwiZXhwIjoyMDcyMzk0MzkxfQ.l5Uujx1rpnVMGCukQtrYDP2n_RcCDMC5mlcCES8rBTc"
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


# --- Preview PDF (temporary, not stored) ---
@app.route("/preview", methods=["POST"])
def preview_pdf():
    """
    Accepts: file, orientation, color_mode, pages (optional)
    Returns: PDF file (blob) + X-Total-Pages header
    """
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Missing file"}), 400

    orientation = request.form.get("orientation", "portrait")
    color_mode = request.form.get("color_mode", "color")
    page_range_str = request.form.get("pages", "").strip()

    try:
        file_bytes = file.read()
        reader = PdfReader(io.BytesIO(file_bytes))
        total_pages = len(reader.pages)

        # --- Parse page range ---
        selected_indices = []
        if page_range_str:
            # e.g. "1-3,5" -> [0,1,2,4]
            for part in page_range_str.split(","):
                part = part.strip()
                if not part:
                    continue
                if "-" in part:
                    start, end = part.split("-")
                    start = int(start)
                    end = int(end)
                    selected_indices.extend(range(start - 1, end))
                else:
                    selected_indices.append(int(part) - 1)
            selected_indices = [i for i in selected_indices if 0 <= i < total_pages]
        else:
            selected_indices = list(range(total_pages))

        writer = PdfWriter()
        for i in selected_indices:
            page = reader.pages[i]
            if orientation == "landscape":
                page.rotate(90)
            writer.add_page(page)

        buf = io.BytesIO()
        writer.write(buf)
        buf.seek(0)

        response = send_file(buf, mimetype="application/pdf", as_attachment=False)
        response.headers["X-Total-Pages"] = str(total_pages)
        return response

    except Exception as e:
        return jsonify({"error": "Preview generation failed", "detail": str(e)}), 500


# --- Upload PDF (after user confirms preview) ---
@app.route("/upload", methods=["POST"])
def upload_pdf():
    file = request.files.get("file")
    customer_id = request.form.get("customer_id")
    if not file or not customer_id:
        return jsonify({"error": "Missing file or customer_id"}), 400

    original_filename = secure_filename(file.filename)
    filename = f"{uuid.uuid4()}_{original_filename}"
    file_bytes = file.read()

    mime_type, _ = mimetypes.guess_type(original_filename)
    if not mime_type:
        mime_type = "application/pdf"

    # --- Parse and filter pages based on user input ---
    page_range_str = request.form.get("pages", "").strip()
    filtered_pdf_bytes = file_bytes  # fallback if no filtering done

    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        total_pages = len(reader.pages)

        selected_indices = []
        if page_range_str:
            for part in page_range_str.split(","):
                part = part.strip()
                if not part:
                    continue
                if "-" in part:
                    start, end = map(int, part.split("-"))
                    selected_indices.extend(range(start - 1, end))
                else:
                    selected_indices.append(int(part) - 1)
            selected_indices = [i for i in selected_indices if 0 <= i < total_pages]
        else:
            selected_indices = list(range(total_pages))

        # Get orientation selection from request
        orientation = request.form.get("orientation", "portrait")

        writer = PdfWriter()
        for i in selected_indices:
            page = reader.pages[i]
            if orientation == "landscape":
                page.rotate(90)  # <-- Rotate pages for saved file
            writer.add_page(page)

        buf = io.BytesIO()
        writer.write(buf)
        buf.seek(0)
        filtered_pdf_bytes = buf.read()
        total_pages = len(selected_indices)  # update total pages to reflect filtered file

    except Exception as e:
        print(f"[WARN] Page filtering/rotation failed: {e}")
        total_pages = None


    # --- Upload to Supabase Storage ---
    try:
        supabase.storage.from_("pdfs").upload(filename, filtered_pdf_bytes, {"content-type": mime_type})
        file_url = supabase.storage.from_("pdfs").get_public_url(filename)
    except Exception as e:
        return jsonify({"error": "Storage upload failed", "detail": str(e)}), 500

    # gather settings & store in DB
    sides = request.form.get("sides")
    orientation = request.form.get("orientation")
    color_mode = request.form.get("color_mode")
    paper_size = request.form.get("paper_size")
    price = request.form.get("price")

    job_payload = {
        "customer_id": customer_id,
        "file_url": file_url,
        "original_filename": original_filename,
        "status": "uploaded",
        "total_pages": total_pages,
        "sides": sides,
        "orientation": orientation,
        "color_mode": color_mode,
        "paper_size": paper_size,
        "page_range": page_range_str,
        "price": price
    }

    try:
        job = supabase.table("print_jobs").insert(job_payload).execute()
    except Exception as e:
        return jsonify({"error": "DB insert failed", "detail": str(e)}), 500

    try:
        created = job.data[0]
        return jsonify({
            "job_id": created.get("id"),
            "file_url": file_url,
            "total_pages": total_pages
        })
    except Exception:
        return jsonify({
            "job": job.data,
            "file_url": file_url,
            "total_pages": total_pages
        })


# --- Confirm print (set job to confirmed and store final settings like price) ---
@app.route("/print", methods=["POST"])
def confirm_print():
    data = request.json or {}
    job_id = data.get("job_id")
    if not job_id:
        return jsonify({"error": "job_id required"}), 400

    update_data = {
        "total_pages": data.get("total_pages"),
        "color_mode": data.get("color_mode"),
        "sides": data.get("sides"),
        "orientation": data.get("orientation"),
        "paper_size": data.get("paper_size"),
        "price": data.get("price"),
        "status": "confirmed",
        "page_range": data.get("pages")
    }

    try:
        job = supabase.table("print_jobs").update(update_data).eq("id", job_id).execute()
        return jsonify({"status": "confirmed", "job": job.data})
    except Exception as e:
        return jsonify({"error": "Failed to confirm print", "detail": str(e)}), 500


if __name__ == "__main__":
    print("[SERVER] Running Flask at http://127.0.0.1:5000")
    app.run(debug=True)
