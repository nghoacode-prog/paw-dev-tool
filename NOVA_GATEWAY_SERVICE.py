# -*- coding: utf-8 -*-
"""PAWNOVA server: license API, dual-game prediction cache and admin panel."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import wraps
from typing import Any

from flask import Flask, jsonify, request, session, send_from_directory

BASE_DIR = Path(__file__).resolve().parent

# Render's filesystem is ephemeral unless a Persistent Disk is mounted. Prefer the
# conventional mounted disk when available, while respecting an explicit operator path.
def _choose_db_path() -> Path:
    configured = os.environ.get("PAWNOVA_DB_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    mount = Path("/var/data")
    if mount.is_dir() and os.access(str(mount), os.W_OK):
        return (mount / "pawnova.sqlite3").resolve()
    return (BASE_DIR / "pawnova.sqlite3").resolve()

DB_PATH = _choose_db_path()
DB_PERSISTENT_PATH_CONFIGURED = str(DB_PATH).startswith("/var/data/")
_BACKUP_LOCK = threading.Lock()
_BACKUP_PATH = DB_PATH.with_name(DB_PATH.name + ".bak")

# One-time, lossless SQLite backup/migration when a new persistent mount is detected.
# This copies a coherent database snapshot (including committed WAL data), never raw WAL files.
def _db_row_count(path: Path, table: str) -> int | None:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    try:
        with sqlite3.connect(str(path), timeout=10) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                return 0
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except Exception:
        return None


def _copy_database_snapshot(source_path: Path, destination_path: Path) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination_path.with_name(destination_path.name + ".restore.tmp")
    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        pass
    src = sqlite3.connect(str(source_path), timeout=30)
    dst = sqlite3.connect(str(tmp), timeout=30)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close(); src.close()
    os.replace(tmp, destination_path)


def _migrate_local_db_if_needed() -> None:
    legacy = (BASE_DIR / "pawnova.sqlite3").resolve()
    # Restore an empty/reset live DB from a previous snapshot rather than silently
    # starting over and making every customer's old key appear invalid.
    if DB_PATH.exists():
        if _BACKUP_PATH.exists():
            live_keys = _db_row_count(DB_PATH, "licenses")
            backup_keys = _db_row_count(_BACKUP_PATH, "licenses")
            if live_keys == 0 and backup_keys is not None and backup_keys > 0:
                _copy_database_snapshot(_BACKUP_PATH, DB_PATH)
                print(f"PAWNOVA restored {backup_keys} licenses from recovery snapshot", flush=True)
        return
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Prefer the last known-good recovery snapshot if the live DB was deleted.
        if _BACKUP_PATH.exists():
            _copy_database_snapshot(_BACKUP_PATH, DB_PATH)
            print(f"PAWNOVA database restored from backup: {_BACKUP_PATH}", flush=True)
            return
        if DB_PATH == legacy or not legacy.exists():
            return
        _copy_database_snapshot(legacy, DB_PATH)
        print(f"PAWNOVA database migrated from {legacy} to {DB_PATH}", flush=True)
    except Exception as exc:
        # Don't silently create an empty replacement if migration failed.
        raise RuntimeError(f"Cannot migrate/restore existing PAWNOVA database: {type(exc).__name__}: {exc}") from exc

def _backup_database() -> None:
    if os.environ.get("PAWNOVA_DB_BACKUP", "true").lower() not in ("1", "true", "yes", "on"):
        return
    if not DB_PATH.exists():
        return
    with _BACKUP_LOCK:
        tmp = DB_PATH.with_name(DB_PATH.name + ".bak.tmp")
        try:
            with sqlite3.connect(str(DB_PATH), timeout=20) as source, sqlite3.connect(str(tmp), timeout=20) as target:
                source.backup(target)
            os.replace(tmp, _BACKUP_PATH)
        except Exception as exc:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            print(f"PAWNOVA database backup failed: {type(exc).__name__}", flush=True)
ADMIN_PASSWORD = os.environ.get("PAWNOVA_ADMIN_PASSWORD", "")
SESSION_SECRET = os.environ.get("PAWNOVA_SESSION_SECRET", "")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() in ("1", "true", "yes", "on")
VN = timezone(timedelta(hours=7))
PACKAGES = {3: 45000, 5: 65000, 7: 85000, 10: 109000}
GAME_URLS = {
    "md5": os.environ.get("PAWNOVA_MD5_URL", "https://wtxmd52.tele68.com/v1/txmd5/sessions"),
    "hu": os.environ.get("PAWNOVA_HU_URL", "https://wtx.tele68.com/v1/tx/sessions"),
}
GAME_LABELS = {"md5": "Bàn MD5", "hu": "Bàn Hũ"}
POLL_INTERVAL = max(3, int(os.environ.get("PAWNOVA_POLL_SECONDS", "5")))

app = Flask(__name__)
app.secret_key = SESSION_SECRET or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=32 * 1024,
)


def now() -> datetime:
    return datetime.now(VN)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextmanager
def db():
    """Open a transaction safely; atomically refresh a local SQLite recovery snapshot after writes."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    changed = False
    committed = False
    try:
        yield conn
        changed = conn.total_changes > 0
        conn.commit()
        committed = True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        if committed and changed:
            _backup_database()

