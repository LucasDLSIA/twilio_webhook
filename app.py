import os
import io
import re
import time
import sqlite3
import json
from datetime import datetime
from typing import Optional, Dict, List

import pandas as pd
from flask import Flask, request, redirect, Response, jsonify

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from twilio.rest import Client

# =========================
# Config
# =========================
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
EMPRESAS_FILE_ID = os.environ.get("EMPRESAS_FILE_ID", "").strip()

# Google Service Account
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_SA_FILE = (
    os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    or ("/etc/secrets/Service_account.json" if os.path.exists("/etc/secrets/Service_account.json") else "")
)

# Twilio
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "").strip()
TWILIO_MESSAGING_SERVICE_SID = os.environ.get("TWILIO_MESSAGING_SERVICE_SID", "").strip()

# Plantillas (Content Templates)
TWILIO_TEMPLATE_SID = os.environ.get("TWILIO_TEMPLATE_SID", "").strip()            # template con botón VIEW_NOW
TWILIO_SIGN_TEMPLATE_SID = os.environ.get("TWILIO_SIGN_TEMPLATE_SID", "").strip()  # (opcional) template con botones SIGN_OK / SIGN_OBS



# Cache
_EMP_CACHE = {"ts": 0.0, "rows": []}
_ENV_CACHE: Dict[str, Dict] = {}  # tenant_slug -> {"ts":..., "rows":[...] }
CACHE_TTL = int(os.environ.get("CACHE_TTL", "120"))

# =========================
# Flask
# =========================
app = Flask(__name__)

# =========================
# Helpers
# =========================
def esc(s: str) -> str:
    return (
        (str(s or ""))
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )

def slugify(name: str) -> str:
    name = (name or "").strip().lower()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name or "empresa"

def norm_digits(s: str) -> str:
    return re.sub(r"\D", "", str(s or ""))

def norm_cuil(s: str) -> str:
    return norm_digits(s)

def strip_pdf(name: str) -> str:
    s = str(name or "").strip()
    if s.lower().endswith(".pdf"):
        s = s[:-4]
    return s.strip()

def norm_whatsapp(s: str) -> str:
    d = norm_digits(s)
    if not d:
        return ""
    if d.startswith("54"):
        return "whatsapp:+" + d
    return "whatsapp:+54" + d

def parse_period_folder(name: str) -> Optional[str]:
    """
    Acepta: '01-2026', '01/2026'
    Devuelve siempre 'mm/aaaa' o None si no reconoce.
    """
    n = (name or "").strip()
    m = re.search(r"(\d{2})[-/](\d{4})", n)
    if m:
        mm, yyyy = m.group(1), m.group(2)
        if 1 <= int(mm) <= 12:
            return f"{mm}/{yyyy}"
    return None

def period_to_folder_name(period: str) -> str:
    """
    Recibe 'mm/aaaa' o 'mm-aaaa' y devuelve 'mm-aaaa' (nombre de carpeta en Drive).
    """
    p = (period or "").strip().replace("-", "/")
    mm, yyyy = p.split("/")
    return f"{mm}-{yyyy}"

# =========================
# Auth admin (token por query/header)
# =========================
def admin_ok() -> bool:
    if not ADMIN_TOKEN:
        return True
    tok = request.args.get("token", "") or request.headers.get("X-Admin-Token", "")
    return tok.strip() == ADMIN_TOKEN

def require_admin():
    if not admin_ok():
        return Response("Unauthorized (admin token requerido)", status=401)

# =========================
# Google Drive
# =========================
def drive_service():
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    if GOOGLE_SA_JSON:
        info = json.loads(GOOGLE_SA_JSON) if isinstance(GOOGLE_SA_JSON, str) else GOOGLE_SA_JSON
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    elif GOOGLE_SA_FILE:
        creds = service_account.Credentials.from_service_account_file(GOOGLE_SA_FILE, scopes=scopes)
    else:
        raise RuntimeError("Falta GOOGLE_SERVICE_ACCOUNT_JSON o GOOGLE_SERVICE_ACCOUNT_FILE/GOOGLE_APPLICATION_CREDENTIALS en ENV")
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def download_excel_df(file_id: str) -> pd.DataFrame:
    service = drive_service()
    req = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)
    df = pd.read_excel(fh)
    df.columns = [str(c).strip() for c in df.columns]
    return df

