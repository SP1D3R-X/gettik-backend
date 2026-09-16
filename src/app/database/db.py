import sqlite3
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
from typing import Optional, List, Dict, Any, Set, Tuple
from src.app.config import DATA_DIR, DEFAULT_DEVICE_DOWNLOADS_DIR

DB_PATH = DATA_DIR / "gettik.db"

def get_db_connection() -> sqlite3.Connection:
    """Return a configured SQLite connection with dict row access."""
    conn = sqlite3.connect(str(DB_PATH), timeout=60.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def _normalize_download_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = dict(row)
    # Ensure both snake_case and spec aliases are populated
    d["download_id"] = d.get("id")
    d["file_path"] = d.get("filepath", "")
    d["file_size"] = d.get("filesize", 0)
    return d

def _normalize_fb_job_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = dict(row)
    d["upload_job_id"] = d.get("job_id", "")
    d["local_file_path"] = d.get("file_path", "")
    d["upload_status"] = d.get("status", "ready")
    return d

def init_db():
    """Create all required tables, default indices, and run schema migrations."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 1. Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT,
                username TEXT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'user',
                plan TEXT DEFAULT 'Pro Plan',
                status TEXT DEFAULT 'ACTIVE',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_login_at DATETIME
            )
        """)
        user_cols = [c["name"] for c in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "name" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN name TEXT")
        if "password_hash" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN password_hash TEXT DEFAULT ''")
        if "updated_at" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN updated_at DATETIME")
        if "last_login_at" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN last_login_at DATETIME")
        if "temp_password_hash" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN temp_password_hash TEXT")
        if "temp_password_expiry" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN temp_password_expiry DATETIME")
        if "must_change_password" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER DEFAULT 0")

        # 1b. Licenses table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                serial_key TEXT UNIQUE NOT NULL,
                status TEXT DEFAULT 'ACTIVE',
                assigned_user_id TEXT,
                start_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                expiry_date DATETIME NOT NULL,
                max_devices INTEGER DEFAULT 1,
                duration_days INTEGER DEFAULT 30,
                issued_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        lic_cols = [c["name"] for c in conn.execute("PRAGMA table_info(licenses)").fetchall()]
        if "duration_days" not in lic_cols:
            cursor.execute("ALTER TABLE licenses ADD COLUMN duration_days INTEGER DEFAULT 30")
        if "issued_at" not in lic_cols:
            cursor.execute("ALTER TABLE licenses ADD COLUMN issued_at DATETIME")

        # 1c. Devices table (tracks authorized hardware fingerprints per license)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_fingerprint TEXT NOT NULL,
                user_id TEXT NOT NULL,
                license_id INTEGER NOT NULL,
                status TEXT DEFAULT 'ACTIVE',
                first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                app_version TEXT DEFAULT '2.2.0',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(license_id, device_fingerprint)
            )
        """)

        # 1d. Sessions table (tracks active authenticated user tokens)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                user_id TEXT NOT NULL,
                device_fingerprint TEXT NOT NULL,
                expires_at DATETIME NOT NULL,
                is_active BOOLEAN DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 1e. Dedicated Admins table (Role-Based Access Control: SUPER_ADMIN, ADMIN, SUPPORT)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'ADMIN',
                permissions TEXT DEFAULT '*',
                status TEXT DEFAULT 'ACTIVE',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_login_at DATETIME
            )
        """)

        # 1f. Dedicated Admin Sessions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                admin_id TEXT NOT NULL,
                expires_at DATETIME NOT NULL,
                is_active BOOLEAN DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 1g. System Audit Logs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id TEXT NOT NULL,
                actor_email TEXT NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT,
                target_id TEXT,
                details TEXT,
                ip_address TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 1h. System Settings table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                category TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_by TEXT
            )
        """)

        # 1i. License Requests table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS license_requests (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                message TEXT DEFAULT '',
                status TEXT DEFAULT 'PENDING',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                reviewed_at DATETIME,
                reviewed_by_admin_id TEXT,
                rejection_reason TEXT DEFAULT '',
                license_id INTEGER,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(license_id) REFERENCES licenses(id)
            )
        """)

        # 1j. Feedback table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                type TEXT NOT NULL,
                subject TEXT NOT NULL,
                message TEXT NOT NULL,
                rating INTEGER,
                status TEXT NOT NULL DEFAULT 'OPEN',
                priority TEXT NOT NULL DEFAULT 'MEDIUM',
                admin_response TEXT,
                assigned_admin_id TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                resolved_at DATETIME,
                resolved_by_admin_id TEXT,
                attachment_path TEXT,
                attachment_url TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(assigned_admin_id) REFERENCES admins(id),
                FOREIGN KEY(resolved_by_admin_id) REFERENCES admins(id)
            )
        """)

        # 1k. Feedback Audit Logs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS feedback_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feedback_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_email TEXT,
                admin_id TEXT,
                action TEXT NOT NULL,
                previous_value TEXT,
                new_value TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(feedback_id) REFERENCES feedback(id)
            )
        """)

        # Indices for Feedback
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_feedback_user_id ON feedback(user_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_feedback_priority ON feedback(priority);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_feedback_assigned_admin ON feedback(assigned_admin_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON feedback(created_at);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_feedback_audit_fid ON feedback_audit_logs(feedback_id);")

        # Seed Primary Super Admin: wak223360@gmail.com / waqas..7866
        cursor.execute("""
            INSERT INTO admins (id, name, email, password_hash, role, permissions, status, updated_at)
            VALUES ('admin_master', 'Waqas (Administrator)', 'wak223360@gmail.com', '$2b$12$7wpajDX9NLMLekjC0I9E0.DONO5DRzgx7yya1MGjUAm9Ai7icJNlC', 'SUPER_ADMIN', '*', 'ACTIVE', CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                email = excluded.email,
                name = excluded.name,
                password_hash = excluded.password_hash,
                role = excluded.role,
                status = excluded.status
        """)

        # Seed Secondary Super Admin for QA test suites: admin@gettik.app / Admin@Gettik2026!
        cursor.execute("""
            INSERT INTO admins (id, name, email, password_hash, role, permissions, status, updated_at)
            VALUES ('admin_qa_test', 'Master Administrator', 'admin@gettik.app', '$2b$12$nKlADV3oVkjWgmGII754X.aN77oKSlY4XYQ3uRFsyNZB/SEytj0P6', 'SUPER_ADMIN', '*', 'ACTIVE', CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                email = excluded.email,
                name = excluded.name,
                password_hash = excluded.password_hash,
                role = excluded.role,
                status = excluded.status
        """)

        # Primary User Account for Waqas: wak223360@gmail.com / waqas..7866
        cursor.execute("""
            INSERT INTO users (id, name, username, email, password_hash, role, plan, status, updated_at)
            VALUES ('user_wak', 'Waqas', 'waqas', 'wak223360@gmail.com', '$2b$12$7wpajDX9NLMLekjC0I9E0.DONO5DRzgx7yya1MGjUAm9Ai7icJNlC', 'user', 'Pro Plan', 'ACTIVE', CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                name = 'Waqas',
                username = excluded.username,
                email = excluded.email,
                password_hash = excluded.password_hash,
                role = excluded.role,
                status = excluded.status
        """)

        # Default Desktop Account: default_user
        cursor.execute("""
            INSERT INTO users (id, name, username, email, password_hash, role, plan, status, updated_at)
            VALUES ('default_user', 'Waqas', 'sparrow', 'user@gettik.app', '', 'user', 'Pro Plan', 'ACTIVE', CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                name = CASE WHEN users.name IS NULL OR users.name = '' OR users.name = 'User' THEN 'Waqas' ELSE users.name END,
                role = 'user',
                plan = 'Pro Plan',
                status = 'ACTIVE'
        """)

        # Clean up legacy dummy / demo records from prior runs
        cursor.execute("""
            DELETE FROM users 
            WHERE id = 'user_test' 
               OR email = 'user@example.com' 
               OR email = 'dummy@gettik.app'
        """)

        # Ensure all existing users have real non-dummy names
        cursor.execute("""
            UPDATE users 
            SET name = COALESCE(NULLIF(name, ''), NULLIF(username, ''), SUBSTR(email, 1, INSTR(email, '@') - 1))
            WHERE name IS NULL OR name = '' OR name = 'User'
        """)

        # Seed Active Licenses with real duration_days, issued_at, max_devices=1
        cursor.execute("""
            INSERT INTO licenses (serial_key, status, assigned_user_id, start_date, expiry_date, max_devices, duration_days, issued_at)
            VALUES ('GETTIK-WAK1-2026-X8K9-M4ZL', 'ACTIVE', 'user_wak', '2026-09-13 00:00:00', '2027-09-13 23:59:59', 1, 365, '2026-09-13 00:00:00')
            ON CONFLICT(serial_key) DO UPDATE SET
                duration_days = 365,
                issued_at = COALESCE(licenses.issued_at, '2026-09-13 00:00:00'),
                max_devices = 1
        """)
        cursor.execute("""
            INSERT INTO licenses (serial_key, status, assigned_user_id, start_date, expiry_date, max_devices, duration_days, issued_at)
            VALUES ('GETTIK-DFLT-2026-X8K9-M4ZL', 'ACTIVE', 'default_user', '2026-09-13 00:00:00', '2027-09-13 23:59:59', 1, 365, '2026-09-13 00:00:00')
            ON CONFLICT(serial_key) DO UPDATE SET
                duration_days = 365,
                issued_at = COALESCE(licenses.issued_at, '2026-09-13 00:00:00'),
                max_devices = 1
        """)
        cursor.execute("DELETE FROM licenses WHERE serial_key = 'GETTIK-PRO1-2026-X8K9-M4ZL' OR serial_key LIKE 'TEST-%' OR assigned_user_id IS NULL OR assigned_user_id = '' OR assigned_user_id NOT IN (SELECT id FROM users)")
        cursor.execute("DELETE FROM devices WHERE license_id NOT IN (SELECT id FROM licenses) OR user_id NOT IN (SELECT id FROM users)")
        cursor.execute("DELETE FROM sessions WHERE user_id NOT IN (SELECT id FROM users)")

        # Clean up legacy dummy / demo records from prior runs
        cursor.execute("DELETE FROM downloads WHERE title IN ('Video 1', 'Video 2', 'Video 3', 'Video 4', 'Video 5', 'Video 11', 'Video 12', 'Video 15', 'Video 17', 'Video 18') OR title LIKE '#rana%'")
        cursor.execute("DELETE FROM creators WHERE username LIKE '@creator_%' OR username LIKE 'creator_%' OR username LIKE 'seq_creator_%' OR username LIKE 'http%' OR username = 'shared_celebrity' OR username IN ('persisted_creator', 'lic_change_creator', 'secret_creator_a', 'spoofed_creator', 'resilient_creator', 'email_change_creator')")
        cursor.execute("DELETE FROM user_creators WHERE creator_id NOT IN (SELECT id FROM creators)")
        cursor.execute("DELETE FROM user_creator_settings WHERE creator_id NOT IN (SELECT id FROM creators)")
        cursor.execute("DELETE FROM creator_activities WHERE creator_username NOT IN (SELECT username FROM creators)")
        cursor.execute("DELETE FROM creator_progress WHERE creator_username NOT IN (SELECT username FROM creators)")
        cursor.execute("DELETE FROM download_batches WHERE creator_username NOT IN (SELECT username FROM creators)")
        cursor.execute("DELETE FROM batch_items WHERE batch_id NOT IN (SELECT batch_id FROM download_batches)")

        # Reconcile any remaining legacy "Video X" titles
        cursor.execute("""
            UPDATE downloads 
            SET title = CASE 
                WHEN description IS NOT NULL AND description != '' AND description NOT LIKE 'Video %' THEN description
                ELSE 'TikTok Video ' || video_id
            END
            WHERE title LIKE 'Video %'
        """)

        # Seed Default System Settings
        default_settings = [
            ("app_name", "Gettik Downloader Pro", "general"),
            ("app_version", "2.2.0", "general"),
            ("max_concurrent_downloads", "3", "downloads"),
            ("default_video_quality", "1080p", "downloads"),
            ("auto_verify_files", "true", "downloads"),
            ("default_download_dir", str(DEFAULT_DEVICE_DOWNLOADS_DIR), "storage"),
            ("cleanup_temp_files", "true", "storage"),
            ("min_free_disk_mb", "500", "storage"),
            ("default_max_devices", "1", "licensing"),
            ("default_license_validity_days", "365", "licensing"),
            ("session_timeout_days", "7", "security"),
            ("max_login_failed_attempts", "5", "security")
        ]
        for s_key, s_val, s_cat in default_settings:
            cursor.execute("""
                INSERT OR IGNORE INTO system_settings (key, value, category, updated_at, updated_by)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, 'system')
            """, (s_key, s_val, s_cat))

        # Migrate default_max_devices setting if currently set to legacy '2'
        cursor.execute("UPDATE system_settings SET value = '1' WHERE key = 'default_max_devices' AND value = '2'")

        # 2. Downloads table (Account-scoped via UNIQUE(user_id, video_id))
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT DEFAULT 'default_user',
                creator_id TEXT,
                video_id TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                description TEXT,
                creator TEXT,
                creator_avatar TEXT,
                thumbnail TEXT,
                duration INTEGER DEFAULT 0,
                filesize INTEGER DEFAULT 0,
                format TEXT DEFAULT 'mp4',
                quality TEXT DEFAULT '1080p',
                filepath TEXT,
                file_name TEXT,
                status TEXT DEFAULT 'completed',
                error_message TEXT,
                download_batch TEXT,
                facebook_upload_status TEXT DEFAULT 'ready',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                downloaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME,
                UNIQUE(user_id, video_id)
            )
        """)

        # Migration: Check if downloads table has legacy global UNIQUE(video_id) constraint
        dl_table_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='downloads'").fetchone()
        if dl_table_sql and "video_id TEXT UNIQUE" in dl_table_sql[0]:
            cursor.execute("""
                CREATE TABLE downloads_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT DEFAULT 'default_user',
                    creator_id TEXT,
                    video_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT,
                    description TEXT,
                    creator TEXT,
                    creator_avatar TEXT,
                    thumbnail TEXT,
                    duration INTEGER DEFAULT 0,
                    filesize INTEGER DEFAULT 0,
                    format TEXT DEFAULT 'mp4',
                    quality TEXT DEFAULT '1080p',
                    filepath TEXT,
                    file_name TEXT,
                    status TEXT DEFAULT 'completed',
                    error_message TEXT,
                    download_batch TEXT,
                    facebook_upload_status TEXT DEFAULT 'ready',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    downloaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    completed_at DATETIME,
                    UNIQUE(user_id, video_id)
                )
            """)
            cursor.execute("""
                INSERT OR IGNORE INTO downloads_v2 (
                    id, user_id, creator_id, video_id, url, title, description, creator,
                    creator_avatar, thumbnail, duration, filesize, format, quality,
                    filepath, file_name, status, error_message, download_batch,
                    facebook_upload_status, created_at, downloaded_at, completed_at
                )
                SELECT
                    id, COALESCE(user_id, 'default_user'), creator_id, video_id, url, title, description, creator,
                    creator_avatar, thumbnail, duration, filesize, format, quality,
                    filepath, file_name, status, error_message, download_batch,
                    facebook_upload_status, created_at, downloaded_at, completed_at
                FROM downloads
            """)
            cursor.execute("DROP TABLE downloads")
            cursor.execute("ALTER TABLE downloads_v2 RENAME TO downloads")

        # Migrations for existing downloads columns
        existing_cols = [c["name"] for c in conn.execute("PRAGMA table_info(downloads)").fetchall()]
        if "user_id" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN user_id TEXT DEFAULT 'default_user'")
        if "creator_id" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN creator_id TEXT")
        if "description" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN description TEXT")
        if "file_name" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN file_name TEXT")
        if "download_batch" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN download_batch TEXT")
        if "facebook_upload_status" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN facebook_upload_status TEXT DEFAULT 'ready'")
        if "downloaded_at" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN downloaded_at DATETIME")
        if "completed_at" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN completed_at DATETIME")
        if "worker_id" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN worker_id TEXT")
        if "attempt_number" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN attempt_number INTEGER DEFAULT 1")
        if "provider" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN provider TEXT DEFAULT ''")
        if "progress" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN progress REAL DEFAULT 0.0")
        if "started_at" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN started_at DATETIME")
        if "updated_at" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN updated_at DATETIME")
        if "retry_count" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN retry_count INTEGER DEFAULT 0")
        if "retry_history" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN retry_history TEXT DEFAULT '[]'")
        if "queue_position" not in existing_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN queue_position INTEGER DEFAULT 0")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_downloads_user_status ON downloads(user_id, status);")

        # 3. Creators table (Global TikTok Creator Profiles registry)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS creators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT DEFAULT 'default_user',
                username TEXT UNIQUE NOT NULL,
                nickname TEXT,
                profile_url TEXT DEFAULT '',
                avatar TEXT,
                avatar_url TEXT DEFAULT '',
                bio TEXT,
                metadata TEXT DEFAULT '{}',
                follower_count INTEGER DEFAULT 0,
                video_count INTEGER DEFAULT 0,
                download_limit INTEGER DEFAULT 3,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        creator_cols = [c["name"] for c in conn.execute("PRAGMA table_info(creators)").fetchall()]
        if "user_id" not in creator_cols:
            cursor.execute("ALTER TABLE creators ADD COLUMN user_id TEXT DEFAULT 'default_user'")
        if "profile_url" not in creator_cols:
            cursor.execute("ALTER TABLE creators ADD COLUMN profile_url TEXT DEFAULT ''")
        if "avatar_url" not in creator_cols:
            cursor.execute("ALTER TABLE creators ADD COLUMN avatar_url TEXT DEFAULT ''")
        if "metadata" not in creator_cols:
            cursor.execute("ALTER TABLE creators ADD COLUMN metadata TEXT DEFAULT '{}'")
        if "updated_at" not in creator_cols:
            cursor.execute("ALTER TABLE creators ADD COLUMN updated_at DATETIME")

        # 3b. User Creators Relational Table (Account-owned data mapping)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_creators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                creator_id INTEGER NOT NULL,
                default_download_limit INTEGER DEFAULT 3,
                preferred_quality TEXT DEFAULT '1080p',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, creator_id),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(creator_id) REFERENCES creators(id)
            )
        """)

        # 3c. User Creator Settings Table (Independent per-account configuration)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_creator_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                creator_id INTEGER NOT NULL,
                default_download_limit INTEGER DEFAULT 3,
                preferred_quality TEXT DEFAULT '1080p',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, creator_id),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(creator_id) REFERENCES creators(id)
            )
        """)

        # Migrate existing creators into user_creators and user_creator_settings
        cursor.execute("""
            INSERT OR IGNORE INTO user_creators (user_id, creator_id, default_download_limit, preferred_quality, created_at, updated_at)
            SELECT COALESCE(user_id, 'default_user'), id, COALESCE(download_limit, 3), '1080p', created_at, CURRENT_TIMESTAMP
            FROM creators
            WHERE id IS NOT NULL
        """)
        cursor.execute("""
            INSERT OR IGNORE INTO user_creator_settings (user_id, creator_id, default_download_limit, preferred_quality, created_at, updated_at)
            SELECT COALESCE(user_id, 'default_user'), id, COALESCE(download_limit, 3), '1080p', created_at, CURRENT_TIMESTAMP
            FROM creators
            WHERE id IS NOT NULL
        """)
        cursor.execute("UPDATE creators SET download_limit = 3 WHERE download_limit = 50 OR download_limit IS NULL")
        cursor.execute("UPDATE user_creators SET default_download_limit = 3 WHERE default_download_limit = 50 OR default_download_limit IS NULL")
        cursor.execute("UPDATE user_creator_settings SET default_download_limit = 3 WHERE default_download_limit = 50 OR default_download_limit IS NULL")

        # 4. Creator Videos relational table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS creator_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_username TEXT NOT NULL,
                video_id TEXT UNIQUE NOT NULL,
                title TEXT,
                description TEXT,
                duration INTEGER DEFAULT 0,
                thumbnail TEXT,
                video_url TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 5. Creator Progress table for persistent resume (Account-scoped)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS creator_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT DEFAULT 'default_user',
                creator_username TEXT NOT NULL,
                status TEXT DEFAULT 'idle',
                total_found INTEGER DEFAULT 0,
                download_limit INTEGER DEFAULT 3,
                completed_count INTEGER DEFAULT 0,
                remaining_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                current_video_id TEXT,
                current_video_title TEXT,
                current_speed TEXT DEFAULT '0 KB/s',
                current_percent REAL DEFAULT 0,
                last_video_index INTEGER DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, creator_username)
            )
        """)
        cursor.execute("UPDATE creator_progress SET download_limit = 3 WHERE download_limit = 50 OR download_limit IS NULL")
        prog_cols = [c["name"] for c in conn.execute("PRAGMA table_info(creator_progress)").fetchall()]
        if "user_id" not in prog_cols:
            cursor.execute("""
                CREATE TABLE creator_progress_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT DEFAULT 'default_user',
                    creator_username TEXT NOT NULL,
                    status TEXT DEFAULT 'idle',
                    total_found INTEGER DEFAULT 0,
                    download_limit INTEGER DEFAULT 3,
                    completed_count INTEGER DEFAULT 0,
                    remaining_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    skipped_count INTEGER DEFAULT 0,
                    current_video_id TEXT,
                    current_video_title TEXT,
                    current_speed TEXT DEFAULT '0 KB/s',
                    current_percent REAL DEFAULT 0,
                    last_video_index INTEGER DEFAULT 0,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, creator_username)
                )
            """)
            cursor.execute("""
                INSERT OR IGNORE INTO creator_progress_v2 (
                    id, user_id, creator_username, status, total_found, download_limit,
                    completed_count, remaining_count, failed_count, skipped_count,
                    current_video_id, current_video_title, current_speed, current_percent,
                    last_video_index, updated_at
                )
                SELECT
                    id, 'default_user', creator_username, status, total_found, download_limit,
                    completed_count, remaining_count, failed_count, skipped_count,
                    current_video_id, current_video_title, current_speed, current_percent,
                    last_video_index, updated_at
                FROM creator_progress
            """)
            cursor.execute("DROP TABLE creator_progress")
            cursor.execute("ALTER TABLE creator_progress_v2 RENAME TO creator_progress")

        # 6. Download Batches table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS download_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT UNIQUE NOT NULL,
                user_id TEXT DEFAULT 'default_user',
                creator_username TEXT,
                target_limit INTEGER DEFAULT 50,
                completed_count INTEGER DEFAULT 0,
                remaining_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 7. Batch Items table with individual states
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS batch_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                title TEXT,
                status TEXT DEFAULT 'Queued',
                file_path TEXT,
                file_size INTEGER DEFAULT 0,
                error_message TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(batch_id, video_id)
            )
        """)

        # 8. Creator Activity List table (Account-scoped)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS creator_activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT DEFAULT 'default_user',
                creator_username TEXT NOT NULL,
                video_id TEXT,
                title TEXT,
                status TEXT DEFAULT 'downloaded',
                message TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        act_cols = [c["name"] for c in conn.execute("PRAGMA table_info(creator_activities)").fetchall()]
        if "user_id" not in act_cols:
            cursor.execute("ALTER TABLE creator_activities ADD COLUMN user_id TEXT DEFAULT 'default_user'")

        # 9. Facebook Upload Jobs table for automated title transfer & upload workflow
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS facebook_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE NOT NULL,
                upload_job_id TEXT,
                user_id TEXT DEFAULT 'default_user',
                download_id INTEGER,
                video_id TEXT NOT NULL,
                title TEXT NOT NULL,
                file_path TEXT NOT NULL,
                local_file_path TEXT,
                creator TEXT,
                description TEXT,
                metadata TEXT,
                target_platform TEXT DEFAULT 'facebook_marketplace',
                status TEXT DEFAULT 'ready',
                upload_status TEXT DEFAULT 'ready',
                field_validated BOOLEAN DEFAULT 0,
                error_message TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migrations for existing facebook_jobs table
        fb_cols = [c["name"] for c in conn.execute("PRAGMA table_info(facebook_jobs)").fetchall()]
        if "user_id" not in fb_cols:
            cursor.execute("ALTER TABLE facebook_jobs ADD COLUMN user_id TEXT DEFAULT 'default_user'")
        if "upload_job_id" not in fb_cols:
            cursor.execute("ALTER TABLE facebook_jobs ADD COLUMN upload_job_id TEXT")
        if "local_file_path" not in fb_cols:
            cursor.execute("ALTER TABLE facebook_jobs ADD COLUMN local_file_path TEXT")
        if "metadata" not in fb_cols:
            cursor.execute("ALTER TABLE facebook_jobs ADD COLUMN metadata TEXT")
        if "upload_status" not in fb_cols:
            cursor.execute("ALTER TABLE facebook_jobs ADD COLUMN upload_status TEXT DEFAULT 'ready'")

        # 10. Indexes for Account Ownership and Fast Relational Queries
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_creators_user ON user_creators(user_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_creators_user_creator ON user_creators(user_id, creator_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_creator_settings_user ON user_creator_settings(user_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_creator_settings_user_creator ON user_creator_settings(user_id, creator_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_creator_progress_user ON creator_progress(user_id, creator_username);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_creator_activities_user ON creator_activities(user_id, creator_username);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_downloads_user_id ON downloads(user_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_downloads_user_video ON downloads(user_id, video_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_download_batches_user ON download_batches(user_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_facebook_jobs_user ON facebook_jobs(user_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_licenses_assigned ON licenses(assigned_user_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_creators_username ON creators(username);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_creator_videos_creator ON creator_videos(creator_username);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_creator_videos_created ON creator_videos(created_at);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_creator_videos_vid ON creator_videos(video_id);")

        # 11. App Settings key-value table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Seed default settings if not exists (using device-accessible ~/Downloads/Gettik/)
        default_settings = {
            "downloadDir": str(DEFAULT_DEVICE_DOWNLOADS_DIR),
            "startupBehavior": "Normal window",
            "autoClipboard": "true",
            "preferredFormat": "MP4",
            "videoQuality": "Highest Available",
            "concurrentThreads": "3",
            "theme": "dark",
            "language": "EN"
        }
        for k, v in default_settings.items():
            cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

        conn.commit()

# --- Helper Functions ---

def is_video_downloaded(video_id: str, user_id: str = "default_user") -> bool:
    """
    Check if a video ID is recorded as completed AND the physical file exists on device storage.
    If database says completed but file is missing on disk, reconciles record to 'missing'
    and returns False so user can re-download.
    """
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id, filepath, status FROM downloads WHERE video_id = ? AND user_id = ?",
            (video_id, user_id)
        ).fetchone()
        if not row:
            return False

        fp = row["filepath"]
        if row["status"] == "completed" and fp:
            path = Path(fp).expanduser().resolve()
            if path.exists() and path.is_file() and path.stat().st_size > 0:
                return True
            else:
                # File is missing on device! Reconcile database!
                conn.execute("UPDATE downloads SET status = 'missing' WHERE id = ?", (row["id"],))
                conn.commit()
                return False
        return False

def get_downloaded_video_ids(video_ids: List[str], user_id: str = "default_user") -> Set[str]:
    """
    Batch check which video IDs from a list are already recorded as completed
    AND physically present on device storage.
    Reconciles missing files and returns the set of valid completed video IDs.
    Completely eliminates N+1 database queries.
    """
    if not video_ids:
        return set()

    completed_ids: Set[str] = set()
    missing_ids: List[int] = []

    # Batch query using chunks to stay well within SQLite variable limits
    chunk_size = 400
    with get_db_connection() as conn:
        for i in range(0, len(video_ids), chunk_size):
            chunk = video_ids[i:i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            query = f"SELECT id, video_id, filepath, status FROM downloads WHERE user_id = ? AND video_id IN ({placeholders})"
            rows = conn.execute(query, [user_id] + chunk).fetchall()

            for row in rows:
                if row["status"] == "completed":
                    fp = row["filepath"]
                    if fp:
                        path = Path(fp).expanduser().resolve()
                        if path.exists() and path.is_file() and path.stat().st_size > 0:
                            completed_ids.add(row["video_id"])
                            continue
                    # Missing physical file
                    missing_ids.append(row["id"])

        if missing_ids:
            for i in range(0, len(missing_ids), chunk_size):
                m_chunk = missing_ids[i:i + chunk_size]
                m_ph = ",".join("?" for _ in m_chunk)
                conn.execute(f"UPDATE downloads SET status = 'missing' WHERE id IN ({m_ph})", m_chunk)
            conn.commit()

    return completed_ids

def record_download(
    video_id: str,
    url: str,
    title: str,
    creator: str,
    creator_avatar: str = "",
    thumbnail: str = "",
    duration: int = 0,
    filesize: int = 0,
    format: str = "mp4",
    quality: str = "1080p",
    filepath: str = "",
    file_name: str = "",
    status: str = "completed",
    error_message: str = "",
    download_batch: str = "",
    facebook_upload_status: str = "ready",
    user_id: str = "default_user",
    creator_id: str = "",
    description: str = "",
    completed_at: Optional[str] = None
):
    """Insert or update a verified download record pointing to physical device storage."""
    if not file_name and filepath:
        file_name = Path(filepath).name

    completed_ts = "CURRENT_TIMESTAMP" if status == "completed" else "NULL"

    with get_db_connection() as conn:
        conn.execute(f"""
            INSERT INTO downloads (
                user_id, creator_id, video_id, url, title, description, creator, creator_avatar, thumbnail,
                duration, filesize, format, quality, filepath, file_name, status,
                error_message, download_batch, facebook_upload_status, created_at, downloaded_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, {completed_ts})
            ON CONFLICT(user_id, video_id) DO UPDATE SET
                url = excluded.url,
                title = excluded.title,
                description = excluded.description,
                creator = excluded.creator,
                creator_avatar = excluded.creator_avatar,
                thumbnail = excluded.thumbnail,
                duration = excluded.duration,
                filesize = excluded.filesize,
                format = excluded.format,
                quality = excluded.quality,
                filepath = excluded.filepath,
                file_name = excluded.file_name,
                status = excluded.status,
                error_message = excluded.error_message,
                download_batch = excluded.download_batch,
                facebook_upload_status = excluded.facebook_upload_status,
                downloaded_at = CURRENT_TIMESTAMP,
                completed_at = CASE WHEN excluded.status = 'completed' THEN CURRENT_TIMESTAMP ELSE completed_at END
        """, (
            user_id, creator_id, video_id, url, title, description, creator, creator_avatar, thumbnail,
            duration, filesize, format, quality, filepath, file_name,
            status, error_message, download_batch, facebook_upload_status
        ))
        conn.commit()
        return get_download_by_video_id(video_id, user_id=user_id)

def get_download_by_id(download_id: int, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieve download record by numeric ID."""
    with get_db_connection() as conn:
        if user_id:
            row = conn.execute("SELECT * FROM downloads WHERE id = ? AND user_id = ?", (download_id, user_id)).fetchone()
        else:
            row = conn.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()
        return _normalize_download_dict(row)

