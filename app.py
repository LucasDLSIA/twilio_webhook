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
    print("\n=== FIND PDF DEBUG ===")
    print("tenant:", tenant)
    print("cuil raw:", cuil)
    print("period raw:", period)

    t = get_tenant(tenant)
    print("tenant config:", t)

    if not t:
        print("❌ tenant no encontrado")
        return None

    root_id = (t.get("recibos_root_id") or t.get("drive_root_id") or "").strip()
    print("root_id:", root_id)
    if not root_id:
        print("❌ tenant sin recibos_root_id/drive_root_id")
        return None

    cuil_norm = strip_pdf(cuil).strip()
    filename = f"{cuil_norm}.pdf"
    print("filename esperado:", filename)

    service = drive_service()

    # 1) Intentar carpeta de período
    period_names = _norm_period_variants(period)
    print("period variants:", period_names)

    folder_id = None
    folders = _drive_list_children(service, root_id, mime_type="application/vnd.google-apps.folder", page_size=200)
    print("carpetas en root:", [f["name"] for f in folders][:40], "..." if len(folders) > 40 else "")

    for f in folders:
        if (f.get("name") or "").strip() in period_names:
            folder_id = f["id"]
            print("✅ carpeta período matcheó:", f["name"], "id:", folder_id)
            break

    if folder_id:
        fid = _drive_find_child_by_exact_name(service, folder_id, filename, mime_type="application/pdf")
        print("pdf en carpeta período:", fid)
        if fid:
            print("✅ PDF encontrado en carpeta período")
            return fid
        else:
            print("❌ no está el pdf en la carpeta período")

    # 2) Fallback: buscar en root directo
    fid = _drive_find_child_by_exact_name(service, root_id, filename, mime_type="application/pdf")
    print("pdf en root:", fid)
    if fid:
        print("✅ PDF encontrado en root")
        return fid

    # 3) Debug extra: ver si existe un archivo parecido (mismo cuil sin .pdf, etc.)
    all_files = _drive_list_children(service, root_id, mime_type=None, page_size=200)
    matches = [x["name"] for x in all_files if cuil_norm in (x.get("name") or "")]
    print("archivos que contienen el cuil en el nombre:", matches[:30], "..." if len(matches) > 30 else "")

    print("❌ PDF NO ENCONTRADO")
    return None

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
    print("\n=== MEDIA PDF ===")
    print("ARGS:", dict(request.args))
    token = request.args.get("token", "").strip()
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        return Response("Unauthorized", status=401)

    tenant = (request.args.get("tenant") or "").strip().lower()
    cuil = (request.args.get("cuil") or "").strip()
    period = (request.args.get("period") or "").strip()

    if not (tenant and cuil and period):
        return Response("Faltan parámetros tenant/cuil/period", status=400)

    # ✅ MISMA FUNCIÓN que usás para validar existencia antes de mandar plantilla
    file_id = find_pdf_file_id(tenant, cuil, period)
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
    if not message_sid:
        return False

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT 1 FROM sent_pdfs WHERE message_sid = ? LIMIT 1;",
        (message_sid,),
    )
    row = cur.fetchone()
    conn.close()

    return bool(row)


