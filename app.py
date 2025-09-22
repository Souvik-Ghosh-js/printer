from flask import Flask, request, jsonify, render_template, send_file
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from supabase import create_client
import uuid
import mimetypes
from PyPDF2 import PdfReader, PdfWriter
import io
import os
import fitz  # PyMuPDF
import tempfile
import hmac, hashlib
import requests
import json
import time
from datetime import datetime

# --- Supabase Config ---
SUPABASE_URL = "https://fgksbxrxskwchjyqxpvx.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZna3NieHJ4c2t3Y2hqeXF4cHZ4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc1NjgxODM5MSwiZXhwIjoyMDcyMzk0MzkxfQ.l5Uujx1rpnVMGCukQtrYDP2n_RcCDMC5mlcCES8rBTc"
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Cashfree Config ---
CASHFREE_APP_ID = "1081246de8c8aebb039c1d380b76421801"
CASHFREE_SECRET_KEY = "cfsk_ma_prod_6c4dd5ba946f5eb8edc06b90e80d8332_642d89ae"
CASHFREE_ENV = "production"
WEBHOOK_SECRET = "x191i9m9ymo4skygxh2z"

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

@app.route("/")
def index():
    return render_template("index.html")

def process_pdf_with_options(file_bytes, orientation, color_mode, page_range_str):
    """
    Process PDF with given options and return processed bytes using PyMuPDF
    """
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    total_pages = len(doc)

    # --- Parse page range ---
    selected_indices = []
    if page_range_str:
        for part in page_range_str.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start, end = part.split("-")
                start = int(start.strip())
                end = int(end.strip())
                selected_indices.extend(range(start - 1, end))
            else:
                selected_indices.append(int(part.strip()) - 1)
        selected_indices = [i for i in selected_indices if 0 <= i < total_pages]
    else:
        selected_indices = list(range(total_pages))

    # Create a new PDF document
    new_doc = fitz.open()

    for page_num in selected_indices:
        page = doc.load_page(page_num)
        
        # Create a new page with the same dimensions
        rect = page.rect
        new_page = new_doc.new_page(width=rect.width, height=rect.height)
        
        # Define transformation matrix for rotation if needed
        mat = fitz.Matrix(1, 1)
        if orientation == "landscape":
            mat = fitz.Matrix(0, 1, -1, 0, rect.width, 0)
        
        # Render the page to a pixmap with color conversion if needed
        if color_mode == "bw":
            # Convert to grayscale during rendering
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
        else:
            # Keep original colors
            pix = page.get_pixmap(matrix=mat)
        
        # Insert the pixmap into the new page
        new_page.insert_image(new_page.rect, pixmap=pix)
    
    # Save the processed PDF to bytes
    pdf_bytes = new_doc.tobytes()
    new_doc.close()
    doc.close()
    
    return pdf_bytes, len(selected_indices)

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
        
        # Process PDF with all options
        processed_pdf_bytes, total_pages = process_pdf_with_options(
            file_bytes, orientation, color_mode, page_range_str
        )

        response = send_file(
            io.BytesIO(processed_pdf_bytes),
            mimetype="application/pdf",
            as_attachment=False
        )
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

    # Get processing options from request
    page_range_str = request.form.get("pages", "").strip()
    orientation = request.form.get("orientation", "portrait")
    color_mode = request.form.get("color_mode", "color")
    price = request.form.get("price", "0.00")

    try:
        # Process PDF with all options (same as preview)
        filtered_pdf_bytes, total_pages = process_pdf_with_options(
            file_bytes, orientation, color_mode, page_range_str
        )
    except Exception as e:
        print(f"[WARN] PDF processing failed: {e}")
        # Fallback: use original file without processing
        filtered_pdf_bytes = file_bytes
        total_pages = len(PdfReader(io.BytesIO(file_bytes)).pages)

    # --- Upload to Supabase Storage ---
    try:
        supabase.storage.from_("pdfs").upload(filename, filtered_pdf_bytes, {"content-type": mime_type})
        file_url = supabase.storage.from_("pdfs").get_public_url(filename)
    except Exception as e:
        return jsonify({"error": "Storage upload failed", "detail": str(e)}), 500

    # Gather settings & store in DB
    sides = request.form.get("sides", "single")
    paper_size = request.form.get("paper_size", "A4")

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
        "price": price,
        "payment_status": "pending",
        "created_at": datetime.now().isoformat()
    }

    try:
        job = supabase.table("print_jobs").insert(job_payload).execute()
        created = job.data[0] if job.data else None
        
        if created:
            return jsonify({
                "job_id": created.get("id"),
                "file_url": file_url,
                "total_pages": total_pages
            })
        else:
            return jsonify({"error": "Failed to create job"}), 500
            
    except Exception as e:
        return jsonify({"error": "DB insert failed", "detail": str(e)}), 500