def init_db() -> None:
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            key_hash TEXT UNIQUE NOT NULL,
            key_prefix TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            calls INTEGER NOT NULL DEFAULT 0,
            device_id TEXT,
            telegram_id TEXT,
            order_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT NOT NULL,
            username TEXT,
            package_days INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at TEXT NOT NULL,
            approved_at TEXT,
            license_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS prediction_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_id INTEGER NOT NULL,
            game TEXT NOT NULL,
            source_session_id TEXT,
            target_session_id TEXT NOT NULL,
            prediction TEXT NOT NULL CHECK(prediction IN ('TAI','XIU')),
            confidence REAL,
            created_at TEXT NOT NULL,
            actual TEXT,
            grade TEXT,
            graded_at TEXT,
            UNIQUE(license_id, game, target_session_id)
        );
        CREATE INDEX IF NOT EXISTS idx_prediction_grade ON prediction_logs(game, target_session_id, actual);
        CREATE INDEX IF NOT EXISTS idx_license_expiry ON licenses(active, expires_at);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, created_at);
        CREATE TABLE IF NOT EXISTS bot_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_id INTEGER NOT NULL,
            notice_type TEXT NOT NULL,
            period_key TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            UNIQUE(license_id, notice_type, period_key)
        );
        """)
        # Safe migrations: retain the original licenses/orders/logs and add new columns only.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(licenses)").fetchall()}
        legacy_schema = "duration_days" not in cols or "activated_at" not in cols
        if "duration_days" not in cols:
            conn.execute("ALTER TABLE licenses ADD COLUMN duration_days INTEGER NOT NULL DEFAULT 0")
        if "activated_at" not in cols:
            conn.execute("ALTER TABLE licenses ADD COLUMN activated_at TEXT")
        if "duration_hours" not in cols:
            conn.execute("ALTER TABLE licenses ADD COLUMN duration_hours INTEGER NOT NULL DEFAULT 0")
        # Old records retain their old expiry; infer duration and consider them already active.
        if legacy_schema:
            conn.execute("UPDATE licenses SET duration_days=MAX(1, MIN(3650, CAST(julianday(expires_at)-julianday(created_at) AS INTEGER))) WHERE duration_days=0")
            conn.execute("UPDATE licenses SET activated_at=created_at WHERE activated_at IS NULL")


init_db()
_backup_database()


def event(kind: str, message: str) -> None:
    with db() as conn:
        conn.execute("INSERT INTO events(kind,message,created_at) VALUES(?,?,?)",
                     (str(kind)[:80], str(message)[:500], iso()))


def issue_key(label: str, days: int = 0, telegram_id: str | int | None = None,
              order_id: int | None = None, conn: sqlite3.Connection | None = None,
              duration_hours: int | None = None):
    """Tạo key; đồng hồ bắt đầu ở lần kích hoạt đầu tiên. Hỗ trợ gói theo giờ/ngày."""
    try:
        hours = int(duration_hours) if duration_hours is not None else int(days) * 24
    except (TypeError, ValueError):
        hours = 0
    if not 1 <= hours <= 24 * 3650:
        raise ValueError("Thời hạn key phải từ 1 giờ đến 10 năm")
    duration_days = max(1, (hours + 23) // 24)
    raw = "PNV-" + secrets.token_urlsafe(30)
    created = now()
    expiry = created + timedelta(hours=hours)
    values = (str(label).strip()[:100] or "Khách", digest(raw), raw[:12], iso(created),
              iso(expiry), str(telegram_id) if telegram_id is not None else None, order_id,
              duration_days, hours)
    sql = "INSERT INTO licenses(label,key_hash,key_prefix,created_at,expires_at,active,calls,telegram_id,order_id,duration_days,activated_at,duration_hours) VALUES(?,?,?,?,?,1,0,?,?,?,NULL,?)"
    if conn is not None:
        cur = conn.execute(sql, values)
        return raw, cur.lastrowid, iso(expiry)
    with db() as own:
        cur = own.execute(sql, values)
        return raw, cur.lastrowid, iso(expiry)


def check_license(raw: str | None, device_id: str | None = None, bind: bool = True):
    if not raw or not str(raw).strip():
        return None, "Thiếu key kích hoạt", 401
    raw = str(raw).strip()
    device_id = str(device_id or "").strip()
    if not device_id:
        return None, "Thiếu mã thiết bị", 400
    with db() as conn:
        row = conn.execute("SELECT * FROM licenses WHERE key_hash=?", (digest(raw),)).fetchone()
        if not row:
            return None, "Key không hợp lệ", 403
        if not row["active"]:
            return None, "Key đã bị khóa hoặc thu hồi", 403
        device_id = str(device_id or "").strip()
        if device_id and len(device_id) > 200:
            return None, "Mã thiết bị không hợp lệ", 400
        bound = row["device_id"]
        if bound and not device_id:
            return None, "Thiếu mã thiết bị đã liên kết", 403
        if bound and device_id and not hmac.compare_digest(str(bound), device_id):
            return None, "Key đã gắn với thiết bị khác", 403
        # New licenses start their timer on first successful activation. Legacy rows
        # were migrated with activated_at=created_at, so their original expiry is preserved.
        activated_at = row["activated_at"] if "activated_at" in row.keys() else row["created_at"]
        if activated_at:
            try:
                expiry = datetime.fromisoformat(row["expires_at"])
            except (TypeError, ValueError):
                return None, "Dữ liệu hạn key lỗi", 403
            if expiry <= now():
                return None, "Key đã hết hạn", 403
        elif bind:
            hours = int(row["duration_hours"] or 0) if "duration_hours" in row.keys() else 0
            if not 1 <= hours <= 24 * 3650:
                duration = int(row["duration_days"] or 0) if "duration_days" in row.keys() else 0
                if not 1 <= duration <= 3650:
                    try:
                        old_expiry = datetime.fromisoformat(row["expires_at"])
                        duration = max(1, (old_expiry - datetime.fromisoformat(row["created_at"])).days)
                    except Exception:
                        duration = 3
                hours = duration * 24
            first_activation = now()
            expiry = first_activation + timedelta(hours=hours)
            conn.execute("UPDATE licenses SET device_id=?,activated_at=?,expires_at=?,duration_days=?,duration_hours=? WHERE id=?",
                         (device_id, iso(first_activation), iso(expiry), max(1, (hours+23)//24), hours, row["id"]))
            row = conn.execute("SELECT * FROM licenses WHERE id=?", (row["id"],)).fetchone()
        elif not bound and device_id:
            conn.execute("UPDATE licenses SET device_id=? WHERE id=?", (device_id, row["id"]))
            row = conn.execute("SELECT * FROM licenses WHERE id=?", (row["id"],)).fetchone()
        return dict(row), None, 200


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("admin_ok"):
            return jsonify(ok=False, error="Cần đăng nhập quản trị"), 401
        return fn(*args, **kwargs)
    return wrapped


# A modest per-process brake on repeated password guessing. Render is configured with one worker.
_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_LOCK = threading.Lock()


@app.before_request
def api_preflight():
    if request.method == "OPTIONS" and request.path.startswith("/api/"):
        return ("", 204)
    return None


@app.after_request
def add_security_and_cors_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.path.startswith("/api/"):
        origin = request.headers.get("Origin", "")
        # Public prediction endpoints authenticate with a per-key header, not cookies.
        response.headers["Access-Control-Allow-Origin"] = origin if origin and origin != "null" else "*"
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, X-Device-ID, Accept"
        response.headers["Access-Control-Max-Age"] = "600"
    return response


@app.get("/")
def root():
    return jsonify(ok=True, service="PAWNOVA API", status="online", admin="/admin", health="/health")


@app.get("/health")
def health():
    with db() as conn:
        conn.execute("SELECT 1").fetchone()
    # Warm empty caches on demand as a fallback if the hosting process did not
    # run background workers during import/startup.
    for game in GAME_URLS:
        cache = _prediction_cache[game]
        age = time.monotonic() - cache.get("last_success_monotonic", 0.0)
        if cache.get("data") is None or cache.get("last_error") or age > max(20, POLL_INTERVAL * 4):
            try:
                refresh_prediction(game, force=True)
            except Exception as exc:
                cache["last_error"] = f"health-refresh:{type(exc).__name__}: {str(exc)[:120]}"
    cache_state = {}
    for game in GAME_URLS:
        cache = _prediction_cache[game]
        item = cache.get("data")
        age = time.monotonic() - cache.get("last_success_monotonic", 0.0)
        stale = bool(item and (cache.get("last_error") or age > max(25, POLL_INTERVAL * 4)))
        cache_state[game] = {
            "ready": bool(item),
            "stale": stale,
            "source_session": item.get("session") if item else None,
            "source_checked_at": item.get("source_checked_at") if item else None,
            "last_error": cache.get("last_error"),
        }
    try:
        with db() as conn:
            counts = {"licenses": conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0],
                      "orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]}
    except Exception as exc:
        return jsonify(ok=False, status="degraded", time=iso(), database="error", db_error=type(exc).__name__), 503
    return jsonify(ok=True, status="online", time=iso(), database="ok",
                   storage={"path": str(DB_PATH), "persistent_path_configured": DB_PERSISTENT_PATH_CONFIGURED,
                            "backup_exists": _BACKUP_PATH.exists(), "counts": counts}, predictions=cache_state)


@app.get("/admin")
def admin_page():
    return send_from_directory(BASE_DIR, "NOVA_ADMIN_CONSOLE.html")


@app.get("/activate")
def activation_page():
    return send_from_directory(BASE_DIR, "NOVA_KEY_ACTIVATION.html")


@app.post("/api/admin/login")
def admin_login():
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0])[:80]
    clock = time.time()
    with _LOGIN_LOCK:
        attempts = [t for t in _LOGIN_FAILURES.get(ip, []) if clock - t < 600]
        _LOGIN_FAILURES[ip] = attempts
        if len(attempts) >= 6:
            return jsonify(ok=False, error="Quá nhiều lần đăng nhập sai; thử lại sau 10 phút"), 429
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", ""))
    if not ADMIN_PASSWORD:
        return jsonify(ok=False, error="Chưa cấu hình PAWNOVA_ADMIN_PASSWORD"), 503
    if not hmac.compare_digest(password, ADMIN_PASSWORD):
        with _LOGIN_LOCK:
            _LOGIN_FAILURES.setdefault(ip, []).append(clock)
        time.sleep(0.35)
        return jsonify(ok=False, error="Sai mật khẩu quản trị"), 401
    with _LOGIN_LOCK:
        _LOGIN_FAILURES.pop(ip, None)
    session.clear()
    session.permanent = True
    session["admin_ok"] = True
    session["csrf"] = secrets.token_urlsafe(24)
    return jsonify(ok=True)


@app.post("/api/admin/logout")
def admin_logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/admin/summary")
@admin_required
def admin_summary():
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM licenses WHERE active=1 AND (activated_at IS NULL OR expires_at>?)", (iso(),)).fetchone()[0]
        expired = conn.execute("SELECT COUNT(*) FROM licenses WHERE activated_at IS NOT NULL AND expires_at<=?", (iso(),)).fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM orders WHERE status='PENDING'").fetchone()[0]
        revenue = conn.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='APPROVED'").fetchone()[0]
        graded = conn.execute("SELECT COUNT(*) FROM prediction_logs WHERE grade IN ('WIN','LOSE')").fetchone()[0]
        wins = conn.execute("SELECT COUNT(*) FROM prediction_logs WHERE grade='WIN'").fetchone()[0]
    return jsonify(ok=True, stats={"total": total, "active": active, "expired": expired,
                                  "pending_orders": pending, "revenue": revenue,
                                  "graded_predictions": graded, "wins": wins,
                                  "observed_win_rate": round(wins * 100 / graded, 2) if graded else None})


@app.get("/api/admin/licenses")
@admin_required
def admin_licenses():
    q = request.args.get("q", "").strip()[:100]
    with db() as conn:
        if q:
            rows = conn.execute("SELECT id,label,key_prefix,created_at,expires_at,active,calls,device_id,telegram_id,duration_days,duration_hours,activated_at,order_id FROM licenses WHERE label LIKE ? OR key_prefix LIKE ? OR telegram_id LIKE ? ORDER BY id DESC LIMIT 300", (f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
        else:
            rows = conn.execute("SELECT id,label,key_prefix,created_at,expires_at,active,calls,device_id,telegram_id,duration_days,duration_hours,activated_at,order_id FROM licenses ORDER BY id DESC LIMIT 500").fetchall()
    out = []
    current = iso()
    for row in rows:
        item = dict(row)
        item["unactivated"] = not bool(item.get("activated_at"))
        item["expired"] = bool(item.get("activated_at")) and item["expires_at"] <= current
        out.append(item)
    return jsonify(ok=True, licenses=out)


@app.post("/api/admin/licenses")
@admin_required
def admin_create_license():
    data = request.get_json(silent=True) or {}
    label = str(data.get("label", "")).strip()[:100]
    try:
        if data.get("hours") is not None:
            hours = int(data.get("hours")); days = max(1, (hours + 23) // 24)
        else:
            days = int(data.get("days", 0)); hours = days * 24
    except (TypeError, ValueError):
        days = 0; hours = 0
    if not label:
        return jsonify(ok=False, error="Vui lòng nhập tên khách hàng hoặc ghi chú"), 400
    if not 1 <= hours <= 24 * 3650:
        return jsonify(ok=False, error="Thời hạn hợp lệ: từ 1 giờ đến 10 năm"), 400
    raw, lid, expiry = issue_key(label, days, duration_hours=hours)
    event("license_created", f"Admin tạo key ID {lid} cho {label}, thời hạn {hours} giờ")
    return jsonify(ok=True, key=raw, id=lid, expires_at=expiry, days=days, hours=hours)


@app.post("/api/admin/licenses/<int:license_id>/toggle")
@admin_required
def admin_toggle_license(license_id: int):
    with db() as conn:
        row = conn.execute("SELECT active FROM licenses WHERE id=?", (license_id,)).fetchone()
        if not row:
            return jsonify(ok=False, error="Không tìm thấy key"), 404
        new_status = 0 if row["active"] else 1
        conn.execute("UPDATE licenses SET active=? WHERE id=?", (new_status, license_id))
    event("license_status", f"Admin đổi trạng thái key ID {license_id} thành {new_status}")
    return jsonify(ok=True, active=bool(new_status))


@app.post("/api/admin/licenses/<int:license_id>/extend")
@admin_required
def admin_extend_license(license_id: int):
    data = request.get_json(silent=True) or {}
    try:
        days = int(data.get("days", 0))
    except (TypeError, ValueError):
        days = 0
    if days not in PACKAGES:
        return jsonify(ok=False, error="Gói hợp lệ: 3, 5, 7 hoặc 10 ngày"), 400
    with db() as conn:
        row = conn.execute("SELECT expires_at,activated_at,duration_days FROM licenses WHERE id=?", (license_id,)).fetchone()
        if not row:
            return jsonify(ok=False, error="Không tìm thấy key"), 404
        if not row["activated_at"]:
            duration = int(row["duration_days"] or 0) + days
            expiry = now() + timedelta(days=duration)
            conn.execute("UPDATE licenses SET duration_days=?,expires_at=?,active=1 WHERE id=?", (duration, iso(expiry), license_id))
        else:
            try:
                old_expiry = datetime.fromisoformat(row["expires_at"])
            except (TypeError, ValueError):
                old_expiry = now()
            expiry = max(old_expiry, now()) + timedelta(days=days)
            conn.execute("UPDATE licenses SET expires_at=?,duration_days=COALESCE(duration_days,0)+?,active=1 WHERE id=?", (iso(expiry), days, license_id))
    event("license_extended", f"Admin gia hạn key ID {license_id} thêm {days} ngày")
    return jsonify(ok=True, expires_at=iso(expiry))


@app.post("/api/activate")
def activate():
    data = request.get_json(silent=True) or {}
    key = str(data.get("key", "")).strip()
    device = str(data.get("device_id", "")).strip()
    if not device or len(device) > 200:
        return jsonify(ok=False, error="Thiếu mã thiết bị hợp lệ"), 400
    license_row, error, status = check_license(key, device, True)
    if error:
        return jsonify(ok=False, error=error), status
    first_activation = bool(license_row.get("activated_at"))
    event("license_activated", f"Key ID {license_row['id']} đã xác thực trên thiết bị {device[:80]}")
    # Gửi thông báo quản trị; không gửi key thô hoặc mã thiết bị đầy đủ ra ngoài.
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    owner = os.environ.get("TELEGRAM_OWNER_ID", "8217369095").strip()
    if token and owner:
        try:
            import requests
            ua = request.headers.get("User-Agent", "Không rõ")[:180]
            ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", request.remote_addr or "Không rõ").split(",")[0]
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=5, json={"chat_id": owner, "text":
                "🔐 PAWNOVA · KÍCH HOẠT KEY\n"
                f"Mã key: #{license_row['id']} ({license_row.get('key_prefix','') }•••)\n"
                f"Khách: {license_row.get('label','—')}\n"
                f"Thiết bị: {device[:80]}\nIP ghi nhận: {str(ip)[:80]}\n"
                f"Trình duyệt/thiết bị: {ua}\nKích hoạt: {license_row.get('activated_at') or iso()}\n"
                f"Hết hạn: {license_row['expires_at']}"})
        except Exception:
            pass
    hours = int(license_row.get("duration_hours") or 0)
    return jsonify(ok=True, expires_at=license_row["expires_at"], activated_at=license_row.get("activated_at"),
                   duration_days=license_row.get("duration_days"), duration_hours=hours, label=license_row["label"],
                   key_id=license_row["id"], device_id=device, user_agent=request.headers.get("User-Agent", "")[:180])


@app.get("/api/license/check")
def license_check():
    lic, error, status = check_license(request.headers.get("X-API-Key"), request.headers.get("X-Device-ID"), True)
    if error:
        return jsonify(ok=False, error=error), status
    return jsonify(ok=True, expires_at=lic["expires_at"], activated_at=lic.get("activated_at"),
                   duration_days=lic.get("duration_days"), label=lic["label"])


# The prediction cache is global so all customers see a consistent forecast for one session.
_prediction_cache: dict[str, dict[str, Any]] = {
    "md5": {"data": None, "last_success_monotonic": 0.0, "last_error": None},
    "hu": {"data": None, "last_success_monotonic": 0.0, "last_error": None},
}
_prediction_locks = {"md5": threading.Lock(), "hu": threading.Lock()}
_FEEDBACK_DONE: set[tuple[str, str]] = set()
_WORKER_LOCK = threading.Lock()
_WORKERS_STARTED = False


def _grade_seen_sessions(game: str, source_rows: list[dict[str, Any]]) -> None:
    """Grade emitted predictions exactly once when their target session appears."""
    source_map = {}
    for source in source_rows:
        try:
            sid = str(int(source["id"]))
            actual = str(source.get("result", "")).upper()
        except (TypeError, ValueError, KeyError):
            continue
        if actual in ("TAI", "XIU"):
            source_map[sid] = actual
    if not source_map:
        return
    placeholders = ",".join("?" for _ in source_map)
    changed_feedback = []
    with db() as conn:
        pending = conn.execute(
            f"SELECT id,target_session_id,prediction FROM prediction_logs "
            f"WHERE game=? AND actual IS NULL AND target_session_id IN ({placeholders})",
            (game, *source_map.keys()),
        ).fetchall()
        for row in pending:
            actual = source_map[row["target_session_id"]]
            grade = "WIN" if row["prediction"] == actual else "LOSE"
            conn.execute("UPDATE prediction_logs SET actual=?,grade=?,graded_at=? WHERE id=? AND actual IS NULL",
                         (actual, grade, iso(), row["id"]))
            token = (game, row["target_session_id"])
            if token not in _FEEDBACK_DONE:
                changed_feedback.append((token, row["prediction"], actual))
                _FEEDBACK_DONE.add(token)
    try:
        from NOVA_PREDICTION_ENGINE import save_result
        for (game_key, target_id), prediction, actual in changed_feedback:
            save_result(target_id, prediction, actual)
    except Exception:
        # Database grading remains authoritative; model feedback is best-effort process-local.
        pass


def refresh_prediction(game: str, force: bool = False) -> dict[str, Any] | None:
    game = str(game).strip().lower()
    if game not in GAME_URLS:
        raise ValueError("game phải là md5 hoặc hu")
    lock = _prediction_locks[game]
    if not lock.acquire(blocking=force):
        return _prediction_cache[game].get("data")
    try:
        from NOVA_PREDICTION_ENGINE import predict_next
        result, error = predict_next(GAME_URLS[game])
        if error or not result:
            _prediction_cache[game]["last_error"] = str(error or "Engine chưa có dự đoán")[:180]
            return _prediction_cache[game].get("data")
        rows = result.get("source_rows") or []
        _grade_seen_sessions(game, rows)
        latest_id = result.get("top_id")
        target_id = result.get("target_id")
        # The source uses integer session ids. Still verify that the latest pair is consecutive
        # before presenting the next id as confirmed.
        try:
            ids = [int(row["id"]) for row in rows[:2]]
            consecutive = len(ids) >= 2 and ids[0] - ids[1] == 1
        except (TypeError, ValueError, KeyError):
            consecutive = False
        if not consecutive:
            target_id = None
        old = _prediction_cache[game].get("data")
        checked_at = iso()
        # Freeze one prediction for each source session; repeated polls must not change it.
        if old and str(old.get("session")) == str(latest_id):
            old["source_checked_at"] = checked_at
            old["source_stale"] = False
            _prediction_cache[game]["last_success_monotonic"] = time.monotonic()
            _prediction_cache[game]["last_error"] = None
            return old
        if target_id is None:
            # If the latest session changed but IDs are not consecutive, the previous target
            # may already have passed. Do not serve it as if it were a current forecast.
            _prediction_cache[game]["last_error"] = "Không xác minh được số phiên kế tiếp liên tục"
            if old and str(old.get("session")) == str(latest_id):
                old["source_checked_at"] = checked_at
                old["source_stale"] = False
                _prediction_cache[game]["last_success_monotonic"] = time.monotonic()
                return old
            _prediction_cache[game]["data"] = None
            _prediction_cache[game]["last_success_monotonic"] = 0.0
            return None
        snapshot = {
            "ok": True,
            "game": "MD5" if game == "md5" else "HU",
            "table": GAME_LABELS[game],
            "session": str(latest_id),
            "target_session": str(target_id),
            "prediction": str(result.get("predict", "")).upper(),
            "confidence": int(result.get("confidence", 0)),
            "p_tai": result.get("p_tai"),
            "status": "online",
            "updated_at": checked_at,
            "source_checked_at": checked_at,
            "source_stale": False,
            "engine": result.get("version", "PAWNOVA-ENSEMBLE"),
            "reason": [str(x) for x in result.get("reasons", []) if x][:4],
        }
        if snapshot["prediction"] not in ("TAI", "XIU"):
            _prediction_cache[game]["last_error"] = "Engine trả dự đoán không hợp lệ"
            return old
        _prediction_cache[game]["data"] = snapshot
        _prediction_cache[game]["last_success_monotonic"] = time.monotonic()
        _prediction_cache[game]["last_error"] = None
        return snapshot
    except Exception as exc:
        _prediction_cache[game]["last_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        print(f"PAWNOVA refresh {game} failed: {type(exc).__name__}: {str(exc)[:180]}", flush=True)
        return _prediction_cache[game].get("data")
    finally:
        lock.release()


def _snapshot_for_request(game: str) -> tuple[dict[str, Any] | None, bool]:
    """Return only a fresh prediction; refresh before serving anything stale.

    Polling logs alone are not enough: the HTTP endpoint must independently ensure
    that its cached snapshot is fresh. If the source cannot be refreshed, fail closed
    rather than presenting an old prediction as if it were current.
    """
    cache = _prediction_cache[game]
    data = cache.get("data")

    def is_stale(snapshot: dict[str, Any] | None) -> bool:
        if snapshot is None:
            return True
        age = time.monotonic() - cache.get("last_success_monotonic", 0.0)
        return bool(cache.get("last_error") or age > max(25, POLL_INTERVAL * 4))

    if is_stale(data):
        refreshed = refresh_prediction(game, force=True)
        if refreshed is not None:
            data = refreshed

    stale = is_stale(data)
    if stale:
        # Never let HTML consume an old forecast. Caller returns a retriable 503.
        return None, True
    if data is not None:
        data["source_stale"] = False
    return data, False


def _log_prediction(license_id: int, game: str, snapshot: dict[str, Any]) -> None:
    if not snapshot.get("target_session"):
        return
    with db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO prediction_logs
            (license_id,game,source_session_id,target_session_id,prediction,confidence,created_at)
            VALUES(?,?,?,?,?,?,?)
        """, (license_id, game, snapshot.get("session"), snapshot["target_session"],
              snapshot["prediction"], snapshot.get("confidence"), iso()))