def get_download_by_video_id(video_id: str, user_id: str = "default_user") -> Optional[Dict[str, Any]]:
    """Retrieve download record by video ID."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM downloads WHERE video_id = ? AND user_id = ?", (video_id, user_id)).fetchone()
        return _normalize_download_dict(row)

def reconcile_all_downloads(user_id: str = "default_user") -> Dict[str, int]:
    """
    Verify all download records against actual physical device files.
    Reconciles status to 'missing' if file disappeared from disk, or 'completed' if file exists.
    """
    missing_count = 0
    valid_count = 0
    with get_db_connection() as conn:
        rows = conn.execute("SELECT id, filepath, status FROM downloads WHERE user_id = ?", (user_id,)).fetchall()
        for r in rows:
            fp = r["filepath"]
            status = r["status"]
            is_valid = False
            if fp:
                p = Path(fp).expanduser().resolve()
                is_valid = p.exists() and p.is_file() and p.stat().st_size > 0

            if status == "completed" and not is_valid:
                conn.execute("UPDATE downloads SET status = 'missing' WHERE id = ?", (r["id"],))
                missing_count += 1
            elif status == "missing" and is_valid:
                conn.execute("UPDATE downloads SET status = 'completed' WHERE id = ?", (r["id"],))
                valid_count += 1
        conn.commit()
    return {"missing_reconciled": missing_count, "valid_count": valid_count}

def get_recent_downloads(limit: int = 50, user_id: str = "default_user") -> List[Dict[str, Any]]:
    """Get list of downloads with live physical file verification."""
    reconcile_all_downloads(user_id)
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM downloads WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
        ).fetchall()
        return [_normalize_download_dict(r) for r in rows if r]

def delete_download(download_id: int, delete_file: bool = False, user_id: str = "default_user"):
    """Delete a download record by ID, optionally deleting the physical file."""
    with get_db_connection() as conn:
        if delete_file:
            row = conn.execute("SELECT filepath FROM downloads WHERE id = ? AND user_id = ?", (download_id, user_id)).fetchone()
            if row and row["filepath"]:
                try:
                    p = Path(row["filepath"]).expanduser().resolve()
                    if p.exists() and p.is_file():
                        p.unlink()
                except Exception:
                    pass
        conn.execute("DELETE FROM downloads WHERE id = ? AND user_id = ?", (download_id, user_id))
        conn.commit()

# --- Creator & Batch Relational Functions ---

def save_creator(
    username: str,
    nickname: str = "",
    avatar: str = "",
    avatar_url: str = "",
    bio: str = "",
    follower_count: int = 0,
    video_count: int = 0,
    download_limit: int = 3,
    user_id: str = "default_user",
    preferred_quality: str = "1080p"
) -> Dict[str, Any]:
    """Save or update a creator record and establish user_creators ownership for user_id."""
    clean_username = username.lstrip("@").strip()
    profile_url = f"https://www.tiktok.com/@{clean_username}"
    real_avatar = avatar_url or avatar
    with get_db_connection() as conn:
        # 1. Global creator registry record
        conn.execute("""
            INSERT INTO creators (
                user_id, username, nickname, profile_url, avatar, avatar_url, bio, follower_count, video_count, download_limit, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(username) DO UPDATE SET
                nickname = CASE WHEN excluded.nickname != '' THEN excluded.nickname ELSE creators.nickname END,
                profile_url = CASE WHEN excluded.profile_url != '' THEN excluded.profile_url ELSE creators.profile_url END,
                avatar = CASE WHEN excluded.avatar != '' THEN excluded.avatar ELSE creators.avatar END,
                avatar_url = CASE WHEN excluded.avatar_url != '' THEN excluded.avatar_url ELSE creators.avatar_url END,
                bio = CASE WHEN excluded.bio != '' THEN excluded.bio ELSE creators.bio END,
                follower_count = CASE WHEN excluded.follower_count > 0 THEN excluded.follower_count ELSE creators.follower_count END,
                video_count = CASE WHEN excluded.video_count > 0 THEN excluded.video_count ELSE creators.video_count END,
                download_limit = excluded.download_limit,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, clean_username, nickname, profile_url, real_avatar, real_avatar, bio, follower_count, video_count, download_limit))

        creator_row = conn.execute("SELECT * FROM creators WHERE username = ?", (clean_username,)).fetchone()
        creator_id = creator_row["id"]

        # 2. Account-scoped user_creators relationship
        if user_id:
            conn.execute("""
                INSERT INTO user_creators (user_id, creator_id, default_download_limit, preferred_quality, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, creator_id) DO UPDATE SET
                    default_download_limit = excluded.default_download_limit,
                    preferred_quality = excluded.preferred_quality,
                    updated_at = CURRENT_TIMESTAMP
            """, (user_id, creator_id, download_limit, preferred_quality))

            conn.execute("""
                INSERT INTO user_creator_settings (user_id, creator_id, default_download_limit, preferred_quality, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, creator_id) DO UPDATE SET
                    default_download_limit = excluded.default_download_limit,
                    preferred_quality = excluded.preferred_quality,
                    updated_at = CURRENT_TIMESTAMP
            """, (user_id, creator_id, download_limit, preferred_quality))

        conn.commit()
        res = dict(creator_row)
        res["download_limit"] = download_limit
        res["preferred_quality"] = preferred_quality
        res["user_id"] = user_id
        return res

