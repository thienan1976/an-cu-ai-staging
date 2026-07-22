#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Payment Monitor (modul_01_payment_monitor) — An Cư Hà Nội
Đối soát thanh toán + TOOL QUẢN TRỊ WEB (dashboard có mật khẩu).

Chạy 2 chế độ (tự nhận qua biến môi trường PORT):
  - Web Service (Render đặt PORT): đối soát lúc khởi động + lặp mỗi CHECK_INTERVAL_HOURS,
    đồng thời phục vụ trang quản trị web tại "/" (có đăng nhập).
  - Cron Job / chạy tay (không PORT): đối soát 1 lần rồi thoát.

Trang quản trị:
  GET  /            → dashboard HTML (cần đăng nhập Basic Auth)
  GET  /status      → JSON kỹ thuật (không cần đăng nhập, cho health check)
  GET  /api/payments→ danh sách hóa đơn (JSON, cần đăng nhập)
  POST /api/mark-paid {id}         → đánh dấu ĐÃ THU
  POST /api/add {room_code,...}    → thêm hóa đơn
  POST /api/run                    → chạy đối soát ngay

Đăng nhập: user tùy ý, mật khẩu = ADMIN_PASSWORD (nếu đặt) hoặc DATABASE_PASSWORD.

Biến môi trường:
  DATABASE_URL / DATABASE_* , TELEGRAM_BOT_TOKEN, TELEGRAM_ALERT_CHANNEL,
  PORT, CHECK_INTERVAL_HOURS (mặc định 6), ADMIN_PASSWORD (tùy chọn)
