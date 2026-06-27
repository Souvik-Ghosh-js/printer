from flask import Flask, request, jsonify, render_template, send_file
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
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

import db

# --- Cashfree Config ---
CASHFREE_APP_ID = "1081246de8c8aebb039c1d380b76421801"
CASHFREE_SECRET_KEY = "cfsk_ma_prod_6c4dd5ba946f5eb8edc06b90e80d8332_642d89ae"
CASHFREE_ENV = "production"
WEBHOOK_SECRET = "x191i9m9ymo4skygxh2z"

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


@app.route("/files/<path:storage_key>")
def serve_file(storage_key):
    """Serve a stored PDF to the shop-PC worker (token-guarded)."""
    if request.args.get("token") != db.FILE_TOKEN:
        return jsonify({"error": "forbidden"}), 403
    safe = secure_filename(storage_key)
    path = db.storage_path(safe)
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    return send_file(path)

def analyze_image_bw_percentage(image_array, threshold_fraction=0.70):
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
                black_percent, white_percent = analyze_image_bw_percentage(img_array, threshold_fraction=0.70)
                
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


@app.route("/delete-job/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    """Delete a single job copy"""
    try:
        print(f"🗑️  Deleting job: {job_id}")
        
        # Get job details
        job = db.get_job(job_id)

        if not job:
            print(f"❌ Job {job_id} not found in database")
            return jsonify({"error": "Job not found"}), 404

        # Check if this is the last copy of the file
        base_name = job.get("original_filename", "").split("_copy")[0]

        # Count remaining copies
        remaining_copies = db.count_by_filename_prefix(base_name)

        # Delete from database
        deleted = db.delete_job(job_id)

        if deleted:
            print(f"✅ Successfully deleted job {job_id}")

            # If this was the last copy, delete the stored file
            if remaining_copies <= 1:
                storage_key = job.get("storage_key")
                if storage_key:
                    if db.storage_remove(storage_key):
                        print(f"✅ Also deleted file from storage")

            return jsonify({"message": "Job deleted successfully"})
        else:
            return jsonify({"error": "Failed to delete job from database"}), 500
            
    except Exception as e:
        print(f"❌ Error deleting job {job_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Deletion failed", "detail": str(e)}), 500

    
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

    # Get copies from request, default to 1
    try:
        copies = int(request.form.get("copies", 1))
        if copies < 1:
            copies = 1
        elif copies > 20:  # Limit to 20 copies maximum
            copies = 20
    except:
        copies = 1
    
    print(f"📁 Copies requested: {copies}")

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
        
        # Check for high black pages (>65%) - REJECT if found
        high_black_threshold = 65
        too_high_black_pages = []
        for page in analysis_result['page_analysis']:
            if page['black_percent'] > high_black_threshold:
                too_high_black_pages.append(page['page_number'])
        
        if too_high_black_pages:
            print(f"❌ PDF has pages with >{high_black_threshold}% black content")
            return jsonify({
                "error": f"PDF contains pages with very high black content (>65%). Cannot print.",
                "black_pages": too_high_black_pages,
                "black_percentages": [page['black_percent'] for page in analysis_result['page_analysis'] 
                                     if page['black_percent'] > high_black_threshold]
            }), 400
        
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
            'normal_pages': total_pages,
            'page_analysis': []
        }
        print(f"🔄 Fallback values - Total pages: {total_pages}, Price: ₹{price}")

    # --- Save to local storage (instance SSD) ---
    print("💾 Saving to local storage...")
    try:
        storage_key = db.storage_save(filename, filtered_pdf_bytes)
        file_url = db.public_url(storage_key)
        print(f"✅ Storage save successful: {file_url}")
    except Exception as e:
        print(f"❌ ERROR: Storage save failed: {e}")
        return jsonify({"error": "Storage save failed", "detail": str(e)}), 500

    # Gather settings & store in DB
    sides = request.form.get("sides", "single")
    paper_size = request.form.get("paper_size", "A4")
    
    print(f"📋 Final settings - Sides: {sides}, Paper size: {paper_size}")

    # Prepare base filename without extension
    original_base = original_filename.rsplit('.', 1)[0] if '.' in original_filename else original_filename
    original_ext = original_filename.rsplit('.', 1)[1] if '.' in original_filename else 'pdf'
    
    # Create multiple job entries for copies
    created_jobs = []
    for copy_num in range(1, copies + 1):
        job_payload = {
            "customer_id": customer_id,
            "file_url": file_url,  # Same URL for all copies
            "storage_key": storage_key,
            "original_filename": f"{original_base}_copy{copy_num}.{original_ext}",
            "status": "uploaded",
            "total_pages": total_pages,
            "sides": sides,
            "orientation": orientation,
            "color_mode": color_mode,
            "paper_size": paper_size,
            "page_range": page_range_str,
            "price": price,
            "payment_status": "pending",
            "copies": copies,
            "copy_number": copy_num,
        }

        print(f"💾 Storing copy {copy_num}/{copies} in database...")
        try:
            new_id = db.insert_job(job_payload)
            if new_id:
                created_jobs.append({
                    "job_id": new_id,
                    "copy_number": copy_num
                })
                print(f"✅ Database insert successful for copy {copy_num} - Job ID: {new_id}")
        except Exception as e:
            print(f"❌ ERROR: DB insert failed for copy {copy_num}: {e}")

    if created_jobs:
        # Calculate total price for all copies
        total_price = float(price) * copies
        
        response_data = {
            "job_ids": [job["job_id"] for job in created_jobs],
            "primary_job_id": created_jobs[0]["job_id"],  # First job ID for backward compatibility
            "copies": copies,
            "file_url": file_url,
            "total_pages": total_pages,
            "high_black_pages": analysis_result['high_black_pages'],
            "normal_pages": analysis_result['normal_pages'],
            "price_per_copy": price,
            "total_price": f"{total_price:.2f}"
        }
        
        print(f"📦 Response data: {response_data}")
        print(f"✅ Created {len(created_jobs)} job(s) for {copies} copy(ies)")
        print("=== UPLOAD PROCESS COMPLETED SUCCESSFULLY ===")
        return jsonify(response_data)
    else:
        print("❌ ERROR: Failed to create any jobs")
        return jsonify({"error": "Failed to create any jobs"}), 500
    
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
    
    # Ensure order_amount is a float with 2 decimal places
    try:
        order_amount_float = float(order_amount)
    except (ValueError, TypeError):
        order_amount_float = 0.0
    
    payload = {
        "order_id": order_id,
        "order_amount": round(order_amount_float, 2),
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
    
    print(f"💰 Creating Cashfree payment session:")
    print(f"   Order ID: {order_id}")
    print(f"   Order Amount: {order_amount_float}")
    print(f"   Customer ID: {customer_id}")
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        response_data = response.json()
        
        print(f"💰 Cashfree API response: {response.status_code}")
        
        if response.status_code == 200:
            return {
                "success": True,
                "payment_session_id": response_data.get("payment_session_id"),
                "order_id": order_id
            }
        else:
            print(f"❌ Cashfree API error: {response.status_code} - {response_data}")
            return {
                "success": False,
                "error": response_data.get("message", "Unknown error from Cashfree")
            }
    except Exception as e:
        print(f"❌ Cashfree API exception: {e}")
        import traceback
        traceback.print_exc()
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
        job = db.get_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404

        customer_id = job.get("customer_id", "customer_123")
        price = job.get("price", "0.00")  # Price for one copy
        copies = job.get("copies", 1)  # Get copies count from database
        
        # Convert price to float and calculate total
        try:
            price_float = float(price)
        except ValueError:
            price_float = 0.0
            
        # Calculate total amount = price per copy × number of copies
        total_amount = price_float * copies
        
        print(f"💰 Payment calculation: {price_float} × {copies} copies = {total_amount}")

    except Exception as e:
        print(f"Error fetching job: {e}")
        import traceback
        traceback.print_exc()
        customer_id = "customer_123"
        total_amount = 0.00
        copies = 1
        price = "0.00"
    
    # Generate a unique order ID
    order_id = f"JOB_{job_id}_{int(time.time())}"
    
    # Create payment session with Cashfree with TOTAL amount
    result = create_cashfree_payment_session(
        order_id=order_id,
        order_amount=total_amount,  # Pass TOTAL amount for all copies
        customer_id=customer_id,
        customer_email="customer@example.com",  # You should collect this from users
        customer_phone="9999999999"  # You should collect this from users
    )
    
    if result["success"]:
        # Update ALL copies with the same order ID
        try:
            # Get the original filename to find all copies
            original_filename = job.get("original_filename", "")
            if "_copy" in original_filename:
                base_filename = original_filename.split("_copy")[0]

                # Update all copies for this customer
                db.update_jobs_by_customer_and_prefix(
                    customer_id, base_filename,
                    {"order_id": order_id, "payment_status": "pending"},
                )
            else:
                # Update just this job
                db.update_job(
                    job_id,
                    {"order_id": order_id, "payment_status": "pending"},
                )
                    
        except Exception as e:
            print(f"Error updating jobs with order ID: {e}")
        
        return jsonify({
            "payment_session_id": result["payment_session_id"],
            "order_id": order_id,
            "price_per_copy": price,  # Price for one copy
            "copies": copies,  # Number of copies
            "total_amount": f"{total_amount:.2f}",  # Total amount for all copies
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
        
        # Update data for all related jobs
        update_data = {
            "payment_status": payment_status_lower,
            "transaction_id": transaction_id,
        }
        
        # If payment is successful, update job status for ALL copies
        if payment_status == "SUCCESS":
            update_data["status"] = "confirmed"
            update_data["paid_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"Payment successful for order {order_id}")
            
        elif payment_status in ["FAILED", "EXPIRED"]:
            update_data["status"] = "uploaded"
            print(f"Payment failed for order {order_id}")
        
        try:
            # First, get the primary job to find all related copies
            primary_jobs = db.get_jobs_by_order(order_id)

            if not primary_jobs:
                print(f"Order {order_id} not found in database")
                # Try to find by job ID if order_id contains job ID
                if "JOB_" in order_id:
                    job_id_part = order_id.split("_")[1]
                    job = db.get_job(job_id_part)
                    primary_jobs = [job] if job else []

                    if not primary_jobs:
                        print(f"Job ID {job_id_part} also not found in database")
                        return jsonify({"error": "Order/Job not found"}), 404

            if primary_jobs:
                primary_job = primary_jobs[0]
                customer_id = primary_job.get("customer_id")
                original_filename = primary_job.get("original_filename", "")

                # Find the base filename (without _copyX suffix)
                base_filename = original_filename.split("_copy")[0]

                # Update ALL copies for this customer with the same base filename
                updated = db.update_jobs_by_customer_and_prefix(
                    customer_id, base_filename, update_data
                )

                print(f"Updated {updated} copies for order {order_id}")

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
        rows = db.get_jobs_by_order(order_id)

        if rows:
            payment_status = rows[0].get("payment_status", "unknown")
            job_status = rows[0].get("status", "unknown")
            
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