def get_saved_creators(user_id: str = "default_user") -> List[Dict[str, Any]]:
    """Retrieve all saved creators for user through user_creators relational mapping with real download counts."""
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT 
                c.id,
                c.username,
                c.nickname,
                c.profile_url,
                COALESCE(NULLIF(c.avatar_url, ''), c.avatar, '') AS avatar_url,
                COALESCE(NULLIF(c.avatar_url, ''), c.avatar, '') AS avatar,
                c.bio,
                c.follower_count,
                COALESCE(c.video_count, 0) AS total_videos,
                COALESCE(c.video_count, 0) AS video_count,
                COALESCE(ucs.default_download_limit, uc.default_download_limit, c.download_limit, 3) AS download_limit,
                COALESCE(ucs.preferred_quality, uc.preferred_quality, '1080p') AS preferred_quality,
                uc.user_id,
                uc.created_at AS saved_at,
                (
                    SELECT COUNT(*)
                    FROM downloads d
                    WHERE d.user_id = uc.user_id
                      AND d.status = 'completed'
                      AND (
                        d.creator_id = CAST(c.id AS TEXT)
                        OR LOWER(REPLACE(REPLACE(COALESCE(d.creator, ''), '@', ''), ' ', '')) = LOWER(REPLACE(c.username, '@', ''))
                      )
                ) AS downloaded_count
            FROM user_creators uc
            JOIN creators c ON uc.creator_id = c.id
            LEFT JOIN user_creator_settings ucs ON (ucs.user_id = uc.user_id AND ucs.creator_id = uc.creator_id)
            WHERE uc.user_id = ?
            ORDER BY uc.id DESC
        """, (user_id,)).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            tot = d.get("total_videos") or d.get("video_count") or 0
            dl = d.get("downloaded_count") or 0
            rem = max(0, tot - dl)
            d["total_videos"] = tot
            d["downloaded"] = dl
            d["downloaded_videos"] = dl
            d["downloaded_count"] = dl
            d["remaining"] = rem
            d["remaining_videos"] = rem
            d["remaining_count"] = rem
            results.append(d)
        return results

def get_creator_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Retrieve global creator record by clean username."""
    clean = username.lstrip("@").strip()
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM creators WHERE username = ?", (clean,)).fetchone()
        return dict(row) if row else None

def get_user_creator(user_id: str, creator_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve creator record if owned by user (IDOR protection) with real download counts."""
    with get_db_connection() as conn:
        row = conn.execute("""
            SELECT 
                c.id,
                c.username,
                c.nickname,
                c.profile_url,
                COALESCE(NULLIF(c.avatar_url, ''), c.avatar, '') AS avatar_url,
                COALESCE(NULLIF(c.avatar_url, ''), c.avatar, '') AS avatar,
                c.bio,
                c.follower_count,
                COALESCE(c.video_count, 0) AS total_videos,
                COALESCE(c.video_count, 0) AS video_count,
                COALESCE(ucs.default_download_limit, uc.default_download_limit, c.download_limit, 3) AS download_limit,
                COALESCE(ucs.preferred_quality, uc.preferred_quality, '1080p') AS preferred_quality,
                uc.user_id,
                uc.created_at AS saved_at,
                (
                    SELECT COUNT(*)
                    FROM downloads d
                    WHERE d.user_id = uc.user_id
                      AND d.status = 'completed'
                      AND (
                        d.creator_id = CAST(c.id AS TEXT)
                        OR LOWER(REPLACE(REPLACE(COALESCE(d.creator, ''), '@', ''), ' ', '')) = LOWER(REPLACE(c.username, '@', ''))
                      )
                ) AS downloaded_count
            FROM user_creators uc
            JOIN creators c ON uc.creator_id = c.id
            LEFT JOIN user_creator_settings ucs ON (ucs.user_id = uc.user_id AND ucs.creator_id = uc.creator_id)
            WHERE uc.user_id = ? AND uc.creator_id = ?
        """, (user_id, creator_id)).fetchone()
        if not row:
            return None
        d = dict(row)
        tot = d.get("total_videos") or d.get("video_count") or 0
        dl = d.get("downloaded_count") or 0
        rem = max(0, tot - dl)
        d["total_videos"] = tot
        d["downloaded"] = dl
        d["downloaded_videos"] = dl
        d["downloaded_count"] = dl
        d["remaining"] = rem
        d["remaining_videos"] = rem
        d["remaining_count"] = rem
        return d

def delete_user_creator(user_id: str, creator_id: int) -> bool:
    """Safely unlink creator from user account without deleting the global creator record."""
    with get_db_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM user_creators WHERE user_id = ? AND creator_id = ?",
            (user_id, creator_id)
        )
        conn.execute(
            "DELETE FROM user_creator_settings WHERE user_id = ? AND creator_id = ?",
            (user_id, creator_id)
        )
        conn.commit()
        return cursor.rowcount > 0

def get_user_creator_settings(user_id: str, creator_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve creator settings (default download limit, preferred quality) for a specific user and creator."""
    with get_db_connection() as conn:
        row = conn.execute("""
            SELECT creator_id, default_download_limit, preferred_quality, created_at, updated_at
            FROM user_creator_settings
            WHERE user_id = ? AND creator_id = ?
        """, (user_id, creator_id)).fetchone()
        if row:
            return dict(row)
        
        # Fallback to user_creators record
        uc_row = conn.execute("""
            SELECT creator_id, default_download_limit, preferred_quality, created_at, updated_at
            FROM user_creators
            WHERE user_id = ? AND creator_id = ?
        """, (user_id, creator_id)).fetchone()
        if uc_row:
            return dict(uc_row)

        # Fallback to base creator record if creator exists
        c_row = conn.execute("""
            SELECT id AS creator_id, download_limit AS default_download_limit, '1080p' AS preferred_quality
            FROM creators
            WHERE id = ?
        """, (creator_id,)).fetchone()
        return dict(c_row) if c_row else None

def update_user_creator_settings(
    user_id: str,
    creator_id: int,
    download_limit: Optional[int] = None,
    preferred_quality: Optional[str] = None,
    auto_download_new: Optional[bool] = None,
    default_download_limit: Optional[int] = None
) -> bool:
    """Update account-specific settings for a saved creator."""
    if default_download_limit is not None and download_limit is None:
        download_limit = default_download_limit

    with get_db_connection() as conn:
        existing = conn.execute(
            "SELECT * FROM user_creators WHERE user_id = ? AND creator_id = ?",
            (user_id, creator_id)
        ).fetchone()
        if not existing:
            c = conn.execute("SELECT * FROM creators WHERE id = ?", (creator_id,)).fetchone()
            if not c:
                return False
            conn.execute("""
                INSERT OR IGNORE INTO user_creators (user_id, creator_id, default_download_limit, preferred_quality, created_at, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (user_id, creator_id, download_limit or 3, preferred_quality or '1080p'))

        rec = conn.execute(
            "SELECT * FROM user_creators WHERE user_id = ? AND creator_id = ?",
            (user_id, creator_id)
        ).fetchone()

        new_limit = download_limit if download_limit is not None else rec["default_download_limit"]
        new_qual = preferred_quality if preferred_quality is not None else rec["preferred_quality"]

        conn.execute("""
            UPDATE user_creators
            SET default_download_limit = ?, preferred_quality = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND creator_id = ?
        """, (new_limit, new_qual, user_id, creator_id))

        conn.execute("""
            INSERT INTO user_creator_settings (user_id, creator_id, default_download_limit, preferred_quality, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, creator_id) DO UPDATE SET
                default_download_limit = excluded.default_download_limit,
                preferred_quality = excluded.preferred_quality,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, creator_id, new_limit, new_qual))
        conn.commit()
        return True