@app.get("/api/predict")
def predict_api():
    game = str(request.args.get("game", "md5")).strip().lower()
    if game not in GAME_URLS:
        return jsonify(ok=False, error="game phải là md5 hoặc hu"), 400
    lic, error, status = check_license(request.headers.get("X-API-Key"),
                                       request.headers.get("X-Device-ID"), True)
    if error:
        return jsonify(ok=False, error=error), status
    snapshot, stale = _snapshot_for_request(game)
    if not snapshot:
        return jsonify(ok=False, error="Chưa có dự đoán; nguồn API lỗi hoặc chưa đủ dữ liệu"), 503
    with db() as conn:
        conn.execute("UPDATE licenses SET calls=calls+1 WHERE id=?", (lic["id"],))
    _log_prediction(lic["id"], game, snapshot)
    result = dict(snapshot)
    result["ok"] = True
    result["stale"] = stale
    result["server_time"] = iso()
    result["reason"] = snapshot.get("reason", [])
    result.pop("source_stale", None)
    return jsonify(result)


@app.get("/api/history")
def prediction_history():
    game = str(request.args.get("game", "md5")).strip().lower()
    if game not in GAME_URLS:
        return jsonify(ok=False, error="game phải là md5 hoặc hu"), 400
    lic, error, status = check_license(request.headers.get("X-API-Key"),
                                       request.headers.get("X-Device-ID"), True)
    if error:
        return jsonify(ok=False, error=error), status
    with db() as conn:
        rows = conn.execute("""
          SELECT game,source_session_id,target_session_id,prediction,confidence,created_at,actual,grade,graded_at
          FROM prediction_logs WHERE license_id=? AND game=? ORDER BY id DESC LIMIT 100
        """, (lic["id"], game)).fetchall()
    return jsonify(ok=True, game=game, history=[dict(row) for row in rows])