@app.post("/twilio/status")
def twilio_status():
    sid = (request.form.get("MessageSid") or "").strip()
    status = (request.form.get("MessageStatus") or "").strip().lower()
    error_code = request.form.get("ErrorCode")
    error_message = request.form.get("ErrorMessage")

    if not sid:
        return Response("OK", status=200)

    # 1) Si es PDF
    if is_pdf_sid(sid):  # tu helper actual
        _set_status_on_table("sent_pdfs", sid, status, error_code, error_message)

        # ✅ Cuando el PDF se entrega, recién ahí mando firma
        if status == "delivered" and TWILIO_SIGN_TEMPLATE_SID:
            info = get_sent_pdf_by_sid(sid)  # (tenant,cuil,period,to_whatsapp)
            if info and not info.get("sign_sent_at"):
                try:
                    sign_sid = send_whatsapp_template(
                        info["to_whatsapp"],
                        content_vars={"1": info["period"]},
                        template_sid=TWILIO_SIGN_TEMPLATE_SID,
                        status_callback=STATUS_CALLBACK_URL,
                    )
                    mark_sign_sent(sid)  # pone sign_sent_at
                    print("SENT SIGN AFTER PDF DELIVERED:", sign_sid)
                except Exception as e:
                    print("WARN sending sign after delivered:", e)

        return Response("OK", status=200)

    # 2) Si es PLANTILLA VIEW_NOW
    if is_template_sid(sid):  # te paso helper abajo
        _set_status_on_table("sent_templates", sid, status, error_code, error_message)
        return Response("OK", status=200)

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
        UNIQUE(to_whatsapp, tenant, cuil, period)
      );
    """)

    # Compatibilidad si venías de esquema viejo
    _try_alter(cur, "ALTER TABLE pending_views ADD COLUMN tenant TEXT;")
    _try_alter(cur, "ALTER TABLE pending_views ADD COLUMN cuil TEXT;")
    _try_alter(cur, "ALTER TABLE pending_views ADD COLUMN period TEXT;")
    # (si tuviste archivo_norm/period_label, se corrige en funciones, no acá)

    # =========
    # recibo_estado (acá se guarda firmado/observado/no_need)
    # =========
    cur.execute("""
      CREATE TABLE IF NOT EXISTS recibo_estado (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant TEXT NOT NULL,
        cuil TEXT NOT NULL,
        period TEXT NOT NULL,
        estado TEXT NOT NULL,         -- FIRMADO | OBSERVADO | NO_NEED (NO usamos DISPONIBLE en reporte)
        updated_at INTEGER NOT NULL,
        UNIQUE(tenant, cuil, period)
      );
    """)
    _try_alter(cur, "ALTER TABLE recibo_estado ADD COLUMN tenant TEXT;")
    _try_alter(cur, "ALTER TABLE recibo_estado ADD COLUMN cuil TEXT;")
    _try_alter(cur, "ALTER TABLE recibo_estado ADD COLUMN period TEXT;")
    _try_alter(cur, "ALTER TABLE recibo_estado ADD COLUMN updated_at INTEGER;")

    # =========
    # message_status (plantilla/pdf: enviado/entregado/leído/falló)
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

    # Migraciones para DB ya creada (este bloque es el que te faltaba)
    _try_alter(cur, "ALTER TABLE message_status ADD COLUMN tenant TEXT;")
    _try_alter(cur, "ALTER TABLE message_status ADD COLUMN cuil TEXT;")
    _try_alter(cur, "ALTER TABLE message_status ADD COLUMN period TEXT;")
    _try_alter(cur, "ALTER TABLE message_status ADD COLUMN nombre TEXT;")
    _try_alter(cur, "ALTER TABLE message_status ADD COLUMN kind TEXT;")
    _try_alter(cur, "ALTER TABLE message_status ADD COLUMN created_at INTEGER;")
    _try_alter(cur, "ALTER TABLE message_status ADD COLUMN last_status TEXT;")
    _try_alter(cur, "ALTER TABLE message_status ADD COLUMN last_status_at INTEGER;")
    _try_alter(cur, "ALTER TABLE message_status ADD COLUMN delivered_at INTEGER;")
    _try_alter(cur, "ALTER TABLE message_status ADD COLUMN read_at INTEGER;")
    _try_alter(cur, "ALTER TABLE message_status ADD COLUMN failed_at INTEGER;")
    _try_alter(cur, "ALTER TABLE message_status ADD COLUMN error_code TEXT;")
    _try_alter(cur, "ALTER TABLE message_status ADD COLUMN error_message TEXT;")

    # =========
    # sent_pdfs (para mandar firma DESPUÉS del PDF vía /twilio/status)
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
    # índices
    # =========
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_pending_to_created ON pending_views(to_whatsapp, created_at);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_estado_key ON recibo_estado(tenant, cuil, period);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_msg_key ON message_status(tenant, cuil, period, kind);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_msg_sid ON message_status(message_sid);")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_sentpdfs_sid ON sent_pdfs(message_sid);")

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

def save_template_sid(tenant: str, cuil: str, period: str, to_whatsapp: str, message_sid: str, nombre: str = ""):
    conn = get_db_connection()
    cur = conn.cursor()
    now = int(time.time())

    # Guardamos en message_status como "template"
    cur.execute("""
      INSERT OR IGNORE INTO message_status
        (message_sid, to_whatsapp, tenant, cuil, period, nombre, kind, created_at, last_status, last_status_at)
      VALUES
        (?, ?, ?, ?, ?, ?, 'template', ?, 'sent', ?);
    """, (message_sid, to_whatsapp, tenant, cuil, period, nombre, now, now))

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
        <label>Período (opcional, mm/aaaa): </label>
        <input type="text" name="period" placeholder="01/2026" style="width:90px;">
        <button type="submit">📄 Descargar reporte recibos (XLSX)</button>
    </form>

    <p>
        <a href="/admin/report_envios.csv?tenant={esc(tenant)}&token={esc(token)}" target="_blank">
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

def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)

@app.get("/admin/report_recibos.xlsx")
@admin_required
def admin_report_recibos_xlsx():
    token = _get_admin_token_from_request()
    tenant = (request.args.get("tenant") or "").strip().lower()
    period = (request.args.get("period") or "").strip()  # opcional

    if not tenant:
        return Response("Falta tenant", status=400)

    def ts_fmt(ts: int | None) -> str:
        if not ts:
            return ""
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(ts)

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1) Base: tomamos todo lo que exista en message_status + recibo_estado para ese tenant (+period opcional)
    # Armamos un set de keys (tenant,cuil,period)
    params = [tenant]
    where_period = ""
    if period:
        where_period = " AND period = ? "
        params.append(period)

    cur.execute(f"""
      SELECT tenant, cuil, period, to_whatsapp, nombre, kind,
             created_at, delivered_at, read_at, failed_at
      FROM message_status
      WHERE tenant = ?
      {where_period}
    """, params)
    ms_rows = cur.fetchall()

    # key -> estructura
    data = {}  # (cuil,period) -> dict

    def ensure(cuil_, period_):
        k = (cuil_, period_)
        if k not in data:
            data[k] = {
                "Periodo": period_,
                "Nombre": "",
                "CUIL": cuil_,
                "WhatsApp": "",
                "Plantilla_enviada": "",
                "Plantilla_entregada": "",
                "Plantilla_leida": "",
                "Plantilla_fallida": "",
                "PDF_enviado": "",
                "PDF_entregado": "",
                "PDF_leido": "",
                "PDF_fallido": "",
                "Respuesta_usuario": "",
                "Respuesta_timestamp": "",
            }
        return data[k]

    for r in ms_rows:
        cuil_ = (r["cuil"] or "").strip()
        period_ = (r["period"] or "").strip()
        if not cuil_ or not period_:
            continue

        row = ensure(cuil_, period_)
        if r["nombre"] and not row["Nombre"]:
            row["Nombre"] = r["nombre"]
        if r["to_whatsapp"] and not row["WhatsApp"]:
            row["WhatsApp"] = r["to_whatsapp"]

        kind = (r["kind"] or "").lower()
        created_at = r["created_at"]
        delivered_at = r["delivered_at"]
        read_at = r["read_at"]
        failed_at = r["failed_at"]

        if kind == "template":
            row["Plantilla_enviada"] = row["Plantilla_enviada"] or ts_fmt(created_at)
            row["Plantilla_entregada"] = row["Plantilla_entregada"] or ts_fmt(delivered_at)
            row["Plantilla_leida"] = row["Plantilla_leida"] or ts_fmt(read_at)
            row["Plantilla_fallida"] = row["Plantilla_fallida"] or ts_fmt(failed_at)

        elif kind == "pdf":
            row["PDF_enviado"] = row["PDF_enviado"] or ts_fmt(created_at)
            row["PDF_entregado"] = row["PDF_entregado"] or ts_fmt(delivered_at)
            row["PDF_leido"] = row["PDF_leido"] or ts_fmt(read_at)
            row["PDF_fallido"] = row["PDF_fallido"] or ts_fmt(failed_at)

    # 2) Respuesta usuario: sale de recibo_estado (solo FIRMADO/OBSERVADO/NO_NEED)
    cur.execute(f"""
      SELECT cuil, period, estado, updated_at
      FROM recibo_estado
      WHERE tenant = ?
      {where_period}
    """, params)
    est_rows = cur.fetchall()

    for r in est_rows:
        cuil_ = (r["cuil"] or "").strip()
        period_ = (r["period"] or "").strip()
        if not cuil_ or not period_:
            continue
        row = ensure(cuil_, period_)
        estado = (r["estado"] or "").strip().lower()

        # Formato como querés: "firmado"/"observado"
        if estado == "firmado":
            row["Respuesta_usuario"] = "firmado"
        elif estado == "observado":
            row["Respuesta_usuario"] = "observado"
        elif estado == "no_need":
            row["Respuesta_usuario"] = "no_need"
        else:
            # si quedó cualquier otra cosa, no lo mostramos
            row["Respuesta_usuario"] = row["Respuesta_usuario"] or ""

        row["Respuesta_timestamp"] = row["Respuesta_timestamp"] or ts_fmt(r["updated_at"])

    conn.close()

    # 3) Generar XLSX
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Recibos"

    headers = [
        "Periodo", "Nombre", "CUIL", "WhatsApp",
        "Plantilla_enviada", "Plantilla_entregada", "Plantilla_leida", "Plantilla_fallida",
        "PDF_enviado", "PDF_entregado", "PDF_leido", "PDF_fallido",
        "Respuesta_usuario", "Respuesta_timestamp"
    ]
    ws.append(headers)

    # Orden: por periodo, luego nombre/cuil
    rows_sorted = sorted(
        data.values(),
        key=lambda x: (x["Periodo"], x["Nombre"], x["CUIL"])
    )
    for r in rows_sorted:
        ws.append([r.get(h, "") for h in headers])

    # Auto ancho simple
    for col_idx, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = max(14, min(40, len(h) + 2))

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)

    filename = f"reporte_recibos_{tenant}_{(period or 'todos').replace('/','-')}.xlsx"
    return send_file(
        out,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )



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
            pdf_file_id = find_pdf_file_id(tenant, cuil, period)
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
