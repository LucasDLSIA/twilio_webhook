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
_TENANTS_CACHE = {"ts": 0, "items": []}
_TENANTS_TTL = 60  # segundos

def load_tenants(force: bool = False) -> list[dict]:
    """
    Lee el Excel maestro (EMPRESAS_FILE_ID) y devuelve tenants normalizados.
    Soporta headers: Empresa, Envios_File_ID, Drive_Root_ID (case-insensitive)
    y también: slug, display_name, envios_file_id, recibos_root_id, drive_root_id, root_id.
    """
    now = time.time()
    if (not force) and _TENANTS_CACHE["items"] and (now - _TENANTS_CACHE["ts"] < _TENANTS_TTL):
        return _TENANTS_CACHE["items"]

    df = download_excel_df(EMPRESAS_FILE_ID)
    if df is None or df.empty:
        _TENANTS_CACHE.update({"ts": now, "items": []})
        return []

    df.columns = [str(c).strip().lower() for c in df.columns]

    def pick(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    c_slug = pick("slug", "empresa", "tenant")
    c_name = pick("display_name", "nombre", "name", "empresa")
    c_env  = pick("envios_file_id", "envios_file_id ", "envios", "envios_id", "envios_file", "enviosfileid")
    c_root = pick("drive_root_id", "recibos_root_id", "root_id", "drive_root_folder_id", "carpeta_root_id", "drive_root")

    # MUY IMPORTANTE: tu Excel real trae Envios_File_ID y Drive_Root_ID
    # al bajar a lower quedan: envios_file_id y drive_root_id → con esto lo levanta.

    items = []
    for _, r in df.iterrows():
        raw_slug = str(r.get(c_slug, "")).strip()
        if not raw_slug:
            continue

        slug = slugify(raw_slug)  # o tu normalizador de slug
        display_name = str(r.get(c_name, raw_slug)).strip() if c_name else raw_slug

        envios_file_id = str(r.get(c_env, "")).strip() if c_env else ""
        drive_root_id  = str(r.get(c_root, "")).strip() if c_root else ""

        # Normalizamos claves: SIEMPRE devolvemos ambos nombres
        items.append({
            "slug": slug,
            "display_name": display_name,
            "envios_file_id": envios_file_id,
            "drive_root_id": drive_root_id,
            "recibos_root_id": drive_root_id,   # compatibilidad con funciones viejas
        })

    _TENANTS_CACHE.update({"ts": now, "items": items})
    return items

def get_tenant(slug: str) -> dict | None:
    slug = (slug or "").strip().lower()
    if not slug:
        return None

    for t in load_tenants():
        if (t.get("slug") or "").strip().lower() == slug:
            # fallback por si falta alguno
            if not t.get("recibos_root_id") and t.get("drive_root_id"):
                t["recibos_root_id"] = t["drive_root_id"]
            if not t.get("drive_root_id") and t.get("recibos_root_id"):
                t["drive_root_id"] = t["recibos_root_id"]
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
def _norm_period_variants(period: str) -> list[str]:
    """
    Devuelve posibles nombres de carpeta según el período.
    Ej: "01/2026" => ["01-2026", "01_2026", "01 2026", "01/2026", "012026", "2026-01"]
    """
    p = (period or "").strip()
    if not p:
        return []
    p2 = p.replace("-", "/").replace("_", "/").replace(" ", "/")
    parts = p2.split("/")
    if len(parts) == 2:
        mm = parts[0].zfill(2)
        yyyy = parts[1]
    else:
        mm = p[:2].zfill(2)
        yyyy = p[-4:]
    return list(dict.fromkeys([
        f"{mm}/{yyyy}",
        f"{mm}-{yyyy}",
        f"{mm}_{yyyy}",
        f"{mm} {yyyy}",
        f"{mm}{yyyy}",
        f"{yyyy}-{mm}",
        f"{yyyy}{mm}",
    ]))


def _drive_list_children(service, parent_id: str, mime_type: str | None = None, page_size: int = 200) -> list[dict]:
    """
    Lista hijos directos de una carpeta.
    """
    q = f"'{parent_id}' in parents and trashed=false"
    if mime_type:
        q += f" and mimeType='{mime_type}'"
    res = service.files().list(
        q=q,
        fields="files(id,name,mimeType)",
        pageSize=page_size
    ).execute()
    return res.get("files", [])


def _drive_find_child_by_exact_name(service, parent_id: str, name: str, mime_type: str | None = None) -> str | None:
    """
    Busca un hijo por nombre exacto dentro de una carpeta.
    """
    # OJO: name necesita escapar comillas simples si las hubiera (raro en PDFs)
    safe_name = (name or "").replace("'", "\\'")
    q = f"'{parent_id}' in parents and trashed=false and name='{safe_name}'"
    if mime_type:
        q += f" and mimeType='{mime_type}'"
    res = service.files().list(
        q=q,
        fields="files(id,name,mimeType)",
        pageSize=1
    ).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None

def debug_list_pdfs_in_folder(folder_id: str):
    service = drive_service()
    q = f"'{folder_id}' in parents and trashed=false and mimeType='application/pdf'"
    res = service.files().list(
        q=q,
        fields="files(id,name)",
        pageSize=50
    ).execute()

    files = res.get("files", [])
    print("📂 DEBUG PDFs en carpeta", folder_id)
    if not files:
        print("   (no hay PDFs)")
    for f in files:
        print("   -", f["name"], "| id:", f["id"])


# Drive: PDF
# =========================
def find_pdf_file_id_for_cuil_period(tenant: str, cuil: str, period: str) -> str | None:
    t = get_tenant(tenant)
    if not t:
        print("❌ tenant inválido:", tenant)
        return None

    root_id = (t.get("recibos_root_id") or t.get("drive_root_id") or "").strip()
    if not root_id:
        print("❌ tenant sin recibos_root_id:", tenant)
        return None

    cuil = strip_pdf(cuil).strip()
    filename = f"{cuil}.pdf"

    # el root tiene subcarpetas por período tipo "12-2025"
    # si te pasan "12/2025", lo convertimos
    period = period.strip()
    period_folder_name = period.replace("/", "-")

    service = drive_service()

    # 1) buscar carpeta del período dentro del root
    q_folder = (
        f"'{root_id}' in parents and trashed=false "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and name='{period_folder_name}'"
    )
    res = service.files().list(q=q_folder, fields="files(id,name)", pageSize=5).execute()
    folders = res.get("files", [])
    if not folders:
        print(f"❌ No encontré carpeta período '{period_folder_name}' en root {root_id}")
        return None

    period_id = folders[0]["id"]

    # 2) buscar el PDF dentro de esa carpeta
    q_pdf = (
        f"'{period_id}' in parents and trashed=false "
        f"and name='{filename}'"
    )
    res2 = service.files().list(q=q_pdf, fields="files(id,name)", pageSize=5).execute()
    files = res2.get("files", [])
    if not files:
        print(f"❌ No encontré {filename} dentro de carpeta período {period_folder_name} ({period_id})")
        return None

    return files[0]["id"]


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


def ensure_sqlite_columns(table: str, columns: dict[str, str]) -> None:
    """
    columns: {"colname": "INTEGER", "error_message": "TEXT", ...}
    Agrega columnas faltantes via ALTER TABLE.
    """
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(f"PRAGMA table_info({table});")
    existing = {row[1] for row in cur.fetchall()}  # row[1] = name

    for col, col_type in columns.items():
        if col not in existing:
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type};")
            except Exception:
                pass

    conn.commit()
    conn.close()