def save_creator_videos(creator_username: str, videos: List[Dict[str, Any]]) -> int:
    """Batch insert or update creator videos into creator_videos table."""
    clean = creator_username.lstrip("@").strip()
    if not videos:
        return 0

    inserted = 0
    with get_db_connection() as conn:
        for v in videos:
            vid = str(v.get("id") or "")
            if not vid:
                continue
            title = v.get("title") or ""
            desc = v.get("description") or title
            dur = int(v.get("duration") or 0)
            thumb = v.get("thumbnail") or v.get("thumbnail_url") or ""
            url = v.get("url") or v.get("canonical_url") or f"https://www.tiktok.com/@{clean}/video/{vid}"
            
            conn.execute("""
                INSERT INTO creator_videos (
                    creator_username, video_id, title, description, duration, thumbnail, video_url, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(video_id) DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    duration = excluded.duration,
                    thumbnail = excluded.thumbnail,
                    video_url = excluded.video_url
            """, (clean, vid, title, desc, dur, thumb, url))
            inserted += 1
        conn.commit()
    return inserted

def get_cached_creator_videos(creator_username: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Retrieve cached creator videos from creator_videos table."""
    clean = creator_username.lstrip("@").strip()
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT video_id AS id, creator_username, title, description, duration, thumbnail, video_url AS url
            FROM creator_videos
            WHERE creator_username = ?
            ORDER BY id DESC
            LIMIT ?
        """, (clean, limit)).fetchall()
        return [dict(r) for r in rows]

def create_download_batch(batch_id: str, creator_username: str, target_limit: int = 50, user_id: str = "default_user", total_items: Optional[int] = None) -> Dict[str, Any]:
    """Create a persistent download batch record owned by user_id."""
    if total_items is not None:
        target_limit = total_items
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO download_batches (batch_id, user_id, creator_username, target_limit, remaining_count, status)
            VALUES (?, ?, ?, ?, ?, 'active')
            ON CONFLICT(batch_id) DO UPDATE SET
                target_limit = excluded.target_limit,
                user_id = excluded.user_id,
                updated_at = CURRENT_TIMESTAMP
        """, (batch_id, user_id, creator_username, target_limit, target_limit))
        conn.commit()
        row = conn.execute("SELECT * FROM download_batches WHERE batch_id = ? AND user_id = ?", (batch_id, user_id)).fetchone()
        return dict(row)

def get_download_batch_by_id(batch_id: str, user_id: str = "default_user") -> Optional[Dict[str, Any]]:
    """Retrieve download batch if owned by user_id."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM download_batches WHERE batch_id = ? AND user_id = ?", (batch_id, user_id)).fetchone()
        return dict(row) if row else None

def get_download_batches(limit: int = 50, user_id: str = "default_user") -> List[Dict[str, Any]]:
    """Retrieve all download batches for user_id."""
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM download_batches WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
        return [dict(r) for r in rows]

def update_download_batch(batch_id: str, completed: int, remaining: int, failed: int, skipped: int, status: str = "active"):
    """Update statistics of a download batch."""
    with get_db_connection() as conn:
        conn.execute("""
            UPDATE download_batches
            SET completed_count = ?, remaining_count = ?, failed_count = ?, skipped_count = ?, status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE batch_id = ?
        """, (completed, remaining, failed, skipped, status, batch_id))
        conn.commit()

def upsert_batch_item(batch_id: str, video_id: str, title: str, status: str = "Queued", file_path: str = "", file_size: int = 0, error_message: str = ""):
    """Track individual batch item status (Queued, Downloading, Verifying, Moving, Downloaded, Failed, Skipped)."""
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO batch_items (batch_id, video_id, title, status, file_path, file_size, error_message, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(batch_id, video_id) DO UPDATE SET
                status = excluded.status,
                file_path = excluded.file_path,
                file_size = excluded.file_size,
                error_message = excluded.error_message,
                updated_at = CURRENT_TIMESTAMP
        """, (batch_id, video_id, title, status, file_path, file_size, error_message))
        conn.commit()

def get_batch_items(batch_id: str) -> List[Dict[str, Any]]:
    """Retrieve all items in a batch."""
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM batch_items WHERE batch_id = ? ORDER BY id ASC", (batch_id,)).fetchall()
        return [dict(r) for r in rows]

def update_creator_progress(
    creator_username: str,
    status: str,
    total_found: int = 0,
    download_limit: int = 50,
    completed_count: int = 0,
    remaining_count: int = 0,
    failed_count: int = 0,
    skipped_count: int = 0,
    current_video_id: str = "",
    current_video_title: str = "",
    current_speed: str = "0 KB/s",
    current_percent: float = 0.0,
    last_video_index: int = 0,
    user_id: str = "default_user"
):
    """Update checkpoint state for creator downloads scoped by user_id."""
    clean_username = creator_username.lstrip("@").strip()
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO creator_progress (
                user_id, creator_username, status, total_found, download_limit,
                completed_count, remaining_count, failed_count, skipped_count,
                current_video_id, current_video_title, current_speed,
                current_percent, last_video_index, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, creator_username) DO UPDATE SET
                status = excluded.status,
                total_found = excluded.total_found,
                download_limit = excluded.download_limit,
                completed_count = excluded.completed_count,
                remaining_count = excluded.remaining_count,
                failed_count = excluded.failed_count,
                skipped_count = excluded.skipped_count,
                current_video_id = excluded.current_video_id,
                current_video_title = excluded.current_video_title,
                current_speed = excluded.current_speed,
                current_percent = excluded.current_percent,
                last_video_index = excluded.last_video_index,
                updated_at = CURRENT_TIMESTAMP
        """, (
            user_id, clean_username, status, total_found, download_limit,
            completed_count, remaining_count, failed_count, skipped_count,
            current_video_id, current_video_title, current_speed,
            current_percent, last_video_index
        ))
        conn.commit()

def get_creator_progress(creator_username: str, user_id: str = "default_user") -> Optional[Dict[str, Any]]:
    """Retrieve creator progress by username scoped by user_id."""
    clean_username = creator_username.lstrip("@").strip()
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM creator_progress WHERE user_id = ? AND creator_username = ?",
            (user_id, clean_username)
        ).fetchone()
        return dict(row) if row else None

def add_creator_activity(
    creator_username: str,
    video_id: str,
    title: str,
    status: str,
    message: str,
    user_id: str = "default_user"
):
    """Log an activity item for creator progress feed scoped by user_id."""
    clean_username = creator_username.lstrip("@").strip()
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO creator_activities (user_id, creator_username, video_id, title, status, message)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, clean_username, video_id, title, status, message))
        conn.commit()

def get_creator_activities(creator_username: str, limit: int = 30, user_id: str = "default_user") -> List[Dict[str, Any]]:
    """Get latest activity logs for creator progress scoped by user_id."""
    clean_username = creator_username.lstrip("@").strip()
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM creator_activities
            WHERE user_id = ? AND creator_username = ?
            ORDER BY id DESC LIMIT ?
        """, (user_id, clean_username, limit)).fetchall()
        return [dict(r) for r in rows]

# --- Facebook Upload Jobs Helpers ---

def create_fb_job_record(
    job_id: str,
    video_id: str,
    title: str,
    file_path: str,
    download_id: Optional[int] = None,
    creator: str = "",
    description: str = "",
    target_platform: str = "facebook_marketplace",
    user_id: str = "default_user",
    metadata: str = "{}"
) -> Dict[str, Any]:
    """Create a persistent Facebook Upload Job."""
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO facebook_jobs (
                job_id, upload_job_id, user_id, download_id, video_id, title, file_path, local_file_path,
                creator, description, metadata, target_platform, status, upload_status,
                field_validated, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', 'ready', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(job_id) DO UPDATE SET
                title = excluded.title,
                file_path = excluded.file_path,
                local_file_path = excluded.local_file_path,
                target_platform = excluded.target_platform,
                status = 'ready',
                upload_status = 'ready',
                updated_at = CURRENT_TIMESTAMP
        """, (job_id, job_id, user_id, download_id, video_id, title, file_path, file_path, creator, description, metadata, target_platform))
        conn.commit()
    return get_fb_job_by_id(job_id, user_id)

def get_fb_job_by_id(job_id: str, user_id: str = "default_user") -> Optional[Dict[str, Any]]:
    """Fetch Facebook job by ID."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM facebook_jobs WHERE job_id = ? AND user_id = ?", (job_id, user_id)).fetchone()
        return _normalize_fb_job_dict(row)

def get_all_fb_jobs(limit: int = 50, user_id: str = "default_user") -> List[Dict[str, Any]]:
    """Fetch all recent Facebook jobs for user."""
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM facebook_jobs WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
        return [_normalize_fb_job_dict(r) for r in rows if r]

def update_fb_job_status(job_id: str, status: str, validated: bool = True, error_msg: str = ""):
    """Update Facebook job status and field validation state."""
    with get_db_connection() as conn:
        conn.execute("""
            UPDATE facebook_jobs
            SET status = ?, upload_status = ?, field_validated = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP
            WHERE job_id = ?
        """, (status, status, 1 if validated else 0, error_msg, job_id))
        conn.commit()

# --- Stats & Settings ---

def format_storage_size(num_bytes: int) -> str:
    """Format bytes into clean human-readable unit (B, KB, MB, GB, TB)."""
    if not num_bytes or num_bytes <= 0:
        return "0 B"
    b = float(num_bytes)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if b < 1024.0:
            return f"{b:.1f} {unit}" if unit != 'B' else f"{int(b)} B"
        b /= 1024.0
    return f"{b:.1f} PB"

def get_stats(user_id: str = "default_user", days: Optional[int] = None) -> Dict[str, Any]:
    """Calculate aggregated stats, multi-interval time-series, success/failure metrics, and recent activity from SQLite."""
    with get_db_connection() as conn:
        date_filter = ""
        date_params = []
        is_today = (days == 1)

        if is_today:
            date_filter = " AND created_at >= datetime('now', '-24 hours')"
        elif days and days > 0:
            date_filter = " AND created_at >= datetime('now', ?)"
            date_params = [f"-{int(days)} days"]

        # Base counts in selected timeframe
        total = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE user_id = ?{date_filter}", [user_id] + date_params).fetchone()[0]
        completed = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE user_id = ? AND status = 'completed'{date_filter}", [user_id] + date_params).fetchone()[0]
        failed = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE user_id = ? AND status = 'failed'{date_filter}", [user_id] + date_params).fetchone()[0]
        skipped_dls = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE user_id = ? AND status = 'skipped'{date_filter}", [user_id] + date_params).fetchone()[0]
        skipped_batches = conn.execute(f"SELECT COALESCE(SUM(skipped_count), 0) FROM download_batches WHERE user_id = ?{date_filter}", [user_id] + date_params).fetchone()[0]
        skipped = max(skipped_dls, skipped_batches)

        active_dls = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE user_id = ? AND status IN ('downloading', 'queued', 'active', 'in_progress'){date_filter}", [user_id] + date_params).fetchone()[0]
        active_batches = conn.execute(f"SELECT COUNT(*) FROM download_batches WHERE user_id = ? AND status = 'active'{date_filter}", [user_id] + date_params).fetchone()[0]
        active = active_dls + active_batches

        mp4_count = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE user_id = ? AND LOWER(format) = 'mp4'{date_filter}", [user_id] + date_params).fetchone()[0]
        mp3_count = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE user_id = ? AND LOWER(format) = 'mp3'{date_filter}", [user_id] + date_params).fetchone()[0]
        hd_count = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE user_id = ? AND quality IN ('1080p', '4K UHD', '2K', 'HD'){date_filter}", [user_id] + date_params).fetchone()[0]
        
        saved_creators = conn.execute(f"SELECT COUNT(*) FROM creators WHERE user_id = ?{date_filter}", [user_id] + date_params).fetchone()[0]
        dl_creators = conn.execute(f"SELECT COUNT(DISTINCT creator) FROM downloads WHERE user_id = ? AND creator IS NOT NULL AND creator != ''{date_filter}", [user_id] + date_params).fetchone()[0]
        creators_count = max(saved_creators, dl_creators)

        # Storage used (bytes from completed downloads)
        storage_bytes = conn.execute(f"SELECT COALESCE(SUM(filesize), 0) FROM downloads WHERE user_id = ? AND status = 'completed'{date_filter}", [user_id] + date_params).fetchone()[0]
        storage_formatted = format_storage_size(storage_bytes)

        # Success rate
        finished = completed + failed
        if finished > 0:
            success_rate = round((completed / finished) * 100.0, 1)
        else:
            success_rate = 100.0 if completed > 0 else 0.0

        # Timeline generation (Hourly for Today, Daily for 7/30/All)
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        daily_data = []
        daily_completed = []
        daily_failed = []
        labels = []
        sf_timeline = []

        if is_today:
            # 24 hourly buckets: from 23 hours ago to current hour
            hourly_rows = conn.execute("""
                SELECT strftime('%Y-%m-%d %H', created_at) as dl_hr,
                       COUNT(*) as cnt,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as comp_cnt,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as fail_cnt
                FROM downloads
                WHERE user_id = ? AND created_at >= datetime('now', '-24 hours')
                GROUP BY dl_hr
            """, (user_id,)).fetchall()
            h_map = {r["dl_hr"]: (r["cnt"], r["comp_cnt"], r["fail_cnt"]) for r in hourly_rows}

            for h_offset in range(23, -1, -1):
                dt = now - timedelta(hours=h_offset)
                key = dt.strftime("%Y-%m-%d %H")
                lbl = dt.strftime("%H:00")
                cnt, comp, fl = h_map.get(key, (0, 0, 0))
                daily_data.append(cnt)
                daily_completed.append(comp)
                daily_failed.append(fl)
                labels.append(lbl)
                sf_timeline.append({
                    "label": lbl,
                    "total": cnt,
                    "completed": comp,
                    "failed": fl
                })
        else:
            chart_days = days if (days and days > 0) else 30
            daily_rows = conn.execute("""
                SELECT date(created_at) as dl_date,
                       COUNT(*) as cnt,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as comp_cnt,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as fail_cnt
                FROM downloads
                WHERE user_id = ? AND created_at >= datetime('now', ?)
                GROUP BY dl_date
                ORDER BY dl_date ASC
            """, [user_id, f"-{int(chart_days)} days"]).fetchall()
            
            daily_map = {r["dl_date"]: (r["cnt"], r["comp_cnt"], r["fail_cnt"]) for r in daily_rows}
            
            for d in range(chart_days - 1, -1, -1):
                day_dt = now - timedelta(days=d)
                day_str = day_dt.strftime("%Y-%m-%d")
                cnt, comp, fl = daily_map.get(day_str, (0, 0, 0))
                daily_data.append(cnt)
                daily_completed.append(comp)
                daily_failed.append(fl)
                lbl = day_dt.strftime("%d %b") if chart_days <= 14 else str(day_dt.day)
                labels.append(lbl)
                sf_timeline.append({
                    "label": lbl,
                    "date": day_str,
                    "total": cnt,
                    "completed": comp,
                    "failed": fl
                })

        # Trends: compare with prior period
        if days and days > 0:
            prev_filter = " AND created_at >= datetime('now', ?) AND created_at < datetime('now', ?)"
            prev_params = [f"-{int(days) * 2} days", f"-{int(days)} days"]
            prev_total = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE user_id = ?{prev_filter}", [user_id] + prev_params).fetchone()[0]
            if prev_total > 0:
                pct = round(((total - prev_total) / prev_total) * 100, 1)
                total_trend = f"{'+' if pct >= 0 else ''}{pct}%"
            else:
                total_trend = "+100%" if total > 0 else "0%"
        else:
            total_trend = "+100%" if total > 0 else "0%"

        # Top creators
        top_creators = conn.execute("""
            SELECT creator, COUNT(*) as count
            FROM downloads
            WHERE user_id = ? AND creator IS NOT NULL AND creator != ''
            GROUP BY creator
            ORDER BY count DESC LIMIT 8
        """, (user_id,)).fetchall()
        
        cleaned_top_creators = []
        creator_sum = sum(r["count"] for r in top_creators) or 1
        for r in top_creators:
            c_name = (r["creator"] or "").strip()
            if c_name:
                cleaned_top_creators.append({
                    "creator": c_name,
                    "count": r["count"],
                    "percentage": round((r["count"] / creator_sum) * 100, 1)
                })

        # Recent downloads activity stream
        recent_rows = conn.execute("""
            SELECT id, title, creator, format, quality, filesize, status, created_at, file_name
            FROM downloads
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 10
        """, (user_id,)).fetchall()

        recent_downloads = []
        for r in recent_rows:
            recent_downloads.append({
                "id": r["id"],
                "title": r["title"] or "Untitled TikTok",
                "creator": (r["creator"] or "").strip(),
                "format": (r["format"] or "mp4").upper(),
                "quality": r["quality"] or "HD",
                "filesize": r["filesize"] or 0,
                "filesize_formatted": format_storage_size(r["filesize"] or 0),
                "status": r["status"] or "completed",
                "created_at": r["created_at"] or ""
            })

        time_range_title = "Today (24 Hours)" if is_today else (f"Last {days} Days" if days else "All Time")

        return {
            "total_downloads": total,
            "completed_downloads": completed,
            "failed_downloads": failed,
            "skipped_downloads": skipped,
            "active_downloads": active,
            "creators_count": creators_count,
            "creator_downloads": creators_count,
            "storage_bytes": storage_bytes,
            "storage_formatted": storage_formatted,
            "success_rate": success_rate,
            "mp4_downloads": mp4_count,
            "mp3_downloads": mp3_count,
            "hd_downloads": hd_count,
            "days": days,
            "time_range": time_range_title,
            "daily_data": daily_data,
            "daily_completed": daily_completed,
            "daily_failed": daily_failed,
            "daily_labels": labels,
            "success_failure_timeline": sf_timeline,
            "total_trend": total_trend,
            "top_creators": cleaned_top_creators,
            "recent_downloads": recent_downloads
        }

def get_settings() -> Dict[str, str]:
    """Fetch all settings."""
    with get_db_connection() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

def update_settings(settings_dict: Dict[str, str]):
    """Update settings."""
    with get_db_connection() as conn:
        for k, v in settings_dict.items():
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
        conn.commit()

# =========================================================================
# Authentication, Licenses, Devices & Sessions Repository Helpers
# =========================================================================

def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Retrieve user record by email (case-insensitive)."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email.strip(),)).fetchone()
        return dict(row) if row else None

def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve user record by ID."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

def create_user_record(user_id: str, email: str, password_hash: str, role: str = "user", plan: str = "Pro Plan", status: str = "ACTIVE", name: Optional[str] = None) -> Dict[str, Any]:
    """Create a new user record with hashed password and name."""
    username = email.split("@")[0]
    real_name = name.strip() if name and name.strip() else username
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO users (id, name, username, email, password_hash, role, plan, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (user_id, real_name, username, email.strip().lower(), password_hash, role, plan, status))
        conn.commit()
    return get_user_by_id(user_id)

def update_user_record(user_id: str, fields: Dict[str, Any]) -> bool:
    """Update user fields."""
    if not fields:
        return False
    set_clauses = [f"{k} = ?" for k in fields.keys()]
    values = list(fields.values()) + [user_id]
    with get_db_connection() as conn:
        conn.execute(f"UPDATE users SET {', '.join(set_clauses)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", values)
        conn.commit()
    return True

def get_all_users() -> List[Dict[str, Any]]:
    """Get all users for Admin Panel."""
    with get_db_connection() as conn:
        rows = conn.execute("SELECT id, name, username, email, role, plan, status, created_at, updated_at, last_login_at FROM users ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

# --- License Helpers ---

def calculate_days_remaining(expiry_str: Optional[str]) -> int:
    """Calculate remaining days from an expiry string."""
    if not expiry_str:
        return 0
    try:
        exp_clean = expiry_str.replace("T", " ").split(".")[0]
        exp_dt = datetime.strptime(exp_clean, "%Y-%m-%d %H:%M:%S")
        delta = exp_dt - datetime.now()
        return max(0, delta.days if delta.total_seconds() > 0 else 0)
    except Exception:
        return 0

def get_license_by_serial(serial_key: str) -> Optional[Dict[str, Any]]:
    """Retrieve license by formatted serial key with days_remaining."""
    clean_key = (serial_key or "").strip()
    if not clean_key:
        return None
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM licenses WHERE LOWER(serial_key) = LOWER(?)", (clean_key,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["days_remaining"] = calculate_days_remaining(d.get("expiry_date"))
        if not d.get("duration_days"):
            d["duration_days"] = 30
        return d

def get_license_by_id(license_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve license by integer ID with days_remaining."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM licenses WHERE id = ?", (license_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["days_remaining"] = calculate_days_remaining(d.get("expiry_date"))
        if not d.get("duration_days"):
            d["duration_days"] = 30
        return d

def get_user_license(user_id: str) -> Optional[Dict[str, Any]]:
    """Get active license assigned to a user."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM licenses WHERE assigned_user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["days_remaining"] = calculate_days_remaining(d.get("expiry_date"))
        if not d.get("duration_days"):
            d["duration_days"] = 30
        return d