# =========================
# Tenants (Empresas.xlsx)
# =========================
def load_tenants(force: bool = False) -> List[dict]:
    if not EMPRESAS_FILE_ID:
        return []

    now = time.time()
    if (not force) and _EMP_CACHE["rows"] and (now - _EMP_CACHE["ts"] < CACHE_TTL):
        return _EMP_CACHE["rows"]

    df = download_excel_df(EMPRESAS_FILE_ID)

    # Formato esperado:
    # Empresa | Envios_File_ID | Drive_Root_ID
    cols_lower = {c.lower(): c for c in df.columns}

    def get_col(*names):
        for n in names:
            if n.lower() in cols_lower:
                return cols_lower[n.lower()]
        return None

    c_empresa = get_col("Empresa", "empresa", "display_name", "nombre")
    c_envios = get_col("Envios_File_ID", "envios_file_id", "envios")
    c_root = get_col("Drive_Root_ID", "drive_root_id", "recibos_root_id", "root_id")

    if not (c_empresa and c_envios and c_root):
        return []

    tenants: List[dict] = []
    for _, r in df.iterrows():
        empresa = str(r.get(c_empresa, "")).strip()
        env_id = str(r.get(c_envios, "")).strip()
        root_id = str(r.get(c_root, "")).strip()
        if not empresa or not env_id or not root_id:
            continue
        tenants.append(
            {
                "slug": slugify(empresa),
                "display_name": empresa,
                "envios_file_id": env_id,
                "drive_root_id": root_id,
            }
        )

    # dedupe por slug
    seen = set()
    out = []
    for t in tenants:
        if t["slug"] in seen:
            continue
        seen.add(t["slug"])
        out.append(t)

    _EMP_CACHE["rows"] = out
    _EMP_CACHE["ts"] = now
    return out

def get_tenant(tenant_slug: str) -> Optional[dict]:
    tenant_slug = (tenant_slug or "").strip().lower()
    for t in load_tenants():
        if t["slug"] == tenant_slug:
            return t
    return None

# =========================
# Envios por tenant
# =========================
def load_envios_rows(tenant_slug: str, force: bool = False) -> List[dict]:
    t = get_tenant(tenant_slug)
    if not t:
        return []
    now = time.time()
    cached = _ENV_CACHE.get(tenant_slug)
    if (not force) and cached and (now - cached["ts"] < CACHE_TTL):
        return cached["rows"]

    df = download_excel_df(t["envios_file_id"])
    df.columns = [str(c).strip() for c in df.columns]
    rows = df.fillna("").to_dict(orient="records")

    _ENV_CACHE[tenant_slug] = {"ts": now, "rows": rows}
    return rows

def find_person_by_cuil(envios_rows: List[dict], cuil: str) -> Optional[dict]:
    """
    Tu formato real por empresa:
      nombre | telefono | archivo | DNI
    donde archivo = '20-xxxxxxxx-x.pdf'
    """
    target = norm_cuil(cuil)
    if not target:
        return None

    for r in envios_rows:
        archivo = strip_pdf(r.get("archivo") or r.get("Archivo") or "")
        if norm_cuil(archivo) == target:
            nombre = str(r.get("nombre") or r.get("Nombre") or "").strip()
            tel = str(r.get("telefono") or r.get("teléfono") or r.get("Telefono") or "").strip()
            dni = str(r.get("dni") or r.get("DNI") or "").strip()
            return {
                "cuil": archivo,
                "nombre": nombre,
                "telefono_raw": tel,
                "to_whatsapp": norm_whatsapp(tel),
                "dni": dni,
            }
    return None

# =========================
# Drive: PDF
# =========================
def find_pdf_file_id_for_cuil_period(tenant_slug: str, cuil: str, period: str) -> Optional[str]:
    t = get_tenant(tenant_slug)
    if not t:
        return None

    service = drive_service()
    root_id = t["drive_root_id"]
    folder_name = period_to_folder_name(period)
    filename = f"{cuil}.pdf"

    # Carpeta del período
    folder_res = service.files().list(
        q=(
            f"'{root_id}' in parents and "
            f"mimeType='application/vnd.google-apps.folder' and "
            f"name='{folder_name}' and trashed=false"
        ),
        fields="files(id,name)",
        pageSize=5,
    ).execute().get("files", [])

    if not folder_res:
        return None

    period_folder_id = folder_res[0]["id"]

    # PDF dentro de carpeta
    file_res = service.files().list(
        q=(
            f"'{period_folder_id}' in parents and "
            f"name='{filename}' and mimeType='application/pdf' and trashed=false"
        ),
        fields="files(id,name)",
        pageSize=1,
    ).execute().get("files", [])

    if not file_res:
        return None

    return file_res[0]["id"]

