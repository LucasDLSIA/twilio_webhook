import os
import io
import re
import time
import sqlite3
import json
from datetime import datetime
import datetime as _dt

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
    period_folder_name = normalize_period_for_drive(period)

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

            # ✅ NUEVO: si este PDF fue un reenvío (RESEND_LAST), NO mandamos SIGN
            try:
                origin = get_receipt_event_origin_by_sid(sid)
            except Exception as e:
                origin = None
                print("WARN: could not resolve origin by sid:", e)

            if origin == "RESEND_LAST":
                # marcamos sign_sent_at para no reintentar (opcional pero recomendable)
                if not sign_sent_at:
                    cur.execute("UPDATE sent_pdfs SET sign_sent_at = ? WHERE message_sid = ?", (now, sid))
                print("SKIP SIGN AFTER PDF (RESEND_LAST):", sid)
            else:
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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_latest_context_for_whatsapp(to_whatsapp: str) -> dict | None:
    """
    Devuelve el último contexto conocido (tenant, cuil, period) para ese WhatsApp,
    mirando message_status (template/pdf).
    """
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT tenant, cuil, period
        FROM message_status
        WHERE to_whatsapp = ?
          AND COALESCE(tenant,'') != ''
          AND COALESCE(cuil,'') != ''
        ORDER BY COALESCE(created_at,0) DESC, id DESC
        LIMIT 1
    """, (to_whatsapp,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def resolve_best_period_with_pdf(tenant: str, cuil: str) -> str | None:
    """
    Devuelve:
      - mes actual si existe PDF
      - si no, el último período disponible con PDF
    """
    import datetime as _dt
    now = _dt.datetime.now()

    current = f"{now.month:02d}/{now.year:04d}"

    # 1) Mes actual (si existe)
    try:
        fid = find_pdf_file_id_for_cuil_period(tenant, cuil, current)
        if fid:
            return current
    except Exception:
        pass  # no existe carpeta/periodo -> seguimos

    # 2) Último período disponible
    periods = []
    try:
        periods = list_periods_for_cuil(tenant, strip_pdf(cuil))  # debería devolverte algo tipo ["01/2026","12/2025",...]
    except Exception:
        periods = []

    if not periods:
        return None

    # normalizamos por si vienen con "-" en vez de "/"
    return (periods[0] or "").replace("-", "/")


def get_receipt_request_count(tenant: str, cuil: str, period: str, to_whatsapp: str) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT request_count
        FROM receipt_requests
        WHERE tenant=? AND cuil=? AND period=? AND to_whatsapp=?
        LIMIT 1
    """, (tenant, cuil, period, to_whatsapp))
    row = cur.fetchone()
    conn.close()
    return int(row[0]) if row and row[0] is not None else 0


def inc_receipt_request_count(tenant: str, cuil: str, period: str, to_whatsapp: str) -> int:
    now = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()

    # upsert compatible
    cur.execute("""
        SELECT id, request_count, first_requested_at
        FROM receipt_requests
        WHERE tenant=? AND cuil=? AND period=? AND to_whatsapp=?
        LIMIT 1
    """, (tenant, cuil, period, to_whatsapp))
    row = cur.fetchone()

    if row:
        rid, cnt, first_ts = row
        cnt = int(cnt or 0) + 1
        cur.execute("""
            UPDATE receipt_requests
            SET request_count=?, last_requested_at=?
            WHERE id=?
        """, (cnt, now, rid))
    else:
        cnt = 1
        cur.execute("""
            INSERT INTO receipt_requests
            (tenant,cuil,period,to_whatsapp,request_count,first_requested_at,last_requested_at)
            VALUES (?,?,?,?,?,?,?)
        """, (tenant, cuil, period, to_whatsapp, cnt, now, now))

    conn.commit()
    conn.close()
    return cnt