# =========================
# Media endpoint (Twilio descarga PDF desde acá)
# =========================
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
    resp.headers["Content-Disposition"] = f'inline; filename="{strip_pdf(cuil)}.pdf"'
    return resp


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


def send_whatsapp_template(
    to_whatsapp: str,
    content_vars: Optional[dict] = None,
    template_sid: Optional[str] = None,
    status_callback: Optional[str] = None,
) -> str:
    """
    Envío de plantilla aprobada (WhatsApp) usando Content Templates.
    template_sid:
      - por defecto usa TWILIO_TEMPLATE_SID (la de VIEW_NOW)
      - podés pasar TWILIO_SIGN_TEMPLATE_SID para la de firma/observa
    """
    tpl = (template_sid or TWILIO_TEMPLATE_SID or "").strip()
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

    if status_callback:
        payload["status_callback"] = status_callback

    msg = client.messages.create(**payload)
    return msg.sid
# =========================

def _set_status_on_table(table: str, sid: str, status: str, error_code=None, error_message=None):
    now = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()

    if status == "delivered":
        cur.execute(f"UPDATE {table} SET delivered_at=? WHERE message_sid=?;", (now, sid))
    elif status == "read":
        cur.execute(f"UPDATE {table} SET read_at=? WHERE message_sid=?;", (now, sid))
    elif status in ("failed", "undelivered"):
        cur.execute(
            f"UPDATE {table} SET failed_at=?, error_code=?, error_message=? WHERE message_sid=?;",
            (now, str(error_code or ""), str(error_message or ""), sid),
        )

    conn.commit()
    conn.close()

def is_pdf_sid(message_sid: str) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_pdfs WHERE message_sid = ? LIMIT 1", (message_sid,))
    ok = cur.fetchone() is not None
    conn.close()
    return ok