def list_periods_for_cuil(tenant_slug: str, cuil: str) -> List[str]:
    t = get_tenant(tenant_slug)
    if not t:
        return []

    root_id = t["drive_root_id"]
    filename = f"{cuil}.pdf"
    service = drive_service()

    folders = service.files().list(
        q=f"'{root_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id,name)",
        pageSize=1000,
    ).execute().get("files", [])

    periods = []
    for f in folders:
        label = parse_period_folder(f.get("name", ""))
        if not label:
            continue
        res = service.files().list(
            q=f"'{f['id']}' in parents and name='{filename}' and mimeType='application/pdf' and trashed=false",
            fields="files(id)",
            pageSize=1,
        ).execute().get("files", [])
        if res:
            periods.append(label)

    def key(p: str):
        mm, yyyy = p.split("/")
        return int(yyyy) * 100 + int(mm)

    return sorted(set(periods), key=key, reverse=True)

# =========================
# Media endpoint (Twilio descarga PDF desde acá)
# =========================
@app.get("/media/pdf")
def media_pdf():
    token = request.args.get("token", "").strip()
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        return Response("Unauthorized", status=401)

    tenant = (request.args.get("tenant") or "").strip().lower()
    cuil = (request.args.get("cuil") or "").strip()
    period = (request.args.get("period") or "").strip()

    if not (tenant and cuil and period):
        return Response("Faltan parámetros tenant/cuil/period", status=400)

    file_id = find_pdf_file_id_for_cuil_period(tenant, cuil, period)
    if not file_id:
        return Response("PDF no encontrado", status=404)

    service = drive_service()
    req = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)

    data = fh.read()
    resp = Response(data, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'inline; filename="{cuil}.pdf"'
    return resp

# =========================
# Twilio senders
# =========================
def _twilio_client() -> Client:
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        raise RuntimeError("Faltan TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN en ENV")
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

STATUS_CALLBACK_URL = os.environ.get("STATUS_CALLBACK_URL", "").strip()

def send_whatsapp_pdf(to_whatsapp: str, media_url: str, body: str, status_callback: str | None = None) -> str:
    if not (TWILIO_WHATSAPP_FROM or TWILIO_MESSAGING_SERVICE_SID):
        raise RuntimeError("Falta TWILIO_WHATSAPP_FROM o TWILIO_MESSAGING_SERVICE_SID en ENV")

    client = _twilio_client()

    payload = {
        "to": to_whatsapp,
        "body": body or " ",
        "media_url": [media_url],
    }

    if status_callback:
        payload["status_callback"] = status_callback

    if TWILIO_MESSAGING_SERVICE_SID:
        payload["messaging_service_sid"] = TWILIO_MESSAGING_SERVICE_SID
    else:
        payload["from_"] = TWILIO_WHATSAPP_FROM

    msg = client.messages.create(**payload)
    return msg.sid

def save_pdf_sid(tenant: str, cuil: str, period: str, to_whatsapp: str, sid: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO sent_pdfs
        (tenant, cuil, period, to_whatsapp, message_sid, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (tenant, cuil, period, to_whatsapp, sid, int(time.time())))
    conn.commit()
    conn.close()


def send_whatsapp_template(to_whatsapp: str, content_vars: Optional[dict] = None, template_sid: Optional[str] = None) -> str:
    """
    Envío de plantilla aprobada (WhatsApp) usando Content Templates.
    template_sid:
      - por defecto usa TWILIO_TEMPLATE_SID (la de VIEW_NOW)
      - podés pasar TWILIO_SIGN_TEMPLATE_SID para la de firma/observa
    """
    tpl = (template_sid or TWILIO_TEMPLATE_SID).strip()
    if not tpl:
        raise RuntimeError("Falta TWILIO_TEMPLATE_SID (ContentSid) en ENV")
    if not (TWILIO_WHATSAPP_FROM or TWILIO_MESSAGING_SERVICE_SID):
        raise RuntimeError("Falta TWILIO_WHATSAPP_FROM o TWILIO_MESSAGING_SERVICE_SID en ENV")

    client = _twilio_client()
    payload = {
        "to": to_whatsapp,
        "content_sid": tpl,
    }
    if TWILIO_MESSAGING_SERVICE_SID:
        payload["messaging_service_sid"] = TWILIO_MESSAGING_SERVICE_SID
    else:
        payload["from_"] = TWILIO_WHATSAPP_FROM

    if content_vars:
        payload["content_variables"] = json.dumps(content_vars)

    msg = client.messages.create(**payload)
    return msg.sid

@app.post("/twilio/status")
def twilio_status():
    sid = (request.form.get("MessageSid") or "").strip()
    status = (request.form.get("MessageStatus") or "").strip()

    print("STATUS:", sid, status)

    if status != "delivered":
        return "OK"

    info = get_pdf_by_sid(sid)
    if not info:
        return "OK"

    # si ya mandamos firma para ese PDF, no repetir
    if info.get("sign_sent_at"):
        return "OK"

    if not TWILIO_SIGN_TEMPLATE_SID:
        return "OK"

    try:
        send_whatsapp_template(
            info["to_whatsapp"],
            template_sid=TWILIO_SIGN_TEMPLATE_SID,
            content_vars={"1": info["period"]},
        )
        print("SIGN TEMPLATE SENT AFTER PDF DELIVERED")

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE sent_pdfs SET sign_sent_at=? WHERE message_sid=?;", (int(time.time()), sid))
        conn.commit()
        conn.close()

    except Exception as e:
        print("WARN sending sign template in status callback:", e)

    return "OK"


# =========================
# DB: pending view + estado firma
# =========================
DB_PATH = os.environ.get("DB_PATH", "/data/app.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
      CREATE TABLE IF NOT EXISTS pending_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        to_whatsapp TEXT NOT NULL,
        tenant TEXT NOT NULL,
        cuil TEXT NOT NULL,
        period TEXT NOT NULL,
        created_at INTEGER NOT NULL
      );
    """)

    cur.execute("""
      CREATE TABLE IF NOT EXISTS recibo_estado (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant TEXT NOT NULL,
        cuil TEXT NOT NULL,
        period TEXT NOT NULL,
        estado TEXT NOT NULL,         -- DISPONIBLE | FIRMADO | OBSERVADO | NO_NEED
        updated_at INTEGER NOT NULL,
        UNIQUE(tenant, cuil, period)
      );
    """)

    cur.execute("""
      CREATE TABLE IF NOT EXISTS sent_pdfs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant TEXT NOT NULL,
        cuil TEXT NOT NULL,
        period TEXT NOT NULL,
        to_whatsapp TEXT NOT NULL,
        message_sid TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL,
        sign_sent_at INTEGER
      );
    """)

    # Si la tabla existía sin sign_sent_at, la agregamos
    try:
        cur.execute("ALTER TABLE sent_pdfs ADD COLUMN sign_sent_at INTEGER;")
    except Exception:
        pass

    # índices útiles
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_to_created ON pending_views(to_whatsapp, created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_estado_key ON recibo_estado(tenant, cuil, period);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sentpdfs_sid ON sent_pdfs(message_sid);")
    except Exception:
        pass

    conn.commit()
    conn.close()


init_db()

def save_pdf_sid(tenant: str, cuil: str, period: str, to_whatsapp: str, sid: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO sent_pdfs
        (tenant, cuil, period, to_whatsapp, message_sid, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (tenant, cuil, period, to_whatsapp, sid, int(time.time())))
    conn.commit()
    conn.close()



def get_pdf_by_sid(sid):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM sent_pdfs WHERE message_sid = ?", (sid,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


from functools import wraps
from flask import request, Response

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

def _get_admin_token_from_request() -> str:
    return (request.args.get("token") or request.form.get("token") or "").strip()

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        tok = _get_admin_token_from_request()
        if not ADMIN_TOKEN or tok != ADMIN_TOKEN:
            return Response("Unauthorized", status=401)
        return fn(*args, **kwargs)
    return wrapper


import sqlite3, time

def add_pending_view(to_whatsapp: str, tenant: str, cuil: str, period: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO pending_views (to_whatsapp, tenant, cuil, period, created_at)
      VALUES (?, ?, ?, ?, ?);
    """, (to_whatsapp, tenant, cuil, period, int(time.time())))
    conn.commit()
    conn.close()