def ensure_receipt_request_events_schema():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()

        # Tabla base (si no existe)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS receipt_request_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant TEXT,
            cuil TEXT,
            period TEXT,
            whatsapp TEXT,
            trigger TEXT,
            outcome TEXT,
            message_sid TEXT
        )
        """)

        # Agregar created_at si falta
        cols = [r[1] for r in cur.execute("PRAGMA table_info(receipt_request_events)").fetchall()]
        if "created_at" not in cols:
            cur.execute("ALTER TABLE receipt_request_events ADD COLUMN created_at INTEGER")

        # (opcional, para el punto 1) origin si falta
        if "origin" not in cols:
            cur.execute("ALTER TABLE receipt_request_events ADD COLUMN origin TEXT")

        conn.commit()

ensure_receipt_request_events_schema()

def get_origin_by_message_sid(message_sid: str) -> str | None:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT origin FROM receipt_request_events
            WHERE message_sid = ?
            ORDER BY id DESC LIMIT 1
        """, (message_sid,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def _log_receipt_request_event(
    tenant: str,
    cuil: str,
    period: str,
    to_whatsapp: str,
    source: str,
    result: str,
    message_sid: str | None = None,
    origin: str | None = None,
):
    created_at = int(time.time())
    origin = origin or source

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()

        # 1) Asegurar tabla (forma mínima)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS receipt_request_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        )
        """)

        # 2) Ver columnas actuales
        cols = [r[1] for r in cur.execute("PRAGMA table_info(receipt_request_events)").fetchall()]

        # 3) Intentar agregar columnas que usamos (si faltan)
        #    (SQLite permite ALTER TABLE ADD COLUMN)
        def _add_col(colname: str, coltype: str):
            nonlocal cols
            if colname not in cols:
                cur.execute(f"ALTER TABLE receipt_request_events ADD COLUMN {colname} {coltype}")
                cols.append(colname)

        # Nombres que pueden variar según versiones anteriores:
        # - whatsapp vs to_whatsapp
        # Vamos a soportar ambos.
        _add_col("tenant", "TEXT")
        _add_col("cuil", "TEXT")
        _add_col("period", "TEXT")
        _add_col("source", "TEXT")
        _add_col("result", "TEXT")
        _add_col("message_sid", "TEXT")
        _add_col("created_at", "INTEGER")
        _add_col("origin", "TEXT")

        # columna whatsapp (si tu tabla vieja tenía to_whatsapp, la dejamos también)
        if "whatsapp" not in cols and "to_whatsapp" not in cols:
            _add_col("whatsapp", "TEXT")

        # 4) Armar INSERT solo con columnas que existen
        data = {
            "tenant": tenant,
            "cuil": cuil,
            "period": period,
            "source": source,
            "result": result,
            "message_sid": message_sid,
            "created_at": created_at,
            "origin": origin,
        }

        # elegir columna destino para el whatsapp
        if "whatsapp" in cols:
            data["whatsapp"] = to_whatsapp
        elif "to_whatsapp" in cols:
            data["to_whatsapp"] = to_whatsapp  # por si tu tabla vieja usa este nombre

        insert_cols = [k for k in data.keys() if k in cols]
        placeholders = ",".join(["?"] * len(insert_cols))
        sql = f"INSERT INTO receipt_request_events ({','.join(insert_cols)}) VALUES ({placeholders})"

        cur.execute(sql, tuple(data[k] for k in insert_cols))
        conn.commit()

    except Exception as e:
        # Fallback ultra defensivo: no romper producción por logging
        print("WARN: _log_receipt_request_event failed:", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()

# =========================
# DB Initialization / Migration
def get_receipt_event_origin_by_sid(message_sid: str) -> str | None:
    if not message_sid:
        return None
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        # origin puede ser NULL en filas viejas
        cur.execute("""
            SELECT origin, source
            FROM receipt_request_events
            WHERE message_sid = ?
            ORDER BY id DESC
            LIMIT 1
        """, (message_sid,))
        row = cur.fetchone()
        if not row:
            return None
        return row[0] or row[1]  # origin si existe, si no source
    finally:
        conn.close()



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
    # pending_views
    # =========
    cur.execute("""
      CREATE TABLE IF NOT EXISTS pending_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        to_whatsapp TEXT NOT NULL,
        tenant TEXT NOT NULL,
        cuil TEXT NOT NULL,
        period TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        step TEXT DEFAULT 'READY',
        dni_attempts INTEGER DEFAULT 0,
        UNIQUE(to_whatsapp, tenant, cuil, period)
      );
    """)
    _try_alter(cur, "ALTER TABLE pending_views ADD COLUMN step TEXT;")
    _try_alter(cur, "ALTER TABLE pending_views ADD COLUMN dni_attempts INTEGER;")

    # =========
    # recibo_estado
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
    # message_status
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
        kind TEXT,
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
    for col, typ in [
        ("to_whatsapp","TEXT"),("tenant","TEXT"),("cuil","TEXT"),("period","TEXT"),
        ("nombre","TEXT"),("kind","TEXT"),("created_at","INTEGER"),
        ("last_status","TEXT"),("last_status_at","INTEGER"),
        ("delivered_at","INTEGER"),("read_at","INTEGER"),("failed_at","INTEGER"),
        ("error_code","TEXT"),("error_message","TEXT"),
    ]:
        _try_alter(cur, f"ALTER TABLE message_status ADD COLUMN {col} {typ};")

    # =========
    # sent_pdfs
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
    # verifications (ya la usás)
    # =========
    cur.execute("""
      CREATE TABLE IF NOT EXISTS verifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant TEXT NOT NULL,
        cuil TEXT NOT NULL,
        to_whatsapp TEXT NOT NULL,
        nombre TEXT,
        dni_hash TEXT,
        dni_last4 TEXT,
        verified_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE(tenant, cuil, to_whatsapp)
      );
    """)
    _try_alter(cur, "ALTER TABLE verifications ADD COLUMN nombre TEXT;")
    _try_alter(cur, "ALTER TABLE verifications ADD COLUMN dni_hash TEXT;")
    _try_alter(cur, "ALTER TABLE verifications ADD COLUMN dni_last4 TEXT;")
    _try_alter(cur, "ALTER TABLE verifications ADD COLUMN verified_at INTEGER;")
    _try_alter(cur, "ALTER TABLE verifications ADD COLUMN updated_at INTEGER;")

    # =========
    # ✅ NUEVO: receipt_requests (contador por período)
    # =========
    cur.execute("""
      CREATE TABLE IF NOT EXISTS receipt_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant TEXT NOT NULL,
        cuil TEXT NOT NULL,
        period TEXT NOT NULL,
        to_whatsapp TEXT NOT NULL,
        request_count INTEGER NOT NULL DEFAULT 0,
        first_requested_at INTEGER,
        last_requested_at INTEGER,
        UNIQUE(tenant, cuil, period, to_whatsapp)
      );
    """)

    # =========
    # ✅ NUEVO: receipt_request_events (log evento por evento)
    # =========
    cur.execute("""
      CREATE TABLE IF NOT EXISTS receipt_request_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant TEXT NOT NULL,
        cuil TEXT NOT NULL,
        period TEXT,
        to_whatsapp TEXT NOT NULL,
        source TEXT NOT NULL,     -- USER_TEXT / VIEW_NOW / DNI_OK / etc.
        result TEXT NOT NULL,     -- SENT / BLOCKED_LIMIT / ASK_DNI / NO_PDF / ERROR / NO_CONTEXT
        message_sid TEXT,
        created_at INTEGER NOT NULL
      );
    """)

    # índices útiles
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_pending_to_created ON pending_views(to_whatsapp, created_at);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_estado_key ON recibo_estado(tenant, cuil, period);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_msg_key ON message_status(tenant, cuil, period, kind);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_msg_sid ON message_status(message_sid);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_sentpdfs_sid ON sent_pdfs(message_sid);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_verif_tenant_cuil ON verifications(tenant, cuil);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_verif_tenant_wa ON verifications(tenant, to_whatsapp);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_rr_key ON receipt_requests(tenant, cuil, period, to_whatsapp);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_rre_key ON receipt_request_events(tenant, cuil, period, to_whatsapp, created_at);")

    conn.commit()
    conn.close()


init_db()

def ensure_verified_contacts_schema(cur):
    # si la tabla no existe, la creamos completa
    cur.execute("""
      CREATE TABLE IF NOT EXISTS verified_contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant TEXT NOT NULL,
        cuil TEXT NOT NULL,
        to_whatsapp TEXT NOT NULL,
        dni TEXT,
        nombre TEXT,
        verified_at INTEGER NOT NULL,
        UNIQUE(tenant, cuil, to_whatsapp)
      );
    """)

    # si ya existía vieja, agregamos columnas que falten
    _try_alter(cur, "ALTER TABLE verified_contacts ADD COLUMN dni TEXT;")
    _try_alter(cur, "ALTER TABLE verified_contacts ADD COLUMN nombre TEXT;")
    _try_alter(cur, "ALTER TABLE verified_contacts ADD COLUMN verified_at INTEGER;")

    # MUY IMPORTANTE:
    # si la tabla vieja no tenía UNIQUE(tenant,cuil,to_whatsapp),
    # ON CONFLICT(...) no va a funcionar. Creamos unique index equivalente.
    _try_alter(cur, """
      CREATE UNIQUE INDEX IF NOT EXISTS ux_verified_contacts_key
      ON verified_contacts(tenant, cuil, to_whatsapp);
    """)

    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_verified_tenant ON verified_contacts(tenant);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_verified_cuil ON verified_contacts(tenant, cuil);")


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

import hashlib

def _hash_dni(dni: str) -> tuple[str, str]:
    dni_digits = "".join(ch for ch in (dni or "") if ch.isdigit())
    last4 = dni_digits[-4:] if len(dni_digits) >= 4 else dni_digits
    h = hashlib.sha256(dni_digits.encode("utf-8")).hexdigest() if dni_digits else ""
    return h, last4

def is_verified(tenant: str, cuil: str, to_whatsapp: str) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
      SELECT 1 FROM verifications
      WHERE tenant=? AND cuil=? AND to_whatsapp=?
      LIMIT 1
    """, (tenant, cuil, to_whatsapp))
    ok = cur.fetchone() is not None
    conn.close()
    return ok

def upsert_verification(tenant: str, cuil: str, to_whatsapp: str, dni: str | None = None):
    now = int(time.time())
    dni_hash, dni_last4 = _hash_dni(dni or "")
    conn = get_db_connection()
    cur = conn.cursor()

    # Upsert “manual” compatible sin depender de UNIQUE en DB vieja
    cur.execute("""
      SELECT id FROM verifications
      WHERE tenant=? AND cuil=? AND to_whatsapp=?
      LIMIT 1
    """, (tenant, cuil, to_whatsapp))
    row = cur.fetchone()

    if row:
        cur.execute("""
          UPDATE verifications
          SET dni_hash=?, dni_last4=?, updated_at=?
          WHERE id=?
        """, (dni_hash or None, dni_last4 or None, now, row[0]))
    else:
        cur.execute("""
          INSERT INTO verifications (tenant, cuil, to_whatsapp, dni_hash, dni_last4, verified_at, updated_at)
          VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (tenant, cuil, to_whatsapp, dni_hash or None, dni_last4 or None, now, now))

    conn.commit()
    conn.close()

def delete_verification(tenant: str, cuil: str, to_whatsapp: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
      DELETE FROM verifications
      WHERE tenant=? AND cuil=? AND to_whatsapp=?
    """, (tenant, cuil, to_whatsapp))
    conn.commit()
    conn.close()

def get_verifications_rows(tenant: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            cuil,
            to_whatsapp,
            COALESCE(nombre,'') AS nombre,
            COALESCE(dni_last4,'') AS dni_last4,
            verified_at,
            updated_at
        FROM verifications
        WHERE tenant=?
        ORDER BY updated_at DESC, verified_at DESC
        LIMIT 1000
    """, (tenant,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def is_verified_contact(tenant: str, cuil: str, to_whatsapp: str) -> bool:
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT 1
        FROM verifications
        WHERE tenant=? AND cuil=? AND to_whatsapp=?
        LIMIT 1
    """, (tenant, cuil, to_whatsapp))

    ok = cur.fetchone() is not None
    conn.close()
    return ok

import hashlib

