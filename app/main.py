"""
Musician & Song Request Web Application (SnowGuin / musiciantradethai.com style)
Core Features:
1. Real Slip / Payment Verification (SlipOK API + Local Mini-QR Parser + Anti-Duplicate Detection)
2. Song Queue Management (Tip Priority, Time FIFO, Drag-and-Drop Manual Reorder)
3. Live WebSocket updates across Audience, Musician Dashboard, and Stage Screen
"""

import os
import io
import time
import json
import uuid
import datetime
from typing import List

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import qrcode

from app.database import get_db, init_db
from app.promptpay import generate_promptpay_payload
from app.slip_verifier import verify_slip, THAI_BANKS

app = FastAPI(title="Musician Song Request Platform", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uploads")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# -------------------------------------------------------------
# WebSocket Connection Manager for Real-Time Synchronization
# -------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        text_data = json.dumps(message)
        dead = []
        for connection in self.active_connections:
            try:
                await connection.send_text(text_data)
            except Exception:
                dead.append(connection)
        for d in dead:
            if d in self.active_connections:
                self.active_connections.remove(d)


ws_manager = ConnectionManager()


@app.on_event("startup")
def on_startup():
    init_db()


# -------------------------------------------------------------
# Helper: Queue Re-Ordering based on Sort Mode
# -------------------------------------------------------------
def recalculate_queue_order(cursor, band_id: int, mode: str):
    """
    Recalculates queue_order for active 'queued' requests.
    - 'tip_priority': highest tip amount first, then oldest request
    - 'fifo_time': oldest request first (standard queue)
    - 'manual': keeps current order
    """
    if mode == "tip_priority":
        cursor.execute("""
            SELECT id FROM requests 
            WHERE band_id = ? AND status = 'queued'
            ORDER BY tip_amount DESC, created_at ASC
        """, (band_id,))
        rows = cursor.fetchall()
        for idx, r in enumerate(rows):
            cursor.execute("UPDATE requests SET queue_order = ? WHERE id = ?", (idx, r["id"]))
    elif mode == "fifo_time":
        cursor.execute("""
            SELECT id FROM requests 
            WHERE band_id = ? AND status = 'queued'
            ORDER BY created_at ASC
        """, (band_id,))
        rows = cursor.fetchall()
        for idx, r in enumerate(rows):
            cursor.execute("UPDATE requests SET queue_order = ? WHERE id = ?", (idx, r["id"]))


# -------------------------------------------------------------
# Public & Audience Endpoints
# -------------------------------------------------------------
@app.get("/api/band")
def get_band_info():
    """Returns band information, PromptPay setup, and active session status."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM band_profiles WHERE slug = 'default'")
        band = cursor.fetchone()
        if not band:
            raise HTTPException(status_code=404, detail="Band not found")
            
        cursor.execute("SELECT COUNT(*) as count FROM requests WHERE band_id = ? AND status = 'queued'", (band["id"],))
        queued_count = cursor.fetchone()["count"]
        
        cursor.execute("SELECT * FROM requests WHERE band_id = ? AND status = 'playing' LIMIT 1", (band["id"],))
        now_playing = cursor.fetchone()
        
        band_dict = dict(band)
        # Hide private API keys from public output
        band_dict.pop("slipok_api_key", None)
        band_dict.pop("slipok_branch_id", None)
        band_dict["queued_count"] = queued_count
        band_dict["now_playing"] = dict(now_playing) if now_playing else None
        return band_dict


@app.get("/api/songs")
def list_songs(q: str = "", genre: str = ""):
    """Lists available songs in band's repertoire with search & genre filter."""
    with get_db() as conn:
        cursor = conn.cursor()
        query = "SELECT * FROM songs WHERE is_available = 1"
        params = []
        if q:
            query += " AND (title LIKE ? OR artist LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%"])
        if genre and genre != "all":
            query += " AND genre = ?"
            params.append(genre)
        query += " ORDER BY times_requested DESC, title ASC"
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


@app.get("/api/queue")
def get_public_queue():
    """Returns current active queue for audience to see."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, song_title, artist, sender_name, table_no, tip_amount, 
                   status, queue_order, created_at, dedication_note
            FROM requests 
            WHERE status IN ('queued', 'playing')
            ORDER BY CASE WHEN status = 'playing' THEN 0 ELSE 1 END, queue_order ASC, created_at ASC
        """)
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


