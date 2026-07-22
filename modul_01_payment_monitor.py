#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Payment Monitor (modul_01_payment_monitor) — An Cư Hà Nội
Đối soát thanh toán, phát hiện nợ quá hạn, cảnh báo Telegram.

Priority: CRITICAL · Phase 1

Cách chạy (2 chế độ, tự nhận biết qua biến môi trường PORT):
  - Web Service (Render đặt PORT): chạy đối soát 1 lần lúc khởi động,
    mở cổng HTTP báo trạng thái, tự lặp lại mỗi CHECK_INTERVAL_HOURS giờ.
  - Cron Job / chạy tay (không có PORT): đối soát 1 lần rồi thoát.

Biến môi trường dùng đến:
  DATABASE_URL            postgresql://... (bắt buộc để đối soát thật)
  TELEGRAM_BOT_TOKEN      token bot (tùy chọn — không có thì bỏ qua cảnh báo)
  TELEGRAM_ALERT_CHANNEL  hoặc TELEGRAM_HAVEN_ID — nơi nhận cảnh báo
  PORT                    Render tự đặt cho Web Service
  CHECK_INTERVAL_HOURS    chu kỳ lặp ở chế độ web (mặc định 6)
"""

import os
import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("payment_monitor")

MODULE_ID = "modul_01_payment_monitor"

# Trạng thái đối soát gần nhất — dùng cho endpoint HTTP
_last_report = {
    "status": "starting",
    "ran_at": None,
    "detail": "Chưa chạy lần đối soát nào.",
}


def _placeholder(value):
    """Giá trị rỗng hoặc còn dạng [PASTE_...] coi như chưa cấu hình."""
    if not value:
        return True
    v = value.strip()
    return v == "" or (v.startswith("[") and v.endswith("]"))


def get_database_url():
    """Lấy DATABASE_URL, hoặc dựng từ các phần rời nếu cần."""
    url = os.getenv("DATABASE_URL", "").strip()
    if url and not _placeholder(url):
        return url
    host = os.getenv("DATABASE_HOST", "").strip()
    if _placeholder(host):
        return None
    user = os.getenv("DATABASE_USER", "postgres").strip()
    pwd = os.getenv("DATABASE_PASSWORD", "").strip()
    port = os.getenv("DATABASE_PORT", "5432").strip()
    name = os.getenv("DATABASE_NAME", "postgres").strip()
    return f"postgresql://{user}:{pwd}@{host}:{port}/{name}"


# Region của project trên Supabase (không phải bí mật — chỉ là vị trí máy chủ).
# Dùng để tự chuyển host trực tiếp (chỉ IPv6, Render free không tới được)
# sang Connection Pooler (IPv4) khi cần.
SUPABASE_POOLER_REGION = os.getenv("SUPABASE_POOLER_REGION", "ap-northeast-2").strip()


def get_connection_candidates():
    """
    Trả về danh sách URL kết nối để thử theo thứ tự.
    Nếu host là dạng Supabase trực tiếp (db.<ref>.supabase.co, chỉ IPv6),
    ưu tiên các URL pooler (IPv4) để chạy được trên Render, rồi mới tới URL gốc.
    """
    import re
    from urllib.parse import urlparse

    base = get_database_url()
    if not base:
        return []

    candidates = []
    try:
        p = urlparse(base)
        host = p.hostname or ""
        m = re.match(r"^db\.([a-z0-9]+)\.supabase\.co$", host)
        if m:
            ref = m.group(1)
            pwd = p.password or ""
            dbname = (p.path or "/postgres").lstrip("/") or "postgres"
            region = SUPABASE_POOLER_REGION
            for node in ("aws-1", "aws-0"):
                for port in (6543, 5432):
                    ph = f"{node}-{region}.pooler.supabase.com"
                    candidates.append(
                        f"postgresql://postgres.{ref}:{pwd}@{ph}:{port}/{dbname}"
                    )
    except Exception as e:  # noqa: BLE001
        logger.warning("Không phân tích được DATABASE_URL: %s", e)

    candidates.append(base)  # host gốc để dự phòng (nơi có IPv6)
    return candidates


def connect_db():
    """Thử lần lượt các URL ứng viên, trả về (conn, url) đầu tiên kết nối được."""
    import psycopg2

    last_err = None
    for url in get_connection_candidates():
        try:
            conn = psycopg2.connect(url, connect_timeout=15)
            safe = url.split("@")[-1]  # ẩn user/mật khẩu khi log
            logger.info("Kết nối DB thành công qua %s", safe)
            return conn, url
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("Không kết nối được (%s): %s", url.split("@")[-1], str(e).splitlines()[0])
    if last_err:
        raise last_err
    raise RuntimeError("Không có URL kết nối nào khả dụng.")


def send_telegram_alert(text):
    """Gửi cảnh báo qua Telegram nếu đã cấu hình. Không có thì bỏ qua êm."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_ALERT_CHANNEL", "") or os.getenv("TELEGRAM_HAVEN_ID", "")
    if _placeholder(token) or _placeholder(chat_id):
        logger.info("Telegram chưa cấu hình — bỏ qua gửi cảnh báo.")
        return False
    try:
        import requests

        resp = requests.post(
            f"https://api.telegram.org/bot{token.strip()}/sendMessage",
            json={"chat_id": chat_id.strip(), "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if resp.status_code == 200:
            logger.info("Đã gửi cảnh báo Telegram.")
            return True
        logger.warning("Telegram trả về mã %s: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("Lỗi gửi Telegram: %s", e)
        return False


def _log_to_db(cur, level, message, details=None):
    try:
        cur.execute(
            "INSERT INTO module_logs (module_id, log_level, message, error_details) "
            "VALUES (%s, %s, %s, %s)",
            (MODULE_ID, level, message, json.dumps(details) if details else None),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Không ghi được module_logs: %s", e)


def _update_health(cur, status, notes, success, error):
    try:
        cur.execute(
            "INSERT INTO module_health_status "
            "(module_id, status, last_check, error_count, success_count, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (MODULE_ID, status, datetime.now(timezone.utc), error, success, notes),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Không ghi được module_health_status: %s", e)


def run_payment_check():
    """Đối soát 1 lần: tìm hóa đơn quá hạn, cập nhật OVERDUE, cảnh báo, ghi log."""
    global _last_report
    ran_at = datetime.now(timezone.utc).isoformat()

    if not get_database_url():
        _last_report = {
            "status": "config_error",
            "ran_at": ran_at,
            "detail": "Thiếu DATABASE_URL — không thể kết nối Supabase.",
        }
        logger.error(_last_report["detail"])
        return _last_report

    try:
        import psycopg2.extras
    except ImportError:
        _last_report = {
            "status": "dependency_error",
            "ran_at": ran_at,
            "detail": "Chưa cài psycopg2-binary.",
        }
        logger.error(_last_report["detail"])
        return _last_report

    conn = None
    try:
        conn, _ = connect_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # 1) Tìm hóa đơn PENDING đã quá hạn (due_date < hôm nay)
        cur.execute(
            "SELECT id, invoice_id, tenant_name, room_code, amount, due_date "
            "FROM payment_monitor_logs "
            "WHERE status = 'PENDING' AND due_date IS NOT NULL "
            "AND due_date < CURRENT_DATE "
            "ORDER BY due_date ASC"
        )
        overdue = cur.fetchall()

        # 2) Đánh dấu các hóa đơn đó thành OVERDUE
        if overdue:
            ids = [row["id"] for row in overdue]
            cur.execute(
                "UPDATE payment_monitor_logs SET status = 'OVERDUE' "
                "WHERE id = ANY(%s)",
                (ids,),
            )

        # 3) Tổng hợp trạng thái hiện tại
        cur.execute(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total "
            "FROM payment_monitor_logs GROUP BY status"
        )
        summary = {r["status"]: {"count": r["n"], "total": float(r["total"])} for r in cur.fetchall()}

        total_overdue_amount = sum(float(r["amount"] or 0) for r in overdue)

        # 4) Ghi log + health
        _log_to_db(
            cur,
            "INFO",
            f"Đối soát xong: {len(overdue)} hóa đơn chuyển OVERDUE.",
            {"overdue_count": len(overdue), "summary": summary},
        )
        _update_health(cur, "HEALTHY", "Đối soát thành công", success=1, error=0)
        conn.commit()

        # 5) Cảnh báo Telegram nếu có nợ mới quá hạn
        if overdue:
            lines = [
                "🔴 <b>CẢNH BÁO NỢ QUÁ HẠN — An Cư Hà Nội</b>",
                f"Có <b>{len(overdue)}</b> hóa đơn vừa chuyển sang quá hạn:",
                "",
            ]
            for r in overdue[:20]:
                amt = f"{float(r['amount'] or 0):,.0f}đ"
                lines.append(
                    f"• {r['room_code'] or '?'} — {r['tenant_name'] or '?'} — "
                    f"{amt} (hạn {r['due_date']})"
                )
            lines.append("")
            lines.append(f"<b>Tổng nợ quá hạn mới: {total_overdue_amount:,.0f}đ</b>")
            send_telegram_alert("\n".join(lines))

        _last_report = {
            "status": "ok",
            "ran_at": ran_at,
            "detail": f"{len(overdue)} hóa đơn chuyển OVERDUE.",
            "overdue_count": len(overdue),
            "overdue_amount": total_overdue_amount,
            "summary": summary,
        }
        logger.info("Đối soát hoàn tất: %s hóa đơn quá hạn.", len(overdue))
        return _last_report

    except Exception as e:  # noqa: BLE001
        if conn:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        _last_report = {
            "status": "db_error",
            "ran_at": ran_at,
            "detail": f"Lỗi kết nối/truy vấn Supabase: {e}",
        }
        logger.error(_last_report["detail"])
        return _last_report
    finally:
        if conn:
            conn.close()


class _StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps(
            {"module": MODULE_ID, "service": "payment-monitor", **_last_report},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # tắt log HTTP ồn ào
        return


def _scheduler_loop(interval_hours):
    import time

    while True:
        time.sleep(max(1, interval_hours) * 3600)
        logger.info("Đến chu kỳ — chạy lại đối soát.")
        run_payment_check()


def main():
    logger.info("Khởi động %s", MODULE_ID)

    # Chạy đối soát ngay lúc khởi động (công việc thật)
    run_payment_check()

    port = os.getenv("PORT", "").strip()
    if not port:
        # Cron Job / chạy tay: xong là thoát
        logger.info("Không có PORT — chế độ chạy một lần. Kết thúc.")
        return

    # Web Service: tự lặp nền + mở cổng HTTP để Render thấy service Live
    interval = int(os.getenv("CHECK_INTERVAL_HOURS", "6") or "6")
    threading.Thread(target=_scheduler_loop, args=(interval,), daemon=True).start()

    server = HTTPServer(("0.0.0.0", int(port)), _StatusHandler)
    logger.info("Lắng nghe HTTP trên cổng %s (chu kỳ đối soát %sh).", port, interval)
    server.serve_forever()


if __name__ == "__main__":
    main()