def _dni_hash(dni: str, tenant: str) -> str:
    # Salt simple por tenant (podés cambiar por SECRET_KEY si tenés)
    raw = f"{tenant}|{dni}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def set_verified_contact(tenant: str, cuil: str, to_whatsapp: str, dni: str, nombre: str = ""):
    now = int(time.time())
    dni = "".join(ch for ch in (dni or "") if ch.isdigit())
    dni_h = _dni_hash(dni, tenant)
    dni_last4 = dni[-4:] if len(dni) >= 4 else None

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO verifications (tenant, cuil, to_whatsapp, nombre, dni_hash, dni_last4, verified_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tenant, cuil, to_whatsapp)
        DO UPDATE SET
            nombre = CASE
                        WHEN excluded.nombre IS NOT NULL AND excluded.nombre <> '' THEN excluded.nombre
                        ELSE verifications.nombre
                     END,
            dni_hash=excluded.dni_hash,
            dni_last4=excluded.dni_last4,
            verified_at=excluded.verified_at,
            updated_at=excluded.updated_at
    """, (tenant, cuil, to_whatsapp, (nombre or "").strip(), dni_h, dni_last4, now, now))

    conn.commit()
    conn.close()
@app.get("/admin/reset_user")
def admin_reset_user():
    token = (request.args.get("token") or "").strip()
    tenant = (request.args.get("tenant") or "").strip().lower()
    cuil = (request.args.get("cuil") or "").strip()
    whatsapp = (request.args.get("whatsapp") or "").strip()  # opcional

    if not token or token != ADMIN_TOKEN:
        return Response("Unauthorized", status=401)
    if not tenant or not cuil:
        return Response("Missing tenant/cuil", status=400)

    conn = get_db_connection()
    cur = conn.cursor()

    def _safe_exec(sql, params=()):
        try:
            cur.execute(sql, params)
            return cur.rowcount
        except Exception as e:
            print("WARN reset_user:", e, "| SQL:", sql)
            return 0

    deleted = {}

    # 1) pending queue
    if whatsapp:
        deleted["pending_views"] = _safe_exec(
            "DELETE FROM pending_views WHERE tenant=? AND cuil=? AND to_whatsapp=?",
            (tenant, cuil, whatsapp)
        )
    else:
        deleted["pending_views"] = _safe_exec(
            "DELETE FROM pending_views WHERE tenant=? AND cuil=?",
            (tenant, cuil)
        )

    # 2) PDFs enviados (para que no dispare SIGN después)
    if whatsapp:
        deleted["sent_pdfs"] = _safe_exec(
            "DELETE FROM sent_pdfs WHERE tenant=? AND cuil=? AND to_whatsapp=?",
            (tenant, cuil, whatsapp)
        )
    else:
        deleted["sent_pdfs"] = _safe_exec(
            "DELETE FROM sent_pdfs WHERE tenant=? AND cuil=?",
            (tenant, cuil)
        )

    # 3) Tracking general (plantillas/pdf/sign) — acá suele estar todo lo que ves en el Excel
    if whatsapp:
        deleted["message_status"] = _safe_exec(
            "DELETE FROM message_status WHERE tenant=? AND cuil=? AND to_whatsapp=?",
            (tenant, cuil, whatsapp)
        )
    else:
        deleted["message_status"] = _safe_exec(
            "DELETE FROM message_status WHERE tenant=? AND cuil=?",
            (tenant, cuil)
        )

    # 4) Eventos/contador auxiliar
    # soporta columnas whatsapp o to_whatsapp según schema viejo/nuevo
    deleted["receipt_request_events"] = 0
    if whatsapp:
        deleted["receipt_request_events"] += _safe_exec(
            "DELETE FROM receipt_request_events WHERE tenant=? AND cuil=? AND whatsapp=?",
            (tenant, cuil, whatsapp)
        )
        deleted["receipt_request_events"] += _safe_exec(
            "DELETE FROM receipt_request_events WHERE tenant=? AND cuil=? AND to_whatsapp=?",
            (tenant, cuil, whatsapp)
        )
    else:
        deleted["receipt_request_events"] += _safe_exec(
            "DELETE FROM receipt_request_events WHERE tenant=? AND cuil=?",
            (tenant, cuil)
        )

    # 5) Estado firmado/observado si existe (opcional pero para testing sirve)
    deleted["recibo_estado"] = _safe_exec(
        "DELETE FROM recibo_estado WHERE tenant=? AND cuil=?",
        (tenant, cuil)
    )

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "tenant": tenant,
        "cuil": cuil,
        "whatsapp": whatsapp or None,
        "deleted": deleted
    })

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

def get_nombre_for_cuil(tenant: str, cuil: str) -> str:
    try:
        df = get_envios_df_for_tenant(tenant)
        if df is None or df.empty:
            return ""
        df.columns = [str(c).strip().lower() for c in df.columns]

        # columnas posibles
        c_nombre = None
        for n in ("nombre", "name", "empleado", "persona"):
            if n in df.columns:
                c_nombre = n
                break

        c_arch = None
        for n in ("archivo", "cuil", "archivo_norm"):
            if n in df.columns:
                c_arch = n
                break

        if not c_nombre or not c_arch:
            return ""

        # normalizar cuil en df y comparar
        def norm(x):
            s = str(x or "").strip().replace(".pdf","")
            try:
                s = strip_pdf(s)
            except Exception:
                pass
            return s

        target = norm(cuil)
        df["_cuil_norm"] = df[c_arch].apply(norm)

        row = df[df["_cuil_norm"] == target]
        if row.empty:
            return ""
        return str(row.iloc[0][c_nombre] or "").strip()
    except Exception:
        return ""

def _db_fetchall_dict(sql, params=()):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def get_recibo_estado_rows(tenant: str, period_label: str = ""):
    if period_label:
        return _db_fetchall_dict(
            "SELECT tenant,cuil,period,estado,updated_at FROM recibo_estado WHERE tenant=? AND period=?",
            (tenant, period_label)
        )
    return _db_fetchall_dict(
        "SELECT tenant,cuil,period,estado,updated_at FROM recibo_estado WHERE tenant=?",
        (tenant,)
    )

def get_verifications_rows_for_report(tenant: str):
    return _db_fetchall_dict(
        "SELECT tenant,cuil,to_whatsapp,nombre,verified_at FROM verifications WHERE tenant=?",
        (tenant,)
    )

def get_message_status_rows(tenant: str, period_label: str = ""):
    if period_label:
        return _db_fetchall_dict(
            """
            SELECT tenant,cuil,period,to_whatsapp,nombre,kind,created_at,last_status,last_status_at,
                   delivered_at,read_at,failed_at,error_code,error_message
            FROM message_status
            WHERE tenant=? AND period=?
            """,
            (tenant, period_label)
        )
    return _db_fetchall_dict(
        """
        SELECT tenant,cuil,period,to_whatsapp,nombre,kind,created_at,last_status,last_status_at,
               delivered_at,read_at,failed_at,error_code,error_message
        FROM message_status
        WHERE tenant=?
        """,
        (tenant,)
    )

from io import BytesIO
import time
import re
from flask import Response, request, send_file
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

def period_to_label(p: str) -> str:
    p = (p or "").strip()
    if not p:
        return ""
    # admite 01/2026 o 01-2026
    p = p.replace("-", "/")
    # normaliza 1/2026 -> 01/2026
    m = re.match(r"^(\d{1,2})/(\d{4})$", p)
    if not m:
        return p
    mm = int(m.group(1))
    yyyy = m.group(2)
    return f"{mm:02d}/{yyyy}"

def ts_str(ts):
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except Exception:
        return str(ts)

def _autosize_ws(ws):
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            v = "" if cell.value is None else str(cell.value)
            if len(v) > max_len:
                max_len = len(v)
        ws.column_dimensions[col_letter].width = min(max_len + 2, 55)


from io import BytesIO
import time
import pandas as pd
from flask import request, Response, send_file

def norm_period_label(p: str) -> str:
    """
    Normaliza a 'MM/AAAA'
    Acepta: 'MM/AAAA', 'MM-AAAA', 'AAAA-MM', 'AAAA/MM'
    """
    p = (p or "").strip()
    if not p:
        return ""
    p = p.replace("\\", "/").replace("-", "/")
    parts = [x for x in p.split("/") if x.strip()]
    if len(parts) != 2:
        return ""

    a, b = parts[0].zfill(2), parts[1]
    # si viene AAAA/MM
    if len(a) == 4:
        yyyy = a
        mm = b.zfill(2)
    else:
        mm = a.zfill(2)
        yyyy = b

    if not (mm.isdigit() and yyyy.isdigit()):
        return ""
    if not (1 <= int(mm) <= 12):
        return ""
    if len(yyyy) != 4:
        return ""

    return f"{mm}/{yyyy}"

def generate_pdf_report_v2(tenant: str, period_filter: str = "") -> BytesIO:
    """
    Genera un PDF simple con el mismo contenido que el XLSX (incluye pedidos_recibo).
    """
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import cm

    # reutilizamos el armado del XLSX para no duplicar lógica:
    # acá armamos una "vista" leyendo directamente desde DB similar a generate_excel_report_v2
    tenant = (tenant or "").strip().lower()
    period_filter = norm_period_label(period_filter)

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT
            to_whatsapp, tenant, cuil, period, nombre, kind,
            created_at, delivered_at, read_at, failed_at
        FROM message_status
        WHERE tenant = ?
    """, (tenant,))
    msg_rows = cur.fetchall()

    cur.execute("""
        SELECT tenant, cuil, period, estado, updated_at
        FROM recibo_estado
        WHERE tenant = ?
    """, (tenant,))
    estado_rows = cur.fetchall()

    cur.execute("""
        SELECT tenant,cuil,period,to_whatsapp,request_count,last_requested_at
        FROM receipt_requests
        WHERE tenant = ?
    """, (tenant,))
    rr_rows = cur.fetchall()

    conn.close()

    estado_map = {}
    for r in estado_rows:
        c = (r["cuil"] or "").strip()
        p = norm_period_label(r["period"] or "")
        if c and p:
            estado_map[(c, p)] = {"estado": (r["estado"] or "").strip(), "ts": r["updated_at"]}

    rr_map = {}
    for r in rr_rows:
        c = (r["cuil"] or "").strip()
        p = norm_period_label(r["period"] or "")
        w = (r["to_whatsapp"] or "").strip()
        if c and p and w:
            rr_map[(w, c, p)] = {"count": int(r["request_count"] or 0), "last": r["last_requested_at"]}

    # agregación: una fila por (whatsapp,period)
    agg = {}
    for row in msg_rows:
        wpp = (row["to_whatsapp"] or "").strip()
        if not wpp:
            continue
        cuil = (row["cuil"] or "").strip()
        nombre = (row["nombre"] or "").strip()
        per = norm_period_label((row["period"] or "").strip())
        if period_filter and per != period_filter:
            continue
        if not per:
            continue

        k = (wpp, per)
        if k not in agg:
            agg[k] = {
                "Periodo": per,
                "Nombre": nombre,
                "CUIL": cuil,
                "WhatsApp": wpp,
                "PDF_enviado": "",
                "PDF_entregado": "",
                "PDF_leido": "",
                "Respuesta": "",
                "Pedidos": 0,
                "Ultimo_pedido": "",
            }

        kind = (row["kind"] or "").strip().lower()
        if kind in ("pdf", "media"):
            # guardamos los timestamps más útiles
            if row["created_at"]:
                agg[k]["PDF_enviado"] = ts_to_str(row["created_at"])
            if row["delivered_at"]:
                agg[k]["PDF_entregado"] = ts_to_str(row["delivered_at"])
            if row["read_at"]:
                agg[k]["PDF_leido"] = ts_to_str(row["read_at"])

    # mezclar estado + pedidos
    for k, rec in agg.items():
        cuil = rec.get("CUIL","")
        per = rec.get("Periodo","")
        st = estado_map.get((cuil, per))
        if st:
            rec["Respuesta"] = st.get("estado","")

        rr = rr_map.get((rec["WhatsApp"], cuil, per))
        if rr:
            rec["Pedidos"] = rr["count"]
            rec["Ultimo_pedido"] = ts_to_str(rr["last"])

    rows = list(agg.values())
    rows.sort(key=lambda r: (r.get("Periodo",""), r.get("Nombre",""), r.get("WhatsApp","")))

    # PDF
    out = BytesIO()
    c = canvas.Canvas(out, pagesize=landscape(A4))
    width, height = landscape(A4)

    title = f"Reporte Recibos - {tenant} - {(period_filter if period_filter else 'TODOS')}"
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1.2*cm, height - 1.2*cm, title)

    headers = ["Periodo","Nombre","CUIL","WhatsApp","PDF_enviado","PDF_entregado","PDF_leido","Respuesta","Pedidos","Ultimo_pedido"]

    x0 = 1.2*cm
    y = height - 2.2*cm
    line_h = 0.6*cm

    # anchos aproximados
    col_w = [2.2*cm, 5.0*cm, 3.5*cm, 5.2*cm, 3.2*cm, 3.2*cm, 3.0*cm, 3.0*cm, 1.8*cm, 3.5*cm]

    def draw_row(vals, bold=False):
        nonlocal y
        if y < 1.2*cm:
            c.showPage()
            c.setFont("Helvetica-Bold", 14)
            c.drawString(1.2*cm, height - 1.2*cm, title)
            y = height - 2.2*cm

        c.setFont("Helvetica-Bold" if bold else "Helvetica", 8.5)
        x = x0
        for i, v in enumerate(vals):
            txt = str(v or "")
            c.drawString(x, y, txt[:60])
            x += col_w[i]
        y -= line_h

    draw_row(headers, bold=True)
    c.setLineWidth(0.5)
    c.line(x0, y + 0.2*cm, width - 1.2*cm, y + 0.2*cm)
    y -= 0.2*cm

    for r in rows:
        draw_row([
            r.get("Periodo",""),
            r.get("Nombre",""),
            r.get("CUIL",""),
            r.get("WhatsApp",""),
            r.get("PDF_enviado",""),
            r.get("PDF_entregado",""),
            r.get("PDF_leido",""),
            r.get("Respuesta",""),
            r.get("Pedidos",""),
            r.get("Ultimo_pedido",""),
        ])

    c.showPage()
    c.save()

    out.seek(0)
    return out