@app.get("/api/promptpay-qr")
def get_promptpay_qr(amount: float = 0.0):
    """Generates dynamic Thai PromptPay QR code image."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT promptpay_number FROM band_profiles WHERE slug = 'default'")
        band = cursor.fetchone()
        target = band["promptpay_number"] if band else "0891234567"

    payload = generate_promptpay_payload(target, amount if amount > 0 else None)
    
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0f172a", back_color="white")
    
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/api/table-qr")
def get_table_qr(table: str = "1", base_url: str = ""):
    """Generates QR code that links directly to table's song request URL."""
    base = base_url.strip() or "https://tuiduimusic.onrender.com"
    target_url = f"{base}/?table={table.strip()}"
    
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(target_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#180424", back_color="white")
    
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png")


@app.post("/api/request")
async def submit_song_request(
    song_title: str = Form(...),
    artist: str = Form(""),
    sender_name: str = Form(""),
    table_no: str = Form(""),
    dedication_note: str = Form(""),
    tip_amount: float = Form(0.0),
    slip: UploadFile = File(None)
):
    """
    Submit a song request.
    Verifies bank transfer slip if tip is provided:
    - Scans Thai Bank Mini-QR
    - Calls SlipOK Live API if configured
    - Detects duplicate slip reuse (anti-fraud)
    - Automatically sorts into queue according to band's sort mode
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM band_profiles WHERE slug = 'default'")
        band = cursor.fetchone()
        if not band or not band["is_accepting_requests"]:
            raise HTTPException(status_code=400, detail="ขณะนี้ทางวงปิดรับขอเพลงชั่วคราว")

        min_tip = float(band["min_tip"])
        if tip_amount < min_tip and min_tip > 0:
            raise HTTPException(
                status_code=400, 
                detail=f"ทางวงกำหนดยอดทิปขั้นต่ำสำหรับขอเพลงที่ {min_tip:.2f} บาท"
            )

        slip_filename = None
        verification_result = None
        slip_verified = 0
        slip_trans_ref = None
        slip_bank = None
        slip_method = None
        slip_msg = None
        status = "queued"

        # If tip > 0, slip is required and must be verified
        if tip_amount > 0:
            if not slip or not slip.filename:
                raise HTTPException(status_code=400, detail="กรุณาแนบสลิปโอนเงินสำหรับยอดทิป")

            image_bytes = await slip.read()
            if len(image_bytes) < 100:
                raise HTTPException(status_code=400, detail="ไฟล์สลิปไม่สมบูรณ์")

            # Save uploaded slip
            ext = os.path.splitext(slip.filename)[1] or ".jpg"
            slip_filename = f"slip_{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
            slip_path = os.path.join(UPLOAD_DIR, slip_filename)
            with open(slip_path, "wb") as f:
                f.write(image_bytes)

            # Get existing trans_refs for duplicate prevention
            cursor.execute("SELECT trans_ref FROM slip_logs")
            existing_refs = {r["trans_ref"] for r in cursor.fetchall() if r["trans_ref"]}

            # Run slip verification engine
            verification_result = await verify_slip(
                image_bytes=image_bytes,
                expected_amount=tip_amount,
                band_settings=dict(band),
                existing_trans_refs=existing_refs
            )

            slip_verified = 1 if verification_result.is_valid else 0
            slip_trans_ref = verification_result.trans_ref
            slip_bank = verification_result.bank_name
            slip_method = verification_result.verification_method
            slip_msg = verification_result.message

            # If duplicate or completely invalid
            if verification_result.status == "DUPLICATE":
                raise HTTPException(
                    status_code=400, 
                    detail=f"สลิปนี้เคยถูกใช้งานไปแล้ว (TransRef: {slip_trans_ref}) ไม่อนุญาตให้ใช้สลิปซ้ำ"
                )
            elif verification_result.status == "AMOUNT_MISMATCH":
                raise HTTPException(status_code=400, detail=verification_result.message)
            elif verification_result.status in ("WRONG_RECEIVER", "EXPIRED_SLIP", "INVALID_SLIP", "API_ERROR", "CONFIG_ERROR", "NO_DATA"):
                raise HTTPException(status_code=400, detail=verification_result.message)
            elif verification_result.status in ("MANUAL_REVIEW", "SUSPICIOUS", "NO_QR"):
                if not band["demo_mode"]:
                    raise HTTPException(
                        status_code=400, 
                        detail=verification_result.message or "ไม่พบ QR Code บนสลิป กรุณาอัปโหลดสลิปธนาคารที่มี Mini-QR ชัดเจน"
                    )
                status = "queued"

        # Determine queue order
        cursor.execute("SELECT COUNT(*) as cnt FROM requests WHERE band_id = ? AND status = 'queued'", (band["id"],))
        current_cnt = cursor.fetchone()["cnt"]

        cursor.execute("""
            INSERT INTO requests (
                band_id, song_title, artist, sender_name, table_no, dedication_note,
                tip_amount, status, queue_order, slip_filename, slip_verified,
                slip_trans_ref, slip_bank, slip_verification_method, slip_verification_msg
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            band["id"], song_title.strip(), artist.strip(), sender_name.strip() or "ผู้ฟังใจดี",
            table_no.strip() or "-", dedication_note.strip(), tip_amount, status, current_cnt,
            slip_filename, slip_verified, slip_trans_ref, slip_bank, slip_method, slip_msg
        ))
        request_id = cursor.lastrowid

        # Record in slip_logs if trans_ref is present to prevent future reuse
        if slip_trans_ref:
            try:
                cursor.execute("""
                    INSERT INTO slip_logs (trans_ref, bank_code, amount, request_id)
                    VALUES (?, ?, ?, ?)
                """, (slip_trans_ref, verification_result.bank_code if verification_result else "", tip_amount, request_id))
            except Exception:
                pass

        # Increment song count in catalog if matches
        cursor.execute("""
            UPDATE songs SET times_requested = times_requested + 1 
            WHERE band_id = ? AND LOWER(title) = LOWER(?)
        """, (band["id"], song_title.strip()))

        # Recalculate queue ordering based on sort mode
        recalculate_queue_order(cursor, band["id"], band["sort_mode"])

    # Broadcast real-time update
    await ws_manager.broadcast({
        "event": "NEW_REQUEST",
        "data": {
            "id": request_id,
            "song_title": song_title,
            "artist": artist,
            "table_no": table_no,
            "sender_name": sender_name,
            "tip_amount": tip_amount,
            "slip_verified": slip_verified,
            "status": status,
            "message": slip_msg
        }
    })

    return {
        "success": True,
        "request_id": request_id,
        "status": status,
        "slip_verified": bool(slip_verified),
        "verification_result": verification_result.to_dict() if verification_result else None,
        "message": "ส่งคำขอเพลงสำเร็จ! คำขอของคุณเข้าสู่ระบบคิวเรียบร้อยแล้ว"
    }