@app.post("/twilio/status")
def twilio_status():
    sid = (request.form.get("MessageSid") or "").strip()
    status = (request.form.get("MessageStatus") or "").strip().lower()
    error_code = (request.form.get("ErrorCode") or "").strip()
    error_message = (request.form.get("ErrorMessage") or "").strip()
    now = int(time.time())

    if not sid:
        return Response("OK", status=200)

    conn = get_db_connection()
    cur = conn.cursor()

    # actualizar tracking
    cur.execute("""
        UPDATE message_status
        SET last_status = ?, last_status_at = ?,
            error_code = CASE WHEN ? != '' THEN ? ELSE error_code END,
            error_message = CASE WHEN ? != '' THEN ? ELSE error_message END
        WHERE message_sid = ?
    """, (status, now, error_code, error_code, error_message, error_message, sid))

    if status == "delivered":
        cur.execute("""
            UPDATE message_status
            SET delivered_at = COALESCE(delivered_at, ?)
            WHERE message_sid = ?
        """, (now, sid))

    if status == "read":
        cur.execute("""
            UPDATE message_status
            SET read_at = COALESCE(read_at, ?)
            WHERE message_sid = ?
        """, (now, sid))

    if status == "failed":
        cur.execute("""
            UPDATE message_status
            SET failed_at = COALESCE(failed_at, ?),
                error_code = COALESCE(NULLIF(?, ''), error_code),
                error_message = COALESCE(NULLIF(?, ''), error_message)
            WHERE message_sid = ?
        """, (now, error_code, error_message, sid))

    # si fue delivered y es PDF -> mandar SIGN una sola vez
    if status == "delivered":
        cur.execute("""
            SELECT tenant, cuil, period, to_whatsapp, sign_sent_at
            FROM sent_pdfs
            WHERE message_sid = ?
            LIMIT 1
        """, (sid,))
        row = cur.fetchone()

        if row:
            tenant, cuil, period, to_whatsapp, sign_sent_at = row
            if not sign_sent_at and TWILIO_SIGN_TEMPLATE_SID:
                try:
                    sid_sign = send_whatsapp_template(
                        to_whatsapp,
                        content_vars={"1": period},
                        template_sid=TWILIO_SIGN_TEMPLATE_SID
                    )
                    # opcional: trackear el SIGN en message_status también
                    cur.execute("""
                        INSERT OR IGNORE INTO message_status
                        (message_sid, to_whatsapp, tenant, cuil, period, kind, created_at, last_status, last_status_at)
                        VALUES (?, ?, ?, ?, ?, 'sign', ?, 'sent', ?)
                    """, (sid_sign, to_whatsapp, tenant, cuil, period, now, now))

                    cur.execute("UPDATE sent_pdfs SET sign_sent_at = ? WHERE message_sid = ?", (now, sid))
                    print("SENT SIGN AFTER PDF DELIVERED:", sid_sign)
                except Exception as e:
                    print("WARN: could not send SIGN:", e)

    conn.commit()
    conn.close()
    return Response("OK", status=200)


def is_template_sid(message_sid: str) -> bool:
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_templates WHERE message_sid=? LIMIT 1;", (message_sid,))
    row = cur.fetchone()
    conn.close()
    return bool(row)

def get_sent_pdf_by_sid(message_sid: str) -> dict | None:
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT tenant,cuil,period,to_whatsapp,sign_sent_at FROM sent_pdfs WHERE message_sid=? LIMIT 1;", (message_sid,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def mark_sign_sent(pdf_sid: str):
    now = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE sent_pdfs SET sign_sent_at=? WHERE message_sid=?;", (now, pdf_sid))
    conn.commit()
    conn.close()