@app.get("/admin/report_recibos.pdf")
def admin_report_recibos_pdf():
    token = _get_admin_token_from_request()
    tenant = (request.args.get("tenant") or "").strip().lower()
    period = (request.args.get("period") or "").strip()

    if not tenant:
        return Response("Falta tenant", status=400)

    buf = generate_pdf_report_v2(tenant, period_filter=period)

    filename = f"reporte_recibos_{tenant}_{(norm_period_label(period).replace('/','-') if period else 'todos')}.pdf"
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/pdf"
    )


@app.get("/admin/report_recibos.xlsx")
def admin_report_recibos_xlsx():
    token = _get_admin_token_from_request()
    tenant = (request.args.get("tenant") or "").strip().lower()
    period = (request.args.get("period") or "").strip()  # MM/AAAA desde el panel (o vacío)

    if not tenant:
        return Response("Falta tenant", status=400)

    buf = generate_excel_report_v2(tenant, period_filter=period)

    filename = f"reporte_recibos_{tenant}_{(norm_period_label(period).replace('/','-') if period else 'todos')}.xlsx"
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )



def list_verified_contacts(tenant: str, q: str = ""):
    conn = get_db_connection()
    cur = conn.cursor()

    q = (q or "").strip().lower()
    params = [tenant]

    sql = """
      SELECT id, tenant, cuil, to_whatsapp, nombre, dni, verified_at
      FROM verified_contacts
      WHERE tenant = ?
    """

    if q:
        sql += " AND (lower(cuil) LIKE ? OR lower(to_whatsapp) LIKE ? OR lower(ifnull(nombre,'')) LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like]

    sql += " ORDER BY verified_at DESC LIMIT 200"

    cur.execute(sql, params)
    rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
    conn.close()
    return rows


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

from flask import send_file
import pandas as pd
import io

from datetime import datetime, timezone

def ts_str(ts: int | None) -> str:
    if not ts:
        return ""
    try:
        # Render usa UTC en logs; acá lo mostramos simple en formato legible
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""

from flask import send_file
import io

@app.get("/admin/verifications_template.xlsx")
@admin_required
def admin_verifications_template_xlsx():
    output = io.BytesIO()
    import pandas as pd

    df = pd.DataFrame(columns=["cuil", "whatsapp", "dni", "nombre"])
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="verificaciones")

    output.seek(0)
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="verifications_template.xlsx",
    )

import time

def _digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())

def normalize_period_for_drive(period: str) -> str:
    """
    Convierte 'MM/AAAA' -> 'MM-AAAA'
    Acepta también 'MM-AAAA' y lo deja igual.
    """
    if not period:
        return ""
    p = period.strip()
    if "/" in p:
        return p.replace("/", "-")
    return p


def normalize_whatsapp(raw: str) -> str | None:
    """
    Recibe:
      - 'whatsapp:+54911...'
      - '+54911...'
      - '11 3622-2572'
      - '1136222572'
    Devuelve:
      - 'whatsapp:+54XXXXXXXXXXX'
    """
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("whatsapp:"):
        s = s.replace("whatsapp:", "").strip()

    d = _digits(s)
    if not d:
        return None

    # Argentina default: si no empieza con 54, lo agregamos
    if not d.startswith("54"):
        d = "54" + d

    return f"whatsapp:+{d}"

def ts_str(ts: int | None) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%d/%m/%Y %H:%M", time.gmtime(int(ts)))
    except Exception:
        return str(ts)