def get_all_licenses_for_user(user_id: str) -> List[Dict[str, Any]]:
    """Retrieve all licenses assigned to a user ordered by id DESC."""
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM licenses WHERE assigned_user_id = ? ORDER BY id DESC", (user_id,)).fetchall()
        licenses = []
        for r in rows:
            d = dict(r)
            d["days_remaining"] = calculate_days_remaining(d.get("expiry_date"))
            if not d.get("duration_days"):
                d["duration_days"] = 30
            licenses.append(d)
        return licenses

def is_license_active(lic: Optional[Dict[str, Any]]) -> bool:
    """Check if a license record is currently active and not expired."""
    if not lic:
        return False
    status = (lic.get("status") or "").upper()
    if status != "ACTIVE":
        return False
    exp_str = lic.get("expiry_date")
    if exp_str:
        try:
            exp_clean = exp_str.replace("T", " ").split(".")[0]
            exp_dt = datetime.strptime(exp_clean, "%Y-%m-%d %H:%M:%S")
            if datetime.now() > exp_dt:
                return False
        except Exception:
            pass
    return True

def get_user_last_expired_license(user_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve the user's most recently expired license.
    Excludes REVOKED or SUSPENDED licenses.
    Returns the expired license with the highest id / most recent date.
    """
    all_lics = get_all_licenses_for_user(user_id)
    now = datetime.now()
    expired_list = []
    for lic in all_lics:
        status = (lic.get("status") or "").upper()
        if status in ("REVOKED", "SUSPENDED"):
            continue
        is_exp = (status == "EXPIRED")
        exp_str = lic.get("expiry_date")
        if exp_str:
            try:
                exp_clean = exp_str.replace("T", " ").split(".")[0]
                exp_dt = datetime.strptime(exp_clean, "%Y-%m-%d %H:%M:%S")
                if now > exp_dt:
                    is_exp = True
            except Exception:
                pass
        if is_exp:
            expired_list.append(lic)

    if not expired_list:
        return None
    def _parse_exp(l):
        s = l.get("expiry_date") or ""
        try:
            return datetime.strptime(s.replace("T", " ").split(".")[0], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.min
    expired_list.sort(key=lambda x: (_parse_exp(x), x.get("id", 0)), reverse=True)
    return expired_list[0]

def create_license_record(
    serial_key: str,
    assigned_user_id: Optional[str],
    expiry_date: Optional[str] = None,
    max_devices: int = 1,
    status: str = "ACTIVE",
    duration_days: int = 30,
    issued_at: Optional[str] = None
) -> Dict[str, Any]:
    """Create a new license record with duration in days and calculated expiry."""
    now_dt = datetime.now()
    if not issued_at:
        issued_at = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    if not expiry_date:
        expiry_date = (now_dt + timedelta(days=duration_days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO licenses (serial_key, status, assigned_user_id, start_date, expiry_date, max_devices, duration_days, issued_at, created_at, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (serial_key.strip(), status, assigned_user_id, expiry_date, max_devices, duration_days, issued_at))
        conn.commit()
        return get_license_by_id(cursor.lastrowid)

def extend_license_days(license_id: int, additional_days: int) -> Dict[str, Any]:
    """
    Safely extend a license duration by adding additional days to its remaining time.
    If already expired, starts extension from current datetime.
    """
    lic = get_license_by_id(license_id)
    if not lic:
        raise RuntimeError("License not found.")

    now_dt = datetime.now()
    current_expiry_str = lic.get("expiry_date", "")
    current_expiry = now_dt
    if current_expiry_str:
        try:
            current_expiry = datetime.strptime(current_expiry_str.replace("T", " ").split(".")[0], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            current_expiry = now_dt

    base_dt = current_expiry if current_expiry > now_dt else now_dt
    new_expiry_dt = base_dt + timedelta(days=additional_days)
    new_expiry_str = new_expiry_dt.strftime("%Y-%m-%d %H:%M:%S")

    current_dur = lic.get("duration_days") or 30
    new_duration = current_dur + additional_days

    fields = {
        "expiry_date": new_expiry_str,
        "duration_days": new_duration,
        "status": "ACTIVE"
    }
    update_license_record(license_id, fields)
    return get_license_by_id(license_id)

def update_license_record(license_id: int, fields: Dict[str, Any]) -> bool:
    """Update license fields (status, expiry_date, max_devices, duration_days)."""
    if not fields:
        return False
    set_clauses = [f"{k} = ?" for k in fields.keys()]
    values = list(fields.values()) + [license_id]
    with get_db_connection() as conn:
        conn.execute(f"UPDATE licenses SET {', '.join(set_clauses)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", values)
        conn.commit()
    return True

def delete_license_record(license_id: int) -> bool:
    """Safely delete a license record and handle dependent sessions/devices/requests without cascading to user/downloads/creators."""
    with get_db_connection() as conn:
        # 1. Invalidate active sessions for any user currently assigned this license
        conn.execute("""
            UPDATE sessions 
            SET is_active = 0 
            WHERE user_id IN (SELECT assigned_user_id FROM licenses WHERE id = ? AND assigned_user_id IS NOT NULL)
        """, (license_id,))
        # 2. Unlink any license requests pointing to this license (set license_id = NULL) to keep request history safe
        conn.execute("UPDATE license_requests SET license_id = NULL WHERE license_id = ?", (license_id,))
        # 3. Remove devices registered under this specific license (unrelated devices for other licenses are NOT touched)
        conn.execute("DELETE FROM devices WHERE license_id = ?", (license_id,))
        # 4. Delete the license itself
        cursor = conn.execute("DELETE FROM licenses WHERE id = ?", (license_id,))
        conn.commit()
        return cursor.rowcount > 0

def get_all_licenses() -> List[Dict[str, Any]]:
    """Get all licenses with device count for Admin Panel."""
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT l.*, u.email as user_email,
                   (SELECT COUNT(*) FROM devices d WHERE d.license_id = l.id AND d.status = 'ACTIVE') as active_devices_count
            FROM licenses l
            LEFT JOIN users u ON l.assigned_user_id = u.id
            ORDER BY l.id DESC
        """).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["days_remaining"] = calculate_days_remaining(d.get("expiry_date"))
            if not d.get("duration_days"):
                d["duration_days"] = 30
            results.append(d)
        return results

# --- License Request Helpers ---

def create_license_request_record(request_id: str, user_id: str, message: str = "") -> Dict[str, Any]:
    """Create a new license request in PENDING status."""
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO license_requests (id, user_id, message, status, created_at, updated_at)
            VALUES (?, ?, ?, 'PENDING', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (request_id, user_id, message.strip()))
        conn.commit()
    return get_license_request_by_id(request_id)

def get_license_request_by_id(request_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve license request by ID joined with user email and license serial."""
    with get_db_connection() as conn:
        row = conn.execute("""
            SELECT lr.*, u.email as user_email, u.username as user_name,
                   l.serial_key, l.duration_days as license_duration_days, l.status as license_status
            FROM license_requests lr
            LEFT JOIN users u ON lr.user_id = u.id
            LEFT JOIN licenses l ON lr.license_id = l.id
            WHERE lr.id = ?
        """, (request_id,)).fetchone()
        return dict(row) if row else None

def get_latest_license_request_for_user(user_id: str) -> Optional[Dict[str, Any]]:
    """Get the most recent license request for a user."""
    with get_db_connection() as conn:
        row = conn.execute("""
            SELECT lr.*, u.email as user_email,
                   l.serial_key, l.duration_days as license_duration_days, l.status as license_status
            FROM license_requests lr
            LEFT JOIN users u ON lr.user_id = u.id
            LEFT JOIN licenses l ON lr.license_id = l.id
            WHERE lr.user_id = ?
            ORDER BY lr.created_at DESC LIMIT 1
        """, (user_id,)).fetchone()
        return dict(row) if row else None

def get_pending_license_requests_count() -> int:
    """Return count of PENDING license requests."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM license_requests WHERE status = 'PENDING'").fetchone()
        return row[0] if row else 0

