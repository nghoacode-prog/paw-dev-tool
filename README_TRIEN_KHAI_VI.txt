PAWNOVA FULL UPDATE — HƯỚNG DẪN TRIỂN KHAI

Thành phần trong bộ này:
- PAWNOVA_GATEWAY.py: Flask API, cấp phép, kích hoạt, dự đoán, lịch sử, đơn hàng, API admin, backup và worker polling.
- PAWNOVA_ENGINE.py: engine đầy đủ; rút ngắn timeout nguồn dự phòng để giảm nguy cơ stale cache.
- PAWNOVA_ADMINBOT.py: Telegram bot khách hàng và admin (/link, /mykey, /create, /keys, /key, /revoke, /enable, /extend, /unbind, /stats, /users, /events, /broadcast).
- PAWNOVA_CONTROL.html: trang quản trị web (tổng quan, key, khách hàng, đơn hàng, thông báo, trạng thái nguồn, sự kiện, backup).
- PAWNOVA_KEY_ACTIVATION.html: trang client/activation.
- render.yaml + PAWNOVA_REQUIREMENTS.txt: cấu hình triển khai.

TRƯỚC KHI DEPLOY:
1. Sao lưu database hiện tại. Không xóa pawnova.sqlite3 hoặc pawnova.sqlite3.bak.
2. Kiểm tra Render Persistent Disk gắn ở /var/data; nếu database hiện ở đường dẫn khác, cần giữ đúng đường dẫn/migrate có kiểm tra trước khi đổi.
3. Thiết lập PAWNOVA_ADMIN_PASSWORD (mật khẩu mạnh), PAWNOVA_SESSION_SECRET (chuỗi ngẫu nhiên cố định), TELEGRAM_BOT_TOKEN và TELEGRAM_OWNER_ID.
4. Chỉ để MỘT instance/process chạy getUpdates cho bot Telegram. Blueprint dùng gunicorn 1 worker để tránh nhân đôi bot/poller.
5. Trang quản trị: https://<service-domain>/admin
6. Kiểm tra /health; cả predictions.hu và predictions.md5 phải ready=true và source_session cập nhật.
7. Kiểm tra đăng nhập admin, tạo key test, liên kết /link, tạo đơn test, duyệt/từ chối, tin nhắn Telegram và tải backup.

LƯU Ý THANH TOÁN:
- Nút duyệt đơn là thao tác xác nhận thủ công của admin; không tự xác nhận giao dịch ngân hàng.
- Khi duyệt, key được tạo và gửi qua Telegram nếu bot token/khách chat bot hợp lệ.
- Khi từ chối, trang admin hỏi lý do và hệ thống cố gửi lý do cho khách qua Telegram.

GIỚI HẠN KIỂM TRA Ở BẢN NÀY:
- Đã chạy kiểm tra cú pháp Python và kiểm tra cấu trúc HTML/JS cơ bản tại thời điểm đóng gói.
- Chưa deploy lên Render và chưa thể xác nhận nguồn HŨ/MD5 trực tiếp trên dịch vụ live.
- Giá gói hiện đang lấy từ PACKAGES trong PAWNOVA_GATEWAY.py; rà soát lại bảng giá trước khi bán thật.