@app.post("/admin/verifications_import")
@admin_required
def admin_verifications_import():
    token = _get_admin_token_from_request()
    tenant = (request.form.get("tenant") or "").strip().lower()

    f = request.files.get("file")
    if not f:
        return Response("Falta archivo", status=400)

    import pandas as pd
    df = pd.read_excel(f)
    if df is None or df.empty:
        return redirect(f"/admin/panel?tenant={tenant}&token={token}&msg=verif_import_empty")

    df.columns = [str(c).strip().lower() for c in df.columns]

    def pick(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    c_cuil = pick("cuil", "archivo", "archivo_norm")
    c_wpp  = pick("whatsapp", "telefono", "tel", "celular", "numero")
    c_nombre = pick("nombre", "name", "empleado", "persona")

    if not c_cuil or not c_wpp:
        return Response("El excel debe tener columnas cuil y whatsapp/telefono", status=400)

    rows = df.to_dict(orient="records")
    now = int(time.time())

    conn = get_db_connection()
    cur = conn.cursor()

    ok = 0
    skipped = 0

    for r in rows:
        cuil_raw = str(r.get(c_cuil, "")).strip()
        wpp_raw  = str(r.get(c_wpp, "")).strip()
        raw_nombre = r.get(c_nombre) if c_nombre else None
        nombre = ""
        if raw_nombre is not None and str(raw_nombre).strip().lower() != "nan":
            nombre = str(raw_nombre).strip()

        if not cuil_raw or not wpp_raw:
            skipped += 1
            continue

        cuil = strip_pdf(cuil_raw).strip()
        to_whatsapp = normalize_whatsapp(wpp_raw)
        if not to_whatsapp:
            skipped += 1
            continue

        # Import: marca como verificado "sin DNI" (solo vínculo número<->cuil)
        cur.execute("""
            INSERT INTO verifications (tenant, cuil, to_whatsapp, nombre, dni_hash, dni_last4, verified_at, updated_at)
            VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)
            ON CONFLICT(tenant, cuil, to_whatsapp)
            DO UPDATE SET
                nombre=excluded.nombre,
                updated_at=excluded.updated_at
        """, (tenant, cuil, to_whatsapp, nombre, now, now))

        ok += 1

    conn.commit()
    conn.close()

    return redirect(f"/admin/panel?tenant={tenant}&token={token}&msg=verif_import_ok&n={ok}&skipped={skipped}")


@app.post("/admin/verifications_update")
@admin_required
def admin_verifications_update():
    token = _get_admin_token_from_request()
    tenant = (request.form.get("tenant") or "").strip().lower()
    return redirect(f"/admin/panel?tenant={tenant}&token={token}&msg=verif_update_disabled")


@app.post("/admin/verifications_delete")
@admin_required
def admin_verifications_delete():
    token = _get_admin_token_from_request()
    tenant = (request.form.get("tenant") or "").strip().lower()
    cuil = (request.form.get("cuil") or "").strip()
    to_whatsapp = (request.form.get("to_whatsapp") or "").strip()

    if not (tenant and cuil and to_whatsapp):
        return Response("Faltan parámetros", status=400)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
      DELETE FROM verified_contacts
      WHERE tenant=? AND cuil=? AND to_whatsapp=?
    """, (tenant, cuil, to_whatsapp))
    conn.commit()
    conn.close()

    return redirect(f"/admin/panel?tenant={tenant}&token={token}&msg=verif_deleted")

from io import BytesIO
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from datetime import datetime, timezone
import datetime as dt

def ts_to_str(ts) -> str:
    if ts is None:
        return ""
    try:
        s = str(ts).strip()
        if s == "" or s.lower() == "nan":
            return ""

        ts_f = float(s)

        # ms -> s
        if ts_f > 10_000_000_000:
            ts_f = ts_f / 1000.0

        return dt.datetime.fromtimestamp(int(ts_f)).strftime("%d/%m/%Y %H:%M:%S")
    except Exception as e:
        print("ts_to_str ERROR:", ts, repr(e))
        return ""






def generate_excel_report_v2(tenant: str, period_filter: str = "") -> BytesIO:
    tenant = (tenant or "").strip().lower()
    period_filter = norm_period_label(period_filter)

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # message_status
    cur.execute("""
        SELECT
            to_whatsapp, tenant, cuil, period, nombre, kind,
            created_at, delivered_at, read_at, failed_at
        FROM message_status
        WHERE tenant = ?
    """, (tenant,))
    msg_rows = cur.fetchall()

    # pending_views (último period por (whatsapp,cuil))
    cur.execute("""
        SELECT to_whatsapp, tenant, cuil, period, MAX(created_at) as last_ts
        FROM pending_views
        WHERE tenant = ?
        GROUP BY to_whatsapp, cuil
    """, (tenant,))
    pv_rows = cur.fetchall()

    last_period_by_user_cuil = {}
    for r in pv_rows:
        w = (r["to_whatsapp"] or "").strip()
        c = (r["cuil"] or "").strip()
        p = norm_period_label(r["period"] or "")
        if w and c and p:
            last_period_by_user_cuil[(w, c)] = p

    # recibo_estado
    cur.execute("""
        SELECT tenant, cuil, period, estado, updated_at
        FROM recibo_estado
        WHERE tenant = ?
    """, (tenant,))
    estado_rows = cur.fetchall()

    # ✅ NUEVO: requests por período
    cur.execute("""
        SELECT tenant,cuil,period,to_whatsapp,request_count,last_requested_at
        FROM receipt_requests
        WHERE tenant = ?
    """, (tenant,))
    rr_rows = cur.fetchall()

    conn.close()

    estado_map = {}
    for r in estado_rows:
        c = (r["cuil"] or "").strip()
        p = norm_period_label(r["period"] or "")
        if c and p:
            estado_map[(c, p)] = {"estado": (r["estado"] or "").strip(), "ts": r["updated_at"]}

    rr_map = {}
    for r in rr_rows:
        c = (r["cuil"] or "").strip()
        p = norm_period_label(r["period"] or "")
        w = (r["to_whatsapp"] or "").strip()
        if c and p and w:
            rr_map[(w, c, p)] = {
                "count": int(r["request_count"] or 0),
                "last": r["last_requested_at"]
            }

    def key(whatsapp: str, period_norm: str):
        return ((whatsapp or "").strip(), (period_norm or "").strip())

    agg = {}

    for row in msg_rows:
        wpp = (row["to_whatsapp"] or "").strip()
        if not wpp:
            continue

        cuil = (row["cuil"] or "").strip()
        nombre = (row["nombre"] or "").strip()

        period_raw = (row["period"] or "").strip()
        period_norm = norm_period_label(period_raw)

        if not period_norm and wpp and cuil:
            period_norm = last_period_by_user_cuil.get((wpp, cuil), "")

        if period_filter and period_norm != period_filter:
            continue
        if not period_norm:
            continue

        k = key(wpp, period_norm)
        rec = agg.get(k)
        if not rec:
            rec = {
                "periodo": period_norm,
                "nombre": nombre,
                "cuil": cuil,
                "whatsapp": wpp,
                "plantilla_sent_at": None,
                "plantilla_delivered_at": None,
                "plantilla_read_at": None,
                "plantilla_failed_at": None,
                "pdf_sent_at": None,
                "pdf_delivered_at": None,
                "pdf_read_at": None,
                "pdf_failed_at": None,
                "respuesta_usuario": "",
                "respuesta_timestamp": None,
                "pedidos_recibo": 0,
                "ultimo_pedido": None,
            }
            agg[k] = rec

        if nombre and not rec["nombre"]:
            rec["nombre"] = nombre
        if cuil and not rec["cuil"]:
            rec["cuil"] = cuil

        kind = (row["kind"] or "").strip().lower()
        created_at = row["created_at"]
        delivered_at = row["delivered_at"]
        read_at = row["read_at"]
        failed_at = row["failed_at"]

        if kind == "template":
            if created_at and (not rec["plantilla_sent_at"] or created_at < rec["plantilla_sent_at"]):
                rec["plantilla_sent_at"] = created_at
            if delivered_at:
                rec["plantilla_delivered_at"] = delivered_at
            if read_at:
                rec["plantilla_read_at"] = read_at
            if failed_at:
                rec["plantilla_failed_at"] = failed_at

        elif kind in ("pdf", "media"):
            if created_at and (not rec["pdf_sent_at"] or created_at < rec["pdf_sent_at"]):
                rec["pdf_sent_at"] = created_at
            if delivered_at:
                rec["pdf_delivered_at"] = delivered_at
            if read_at:
                rec["pdf_read_at"] = read_at
            if failed_at:
                rec["pdf_failed_at"] = failed_at

    # mezclar recibo_estado
    for (wpp, per), rec in list(agg.items()):
        cuil = rec.get("cuil", "")
        st = estado_map.get((cuil, per))
        if st:
            rec["respuesta_usuario"] = st.get("estado", "") or ""
            rec["respuesta_timestamp"] = st.get("ts")

        rr = rr_map.get((wpp, cuil, per))
        if rr:
            rec["pedidos_recibo"] = rr["count"]
            rec["ultimo_pedido"] = rr["last"]

    # Excel
    wb = Workbook()
    ws = wb.active
    ws.title = "Recibos"

    headers = [
        "Periodo","Nombre","CUIL","WhatsApp",
        "Plantilla_enviada","Plantilla_entregada","Plantilla_leida","Plantilla_fallida",
        "PDF_enviado","PDF_entregado","PDF_leido","PDF_fallido",
        "Respuesta_usuario","Respuesta_timestamp",
        "Pedidos_recibo","Ultimo_pedido"
    ]
    ws.append(headers)

    items = list(agg.values())
    items.sort(key=lambda r: (r.get("periodo") or "", r.get("nombre") or "", r.get("whatsapp") or ""))

    for rec in items:
        ws.append([
            rec.get("periodo",""),
            rec.get("nombre",""),
            rec.get("cuil",""),
            rec.get("whatsapp",""),
            ts_to_str(rec.get("plantilla_sent_at")),
            ts_to_str(rec.get("plantilla_delivered_at")),
            ts_to_str(rec.get("plantilla_read_at")),
            ts_to_str(rec.get("plantilla_failed_at")),
            ts_to_str(rec.get("pdf_sent_at")),
            ts_to_str(rec.get("pdf_delivered_at")),
            ts_to_str(rec.get("pdf_read_at")),
            ts_to_str(rec.get("pdf_failed_at")),
            rec.get("respuesta_usuario",""),
            ts_to_str(rec.get("respuesta_timestamp")),
            int(rec.get("pedidos_recibo") or 0),
            ts_to_str(rec.get("ultimo_pedido")),
        ])

    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                max_len = max(max_len, len(str(cell.value or "")))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = max(10, max_len + 2)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


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
    # ---- Selector periodo reportes ----
    html.append("<hr>")
    html.append("<h3>Reportes</h3>")

    selected_period = (request.args.get("period") or "").strip()

    period_folders = list_tenant_period_folders(tenant)   # ['01-2026','12-2025',...]
    period_labels = []
    for p in period_folders:
        lbl = period_folder_to_label(p)   # '01/2026'
        if lbl:
            period_labels.append(lbl)

    if not selected_period and period_labels:
        selected_period = period_labels[0]   # default al más nuevo

    html.append("<form method='get' action='/admin/panel'>")
    html.append(f"<input type='hidden' name='token' value='{esc(token)}'>")
    html.append(f"<input type='hidden' name='tenant' value='{esc(tenant)}'>")

    html.append("<label>Período para reportes:</label> ")
    html.append("<select name='period'>")
    html.append("<option value=''>-- Todos / Sin filtro --</option>")
    for lbl in period_labels:
        sel = "selected" if lbl == selected_period else ""
        html.append(f"<option value='{esc(lbl)}' {sel}>{esc(lbl)}</option>")
    html.append("</select> ")
    html.append("<button type='submit'>Aplicar</button>")
    html.append("</form>")

    period_q = quote(selected_period, safe="")

    html.append(
        f"<p><a href='/admin/report_recibos.xlsx?tenant={esc(tenant)}&period={period_q}&token={esc(token)}'>"
        "📄 Descargar reporte de recibos</a></p>"
    )

    html.append(
        f"<p><a href='/admin/report_envios.csv?tenant={esc(tenant)}&token={esc(token)}'>"
        "📄 Envíos realizados (CSV)</a></p>"
    )

    html.append(
    f"<p><a href='/admin/report_recibos.pdf?tenant={esc(tenant)}&period={period_q}&token={esc(token)}'>"
    "🧾 Descargar informe PDF</a></p>"
)

    # ---- Verificaciones ----
    verifs = get_verifications_rows(tenant)
    html.append(f"<p>Registros: {len(verifs)}</p>")

    if verifs:
        html.append(f"""
        <form method="post" action="/admin/verifications_delete_bulk" onsubmit="return confirm('¿Borrar verificaciones seleccionadas?');">
        <input type="hidden" name="token" value="{esc(token)}">
        <input type="hidden" name="tenant" value="{esc(tenant)}">

        <table border='1' cellpadding='6' cellspacing='0'>
            <tr>
            <th></th>
            <th>CUIL</th>
            <th>Nombre</th>
            <th>WhatsApp</th>
            <th>Verificado</th>
            <th>Acción</th>
            </tr>
        """)

        for r in verifs[:500]:
            key = f"{r['cuil']}|{r['to_whatsapp']}"
            html.append("<tr>")
            html.append(f"<td><input type='checkbox' name='keys' value='{esc(key)}'></td>")
            html.append(f"<td>{esc(r['cuil'])}</td>")
            html.append(f"<td>{esc(r.get('nombre','') or '')}</td>")
            html.append(f"<td>{esc(r['to_whatsapp'])}</td>")
            html.append(f"<td>{esc(ts_str(r.get('verified_at')))}</td>")
            html.append("<td>")
            html.append(f"""
            <form method="post" action="/admin/verifications_delete" style="display:inline;" onsubmit="return confirm('¿Borrar verificación?');">
                <input type="hidden" name="token" value="{esc(token)}">
                <input type="hidden" name="tenant" value="{esc(tenant)}">
                <input type="hidden" name="cuil" value="{esc(r['cuil'])}">
                <input type="hidden" name="to_whatsapp" value="{esc(r['to_whatsapp'])}">
                <button type="submit">Borrar</button>
            </form>
            """)
            html.append("</td>")
            html.append("</tr>")

        html.append("""
        </table>
        <p style="margin-top:10px;">
            <button type="submit">🗑️ Borrar seleccionados</button>
        </p>
        </form>
        """)
    else:
        html.append("<p>No hay verificaciones cargadas.</p>")


    # Importar excel verificaciones
    html.append(f"""
    <form method="post" action="/admin/verifications_import" enctype="multipart/form-data" style="border:1px solid #ddd;padding:10px;border-radius:8px;">
    <input type="hidden" name="token" value="{esc(token)}">
    <input type="hidden" name="tenant" value="{esc(tenant)}">
    <div><b>Importar verificaciones</b></div>
    <input type="file" name="file" accept=".xlsx" required>
    <button type="submit">Importar</button>
    <div style="font-size:12px;color:#666;margin-top:6px;">
        Columnas requeridas: <code>cuil</code>, <code>whatsapp</code> (o teléfono). Opcional: <code>dni</code>.
    </div>
    </form>
    """)

    html.append("</div>")





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


@app.get("/admin/verifications.xlsx")
@admin_required
def admin_verifications_xlsx():
    token = _get_admin_token_from_request()
    tenant = (request.args.get("tenant") or "").strip().lower()
    if not tenant:
        return Response("Falta tenant", status=400)

    rows = get_verifications_rows(tenant)  # debe leer de verifications
    import pandas as pd
    df = pd.DataFrame(rows or [])

    # orden y nombres
    if not df.empty:
        cols = [c for c in ["tenant","cuil","to_whatsapp","nombre","verified_at","updated_at"] if c in df.columns]
        df = df[cols]

    import io
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="verifications")
    out.seek(0)

    resp = Response(out.read(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp.headers["Content-Disposition"] = f'attachment; filename="verifications_{tenant}.xlsx"'
    return resp

@app.post("/admin/verifications_delete_bulk")
@admin_required
def admin_verifications_delete_bulk():
    token = _get_admin_token_from_request()
    tenant = (request.form.get("tenant") or "").strip().lower()
    keys = request.form.getlist("keys")

    if not tenant or not keys:
        return redirect(f"/admin/panel?tenant={tenant}&token={token}&msg=verif_bulk_empty")

    conn = get_db_connection()
    cur = conn.cursor()

    n = 0
    for k in keys:
        try:
            cuil, to_whatsapp = k.split("|", 1)
        except Exception:
            continue
        cur.execute(
            "DELETE FROM verifications WHERE tenant=? AND cuil=? AND to_whatsapp=?",
            (tenant, cuil, to_whatsapp)
        )
        n += cur.rowcount

    conn.commit()
    conn.close()

    return redirect(f"/admin/panel?tenant={tenant}&token={token}&msg=verif_bulk_deleted&n={n}")



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


import re
from urllib.parse import quote

FOLDER_MIME = "application/vnd.google-apps.folder"

def period_folder_to_label(folder_name: str) -> str:
    # '01-2026' -> '01/2026'
    m = re.match(r"^(\d{2})-(\d{4})$", (folder_name or "").strip())
    if not m:
        return ""
    return f"{m.group(1)}/{m.group(2)}"

def normalize_period_label(s: str) -> str:
    # '1/2026' -> '01/2026' ; '01/2026' queda igual
    m = re.match(r"^\s*(\d{1,2})\s*/\s*(\d{4})\s*$", (s or "").strip())
    if not m:
        return (s or "").strip()
    mm = int(m.group(1))
    yyyy = int(m.group(2))
    return f"{mm:02d}/{yyyy:04d}"

import re

import re

FOLDER_MIME = "application/vnd.google-apps.folder"

def list_tenant_period_folders(tenant_slug: str) -> list[str]:
    """
    Devuelve nombres de carpetas período existentes en Drive para el tenant.
    Formato carpeta esperado: 'MM-AAAA'
    """
    t = get_tenant(tenant_slug)
    if not t:
        return []

    root_id = (t.get("drive_root_id") or t.get("recibos_root_id") or "").strip()
    if not root_id:
        return []

    service = drive_service()

    children = _drive_list_children(
        service,
        parent_id=root_id,
        mime_type=FOLDER_MIME,
        page_size=500
    )

    names = [
        c.get("name", "")
        for c in children
        if re.match(r"^\d{2}-\d{4}$", (c.get("name", "") or "").strip())
    ]

    # ordenar desc por (yyyy, mm)
    def key(name: str):
        mm, yyyy = name.split("-")
        return (int(yyyy), int(mm))

    names.sort(key=key, reverse=True)
    return names


def list_tenant_period_labels(service, tenant: str) -> list[str]:
    # ['01-2026', '12-2025'] -> ['01/2026','12/2025']
    folders = list_tenant_period_folders(tenant)
    labels = []
    for f in folders:
        lbl = period_folder_to_label(f)
        if lbl:
            labels.append(lbl)
    return labels



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
def twilio_webhook():
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

from io import BytesIO
from flask import send_file
import sqlite3
import pandas as pd
import time

def _db_row_to_dict(r):
    # r puede ser sqlite3.Row o tupla
    if r is None:
        return None
    if isinstance(r, sqlite3.Row):
        return dict(r)
    # fallback: si fuera tupla (no debería si usás row_factory)
    return {str(i): v for i, v in enumerate(r)}

def _get_last_msg_status(tenant: str, cuil: str, period: str, kind: str):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT *
        FROM message_status
        WHERE tenant=? AND cuil=? AND period=? AND kind=?
        ORDER BY COALESCE(created_at, 0) DESC, id DESC
        LIMIT 1
    """, (tenant, cuil, period, kind))
    row = cur.fetchone()
    conn.close()
    return _db_row_to_dict(row)