def get_latest_pending_view(from_whatsapp: str):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
      SELECT id, to_whatsapp, tenant, cuil, period, created_at
      FROM pending_views
      WHERE to_whatsapp = ?
      ORDER BY created_at DESC
      LIMIT 1;
    """, (from_whatsapp,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def consume_pending_view(pending_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM pending_views WHERE id=?;", (pending_id,))
    conn.commit()
    conn.close()

def set_recibo_estado(tenant: str, cuil: str, period: str, estado: str):
    conn = get_db_connection()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute("""
      INSERT INTO recibo_estado (tenant, cuil, period, estado, updated_at)
      VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(tenant, cuil, period) DO UPDATE SET
        estado=excluded.estado,
        updated_at=excluded.updated_at;
    """, (tenant, cuil, period, estado, now))
    conn.commit()
    conn.close()

def get_recibo_estado(tenant: str, cuil: str, period: str):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
      SELECT estado FROM recibo_estado
      WHERE tenant=? AND cuil=? AND period=?
      LIMIT 1;
    """, (tenant, cuil, period))
    row = cur.fetchone()
    conn.close()
    return row["estado"] if row else None

import requests

def pdf_exists_for_tenant_period_cuil(tenant, cuil, period):
    url = f"{os.environ.get('PUBLIC_BASE_URL','').rstrip('/')}/media/pdf"
    if not url.startswith("http"):
        # fallback: no podemos verificar
        return True
    r = requests.get(url, params={
        "tenant": tenant,
        "cuil": cuil,
        "period": period,
        "token": ADMIN_TOKEN
    }, timeout=12)
    return r.status_code == 200