# =========================
# DB: pending view + estado firma
# =========================
DB_PATH = os.environ.get("DB_PATH", "/data/app.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn


import time, hashlib

def _try_alter(cur, sql: str):
    try:
        cur.execute(sql)
    except Exception:
        pass

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")

    # =========
    # pending_views (ahora con step + intentos)
    # =========
    cur.execute("""
      CREATE TABLE IF NOT EXISTS pending_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        to_whatsapp TEXT NOT NULL,
        tenant TEXT NOT NULL,
        cuil TEXT NOT NULL,
        period TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        step TEXT DEFAULT 'READY',          -- READY | AWAIT_DNI
        dni_attempts INTEGER DEFAULT 0,
        UNIQUE(to_whatsapp, tenant, cuil, period)
      );
    """)
    _try_alter(cur, "ALTER TABLE pending_views ADD COLUMN step TEXT;")
    _try_alter(cur, "ALTER TABLE pending_views ADD COLUMN dni_attempts INTEGER;")

    # =========
    # recibo_estado (FIRMADO/OBSERVADO/NO_NEED)
    # =========
    cur.execute("""
      CREATE TABLE IF NOT EXISTS recibo_estado (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant TEXT NOT NULL,
        cuil TEXT NOT NULL,
        period TEXT NOT NULL,
        estado TEXT NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE(tenant, cuil, period)
      );
    """)

    # =========
    # message_status (status template/pdf)
    # =========
    cur.execute("""
      CREATE TABLE IF NOT EXISTS message_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_sid TEXT UNIQUE NOT NULL,
        to_whatsapp TEXT,
        tenant TEXT,
        cuil TEXT,
        period TEXT,
        nombre TEXT,
        kind TEXT,                -- 'template' | 'pdf'
        created_at INTEGER,
        last_status TEXT,
        last_status_at INTEGER,
        delivered_at INTEGER,
        read_at INTEGER,
        failed_at INTEGER,
        error_code TEXT,
        error_message TEXT
      );
    """)
    # migraciones safe
    for col, typ in [
        ("to_whatsapp","TEXT"),("tenant","TEXT"),("cuil","TEXT"),("period","TEXT"),
        ("nombre","TEXT"),("kind","TEXT"),("created_at","INTEGER"),
        ("last_status","TEXT"),("last_status_at","INTEGER"),
        ("delivered_at","INTEGER"),("read_at","INTEGER"),("failed_at","INTEGER"),
        ("error_code","TEXT"),("error_message","TEXT"),
    ]:
        _try_alter(cur, f"ALTER TABLE message_status ADD COLUMN {col} {typ};")

    # =========
    # sent_pdfs (para mandar SIGN después del delivered)
    # =========
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
    _try_alter(cur, "ALTER TABLE sent_pdfs ADD COLUMN sign_sent_at INTEGER;")

    # =========
    # ✅ NUEVO: verified_contacts (verificación DNI por número)
    # =========
    cur.execute("""
      CREATE TABLE IF NOT EXISTS verified_contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant TEXT NOT NULL,
        cuil TEXT NOT NULL,
        to_whatsapp TEXT NOT NULL,
        dni_hash TEXT NOT NULL,
        dni_last4 TEXT,
        verified_at INTEGER NOT NULL,
        UNIQUE(tenant, cuil, to_whatsapp)
      );
    """)

    # índices útiles
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_pending_to_created ON pending_views(to_whatsapp, created_at);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_estado_key ON recibo_estado(tenant, cuil, period);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_msg_key ON message_status(tenant, cuil, period, kind);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_msg_sid ON message_status(message_sid);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_sentpdfs_sid ON sent_pdfs(message_sid);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_verified_key ON verified_contacts(tenant, cuil, to_whatsapp);")

    conn.commit()
    conn.close()


init_db()

def save_pdf_sid(tenant: str, cuil: str, period: str, to_whatsapp: str, sid: str):
    now = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()

    # tracking para reporte
    cur.execute("""
        INSERT OR IGNORE INTO message_status
        (message_sid, to_whatsapp, tenant, cuil, period, kind, created_at, last_status, last_status_at)
        VALUES (?, ?, ?, ?, ?, 'pdf', ?, 'sent', ?)
    """, (sid, to_whatsapp, tenant, cuil, period, now, now))

    # tracking para enviar SIGN al delivered
    cur.execute("""
        INSERT OR IGNORE INTO sent_pdfs
        (tenant, cuil, period, to_whatsapp, message_sid, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (tenant, cuil, period, to_whatsapp, sid, now))

    conn.commit()
    conn.close()

def _digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())

def cuil_to_dni(cuil: str) -> str | None:
    """
    Para personas físicas AR: CUIL = XX + DNI(8) + X
    Ej: 20-28169249-3 -> DNI 28169249
    """
    d = _digits(cuil)
    if len(d) != 11:
        return None
    return d[2:10]  # 8 dígitos

def dni_hash(dni: str) -> str:
    # no guardamos el DNI plano
    return hashlib.sha256(dni.encode("utf-8")).hexdigest()

def is_verified_contact(tenant: str, cuil: str, to_whatsapp: str) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
      SELECT 1 FROM verified_contacts
      WHERE tenant=? AND cuil=? AND to_whatsapp=?
      LIMIT 1
    """, (tenant, cuil, to_whatsapp))
    ok = cur.fetchone() is not None
    conn.close()
    return ok

def set_verified_contact(tenant: str, cuil: str, to_whatsapp: str, dni: str):
    h = dni_hash(dni)
    last4 = dni[-4:] if dni else None
    now = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO verified_contacts (tenant, cuil, to_whatsapp, dni_hash, dni_last4, verified_at)
      VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT(tenant, cuil, to_whatsapp) DO UPDATE SET
        dni_hash=excluded.dni_hash,
        dni_last4=excluded.dni_last4,
        verified_at=excluded.verified_at
    """, (tenant, cuil, to_whatsapp, h, last4, now))
    conn.commit()
    conn.close()

def set_pending_step(pending_id: int, step: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE pending_views SET step=? WHERE id=?", (step, pending_id))
    conn.commit()
    conn.close()

def inc_pending_dni_attempts(pending_id: int) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE pending_views SET dni_attempts = COALESCE(dni_attempts,0) + 1 WHERE id=?", (pending_id,))
    cur.execute("SELECT dni_attempts FROM pending_views WHERE id=?", (pending_id,))
    n = (cur.fetchone() or [0])[0]
    conn.commit()
    conn.close()
    return int(n)


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
    now = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()

    # borrar cualquier pending previo para ese 4-tuple (evita duplicados)
    cur.execute("""
      DELETE FROM pending_views
      WHERE to_whatsapp=? AND tenant=? AND cuil=? AND period=?
    """, (to_whatsapp, tenant, cuil, period))

    # insertar nuevo pending
    cur.execute("""
      INSERT INTO pending_views (to_whatsapp, tenant, cuil, period, created_at, step, dni_attempts)
      VALUES (?, ?, ?, ?, ?, 'READY', 0)
    """, (to_whatsapp, tenant, cuil, period, now))

    conn.commit()
    conn.close()


def get_latest_pending_view(to_whatsapp: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
      SELECT
        id,
        to_whatsapp,
        tenant,
        cuil,
        period,
        created_at,
        COALESCE(step, 'READY') AS step,
        COALESCE(dni_attempts, 0) AS dni_attempts
      FROM pending_views
      WHERE to_whatsapp=?
      ORDER BY created_at DESC
      LIMIT 1
    """, (to_whatsapp,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None

    # si usás sqlite3.Row como row_factory, esto funciona:
    try:
        return dict(row)
    except Exception:
        # fallback si fetchone devuelve tuple
        keys = ["id","to_whatsapp","tenant","cuil","period","created_at","step","dni_attempts"]
        return dict(zip(keys, row))

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

def save_template_sid(tenant: str, cuil: str, period: str, to_whatsapp: str, sid: str, nombre: str = ""):
    now = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO message_status
        (message_sid, to_whatsapp, tenant, cuil, period, nombre, kind, created_at, last_status, last_status_at)
        VALUES (?, ?, ?, ?, ?, ?, 'template', ?, 'sent', ?)
    """, (sid, to_whatsapp, tenant, cuil, period, nombre, now, now))
    conn.commit()
    conn.close()



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

    html.append("<hr>")
    html.append("<h3>Reportes</h3>")
    html.append(f"""
    <form method="get" action="/admin/report_recibos.xlsx" style="margin-bottom:10px;">
        <input type="hidden" name="token" value="{esc(token)}">
        <input type="hidden" name="tenant" value="{esc(tenant)}">
        <label>Período (opcional mm/aaaa):</label>
        <input type="text" name="period" placeholder="01/2026" style="margin-left:8px;">
        <button type="submit">📄 Descargar reporte recibos (XLSX)</button>
    </form>

    <p>
        <a href="/admin/report_envios.csv?tenant={esc(tenant)}&token={esc(token)}">
        📄 Envíos realizados (CSV)
        </a>
    </p>
    """)



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

def get_estado_report(tenant: str, period: str | None = None):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if period:
        cur.execute("""
            SELECT cuil, period, estado, updated_at
            FROM recibo_estado
            WHERE tenant = ? AND period = ?
            ORDER BY cuil
        """, (tenant, period))
    else:
        cur.execute("""
            SELECT cuil, period, estado, updated_at
            FROM recibo_estado
            WHERE tenant = ?
            ORDER BY period DESC, cuil
        """, (tenant,))

    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from flask import send_file
import io
import datetime


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return ""
    # tu server está en UTC, si querés BA: ajustá acá (UTC-3) o dejalo así
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@app.get("/admin/report_recibos.xlsx")
@admin_required
def admin_report_recibos_xlsx():
    token = _get_admin_token_from_request()
    tenant = (request.args.get("tenant") or "").strip().lower()
    period = (request.args.get("period") or "").strip()

    if not tenant:
        return Response("Falta tenant", status=400)

    # 1) Traigo envíos (para nombre/whatsapp). Si no querés depender del excel, igual te queda.
    df_env = None
    try:
        df_env = get_envios_df_for_tenant(tenant)
        if df_env is not None and not df_env.empty:
            df_env.columns = [str(c).strip().lower() for c in df_env.columns]
    except Exception:
        df_env = None

    # map CUIL -> (nombre, whatsapp)
    env_map = {}
    if df_env is not None and not df_env.empty:
        def pick(*names):
            for n in names:
                if n in df_env.columns:
                    return n
            return None
        c_nombre = pick("nombre", "name", "empleado", "persona")
        c_tel = pick("telefono", "tel", "celular", "whatsapp", "numero")
        c_arch = pick("archivo", "cuil", "archivo_norm")
        if c_tel and c_arch:
            for r in df_env.to_dict(orient="records"):
                arch_raw = str(r.get(c_arch, "")).strip()
                tel_raw = str(r.get(c_tel, "")).strip()
                nombre = str(r.get(c_nombre, "")).strip() if c_nombre else ""
                if not arch_raw:
                    continue
                cuil = arch_raw.replace(".pdf", "").strip()
                try:
                    cuil = strip_pdf(cuil)
                except Exception:
                    pass
                # normalizar whatsapp
                tel_digits = "".join(ch for ch in tel_raw if ch.isdigit())
                if tel_digits:
                    if not tel_digits.startswith("54"):
                        tel_digits = "54" + tel_digits
                    wa = f"whatsapp:+{tel_digits}"
                else:
                    wa = ""
                env_map[(cuil)] = (nombre, wa)

    conn = get_db_connection()
    cur = conn.cursor()

    # 2) base: todas las combinaciones tenant/period/cuil que aparecen en message_status o recibo_estado
    params = [tenant]
    where_period = ""
    if period:
        where_period = " AND period = ? "
        params.append(period)

    cur.execute(f"""
        SELECT DISTINCT tenant, period, cuil
        FROM (
            SELECT tenant, period, cuil FROM message_status WHERE tenant = ? {where_period}
            UNION
            SELECT tenant, period, cuil FROM recibo_estado WHERE tenant = ? {where_period}
        )
        ORDER BY period, cuil
    """, params + params)

    keys = cur.fetchall()

    rows = []
    for _tenant, _period, _cuil in keys:
        # plantilla (VIEW_NOW)
        cur.execute("""
            SELECT created_at, delivered_at, read_at, failed_at
            FROM message_status
            WHERE tenant=? AND period=? AND cuil=? AND kind='template'
            ORDER BY created_at DESC
            LIMIT 1
        """, (_tenant, _period, _cuil))
        t = cur.fetchone() or (None, None, None, None)

        # pdf
        cur.execute("""
            SELECT created_at, delivered_at, read_at, failed_at
            FROM message_status
            WHERE tenant=? AND period=? AND cuil=? AND kind='pdf'
            ORDER BY created_at DESC
            LIMIT 1
        """, (_tenant, _period, _cuil))
        p = cur.fetchone() or (None, None, None, None)

        # respuesta usuario
        cur.execute("""
            SELECT estado, updated_at
            FROM recibo_estado
            WHERE tenant=? AND period=? AND cuil=?
            LIMIT 1
        """, (_tenant, _period, _cuil))
        e = cur.fetchone()
        estado = (e[0] if e else "") or ""
        updated_at = (e[1] if e else None)

        nombre, wa = env_map.get(_cuil, ("", ""))

        rows.append({
            "Periodo": _period,
            "Nombre": nombre,
            "CUIL": _cuil,
            "WhatsApp": wa,
            "Plantilla_enviada": _fmt_ts(t[0]),
            "Plantilla_entregada": _fmt_ts(t[1]),
            "Plantilla_leida": _fmt_ts(t[2]),
            "Plantilla_fallida": _fmt_ts(t[3]),
            "PDF_enviado": _fmt_ts(p[0]),
            "PDF_entregado": _fmt_ts(p[1]),
            "PDF_leido": _fmt_ts(p[2]),
            "PDF_fallido": _fmt_ts(p[3]),
            "Respuesta_usuario": estado.lower() if estado else "",
            "Respuesta_timestamp": _fmt_ts(updated_at),
        })

    conn.close()

    import pandas as pd
    import io

    df = pd.DataFrame(rows, columns=[
        "Periodo","Nombre","CUIL","WhatsApp",
        "Plantilla_enviada","Plantilla_entregada","Plantilla_leida","Plantilla_fallida",
        "PDF_enviado","PDF_entregado","PDF_leido","PDF_fallido",
        "Respuesta_usuario","Respuesta_timestamp"
    ])

    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Reporte")

    bio.seek(0)  # <- CLAVE para que no salga "a mitad"
    filename = f"reporte_recibos_{tenant}_{(period or 'todos').replace('/','-')}.xlsx"
    resp = Response(
        bio.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp



@app.get("/admin/report_estado.csv")
def admin_report_estado():
    auth = require_admin()
    if auth:
        return auth

    tenant = request.args.get("tenant", "").strip().lower()
    period = request.args.get("period")

    rows = get_estado_report(tenant, period)

    def generate():
        yield "cuil,period,estado,updated_at\n"
        for r in rows:
            yield f"{r['cuil']},{r['period']},{r['estado']},{r['updated_at']}\n"

    return Response(
        generate(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=estado_{tenant}.csv"
        }
    )

def get_sent_pdfs_report(tenant: str):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT cuil, period, to_whatsapp, message_sid, created_at, sign_sent_at
        FROM sent_pdfs
        WHERE tenant = ?
        ORDER BY created_at DESC
    """, (tenant,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]



@app.get("/admin/report_envios.csv")
def admin_report_envios():
    auth = require_admin()
    if auth:
        return auth

    tenant = request.args.get("tenant", "").strip().lower()
    rows = get_sent_pdfs_report(tenant)

    def generate():
        yield "cuil,period,whatsapp,message_sid,created_at,sign_sent_at\n"
        for r in rows:
            yield f"{r['cuil']},{r['period']},{r['to_whatsapp']},{r['message_sid']},{r['created_at']},{r['sign_sent_at'] or ''}\n"

    return Response(
        generate(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=envios_{tenant}.csv"
        }
    )

def _period_variants(period: str) -> list[str]:
    # period esperado: "01/2026" (o "01-2026")
    p = (period or "").strip()
    p = p.replace("-", "/").replace("_", "/").replace(".", "/").replace(" ", "/")
    # si viene "012026" intentar normalizar
    if len(p) == 6 and p.isdigit():
        p = f"{p[:2]}/{p[2:]}"
    if "/" not in p:
        return [p]

    mm, yyyy = p.split("/", 1)
    mm = mm.zfill(2)
    yyyy = yyyy.strip()

    return [
        f"{mm}/{yyyy}",
        f"{mm}-{yyyy}",
        f"{mm}_{yyyy}",
        f"{mm} {yyyy}",
        f"{mm}.{yyyy}",
        f"{mm}{yyyy}",
    ]


def _find_period_folder_id(service, root_id: str, period: str) -> str | None:
    # Busca una carpeta del período DIRECTAMENTE bajo root_id
    for name in _period_variants(period):
        q = (
            f"'{root_id}' in parents and trashed=false "
            f"and mimeType='application/vnd.google-apps.folder' "
            f"and name='{name}'"
        )
        res = service.files().list(q=q, fields="files(id,name)", pageSize=1).execute()
        files = res.get("files", [])
        if files:
            return files[0]["id"]
    return None


def find_pdf_file_id(tenant: str, cuil: str, period: str) -> str | None:
    """
    Busca el PDF {cuil}.pdf en el root del tenant.
    (Misma lógica que /media/pdf)
    """
    t = get_tenant(tenant)
    if not t:
        print("❌ tenant inválido:", tenant)
        return None

    root_id = (t.get("drive_root_id") or t.get("recibos_root_id") or "").strip()
    print("ROOT_ID:", root_id)

    if not root_id:
        print("❌ tenant sin drive_root_id/recibos_root_id:", tenant, t)
        return None

    cuil = strip_pdf(cuil).strip()
    filename = f"{cuil}.pdf"

    service = drive_service()

    q = f"'{root_id}' in parents and trashed=false and name='{filename}'"
    res = service.files().list(q=q, fields="files(id,name)", pageSize=5).execute()
    files = res.get("files", [])

    # debug útil
    if not files:
        print("🔎 NO ENCONTRÉ:", filename, "en root:", root_id)

    return files[0]["id"] if files else None



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

def _drive_list_children(parent_id: str, page_size: int = 30):
    service = drive_service()
    q = f"'{parent_id}' in parents and trashed=false"
    res = service.files().list(
        q=q,
        fields="files(id,name,mimeType)",
        pageSize=page_size
    ).execute()
    return res.get("files", [])

def _drive_find_folder_by_name(parent_id: str, name: str) -> str | None:
    service = drive_service()
    q = (
        f"'{parent_id}' in parents and trashed=false "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and name='{name}'"
    )
    res = service.files().list(q=q, fields="files(id,name)", pageSize=5).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


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

        # Normalizar WhatsApp
        tel_digits = "".join(ch for ch in tel_raw if ch.isdigit())
        if not tel_digits:
            continue
        if not tel_digits.startswith("54"):
            tel_digits = "54" + tel_digits
        to_whatsapp = f"whatsapp:+{tel_digits}"

        # CUIL desde archivo
        cuil = arch_raw.replace(".pdf", "").strip()
        try:
            cuil = strip_pdf(cuil)
        except Exception:
            continue
        print("\n--- ROW DEBUG ---")
        print("tenant:", tenant)
        print("nombre:", nombre)
        print("cuil:", cuil)
        print("period:", period)

        # 🔒 verificar PDF (solo si require_pdf=True)
        if require_pdf:
            pdf_file_id = find_pdf_file_id_for_cuil_period(tenant, cuil, period)

            if not pdf_file_id:
                skipped_no_pdf += 1
                print("SKIP (no pdf):", tenant, cuil, period)
                continue
        # ✅ Recién ahora mandamos VIEW_NOW
        try:
            sid = send_whatsapp_template(
                to_whatsapp,
                content_vars={"1": (nombre or "Hola")},
                template_sid=TWILIO_TEMPLATE_SID,
                status_callback=STATUS_CALLBACK_URL,
            )

            sent += 1
            save_template_sid(tenant, cuil, period, to_whatsapp, sid, nombre=nombre)

            print("SENT VIEW_NOW", sid, tenant, cuil, period)
            add_pending_view(to_whatsapp, tenant, cuil, period)

        except Exception as e:
            failed += 1
            print("ERROR send template:", tenant, cuil, to_whatsapp, e)

    return redirect(
        f"/admin/panel?tenant={tenant}&token={token}&msg=mass_send_ok"
        f"&sent={sent}&failed={failed}&skipped={skipped_no_pdf}&period={period}"
    )

def debug_list_root_pdfs(tenant: str, limit=20):
    t = get_tenant(tenant)
    root_id = t.get("drive_root_id") or t.get("recibos_root_id")
    service = drive_service()

    q = f"'{root_id}' in parents and trashed=false"
    res = service.files().list(
        q=q,
        fields="files(id,name,mimeType)",
        pageSize=limit
    ).execute()

    print("\n=== ROOT FILES ===")
    for f in res.get("files", []):
        print(f["name"], f["mimeType"])



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
    step = (pending.get("step") or "READY").upper()
    print("STEP:", step, "BODY_DIGITS:", "".join(ch for ch in body if ch.isdigit()))

    # 🔒 Si ya cerró, no hacer nada más
    estado = get_recibo_estado(tenant, cuil, period)
    if estado in ("FIRMADO", "OBSERVADO"):
        msg = "✅ Este recibo ya fue firmado." if estado == "FIRMADO" else "📝 Este recibo quedó como observado."
        return Response(f"<Response><Message>{msg}</Message></Response>", mimetype="application/xml", status=200)

    # 0) Si estamos esperando DNI, tratamos el body como DNI
    if step == "AWAIT_DNI":
        dni_user = _digits(body)

        # si tocó botones mientras pedíamos DNI, lo guiamos
        if button:
            return Response(
                "<Response><Message>🔐 Para continuar, enviá tu DNI (solo números).</Message></Response>",
                mimetype="application/xml",
                status=200
            )

        dni_expected = cuil_to_dni(cuil)
        if not dni_expected or len(dni_user) < 7:
            inc_pending_dni_attempts(pending["id"])
            return Response(
                "<Response><Message>🔐 Enviá tu DNI (solo números, sin puntos). Ej: 28169249</Message></Response>",
                mimetype="application/xml",
                status=200
            )

        if dni_user != dni_expected:
            tries = inc_pending_dni_attempts(pending["id"])
            if tries >= 3:
                # seguridad: cortamos para evitar brute force
                consume_pending_view(pending["id"])
                return Response(
                    "<Response><Message>❌ DNI incorrecto (3 intentos). Volvé a solicitar el recibo desde el mensaje inicial.</Message></Response>",
                    mimetype="application/xml",
                    status=200
                )
            return Response(
                f"<Response><Message>❌ DNI incorrecto. Intento {tries}/3. Probá de nuevo (solo números).</Message></Response>",
                mimetype="application/xml",
                status=200
            )

        # ✅ verificado
        set_verified_contact(tenant, cuil, from_whatsapp, dni_user)
        set_pending_step(pending["id"], "READY")

        # ahora sí mandamos el PDF como si hubiera tocado VIEW_NOW
        return _send_pdf_flow(from_whatsapp, tenant, cuil, period)

    # 1) VIEW_NOW
    if button == "VIEW_NOW" or body == "VIEW_NOW":
        # ✅ si NO está verificado, pedimos DNI
        if not is_verified_contact(tenant, cuil, from_whatsapp):
            set_pending_step(pending["id"], "AWAIT_DNI")
            return Response(
                "<Response><Message>🔐 Para ver tu recibo, enviá tu DNI (solo números, sin puntos).</Message></Response>",
                mimetype="application/xml",
                status=200
            )

        # ✅ ya verificado: mandamos directo
        return _send_pdf_flow(from_whatsapp, tenant, cuil, period)

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


def _send_pdf_flow(from_whatsapp: str, tenant: str, cuil: str, period: str):
    # ⚠️ opcional pero recomendado: re-chequear que exista PDF antes de enviar
    file_id = find_pdf_file_id_for_cuil_period(tenant, cuil, period)
    if not file_id:
        return Response(
            "<Response><Message>⚠️ No encontramos tu recibo para ese período. Si creés que es un error, avisá a RRHH.</Message></Response>",
            mimetype="application/xml",
            status=200
        )

    pdf_url = (
        f"{request.host_url.rstrip('/')}/media/pdf"
        f"?tenant={tenant}&cuil={cuil}&period={period}&token={ADMIN_TOKEN}"
    )

    try:
        sid_pdf = send_whatsapp_pdf(
            from_whatsapp,
            pdf_url,
            body=f"Acá tenés tu recibo {period}.",
            status_callback=STATUS_CALLBACK_URL,
        )
        print("SENT PDF SID:", sid_pdf)

        # estado interno si querés, pero tu reporte NO lo usa como "respuesta usuario"
        set_recibo_estado(tenant, cuil, period, "DISPONIBLE")

        save_pdf_sid(tenant, cuil, period, from_whatsapp, sid_pdf)

    except Exception as e:
        print("ERROR sending PDF:", e)

    return Response("OK", status=200)


@app.get("/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time()), "db_path": DB_PATH})