@app.get("/api/request/{req_id}")
def check_request_status(req_id: int):
    """Audience phone polling to check specific request's live status and position."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM requests WHERE id = ?", (req_id,))
        req = cursor.fetchone()
        if not req:
            raise HTTPException(status_code=404, detail="Request not found")

        req_dict = dict(req)
        if req_dict["status"] == "queued":
            # Calculate position ahead in queue
            cursor.execute("""
                SELECT COUNT(*) as ahead FROM requests 
                WHERE band_id = ? AND status = 'queued' AND queue_order < ?
            """, (req_dict["band_id"], req_dict["queue_order"]))
            ahead = cursor.fetchone()["ahead"]
            req_dict["position_in_queue"] = ahead + 1
        elif req_dict["status"] == "playing":
            req_dict["position_in_queue"] = 0
            req_dict["stage_status"] = "NOW_PLAYING"
        else:
            req_dict["position_in_queue"] = None

        return req_dict


# -------------------------------------------------------------
# Musician / Admin Dashboard Endpoints
# -------------------------------------------------------------
@app.get("/api/musician/queue")
def musician_get_queue():
    """Musician live queue list with all verification flags and controls."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM band_profiles WHERE slug = 'default'")
        band = cursor.fetchone()
        band_id = band["id"] if band else 1

        cursor.execute("""
            SELECT * FROM requests 
            WHERE band_id = ? AND status IN ('queued', 'playing', 'pending_slip')
            ORDER BY CASE 
                WHEN status = 'playing' THEN 0 
                WHEN status = 'pending_slip' THEN 1
                ELSE 2 
            END, queue_order ASC, created_at ASC
        """, (band_id,))
        active_requests = [dict(r) for r in cursor.fetchall()]

        cursor.execute("""
            SELECT * FROM requests 
            WHERE band_id = ? AND status IN ('completed', 'rejected')
            ORDER BY COALESCE(played_at, created_at) DESC
            LIMIT 30
        """, (band_id,))
        history_requests = [dict(r) for r in cursor.fetchall()]

        return {
            "active": active_requests,
            "history": history_requests,
            "sort_mode": band["sort_mode"] if band else "tip_priority"
        }


