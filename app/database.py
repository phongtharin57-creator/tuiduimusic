"""
Database initialization and helpers (SQLite)
Zero-configuration, resilient, and fast for live sessions.
"""

import sqlite3
import os
from contextlib import contextmanager
from app.song_catalog import DEFAULT_SONGS

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data.db")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Initializes tables and seeds default band and songs if empty."""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # 1. Band Profiles
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS band_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE,
                name TEXT NOT NULL,
                tagline TEXT,
                venue_name TEXT,
                promptpay_number TEXT NOT NULL,
                promptpay_name TEXT NOT NULL,
                min_tip REAL DEFAULT 0,
                is_accepting_requests INTEGER DEFAULT 1,
                slipok_api_key TEXT,
                slipok_branch_id TEXT,
                sort_mode TEXT DEFAULT 'tip_priority', -- 'tip_priority', 'fifo_time', 'manual'
                demo_mode INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 2. Songs Repertoire
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                band_id INTEGER,
                title TEXT NOT NULL,
                artist TEXT,
                genre TEXT,
                key TEXT,
                is_available INTEGER DEFAULT 1,
                times_requested INTEGER DEFAULT 0,
                FOREIGN KEY (band_id) REFERENCES band_profiles (id)
            )
        """)
        
        # 3. Requests / Queue
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                band_id INTEGER,
                song_title TEXT NOT NULL,
                artist TEXT,
                sender_name TEXT,
                table_no TEXT,
                dedication_note TEXT,
                tip_amount REAL DEFAULT 0,
                status TEXT DEFAULT 'queued', -- 'pending_slip', 'queued', 'playing', 'completed', 'rejected'
                queue_order INTEGER DEFAULT 0,
                slip_filename TEXT,
                slip_verified INTEGER DEFAULT 0,
                slip_trans_ref TEXT,
                slip_bank TEXT,
                slip_verification_method TEXT,
                slip_verification_msg TEXT,
                rejection_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                played_at TIMESTAMP,
                FOREIGN KEY (band_id) REFERENCES band_profiles (id)
            )
        """)
        
        # 4. Slip Logs (For Anti-Duplicate Replay Attack Prevention)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS slip_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trans_ref TEXT UNIQUE,
                bank_code TEXT,
                amount REAL,
                request_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Check if default band exists
        cursor.execute("SELECT id FROM band_profiles WHERE slug = 'default'")
        band = cursor.fetchone()
        if not band:
            cursor.execute("""
                INSERT INTO band_profiles (
                    slug, name, tagline, venue_name, promptpay_number, promptpay_name,
                    min_tip, is_accepting_requests, sort_mode, demo_mode
                ) VALUES (
                    'default',
                    'The Soundwave Acoustic & Rock Band',
                    'ดนตรีสดฟังสบายและร็อคมันส์ๆ พร้อมรับทุกคำขอเพลง',
                    'The Moonlight Bar & Bistro',
                    '0891234567',
                    'นายธนพล มีดนตรี (วง The Soundwave)',
                    20,
                    1,
                    'tip_priority',
                    1
                )
            """)
            band_id = cursor.lastrowid
            
            # Seed default songs
            for s in DEFAULT_SONGS:
                cursor.execute("""
                    INSERT INTO songs (band_id, title, artist, genre, key, is_available)
                    VALUES (?, ?, ?, ?, ?, 1)
                """, (band_id, s["title"], s["artist"], s["genre"], s.get("key", "")))
                
            # Seed 2 sample requests for instant display
            cursor.execute("""
                INSERT INTO requests (
                    band_id, song_title, artist, sender_name, table_no, dedication_note,
                    tip_amount, status, queue_order, slip_verified, slip_trans_ref, slip_bank,
                    slip_verification_msg
                ) VALUES 
                (?, 'ถ้าเธอรักฉันจริง', 'Three Man Down', 'โต๊ะ 4 น้องพลอย', '4', 'ขอเพลงนี้ให้เพื่อนสนิทหน่อยค่าา 🎉', 100.0, 'queued', 1, 1, 'KBANK202611049281', 'Kasikornbank', 'สลิปถูกต้อง ตรวจพบ QR โอนเข้าบัญชีวงเรียบร้อย'),
                (?, 'คุกเข่า', 'Cocktail', 'โต๊ะ 8 พี่บอย', '8', 'ฉลองโปรเจกต์ผ่าน ขอร็อคๆ มันส์ๆ เลยครับพี่!', 200.0, 'queued', 0, 1, 'SCB202611041920', 'Siam Commercial Bank', 'สลิปถูกต้อง ยอดเงิน 200.00 บาท (ทิปท็อปอันดับ 1)')
            """, (band_id, band_id))