# =========================
# Routes
# =========================
@app.get("/")
def root():
    tok = request.args.get("token", "")
    if tok:
        return redirect(f"/admin?token={tok}")
    return redirect("/admin")

@app.get("/admin")
def admin_home():
    auth = require_admin()
    if auth:
        return auth

    token = request.args.get("token", "")
    tenants = load_tenants(force=True)

    html = []
    html.append("<h2>Panel Admin</h2>")
    if not EMPRESAS_FILE_ID:
        html.append("<p style='color:red'>Falta EMPRESAS_FILE_ID en ENV.</p>")
    if not tenants:
        html.append("<p>No hay empresas detectadas en el Excel maestro.</p>")
        html.append("<p>Encabezados esperados: Empresa | Envios_File_ID | Drive_Root_ID</p>")
    else:
        html.append("<p>Elegí la empresa:</p><ul>")
        for t in tenants:
            panel_url = f"/admin/panel?tenant={esc(t['slug'])}&token={esc(token)}"
            test_url = f"/admin/send_test?tenant={esc(t['slug'])}&token={esc(token)}"
            html.append(
                f"<li>"
                f"<a href='{panel_url}'><b>{esc(t['display_name'])}</b></a>"
                f" &nbsp;|&nbsp; "
                f"<a href='{test_url}'>🧪 Prueba</a>"
                f"</li>"
            )
        html.append("</ul>")
    return Response("".join(html), mimetype="text/html")

@app.get("/admin/panel")
def admin_panel():
    auth = require_admin()
    if auth:
        return auth

    token = (request.args.get("token") or "").strip()
    tenant = (request.args.get("tenant") or "").strip().lower()

    if not tenant:
        return Response("Falta tenant. Volvé a /admin.", status=400)

    t = get_tenant(tenant)
    if not t:
        return Response("Tenant inválido. Volvé a /admin.", status=400)

    # Lee envíos (cacheado si tu load_envios_rows cachea)
    envios_rows = load_envios_rows(tenant, force=False) or []

    html = []
    html.append("<!doctype html><html><head><meta charset='utf-8'><title>Panel empresa</title></head><body>")
    html.append("<h2>Panel empresa</h2>")
    html.append(f"<p><b>Empresa:</b> {esc(t.get('display_name',''))} &nbsp; (<code>{esc(t.get('slug',''))}</code>)</p>")
    html.append(f"<p><a href='/admin?token={esc(token)}'>← volver</a></p>")

    # Botón prueba
    html.append(f"<p><a href='/admin/send_test?tenant={esc(tenant)}&token={esc(token)}'>🧪 Envío de prueba (1 persona)</a></p>")

    # ---------- Envío masivo ----------
    html.append("<hr>")
    html.append("<h3>📩 Envío masivo (empresa completa)</h3>")
    html.append("<form method='post' action='/admin/send_template_queue_start'>")
    html.append(f"<input type='hidden' name='token' value='{esc(token)}'>")
    html.append(f"<input type='hidden' name='tenant' value='{esc(tenant)}'>")
    html.append("<label>Período (mm/aaaa): <input type='text' name='period' placeholder='01/2026' required></label><br><br>")
    html.append("<label>Límite (0 = todos): <input type='number' name='limit' min='0' value='0'></label><br><br>")
    # si querés que sea opción, cambiá a checkbox. Por ahora lo dejo fijo en true como venías usando.
    html.append("<input type='hidden' name='require_pdf' value='true'>")
    html.append("<button type='submit'>Enviar plantilla a toda la empresa</button>")
    html.append("</form>")

    # ---------- Reset ----------
    html.append("<hr>")
    html.append("<h3>🧹 Reset (limpiar por empresa/período)</h3>")
    html.append("<p>Esto borra <code>pending_views</code> y <code>recibo_estado</code> SOLO para esta empresa (y período si lo completás).</p>")
    html.append("<form method='post' action='/admin/reset' onsubmit='return confirm(\"¿Seguro? Esto borra pending y estados.\");'>")
    html.append(f"<input type='hidden' name='token' value='{esc(token)}'>")
    html.append(f"<input type='hidden' name='tenant' value='{esc(tenant)}'>")
    html.append("<label>Período a resetear (opcional, mm/aaaa): <input type='text' name='period' placeholder='01/2026'></label><br><br>")
    html.append("<button type='submit'>Resetear</button>")
    html.append("</form>")

    # ---------- Preview envíos ----------
    html.append("<hr>")
    html.append("<h3>Preview Excel de envíos</h3>")
    html.append(f"<p>Filas: {len(envios_rows)}</p>")

    sample = envios_rows[:10]
    if sample:
        cols = list(sample[0].keys())
        html.append("<table border='1' cellpadding='6' cellspacing='0'>")
        html.append("<tr>" + "".join(f"<th>{esc(c)}</th>" for c in cols) + "</tr>")
        for r in sample:
            html.append("<tr>" + "".join(f"<td>{esc(str(r.get(c,'')))}</td>" for c in cols) + "</tr>")
        html.append("</table>")
    else:
        html.append("<p>No se pudo leer el Excel de envíos o está vacío.</p>")

    # ---------- Buscar períodos ----------
    html.append("<hr>")
    html.append("<h3>Buscar períodos por CUIL</h3>")
    html.append(f"""
      <form method="get" action="/admin/periodos">
        <input type="hidden" name="token" value="{esc(token)}">
        <input type="hidden" name="tenant" value="{esc(tenant)}">
        <input type="text" name="cuil" placeholder="xx-xxxxxxxx-x" required>
        <button type="submit">Buscar</button>
      </form>
    """)

    html.append("</body></html>")
    return Response("".join(html), mimetype="text/html")