@app.post("/api/musician/request/{req_id}/action")
async def musician_action(req_id: int, payload: dict):
    """
    Musician stage controls:
    - action: 'play' -> start playing this song
    - action: 'complete' -> mark as finished
    - action: 'reject' -> reject song (with optional reason)
    - action: 'approve_slip' -> manual slip approval
    - action: 'move_up' / 'move_down' -> adjust queue order
    """
    action = payload.get("action")
    reason = payload.get("reason", "")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM requests WHERE id = ?", (req_id,))
        req = cursor.fetchone()
        if not req:
            raise HTTPException(status_code=404, detail="Request not found")

        band_id = req["band_id"]

        if action == "play":
            # If there's currently another song playing, complete it
            cursor.execute("""
                UPDATE requests 
                SET status = 'completed', played_at = CURRENT_TIMESTAMP 
                WHERE band_id = ? AND status = 'playing'
            """, (band_id,))

            # Set this song to playing
            cursor.execute("""
                UPDATE requests 
                SET status = 'playing', played_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            """, (req_id,))
            
        elif action == "complete":
            cursor.execute("""
                UPDATE requests 
                SET status = 'completed', played_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            """, (req_id,))
            
        elif action == "reject":
            cursor.execute("""
                UPDATE requests 
                SET status = 'rejected', rejection_reason = ? 
                WHERE id = ?
            """, (reason or "ทางวงไม่สามารถเล่นเพลงนี้ได้ในรอบนี้", req_id))
            
        elif action == "approve_slip":
            cursor.execute("""
                UPDATE requests 
                SET slip_verified = 1, status = 'queued', 
                    slip_verification_msg = 'อนุมัติสลิปด้วยตนเองโดยนักดนตรี'
                WHERE id = ?
            """, (req_id,))
            
        elif action == "requeue":
            cursor.execute("""
                UPDATE requests SET status = 'queued' WHERE id = ?
            """, (req_id,))

        # Refresh queue ordering
        cursor.execute("SELECT sort_mode FROM band_profiles WHERE id = ?", (band_id,))
        sort_mode = cursor.fetchone()["sort_mode"]
        if sort_mode != "manual":
            recalculate_queue_order(cursor, band_id, sort_mode)

    await ws_manager.broadcast({
        "event": "QUEUE_UPDATED",
        "action": action,
        "req_id": req_id
    })

    return {"success": True, "action": action, "req_id": req_id}


@app.post("/api/musician/queue/reorder")
async def musician_reorder_queue(payload: dict):
    """
    Manual Drag & Drop or Custom Reordering of the song queue.
    Accepts ordered list of request IDs: [5, 2, 8, 1].
    Switches sort_mode to 'manual' so custom arrangement is preserved.
    """
    ordered_ids = payload.get("ordered_ids", [])
    if not ordered_ids:
        return {"success": False, "message": "No IDs provided"}

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM band_profiles WHERE slug = 'default'")
        band_id = cursor.fetchone()["id"]

        for idx, req_id in enumerate(ordered_ids):
            cursor.execute("""
                UPDATE requests 
                SET queue_order = ? 
                WHERE id = ? AND band_id = ?
            """, (idx, req_id, band_id))

        # Update sort_mode to manual
        cursor.execute("UPDATE band_profiles SET sort_mode = 'manual' WHERE id = ?", (band_id,))

    await ws_manager.broadcast({"event": "QUEUE_UPDATED", "action": "reorder"})
    return {"success": True, "message": "จัดเรียงคิวเพลงใหม่เรียบร้อยแล้ว"}


@app.post("/api/musician/settings/sort-mode")
async def update_sort_mode(payload: dict):
    """Switches sorting mode: 'tip_priority', 'fifo_time', or 'manual'."""
    mode = payload.get("mode")
    if mode not in ("tip_priority", "fifo_time", "manual"):
        raise HTTPException(status_code=400, detail="Invalid sort mode")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE band_profiles SET sort_mode = ? WHERE slug = 'default'", (mode,))
        cursor.execute("SELECT id FROM band_profiles WHERE slug = 'default'")
        band_id = cursor.fetchone()["id"]
        recalculate_queue_order(cursor, band_id, mode)

    await ws_manager.broadcast({"event": "QUEUE_UPDATED", "action": "sort_mode_changed", "mode": mode})
    return {"success": True, "mode": mode}


