# -*- coding: utf-8 -*-
"""PAWNOVA server: license API, dual-game prediction cache and admin panel."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import wraps
from typing import Any

from flask import Flask, jsonify, request, session, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PAWNOVA_DB_PATH", str(BASE_DIR / "pawnova.sqlite3"))).expanduser()
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
    """Open, commit/rollback, and always close a SQLite connection."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=20, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
        """)


init_db()


def event(kind: str, message: str) -> None:
    with db() as conn:
        conn.execute("INSERT INTO events(kind,message,created_at) VALUES(?,?,?)",
                     (str(kind)[:80], str(message)[:500], iso()))


def issue_key(label: str, days: int, telegram_id: str | int | None = None,
              order_id: int | None = None, conn: sqlite3.Connection | None = None):
    if int(days) not in PACKAGES:
        raise ValueError("Gói key không hợp lệ")
    raw = "PNV-" + secrets.token_urlsafe(30)
    created = now()
    expiry = created + timedelta(days=int(days))
    values = (str(label).strip()[:100] or "Khách", digest(raw), raw[:12], iso(created),
              iso(expiry), str(telegram_id) if telegram_id is not None else None, order_id)
    sql = "INSERT INTO licenses(label,key_hash,key_prefix,created_at,expires_at,active,calls,telegram_id,order_id) VALUES(?,?,?,?,?,1,0,?,?)"
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
        try:
            expiry = datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError):
            return None, "Dữ liệu hạn key lỗi", 403
        if expiry <= now():
            return None, "Key đã hết hạn", 403
        device_id = str(device_id or "").strip()
        bound = row["device_id"]
        if device_id and len(device_id) > 200:
            return None, "Mã thiết bị không hợp lệ", 400
        if bound and not device_id:
            return None, "Thiếu mã thiết bị đã liên kết", 403
        if device_id:
            if bound and not hmac.compare_digest(str(bound), device_id):
                return None, "Key đã gắn với thiết bị khác", 403
            if not bound and bind:
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
    return jsonify(ok=True, status="online", time=iso(), database="ok", predictions=cache_state)


@app.get("/admin")
def admin_page():
    return send_from_directory(BASE_DIR, "PAWNOVA_CONTROL.html")


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


@app.get("/api/admin/predictions")
@admin_required
def admin_predictions():
    # Admin-only live snapshots for the two independently polled source tables.
    out = {}
    for game in GAME_URLS:
        cache = _prediction_cache[game]
        data = cache.get("data")
        out[game] = {"ready": bool(data), "data": data, "last_error": cache.get("last_error")}
    return jsonify(ok=True, predictions=out)


@app.get("/api/admin/history")
@admin_required
def admin_prediction_history():
    game = str(request.args.get("game", "md5")).strip().lower()
    if game not in GAME_URLS:
        return jsonify(ok=False, error="game phải là md5 hoặc hu"), 400
    with db() as conn:
        rows = conn.execute("""
          SELECT pl.id,pl.game,pl.source_session_id,pl.target_session_id,pl.prediction,
                 pl.confidence,pl.created_at,pl.actual,pl.grade,pl.graded_at,l.label
          FROM prediction_logs pl LEFT JOIN licenses l ON l.id=pl.license_id
          WHERE pl.game=? ORDER BY pl.id DESC LIMIT 200
        """, (game,)).fetchall()
    return jsonify(ok=True, game=game, history=[dict(row) for row in rows])


@app.get("/api/admin/summary")
@admin_required
def admin_summary():
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM licenses WHERE active=1 AND expires_at>?", (iso(),)).fetchone()[0]
        expired = conn.execute("SELECT COUNT(*) FROM licenses WHERE expires_at<=?", (iso(),)).fetchone()[0]
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
            rows = conn.execute("SELECT id,label,key_prefix,created_at,expires_at,active,calls,device_id,telegram_id FROM licenses WHERE label LIKE ? OR key_prefix LIKE ? ORDER BY id DESC LIMIT 300", (f"%{q}%", f"%{q}%")).fetchall()
        else:
            rows = conn.execute("SELECT id,label,key_prefix,created_at,expires_at,active,calls,device_id,telegram_id FROM licenses ORDER BY id DESC LIMIT 300").fetchall()
    out = []
    current = iso()
    for row in rows:
        item = dict(row)
        item["expired"] = item["expires_at"] <= current
        out.append(item)
    return jsonify(ok=True, licenses=out)


@app.post("/api/admin/licenses")
@admin_required
def admin_create_license():
    data = request.get_json(silent=True) or {}
    label = str(data.get("label", "")).strip()[:100]
    try:
        days = int(data.get("days", 0))
    except (TypeError, ValueError):
        days = 0
    if not label:
        return jsonify(ok=False, error="Nhập tên khách hàng"), 400
    if days not in PACKAGES:
        return jsonify(ok=False, error="Gói hợp lệ: 3, 5, 7 hoặc 10 ngày"), 400
    raw, lid, expiry = issue_key(label, days)
    event("license_created", f"Admin tạo key ID {lid} cho {label}, {days} ngày")
    return jsonify(ok=True, key=raw, id=lid, expires_at=expiry, days=days)


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
        row = conn.execute("SELECT expires_at FROM licenses WHERE id=?", (license_id,)).fetchone()
        if not row:
            return jsonify(ok=False, error="Không tìm thấy key"), 404
        try:
            old_expiry = datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError):
            old_expiry = now()
        expiry = max(old_expiry, now()) + timedelta(days=days)
        conn.execute("UPDATE licenses SET expires_at=?,active=1 WHERE id=?", (iso(expiry), license_id))
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
    event("license_activated", f"Key ID {license_row['id']} đã kiểm tra/kích hoạt trên thiết bị")
    return jsonify(ok=True, expires_at=license_row["expires_at"], label=license_row["label"])


@app.get("/api/license/check")
def license_check():
    lic, error, status = check_license(request.headers.get("X-API-Key"), request.headers.get("X-Device-ID"), True)
    if error:
        return jsonify(ok=False, error=error), status
    return jsonify(ok=True, expires_at=lic["expires_at"], label=lic["label"])


# The prediction cache is global so all customers see a consistent forecast for one session.
_prediction_cache: dict[str, dict[str, Any]] = {
    "md5": {"data": None, "last_success_monotonic": 0.0, "last_error": None},
    "hu": {"data": None, "last_success_monotonic": 0.0, "last_error": None},
}
_prediction_locks = {"md5": threading.Lock(), "hu": threading.Lock()}
_FEEDBACK_DONE: set[tuple[str, str]] = set()


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
        from PAWNOVA_ENGINE import save_result
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
        from PAWNOVA_ENGINE import predict_next
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
        _prediction_cache[game]["last_error"] = type(exc).__name__
        return _prediction_cache[game].get("data")
    finally:
        lock.release()


def _snapshot_for_request(game: str) -> tuple[dict[str, Any] | None, bool]:
    cache = _prediction_cache[game]
    data = cache.get("data")
    if data is None:
        data = refresh_prediction(game, force=True)
    age = time.monotonic() - cache.get("last_success_monotonic", 0.0)
    stale = bool(data and (cache.get("last_error") or age > max(25, POLL_INTERVAL * 4)))
    if data:
        data["source_stale"] = stale
    return data, stale


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
                          json={"chat_id": "8217369095", "text":
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
                       f"Key kích hoạt:\n{raw}\nHạn dùng: {expiry}\nHãy giữ key riêng tư.")
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
    with db() as conn:
        cur = conn.execute("UPDATE orders SET status='REJECTED' WHERE id=? AND status='PENDING'", (order_id,))
        if not cur.rowcount:
            return jsonify(ok=False, error="Không có đơn chờ duyệt"), 404
    event("order_rejected", f"Từ chối đơn #{order_id}")
    return jsonify(ok=True)


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
    if os.environ.get("PAWNOVA_ENABLE_POLLING", "true").lower() not in ("1", "true", "yes", "on"):
        return
    for game in GAME_URLS:
        thread = threading.Thread(target=_poll_loop, args=(game,), name=f"pawnova-poll-{game}", daemon=True)
        thread.start()
    if os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("PAWNOVA_ENABLE_BOT", "true").lower() in ("1", "true", "yes", "on"):
        def bot_runner():
            try:
                from PAWNOVA_ADMINBOT import main as bot_main
                bot_main()
            except Exception as exc:
                print(f"PAWNOVA Telegram bot stopped: {type(exc).__name__}", flush=True)
        threading.Thread(target=bot_runner, name="pawnova-admin-bot", daemon=True).start()


_start_workers()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False, threaded=True)