@app.get("/admin/periodos")
def admin_periodos():
    auth = require_admin()
    if auth:
        return auth

    tenant = (request.args.get("tenant") or "").strip().lower()
    cuil = (request.args.get("cuil") or "").strip()
    if not get_tenant(tenant):
        return Response("Tenant inválido", status=400)
    if not cuil:
        return Response("Falta CUIL", status=400)

    periods = list_periods_for_cuil(tenant, cuil)
    return jsonify({"tenant": tenant, "cuil": cuil, "periodos": periods})

import pandas as pd

def get_envios_df_for_tenant(tenant_slug: str, force: bool = False) -> pd.DataFrame:
    """
    Devuelve DataFrame del Excel de envíos de la empresa (tenant).
    Usa tu función existente load_envios_rows(tenant,...).
    """
    rows = load_envios_rows(tenant_slug, force=force) or []
    return pd.DataFrame(rows)


@app.post("/twilio/webhook")
def twilio_webhook_alias():
    return twilio_inbound()

@app.post("/admin/reset_tenant")
@admin_required
def admin_reset_tenant():
    token = _get_admin_token_from_request()
    tenant = (request.form.get("tenant") or "").strip().lower()
    period = (request.form.get("period") or "").strip()  # opcional

    if not tenant:
        return Response("Falta tenant", status=400)

    conn = get_db_connection()
    cur = conn.cursor()

    # limpia pendings de esa empresa
    cur.execute("DELETE FROM pending_views WHERE tenant=?;", (tenant,))

    # si mandan period, limpia solo ese período; si no, limpia todo el estado
    if period:
        cur.execute("DELETE FROM recibo_estado WHERE tenant=? AND period=?;", (tenant, period))
    else:
        cur.execute("DELETE FROM recibo_estado WHERE tenant=?;", (tenant,))

    conn.commit()
    conn.close()

    return redirect(f"/admin/panel?tenant={tenant}&token={token}&msg=reset_ok")

from flask import redirect

from flask import redirect

@app.post("/admin/reset")
@admin_required
def admin_reset():
    token = _get_admin_token_from_request()
    tenant = (request.form.get("tenant") or "").strip().lower()
    period = (request.form.get("period") or "").strip()

    if not tenant:
        return Response("Falta tenant", status=400)

    conn = get_db_connection()
    cur = conn.cursor()

    # Limpia pending siempre
    if period:
        cur.execute("DELETE FROM pending_views WHERE tenant=? AND period=?;", (tenant, period))
        cur.execute("DELETE FROM recibo_estado WHERE tenant=? AND period=?;", (tenant, period))
    else:
        cur.execute("DELETE FROM pending_views WHERE tenant=?;", (tenant,))
        cur.execute("DELETE FROM recibo_estado WHERE tenant=?;", (tenant,))

    conn.commit()
    conn.close()

    return redirect(f"/admin/panel?tenant={tenant}&token={token}&msg=reset_ok&period={period}")