@app.post("/api/orders")
def create_order():
    """Create a pending order for external clients; payment confirmation stays manual."""
    data = request.get_json(silent=True) or {}
    telegram_id = str(data.get("telegram_id", "")).strip()
    username = str(data.get("username", "")).strip().lstrip("@")[:100]
    try:
        days = int(data.get("days", 0))
    except (TypeError, ValueError):
        days = 0
    if not telegram_id.isdigit() or len(telegram_id) > 24:
        return jsonify(ok=False, error="telegram_id phải là ID số hợp lệ"), 400
    if days not in PACKAGES:
        return jsonify(ok=False, error="Gói hợp lệ: 3, 5, 7 hoặc 10 ngày"), 400
    with db() as conn:
        cur = conn.execute("INSERT INTO orders(telegram_id,username,package_days,amount,status,created_at) VALUES(?,?,?,?,'PENDING',?)",
                           (telegram_id, username or None, days, PACKAGES[days], iso()))
        order_id = cur.lastrowid
    event("order_created", f"Đơn #{order_id} tạo qua API cho Telegram ID {telegram_id}, gói {days} ngày")
    # Best-effort notify the fixed administrator through the configured bot.
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        try:
            import requests
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=6,
                          json={"chat_id": os.environ.get("TELEGRAM_OWNER_ID", "8217369095").strip(), "text":
                                f"ĐƠN MỚI #{order_id}\nTelegram ID: {telegram_id}\nTên: {username or '—'}\nGói: {days} ngày\nSố tiền: {PACKAGES[days]:,} VND\nTrạng thái: CHỜ DUYỆT"})
        except Exception:
            pass
    return jsonify(ok=True, order_id=order_id, days=days, amount=PACKAGES[days], status="PENDING"), 201


