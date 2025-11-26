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
import cv2
import numpy as np

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

def analyze_image_bw_percentage(image_array, threshold_fraction=0.90):
    """
    Analyzes an image array and calculates the percentage of black and white pixels
    using the new threshold fraction logic.
    
    Args:
        image_array: numpy array of the image
        threshold_fraction (float): The fraction threshold for binarization (0-1).
    
    Returns:
        tuple: (percentage_black, percentage_white)
    """
    try:
        # Convert to grayscale if needed
        if len(image_array.shape) == 3:
            gray_img = cv2.cvtColor(image_array, cv2.COLOR_BGR2GRAY)
        else:
            gray_img = image_array

        # Determine the threshold value (0-255) using fraction
        threshold_value = int(threshold_fraction * 255)

        # Apply binarization (inverted: black pixels become 0, white become 255)
        _, bw_img = cv2.threshold(gray_img, threshold_value, 255, cv2.THRESH_BINARY)

        # Calculate pixel counts
        total_pixels = bw_img.size
        white_pixels = np.count_nonzero(bw_img == 255)
        black_pixels = np.count_nonzero(bw_img == 0)

        # Calculate percentages
        percent_white = (white_pixels / total_pixels) * 100
        percent_black = (black_pixels / total_pixels) * 100

        return percent_black, percent_white
        
    except Exception as e:
        print(f"Error analyzing image: {e}")
        return 0, 100  # Default to white page if analysis fails
def analyze_pdf_page_content(pdf_bytes, page_range=None, dpi=150):
    """
    Analyzes PDF pages to determine black percentage for pricing.
    
    Args:
        pdf_bytes: PDF file bytes
        page_range: List of page indices to analyze (0-based)
        dpi: Resolution for image conversion
    
    Returns:
        dict: Page analysis results and pricing information
    """
    try:
        # Open PDF with PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        total_pages = len(doc)
        
        # Determine which pages to analyze
        if page_range is None:
            pages_to_analyze = list(range(total_pages))
        else:
            pages_to_analyze = [p for p in page_range if 0 <= p < total_pages]
        
        page_analysis = []
        high_black_pages = 0
        
        for page_num in pages_to_analyze:
            try:
                page = doc.load_page(page_num)
                
                # Convert page to image with specified DPI
                mat = fitz.Matrix(dpi/72, dpi/72)  # 72 is the default PDF DPI
                pix = page.get_pixmap(matrix=mat)
                
                # Convert to numpy array
                img_data = np.frombuffer(pix.samples, dtype=np.uint8)
                img_array = img_data.reshape(pix.height, pix.width, pix.n)
                
                # Analyze black percentage
                black_percent, white_percent = analyze_image_bw_percentage(img_array, threshold_fraction=0.90)
                
                # Determine if page has high black content (>50%)
                is_high_black = black_percent > 30
                if is_high_black:
                    high_black_pages += 1
                
                page_analysis.append({
                    'page_number': page_num + 1,
                    'black_percent': black_percent,
                    'white_percent': white_percent,
                    'is_high_black': is_high_black
                })
                
            except Exception as e:
                print(f"Error analyzing page {page_num + 1}: {e}")
                # Default to non-high-black page if analysis fails
                page_analysis.append({
                    'page_number': page_num + 1,
                    'black_percent': 0,
                    'white_percent': 100,
                    'is_high_black': False
                })
        
        doc.close()
        
        return {
            'total_pages': len(pages_to_analyze),
            'high_black_pages': high_black_pages,
            'normal_pages': len(pages_to_analyze) - high_black_pages,
            'page_analysis': page_analysis
        }
        
    except Exception as e:
        print(f"Error in PDF analysis: {e}")
        return {
            'total_pages': 0,
            'high_black_pages': 0,
            'normal_pages': 0,
            'page_analysis': []
        }

def calculate_price_v2(total_pages, high_black_pages, normal_pages):
    """
    CORRECTED PRICING LOGIC:
    - Base price for ALL pages: 4 or fewer pages - ₹3 per page, More than 4 pages - ₹2 per page
    - Additional ₹2 per high black page
    """
    if total_pages <= 0:
        return "0.00"
    
    # Base price for ALL pages
    base_rate = 3 if total_pages <= 4 else 2
    base_price = total_pages * base_rate  # Apply to ALL pages
    
    # Additional charge for high black pages ONLY
    high_black_charge = high_black_pages * 2
    
    total_price = base_price + high_black_charge
    
    return f"{total_price:.2f}"
    