def get_all_license_requests(status: Optional[str] = None, search: Optional[str] = None, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Retrieve all license requests for Admin Panel."""
    query = """
        SELECT lr.*, u.email as user_email, u.username as user_name,
               l.serial_key, l.duration_days as license_duration_days, l.status as license_status
        FROM license_requests lr
        LEFT JOIN users u ON lr.user_id = u.id
        LEFT JOIN licenses l ON lr.license_id = l.id
        WHERE 1=1
    """
    params = []
    if search:
        query += " AND (lr.id LIKE ? OR u.email LIKE ? OR lr.message LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    if status and status.upper() != "ALL":
        query += " AND lr.status = ?"
        params.append(status.upper())
    query += " ORDER BY CASE WHEN lr.status = 'PENDING' THEN 0 ELSE 1 END, lr.created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

def update_license_request_record(request_id: str, fields: Dict[str, Any]) -> bool:
    """Update license request record."""
    if not fields:
        return False
    set_clauses = [f"{k} = ?" for k in fields.keys()]
    values = list(fields.values()) + [request_id]
    with get_db_connection() as conn:
        conn.execute(f"UPDATE license_requests SET {', '.join(set_clauses)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", values)
        conn.commit()
    return True

# --- Device Helpers ---

def get_device_by_fingerprint_and_license(fingerprint: str, license_id: int) -> Optional[Dict[str, Any]]:
    """Check if device is registered for a license."""
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM devices WHERE device_fingerprint = ? AND license_id = ?",
            (fingerprint, license_id)
        ).fetchone()
        return dict(row) if row else None

def get_active_device_count_for_license(license_id: int) -> int:
    """Count active devices for a license."""
    with get_db_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM devices WHERE license_id = ? AND status = 'ACTIVE'",
            (license_id,)
        ).fetchone()[0]
        return count

def register_device(device_fingerprint: str, user_id: str, license_id: int, app_version: str = "2.2.0") -> Dict[str, Any]:
    """Register a new device or reactivate an existing record."""
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO devices (device_fingerprint, user_id, license_id, status, first_seen_at, last_seen_at, app_version, created_at, updated_at)
            VALUES (?, ?, ?, 'ACTIVE', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(license_id, device_fingerprint) DO UPDATE SET
                status = 'ACTIVE',
                last_seen_at = CURRENT_TIMESTAMP,
                app_version = excluded.app_version,
                updated_at = CURRENT_TIMESTAMP
        """, (device_fingerprint, user_id, license_id, app_version))
        conn.commit()
    return get_device_by_fingerprint_and_license(device_fingerprint, license_id)

def update_device_last_seen(device_id: int) -> bool:
    """Update device last_seen_at timestamp."""
    with get_db_connection() as conn:
        conn.execute("UPDATE devices SET last_seen_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (device_id,))
        conn.commit()
    return True

def deactivate_device(device_id: int, status: str = "DEACTIVATED") -> bool:
    """
    Deactivate or revoke a device.
    CRITICAL: Only administrative operation frees a device slot!
    """
    with get_db_connection() as conn:
        conn.execute("UPDATE devices SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, device_id))
        conn.commit()
    return True

def get_device_by_id(device_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve device by primary key ID."""
    with get_db_connection() as conn:
        row = conn.execute("""
            SELECT d.*, u.email as user_email, l.serial_key
            FROM devices d
            LEFT JOIN users u ON d.user_id = u.id
            LEFT JOIN licenses l ON d.license_id = l.id
            WHERE d.id = ?
        """, (device_id,)).fetchone()
        return dict(row) if row else None

def get_all_devices() -> List[Dict[str, Any]]:
    """Get all devices for Admin Panel."""
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT d.*, u.email as user_email, l.serial_key
            FROM devices d
            LEFT JOIN users u ON d.user_id = u.id
            LEFT JOIN licenses l ON d.license_id = l.id
            ORDER BY d.last_seen_at DESC
        """).fetchall()
        return [dict(r) for r in rows]

# --- Session Helpers ---

def create_session_record(token: str, user_id: str, device_fingerprint: str, expires_at: str) -> Dict[str, Any]:
    """Create an active session token."""
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO sessions (token, user_id, device_fingerprint, expires_at, is_active, created_at)
            VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
        """, (token, user_id, device_fingerprint, expires_at))
        conn.commit()
    return get_session_by_token(token)

def get_session_by_token(token: str) -> Optional[Dict[str, Any]]:
    """Retrieve session record by token if active."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE token = ? AND is_active = 1", (token,)).fetchone()
        return dict(row) if row else None

def deactivate_session(token: str) -> bool:
    """
    Deactivate a user session on logout.
    CRITICAL: Does NOT free device slot in devices table!
    """
    with get_db_connection() as conn:
        conn.execute("UPDATE sessions SET is_active = 0 WHERE token = ?", (token,))
        conn.commit()
    return True

def deactivate_sessions_for_user(user_id: str) -> int:
    """Deactivate all active sessions for a user upon revocation/suspension/disablement."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE sessions SET is_active = 0 WHERE user_id = ? AND is_active = 1", (user_id,))
        count = cur.rowcount
        conn.commit()
        return count

def deactivate_sessions_for_license(license_id: int) -> int:
    """Deactivate all active sessions for users assigned to a specific license."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE sessions SET is_active = 0 
            WHERE user_id IN (SELECT assigned_user_id FROM licenses WHERE id = ?)
              AND is_active = 1
        """, (license_id,))
        count = cur.rowcount
        conn.commit()
        return count

def deactivate_sessions_for_device(device_fingerprint: str) -> int:
    """Deactivate all active sessions originating from a deactivated or revoked device."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE sessions SET is_active = 0 WHERE device_fingerprint = ? AND is_active = 1", (device_fingerprint,))
        count = cur.rowcount
        conn.commit()
        return count

# =========================================================================
# Dedicated Admin & Admin Session Helpers
# =========================================================================

def get_admin_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Retrieve administrator record by email."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM admins WHERE email = ?", (email.strip().lower(),)).fetchone()
        return dict(row) if row else None

def get_admin_by_id(admin_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve administrator record by ID."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM admins WHERE id = ?", (admin_id,)).fetchone()
        return dict(row) if row else None

def get_all_admins() -> List[Dict[str, Any]]:
    """List all administrators with stripped password hashes."""
    with get_db_connection() as conn:
        rows = conn.execute("SELECT id, name, email, role, permissions, status, created_at, updated_at, last_login_at FROM admins ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]

def create_admin_record(admin_id: str, name: str, email: str, password_hash: str, role: str = 'ADMIN', permissions: str = '*', status: str = 'ACTIVE') -> Dict[str, Any]:
    """Create a new administrator record."""
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO admins (id, name, email, password_hash, role, permissions, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (admin_id, name, email.strip().lower(), password_hash, role, permissions, status))
        conn.commit()
    return get_admin_by_id(admin_id)

def update_admin_record(admin_id: str, fields: Dict[str, Any]) -> bool:
    """Update fields on an administrator record."""
    if not fields:
        return False
    set_clause = ", ".join([f"{k} = ?" for k in fields.keys()])
    values = list(fields.values()) + [admin_id]
    with get_db_connection() as conn:
        conn.execute(f"UPDATE admins SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", values)
        conn.commit()
    return True

def delete_admin_record(admin_id: str) -> bool:
    """Delete an administrator record and invalidate active sessions."""
    with get_db_connection() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE admin_id = ?", (admin_id,))
        conn.execute("DELETE FROM admins WHERE id = ?", (admin_id,))
        conn.commit()
    return True

def create_admin_session(token: str, admin_id: str, expires_at: str) -> Dict[str, Any]:
    """Create a new active administrator session token."""
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO admin_sessions (token, admin_id, expires_at, is_active, created_at)
            VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
        """, (token, admin_id, expires_at))
        conn.commit()
    return get_admin_session_by_token(token)

def get_admin_session_by_token(token: str) -> Optional[Dict[str, Any]]:
    """Retrieve an active administrator session."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM admin_sessions WHERE token = ? AND is_active = 1", (token,)).fetchone()
        return dict(row) if row else None

def deactivate_admin_session(token: str) -> bool:
    """Deactivate an administrator session on logout."""
    with get_db_connection() as conn:
        conn.execute("UPDATE admin_sessions SET is_active = 0 WHERE token = ?", (token,))
        conn.commit()
    return True

def deactivate_all_admin_sessions(admin_id: str, except_token: Optional[str] = None) -> bool:
    """Deactivate active sessions for a given admin (e.g. after password change). Keeps except_token active if provided."""
    with get_db_connection() as conn:
        if except_token:
            conn.execute("UPDATE admin_sessions SET is_active = 0 WHERE admin_id = ? AND token != ?", (admin_id, except_token))
        else:
            conn.execute("UPDATE admin_sessions SET is_active = 0 WHERE admin_id = ?", (admin_id,))
        conn.commit()
    return True

# =========================================================================
# Audit Logging Helpers
# =========================================================================

def add_audit_log(actor_id: str, actor_email: str, action: str, target_type: Optional[str] = None, target_id: Optional[str] = None, details: Optional[str] = None, ip_address: Optional[str] = None) -> int:
    """Insert an immutable audit log record for tracking administrative actions."""
    with get_db_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO audit_logs (actor_id, actor_email, action, target_type, target_id, details, ip_address, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (actor_id, actor_email, action, target_type, str(target_id) if target_id is not None else None, details, ip_address))
        conn.commit()
        return cursor.lastrowid

def get_audit_logs(limit: int = 100, offset: int = 0, action_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieve audit log records in reverse chronological order."""
    with get_db_connection() as conn:
        if action_filter:
            rows = conn.execute("""
                SELECT * FROM audit_logs WHERE action = ? ORDER BY id DESC LIMIT ? OFFSET ?
            """, (action_filter, limit, offset)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM audit_logs ORDER BY id DESC LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()
        return [dict(r) for r in rows]

# =========================================================================
# System Settings Helpers
# =========================================================================

def get_system_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Retrieve a single system setting value by key."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT value FROM system_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

def get_system_settings_dict() -> Dict[str, Any]:
    """Retrieve all system settings grouped by category."""
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM system_settings").fetchall()
        res: Dict[str, Any] = {}
        for r in rows:
            res[r["key"]] = r["value"]
        return res

def update_system_setting(key: str, value: str, category: str = "general", updated_by: str = "admin") -> bool:
    """Upsert a system setting."""
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO system_settings (key, value, category, updated_at, updated_by)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                category = excluded.category,
                updated_at = CURRENT_TIMESTAMP,
                updated_by = excluded.updated_by
        """, (key, str(value), category, updated_by))
        conn.commit()
    return True

# =========================================================================
# Admin Dashboard, Analytics, and Data Management Queries
# =========================================================================

def get_admin_dashboard_metrics() -> Dict[str, Any]:
    """Calculate and return real database metrics for the Admin Dashboard."""
    with get_db_connection() as conn:
        # Users
        u_total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        u_active = conn.execute("SELECT COUNT(*) FROM users WHERE status = 'ACTIVE'").fetchone()[0]
        u_disabled = conn.execute("SELECT COUNT(*) FROM users WHERE status != 'ACTIVE'").fetchone()[0]

        # Licenses
        l_total = conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        l_active = conn.execute("SELECT COUNT(*) FROM licenses WHERE status = 'ACTIVE'").fetchone()[0]
        l_expired = conn.execute("SELECT COUNT(*) FROM licenses WHERE status = 'EXPIRED'").fetchone()[0]
        l_suspended = conn.execute("SELECT COUNT(*) FROM licenses WHERE status = 'SUSPENDED'").fetchone()[0]
        l_revoked = conn.execute("SELECT COUNT(*) FROM licenses WHERE status = 'REVOKED'").fetchone()[0]

        # Devices
        d_total = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        d_active = conn.execute("SELECT COUNT(*) FROM devices WHERE status = 'ACTIVE'").fetchone()[0]
        d_deactivated = conn.execute("SELECT COUNT(*) FROM devices WHERE status = 'DEACTIVATED'").fetchone()[0]
        d_revoked = conn.execute("SELECT COUNT(*) FROM devices WHERE status = 'REVOKED'").fetchone()[0]

        # Downloads
        dl_total = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        dl_today = conn.execute("SELECT COUNT(*) FROM downloads WHERE date(created_at) = date('now')").fetchone()[0]
        dl_week = conn.execute("SELECT COUNT(*) FROM downloads WHERE created_at >= datetime('now', '-7 days')").fetchone()[0]
        dl_month = conn.execute("SELECT COUNT(*) FROM downloads WHERE created_at >= datetime('now', '-30 days')").fetchone()[0]

        dl_completed = conn.execute("SELECT COUNT(*) FROM downloads WHERE status = 'completed'").fetchone()[0]
        dl_failed = conn.execute("SELECT COUNT(*) FROM downloads WHERE status = 'failed'").fetchone()[0]
        dl_active_count = conn.execute("SELECT COUNT(*) FROM downloads WHERE status IN ('downloading', 'verifying', 'moving', 'queued')").fetchone()[0]

        # Creators
        c_total = conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0]
        c_videos = conn.execute("SELECT COUNT(*) FROM creator_videos").fetchone()[0]

        # License Requests
        req_total = conn.execute("SELECT COUNT(*) FROM license_requests").fetchone()[0]
        req_pending = conn.execute("SELECT COUNT(*) FROM license_requests WHERE status = 'PENDING'").fetchone()[0]
        req_approved = conn.execute("SELECT COUNT(*) FROM license_requests WHERE status = 'APPROVED'").fetchone()[0]
        req_rejected = conn.execute("SELECT COUNT(*) FROM license_requests WHERE status = 'REJECTED'").fetchone()[0]

        # Feedback
        fb_total = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        fb_open = conn.execute("SELECT COUNT(*) FROM feedback WHERE status = 'OPEN'").fetchone()[0]
        fb_in_progress = conn.execute("SELECT COUNT(*) FROM feedback WHERE status = 'IN_PROGRESS'").fetchone()[0]
        fb_resolved = conn.execute("SELECT COUNT(*) FROM feedback WHERE status = 'RESOLVED'").fetchone()[0]
        fb_closed = conn.execute("SELECT COUNT(*) FROM feedback WHERE status = 'CLOSED'").fetchone()[0]

        return {
            "users": {
                "total": u_total,
                "active": u_active,
                "disabled": u_disabled
            },
            "license_requests": {
                "total": req_total,
                "pending": req_pending,
                "approved": req_approved,
                "rejected": req_rejected
            },
            "feedback": {
                "total": fb_total,
                "open": fb_open,
                "in_progress": fb_in_progress,
                "resolved": fb_resolved,
                "closed": fb_closed
            },
            "licenses": {
                "total": l_total,
                "active": l_active,
                "expired": l_expired,
                "suspended": l_suspended,
                "revoked": l_revoked
            },
            "devices": {
                "total": d_total,
                "active": d_active,
                "deactivated": d_deactivated,
                "revoked": d_revoked
            },
            "downloads": {
                "total": dl_total,
                "today": dl_today,
                "this_week": dl_week,
                "this_month": dl_month,
                "completed": dl_completed,
                "failed": dl_failed,
                "active": dl_active_count
            },
            "creators": {
                "total": c_total,
                "total_videos": c_videos
            }
        }