@app.get("/api/admin/orders")
@admin_required
def admin_orders():
    with db() as conn:
        rows = conn.execute("SELECT id,telegram_id,username,package_days,amount,status,created_at,approved_at FROM orders ORDER BY id DESC LIMIT 200").fetchall()
    return jsonify(ok=True, orders=[dict(row) for row in rows])


@app.post("/api/admin/orders/<int:order_id>/approve")
@admin_required
def approve_order(order_id: int):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            return jsonify(ok=False, error="Không tìm thấy đơn"), 404
        if order["status"] != "PENDING":
            return jsonify(ok=False, error="Đơn đã được xử lý trước đó"), 409
        raw, license_id, expiry = issue_key(
            "Telegram " + (order["username"] or order["telegram_id"]),
            order["package_days"], order["telegram_id"], order_id, conn=conn
        )
        conn.execute("UPDATE orders SET status='APPROVED',approved_at=?,license_id=? WHERE id=?",
                     (iso(), license_id, order_id))
    event("order_approved", f"Duyệt đơn Telegram #{order_id}")
    delivered = False
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        try:
            import requests
            message = (f"Đơn #{order_id} đã được xác nhận.\nGói: {order['package_days']} ngày\n"
                       f"Key kích hoạt:\n{raw}\nThời hạn: {order['package_days']} ngày kể từ lần kích hoạt đầu tiên.\nHãy giữ key riêng tư.")
            resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=8,
                                 json={"chat_id": order["telegram_id"], "text": message})
            delivered = bool(resp.ok and resp.json().get("ok"))
        except Exception:
            delivered = False
    # Only the admin endpoint reveals the key; the customer receives it by Telegram if configured.
    return jsonify(ok=True, order_id=order_id, key=raw, expires_at=expiry,
                   telegram_id=order["telegram_id"], delivered=delivered)


