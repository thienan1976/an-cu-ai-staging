#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
An Cư Hà Nội — App Quản Trị AI Modules (Phase 1)
Một web app duy nhất phục vụ 3 module, mỗi module 1 tab với CRUD đầy đủ:
  💰 Thanh toán  (payment_monitor_logs)
  📄 Hợp đồng    (contract_alerts)
  📢 Marketing   (marketing_conversations)

Chạy 2 chế độ (tự nhận qua PORT):
  - Web Service (Render đặt PORT): phục vụ dashboard + chạy đối soát nền mỗi 6h.
  - Không PORT: chạy đối soát 1 lần rồi thoát (cho Cron Job).

Đăng nhập Basic Auth: mật khẩu = ADMIN_PASSWORD (nếu đặt) hoặc DATABASE_PASSWORD.
Kết nối Supabase tự chuyển host trực tiếp (IPv6) -> pooler (IPv4) cho Render.
"""

import os
import re
import json
import base64
import logging
import threading
from datetime import datetime, timezone, date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ai_admin")

APP_ID = "an-cu-ai-admin"
SUPABASE_POOLER_REGION = os.getenv("SUPABASE_POOLER_REGION", "ap-northeast-2").strip()
_last_report = {"status": "starting", "ran_at": None, "detail": "Chưa chạy."}
_run_lock = threading.Lock()


# =========================================================== cấu hình module
# Mỗi resource khai báo bảng + cột. Cột name lấy từ đây (an toàn, không phải input).
RESOURCES = {
    "payment": {
        "label": "💰 Thanh toán",
        "table": "payment_monitor_logs",
        "order": "CASE status WHEN 'OVERDUE' THEN 0 WHEN 'PENDING' THEN 1 ELSE 2 END, due_date",
        "badge": "status", "countdown": "due_date",
        "cols": [
            {"k": "room_code", "l": "Phòng", "t": "text"},
            {"k": "tenant_name", "l": "Khách", "t": "text"},
            {"k": "amount", "l": "Số tiền", "t": "number", "money": True},
            {"k": "due_date", "l": "Hạn", "t": "date"},
            {"k": "status", "l": "Trạng thái", "t": "select", "opts": ["PENDING", "OVERDUE", "PAID"]},
            {"k": "notes", "l": "Ghi chú", "t": "text"},
        ],
        "actions": ["run"],
    },
    "contract": {
        "label": "📄 Hợp đồng",
        "table": "contract_alerts",
        "order": "expiry_date",
        "badge": "alert_type", "countdown": "expiry_date",
        "cols": [
            {"k": "contract_id", "l": "Mã HĐ", "t": "text"},
            {"k": "room_code", "l": "Phòng", "t": "text"},
            {"k": "tenant_name", "l": "Khách", "t": "text"},
            {"k": "expiry_date", "l": "Hết hạn", "t": "date"},
            {"k": "alert_type", "l": "Loại", "t": "select", "opts": ["RENEWAL", "WARNING", "TERMINATION"]},
        ],
        "actions": ["run"],
    },
    "marketing": {
        "label": "📢 Marketing",
        "table": "marketing_conversations",
        "order": "id DESC",
        "badge": "conversation_status", "countdown": None,
        "cols": [
            {"k": "conversation_id", "l": "Mã", "t": "text"},
            {"k": "platform", "l": "Kênh", "t": "select", "opts": ["ZALO", "MESSENGER", "FACEBOOK", "TELEGRAM"]},
            {"k": "customer_name", "l": "Khách", "t": "text"},
            {"k": "customer_phone", "l": "SĐT", "t": "text"},
            {"k": "room_inquired", "l": "Phòng hỏi", "t": "text"},
            {"k": "conversation_status", "l": "Trạng thái", "t": "select",
             "opts": ["ACTIVE", "CONVERTED", "ABANDONED"]},
        ],
        "actions": [],
    },
    "tenant": {
        "label": "🏅 Chấm điểm",
        "table": "tenant_scores",
        "order": "overall_score DESC NULLS LAST, id DESC",
        "badge": "recommendation", "countdown": None,
        "cols": [
            {"k": "tenant_name", "l": "Khách", "t": "text"},
            {"k": "room_code", "l": "Phòng", "t": "text"},
            {"k": "payment_score", "l": "Điểm TT", "t": "number"},
            {"k": "behavior_score", "l": "Điểm HV", "t": "number"},
            {"k": "risk_score", "l": "Điểm RR", "t": "number"},
            {"k": "overall_score", "l": "Tổng", "t": "number"},
            {"k": "recommendation", "l": "Đề xuất", "t": "select", "opts": ["GIU", "THEO_DOI", "CANH_BAO"]},
        ],
        "actions": ["run"],
    },
    "asset": {
        "label": "📇 Giấy tờ",
        "table": "asset_ocr_records",
        "order": "id DESC",
        "badge": "document_type", "countdown": None,
        "cols": [
            {"k": "tenant_id", "l": "Mã khách", "t": "text"},
            {"k": "document_type", "l": "Loại GT", "t": "select", "opts": ["CCCD", "METER", "OTHER"]},
            {"k": "confidence_score", "l": "Độ tin (%)", "t": "number"},
            {"k": "verified", "l": "Đã xác minh", "t": "bool"},
        ],
        "actions": [],
    },
    "maintenance": {
        "label": "🔧 Bảo trì",
        "table": "maintenance_predictions",
        "order": "CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, predicted_date NULLS LAST",
        "badge": "severity", "countdown": "predicted_date",
        "cols": [
            {"k": "room_code", "l": "Phòng", "t": "text"},
            {"k": "asset_type", "l": "Thiết bị", "t": "select", "opts": ["AC", "ELECTRICAL", "WATER", "OTHER"]},
            {"k": "failure_probability", "l": "Xác suất hỏng (%)", "t": "number"},
            {"k": "predicted_date", "l": "Dự kiến", "t": "date"},
            {"k": "severity", "l": "Mức độ", "t": "select", "opts": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
            {"k": "recommended_action", "l": "Hành động", "t": "text"},
            {"k": "maintenance_ticket_created", "l": "Đã tạo phiếu", "t": "bool"},
        ],
        "actions": ["run"],
    },
}


# =============================================================== tiện ích DB
def _placeholder(v):
    if not v:
        return True
    v = v.strip()
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
    base = get_database_url()
    if not base:
        return []
    out = []
    try:
        p = urlparse(base)
        m = re.match(r"^db\.([a-z0-9]+)\.supabase\.co$", p.hostname or "")
        if m:
            ref, pwd = m.group(1), (p.password or "")
            db = (p.path or "/postgres").lstrip("/") or "postgres"
            for node in ("aws-1", "aws-0"):
                for port in (6543, 5432):
                    out.append(f"postgresql://postgres.{ref}:{pwd}@{node}-{SUPABASE_POOLER_REGION}.pooler.supabase.com:{port}/{db}")
    except Exception as e:  # noqa: BLE001
        logger.warning("parse DATABASE_URL: %s", e)
    out.append(base)
    return out


def connect_db():
    import psycopg2
    last = None
    for url in get_connection_candidates():
        try:
            c = psycopg2.connect(url, connect_timeout=15)
            return c
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning("connect fail %s: %s", url.split("@")[-1], str(e).splitlines()[0])
    if last:
        raise last
    raise RuntimeError("Không có URL DB.")


def admin_password():
    p = os.getenv("ADMIN_PASSWORD", "").strip()
    if p and not _placeholder(p):
        return p
    return os.getenv("DATABASE_PASSWORD", "").strip()


def send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_ALERT_CHANNEL", "") or os.getenv("TELEGRAM_HAVEN_ID", "")
    if _placeholder(token) or _placeholder(chat):
        return False
    try:
        import requests
        r = requests.post(f"https://api.telegram.org/bot{token.strip()}/sendMessage",
                          json={"chat_id": chat.strip(), "text": text, "parse_mode": "HTML"}, timeout=15)
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        logger.warning("telegram: %s", e)
        return False


def _coerce(col, value):
    t = col["t"]
    if t == "bool":
        return value in (True, "true", "True", "1", 1, "Có", "co", "CO")
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if t == "number":
        return float(value)
    return value


# ================================================== CRUD tổng quát theo config
def res_list(res_id):
    import psycopg2.extras
    cfg = RESOURCES[res_id]
    keys = [c["k"] for c in cfg["cols"]]
    conn = connect_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"SELECT id, {', '.join(keys)} FROM {cfg['table']} ORDER BY {cfg['order']}")
        rows = [dict(r) for r in cur.fetchall()]
        today = date.today()
        for r in rows:
            for c in cfg["cols"]:
                v = r.get(c["k"])
                if isinstance(v, (date, datetime)):
                    r[c["k"]] = v.isoformat()[:10]
                elif isinstance(v, Decimal):
                    r[c["k"]] = float(v)
            # cột phụ: số ngày tới hạn (theo cột 'countdown' của resource)
            cd = cfg.get("countdown")
            if cd and r.get(cd):
                try:
                    d = datetime.strptime(str(r[cd])[:10], "%Y-%m-%d").date()
                    r["_days"] = (d - today).days
                except Exception:  # noqa: BLE001
                    r["_days"] = None
        return rows
    finally:
        conn.close()


def res_save(res_id, body):
    cfg = RESOURCES[res_id]
    cols = cfg["cols"]
    present = [c for c in cols if c["k"] in body]
    conn = connect_db()
    try:
        cur = conn.cursor()
        rec_id = body.get("id")
        if rec_id:  # UPDATE
            sets = ", ".join(f"{c['k']}=%s" for c in present)
            vals = [_coerce(c, body.get(c["k"])) for c in present] + [int(rec_id)]
            cur.execute(f"UPDATE {cfg['table']} SET {sets} WHERE id=%s", vals)
        else:  # INSERT
            names = ", ".join(c["k"] for c in present)
            ph = ", ".join(["%s"] * len(present))
            vals = [_coerce(c, body.get(c["k"])) for c in present]
            cur.execute(f"INSERT INTO {cfg['table']} ({names}) VALUES ({ph})", vals)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def res_delete(res_id, rec_id):
    cfg = RESOURCES[res_id]
    conn = connect_db()
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {cfg['table']} WHERE id=%s", (int(rec_id),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ============================================= nghiệp vụ riêng từng module (run)
def run_payment():
    import psycopg2.extras
    conn = connect_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id,tenant_name,room_code,amount,due_date FROM payment_monitor_logs "
                    "WHERE status='PENDING' AND due_date IS NOT NULL AND due_date<CURRENT_DATE ORDER BY due_date")
        overdue = cur.fetchall()
        if overdue:
            cur.execute("UPDATE payment_monitor_logs SET status='OVERDUE' WHERE id=ANY(%s)",
                        ([r["id"] for r in overdue],))
        conn.commit()
        if overdue:
            lines = ["🔴 <b>NỢ QUÁ HẠN</b>", f"{len(overdue)} hóa đơn vừa quá hạn:"]
            for r in overdue[:20]:
                lines.append(f"• {r['room_code']} — {r['tenant_name']} — {float(r['amount'] or 0):,.0f}đ (hạn {r['due_date']})")
            send_telegram("\n".join(lines))
        return f"{len(overdue)} hóa đơn chuyển OVERDUE."
    finally:
        conn.close()


def run_contract():
    """Tính lại số ngày còn hạn + đặt loại cảnh báo theo mốc 30/14/7; báo HĐ sắp hết hạn."""
    import psycopg2.extras
    conn = connect_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id,contract_id,tenant_name,room_code,expiry_date FROM contract_alerts "
                    "WHERE expiry_date IS NOT NULL")
        rows = cur.fetchall()
        today = date.today()
        soon = []
        for r in rows:
            days = (r["expiry_date"] - today).days
            atype = "TERMINATION" if days < 0 else ("WARNING" if days <= 14 else ("RENEWAL" if days <= 30 else "RENEWAL"))
            cur.execute("UPDATE contract_alerts SET days_until_expiry=%s, alert_type=%s WHERE id=%s",
                        (days, atype, r["id"]))
            if 0 <= days <= 30:
                soon.append((r, days))
        conn.commit()
        if soon:
            lines = ["📄 <b>HỢP ĐỒNG SẮP HẾT HẠN</b>"]
            for r, d in sorted(soon, key=lambda x: x[1]):
                lines.append(f"• {r['room_code']} — {r['tenant_name']} — còn {d} ngày (hết hạn {r['expiry_date']})")
            send_telegram("\n".join(lines))
        return f"Cập nhật {len(rows)} HĐ · {len(soon)} sắp hết hạn (≤30 ngày)."
    finally:
        conn.close()


def run_tenant():
    """Tính điểm tổng = TB(payment, behavior, 100-risk) và đề xuất theo ngưỡng."""
    conn = connect_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, COALESCE(payment_score,0), COALESCE(behavior_score,0), "
                    "COALESCE(risk_score,0) FROM tenant_scores")
        rows = cur.fetchall()
        for rid, pay, beh, risk in rows:
            overall = round((float(pay) + float(beh) + (100 - float(risk))) / 3, 1)
            rec = "GIU" if overall >= 80 else ("THEO_DOI" if overall >= 60 else "CANH_BAO")
            cur.execute("UPDATE tenant_scores SET overall_score=%s, recommendation=%s WHERE id=%s",
                        (overall, rec, rid))
        conn.commit()
        return f"Chấm điểm {len(rows)} khách."
    finally:
        conn.close()


def run_maintenance():
    """Cảnh báo thiết bị mức HIGH/CRITICAL dự kiến hỏng trong 30 ngày."""
    import psycopg2.extras
    conn = connect_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT room_code, asset_type, severity, predicted_date, recommended_action "
                    "FROM maintenance_predictions WHERE severity IN ('HIGH','CRITICAL')")
        rows = cur.fetchall()
        today = date.today()
        soon = [r for r in rows if r["predicted_date"] and 0 <= (r["predicted_date"] - today).days <= 30]
        if soon:
            lines = ["🔧 <b>CẢNH BÁO BẢO TRÌ</b>"]
            for r in soon:
                lines.append(f"• {r['room_code']} — {r['asset_type']} [{r['severity']}] dự kiến {r['predicted_date']}"
                             + (f" → {r['recommended_action']}" if r['recommended_action'] else ""))
            send_telegram("\n".join(lines))
        return f"{len(rows)} thiết bị nguy cơ cao · {len(soon)} cần xử lý trong 30 ngày."
    finally:
        conn.close()


def run_action(res_id):
    if res_id == "payment":
        return run_payment()
    if res_id == "contract":
        return run_contract()
    if res_id == "tenant":
        return run_tenant()
    if res_id == "maintenance":
        return run_maintenance()
    return "Module này chưa có nghiệp vụ đối soát."


def run_all_scheduled():
    global _last_report
    ran = datetime.now(timezone.utc).isoformat()
    if not get_database_url():
        _last_report = {"status": "config_error", "ran_at": ran, "detail": "Thiếu DATABASE_URL."}
        return _last_report
    try:
        p = run_payment()
        c = run_contract()
        _last_report = {"status": "ok", "ran_at": ran, "detail": f"Payment: {p} | Contract: {c}"}
    except Exception as e:  # noqa: BLE001
        _last_report = {"status": "db_error", "ran_at": ran, "detail": f"Lỗi: {e}"}
        logger.error(_last_report["detail"])
    return _last_report


# =================================================================== HTTP
def _client_config():
    """Config rút gọn gửi cho trình duyệt để dựng bảng/form động."""
    return {rid: {"label": c["label"], "badge": c["badge"], "cols": c["cols"],
                  "actions": c["actions"], "countdown": c.get("countdown")}
            for rid, c in RESOURCES.items()}


DASHBOARD_HTML = r"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quản trị AI Modules — An Cư Hà Nội</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--line:#334155;--txt:#e2e8f0;--muted:#94a3b8}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt)}
.wrap{max-width:1040px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:6px 0}.sub{color:var(--muted);font-size:13px;margin-bottom:14px}
.tabs{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.tab{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 14px;cursor:pointer;font-weight:600;font-size:14px}
.tab.on{background:#2563eb;border-color:#2563eb;color:#fff}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
button{cursor:pointer;border:0;border-radius:8px;padding:8px 12px;font-size:13px;font-weight:600}
.b1{background:#2563eb;color:#fff}.b2{background:#334155;color:#e2e8f0}.paid{background:#059669;color:#fff}.del{background:#7f1d1d;color:#fecaca}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:12px;overflow:hidden}
th,td{padding:9px 11px;text-align:left;font-size:13px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase}
.badge{padding:2px 8px;border-radius:99px;font-size:11px;font-weight:700;background:#334155;color:#cbd5e1}
.OVERDUE,.TERMINATION,.ABANDONED,.CANH_BAO,.HIGH,.CRITICAL{background:#7f1d1d;color:#fecaca}
.PENDING,.WARNING,.ACTIVE,.THEO_DOI,.MEDIUM{background:#78350f;color:#fde68a}
.PAID,.CONVERTED,.RENEWAL,.GIU,.LOW{background:#064e3b;color:#a7f3d0}
.money{font-variant-numeric:tabular-nums;font-weight:600}
.tblwrap{overflow-x:auto}.muted{color:var(--muted)}
dialog{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:12px;padding:18px;width:min(94vw,420px)}
dialog input,dialog select{width:100%;padding:8px;margin:4px 0 10px;background:var(--bg);border:1px solid var(--line);border-radius:8px;color:var(--txt)}
label{font-size:12px;color:var(--muted)}.err{color:#f87171;font-size:13px;margin-top:8px}
</style></head><body><div class="wrap">
<h1>🗂️ Quản trị AI Modules — An Cư Hà Nội</h1>
<div class="sub">Dữ liệu trực tiếp từ Supabase · cập nhật <span id="t">—</span></div>
<div class="tabs" id="tabs"></div>
<div class="bar">
  <button class="b1" onclick="openForm()">＋ Thêm mới</button>
  <button class="b2" id="runbtn" onclick="runNow()" style="display:none">🔄 Chạy đối soát</button>
  <button class="b2" onclick="load()">↻ Tải lại</button>
  <span class="muted" id="count"></span>
</div>
<div class="tblwrap"><table><thead id="thead"></thead><tbody id="rows"></tbody></table></div>
<div class="err" id="err"></div>
</div>
<dialog id="dlg"><h3 style="margin-top:0" id="dlg-title">Thêm</h3><div id="form"></div>
<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:6px">
<button class="b2" onclick="dlg.close()">Hủy</button><button class="b1" onclick="submitForm()">Lưu</button></div>
</dialog>
<script>
const $=id=>document.getElementById(id);
const vnd=n=>n==null?'—':Number(n).toLocaleString('vi-VN')+'đ';
let CFG={},CUR='payment',DATA=[],EDIT=null;
async function api(p,m,b){const r=await fetch(p,{method:m||'GET',headers:b?{'Content-Type':'application/json'}:{},body:b?JSON.stringify(b):undefined});if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}
async function init(){CFG=await api('/api/config');const tb=$('tabs');tb.innerHTML='';
  Object.keys(CFG).forEach(k=>{const d=document.createElement('div');d.className='tab'+(k===CUR?' on':'');d.textContent=CFG[k].label;d.onclick=()=>{CUR=k;init2();};tb.appendChild(d);});
  init2();}
function init2(){document.querySelectorAll('.tab').forEach((el,i)=>el.classList.toggle('on',Object.keys(CFG)[i]===CUR));
  $('runbtn').style.display=CFG[CUR].actions.includes('run')?'':'none';
  const th=CFG[CUR].cols.map(c=>`<th>${c.l}</th>`).join('')+(CFG[CUR].countdown?'<th>Ngày</th>':'')+'<th></th>';
  $('thead').innerHTML='<tr>'+th+'</tr>';load();}
async function load(){try{const d=await api('/api/'+CUR+'/list');DATA=d;const b=$('badge'in CFG?'':'');
  const rows=$('rows');rows.innerHTML='';
  d.forEach(p=>{
    let tds=CFG[CUR].cols.map(c=>{let v=p[c.k];
      if(c.k===CFG[CUR].badge)return`<td><span class="badge ${v||''}">${v==null?'':v}</span></td>`;
      if(c.t==='bool')return`<td>${v?'✅':'—'}</td>`;
      if(c.money)return`<td class="money">${vnd(v)}</td>`;
      return`<td>${v==null?'':v}</td>`;}).join('');
    if(CFG[CUR].countdown){let dd=p._days==null?'—':(p._days<0?('quá '+(-p._days)+'n'):('còn '+p._days+'n'));tds+=`<td>${dd}</td>`;}
    const pay=(CUR==='payment'&&p.status!=='PAID')?`<button class="paid" onclick="markPaid(${p.id})">Đã thu</button> `:'';
    tds+=`<td style="white-space:nowrap">${pay}<button class="b2" onclick="edit(${p.id})">Sửa</button> <button class="del" onclick="del(${p.id})">Xóa</button></td>`;
    rows.insertAdjacentHTML('beforeend','<tr>'+tds+'</tr>');
  });
  $('count').textContent=d.length+' dòng';$('t').textContent=new Date().toLocaleString('vi-VN');$('err').textContent='';
}catch(e){$('err').textContent='Lỗi tải: '+e.message;}}
function buildForm(p){const f=$('form');f.innerHTML=CFG[CUR].cols.map(c=>{
  const val=p?(p[c.k]==null?'':p[c.k]):'';
  if(c.t==='bool'){const yes=(p&&(p[c.k]===true||p[c.k]==='true'));return`<label>${c.l}</label><select id="f_${c.k}"><option value="Không" ${!yes?'selected':''}>Không</option><option value="Có" ${yes?'selected':''}>Có</option></select>`;}
  if(c.t==='select'){const o=c.opts.map(x=>`<option ${x===val?'selected':''}>${x}</option>`).join('');return`<label>${c.l}</label><select id="f_${c.k}">${o}</select>`;}
  const type=c.t==='number'?'number':(c.t==='date'?'date':'text');
  return`<label>${c.l}</label><input id="f_${c.k}" type="${type}" value="${String(val).replace(/"/g,'&quot;')}">`;
}).join('');}
function openForm(){EDIT=null;$('dlg-title').textContent='Thêm mới';buildForm(null);$('dlg').showModal();}
function edit(id){const p=DATA.find(x=>x.id===id);if(!p)return;EDIT=id;$('dlg-title').textContent='Sửa (#'+id+')';buildForm(p);$('dlg').showModal();}
async function submitForm(){const body={};CFG[CUR].cols.forEach(c=>{body[c.k]=$('f_'+c.k).value;});
  if(EDIT)body.id=EDIT;try{await api('/api/'+CUR+'/save','POST',body);$('dlg').close();load();}catch(e){alert('Lỗi: '+e.message);}}
async function del(id){const p=DATA.find(x=>x.id===id);if(!confirm('XÓA dòng #'+id+' ?\\nKhông khôi phục được.'))return;
  try{await api('/api/'+CUR+'/delete','POST',{id});load();}catch(e){alert('Lỗi: '+e.message);}}
async function markPaid(id){if(!confirm('Đánh dấu ĐÃ THU?'))return;try{await api('/api/payment/save','POST',{id,status:'PAID'});load();}catch(e){alert('Lỗi: '+e.message);}}
async function runNow(){try{const r=await api('/api/'+CUR+'/run','POST',{});alert('Xong: '+(r.detail||''));load();}catch(e){alert('Lỗi: '+e.message);}}
init();setInterval(load,60000);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "AiAdmin/2.0"

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
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                return given == pw
            except Exception:  # noqa: BLE001
                return False
        return False

    def _need_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Quan tri AI"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Can dang nhap.".encode("utf-8"))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", "0") or "0")
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n > 0 else {}
        except Exception:  # noqa: BLE001
            return {}

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/status":
            self._send(200, json.dumps({"app": APP_ID, **_last_report}, ensure_ascii=False, indent=2)); return
        if not self._authed():
            self._need_auth(); return
        if path in ("/", "/index.html"):
            self._send(200, DASHBOARD_HTML, "text/html; charset=utf-8"); return
        if path == "/api/config":
            self._send(200, json.dumps(_client_config(), ensure_ascii=False)); return
        m = re.match(r"^/api/(\w+)/list$", path)
        if m and m.group(1) in RESOURCES:
            try:
                self._send(200, json.dumps(res_list(m.group(1)), ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if not self._authed():
            self._need_auth(); return
        body = self._body()
        m = re.match(r"^/api/(\w+)/(save|delete|run)$", path)
        if not m or m.group(1) not in RESOURCES:
            self._send(404, json.dumps({"error": "not found"})); return
        res_id, act = m.group(1), m.group(2)
        try:
            if act == "save":
                n = res_save(res_id, body); self._send(200, json.dumps({"ok": True, "n": n}))
            elif act == "delete":
                n = res_delete(res_id, body.get("id")); self._send(200, json.dumps({"ok": True, "n": n}))
            elif act == "run":
                detail = run_action(res_id); self._send(200, json.dumps({"ok": True, "detail": detail}, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    def log_message(self, *a):
        return


def _scheduler(interval_hours):
    import time
    while True:
        time.sleep(max(1, interval_hours) * 3600)
        with _run_lock:
            run_all_scheduled()


def main():
    logger.info("Khởi động %s", APP_ID)
    with _run_lock:
        run_all_scheduled()
    port = os.getenv("PORT", "").strip()
    if not port:
        logger.info("Không có PORT — chạy một lần, kết thúc."); return
    interval = int(os.getenv("CHECK_INTERVAL_HOURS", "6") or "6")
    threading.Thread(target=_scheduler, args=(interval,), daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", int(port)), _Handler)
    logger.info("Dashboard chạy trên cổng %s (đối soát mỗi %sh).", port, interval)
    srv.serve_forever()


if __name__ == "__main__":
    main()