def get_admin_users_extended(search: Optional[str] = None, status: Optional[str] = None, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Retrieve detailed user records with active license and device counts."""
    query = """
        SELECT u.id, u.username, u.email, u.role, u.plan, u.status, u.created_at, u.updated_at, u.last_login_at,
               l.serial_key, l.status as license_status, l.expiry_date as license_expiry, l.max_devices,
               (SELECT COUNT(*) FROM devices d WHERE d.user_id = u.id AND d.status = 'ACTIVE') as active_devices_count,
               (SELECT COUNT(*) FROM downloads dl WHERE dl.user_id = u.id) as download_count
        FROM users u
        LEFT JOIN licenses l ON u.id = l.assigned_user_id
        WHERE 1=1
    """
    params = []
    if search:
        query += " AND (u.email LIKE ? OR u.username LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    if status:
        query += " AND u.status = ?"
        params.append(status.upper())
    query += " ORDER BY u.created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

def get_admin_licenses_extended(search: Optional[str] = None, status: Optional[str] = None, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Retrieve detailed license records with assigned user email, duration_days, and days_remaining."""
    query = """
        SELECT l.*, u.email as user_email, u.username as user_name,
               (SELECT COUNT(*) FROM devices d WHERE d.license_id = l.id AND d.status = 'ACTIVE') as active_device_count
        FROM licenses l
        LEFT JOIN users u ON l.assigned_user_id = u.id
        WHERE 1=1
    """
    params = []
    if search:
        query += " AND (l.serial_key LIKE ? OR u.email LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    if status:
        query += " AND l.status = ?"
        params.append(status.upper())
    query += " ORDER BY l.created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["days_remaining"] = calculate_days_remaining(d.get("expiry_date"))
            if not d.get("duration_days"):
                d["duration_days"] = 30
            results.append(d)
        return results

def get_admin_devices_extended(search: Optional[str] = None, status: Optional[str] = None, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Retrieve detailed device records with user email and license serial."""
    query = """
        SELECT d.*, u.email as user_email, l.serial_key, l.status as license_status
        FROM devices d
        LEFT JOIN users u ON d.user_id = u.id
        LEFT JOIN licenses l ON d.license_id = l.id
        WHERE 1=1
    """
    params = []
    if search:
        query += " AND (d.device_fingerprint LIKE ? OR u.email LIKE ? OR l.serial_key LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    if status:
        query += " AND d.status = ?"
        params.append(status.upper())
    query += " ORDER BY d.last_seen_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

def get_admin_downloads_extended(search: Optional[str] = None, status: Optional[str] = None, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Retrieve real download records for admin inspection."""
    query = """
        SELECT d.id, d.user_id, d.video_id, d.title, d.creator, d.quality, d.format, d.filesize,
               d.filepath, d.file_name, d.status, d.error_message, d.created_at, d.completed_at,
               u.email as user_email
        FROM downloads d
        LEFT JOIN users u ON d.user_id = u.id
        WHERE 1=1
    """
    params = []
    if search:
        query += " AND (d.title LIKE ? OR d.creator LIKE ? OR d.video_id LIKE ? OR u.email LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%"])
    if status:
        query += " AND d.status = ?"
        params.append(status.lower())
    query += " ORDER BY d.created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