def _is_verified_link(tenant: str, cuil: str, to_whatsapp: str) -> bool:
    # usa tu tabla UNICA verifications
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT 1
        FROM verifications
        WHERE tenant=? AND cuil=? AND to_whatsapp=?
        LIMIT 1
    """, (tenant, cuil, to_whatsapp))
    ok = cur.fetchone() is not None
    conn.close()
    return ok

def _get_verif_nombre(tenant: str, cuil: str, to_whatsapp: str) -> str:
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT nombre
        FROM verifications
        WHERE tenant=? AND cuil=? AND to_whatsapp=?
        LIMIT 1
    """, (tenant, cuil, to_whatsapp))
    row = cur.fetchone()
    conn.close()
    return (row["nombre"] if row and row["nombre"] else "") if row else ""



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

def twiml(msg: str):
    return Response(
        f"<Response><Message>{msg}</Message></Response>",
        mimetype="application/xml",
        status=200
    )

import os, json
from twilio.rest import Client

WHATSAPP_MENU_CONTENT_SID = os.getenv("WHATSAPP_MENU_CONTENT_SID", "")

def send_whatsapp_menu_template(to_whatsapp: str, nombre: str = "") -> str | None:
    """
    Envía la plantilla del menú (Quick Reply) vía Content API.
    Devuelve Message SID o None.
    """
    if not WHATSAPP_MENU_CONTENT_SID:
        print("ERROR: falta WHATSAPP_MENU_CONTENT_SID")
        return None

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    # Variables de plantilla: {{1}} -> "1"
    vars_ = {"1": (nombre or "").strip()}

    msg = client.messages.create(
        to=to_whatsapp,
        from_=TWILIO_WHATSAPP_FROM,
        content_sid=WHATSAPP_MENU_CONTENT_SID,
        content_variables=json.dumps(vars_)
    )
    return msg.sid