"""

import os
import re
import json
import base64
import logging
import threading
from datetime import datetime, timezone, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("payment_monitor")

MODULE_ID = "modul_01_payment_monitor"
SUPABASE_POOLER_REGION = os.getenv("SUPABASE_POOLER_REGION", "ap-northeast-2").strip()

_last_report = {"status": "starting", "ran_at": None, "detail": "Chưa chạy đối soát."}
_run_lock = threading.Lock()


# ------------------------------------------------------------------ tiện ích
def _placeholder(value):
    if not value:
        return True
    v = value.strip()
    return v == "" or (v.startswith("[") and v.endswith("]"))


def get_database_url():
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


def get_connection_candidates():
    """Ưu tiên pooler (IPv4) nếu host là Supabase trực tiếp (chỉ IPv6)."""
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
            for node in ("aws-1", "aws-0"):
                for port in (6543, 5432):
                    ph = f"{node}-{SUPABASE_POOLER_REGION}.pooler.supabase.com"
                    candidates.append(f"postgresql://postgres.{ref}:{pwd}@{ph}:{port}/{dbname}")
    except Exception as e:  # noqa: BLE001
        logger.warning("Không phân tích được DATABASE_URL: %s", e)
    candidates.append(base)
    return candidates


def connect_db():
    import psycopg2
    last_err = None
    for url in get_connection_candidates():
        try:
            conn = psycopg2.connect(url, connect_timeout=15)
            logger.info("Kết nối DB qua %s", url.split("@")[-1])
            return conn
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("Không kết nối được (%s): %s", url.split("@")[-1], str(e).splitlines()[0])
    if last_err:
        raise last_err
    raise RuntimeError("Không có URL kết nối khả dụng.")


def admin_password():
    p = os.getenv("ADMIN_PASSWORD", "").strip()
    if p and not _placeholder(p):
        return p
    return os.getenv("DATABASE_PASSWORD", "").strip()


def send_telegram_alert(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_ALERT_CHANNEL", "") or os.getenv("TELEGRAM_HAVEN_ID", "")
    if _placeholder(token) or _placeholder(chat_id):
        logger.info("Telegram chưa cấu hình — bỏ qua.")
        return False
    try:
        import requests
        r = requests.post(
            f"https://api.telegram.org/bot{token.strip()}/sendMessage",
            json={"chat_id": chat_id.strip(), "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        logger.warning("Lỗi Telegram: %s", e)
        return False


# ------------------------------------------------------------ thao tác DB
def db_list_payments():
    import psycopg2.extras
    conn = connect_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, room_code, tenant_name, amount, due_date, payment_date, status, notes "
            "FROM payment_monitor_logs "
            "ORDER BY CASE status WHEN 'OVERDUE' THEN 0 WHEN 'PENDING' THEN 1 ELSE 2 END, due_date"
        )
        rows = cur.fetchall()
        today = date.today()
        out = []
        for r in rows:
            due = r["due_date"]
            days = (due - today).days if due else None
            out.append({
                "id": r["id"], "room_code": r["room_code"], "tenant_name": r["tenant_name"],
                "amount": float(r["amount"]) if r["amount"] is not None else None,
                "due_date": due.isoformat() if due else None,
                "payment_date": r["payment_date"].isoformat() if r["payment_date"] else None,
                "status": r["status"], "notes": r["notes"], "days": days,
            })
        return out
    finally:
        conn.close()


def db_mark_paid(inv_id):
    conn = connect_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE payment_monitor_logs SET status='PAID', payment_date=CURRENT_DATE WHERE id=%s",
            (int(inv_id),),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def db_add_payment(room_code, tenant_name, amount, due_date, notes=None, status="PENDING"):
    conn = connect_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO payment_monitor_logs "
            "(invoice_id, room_code, tenant_name, amount, due_date, status, notes) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (f"{room_code}-{str(due_date).replace('-','')[:6]}", room_code, tenant_name,
             float(amount), due_date, (status or "PENDING").upper(), notes),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def db_update_payment(inv_id, room_code, tenant_name, amount, due_date, status, notes=None):
    conn = connect_db()
    try:
        cur = conn.cursor()
        pay_date = None
        if (status or "").upper() == "PAID":
            cur.execute("SELECT payment_date FROM payment_monitor_logs WHERE id=%s", (int(inv_id),))
            row = cur.fetchone()
            pay_date = (row[0] if row and row[0] else date.today())
        cur.execute(
            "UPDATE payment_monitor_logs SET room_code=%s, tenant_name=%s, amount=%s, "
            "due_date=%s, status=%s, notes=%s, payment_date=%s WHERE id=%s",
            (room_code, tenant_name, float(amount), due_date, (status or "PENDING").upper(),
             notes, pay_date, int(inv_id)),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def db_delete_payment(inv_id):
    conn = connect_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM payment_monitor_logs WHERE id=%s", (int(inv_id),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ------------------------------------------------------- đối soát chính
def run_payment_check():
    global _last_report
    ran_at = datetime.now(timezone.utc).isoformat()
    if not get_database_url():
        _last_report = {"status": "config_error", "ran_at": ran_at, "detail": "Thiếu DATABASE_URL."}
        return _last_report
    try:
        import psycopg2.extras
    except ImportError:
        _last_report = {"status": "dependency_error", "ran_at": ran_at, "detail": "Chưa cài psycopg2-binary."}
        return _last_report

    conn = None
    try:
        conn = connect_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, tenant_name, room_code, amount, due_date FROM payment_monitor_logs "
            "WHERE status='PENDING' AND due_date IS NOT NULL AND due_date < CURRENT_DATE "
            "ORDER BY due_date ASC"
        )
        overdue = cur.fetchall()
        if overdue:
            ids = [r["id"] for r in overdue]
            cur.execute("UPDATE payment_monitor_logs SET status='OVERDUE' WHERE id = ANY(%s)", (ids,))
        cur.execute(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(amount),0) AS total "
            "FROM payment_monitor_logs GROUP BY status"
        )
        summary = {r["status"]: {"count": r["n"], "total": float(r["total"])} for r in cur.fetchall()}
        total_overdue = sum(float(r["amount"] or 0) for r in overdue)
        try:
            cur.execute(
                "INSERT INTO module_logs (module_id, log_level, message) VALUES (%s,%s,%s)",
                (MODULE_ID, "INFO", f"Đối soát: {len(overdue)} chuyển OVERDUE."),
            )
        except Exception:  # noqa: BLE001
            pass
        conn.commit()

        if overdue:
            lines = ["🔴 <b>NỢ QUÁ HẠN — An Cư Hà Nội</b>",
                     f"{len(overdue)} hóa đơn vừa quá hạn:", ""]
            for r in overdue[:20]:
                lines.append(f"• {r['room_code'] or '?'} — {r['tenant_name'] or '?'} — "
                             f"{float(r['amount'] or 0):,.0f}đ (hạn {r['due_date']})")
            lines += ["", f"<b>Tổng: {total_overdue:,.0f}đ</b>"]
            send_telegram_alert("\n".join(lines))

        _last_report = {"status": "ok", "ran_at": ran_at,
                        "detail": f"{len(overdue)} hóa đơn chuyển OVERDUE.",
                        "overdue_count": len(overdue), "overdue_amount": total_overdue,
                        "summary": summary}
        logger.info("Đối soát xong: %s quá hạn.", len(overdue))
        return _last_report
    except Exception as e:  # noqa: BLE001
        if conn:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        _last_report = {"status": "db_error", "ran_at": ran_at, "detail": f"Lỗi Supabase: {e}"}
        logger.error(_last_report["detail"])
        return _last_report
    finally:
        if conn:
            conn.close()


# ------------------------------------------------------------ HTTP server
DASHBOARD_HTML = r"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quản trị Thanh toán — An Cư Hà Nội</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--line:#334155;--txt:#e2e8f0;--muted:#94a3b8}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt)}
.wrap{max-width:1000px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0}
.sub{color:var(--muted);font-size:13px;margin-bottom:16px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.c{flex:1;min-width:150px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.c .n{font-size:22px;font-weight:700}.c .l{color:var(--muted);font-size:12px}
.red .n{color:#f87171}.yel .n{color:#fbbf24}.grn .n{color:#34d399}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
button{cursor:pointer;border:0;border-radius:8px;padding:8px 12px;font-size:13px;font-weight:600}
.b1{background:#2563eb;color:#fff}.b2{background:#334155;color:#e2e8f0}.paid{background:#059669;color:#fff}.del{background:#7f1d1d;color:#fecaca}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:12px;overflow:hidden}
th,td{padding:10px 12px;text-align:left;font-size:13px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase}
.badge{padding:2px 8px;border-radius:99px;font-size:11px;font-weight:700}
.OVERDUE{background:#7f1d1d;color:#fecaca}.PENDING{background:#78350f;color:#fde68a}.PAID{background:#064e3b;color:#a7f3d0}
.money{font-variant-numeric:tabular-nums;font-weight:600}
.tblwrap{overflow-x:auto}
dialog{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:12px;padding:18px;width:min(92vw,380px)}
dialog input{width:100%;padding:8px;margin:6px 0 12px;background:var(--bg);border:1px solid var(--line);border-radius:8px;color:var(--txt)}
label{font-size:12px;color:var(--muted)}
.err{color:#f87171;font-size:13px;margin-top:8px}
</style></head><body><div class="wrap">
<h1>💰 Quản trị Thanh toán — Module 01</h1>
<div class="sub">An Cư Hà Nội · dữ liệu trực tiếp từ Supabase · cập nhật lúc <span id="t">—</span></div>
<div class="cards">
  <div class="c red"><div class="n" id="s-over">—</div><div class="l">Quá hạn (OVERDUE)</div></div>
  <div class="c yel"><div class="n" id="s-pend">—</div><div class="l">Chờ thu (PENDING)</div></div>
  <div class="c grn"><div class="n" id="s-paid">—</div><div class="l">Đã thu (PAID)</div></div>
</div>
<div class="bar">
  <button class="b1" onclick="add()">＋ Thêm hóa đơn</button>
  <button class="b2" onclick="runCheck()">🔄 Chạy đối soát ngay</button>
  <button class="b2" onclick="load()">↻ Tải lại</button>
</div>
<div class="tblwrap"><table><thead><tr>
<th>Phòng</th><th>Khách</th><th>Số tiền</th><th>Hạn</th><th>Còn/Quá</th><th>Trạng thái</th><th></th>
</tr></thead><tbody id="rows"><tr><td colspan="7">Đang tải…</td></tr></tbody></table></div>
<div class="err" id="err"></div>
</div>

<dialog id="dlg"><h3 style="margin-top:0" id="dlg-title">Thêm hóa đơn</h3>
<label>Mã phòng</label><input id="f-room" placeholder="KD-P6A">
<label>Tên khách</label><input id="f-ten" placeholder="Nguyễn Văn A">
<label>Số tiền (đ)</label><input id="f-amt" type="number" placeholder="5000000">
<label>Ngày đến hạn</label><input id="f-due" type="date">
<label>Trạng thái</label><select id="f-stt" style="width:100%;padding:8px;margin:6px 0 12px;background:var(--bg);border:1px solid var(--line);border-radius:8px;color:var(--txt)">
<option value="PENDING">PENDING — chờ thu</option><option value="OVERDUE">OVERDUE — quá hạn</option><option value="PAID">PAID — đã thu</option></select>
<label>Ghi chú</label><input id="f-note" placeholder="(tùy chọn)">
<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">
<button class="b2" onclick="dlg.close()">Hủy</button><button class="b1" onclick="submitForm()">Lưu</button></div>
</dialog>

<script>
const $=id=>document.getElementById(id);
const vnd=n=>n==null?'—':n.toLocaleString('vi-VN')+'đ';
let PAYMENTS=[], EDIT_ID=null;
async function api(path,method,body){
  const r=await fetch(path,{method:method||'GET',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});
  if(!r.ok)throw new Error('HTTP '+r.status);return r.json();
}
async function load(){
  try{
    const d=await api('/api/payments');PAYMENTS=d;
    let over=0,pend=0,paid=0,ov=0,pe=0;
    const tb=$('rows');tb.innerHTML='';
    d.forEach(p=>{
      if(p.status==='OVERDUE'){over++;ov+=p.amount||0;}
      else if(p.status==='PENDING'){pend++;pe+=p.amount||0;}
      else if(p.status==='PAID')paid++;
      const dd=p.days==null?'—':(p.days<0?('quá '+(-p.days)+'n'):('còn '+p.days+'n'));
      const paidBtn=p.status==='PAID'?'':`<button class="paid" onclick="markPaid(${p.id})">Đã thu</button> `;
      const acts=`${paidBtn}<button class="b2" onclick="edit(${p.id})">Sửa</button> <button class="del" onclick="del(${p.id})">Xóa</button>`;
      tb.insertAdjacentHTML('beforeend',
       `<tr><td>${p.room_code||''}</td><td>${p.tenant_name||''}</td>
        <td class="money">${vnd(p.amount)}</td><td>${p.due_date||''}</td><td>${dd}</td>
        <td><span class="badge ${p.status}">${p.status}</span></td><td style="white-space:nowrap">${acts}</td></tr>`);
    });
    $('s-over').textContent=over+' · '+vnd(ov);
    $('s-pend').textContent=pend+' · '+vnd(pe);
    $('s-paid').textContent=paid;
    $('t').textContent=new Date().toLocaleString('vi-VN');
    $('err').textContent='';
  }catch(e){$('err').textContent='Lỗi tải dữ liệu: '+e.message;}
}
async function markPaid(id){
  if(!confirm('Đánh dấu hóa đơn này ĐÃ THU?'))return;
  try{await api('/api/mark-paid','POST',{id});load();}catch(e){alert('Lỗi: '+e.message);}
}
function fillForm(p){
  $('f-room').value=p?p.room_code||'':'';$('f-ten').value=p?p.tenant_name||'':'';
  $('f-amt').value=p?p.amount||'':'';$('f-due').value=p?p.due_date||'':'';
  $('f-stt').value=p?p.status||'PENDING':'PENDING';$('f-note').value=p?p.notes||'':'';
}
function add(){EDIT_ID=null;$('dlg-title').textContent='Thêm hóa đơn';fillForm(null);$('dlg').showModal();}
function edit(id){
  const p=PAYMENTS.find(x=>x.id===id);if(!p)return;
  EDIT_ID=id;$('dlg-title').textContent='Sửa hóa đơn — '+(p.room_code||'');fillForm(p);$('dlg').showModal();
}
async function del(id){
  const p=PAYMENTS.find(x=>x.id===id);
  if(!confirm('XÓA hóa đơn '+(p?p.room_code+' — '+p.tenant_name:id)+' ?\\nKhông khôi phục được.'))return;
  try{await api('/api/delete','POST',{id});load();}catch(e){alert('Lỗi: '+e.message);}
}
async function submitForm(){
  const body={room_code:$('f-room').value.trim(),tenant_name:$('f-ten').value.trim(),
    amount:$('f-amt').value,due_date:$('f-due').value,status:$('f-stt').value,notes:$('f-note').value.trim()};
  if(!body.room_code||!body.amount||!body.due_date){alert('Nhập đủ phòng, số tiền, hạn.');return;}
  try{
    if(EDIT_ID){body.id=EDIT_ID;await api('/api/update','POST',body);}
    else{await api('/api/add','POST',body);}
    $('dlg').close();load();
  }catch(e){alert('Lỗi: '+e.message);}
}
async function runCheck(){
  try{const r=await api('/api/run','POST',{});alert('Đối soát xong: '+(r.detail||''));load();}
  catch(e){alert('Lỗi: '+e.message);}
}
load();setInterval(load,60000);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "PaymentMonitor/1.0"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        pw = admin_password()
        if not pw:
            return True  # chưa có mật khẩu DB thì không chặn (staging)
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                raw = base64.b64decode(hdr[6:]).decode("utf-8")
                _, _, given = raw.partition(":")
                return given == pw
            except Exception:  # noqa: BLE001
                return False
        return False

    def _need_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Quan tri Thanh toan"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Can dang nhap.".encode("utf-8"))

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length", "0") or "0")
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return {}

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/status":
            self._send(200, json.dumps({"module": MODULE_ID, **_last_report}, ensure_ascii=False, indent=2))
            return
        if not self._authed():
            self._need_auth(); return
        if path == "/" or path == "/index.html":
            self._send(200, DASHBOARD_HTML, "text/html; charset=utf-8"); return
        if path == "/api/payments":
            try:
                self._send(200, json.dumps(db_list_payments(), ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if not self._authed():
            self._need_auth(); return
        body = self._body_json()
        try:
            if path == "/api/mark-paid":
                n = db_mark_paid(body.get("id"))
                self._send(200, json.dumps({"ok": True, "updated": n}))
            elif path == "/api/add":
                db_add_payment(body.get("room_code"), body.get("tenant_name"),
                               body.get("amount"), body.get("due_date"),
                               body.get("notes") or None, body.get("status") or "PENDING")
                self._send(200, json.dumps({"ok": True}))
            elif path == "/api/update":
                n = db_update_payment(body.get("id"), body.get("room_code"),
                                      body.get("tenant_name"), body.get("amount"),
                                      body.get("due_date"), body.get("status"),
                                      body.get("notes") or None)
                self._send(200, json.dumps({"ok": True, "updated": n}))
            elif path == "/api/delete":
                n = db_delete_payment(body.get("id"))
                self._send(200, json.dumps({"ok": True, "deleted": n}))
            elif path == "/api/run":
                rep = run_payment_check()
                self._send(200, json.dumps(rep, ensure_ascii=False))
            else:
                self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    def log_message(self, *args):  # tắt log ồn
        return


def _scheduler_loop(interval_hours):
    import time
    while True:
        time.sleep(max(1, interval_hours) * 3600)
        with _run_lock:
            run_payment_check()


def main():
    logger.info("Khởi động %s", MODULE_ID)
    with _run_lock:
        run_payment_check()

    port = os.getenv("PORT", "").strip()
    if not port:
        logger.info("Không có PORT — chế độ chạy một lần. Kết thúc.")
        return

    interval = int(os.getenv("CHECK_INTERVAL_HOURS", "6") or "6")
    threading.Thread(target=_scheduler_loop, args=(interval,), daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", int(port)), _Handler)
    logger.info("Trang quản trị chạy trên cổng %s (đối soát mỗi %sh).", port, interval)
    server.serve_forever()


if __name__ == "__main__":
    main()
