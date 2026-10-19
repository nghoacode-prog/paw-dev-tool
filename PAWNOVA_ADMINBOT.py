# -*- coding: utf-8 -*-
"""PAWNOVA Telegram bot: guarded admin console + customer license self-service."""
from __future__ import annotations
import os, time, hashlib, requests
from datetime import datetime, timedelta
from PAWNOVA_GATEWAY import db, issue_key, event, now, iso, PACKAGES, digest

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
OWNER_ID = os.environ.get("TELEGRAM_OWNER_ID", "8217369095").strip()
ADMIN_IDS = {OWNER_ID} | {x.strip() for x in os.environ.get("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip()}
HELP = """PAWNOVA BOT · CUSTOMER CARE

Khách hàng:
/link <KEY>  · Liên kết key với tài khoản Telegram
/mykey       · Xem key đã liên kết, trạng thái và hạn
/mystatus    · Tóm tắt tài khoản

Quản trị viên:
/create <3|5|7|10> <tên khách>
/keys [từ khóa]
/key <ID>
/revoke <ID> · Khóa key
/enable <ID> · Mở khóa
/extend <ID> <số ngày>
/unbind <ID> · Gỡ thiết bị
/stats · Thống kê
/users · Tài khoản đã liên kết
/events · Nhật ký
/broadcast <nội dung> · Gửi thông báo khách liên kết
/help · Trợ giúp

Thời hạn key mới bắt đầu ở lần kích hoạt đầu tiên trên thiết bị."""

def tg(method, **data):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN chưa cấu hình")
    r = requests.post(f"{API}/{method}", json=data, timeout=35)
    r.raise_for_status(); payload = r.json()
    if not payload.get("ok"): raise RuntimeError(str(payload))
    return payload["result"]

def send(chat, text, reply_markup=None):
    data = {"chat_id": chat, "text": str(text)[:4000], "disable_web_page_preview": True}
    if reply_markup: data["reply_markup"] = reply_markup
    tg("sendMessage", **data)

def menu_keyboard(is_admin=False):
    rows = [[{"text":"🔑 Key của tôi", "callback_data":"mykey"}, {"text":"📊 Trạng thái", "callback_data":"status"}],
            [{"text":"🔗 Liên kết key", "callback_data":"link_help"}, {"text":"❓ Hướng dẫn", "callback_data":"help"}]]
    if is_admin:
        rows += [[{"text":"🧰 Tool / API", "callback_data":"tool"}, {"text":"📈 Thống kê", "callback_data":"stats"}],
                 [{"text":"🔐 Quản lý key", "callback_data":"keys"}, {"text":"👥 Khách hàng", "callback_data":"users"}],
                 [{"text":"🧾 Nhật ký", "callback_data":"events"}]]
    return {"inline_keyboard": rows}

def tool_status_text():
    base = os.environ.get("PAWNOVA_PUBLIC_URL", "https://paw-dev-private-api78.onrender.com").rstrip("/")
    try:
        r = requests.get(base + "/health", timeout=8); r.raise_for_status(); h = r.json()
        out = ["PAWNOVA · TOOL / API STATUS", f"Gateway: {h.get('status', 'responding')}"]
        preds = h.get("predictions") or {}
        for name in ("hu", "md5"):
            v = preds.get(name) or {}
            out.append(f"{name.upper()}: {'READY' if v.get('ready') else 'NOT READY'} | phiên nguồn: {v.get('source_session') or '—'} | stale: {bool(v.get('stale'))}")
        return "\n".join(out)
    except Exception as exc:
        return "Không đọc được gateway /health: " + type(exc).__name__ + ". Kiểm tra PAWNOVA_PUBLIC_URL và Render."

def delete_user_message(chat, message_id):
    try: tg("deleteMessage", chat_id=chat, message_id=message_id)
    except Exception: pass

def _get_user_licenses(user_id):
    with db() as c:
        return c.execute("SELECT id,label,key_prefix,created_at,expires_at,active,calls,device_id,telegram_id,duration_days,activated_at FROM licenses WHERE telegram_id=? ORDER BY id DESC LIMIT 30", (str(user_id),)).fetchall()

def customer_status(user_id):
    rows = _get_user_licenses(user_id)
    if not rows: return "Tài khoản chưa liên kết key. Dùng /link KEY để liên kết."
    out = ["PAWNOVA · KEY CỦA BẠN"]
    for r in rows:
        if not r["active"]: state = "ĐÃ KHÓA"
        elif not r["activated_at"]: state = "CHƯA KÍCH HOẠT · CHƯA TÍNH HẠN"
        elif r["expires_at"] <= iso(now()): state = "HẾT HẠN"
        else: state = "ĐANG HOẠT ĐỘNG"
        out.append(f"\n#{r['id']} · {r['label']}\nKey: {r['key_prefix']}•••\nTrạng thái: {state}\nThời hạn: {r['duration_days'] or '—'} ngày\nHạn dùng: {r['expires_at'] if r['activated_at'] else 'Bắt đầu khi kích hoạt lần đầu'}\nThiết bị: {'Đã liên kết' if r['device_id'] else 'Chưa liên kết'}")
    return "\n".join(out)

def link_key(user_id, raw):
    raw = str(raw or "").strip()
    if not raw or len(raw) > 200: return "Cú pháp: /link <KEY>"
    with db() as c:
        row = c.execute("SELECT id,label,telegram_id,active FROM licenses WHERE key_hash=?", (digest(raw),)).fetchone()
        if not row: return "Không tìm thấy key. Hãy kiểm tra lại key hoặc liên hệ Admin."
        if not row["active"]: return "Key đang bị khóa. Hãy liên hệ Admin."
        current = str(row["telegram_id"] or "")
        if current and current != str(user_id): return "Key đã liên kết với một tài khoản Telegram khác. Liên hệ Admin nếu cần chuyển tài khoản."
        c.execute("UPDATE licenses SET telegram_id=? WHERE id=?", (str(user_id), row["id"]))
    event("telegram_link", f"Khách Telegram {user_id} liên kết key ID {row['id']}")
    return f"Liên kết thành công với key #{row['id']} ({row['label']}).\nDùng /mykey để xem trạng thái và hạn."

def admin_process(chat, user_id, cmd, args):
    if cmd in ("/start", "/help"):
        send(chat, "PAWNOVA CONTROL · Chọn chức năng bên dưới.", menu_keyboard(str(user_id) in ADMIN_IDS)); return
    if cmd == "/create":
        if len(args) < 2: send(chat,"Cú pháp: /create <3|5|7|10> <tên khách>"); return
        try: days=int(args[0])
        except ValueError: days=0
        label=" ".join(args[1:]).strip()[:100]
        if days not in PACKAGES or not label: send(chat,"Gói hợp lệ: 3, 5, 7, 10 ngày. Cú pháp /create 3 Khách A"); return
        key,lid,expiry=issue_key(label,days)
        event("license_created", f"Telegram admin tạo key ID {lid} cho {label}, {days} ngày")
        send(chat,f"ĐÃ TẠO KEY\nID: #{lid}\nKhách: {label}\nGói: {days} ngày\nHạn tính từ lần kích hoạt đầu tiên\n\nKEY (chỉ hiện một lần):\n{key}"); return
    if cmd == "/keys":
        q=" ".join(args).strip()[:80] if args else ""
        with db() as c:
            if q: rows=c.execute("SELECT id,label,key_prefix,expires_at,active,calls,activated_at FROM licenses WHERE label LIKE ? OR key_prefix LIKE ? OR telegram_id LIKE ? ORDER BY id DESC LIMIT 20",(f"%{q}%",f"%{q}%",f"%{q}%")).fetchall()
            else: rows=c.execute("SELECT id,label,key_prefix,expires_at,active,calls,activated_at FROM licenses ORDER BY id DESC LIMIT 20").fetchall()
        if not rows: send(chat,"Không tìm thấy key."); return
        lines=["KEY GẦN ĐÂY · tối đa 20"]
        for r in rows:
            state="CHƯA KÍCH HOẠT" if not r["activated_at"] else ("HẾT HẠN" if r["expires_at"]<=iso(now()) else ("ĐANG BẬT" if r["active"] else "ĐÃ KHÓA"))
            lines.append(f"#{r['id']} · {r['label']} · {r['key_prefix']}••• · {state} · {r['expires_at']} · gọi {r['calls']}")
        send(chat,"\n".join(lines)); return
    if cmd == "/key":
        if not args or not args[0].isdigit(): send(chat,"Cú pháp: /key <ID>"); return
        with db() as c:r=c.execute("SELECT * FROM licenses WHERE id=?",(int(args[0]),)).fetchone()
        if not r:send(chat,"Không tìm thấy key.");return
        state="CHƯA KÍCH HOẠT" if not r["activated_at"] else ("HẾT HẠN" if r["expires_at"]<=iso(now()) else ("ĐANG BẬT" if r["active"] else "ĐÃ KHÓA"))
        send(chat,f"KEY #{r['id']}\nTên: {r['label']}\nPrefix: {r['key_prefix']}•••\nTrạng thái: {state}\nThời hạn: {r['duration_days']} ngày\nKích hoạt: {r['activated_at'] or 'Chưa'}\nHạn: {r['expires_at']}\nAPI calls: {r['calls']}\nTelegram: {r['telegram_id'] or 'Chưa liên kết'}\nThiết bị: {r['device_id'] or 'Chưa liên kết'}");return
    if cmd in ("/revoke", "/enable", "/unbind"):
        if not args or not args[0].isdigit():send(chat,f"Cú pháp: {cmd} <ID>");return
        lid=int(args[0])
        with db() as c:
            r=c.execute("SELECT id,active,expires_at,activated_at FROM licenses WHERE id=?",(lid,)).fetchone()
            if not r:send(chat,"Không tìm thấy key.");return
            if cmd=="/unbind":c.execute("UPDATE licenses SET device_id=NULL WHERE id=?",(lid,)); detail="đã gỡ thiết bị"
            else:
                if cmd=="/enable" and r["activated_at"] and r["expires_at"]<=iso(now()):send(chat,"Key đã hết hạn; hãy gia hạn trước.");return
                active=0 if cmd=="/revoke" else 1;c.execute("UPDATE licenses SET active=? WHERE id=?",(active,lid));detail="đã khóa" if not active else "đã mở"
        event("license_admin",f"Telegram admin {cmd} key #{lid}: {detail}");send(chat,f"Key #{lid} {detail}.");return
    if cmd == "/extend":
        if len(args)<2 or not args[0].isdigit():send(chat,"Cú pháp: /extend <ID> <số ngày>");return
        lid=int(args[0])
        try:days=int(args[1])
        except ValueError:days=0
        if not 1<=days<=3650:send(chat,"Số ngày phải trong khoảng 1–3650.");return
        with db() as c:
            r=c.execute("SELECT expires_at,activated_at,duration_days FROM licenses WHERE id=?",(lid,)).fetchone()
            if not r:send(chat,"Không tìm thấy key.");return
            if not r["activated_at"]:
                duration=int(r["duration_days"] or 0)+days; expiry=now()+timedelta(days=duration)
                c.execute("UPDATE licenses SET duration_days=?,expires_at=?,active=1 WHERE id=?",(duration,iso(expiry),lid))
            else:
                try:old=datetime.fromisoformat(r["expires_at"])
                except Exception:old=now()
                expiry=max(old,now())+timedelta(days=days)
                c.execute("UPDATE licenses SET expires_at=?,duration_days=COALESCE(duration_days,0)+?,active=1 WHERE id=?",(iso(expiry),days,lid))
        event("license_extended",f"Telegram admin gia hạn key ID {lid} thêm {days} ngày");send(chat,f"Đã gia hạn key #{lid}. Hạn mới: {iso(expiry)}");return
    if cmd in ("/tool", "/toolstatus", "/api"):
        send(chat, tool_status_text(), menu_keyboard(True)); return
    if cmd == "/stats":
        with db() as c:
            total=c.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
            active=c.execute("SELECT COUNT(*) FROM licenses WHERE active=1 AND (activated_at IS NULL OR expires_at>?)",(iso(),)).fetchone()[0]
            expired=c.execute("SELECT COUNT(*) FROM licenses WHERE activated_at IS NOT NULL AND expires_at<=?",(iso(),)).fetchone()[0]
            orders=c.execute("SELECT COUNT(*) FROM orders WHERE status='PENDING'").fetchone()[0]
        send(chat,f"PAWNOVA · STATS\nTổng key: {total}\nHoạt động/chưa kích hoạt: {active}\nHết hạn: {expired}\nĐơn chờ: {orders}");return
    if cmd == "/users":
        with db() as c:rows=c.execute("SELECT telegram_id,COUNT(*) AS n FROM licenses WHERE telegram_id IS NOT NULL AND telegram_id<>'' GROUP BY telegram_id ORDER BY MAX(id) DESC LIMIT 30").fetchall()
        send(chat,"KHÁCH ĐÃ LIÊN KẾT\n"+"\n".join(f"{r['telegram_id']} · {r['n']} key" for r in rows) if rows else "Chưa có tài khoản liên kết.");return
    if cmd == "/events":
        with db() as c:rows=c.execute("SELECT kind,message,created_at FROM events ORDER BY id DESC LIMIT 15").fetchall()
        send(chat,"\n".join(f"{r['created_at']} · {r['kind']} · {r['message']}" for r in rows) if rows else "Chưa có nhật ký.");return
    if cmd == "/broadcast":
        msg=" ".join(args).strip()
        if not msg:send(chat,"Cú pháp: /broadcast Nội dung thông báo");return
        with db() as c:rows=c.execute("SELECT DISTINCT telegram_id FROM licenses WHERE telegram_id IS NOT NULL AND telegram_id<>''").fetchall()
        ok=fail=0
        for row in rows:
            try:tg("sendMessage",chat_id=row["telegram_id"],text=msg[:3500],disable_web_page_preview=True);ok+=1
            except Exception:fail+=1
            time.sleep(.04)
        event("broadcast",f"Telegram admin broadcast {ok} sent / {fail} failed");send(chat,f"Hoàn tất gửi thông báo: {ok} thành công, {fail} lỗi.");return
    send(chat,"Lệnh chưa nhận diện. Gõ /help.")

def process(update):
    cb = update.get("callback_query")
    if cb:
        user = str((cb.get("from") or {}).get("id", "")); chat = cb.get("message", {}).get("chat", {}).get("id")
        data = cb.get("data", "")
        try: tg("answerCallbackQuery", callback_query_id=cb.get("id"))
        except Exception: pass
        is_admin = user in ADMIN_IDS
        if data == "mykey": send(chat, customer_status(user), menu_keyboard(is_admin))
        elif data == "status": send(chat, "Tài khoản PAWNOVA\n" + customer_status(user), menu_keyboard(is_admin))
        elif data == "link_help": send(chat, "Gửi lệnh /link KEY để liên kết. Tin nhắn chứa key sẽ được bot cố gắng xóa sau khi xử lý.", menu_keyboard(is_admin))
        elif data == "help": send(chat, HELP, menu_keyboard(is_admin))
        elif data == "tool" and is_admin: send(chat, tool_status_text(), menu_keyboard(True))
        elif data == "stats" and is_admin: admin_process(chat, user, "/stats", [])
        elif data == "keys" and is_admin: admin_process(chat, user, "/keys", [])
        elif data == "users" and is_admin: admin_process(chat, user, "/users", [])
        elif data == "events" and is_admin: admin_process(chat, user, "/events", [])
        return
    m=update.get("message")
    if not m or not m.get("text"): return
    user=str((m.get("from") or {}).get("id", "")); chat=m["chat"]["id"]
    parts=m["text"].strip().split(maxsplit=1); cmd=parts[0].split("@")[0].lower(); payload=parts[1] if len(parts)>1 else ""; args=payload.split()
    if cmd in ("/start", "/help"):
        send(chat, "PAWNOVA CONTROL · Chọn chức năng bên dưới.", menu_keyboard(user in ADMIN_IDS)); return
    if cmd=="/link":
        key=payload.strip()
        if not key: send(chat,"Cú pháp: /link <KEY>"); return
        reply=link_key(user,key); delete_user_message(chat,m.get("message_id")); send(chat,reply,menu_keyboard(user in ADMIN_IDS)); return
    if cmd in ("/mykey","/mystatus"):
        send(chat,customer_status(user),menu_keyboard(user in ADMIN_IDS)); return
    if user not in ADMIN_IDS: return
    admin_process(chat,user,cmd,args)

def send_expiry_reminders():
    """Send at most one reminder per key and UTC+7 date; durable marker prevents duplicates."""
    if not TOKEN:return
    current=now(); sent=0
    with db() as c:
        rows=c.execute("SELECT id,label,telegram_id,expires_at FROM licenses WHERE active=1 AND activated_at IS NOT NULL AND telegram_id IS NOT NULL AND telegram_id<>'' AND expires_at>? AND expires_at<=?",(iso(current),iso(current+timedelta(hours=24)))).fetchall()
        for r in rows:
            period=current.strftime("%Y-%m-%d")
            exists=c.execute("SELECT 1 FROM bot_notifications WHERE license_id=? AND notice_type='expiry_24h' AND period_key=?",(r["id"],period)).fetchone()
            if exists:continue
            try:
                tg("sendMessage",chat_id=r["telegram_id"],text=f"PAWNOVA nhắc hạn: key #{r['id']} ({r['label']}) sẽ hết hạn trong 24 giờ, vào {r['expires_at']}. Dùng /mykey để kiểm tra.")
                c.execute("INSERT OR IGNORE INTO bot_notifications(license_id,notice_type,period_key,sent_at) VALUES(?,'expiry_24h',?,?)",(r["id"],period,iso()));sent+=1
            except Exception:pass
    if sent:event("expiry_reminder",f"Đã gửi {sent} nhắc hạn Telegram")

def main():
    if not TOKEN:print("TELEGRAM_BOT_TOKEN missing; bot disabled.",flush=True);return
    print(f"PAWNOVA Telegram bot started; admins={len(ADMIN_IDS)}",flush=True)
    offset=None;last_reminder=0
    while True:
        try:
            if time.time()-last_reminder>=1800:
                send_expiry_reminders();last_reminder=time.time()
            data={"timeout":25}
            if offset is not None:data["offset"]=offset
            updates=tg("getUpdates",**data)
            for upd in updates:
                offset=upd["update_id"]+1
                try:process(upd)
                except Exception as exc:print("Telegram update error:",type(exc).__name__,str(exc)[:160],flush=True)
        except KeyboardInterrupt:break
        except Exception as exc:print("Telegram polling error:",type(exc).__name__,str(exc)[:160],flush=True);time.sleep(4)

if __name__=="__main__":main()