TWILIO_SIGN_TEMPLATE_SID = os.environ.get("TWILIO_SIGN_TEMPLATE_SID", "").strip()

@app.post("/twilio/inbound")
def twilio_inbound():
    from_whatsapp = (request.form.get("From") or "").strip()
    button = (request.form.get("ButtonPayload") or "").strip()
    body = (request.form.get("Body") or "").strip()

    print("INBOUND:", from_whatsapp, "ButtonPayload:", button, "Body:", body)

    def _is_receipt_request_text(t: str) -> bool:
        t = (t or "").strip().lower()
        if not t:
            return False
        if t.startswith("recibo"):
            return True
        return t in ("pdf", "reenviar", "reenviar recibo", "reenvio", "reenvío", "pedir recibo", "quiero mi recibo")

    pending = get_latest_pending_view(from_whatsapp)
    print("PENDING:", pending)
    # =========================
    # REGLA: cualquier texto (sin botón) dispara menú,
    # EXCEPTO cuando estamos esperando DNI o selección de períodos
    # =========================
    if not button:
        step_now = (pending.get("step") or "READY").upper() if pending else "READY"
        body_norm = (body or "").strip()

        # AWAIT_DNI: dejamos pasar para que lo procese el bloque AWAIT_DNI
        if step_now == "AWAIT_DNI":
            pass

        # CHOOSE_PREVIOUS: si no es 1/2/3, devolvemos ayuda (no menú)
        elif step_now == "CHOOSE_PREVIOUS":
            if body_norm in ("1", "2", "3"):
                pass  # lo procesa el bloque de selección
            else:
                return twiml("🗂️ Respondé con 1, 2 o 3 para elegir un período anterior.")

        else:
            # Disparar menú ante cualquier texto
            sid = send_whatsapp_menu_template(
                from_whatsapp,
                nombre=(pending.get("nombre","") if isinstance(pending, dict) else "")
            )

    # =========================
    # SIN PENDING: o guía o reconstrucción para "RECIBO"
    # =========================
    if not pending:
        if not _is_receipt_request_text(body) and not button:
            # 🔥 En vez de contestar texto, mandamos la plantilla menú
            sid = send_whatsapp_menu_template(from_whatsapp, nombre="")
            if sid:
                return twiml("✅ Te envié el menú de recibos.")
            return twiml("👋 Hola. \nPara recibir tu recibo escribí: *RECIBO*.\nSi no recibiste el mensaje inicial, avisá a RRHH.")

        # si es pedido de recibo -> reconstruimos contexto desde último envío
        ctx = get_latest_context_for_whatsapp(from_whatsapp)
        if not ctx:
            _log_receipt_request_event("", "", "", from_whatsapp, "USER_TEXT", "NO_CONTEXT")
            return twiml("👋 Para enviarte tu recibo, primero necesitás el mensaje inicial de RRHH. Si no lo tenés, avisá a RRHH.")

        tenant = (ctx.get("tenant") or "").strip().lower()
        cuil = (ctx.get("cuil") or "").strip()

        if not (tenant and cuil):
            _log_receipt_request_event(tenant, cuil, "", from_whatsapp, "USER_TEXT", "NO_CONTEXT")
            return twiml("👋 No pude identificar tu recibo. Avisá a RRHH.")

        period = resolve_best_period_with_pdf(tenant, cuil)
        if not period:
            _log_receipt_request_event(tenant, cuil, "", from_whatsapp, "USER_TEXT", "NO_PDF")
            return twiml("⚠️ No encontré ningún recibo disponible para tu CUIL. Avisá a RRHH.")

        add_pending_view(from_whatsapp, tenant, strip_pdf(cuil), period)
        pending = get_latest_pending_view(from_whatsapp)

    if not pending:
        return Response("OK", status=200)

    tenant = (pending.get("tenant") or "").strip().lower()
    cuil = (pending.get("cuil") or "").strip()
    period = (pending.get("period") or "").strip()
    step = (pending.get("step") or "READY").upper()

    # 🔒 Si ya cerró
    # 🔒 Bloquear SOLO si intenta firmar/observar
    if button in ("SIGN_OK", "SIGN_OBS") or body in ("SIGN_OK", "SIGN_OBS"):
        estado = get_recibo_estado(tenant, cuil, period)
        if estado in ("FIRMADO", "OBSERVADO"):
            msg = "✅ Este recibo ya fue firmado." if estado == "FIRMADO" else "📝 Este recibo quedó como observado."
            return twiml(msg)


    # =========================
    # BOTONES PLANTILLA: RESEND_LAST / SEE_PREVIOUS / MORE_OPTIONS
    # =========================

    # MORE_OPTIONS (respuesta simple por ahora)
    if button == "MORE_OPTIONS":
        return twiml("ℹ️ Esta opción estará disponible próximamente.")

    # RESEND_LAST -> envía el último recibo disponible (mes actual si existe, sino el último real)
    if button == "RESEND_LAST":
        best_period = resolve_best_period_with_pdf(tenant, cuil)
        if not best_period:
            _log_receipt_request_event(tenant, cuil, "", from_whatsapp, "RESEND_LAST", "NO_PDF")
            return twiml("⚠️ No encontré recibos disponibles para tu CUIL. Avisá a RRHH.")

        cnt = get_receipt_request_count(tenant, cuil, best_period, from_whatsapp)
        if cnt >= 3:
            _log_receipt_request_event(tenant, cuil, best_period, from_whatsapp, "RESEND_LAST", "BLOCKED_LIMIT")
            return twiml(f"⚠️ Ya pediste este recibo {cnt}/3 veces para {best_period}. Si necesitás más, avisá a RRHH.")

        if not is_verified_contact(tenant, cuil, from_whatsapp):
            # guardamos el período que vamos a reenviar
            add_pending_view(from_whatsapp, tenant, cuil, best_period)
            pending = get_latest_pending_view(from_whatsapp)
            set_pending_step(pending["id"], "AWAIT_DNI")

            _log_receipt_request_event(tenant, cuil, best_period, from_whatsapp, "RESEND_LAST", "ASK_DNI")
            return twiml("🔐 Para reenviar tu recibo, enviá tu DNI (solo números, sin puntos).")


        sid_pdf = _send_pdf_flow(from_whatsapp, tenant, cuil, best_period)
        if not sid_pdf:
            _log_receipt_request_event(tenant, cuil, best_period, from_whatsapp, "RESEND_LAST", "ERROR")
            return twiml("❌ No pude enviar el PDF en este momento. Probá de nuevo o avisá a RRHH.")

        n = inc_receipt_request_count(tenant, cuil, best_period, from_whatsapp)
        _log_receipt_request_event(tenant, cuil, best_period, from_whatsapp, "RESEND_LAST", "SENT", message_sid=sid_pdf, origin="RESEND_LAST")
        return twiml(f"📄 Listo. Te reenvié el recibo {best_period}. (Pedido {n}/3)")

    # SEE_PREVIOUS -> ofrece hasta 3 períodos anteriores (no envía PDF todavía)
    if button == "SEE_PREVIOUS":
        periods = list_periods_for_cuil(tenant, strip_pdf(cuil)) or []
        prev = periods[1:4]  # tres anteriores al último disponible

        if not prev:
            return twiml("ℹ️ No tengo períodos anteriores disponibles.")

        set_pending_step(pending["id"], "CHOOSE_PREVIOUS")

        msg = "🗂️ Períodos anteriores:\n\n"
        for i, p in enumerate(prev, start=1):
            msg += f"{i}. {p}\n"
        msg += "\nRespondé con 1, 2 o 3 para elegir."
        return twiml(msg)

    # =========================
    # SELECCIÓN de período anterior (1/2/3) cuando step=CHOOSE_PREVIOUS
    # =========================
    if step == "CHOOSE_PREVIOUS" and (not button) and (body or "").strip() in ("1", "2", "3"):
        idx = int((body or "").strip()) - 1

        periods = list_periods_for_cuil(tenant, strip_pdf(cuil)) or []
        prev = periods[1:4]
        if idx >= len(prev):
            return twiml("❌ Opción inválida. Respondé con 1, 2 o 3.")

        chosen_period = prev[idx]

        # volvemos a READY para no quedar pegados en modo selección
        set_pending_step(pending["id"], "READY")

        cnt = get_receipt_request_count(tenant, cuil, chosen_period, from_whatsapp)
        if cnt >= 3:
            _log_receipt_request_event(tenant, cuil, chosen_period, from_whatsapp, "CHOOSE_PREVIOUS", "BLOCKED_LIMIT")
            return twiml(f"⚠️ Ya pediste este recibo {cnt}/3 veces para {chosen_period}. Si necesitás más, avisá a RRHH.")

        if not is_verified_contact(tenant, cuil, from_whatsapp):
            # guardamos el período elegido antes de pedir DNI
            add_pending_view(from_whatsapp, tenant, cuil, chosen_period)
            pending = get_latest_pending_view(from_whatsapp)
            set_pending_step(pending["id"], "AWAIT_DNI")

            _log_receipt_request_event(tenant, cuil, chosen_period, from_whatsapp, "CHOOSE_PREVIOUS", "ASK_DNI", origin="CHOOSE_PREVIOUS")
            return twiml("🔐 Para reenviar tu recibo, enviá tu DNI (solo números, sin puntos).")

        sid_pdf = _send_pdf_flow(from_whatsapp, tenant, cuil, chosen_period)
        if not sid_pdf:
            _log_receipt_request_event(tenant, cuil, chosen_period, from_whatsapp, "CHOOSE_PREVIOUS", "ERROR")
            return twiml("❌ No pude enviar el PDF en este momento. Probá de nuevo o avisá a RRHH.")

        n = inc_receipt_request_count(tenant, cuil, chosen_period, from_whatsapp)
        _log_receipt_request_event(tenant, cuil, chosen_period, from_whatsapp, "CHOOSE_PREVIOUS", "SENT", message_sid=sid_pdf)
        return twiml(f"📄 Listo. Te reenvié el recibo {chosen_period}. (Pedido {n}/3)")

    # =========================
    # AWAIT_DNI
    # =========================
    if step == "AWAIT_DNI":
        if button:
            return twiml("🔐 Para continuar, enviá tu DNI (solo números).")

        dni_user = _digits(body)
        dni_expected = cuil_to_dni(cuil)

        if not dni_expected or len(dni_user) < 7:
            inc_pending_dni_attempts(pending["id"])
            return twiml("🔐 Enviá tu DNI (solo números, sin puntos). Ej: 28169249")

        if dni_user != dni_expected:
            tries = inc_pending_dni_attempts(pending["id"])
            if tries >= 3:
                consume_pending_view(pending["id"])
                return twiml("❌ DNI incorrecto (3 intentos). Volvé a solicitar el recibo desde el mensaje inicial.")
            return twiml(f"❌ DNI incorrecto. Intento {tries}/3. Probá de nuevo (solo números).")

        # DNI OK
        set_verified_contact(tenant, cuil, from_whatsapp, dni_user, nombre=pending.get("nombre",""))
        set_pending_step(pending["id"], "READY")

        cnt = get_receipt_request_count(tenant, cuil, period, from_whatsapp)
        if cnt >= 3:
            _log_receipt_request_event(tenant, cuil, period, from_whatsapp, "DNI_OK", "BLOCKED_LIMIT")
            consume_pending_view(pending["id"])
            return twiml(f"⚠️ Ya pediste este recibo {cnt}/3 veces para {period}. Si necesitás más, avisá a RRHH.")

        sid_pdf = _send_pdf_flow(from_whatsapp, tenant, cuil, period)
        if not sid_pdf:
            _log_receipt_request_event(tenant, cuil, period, from_whatsapp, "DNI_OK", "ERROR")
            return twiml("✅ DNI verificado, pero hubo un error enviando el recibo. Avisá a RRHH.")

        n = inc_receipt_request_count(tenant, cuil, period, from_whatsapp)
        _log_receipt_request_event(tenant, cuil, period, from_whatsapp, "DNI_OK", "SENT", message_sid=sid_pdf)
        return twiml(f"✅ DNI verificado. Te envío el recibo ahora. (Pedido {n}/3)")

    # =========================
    # VIEW_NOW
    # =========================
    if button == "VIEW_NOW" or body == "VIEW_NOW":
        if not is_verified_contact(tenant, cuil, from_whatsapp):
            set_pending_step(pending["id"], "AWAIT_DNI")
            return twiml("🔐 Para ver tu recibo, enviá tu DNI (solo números, sin puntos).")

        cnt = get_receipt_request_count(tenant, cuil, period, from_whatsapp)
        if cnt >= 3:
            _log_receipt_request_event(tenant, cuil, period, from_whatsapp, "VIEW_NOW", "BLOCKED_LIMIT")
            return twiml(f"⚠️ Ya pediste este recibo {cnt}/3 veces para {period}. Si necesitás más, avisá a RRHH.")

        sid_pdf = _send_pdf_flow(from_whatsapp, tenant, cuil, period)
        if not sid_pdf:
            _log_receipt_request_event(tenant, cuil, period, from_whatsapp, "VIEW_NOW", "ERROR")
            return twiml("❌ No pude enviar el PDF en este momento. Avisá a RRHH.")

        n = inc_receipt_request_count(tenant, cuil, period, from_whatsapp)
        _log_receipt_request_event(tenant, cuil, period, from_whatsapp, "VIEW_NOW", "SENT", message_sid=sid_pdf)
        return twiml(f"📄 Perfecto. Te envío el recibo ahora. (Pedido {n}/3)")

    # NO_NEED
    if button == "NO_NEED" or body == "NO_NEED":
        set_recibo_estado(tenant, cuil, period, "NO_NEED")
        consume_pending_view(pending["id"])
        return twiml("✅ Perfecto, no hay problema.")

    # SIGN_OK / SIGN_OBS
    if button in ("SIGN_OK", "SIGN_OBS") or body in ("SIGN_OK", "SIGN_OBS"):
        if button == "SIGN_OK" or body == "SIGN_OK":
            set_recibo_estado(tenant, cuil, period, "FIRMADO")
            consume_pending_view(pending["id"])
            return twiml("✅ Recibo firmado. ¡Gracias!")
        else:
            set_recibo_estado(tenant, cuil, period, "OBSERVADO")
            consume_pending_view(pending["id"])
            return twiml("📝 Recibo observado. Vamos a revisarlo y te contactamos.")

    return Response("OK", status=200)

def _send_pdf_flow(from_whatsapp: str, tenant: str, cuil: str, period: str) -> str | None:
    file_id = find_pdf_file_id_for_cuil_period(tenant, cuil, period)
    if not file_id:
        return None

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

        set_recibo_estado(tenant, cuil, period, "DISPONIBLE")
        save_pdf_sid(tenant, cuil, period, from_whatsapp, sid_pdf)
        return sid_pdf

    except Exception as e:
        print("ERROR sending PDF:", e)
        return None


@app.get("/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time()), "db_path": DB_PATH})