@app.post("/api/admin/orders/<int:order_id>/reject")
@admin_required
def reject_order(order_id: int):
    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason", "Chưa xác nhận được thanh toán")).strip()[:500]
    with db() as conn:
        order = conn.execute("SELECT telegram_id, package_days, amount FROM orders WHERE id=? AND status='PENDING'", (order_id,)).fetchone()
        if not order:
            return jsonify(ok=False, error="Không có đơn chờ duyệt"), 404
        cur = conn.execute("UPDATE orders SET status='REJECTED' WHERE id=? AND status='PENDING'", (order_id,))
        if not cur.rowcount:
            return jsonify(ok=False, error="Đơn đã được xử lý"), 409
    delivered = False
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        try:
            import requests
            resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=8,
                json={"chat_id": order["telegram_id"], "text": f"Đơn #{order_id} chưa được duyệt.\nGói: {order['package_days']} ngày · {order['amount']:,} VND\nLý do: {reason}\nNếu cần hỗ trợ, vui lòng liên hệ Admin."})
            delivered = bool(resp.ok and resp.json().get("ok"))
        except Exception:
            delivered = False
    event("order_rejected", f"Từ chối đơn #{order_id}; lý do: {reason}")
    return jsonify(ok=True, delivered=delivered)


@app.post("/api/admin/licenses/<int:license_id>/reset-device")
@admin_required
def admin_reset_device(license_id: int):
    with db() as conn:
        row = conn.execute("SELECT id,label,device_id FROM licenses WHERE id=?", (license_id,)).fetchone()
        if not row:
            return jsonify(ok=False, error="Không tìm thấy key"), 404
        conn.execute("UPDATE licenses SET device_id=NULL WHERE id=?", (license_id,))
    event("device_reset", f"Admin gỡ liên kết thiết bị key #{license_id}")
    return jsonify(ok=True, id=license_id, previous_device=row["device_id"])