@app.post("/api/musician/settings")
async def update_band_settings(payload: dict):
    """Update Band Profile, PromptPay account, and SlipOK API configurations."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE band_profiles SET
                name = COALESCE(?, name),
                tagline = COALESCE(?, tagline),
                venue_name = COALESCE(?, venue_name),
                promptpay_number = COALESCE(?, promptpay_number),
                promptpay_name = COALESCE(?, promptpay_name),
                min_tip = COALESCE(?, min_tip),
                is_accepting_requests = COALESCE(?, is_accepting_requests),
                slipok_api_key = COALESCE(?, slipok_api_key),
                slipok_branch_id = COALESCE(?, slipok_branch_id),
                demo_mode = COALESCE(?, demo_mode)
            WHERE slug = 'default'
        """, (
            payload.get("name"),
            payload.get("tagline"),
            payload.get("venue_name"),
            payload.get("promptpay_number"),
            payload.get("promptpay_name"),
            payload.get("min_tip"),
            payload.get("is_accepting_requests"),
            payload.get("slipok_api_key"),
            payload.get("slipok_branch_id"),
            payload.get("demo_mode")
        ))

    await ws_manager.broadcast({"event": "BAND_SETTINGS_UPDATED"})
    return {"success": True, "message": "บันทึกการตั้งค่าวและระบบตรวจสลิปเรียบร้อย"}


@app.get("/api/musician/stats")
def get_session_stats():
    """Live session statistics: total tips, total songs, queue breakdown."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM band_profiles WHERE slug = 'default'")
        band_id = cursor.fetchone()["id"]

        cursor.execute("""
            SELECT 
                COUNT(*) as total_requests,
                SUM(tip_amount) as total_tips,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_count,
                SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) as queued_count,
                SUM(CASE WHEN status = 'playing' THEN 1 ELSE 0 END) as playing_count
            FROM requests
            WHERE band_id = ?
        """, (band_id,))
        stats = dict(cursor.fetchone())
        stats["total_tips"] = stats["total_tips"] or 0.0

        # Top requested songs tonight
        cursor.execute("""
            SELECT song_title, artist, COUNT(*) as count, SUM(tip_amount) as tips
            FROM requests 
            WHERE band_id = ?
            GROUP BY song_title, artist
            ORDER BY tips DESC, count DESC
            LIMIT 5
        """, (band_id,))
        top_songs = [dict(r) for r in cursor.fetchall()]
        stats["top_songs"] = top_songs

        return stats


@app.post("/api/musician/songs")
def add_song(payload: dict):
    """Add a new song to repertoire."""
    title = payload.get("title", "").strip()
    artist = payload.get("artist", "").strip()
    genre = payload.get("genre", "Pop").strip()
    key = payload.get("key", "").strip()

    if not title:
        raise HTTPException(status_code=400, detail="Song title is required")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM band_profiles WHERE slug = 'default'")
        band_id = cursor.fetchone()["id"]

        cursor.execute("""
            INSERT INTO songs (band_id, title, artist, genre, key, is_available)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (band_id, title, artist, genre, key))
        song_id = cursor.lastrowid

    return {"success": True, "song_id": song_id, "title": title}


@app.patch("/api/musician/songs/{song_id}/toggle")
def toggle_song(song_id: int):
    """Toggle song availability (available / temporarily resting)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE songs SET is_available = 1 - is_available WHERE id = ?", (song_id,))
        cursor.execute("SELECT is_available FROM songs WHERE id = ?", (song_id,))
        row = cursor.fetchone()
        return {"success": True, "is_available": row["is_available"]}


@app.delete("/api/musician/songs/{song_id}")
def delete_song(song_id: int):
    """Delete a song from repertoire."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM songs WHERE id = ?", (song_id,))
        return {"success": True}


# -------------------------------------------------------------
# WebSocket Endpoint
# -------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Handle client heartbeats or custom pings
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


# -------------------------------------------------------------
# HTML Pages
# -------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index_page():
    """Audience Request Song Portal."""
    html_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return "<h1>Song Request Platform</h1>"


@app.get("/musician", response_class=HTMLResponse)
def musician_page():
    """Musician Stage & Queue Control Dashboard."""
    html_path = os.path.join(STATIC_DIR, "musician.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return "<h1>Musician Dashboard</h1>"


@app.get("/stage", response_class=HTMLResponse)
def stage_page():
    """Stage Display / TV Screen / OBS Overlay."""
    html_path = os.path.join(STATIC_DIR, "stage.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return "<h1>Stage View</h1>"


# -------------------------------------------------------------
# SEO: robots.txt & sitemap.xml for Google Indexing
# -------------------------------------------------------------
@app.get("/robots.txt", response_class=Response)
def robots_txt():
    content = """User-agent: *
Allow: /
Allow: /stage
Disallow: /musician
Disallow: /api/
Disallow: /uploads/

Sitemap: /sitemap.xml
"""
    return Response(content=content, media_type="text/plain")


@app.get("/sitemap.xml", response_class=Response)
def sitemap_xml():
    content = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>/</loc>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>/stage</loc>
    <changefreq>daily</changefreq>
    <priority>0.8</priority>
  </url>
</urlset>
"""
    return Response(content=content, media_type="application/xml")