def get_page_range_from_string(page_range_str, max_page):
    """
    Convert page range string to list of page indices.
    """
    if not page_range_str or not page_range_str.strip():
        return list(range(max_page))
    
    selected_indices = []
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
    
    # Filter valid indices
    selected_indices = [i for i in selected_indices if 0 <= i < max_page]
    return selected_indices

def process_pdf_with_options(file_bytes, orientation, color_mode, page_range_str):
    """
    Process PDF with given options while preserving maximum quality
    """
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    total_pages = len(doc)

    # Parse page range
    selected_indices = get_page_range_from_string(page_range_str, total_pages)

    # For simple operations, use PyPDF2 which preserves quality better
    if orientation == "portrait" and color_mode == "color":
        # Simple case - just extract pages using PyPDF2
        pdf_reader = PdfReader(io.BytesIO(file_bytes))
        pdf_writer = PdfWriter()
        
        for page_num in selected_indices:
            pdf_writer.add_page(pdf_reader.pages[page_num])
        
        output_stream = io.BytesIO()
        pdf_writer.write(output_stream)
        pdf_bytes = output_stream.getvalue()
        total_pages_processed = len(selected_indices)
        
    else:
        # Complex case - use PyMuPDF but avoid rasterization
        new_doc = fitz.open()
        
        for page_num in selected_indices:
            page = doc.load_page(page_num)
            new_page = new_doc.new_page(width=page.rect.width, height=page.rect.height)
            
            # Copy the page content directly
            new_page.show_pdf_page(new_page.rect, doc, page_num)
            
            if orientation == "landscape":
                new_page.set_rotation(90)
        
        pdf_bytes = new_doc.tobytes()
        new_doc.close()
        total_pages_processed = len(selected_indices)
    
    doc.close()
    return pdf_bytes, total_pages_processed, selected_indices

@app.route("/")
def index():
    return render_template("index.html")