@app.get("/api/admin/users")
@admin_required
def admin_users():
    q = request.args.get("q", "").strip()[:100]
    with db() as conn:
        rows = conn.execute("""
            SELECT telegram_id, COUNT(*) AS key_count,
                   SUM(CASE WHEN active=1 AND (activated_at IS NULL OR expires_at>?) THEN 1 ELSE 0 END) AS active_keys,
                   MAX(created_at) AS last_key_created
            FROM licenses WHERE telegram_id IS NOT NULL AND telegram_id<>''
            AND (?='' OR telegram_id LIKE ? OR label LIKE ?)
            GROUP BY telegram_id ORDER BY MAX(id) DESC LIMIT 300
        """, (iso(), q, f"%{q}%", f"%{q}%")).fetchall()
    return jsonify(ok=True, users=[dict(r) for r in rows])


@app.post("/api/admin/broadcast")
@admin_required
def admin_broadcast():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()
    audience = str(data.get("audience", "linked"))
    if not message or len(message) > 3500:
        return jsonify(ok=False, error="Nội dung thông báo phải từ 1 đến 3500 ký tự"), 400
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return jsonify(ok=False, error="Chưa cấu hình TELEGRAM_BOT_TOKEN trên Render"), 503
    with db() as conn:
        if audience == "active":
            rows = conn.execute("SELECT DISTINCT telegram_id FROM licenses WHERE telegram_id IS NOT NULL AND telegram_id<>'' AND active=1 AND (activated_at IS NULL OR expires_at>?)", (iso(),)).fetchall()
        else:
            rows = conn.execute("SELECT DISTINCT telegram_id FROM licenses WHERE telegram_id IS NOT NULL AND telegram_id<>''").fetchall()
    import requests
    sent = failed = 0
    for row in rows:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=8,
                              json={"chat_id": row["telegram_id"], "text": message, "disable_web_page_preview": True})
            if r.ok and r.json().get("ok"):
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    event("broadcast", f"Admin gửi thông báo Telegram: gửi {sent}, lỗi {failed}")
    return jsonify(ok=True, recipients=len(rows), sent=sent, failed=failed)