@app.post("/admin/send_template_queue_start")
@admin_required
def admin_send_template_queue_start():
    token = _get_admin_token_from_request()

    tenant = (request.form.get("tenant") or "").strip().lower()
    period = (request.form.get("period") or "").strip()
    limit = int((request.form.get("limit") or "0") or 0)
    require_pdf = (request.form.get("require_pdf") or "true").lower() in ("1", "true", "yes", "on")

    if not tenant:
        return Response("Falta tenant", status=400)
    if not period:
        return Response("Falta period", status=400)

    df = get_envios_df_for_tenant(tenant)
    if df is None or df.empty:
        return Response("Excel de envíos vacío o no encontrado", status=400)

    df.columns = [str(c).strip().lower() for c in df.columns]

    def pick(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    c_nombre = pick("nombre", "name", "empleado", "persona")
    c_tel = pick("telefono", "tel", "celular", "whatsapp", "numero")
    c_arch = pick("archivo", "cuil", "archivo_norm")

    if not c_tel or not c_arch:
        return Response("El Excel debe tener columnas telefono y archivo (cuil).", status=400)

    rows = df.to_dict(orient="records")
    if limit > 0:
        rows = rows[:limit]

    sent = 0
    skipped_no_pdf = 0
    failed = 0

    for r in rows:
        nombre = str(r.get(c_nombre, "")).strip() if c_nombre else ""
        tel_raw = str(r.get(c_tel, "")).strip()
        arch_raw = str(r.get(c_arch, "")).strip()

        if not tel_raw or not arch_raw:
            continue

        # normalizar whatsapp
        tel_digits = "".join(ch for ch in tel_raw if ch.isdigit())
        if not tel_digits:
            continue
        if not tel_digits.startswith("54"):
            tel_digits = "54" + tel_digits
        to_whatsapp = f"whatsapp:+{tel_digits}"

        # cuil desde "archivo"
        cuil = arch_raw.replace(".pdf", "").strip()
        try:
            cuil = strip_pdf(cuil)
        except Exception:
            pass

        # si require_pdf, chequeo
        if require_pdf:
            try:
                ok = pdf_exists_for_tenant_period_cuil(tenant, cuil, period)
            except Exception:
                ok = False
            if not ok:
                skipped_no_pdf += 1
                continue

        try:
            sid = send_whatsapp_template(
                to_whatsapp,
                content_vars={"1": (nombre or "Hola")},
                template_sid=TWILIO_TEMPLATE_SID,
            )
            sent += 1
            print("SENT TEMPLATE", sid, tenant, cuil, period, to_whatsapp)

            add_pending_view(to_whatsapp, tenant, cuil, period)

        except Exception as e:
            failed += 1
            print("ERROR send template:", tenant, cuil, to_whatsapp, e)

    return redirect(
        f"/admin/panel?tenant={tenant}&token={token}&msg=mass_send_ok"
        f"&sent={sent}&failed={failed}&skipped={skipped_no_pdf}&period={period}"
    )


@app.get("/admin/send_test")
def admin_send_test():
    auth = require_admin()
    if auth:
        return auth

    token = request.args.get("token", "")
    tenant = (request.args.get("tenant") or "").strip().lower()
    cuil = (request.args.get("cuil") or "").strip()
    period = (request.args.get("period") or "").strip()

    t = get_tenant(tenant)
    if not t:
        return Response("Tenant inválido", status=400)

    html = []
    html.append("<h2>Envío de prueba (1 persona)</h2>")
    html.append(f"<p><b>Empresa:</b> {esc(t['display_name'])}</p>")
    html.append(f"<p><a href='/admin?token={esc(token)}'>← volver</a></p>")

    html.append(f"""
    <form method="get">
      <input type="hidden" name="tenant" value="{esc(tenant)}">
      <input type="hidden" name="token" value="{esc(token)}">

      <label>CUIL<br>
        <input type="text" name="cuil" value="{esc(cuil)}" required>
      </label><br><br>

      <label>Período (mm/aaaa)<br>
        <input type="text" name="period" value="{esc(period)}" required>
      </label><br><br>

      <button type="submit">Enviar plantilla (prueba)</button>
    </form>
    """)

    if cuil and period:
        html.append("<hr><h3>Resultado</h3>")

        envios = load_envios_rows(tenant)
        person = find_person_by_cuil(envios, cuil)

        if not person or not person.get("to_whatsapp"):
            html.append("<p style='color:red'>No se encontró WhatsApp para ese CUIL en el Excel de envíos.</p>")
            return Response("".join(html), mimetype="text/html")

        phone = person["to_whatsapp"]
        html.append("<p>✔ Persona encontrada</p>")
        html.append(f"<p>👤 Nombre: {esc(person.get('nombre',''))}</p>")
        html.append(f"<p>📞 WhatsApp: {esc(phone)}</p>")

        periods = list_periods_for_cuil(tenant, cuil)
        if period not in periods:
            html.append("<p style='color:red'>No se encontró el PDF para ese período.</p>")
            html.append(f"<p class='mono'>Períodos disponibles: {esc(', '.join(periods))}</p>")
            return Response("".join(html), mimetype="text/html")

        html.append(f"<p>📄 PDF disponible para {esc(period)}</p>")

        # Guardamos pending view ANTES del click VIEW_NOW
        add_pending_view(phone, tenant, strip_pdf(cuil), period)

        # Enviamos plantilla con botón VIEW_NOW (abre conversación)
        try:
            sid_tpl = send_whatsapp_template(
                phone,
                content_vars={
                    "1": person.get("nombre", ""),
                    "2": period,
                },
                template_sid=TWILIO_TEMPLATE_SID or None,
            )
            html.append(f"<p style='color:green'>✅ Plantilla enviada. SID: {esc(sid_tpl)}</p>")
        except Exception as e:
            html.append(f"<p style='color:red'>❌ Error enviando plantilla: {esc(str(e))}</p>")
            return Response("".join(html), mimetype="text/html")

        # Debug: URL del PDF (Twilio la va a pedir cuando toque VIEW_NOW)
        pdf_url = (
            f"{request.host_url.rstrip('/')}/media/pdf"
            f"?tenant={tenant}&cuil={strip_pdf(cuil)}&period={period}&token={ADMIN_TOKEN or token}"
        )
        html.append(f"<p class='mono'>PDF URL (debug): {esc(pdf_url)}</p>")
        html.append("<p>👉 Ahora el empleado toca el botón <b>VIEW_NOW</b> y se envía el PDF automáticamente.</p>")

    return Response("".join(html), mimetype="text/html")

# =========================
# Twilio inbound: VIEW_NOW + firma/observa
# =========================
import time
from flask import Response, request

TWILIO_SIGN_TEMPLATE_SID = os.environ.get("TWILIO_SIGN_TEMPLATE_SID", "").strip()

@app.post("/twilio/inbound")
def twilio_inbound():
    from_whatsapp = (request.form.get("From") or "").strip()
    button = (request.form.get("ButtonPayload") or "").strip()
    body = (request.form.get("Body") or "").strip()

    print("INBOUND:", from_whatsapp, "ButtonPayload:", button, "Body:", body)

    pending = get_latest_pending_view(from_whatsapp)
    print("PENDING:", pending)

    if not pending:
        return Response("OK", status=200)

    tenant = pending["tenant"]
    cuil = pending["cuil"]
    period = pending["period"]

    # 🔒 Si ya cerró, no hacer nada más
    estado = get_recibo_estado(tenant, cuil, period)
    if estado in ("FIRMADO", "OBSERVADO"):
        msg = "✅ Este recibo ya fue firmado." if estado == "FIRMADO" else "📝 Este recibo quedó como observado."
        return Response(f"<Response><Message>{msg}</Message></Response>", mimetype="application/xml", status=200)

    # 1) VIEW_NOW -> enviar PDF + luego firma
    if button == "VIEW_NOW" or body == "VIEW_NOW":
        pdf_url = (
            f"{request.host_url.rstrip('/')}/media/pdf"
            f"?tenant={tenant}&cuil={cuil}&period={period}&token={ADMIN_TOKEN}"
        )

        try:
            sid_pdf = send_whatsapp_pdf(
                from_whatsapp,
                pdf_url,
                body=f"Acá tenés tu recibo {period}.",
                status_callback=STATUS_CALLBACK_URL,   # <-- CLAVE
            )
            print("SENT PDF SID:", sid_pdf)

            set_recibo_estado(tenant, cuil, period, "DISPONIBLE")

            # Guardamos SID para que /twilio/status sepa que este delivered era un PDF
            save_pdf_sid(tenant, cuil, period, from_whatsapp, sid_pdf)

            # NO mandes la plantilla de firma acá
            # Se manda en /twilio/status cuando delivered

        except Exception as e:
            print("ERROR sending PDF:", e)

        return Response("OK", status=200)


    # 2) NO_NEED
    if button == "NO_NEED" or body == "NO_NEED":
        set_recibo_estado(tenant, cuil, period, "NO_NEED")
        consume_pending_view(pending["id"])
        return Response("<Response><Message>✅ Perfecto, no hay problema.</Message></Response>", mimetype="application/xml", status=200)

    # 3) SIGN_OK / SIGN_OBS
    if button in ("SIGN_OK", "SIGN_OBS") or body in ("SIGN_OK", "SIGN_OBS"):
        if button == "SIGN_OK" or body == "SIGN_OK":
            set_recibo_estado(tenant, cuil, period, "FIRMADO")
            consume_pending_view(pending["id"])
            return Response("<Response><Message>✅ Recibo firmado. ¡Gracias!</Message></Response>", mimetype="application/xml", status=200)
        else:
            set_recibo_estado(tenant, cuil, period, "OBSERVADO")
            consume_pending_view(pending["id"])
            return Response("<Response><Message>📝 Recibo observado. Vamos a revisarlo y te contactamos.</Message></Response>", mimetype="application/xml", status=200)

    return Response("OK", status=200)


@app.get("/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time()), "db_path": DB_PATH})