def get_admin_creators_extended(search: Optional[str] = None, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Retrieve real creator records for admin inspection."""
    query = """
        SELECT c.*, u.email as user_email,
               (SELECT COUNT(*) FROM downloads d WHERE d.creator = c.username OR d.creator = '@' || c.username) as total_downloaded_count
        FROM creators c
        LEFT JOIN users u ON c.user_id = u.id
        WHERE 1=1
    """
    params = []
    if search:
        query += " AND (c.username LIKE ? OR c.nickname LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    query += " ORDER BY c.created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

def get_admin_analytics_data(days: Optional[int] = 30) -> Dict[str, Any]:
    """Retrieve comprehensive platform statistics, KPIs, interactive chart time-series,
    cross-user download activities, and system audit logs for the Admin Console."""
    with get_db_connection() as conn:
        date_filter = ""
        date_params = []
        is_today = (days == 1)

        if is_today:
            date_filter = " AND created_at >= datetime('now', '-24 hours')"
        elif days and days > 0:
            date_filter = " AND created_at >= datetime('now', ?)"
            date_params = [f"-{int(days)} days"]

        # 1. User metrics
        u_total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        u_active = conn.execute("SELECT COUNT(*) FROM users WHERE status = 'ACTIVE'").fetchone()[0]
        u_disabled = conn.execute("SELECT COUNT(*) FROM users WHERE status != 'ACTIVE'").fetchone()[0]
        u_new_period = conn.execute(f"SELECT COUNT(*) FROM users WHERE 1=1{date_filter}", date_params).fetchone()[0]

        # 2. License metrics
        l_total = conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        l_active = conn.execute("SELECT COUNT(*) FROM licenses WHERE status = 'ACTIVE'").fetchone()[0]
        l_expired = conn.execute("SELECT COUNT(*) FROM licenses WHERE status = 'EXPIRED'").fetchone()[0]
        l_suspended = conn.execute("SELECT COUNT(*) FROM licenses WHERE status = 'SUSPENDED'").fetchone()[0]
        l_revoked = conn.execute("SELECT COUNT(*) FROM licenses WHERE status = 'REVOKED'").fetchone()[0]
        l_new_period = conn.execute(f"SELECT COUNT(*) FROM licenses WHERE 1=1{date_filter}", date_params).fetchone()[0]

        # 3. Downloads metrics
        dl_total = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE 1=1{date_filter}", date_params).fetchone()[0]
        dl_completed = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE status = 'completed'{date_filter}", date_params).fetchone()[0]
        dl_failed = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE status = 'failed'{date_filter}", date_params).fetchone()[0]
        dl_skipped = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE status = 'skipped'{date_filter}", date_params).fetchone()[0]
        dl_active = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE status IN ('downloading', 'queued', 'active', 'in_progress'){date_filter}", date_params).fetchone()[0]
        
        mp4_total = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE LOWER(format) = 'mp4'{date_filter}", date_params).fetchone()[0]
        mp3_total = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE LOWER(format) = 'mp3'{date_filter}", date_params).fetchone()[0]
        hd_total = conn.execute(f"SELECT COUNT(*) FROM downloads WHERE quality IN ('1080p', '4K UHD', '2K', 'HD'){date_filter}", date_params).fetchone()[0]

        storage_bytes = conn.execute(f"SELECT COALESCE(SUM(filesize), 0) FROM downloads WHERE status = 'completed'{date_filter}", date_params).fetchone()[0]
        storage_formatted = format_storage_size(storage_bytes)

        finished_dls = dl_completed + dl_failed
        success_rate = round((dl_completed / finished_dls) * 100.0, 1) if finished_dls > 0 else (100.0 if dl_completed > 0 else 0.0)

        # 4. License requests
        req_total = conn.execute(f"SELECT COUNT(*) FROM license_requests WHERE 1=1{date_filter}", date_params).fetchone()[0]
        req_pending = conn.execute("SELECT COUNT(*) FROM license_requests WHERE status = 'PENDING'").fetchone()[0]
        req_approved = conn.execute(f"SELECT COUNT(*) FROM license_requests WHERE status = 'APPROVED'{date_filter}", date_params).fetchone()[0]
        req_rejected = conn.execute(f"SELECT COUNT(*) FROM license_requests WHERE status = 'REJECTED'{date_filter}", date_params).fetchone()[0]

        # 5. Feedback
        fb_total = conn.execute(f"SELECT COUNT(*) FROM feedback WHERE 1=1{date_filter}", date_params).fetchone()[0]
        fb_open = conn.execute("SELECT COUNT(*) FROM feedback WHERE status = 'OPEN'").fetchone()[0]
        fb_in_progress = conn.execute("SELECT COUNT(*) FROM feedback WHERE status = 'IN_PROGRESS'").fetchone()[0]
        fb_resolved = conn.execute("SELECT COUNT(*) FROM feedback WHERE status = 'RESOLVED'").fetchone()[0]

        # 6. Time series timeline
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        daily_downloads = []
        labels = []
        timeline_total = []
        timeline_completed = []
        timeline_failed = []

        if is_today:
            hourly_rows = conn.execute("""
                SELECT strftime('%Y-%m-%d %H', created_at) as dl_hr,
                       COUNT(*) as count,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as comp_cnt,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as fail_cnt
                FROM downloads
                WHERE created_at >= datetime('now', '-24 hours')
                GROUP BY dl_hr
            """).fetchall()
            h_map = {r["dl_hr"]: (r["count"], r["comp_cnt"], r["fail_cnt"]) for r in hourly_rows}

            for h_offset in range(23, -1, -1):
                dt = now - timedelta(hours=h_offset)
                key = dt.strftime("%Y-%m-%d %H")
                lbl = dt.strftime("%H:00")
                cnt, comp, fl = h_map.get(key, (0, 0, 0))
                daily_downloads.append({"dl_date": lbl, "count": cnt, "completed": comp, "failed": fl})
                labels.append(lbl)
                timeline_total.append(cnt)
                timeline_completed.append(comp)
                timeline_failed.append(fl)
        else:
            chart_days = days if (days and days > 0) else 30
            daily_rows = conn.execute("""
                SELECT date(created_at) as dl_date,
                       COUNT(*) as count,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as comp_cnt,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as fail_cnt
                FROM downloads
                WHERE created_at >= datetime('now', ?)
                GROUP BY date(created_at)
                ORDER BY dl_date ASC
            """, [f"-{int(chart_days)} days"]).fetchall()
            d_map = {r["dl_date"]: (r["count"], r["comp_cnt"], r["fail_cnt"]) for r in daily_rows}

            for d in range(chart_days - 1, -1, -1):
                day_dt = now - timedelta(days=d)
                day_str = day_dt.strftime("%Y-%m-%d")
                cnt, comp, fl = d_map.get(day_str, (0, 0, 0))
                lbl = day_dt.strftime("%d %b") if chart_days <= 14 else str(day_dt.day)
                daily_downloads.append({"dl_date": day_str, "count": cnt, "completed": comp, "failed": fl})
                labels.append(lbl)
                timeline_total.append(cnt)
                timeline_completed.append(comp)
                timeline_failed.append(fl)

        # 7. Distributions
        status_rows = conn.execute("SELECT status, COUNT(*) as count FROM downloads GROUP BY status").fetchall()
        lic_rows = conn.execute("SELECT status, COUNT(*) as count FROM licenses GROUP BY status").fetchall()
        user_rows = conn.execute("SELECT status, COUNT(*) as count FROM users GROUP BY status").fetchall()

        # 8. Top creators across the whole platform
        top_creator_rows = conn.execute("""
            SELECT creator, COUNT(*) as count
            FROM downloads
            WHERE creator IS NOT NULL AND creator != ''
            GROUP BY creator
            ORDER BY count DESC LIMIT 8
        """).fetchall()

        total_cr_cnt = sum(r["count"] for r in top_creator_rows) or 1
        top_creators = [{
            "creator": (r["creator"] or "").strip(),
            "count": r["count"],
            "percentage": round((r["count"] / total_cr_cnt) * 100, 1)
        } for r in top_creator_rows if (r["creator"] or "").strip()]

        # 9. Recent Platform Downloads (with user email)
        recent_dl_rows = conn.execute("""
            SELECT d.id, d.title, d.creator, d.format, d.quality, d.filesize, d.status, d.created_at,
                   COALESCE(u.email, 'Unknown User') as user_email
            FROM downloads d
            LEFT JOIN users u ON d.user_id = u.id
            ORDER BY d.created_at DESC LIMIT 10
        """).fetchall()

        recent_platform_downloads = [{
            "id": r["id"],
            "title": r["title"] or "Untitled TikTok",
            "creator": (r["creator"] or "").strip(),
            "format": (r["format"] or "mp4").upper(),
            "quality": r["quality"] or "HD",
            "filesize": r["filesize"] or 0,
            "filesize_formatted": format_storage_size(r["filesize"] or 0),
            "status": r["status"] or "completed",
            "created_at": r["created_at"] or "",
            "user_email": r["user_email"]
        } for r in recent_dl_rows]

        # 10. Recent Audit Logs (System Activity)
        recent_audit_rows = conn.execute("""
            SELECT id, actor_email, action, target_type, target_id, details, ip_address, created_at
            FROM audit_logs
            ORDER BY created_at DESC LIMIT 10
        """).fetchall()

        recent_system_activity = [dict(r) for r in recent_audit_rows]

        time_range_title = "Today (24 Hours)" if is_today else (f"Last {days} Days" if days else "All Time")

        return {
            "days": days,
            "time_range": time_range_title,
            "metrics": {
                "users": {
                    "total": u_total,
                    "active": u_active,
                    "disabled": u_disabled,
                    "new_in_period": u_new_period
                },
                "licenses": {
                    "total": l_total,
                    "active": l_active,
                    "expired": l_expired,
                    "suspended": l_suspended,
                    "revoked": l_revoked,
                    "issued_in_period": l_new_period
                },
                "downloads": {
                    "total": dl_total,
                    "completed": dl_completed,
                    "failed": dl_failed,
                    "skipped": dl_skipped,
                    "active": dl_active,
                    "success_rate": success_rate,
                    "mp4_count": mp4_total,
                    "mp3_count": mp3_total,
                    "hd_count": hd_total
                },
                "license_requests": {
                    "total": req_total,
                    "pending": req_pending,
                    "approved": req_approved,
                    "rejected": req_rejected
                },
                "feedback": {
                    "total": fb_total,
                    "open": fb_open,
                    "in_progress": fb_in_progress,
                    "resolved": fb_resolved
                },
                "storage": {
                    "bytes": storage_bytes,
                    "formatted": storage_formatted
                }
            },
            "timeline": {
                "labels": labels,
                "total": timeline_total,
                "completed": timeline_completed,
                "failed": timeline_failed
            },
            "daily_downloads": daily_downloads,
            "download_status_distribution": {r["status"]: r["count"] for r in status_rows},
            "license_distribution": {r["status"]: r["count"] for r in lic_rows},
            "user_status_distribution": {r["status"]: r["count"] for r in user_rows},
            "format_distribution": {
                "mp4": mp4_total,
                "mp3": mp3_total
            },
            "top_creators": top_creators,
            "recent_platform_downloads": recent_platform_downloads,
            "recent_system_activity": recent_system_activity
        }


# =========================================================================
# Feedback System Database Operations (Sections 35-66)
# =========================================================================

def create_feedback_record(data: Dict[str, Any]) -> Dict[str, Any]:
    """Store new feedback record in database and return normalized dict."""
    feedback_id = data.get("id") or str(uuid.uuid4())
    user_id = data["user_id"]
    fb_type = data.get("type", "General Feedback")
    subject = data.get("subject", "").strip()
    message = data.get("message", "").strip()
    rating = data.get("rating")
    status = (data.get("status") or "OPEN").upper()
    priority = (data.get("priority") or "MEDIUM").upper()
    attachment_path = data.get("attachment_path")
    attachment_url = data.get("attachment_url")

    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO feedback (
                id, user_id, type, subject, message, rating, status, priority,
                attachment_path, attachment_url, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (
            feedback_id, user_id, fb_type, subject, message, rating, status, priority,
            attachment_path, attachment_url
        ))
        conn.commit()

    # Record audit log
    add_feedback_audit_log(
        feedback_id=feedback_id,
        actor_id=user_id,
        action="FEEDBACK_CREATED",
        new_value=f"Status: {status}, Priority: {priority}"
    )

    rec = get_feedback_by_id(feedback_id)
    return rec or {}

def get_feedback_by_id(feedback_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieve feedback record with joined user and admin details."""
    query = """
        SELECT f.*,
               u.name as user_name, u.email as user_email, u.username as user_username,
               a.name as assigned_admin_name, a.email as assigned_admin_email,
               ra.name as resolved_by_admin_name, ra.email as resolved_by_admin_email
        FROM feedback f
        LEFT JOIN users u ON f.user_id = u.id
        LEFT JOIN admins a ON f.assigned_admin_id = a.id
        LEFT JOIN admins ra ON f.resolved_by_admin_id = ra.id
        WHERE f.id = ?
    """
    params = [feedback_id]
    if user_id:
        query += " AND f.user_id = ?"
        params.append(user_id)

    with get_db_connection() as conn:
        row = conn.execute(query, params).fetchone()
        if not row:
            return None
        res = dict(row)
        res["audit_logs"] = get_feedback_audit_logs(feedback_id)
        return res

def get_user_feedback_list(user_id: str) -> List[Dict[str, Any]]:
    """Retrieve all feedback submitted by a specific authenticated user."""
    query = """
        SELECT f.*,
               u.name as user_name, u.email as user_email,
               a.name as assigned_admin_name
        FROM feedback f
        LEFT JOIN users u ON f.user_id = u.id
        LEFT JOIN admins a ON f.assigned_admin_id = a.id
        WHERE f.user_id = ?
        ORDER BY f.created_at DESC
    """
    with get_db_connection() as conn:
        rows = conn.execute(query, [user_id]).fetchall()
        return [dict(r) for r in rows]

def get_all_feedback_admin(
    search: Optional[str] = None,
    type_filter: Optional[str] = None,
    status_filter: Optional[str] = None,
    priority_filter: Optional[str] = None,
    assigned_admin_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """Retrieve feedback list for Admin Panel with full multi-criteria search and filter."""
    query = """
        SELECT f.*,
               u.name as user_name, u.email as user_email, u.username as user_username,
               a.name as assigned_admin_name, a.email as assigned_admin_email,
               ra.name as resolved_by_admin_name
        FROM feedback f
        LEFT JOIN users u ON f.user_id = u.id
        LEFT JOIN admins a ON f.assigned_admin_id = a.id
        LEFT JOIN admins ra ON f.resolved_by_admin_id = ra.id
        WHERE 1=1
    """
    params = []
    if search:
        s_term = f"%{search.strip()}%"
        query += " AND (f.subject LIKE ? OR f.message LIKE ? OR u.name LIKE ? OR u.email LIKE ? OR u.username LIKE ?)"
        params.extend([s_term, s_term, s_term, s_term, s_term])
    if type_filter:
        query += " AND f.type = ?"
        params.append(type_filter)
    if status_filter:
        query += " AND f.status = ?"
        params.append(status_filter.upper())
    if priority_filter:
        query += " AND f.priority = ?"
        params.append(priority_filter.upper())
    if assigned_admin_id:
        query += " AND f.assigned_admin_id = ?"
        params.append(assigned_admin_id)
    if date_from:
        query += " AND date(f.created_at) >= date(?)"
        params.append(date_from)
    if date_to:
        query += " AND date(f.created_at) <= date(?)"
        params.append(date_to)

    query += " ORDER BY f.created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

def get_feedback_metrics() -> Dict[str, int]:
    """Return live dynamic counts for feedback statuses from database."""
    with get_db_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        open_cnt = conn.execute("SELECT COUNT(*) FROM feedback WHERE status = 'OPEN'").fetchone()[0]
        in_prog = conn.execute("SELECT COUNT(*) FROM feedback WHERE status = 'IN_PROGRESS'").fetchone()[0]
        resolved = conn.execute("SELECT COUNT(*) FROM feedback WHERE status = 'RESOLVED'").fetchone()[0]
        closed = conn.execute("SELECT COUNT(*) FROM feedback WHERE status = 'CLOSED'").fetchone()[0]
        return {
            "total": total,
            "open": open_cnt,
            "in_progress": in_prog,
            "resolved": resolved,
            "closed": closed
        }

def update_feedback_record(feedback_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update feedback record fields with validation and timestamp refresh."""
    allowed_fields = [
        "type", "subject", "message", "rating", "status", "priority",
        "admin_response", "assigned_admin_id", "resolved_at", "resolved_by_admin_id",
        "attachment_path", "attachment_url"
    ]
    set_clauses = []
    params = []
    for k, v in updates.items():
        if k in allowed_fields:
            set_clauses.append(f"{k} = ?")
            params.append(v)
    if not set_clauses:
        return get_feedback_by_id(feedback_id)

    set_clauses.append("updated_at = CURRENT_TIMESTAMP")
    params.append(feedback_id)

    with get_db_connection() as conn:
        conn.execute(f"UPDATE feedback SET {', '.join(set_clauses)} WHERE id = ?", params)
        conn.commit()

    return get_feedback_by_id(feedback_id)

def add_feedback_audit_log(
    feedback_id: str,
    actor_id: str,
    action: str,
    previous_value: Optional[str] = None,
    new_value: Optional[str] = None,
    admin_id: Optional[str] = None,
    actor_email: Optional[str] = None
):
    """Insert immutable audit log for feedback events."""
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO feedback_audit_logs (
                feedback_id, actor_id, actor_email, admin_id, action, previous_value, new_value, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            feedback_id, actor_id, actor_email, admin_id, action, previous_value, new_value
        ))
        conn.commit()

def get_feedback_audit_logs(feedback_id: str) -> List[Dict[str, Any]]:
    """Retrieve audit trail for a specific feedback item."""
    query = """
        SELECT l.*,
               a.name as admin_name, a.email as admin_email
        FROM feedback_audit_logs l
        LEFT JOIN admins a ON l.admin_id = a.id
        WHERE l.feedback_id = ?
        ORDER BY l.timestamp ASC, l.id ASC
    """
    with get_db_connection() as conn:
        rows = conn.execute(query, [feedback_id]).fetchall()
        return [dict(r) for r in rows]

# =========================================================================
# Download Worker Concurrency & Atomic Claiming Helpers
# =========================================================================

def claim_download_job(video_id: str, worker_id: int, user_id: str = "default_user") -> bool:
    """
    Atomically claim a queued download job for a specific worker.
    Returns True if successfully claimed, False if already claimed by another worker.
    """
    with get_db_connection() as conn:
        cursor = conn.execute("""
            UPDATE downloads
            SET status = 'downloading',
                worker_id = ?,
                started_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE video_id = ? AND user_id = ? AND status IN ('queued', 'retrying')
        """, (str(worker_id), video_id, user_id))
        conn.commit()
        return cursor.rowcount > 0

def get_active_job(user_id: str, video_id: str, quality: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Duplicate protection check: returns active job if already queued, downloading,
    retrying, verifying, or moving.
    """
    with get_db_connection() as conn:
        if quality:
            row = conn.execute("""
                SELECT * FROM downloads
                WHERE user_id = ? AND video_id = ? AND quality = ?
                  AND status IN ('queued', 'downloading', 'retrying', 'verifying', 'moving')
            """, (user_id, video_id, quality)).fetchone()
        else:
            row = conn.execute("""
                SELECT * FROM downloads
                WHERE user_id = ? AND video_id = ?
                  AND status IN ('queued', 'downloading', 'retrying', 'verifying', 'moving')
            """, (user_id, video_id)).fetchone()
        return _normalize_download_dict(row)

def update_download_retry_db(
    video_id: str,
    user_id: str,
    attempt_number: int,
    provider: str,
    worker_id: Optional[int] = None
):
    """Update attempt number and provider info for a running download."""
    with get_db_connection() as conn:
        conn.execute("""
            UPDATE downloads
            SET attempt_number = ?,
                provider = ?,
                worker_id = COALESCE(?, worker_id),
                updated_at = CURRENT_TIMESTAMP
            WHERE video_id = ? AND user_id = ?
        """, (attempt_number, provider, str(worker_id) if worker_id else None, video_id, user_id))
        conn.commit()

def update_download_progress_db(
    video_id: str,
    user_id: str,
    progress: float,
    status: Optional[str] = None,
    error_message: Optional[str] = None
):
    """Update progress percentage and optional status in database."""
    with get_db_connection() as conn:
        if status:
            conn.execute("""
                UPDATE downloads
                SET progress = ?,
                    status = ?,
                    error_message = COALESCE(?, error_message),
                    updated_at = CURRENT_TIMESTAMP
                WHERE video_id = ? AND user_id = ?
            """, (progress, status, error_message, video_id, user_id))
        else:
            conn.execute("""
                UPDATE downloads
                SET progress = ?,
                    error_message = COALESCE(?, error_message),
                    updated_at = CURRENT_TIMESTAMP
                WHERE video_id = ? AND user_id = ?
            """, (progress, error_message, video_id, user_id))
        conn.commit()

def recover_stale_downloads() -> List[Dict[str, Any]]:
    """
    Recover orphaned downloads on application startup or worker restart.
    - If physical file exists on disk -> mark completed.
    - If file missing and was downloading/verifying/moving/retrying/queued -> reset to queued.
    Returns list of jobs to re-enqueue.
    """
    recovered: List[Dict[str, Any]] = []
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM downloads
            WHERE status IN ('downloading', 'verifying', 'moving', 'retrying', 'queued')
        """).fetchall()
        for r in rows:
            d = dict(r)
            fp = d.get("filepath")
            if fp and Path(fp).exists() and Path(fp).is_file() and Path(fp).stat().st_size > 0:
                conn.execute("UPDATE downloads SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE id = ?", (d["id"],))
            else:
                conn.execute("""
                    UPDATE downloads
                    SET status = 'queued',
                        worker_id = NULL,
                        progress = 0.0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (d["id"],))
                recovered.append(d)
        conn.commit()
    return recovered

def get_download_stats_counts() -> Dict[str, int]:
    """Return live counts of downloads by status."""
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT status, COUNT(*) as cnt
            FROM downloads
            GROUP BY status
        """).fetchall()
        counts = {
            "total": 0,
            "completed": 0,
            "queued": 0,
            "downloading": 0,
            "retrying": 0,
            "failed": 0,
            "skipped": 0
        }
        for r in rows:
            st = (r["status"] or "").lower()
            cnt = r["cnt"]
            counts["total"] += cnt
            if st in ("completed", "downloaded"):
                counts["completed"] += cnt
            elif st in counts:
                counts[st] += cnt
        return counts

NETWORK_ERROR_PATTERNS = [
    "name resolution",
    "resolve host",
    "curl: (6)",
    "curl: (7)",
    "curl: (28)",
    "connection refused",
    "connection reset",
    "timed out",
    "timeout",
    "unreachable",
    "tertiary fallback provider could not obtain playable stream url",
    "secondary api provider could not resolve direct video stream",
    "stream connection closed with 0 bytes",
    "network drop",
    "network interrupted",
    "network error",
    "transporterror",
    "transport error",
    "reconnecting",
    "temporary failure",
    "errno -3",
    "errno -2",
    "errno 110",
    "errno 111",
]

def is_network_error_message(err_msg: Optional[str]) -> bool:
    """Return True if error indicates network drop, DNS failure, or connection interruption."""
    if not err_msg:
        return False
    msg_lower = str(err_msg).lower()
    return any(p in msg_lower for p in NETWORK_ERROR_PATTERNS)

def recover_network_stopped_downloads(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Recover all downloads and queue messages stopped or failed due to network issues.
    Resets status to 'queued', clears error message, resets attempt count to 1,
    and returns list of download records ready to be pushed back onto the queue.
    """
    recovered: List[Dict[str, Any]] = []
    with get_db_connection() as conn:
        query = "SELECT * FROM downloads WHERE status IN ('failed', 'stopped', 'interrupted', 'paused', 'queued')"
        params: List[Any] = []
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        
        rows = conn.execute(query, tuple(params)).fetchall()
        for r in rows:
            d = dict(r)
            status = (d.get("status") or "").lower()
            err_msg = d.get("error_message") or ""
            
            is_net_issue = (
                status in ("stopped", "interrupted", "paused")
                or status == "queued"
                or (status == "failed" and (is_network_error_message(err_msg) or not err_msg))
            )
            
            if is_net_issue:
                fp = d.get("filepath")
                if fp and Path(fp).exists() and Path(fp).is_file() and Path(fp).stat().st_size > 0:
                    conn.execute("UPDATE downloads SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE id = ?", (d["id"],))
                else:
                    conn.execute("""
                        UPDATE downloads
                        SET status = 'queued',
                            attempt_number = 1,
                            error_message = NULL,
                            worker_id = NULL,
                            progress = 0.0,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (d["id"],))
                    
                    vid = d.get("video_id")
                    if vid:
                        conn.execute("""
                            UPDATE batch_items
                            SET status = 'Queued',
                                error_message = NULL,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE video_id = ? AND status IN ('Failed', 'Stopped', 'Interrupted')
                        """, (str(vid),))
                    
                    recovered.append(d)
        conn.commit()
    return recovered

def get_network_stopped_count(user_id: Optional[str] = None) -> int:
    """Return count of downloads currently in stopped/network-failed state."""
    with get_db_connection() as conn:
        query = "SELECT error_message, status FROM downloads WHERE status IN ('failed', 'stopped', 'interrupted', 'paused')"
        params: List[Any] = []
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        rows = conn.execute(query, tuple(params)).fetchall()
        count = 0
        for r in rows:
            st = (r["status"] or "").lower()
            err = r["error_message"] or ""
            if st in ("stopped", "interrupted", "paused") or is_network_error_message(err) or not err:
                count += 1
        return count