# --- Preview PDF (temporary, not stored) ---
@app.route("/preview", methods=["POST"])
def preview_pdf():
    """
    Accepts: file, orientation, color_mode, pages (optional)
    Returns: PDF file (blob) + X-Total-Pages header + X-Price header
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
        processed_pdf_bytes, total_pages, selected_indices = process_pdf_with_options(
            file_bytes, orientation, color_mode, page_range_str
        )

        # Analyze PDF content for pricing
        analysis_result = analyze_pdf_page_content(file_bytes, selected_indices)
        
        # Calculate price with new logic
        price = calculate_price_v2(
            analysis_result['total_pages'],
            analysis_result['high_black_pages'],
            analysis_result['normal_pages']
        )

        response = send_file(
            io.BytesIO(processed_pdf_bytes),
            mimetype="application/pdf",
            as_attachment=False
        )
        response.headers["X-Total-Pages"] = str(total_pages)
        response.headers["X-Price"] = price
        response.headers["X-High-Black-Pages"] = str(analysis_result['high_black_pages'])
        response.headers["X-Normal-Pages"] = str(analysis_result['normal_pages'])
        
        return response

    except Exception as e:
        return jsonify({"error": "Preview generation failed", "detail": str(e)}), 500

# --- Upload PDF (after user confirms preview) ---
@app.route("/upload", methods=["POST"])
def upload_pdf():
    print("=== UPLOAD PROCESS STARTED ===")
    
    file = request.files.get("file")
    customer_id = request.form.get("customer_id")
    
    if not file or not customer_id:
        print("❌ ERROR: Missing file or customer_id")
        return jsonify({"error": "Missing file or customer_id"}), 400

    original_filename = secure_filename(file.filename)
    filename = f"{uuid.uuid4()}_{original_filename}"
    
    print(f"📁 File: {original_filename}")
    print(f"👤 Customer ID: {customer_id}")
    print(f"🔧 Generated filename: {filename}")

    # Read file bytes
    file_bytes = file.read()
    print(f"📄 File size: {len(file_bytes)} bytes")

    mime_type, _ = mimetypes.guess_type(original_filename)
    if not mime_type:
        mime_type = "application/pdf"
    print(f"📋 MIME type: {mime_type}")

    # Get processing options from request
    page_range_str = request.form.get("pages", "").strip()
    orientation = request.form.get("orientation", "portrait")
    color_mode = request.form.get("color_mode", "color")
    
    print(f"⚙️  Settings - Orientation: {orientation}, Color: {color_mode}, Page range: '{page_range_str}'")

    try:
        print("🔄 Processing PDF with options...")
        # Process PDF with all options (same as preview)
        filtered_pdf_bytes, total_pages, selected_indices = process_pdf_with_options(
            file_bytes, orientation, color_mode, page_range_str
        )
        print(f"✅ PDF processed - Total pages: {total_pages}, Selected indices: {selected_indices}")
        
        print("🔍 Analyzing PDF content for pricing...")
        # Analyze PDF content for pricing
        analysis_result = analyze_pdf_page_content(file_bytes, selected_indices)
        print(f"📊 Analysis result: {analysis_result}")
        
        # Calculate price with new logic
        price = calculate_price_v2(
            analysis_result['total_pages'],
            analysis_result['high_black_pages'],
            analysis_result['normal_pages']
        )
        print(f"💰 Calculated price: ₹{price}")
        
    except Exception as e:
        print(f"❌ WARNING: PDF processing failed: {e}")
        import traceback
        traceback.print_exc()  # This will print the full stack trace
        
        # Fallback: use original file without processing
        print("🔄 Using fallback processing...")
        filtered_pdf_bytes = file_bytes
        total_pages = len(PdfReader(io.BytesIO(file_bytes)).pages)
        price = "0.00"
        analysis_result = {
            'total_pages': total_pages,
            'high_black_pages': 0,
            'normal_pages': total_pages
        }
        print(f"🔄 Fallback values - Total pages: {total_pages}, Price: ₹{price}")

    # --- Upload to Supabase Storage ---
    print("☁️  Uploading to Supabase storage...")
    try:
        upload_result = supabase.storage.from_("pdfs").upload(filename, filtered_pdf_bytes, {"content-type": mime_type})
        file_url = supabase.storage.from_("pdfs").get_public_url(filename)
        print(f"✅ Storage upload successful: {file_url}")
    except Exception as e:
        print(f"❌ ERROR: Storage upload failed: {e}")
        return jsonify({"error": "Storage upload failed", "detail": str(e)}), 500

    # Gather settings & store in DB
    sides = request.form.get("sides", "single")
    paper_size = request.form.get("paper_size", "A4")
    
    print(f"📋 Final settings - Sides: {sides}, Paper size: {paper_size}")

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

    print("💾 Storing job in database...")
    try:
        job = supabase.table("print_jobs").insert(job_payload).execute()
        created = job.data[0] if job.data else None
        
        if created:
            print(f"✅ Database insert successful - Job ID: {created.get('id')}")
            response_data = {
                "job_id": created.get("id"),
                "file_url": file_url,
                "total_pages": total_pages,
                "high_black_pages": analysis_result['high_black_pages'],
                "normal_pages": analysis_result['normal_pages'],
                "price": price
            }
            print(f"📦 Response data: {response_data}")
            print("=== UPLOAD PROCESS COMPLETED SUCCESSFULLY ===")
            return jsonify(response_data)
        else:
            print("❌ ERROR: Failed to create job - No data returned from database")
            return jsonify({"error": "Failed to create job"}), 500
            
    except Exception as e:
        print(f"❌ ERROR: DB insert failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "DB insert failed", "detail": str(e)}), 500
# ... [Keep the existing payment-related functions unchanged - create_cashfree_payment_session, create_payment, payment_webhook, verify_webhook_signature, check_payment_status] ...

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
            "notify_url": f"{base_url}payment-callback"
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

    if not job_id:
        return jsonify({"error": "Job ID is required"}), 400
    
    # Get job details from database
    try:
        job_result = supabase.table("print_jobs").select("*").eq("id", job_id).execute()
        if not job_result.data:
            return jsonify({"error": "Job not found"}), 404
        
        job = job_result.data[0]
        customer_id = job.get("customer_id", "customer_123")
        price = job.get("price", "0.00")  # ✅ Get price from database

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
            "price": price,  # ✅ Return the actual price to frontend

            "mode": CASHFREE_ENV
        })
    else:
        return jsonify({"error": result["error"]}), 400

@app.route("/payment-callback", methods=["POST"])
def payment_webhook():
    """Handle payment webhook from Cashfree"""
    try:
        # Get webhook data
        webhook_data = request.get_json()
        if not webhook_data:
            print("No webhook data received")
            return jsonify({"error": "No webhook data received"}), 400
        
        print("Received webhook:", json.dumps(webhook_data, indent=2))
        
        # Verify webhook signature
        
        # Extract order information based on actual webhook structure
        order_data = webhook_data.get("data", {}).get("order", {})
        order_id = order_data.get("order_id")
        
        # Get payment status from payment section
        payment_data = webhook_data.get("data", {}).get("payment", {})
        payment_status = payment_data.get("payment_status", "").upper()
        
        transaction_id = payment_data.get("cf_payment_id")
        payment_amount = payment_data.get("payment_amount")
        
        if not order_id:
            print("Missing order_id in webhook data")
            return jsonify({"error": "Missing order_id"}), 400
        
        print(f"Processing order {order_id} with status: {payment_status}")
        
        # Map Cashfree status to your application status
        status_mapping = {
            "SUCCESS": "paid",
            "FAILED": "failed",
            "USER_DROPPED": "cancelled",
            "EXPIRED": "expired"
        }
        
        payment_status_lower = status_mapping.get(payment_status, "pending")
        
        # Update the order status in database
        update_data = {
            "payment_status": payment_status_lower,
            "transaction_id": transaction_id,
        }
        
        # If payment is successful, update job status
        if payment_status == "SUCCESS":
            update_data["status"] = "confirmed"
            update_data["paid_at"] = datetime.now().isoformat()
            print(f"Payment successful for order {order_id}")
        elif payment_status in ["FAILED", "EXPIRED"]:
            update_data["status"] = "uploaded"
            print(f"Payment failed for order {order_id}")
        
        try:
            # Update the print job in database
            result = supabase.table("print_jobs").update(update_data).eq("order_id", order_id).execute()
            
            if not result.data:
                print(f"Order {order_id} not found in database")
                # Try to find by job ID if order_id contains job ID
                if "JOB_" in order_id:
                    job_id_part = order_id.split("_")[1]
                    result = supabase.table("print_jobs").update(update_data).eq("id", job_id_part).execute()
                    
                    if not result.data:
                        print(f"Job ID {job_id_part} also not found in database")
                        return jsonify({"error": "Order/Job not found"}), 404
                    else:
                        print(f"Found job by ID: {job_id_part}")
                else:
                    return jsonify({"error": "Order not found"}), 404
                
            print(f"Successfully updated order {order_id} with status {payment_status_lower}")
            
        except Exception as e:
            print(f"Error updating database for order {order_id}: {e}")
            return jsonify({"error": "Database update failed"}), 500
        
        return jsonify({"status": "success", "message": "Webhook processed successfully"})
    
    except Exception as e:
        print(f"Error processing webhook: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Webhook processing failed"}), 500

def verify_webhook_signature(webhook_data, signature):
    """Verify Cashfree webhook signature"""
    if not signature:
        print("No signature provided")
        return False
    
    try:
        # Convert webhook data to string in the exact format Cashfree expects
        # Cashfree webhook signature is computed on the raw request body
        webhook_body = request.get_data(as_text=True)
        
        if not webhook_body:
            print("Empty webhook body")
            return False
            
        print(f"Webhook body for signature: {webhook_body}")
        
        # Compute HMAC SHA256 signature
        computed_signature = hmac.new(
            key=WEBHOOK_SECRET.encode('utf-8'),
            msg=webhook_body.encode('utf-8'),
            digestmod=hashlib.sha256
        ).hexdigest()
        
        print(f"Computed signature: {computed_signature}")
        print(f"Received signature: {signature}")
        
        # Compare signatures
        result = hmac.compare_digest(computed_signature, signature)
        print(f"Signature verification result: {result}")
        return result
        
    except Exception as e:
        print(f"Error verifying signature: {e}")
        import traceback
        traceback.print_exc()
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