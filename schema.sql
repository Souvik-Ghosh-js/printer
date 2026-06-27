-- ============================================================
--  Printer app schema for Lightsail instance MySQL
--  Run on the instance:  sudo mysql < schema.sql
--  Then create an app user (see deploy_setup.sh) — do NOT use root from the app.
-- ============================================================

CREATE DATABASE IF NOT EXISTS printer
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE printer;

-- Mirrors every column the Flask app + worker read/write.
-- id was a Supabase UUID; here we use an auto-increment BIGINT.
-- (file_url, order_id, etc. are all strings the app builds itself.)
CREATE TABLE IF NOT EXISTS print_jobs (
    id                 BIGINT       NOT NULL AUTO_INCREMENT,
    customer_id        VARCHAR(255),
    file_url           TEXT,                 -- public download URL (served by Flask)
    storage_key        VARCHAR(512),         -- filename on disk in UPLOAD_DIR
    original_filename  VARCHAR(512),
    status             VARCHAR(32)  NOT NULL DEFAULT 'uploaded',  -- uploaded|confirmed|printed
    total_pages        INT          DEFAULT 0,
    sides              VARCHAR(16)  DEFAULT 'single',
    orientation        VARCHAR(16)  DEFAULT 'portrait',
    color_mode         VARCHAR(16)  DEFAULT 'bw',
    paper_size         VARCHAR(16)  DEFAULT 'A4',
    page_range         VARCHAR(255),
    price              DECIMAL(10,2) DEFAULT 0.00,
    payment_status     VARCHAR(32)  NOT NULL DEFAULT 'pending',   -- pending|paid|failed|cancelled|expired
    copies             INT          DEFAULT 1,
    copy_number        INT          DEFAULT 1,
    order_id           VARCHAR(128),
    transaction_id     VARCHAR(128),
    paid_at            DATETIME,
    created_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_status (status),
    KEY idx_order_id (order_id),
    KEY idx_customer (customer_id),
    KEY idx_original_filename (original_filename)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
