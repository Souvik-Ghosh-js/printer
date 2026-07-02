"""
Data + storage layer for the printer app.

Replaces Supabase with:
  - MySQL (via PyMySQL) for the print_jobs table
  - the local filesystem (UPLOAD_DIR) for PDF storage

Exposes small helpers so app.py / worker.py read cleanly instead of
sprinkling raw SQL everywhere.
"""
import os
import pymysql
from pymysql.cursors import DictCursor

# ---------------------------------------------------------------------------
# Config (hardcoded — kept inline by request)
# ---------------------------------------------------------------------------
DB_HOST = "127.0.0.1"      # MySQL runs on the same instance
DB_PORT = 3306
DB_NAME = "printer"
DB_USER = "printer_app"
DB_PASSWORD = "CHANGE_ME_strong_db_password"   # set this to match deploy script

# Folder on the instance's SSD where uploaded PDFs live
UPLOAD_DIR = "/var/lib/printer/uploads"

# Public base URL of this app (used to build file download URLs for the worker)
PUBLIC_BASE_URL = "https://print.mohiniprintshop.org"

# Shared secret guarding the /files download route
FILE_TOKEN = "CHANGE_ME_random_token"


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def get_conn():
    """A fresh autocommit connection. Cheap enough for this workload."""
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        cursorclass=DictCursor,
        autocommit=True,
        charset="utf8mb4",
    )


def query(sql, params=None):
    """Run a SELECT, return list of dict rows."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()
    finally:
        conn.close()


def execute(sql, params=None):
    """Run an INSERT/UPDATE/DELETE. Returns (rowcount, lastrowid)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.rowcount, cur.lastrowid
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# print_jobs helpers
# ---------------------------------------------------------------------------
_INSERT_COLS = [
    "customer_id", "file_url", "storage_key", "original_filename", "status",
    "total_pages", "sides", "orientation", "color_mode", "paper_size",
    "page_range", "price", "payment_status", "copies", "copy_number",
]


def insert_job(payload):
    """Insert one print_jobs row; returns the new row's id."""
    cols = [c for c in _INSERT_COLS if c in payload]
    placeholders = ", ".join(["%s"] * len(cols))
    sql = f"INSERT INTO print_jobs ({', '.join(cols)}) VALUES ({placeholders})"
    _, new_id = execute(sql, tuple(payload[c] for c in cols))
    return new_id


def get_job(job_id):
    rows = query("SELECT * FROM print_jobs WHERE id = %s", (job_id,))
    return rows[0] if rows else None


def get_jobs_by_status(status):
    return query("SELECT * FROM print_jobs WHERE status = %s", (status,))


def get_jobs_by_order(order_id):
    return query("SELECT * FROM print_jobs WHERE order_id = %s", (order_id,))


def count_by_filename_prefix(prefix):
    rows = query(
        "SELECT COUNT(*) AS n FROM print_jobs WHERE original_filename LIKE %s",
        (prefix + "%",),
    )
    return rows[0]["n"] if rows else 0


def delete_job(job_id):
    rowcount, _ = execute("DELETE FROM print_jobs WHERE id = %s", (job_id,))
    return rowcount


def update_job(job_id, fields):
    if not fields:
        return 0
    sets = ", ".join(f"{k} = %s" for k in fields)
    params = tuple(fields.values()) + (job_id,)
    rowcount, _ = execute(f"UPDATE print_jobs SET {sets} WHERE id = %s", params)
    return rowcount


def update_jobs_by_order(order_id, fields):
    if not fields:
        return 0
    sets = ", ".join(f"{k} = %s" for k in fields)
    params = tuple(fields.values()) + (order_id,)
    rowcount, _ = execute(
        f"UPDATE print_jobs SET {sets} WHERE order_id = %s", params
    )
    return rowcount


def update_jobs_by_customer_and_prefix(customer_id, filename_prefix, fields):
    """Mirror the old ilike(base%) bulk-update of all copies of a file."""
    if not fields:
        return 0
    sets = ", ".join(f"{k} = %s" for k in fields)
    params = tuple(fields.values()) + (customer_id, filename_prefix + "%")
    rowcount, _ = execute(
        f"UPDATE print_jobs SET {sets} "
        f"WHERE customer_id = %s AND original_filename LIKE %s",
        params,
    )
    return rowcount


# ---------------------------------------------------------------------------
# File storage (local disk on the instance's SSD)
# ---------------------------------------------------------------------------
def storage_save(filename, data: bytes):
    """Write bytes to UPLOAD_DIR/filename. Returns the storage key (filename)."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, filename)
    with open(path, "wb") as f:
        f.write(data)
    return filename


def storage_path(filename):
    return os.path.join(UPLOAD_DIR, filename)


def storage_remove(filename):
    """Delete a stored file; ignore if already gone."""
    try:
        os.remove(os.path.join(UPLOAD_DIR, filename))
        return True
    except FileNotFoundError:
        return False


def public_url(storage_key):
    """Build the URL the worker uses to download a file."""
    return f"{PUBLIC_BASE_URL}/files/{storage_key}?token={FILE_TOKEN}"