@app.get("/api/admin/predictions")
@admin_required
def admin_predictions():
    """Admin-only live status for both prediction sources; exposes cache status, not secrets."""
    items = {}
    for game in GAME_URLS:
        cache = _prediction_cache[game]
        snap = cache.get("data")
        age = time.monotonic() - cache.get("last_success_monotonic", 0.0) if snap else None
        if snap is None or cache.get("last_error") or (age is not None and age > max(25, POLL_INTERVAL * 4)):
            try:
                refresh_prediction(game, force=True)
            except Exception:
                pass
            snap = cache.get("data")
            age = time.monotonic() - cache.get("last_success_monotonic", 0.0) if snap else None
        stale = bool(snap and (cache.get("last_error") or age is None or age > max(25, POLL_INTERVAL * 4)))
        items[game] = {
            "game": game,
            "label": GAME_LABELS[game],
            "ready": bool(snap) and not stale,
            "stale": stale,
            "source_session": (snap or {}).get("session"),
            "target_session": (snap or {}).get("target_session"),
            "prediction": (snap or {}).get("prediction"),
            "confidence": (snap or {}).get("confidence"),
            "updated_at": (snap or {}).get("updated_at"),
            "age_seconds": int(age) if age is not None else None,
            "last_error": cache.get("last_error"),
            "engine": (snap or {}).get("engine"),
        }
    return jsonify(ok=True, predictions=items, poll_seconds=POLL_INTERVAL, checked_at=iso())


@app.get("/api/admin/history")
@admin_required
def admin_prediction_history():
    game = str(request.args.get("game", "hu")).strip().lower()
    try:
        limit = min(200, max(1, int(request.args.get("limit", "100"))))
    except (TypeError, ValueError):
        limit = 100
    if game not in GAME_URLS:
        return jsonify(ok=False, error="game phải là hu hoặc md5"), 400
    with db() as conn:
        rows = conn.execute("""
            SELECT p.id, p.game, p.source_session_id, p.target_session_id,
                   p.prediction, p.confidence, p.created_at, p.actual, p.grade,
                   p.graded_at, l.label AS license_label
            FROM prediction_logs p
            LEFT JOIN licenses l ON l.id=p.license_id
            WHERE p.game=?
            ORDER BY p.id DESC LIMIT ?
        """, (game, limit)).fetchall()
    return jsonify(ok=True, game=game, history=[dict(row) for row in rows])


@app.get("/api/admin/system")
@admin_required
def admin_system():
    with db() as conn:
        table_counts = {}
        for table in ("licenses", "orders", "events", "prediction_logs"):
            table_counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    sources = {}
    for game in GAME_URLS:
        c = _prediction_cache[game]
        age = time.monotonic() - c.get("last_success_monotonic", 0.0)
        sources[game] = {"ready": bool(c.get("data")), "age_seconds": int(age) if c.get("data") else None,
                         "last_error": c.get("last_error"), "source_session": (c.get("data") or {}).get("session")}
    return jsonify(ok=True, storage={"path": str(DB_PATH), "persistent_path_configured": DB_PERSISTENT_PATH_CONFIGURED,
                                     "backup_exists": _BACKUP_PATH.exists(), "backup_path": str(_BACKUP_PATH)},
                   counts=table_counts, sources=sources, poll_seconds=POLL_INTERVAL)


@app.get("/api/admin/backup")
@admin_required
def admin_backup():
    # Generate a consistent snapshot on demand; never expose the live database file itself.
    temp_path = DB_PATH.with_name(DB_PATH.name + ".download.tmp")
    try:
        with sqlite3.connect(str(DB_PATH), timeout=30) as source, sqlite3.connect(str(temp_path), timeout=30) as target:
            source.backup(target)
        from flask import send_file
        return send_file(str(temp_path), as_attachment=True, download_name="pawnova-backup.sqlite3", mimetype="application/octet-stream")
    except Exception as exc:
        try: temp_path.unlink(missing_ok=True)
        except Exception: pass
        return jsonify(ok=False, error=f"Không tạo được bản sao lưu: {type(exc).__name__}"), 500


@app.get("/api/admin/events")
@admin_required
def admin_events():
    with db() as conn:
        rows = conn.execute("SELECT kind,message,created_at FROM events ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify(ok=True, events=[dict(row) for row in rows])


def _poll_loop(game: str) -> None:
    while True:
        started = time.monotonic()
        try:
            refresh_prediction(game, force=True)
        except Exception as exc:
            _prediction_cache[game]["last_error"] = type(exc).__name__
            print(f"PAWNOVA polling {game} failed: {type(exc).__name__}", flush=True)
        delay = max(1.0, POLL_INTERVAL - (time.monotonic() - started))
        time.sleep(delay)


def _start_workers() -> None:
    global _WORKERS_STARTED
    if os.environ.get("PAWNOVA_ENABLE_POLLING", "true").lower() not in ("1", "true", "yes", "on"):
        print("PAWNOVA polling disabled by PAWNOVA_ENABLE_POLLING", flush=True)
        return
    with _WORKER_LOCK:
        if _WORKERS_STARTED:
            return
        _WORKERS_STARTED = True
    for game in GAME_URLS:
        thread = threading.Thread(target=_poll_loop, args=(game,), name=f"pawnova-poll-{game}", daemon=True)
        thread.start()
        print(f"PAWNOVA polling worker started: {game}", flush=True)
    if os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("PAWNOVA_ENABLE_BOT", "true").lower() in ("1", "true", "yes", "on"):
        def bot_runner():
            try:
                from NOVA_ADMIN_BOT import main as bot_main
                bot_main()
            except Exception as exc:
                print(f"PAWNOVA Telegram bot stopped: {type(exc).__name__}", flush=True)
        threading.Thread(target=bot_runner, name="pawnova-admin-bot", daemon=True).start()


_start_workers()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False, threaded=True)