def create_cashfree_payment_session(order_id, order_amount, customer_id, customer_email, customer_phone):
    """Create a payment session with Cashfree"""
    url = "https://api.cashfree.com/pg/orders" if CASHFREE_ENV == "production" else "https://sandbox.cashfree.com/pg/orders"
    
    headers = {
        "Content-Type": "application/json",
        "x-api-version": "2022-09-01",
        "x-client-id": CASHFREE_APP_ID,
        "x-client-secret": CASHFREE_SECRET_KEY
    }
    
    # Ensure we're using HTTPS for the return URL
    base_url = request.url_root.replace('http://', 'https://')
    return_url = f"{base_url}payment-callback"
    
    payload = {
        "order_id": order_id,
        "order_amount": float(order_amount),
        "order_currency": "INR",
        "order_note": "Print job payment",
        "customer_details": {
            "customer_id": customer_id,
            "customer_email": customer_email,
            "customer_phone": customer_phone
        },
        "order_meta": {
            "return_url": return_url,
            "notify_url": f"{base_url}payment-webhook"
        }
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        response_data = response.json()
        
        if response.status_code == 200:
            return {
                "success": True,
                "payment_session_id": response_data.get("payment_session_id"),
                "order_id": order_id
            }
        else:
            print(f"Cashfree API error: {response.status_code} - {response_data}")
            return {
                "success": False,
                "error": response_data.get("message", "Unknown error from Cashfree")
            }
    except Exception as e:
        print(f"Cashfree API exception: {e}")
        return {
            "success": False,
            "error": str(e)
        }

@app.route("/create-payment", methods=["POST"])
def create_payment():
    """Create a payment session for a print job"""
    data = request.json
    job_id = data.get("job_id")
    price = data.get("price", 0)
    
    if not job_id:
        return jsonify({"error": "Job ID is required"}), 400
    
    # Get job details from database
    try:
        job_result = supabase.table("print_jobs").select("*").eq("id", job_id).execute()
        if not job_result.data:
            return jsonify({"error": "Job not found"}), 404
        
        job = job_result.data[0]
        customer_id = job.get("customer_id", "customer_123")
        
    except Exception as e:
        print(f"Error fetching job: {e}")
        customer_id = "customer_123"
    
    # Generate a unique order ID
    order_id = f"JOB_{job_id}_{int(time.time())}"
    
    # Create payment session with Cashfree
    result = create_cashfree_payment_session(
        order_id=order_id,
        order_amount=price,
        customer_id=customer_id,
        customer_email="customer@example.com",  # You should collect this from users
        customer_phone="9999999999"  # You should collect this from users
    )
    
    if result["success"]:
        # Update the job with order ID
        try:
            supabase.table("print_jobs").update({
                "order_id": order_id,
                "payment_status": "pending",
            }).eq("id", job_id).execute()
        except Exception as e:
            print(f"Error updating job with order ID: {e}")
        
        return jsonify({
            "payment_session_id": result["payment_session_id"],
            "order_id": order_id,
            "mode": CASHFREE_ENV
        })
    else:
        return jsonify({"error": result["error"]}), 400

@app.route("/payment-callback", methods=["GET"])
def payment_callback():
    """Handle payment return URL (user redirected back after payment)"""
    order_id = request.args.get("order_id")
    payment_status = request.args.get("payment_status", "unknown")
    
    if order_id:
        # Update payment status based on callback parameters
        try:
            update_data = {
                "payment_status": payment_status.lower(),
            }
            
            if payment_status.lower() == "success":
                update_data["status"] = "completed"
                update_data["paid_at"] = datetime.now().isoformat()
            
            supabase.table("print_jobs").update(update_data).eq("order_id", order_id).execute()
        except Exception as e:
            print(f"Error updating payment status: {e}")
    
    return render_template("index.html", order_id=order_id, status=payment_status)

@app.route("/razorpay/webhook", methods=["POST"])
def payment_webhook():
    """Handle payment webhook from Cashfree"""
    try:
        # Get webhook data
        webhook_data = request.get_json()
        if not webhook_data:
            return jsonify({"error": "No webhook data received"}), 400
        
        print("Received webhook:", json.dumps(webhook_data, indent=2))
        
        # Verify webhook signature
        signature = request.headers.get("x-webhook-signature")
        if not verify_webhook_signature(webhook_data, signature):
            print("Invalid webhook signature" , signature)
            return jsonify({"error": "Invalid signature"}), 401
        
        # Extract order information
        order_info = webhook_data.get("data", {}).get("order", {}) or webhook_data.get("order", {})
        order_id = order_info.get("order_id")
        payment_status = order_info.get("order_status")
        transaction_id = order_info.get("transaction_id")
        payment_amount = order_info.get("order_amount")
        
        if not order_id or not payment_status:
            return jsonify({"error": "Missing order_id or order_status"}), 400
        
        print(f"Processing order {order_id} with status: {payment_status}")
        
        # Update the order status in database
        update_data = {
            "payment_status": payment_status.lower(),
        }
        
        # Add transaction ID if available
        if transaction_id:
            update_data["transaction_id"] = transaction_id
        
        # If payment is successful, update job status
        if payment_status.upper() == "PAID":
            update_data["status"] = "paid"
            update_data["paid_at"] = datetime.now().isoformat()
        elif payment_status.upper() in ["FAILED", "EXPIRED"]:
            update_data["status"] = "payment_failed"
        
        try:
            # Update the print job in database
            result = supabase.table("print_jobs").update(update_data).eq("order_id", order_id).execute()
            
            if len(result.data) == 0:
                print(f"Order {order_id} not found in database")
                return jsonify({"error": "Order not found"}), 404
                
            print(f"Successfully updated order {order_id} with status {payment_status}")
            
        except Exception as e:
            print(f"Error updating database for order {order_id}: {e}")
            return jsonify({"error": "Database update failed"}), 500
        
        return jsonify({"status": "success", "message": "Webhook processed successfully"})
    
    except Exception as e:
        print(f"Error processing webhook: {e}")
        return jsonify({"error": "Webhook processing failed"}), 500

def verify_webhook_signature(webhook_data, signature):
    """Verify Cashfree webhook signature"""
    if not signature:
        return False
    
    try:
        # Convert webhook data to string and encode
        webhook_body = json.dumps(webhook_data, separators=(',', ':'), sort_keys=True)
        webhook_body_bytes = webhook_body.encode('utf-8')
        
        # Compute HMAC SHA256 signature
        computed_signature = hmac.new(
            key=WEBHOOK_SECRET.encode('utf-8'),
            msg=webhook_body_bytes,
            digestmod=hashlib.sha256
        ).hexdigest()
        
        # Compare signatures
        return hmac.compare_digest(computed_signature, signature)
    except Exception as e:
        print(f"Error verifying signature: {e}")
        return False

@app.route("/check-payment-status/<order_id>")
def check_payment_status(order_id):
    """Check the payment status of an order"""
    try:
        # Get order details from database
        result = supabase.table("print_jobs").select("payment_status, status, order_id").eq("order_id", order_id).execute()
        
        if result.data:
            payment_status = result.data[0].get("payment_status", "unknown")
            job_status = result.data[0].get("status", "unknown")
            
            return jsonify({
                "order_id": order_id,
                "payment_status": payment_status,
                "job_status": job_status
            })
        else:
            return jsonify({"error": "Order not found"}), 404
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)