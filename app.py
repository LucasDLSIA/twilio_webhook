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
    return re.sub(r"\D+", "", (s or "")).strip()


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
def format_cuil_with_dashes(cuil: str) -> str:
    d = norm_digits(cuil)
    if len(d) != 11:
        return cuil.strip()
    return f"{d[0:2]}-{d[2:10]}-{d[10:11]}"


def find_pdf_file_id_for_cuil_period(tenant: str, cuil: str, period: str) -> str | None:
    t = get_tenant(tenant)
    if not t:
        print("❌ tenant inválido:", tenant)
        return None

    root_id = (t.get("recibos_root_id") or t.get("drive_root_id") or "").strip()
    if not root_id:
        print("❌ tenant sin recibos_root_id:", tenant)
        return None

    cuil_digits = norm_digits(strip_pdf(cuil).strip())
    if len(cuil_digits) != 11:
        print("❌ CUIL inválido:", cuil)
        return None

    cuil_dash = format_cuil_with_dashes(cuil_digits)
    filename_exact = f"{cuil_dash}.pdf"

    period_folder_name = normalize_period_for_drive((period or "").strip())

    service = drive_service()

    # 1) carpeta período
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

    # 2a) buscar exacto (xx-xxxxxxxx-x.pdf)
    q_pdf_exact = (
        f"'{period_id}' in parents and trashed=false "
        f"and name='{filename_exact}'"
    )
    res2 = service.files().list(q=q_pdf_exact, fields="files(id,name)", pageSize=5).execute()
    files = res2.get("files", [])
    if files:
        return files[0]["id"]

    # 2b) fallback robusto: pdf + contiene el cuil con guiones
    q_pdf_contains = (
        f"'{period_id}' in parents and trashed=false "
        f"and mimeType='application/pdf' "
        f"and name contains '{cuil_dash}'"
    )
    res3 = service.files().list(q=q_pdf_contains, fields="files(id,name)", pageSize=10).execute()
    files2 = res3.get("files", [])
    if files2:
        # si hay más de uno, agarramos el primero (podés ordenar por modifiedTime si querés)
        return files2[0]["id"]

    print(f"❌ No encontré {filename_exact} (ni variantes) dentro de carpeta período {period_folder_name} ({period_id})")
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

    # 1) intentar update (si existe fila)
    cur.execute("""
        UPDATE message_status
        SET last_status = ?, last_status_at = ?,
            error_code = CASE WHEN ? != '' THEN ? ELSE error_code END,
            error_message = CASE WHEN ? != '' THEN ? ELSE error_message END
        WHERE message_sid = ?
    """, (status, now, error_code, error_code, error_message, error_message, sid))

    # 2) ✅ si no existía, crearla desde sent_pdfs (caso PDF/media)
    if cur.rowcount == 0:
        cur.execute("""
            SELECT tenant, cuil, period, to_whatsapp
            FROM sent_pdfs
            WHERE message_sid = ?
            LIMIT 1
        """, (sid,))
        r = cur.fetchone()

        if r:
            tenant, cuil, period, to_whatsapp = r
            # creamos fila mínima en message_status para que el callback pueda registrar delivered/read
            cur.execute("""
                INSERT OR IGNORE INTO message_status
                (message_sid, to_whatsapp, tenant, cuil, period, nombre, kind, created_at, last_status, last_status_at)
                VALUES (?, ?, ?, ?, ?, '', 'media', ?, ?, ?)
            """, (sid, to_whatsapp, tenant, cuil, period, now, status, now))

            # volver a aplicar update (por si insertó recién)
            cur.execute("""
                UPDATE message_status
                SET last_status = ?, last_status_at = ?,
                    error_code = CASE WHEN ? != '' THEN ? ELSE error_code END,
                    error_message = CASE WHEN ? != '' THEN ? ELSE error_message END
                WHERE message_sid = ?
            """, (status, now, error_code, error_code, error_message, error_message, sid))

    # 3) timestamps por estado
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

    # 4) SIGN después de delivered (solo INITIAL)
        # 4) SIGN después de delivered (solo INITIAL) + estado A_FINALIZAR
    if status == "delivered":
        cur.execute("""
            SELECT tenant, cuil, period, to_whatsapp, sign_sent_at, COALESCE(origin, 'INITIAL')
            FROM sent_pdfs
            WHERE message_sid = ?
            LIMIT 1
        """, (sid,))
        row = cur.fetchone()

        if row:
            tenant, cuil, period, to_whatsapp, sign_sent_at, origin = row

            # ✅ Marcar A_FINALIZAR al entregar (pero NO pisar si ya está FIRMADO/OBSERVADO)
            cur.execute("""
                INSERT INTO recibo_estado (tenant, cuil, period, estado, updated_at)
                VALUES (?, ?, ?, 'A_FINALIZAR', ?)
                ON CONFLICT(tenant, cuil, period) DO UPDATE SET
                  estado='A_FINALIZAR',
                  updated_at=excluded.updated_at
                WHERE recibo_estado.estado NOT IN ('FIRMADO','OBSERVADO');
            """, (tenant, cuil, period, now))

            # 🔒 Si ya está cerrado, no mandes firma
            cur.execute("""
                SELECT estado FROM recibo_estado
                WHERE tenant=? AND cuil=? AND period=?
                LIMIT 1
            """, (tenant, cuil, period))
            est = (cur.fetchone() or [None])[0]

            # Si es reenvío, NO firmar nunca
            if origin != "INITIAL":
                print("SKIP SIGN AFTER PDF (origin=", origin, "):", sid)

            elif est in ("FIRMADO", "OBSERVADO"):
                print("SKIP SIGN (already closed):", tenant, cuil, period, est)

            else:
                if not sign_sent_at and TWILIO_SIGN_TEMPLATE_SID:
                    try:
                        sid_sign = send_whatsapp_template(
                            to_whatsapp,
                            content_vars={"1": period},
                            template_sid=TWILIO_SIGN_TEMPLATE_SID
                        )

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
    ts = int(time.time())
    origin = origin or source

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()

        # 2) Ver columnas actuales
        cols = {r[1] for r in cur.execute("PRAGMA table_info(receipt_request_events)").fetchall()}

        def _add_col(colname: str, coltype: str):
            nonlocal cols
            if colname not in cols:
                cur.execute(f"ALTER TABLE receipt_request_events ADD COLUMN {colname} {coltype}")
                cols.add(colname)

        # 3) Asegurar columnas usadas (compatibilidad)
        _add_col("tenant", "TEXT")
        _add_col("cuil", "TEXT")
        _add_col("period", "TEXT")
        _add_col("source", "TEXT")
        _add_col("result", "TEXT")
        _add_col("message_sid", "TEXT")
        _add_col("created_at", "INTEGER")
        _add_col("requested_at", "INTEGER")   # ✅ clave para tu NOT NULL viejo
        _add_col("origin", "TEXT")

        # compatibilidad con nombres viejos/nuevos
        if "to_whatsapp" not in cols and "whatsapp" not in cols:
            _add_col("to_whatsapp", "TEXT")
        else:
            _add_col("to_whatsapp", "TEXT")
            _add_col("whatsapp", "TEXT")

        # 4) Preparar data
        data = {
            "tenant": tenant,
            "cuil": cuil,
            "period": period,
            "source": source,
            "result": result,
            "message_sid": message_sid,
            "created_at": ts,
            "requested_at": ts,   # ✅ siempre seteamos ambos si existen
            "origin": origin,
        }

        # guardar whatsapp en la columna que exista
        if "to_whatsapp" in cols:
            data["to_whatsapp"] = to_whatsapp
        if "whatsapp" in cols:
            data["whatsapp"] = to_whatsapp

        insert_cols = [k for k in data.keys() if k in cols]
        placeholders = ",".join(["?"] * len(insert_cols))
        sql = f"INSERT INTO receipt_request_events ({','.join(insert_cols)}) VALUES ({placeholders})"

        cur.execute(sql, tuple(data[k] for k in insert_cols))
        conn.commit()

    except Exception as e:
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
    _try_alter(cur, "ALTER TABLE pending_views ADD COLUMN origin TEXT;")


    # (opcional pero recomendado) limpiar duplicados por to_whatsapp antes del índice único
    _try_alter(cur, """
    DELETE FROM pending_views
    WHERE id NOT IN (
    SELECT pv.id
    FROM pending_views pv
    JOIN (
        SELECT to_whatsapp, MAX(created_at) AS max_created
        FROM pending_views
        GROUP BY to_whatsapp
    ) x
    ON x.to_whatsapp = pv.to_whatsapp AND x.max_created = pv.created_at
    );
    """)

    _try_alter(cur, """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_pending_views_to_whatsapp
    ON pending_views(to_whatsapp);
    """)

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
    # template_send_queue (cola de envíos de templates)
    # =========
    cur.execute("""
    CREATE TABLE IF NOT EXISTS template_send_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant TEXT NOT NULL,
        period TEXT NOT NULL,
        to_whatsapp TEXT NOT NULL,
        cuil TEXT NOT NULL,
        nombre TEXT,
        require_pdf INTEGER DEFAULT 1,
        status TEXT DEFAULT 'PENDING',     -- PENDING | SENT | SKIPPED | FAILED
        error TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER,
        sent_sid TEXT,
        sent_at INTEGER,
        UNIQUE(tenant, period, to_whatsapp, cuil)
    );
    """)
    _try_alter(cur, "ALTER TABLE template_send_queue ADD COLUMN nombre TEXT;")
    _try_alter(cur, "ALTER TABLE template_send_queue ADD COLUMN require_pdf INTEGER;")
    _try_alter(cur, "ALTER TABLE template_send_queue ADD COLUMN status TEXT;")
    _try_alter(cur, "ALTER TABLE template_send_queue ADD COLUMN error TEXT;")
    _try_alter(cur, "ALTER TABLE template_send_queue ADD COLUMN created_at INTEGER;")
    _try_alter(cur, "ALTER TABLE template_send_queue ADD COLUMN updated_at INTEGER;")
    _try_alter(cur, "ALTER TABLE template_send_queue ADD COLUMN sent_sid TEXT;")
    _try_alter(cur, "ALTER TABLE template_send_queue ADD COLUMN sent_at INTEGER;")

    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_ts_queue_pending ON template_send_queue(status, tenant, period, created_at);")

    
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
        sign_sent_at INTEGER,
        origin TEXT
      );
    """)
    _try_alter(cur, "ALTER TABLE sent_pdfs ADD COLUMN sign_sent_at INTEGER;")
    _try_alter(cur, "ALTER TABLE sent_pdfs ADD COLUMN origin TEXT;")
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

    #    # =========
    # ✅ NUEVO: receipt_request_events (log evento por evento)
    # =========
    cur.execute("""
    CREATE TABLE IF NOT EXISTS receipt_request_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT,
    cuil TEXT,
    period TEXT,
    to_whatsapp TEXT,
    source TEXT,        -- VIEW_NOW, RESEND_LAST, DNI_OK, CHOOSE_PREVIOUS, USER_TEXT...
    result TEXT,        -- SENT, ERROR, ASK_DNI, NO_CONTEXT, NO_PDF, BLOCKED_LIMIT...
    message_sid TEXT,
    created_at INTEGER, -- timestamp evento
    origin TEXT         -- INITIAL / RESEND_LAST / CHOOSE_PREVIOUS (o el mismo source)
    )
    """)
    
    _try_alter(cur, "ALTER TABLE receipt_request_events ADD COLUMN created_at INTEGER;")
    _try_alter(cur, "CREATE INDEX IF NOT EXISTS idx_rre_key ON receipt_request_events(tenant,cuil,period,to_whatsapp,created_at);")


    # =========
    # ✅ NUEVO: inbound_dedup (para evitar doble procesamiento)
    # =========
    cur.execute("""
      CREATE TABLE IF NOT EXISTS inbound_dedup (
        message_sid TEXT PRIMARY KEY,
        created_at INTEGER
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

def inbound_seen(message_sid: str) -> bool:
    if not message_sid:
        return False
    now = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO inbound_dedup(message_sid, created_at) VALUES (?, ?)", (message_sid, now))
    conn.commit()
    inserted = (cur.rowcount == 1)
    conn.close()
    return (not inserted)  # True si ya existía







def save_pdf_sid(tenant: str, cuil: str, period: str, to_whatsapp: str, message_sid: str, origin: str = "INITIAL"):
    now = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()

    # 1) histórico de PDFs
    cur.execute("""
        INSERT OR REPLACE INTO sent_pdfs
        (tenant, cuil, period, to_whatsapp, message_sid, created_at, sign_sent_at, origin)
        VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM sent_pdfs WHERE message_sid = ?), ?),
                COALESCE((SELECT sign_sent_at FROM sent_pdfs WHERE message_sid = ?), NULL),
                ?)
    """, (tenant, cuil, period, to_whatsapp, message_sid, message_sid, now, message_sid, origin))

    # 2) ✅ tracking para reportes + callbacks
    cur.execute("""
        INSERT OR IGNORE INTO message_status
        (message_sid, to_whatsapp, tenant, cuil, period, nombre, kind, created_at, last_status, last_status_at)
        VALUES (?, ?, ?, ?, ?, '', 'media', ?, 'sent', ?)
    """, (message_sid, to_whatsapp, tenant, cuil, period, now, now))

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
    """
    Wrapper legacy. Mantener por compatibilidad.
    Usa is_verified_contact como única fuente de verdad.
    """
    return is_verified_contact(tenant, cuil, to_whatsapp)

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


def norm_cuil_digits(x: str) -> str:
    return "".join(ch for ch in (x or "") if ch.isdigit())

def is_verified_contact(tenant: str, cuil: str, to_whatsapp: str) -> bool:
    cuil_d = norm_cuil_digits(cuil)
    w = normalize_whatsapp(to_whatsapp)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
      SELECT 1
      FROM verifications
      WHERE tenant = ?
        AND (
          -- match por cuil normalizado (saca guiones, espacios, etc.)
          replace(replace(cuil, '-', ''), ' ', '') = ?
        )
        AND (
          -- match por whatsapp normalizado
          to_whatsapp = ?
          OR replace(replace(replace(to_whatsapp,'whatsapp:',''),'+',''),' ','') =
             replace(replace(replace(?,'whatsapp:',''),'+',''),' ','')
        )
      LIMIT 1
    """, (tenant, cuil_d, w, w))
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

@app.get("/admin/reset_reenvios")
def admin_reset_reenvios():
    token = (request.args.get("token") or "").strip()
    tenant = (request.args.get("tenant") or "").strip().lower()
    cuil = (request.args.get("cuil") or "").strip()
    whatsapp = (request.args.get("whatsapp") or "").strip()

    if not token or token != ADMIN_TOKEN:
        return Response("Unauthorized", status=401)
    if not tenant or not cuil:
        return Response("Missing tenant/cuil", status=400)

    conn = get_db_connection()
    cur = conn.cursor()

    def _safe(sql, params=()):
        try:
            cur.execute(sql, params)
            return cur.rowcount
        except Exception as e:
            print("WARN reset_reenvios:", e)
            return 0

    deleted = {}

    # Borra SOLO eventos de reenvío (RESEND_LAST)
    # soporta schema viejo/nuevo: origin o source
    if whatsapp:
        deleted["receipt_request_events"] = _safe("""
            DELETE FROM receipt_request_events
            WHERE tenant=? AND cuil=? AND (whatsapp=? OR to_whatsapp=?)
              AND (
                origin='RESEND_LAST' OR source='RESEND_LAST'
              )
        """, (tenant, cuil, whatsapp, whatsapp))
    else:
        deleted["receipt_request_events"] = _safe("""
            DELETE FROM receipt_request_events
            WHERE tenant=? AND cuil=?
              AND (
                origin='RESEND_LAST' OR source='RESEND_LAST'
              )
        """, (tenant, cuil))

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "mode": "safe_reset_reenvios",
        "tenant": tenant,
        "cuil": cuil,
        "whatsapp": whatsapp or None,
        "deleted": deleted
    })

@app.get("/admin/send_template_preview")
def admin_send_template_preview():
    auth = require_admin()
    if auth:
        return auth

    tenant = (request.args.get("tenant") or "").strip().lower()
    period_label = (request.args.get("period") or "").strip()  # "01/2026"
    limit = int((request.args.get("limit") or "0").strip() or 0)
    require_pdf = (request.args.get("require_pdf") or "true").strip().lower() in ("1", "true", "yes", "on")

    if not tenant:
        return jsonify({"ok": False, "error": "Falta tenant"}), 400

    t = get_tenant(tenant)
    if not t:
        return jsonify({"ok": False, "error": "Tenant inválido"}), 400

    envios_rows = load_envios_rows(tenant, force=False) or []

    def pick(r, keys):
        for k in keys:
            v = r.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    # --- 1) Si require_pdf: obtener set de CUILs que tienen PDF en ese período ---
    pdf_cuils = None
    period_folder_id = ""

    if require_pdf:
        period_folder_id = get_tenant_period_folder_id(tenant, period_label)  # root -> "MM-YYYY"
        if not period_folder_id:
            return jsonify({
                "ok": True,
                "tenant": tenant,
                "period": period_label,
                "limit": limit,
                "total_match": 0,
                "showing": 0,
                "recipients": [],
                "note": f"No existe carpeta de período para {period_label} (o get_tenant_period_folder_id no la encontró)."
            })

        service = drive_service()

        # IMPORTANTE: esto debe listar archivos del folder (no solo subfolders)
        children = _drive_list_children(service, parent_id=period_folder_id, mime_type=None, page_size=1000) or []

        pdf_cuils = set()
        pdf_names_sample = []

        for c in children:
            name = (c.get("name") or "").strip()
            if not name:
                continue
            if name.lower().endswith(".pdf"):
                base = name[:-4].strip()
                nc = norm_cuil(base)
                if nc:
                    pdf_cuils.add(nc)
                    if len(pdf_names_sample) < 5:
                        pdf_names_sample.append(name)

        # Si no encontró ningún pdf, te devolvemos note con diagnóstico
        if not pdf_cuils:
            return jsonify({
                "ok": True,
                "tenant": tenant,
                "period": period_label,
                "limit": limit,
                "total_match": 0,
                "showing": 0,
                "recipients": [],
                "note": "Encontré la carpeta del período pero no vi PDFs adentro (o _drive_list_children no está listando archivos)."
            })

    # --- 2) Construir recipients desde Excel y filtrar por PDF ---
    recipients = []
    excel_cuil_sample = []

    for r in envios_rows:
        cuil_raw = pick(r, ["cuil", "CUIL"])
        cuil = norm_cuil(cuil_raw)
        nombre = pick(r, ["nombre", "Nombre", "NOMBRE", "name"])
        whatsapp = pick(r, ["to_whatsapp", "whatsapp", "telefono", "tel", "phone"])

        if len(excel_cuil_sample) < 5 and cuil_raw:
            excel_cuil_sample.append(cuil_raw)

        if require_pdf:
            if not cuil:
                continue
            if cuil not in pdf_cuils:
                continue

        recipients.append({"nombre": nombre, "whatsapp": whatsapp, "cuil": cuil})

    total = len(recipients)
    if limit and limit > 0:
        recipients = recipients[:limit]

    resp = {
        "ok": True,
        "tenant": tenant,
        "period": period_label,
        "limit": limit,
        "total_match": total,
        "showing": len(recipients),
        "recipients": recipients,
    }

    # Debug suave (te ayuda si vuelve a quedar en 0)
    if require_pdf and total == 0:
        resp["note"] = "0 match. Revisar formato CUIL. Muestras:"
        resp["excel_cuil_sample"] = excel_cuil_sample
        resp["pdf_cuil_sample"] = list(sorted(list(pdf_cuils)))[:5]

    return jsonify(resp)




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

from io import BytesIO
import sqlite3
import time



def generate_pdf_report_v2(tenant: str, period_filter: str = ""):
    """
    Página 1: KPIs + 1 donut (Estados por jerarquía)
    Página 2+: Tabla con TODAS las personas (sin recortes) y sin duplicar "FIRMADO"
    Jerarquía: FIRMADO > OBSERVADO > PEND. RESPUESTA > PEND. ENVÍO
    Latest-wins por timestamps.
    """
    from io import BytesIO
    import time
    import sqlite3

    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, PageBreak

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tenant = (tenant or "").strip().lower()
    period_filter = norm_period_label(period_filter)

    def _safe_int(x):
        try:
            return int(x or 0)
        except Exception:
            return 0

    # ========= DB =========
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

    # ========= maps (más reciente por key) =========
    estado_map = {}
    for r in estado_rows:
        c = (r["cuil"] or "").strip()
        p = norm_period_label(r["period"] or "")
        ts = _safe_int(r["updated_at"])
        if not (c and p):
            continue
        key = (c, p)
        prev = estado_map.get(key)
        if (prev is None) or (ts >= _safe_int(prev.get("ts"))):
            estado_map[key] = {"estado": (r["estado"] or "").strip(), "ts": ts}

    rr_map = {}
    for r in rr_rows:
        c = (r["cuil"] or "").strip()
        p = norm_period_label(r["period"] or "")
        w = (r["to_whatsapp"] or "").strip()
        ts = _safe_int(r["last_requested_at"])
        if not (c and p and w):
            continue
        key = (w, c, p)
        prev = rr_map.get(key)
        if (prev is None) or (ts >= _safe_int(prev.get("last"))):
            rr_map[key] = {"count": int(r["request_count"] or 0), "last": ts}

    # ========= agregación por (whatsapp, periodo) con latest-wins =========
    agg = {}

    def _ensure(k, base):
        if k not in agg:
            agg[k] = {
                "Periodo": base.get("Periodo", ""),
                "Nombre": base.get("Nombre", ""),
                "CUIL": base.get("CUIL", ""),
                "WhatsApp": base.get("WhatsApp", ""),

                "Respuesta": "",          # se usa solo para clasificar, no se imprime
                "Pedidos": 0,
                "Ultimo_pedido": "",

                "_sent_ts": 0,
                "_delivered_ts": 0,
                "_read_ts": 0,
                "_failed_ts": 0,
                "_estado_ts": 0,
                "_pedido_ts": 0,
                "_last_ts": 0,
            }

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
        _ensure(k, {"Periodo": per, "Nombre": nombre, "CUIL": cuil, "WhatsApp": wpp})
        rec = agg[k]

        if nombre and not rec["Nombre"]:
            rec["Nombre"] = nombre
        if cuil and not rec["CUIL"]:
            rec["CUIL"] = cuil

        kind = (row["kind"] or "").strip().lower()
        if kind not in ("pdf", "media"):
            continue

        sent_ts = _safe_int(row["created_at"])
        deliv_ts = _safe_int(row["delivered_at"])
        read_ts = _safe_int(row["read_at"])
        fail_ts = _safe_int(row["failed_at"])

        if sent_ts and sent_ts >= rec["_sent_ts"]:
            rec["_sent_ts"] = sent_ts
        if deliv_ts and deliv_ts >= rec["_delivered_ts"]:
            rec["_delivered_ts"] = deliv_ts
        if read_ts and read_ts >= rec["_read_ts"]:
            rec["_read_ts"] = read_ts
        if fail_ts and fail_ts >= rec["_failed_ts"]:
            rec["_failed_ts"] = fail_ts

        rec["_last_ts"] = max(rec["_last_ts"], sent_ts, deliv_ts, read_ts, fail_ts)

    # mezclar estado + pedidos (latest-wins)
    for rec in agg.values():
        cuil = rec.get("CUIL", "")
        per = rec.get("Periodo", "")

        st = estado_map.get((cuil, per))
        if st:
            ts = _safe_int(st.get("ts"))
            if ts >= rec["_estado_ts"]:
                rec["_estado_ts"] = ts
                rec["Respuesta"] = st.get("estado", "") or ""
            rec["_last_ts"] = max(rec["_last_ts"], ts)

        rr = rr_map.get((rec["WhatsApp"], cuil, per))
        if rr:
            ts = _safe_int(rr.get("last"))
            if ts >= rec["_pedido_ts"]:
                rec["_pedido_ts"] = ts
                rec["Pedidos"] = int(rr.get("count") or 0)
                rec["Ultimo_pedido"] = ts_to_str(ts) if ts else ""
            rec["_last_ts"] = max(rec["_last_ts"], ts)

    rows = list(agg.values())

    # ========= Estado ÚNICO con jerarquía =========
    def classify_status(r: dict) -> str:
        resp = (r.get("Respuesta", "") or "").strip().upper()
        sent = r.get("_sent_ts", 0) > 0
        read = r.get("_read_ts", 0) > 0

        if resp == "FIRMADO":
            return "FIRMADO"
        if resp == "OBSERVADO":
            return "OBSERVADO"
        if read and resp == "":
            return "PEND. RESPUESTA"
        if not sent:
            return "PEND. ENVÍO"
        if sent and not read and resp == "":
            return "PEND. LECTURA"
        return resp or "OK"

    for r in rows:
        r["_status"] = classify_status(r)

    def count_status(s):
        return sum(1 for r in rows if r.get("_status") == s)

    c_firm = count_status("FIRMADO")
    c_obs = count_status("OBSERVADO")
    c_presp = count_status("PEND. RESPUESTA")
    c_penv = count_status("PEND. ENVÍO")
    c_plec = count_status("PEND. LECTURA")
    c_ok = count_status("OK")

    # ========= Donut estados =========
    def donut_png(labels, values, title):
        fig = plt.figure(figsize=(3.8, 3.8), dpi=170)
        ax = fig.add_subplot(111)
        ax.pie(values, startangle=90, wedgeprops=dict(width=0.42))
        ax.axis("equal")
        ax.set_title(title, fontsize=11, pad=8)
        ax.legend(labels, loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=8, frameon=False)
        buf = BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", transparent=True)
        plt.close(fig)
        buf.seek(0)
        return buf

    labels_est = ["Firmados", "Observados", "Pend. respuesta", "Pend. envío"]
    values_est = [c_firm, c_obs, c_presp, c_penv]
    if c_plec:
        labels_est.append("Pend. lectura")
        values_est.append(c_plec)
    if c_ok:
        labels_est.append("OK")
        values_est.append(c_ok)

    donut_estados = donut_png(labels_est, values_est, "Estados (jerarquía)")

    # ========= Orden de tabla =========
    order_rank = {
        "FIRMADO": 1,
        "OBSERVADO": 2,
        "PEND. RESPUESTA": 3,
        "PEND. ENVÍO": 4,
        "PEND. LECTURA": 5,
        "OK": 6,
    }
    rows_sorted = sorted(
        rows,
        key=lambda r: (order_rank.get(r.get("_status"), 99), -int(r.get("_last_ts") or 0))
    )

    # ========= PDF =========
    out = BytesIO()
    page_w, page_h = landscape(A4)

    doc = SimpleDocTemplate(
        out,
        pagesize=landscape(A4),
        leftMargin=1.2 * cm,
        rightMargin=1.2 * cm,
        topMargin=1.0 * cm,
        bottomMargin=1.0 * cm,
        title=f"Recibos - {tenant}",
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="TitleCool", fontSize=18, leading=20, spaceAfter=2))
    styles.add(ParagraphStyle(name="SubCool", fontSize=10, leading=12, textColor=colors.HexColor("#6b7280")))
    styles.add(ParagraphStyle(name="HSection", fontSize=12, leading=14, spaceBefore=8, spaceAfter=6))
    styles.add(ParagraphStyle(name="Cell7", fontSize=7, leading=8))
    styles.add(ParagraphStyle(name="Cell7B", fontSize=7, leading=8, fontName="Helvetica-Bold"))

    period_label = period_filter if period_filter else "TODOS"
    gen_ts = time.strftime("%Y-%m-%d %H:%M")

    story = []
    # --- Página 1: solo gráficos / KPIs ---
    story.append(Paragraph(f"📌 Control de Recibos • <b>{tenant}</b>", styles["TitleCool"]))
    story.append(Paragraph(f"Período: <b>{period_label}</b> • Generado: {gen_ts}", styles["SubCool"]))
    story.append(Spacer(1, 0.25 * cm))

    def kpi_box(items):
        data = []
        for lab, val in items:
            data.append([
                Paragraph(f"<font color='#6b7280'>{lab}</font>", styles["BodyText"]),
                Paragraph(f"<b><font size='16'>{val}</font></b>", styles["BodyText"])
            ])
        t = Table(data, colWidths=[4.2*cm, 1.8*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#f6f7fb")),
            ("BOX", (0,0), (-1,-1), 0.6, colors.HexColor("#e5e7eb")),
            ("INNERGRID", (0,0), (-1,-1), 0.4, colors.HexColor("#e5e7eb")),
            ("LEFTPADDING", (0,0), (-1,-1), 10),
            ("RIGHTPADDING", (0,0), (-1,-1), 10),
            ("TOPPADDING", (0,0), (-1,-1), 7),
            ("BOTTOMPADDING", (0,0), (-1,-1), 7),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ]))
        return t

    kpis = kpi_box([
        ("Firmados", c_firm),
        ("Observados", c_obs),
        ("Pend. respuesta", c_presp),
        ("Pend. envío", c_penv),
        ("Pend. lectura", c_plec),
        ("OK", c_ok),
        ("Total", len(rows)),
    ])

    top_grid = Table(
        [[kpis, Image(donut_estados, width=18.0*cm, height=6.2*cm)]],
        colWidths=[7.0*cm, 20.3*cm]
    )
    top_grid.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("TOPPADDING", (0,0), (-1,-1), 0),
        ("BOTTOMPADDING", (0,0), (-1,-1), 0),
    ]))
    story.append(top_grid)

    # --- salto: tabla arranca en hoja 2 ---
    story.append(PageBreak())

    # --- Página 2+: tabla ---
    story.append(Paragraph("📋 Detalle (todas las personas)", styles["HSection"]))

    def clip(s, n):
        s = str(s or "")
        return s if len(s) <= n else s[:n-1] + "…"

    def fmt_ts(ts):
        ts = _safe_int(ts)
        return ts_to_str(ts) if ts else ""

    # ✅ Sin columna "Respuesta" para que no duplique FIRMADO/OBSERVADO
    headers = ["Estado", "Período", "Nombre", "CUIL", "WhatsApp", "Enviado", "Leído", "Pedidos", "Últ. pedido", "Últ. act."]
    table_data = [[Paragraph(h, styles["Cell7B"]) for h in headers]]

    for r in rows_sorted:
        table_data.append([
            Paragraph(r.get("_status",""), styles["Cell7"]),
            Paragraph(r.get("Periodo","") or "", styles["Cell7"]),
            Paragraph(clip(r.get("Nombre","") or "—", 34), styles["Cell7"]),
            Paragraph(clip(r.get("CUIL","") or "—", 16), styles["Cell7"]),
            Paragraph(clip(r.get("WhatsApp","") or "—", 18), styles["Cell7"]),
            Paragraph(fmt_ts(r.get("_sent_ts")), styles["Cell7"]),
            Paragraph(fmt_ts(r.get("_read_ts")), styles["Cell7"]),
            Paragraph(str(int(r.get("Pedidos") or 0)), styles["Cell7"]),
            Paragraph(r.get("Ultimo_pedido","") or "", styles["Cell7"]),
            Paragraph(fmt_ts(r.get("_last_ts")), styles["Cell7"]),
        ])

    # Anchos que entran en A4 apaisado con tus márgenes (~27.3 cm útiles)
    col_widths = [
        2.6*cm,  # Estado
        2.2*cm,  # Período
        6.6*cm,  # Nombre
        3.0*cm,  # CUIL
        3.2*cm,  # WhatsApp
        2.6*cm,  # Enviado
        2.6*cm,  # Leído
        1.5*cm,  # Pedidos
        3.0*cm,  # Últ. pedido
        2.6*cm,  # Últ. act.
    ]

    people_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    people_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#111827")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.35, colors.HexColor("#e5e7eb")),
        ("BOX", (0,0), (-1,-1), 0.8, colors.HexColor("#e5e7eb")),
        ("LEFTPADDING", (0,0), (-1,-1), 4),
        ("RIGHTPADDING", (0,0), (-1,-1), 4),
        ("TOPPADDING", (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("ALIGN", (7,1), (7,-1), "CENTER"),  # Pedidos
    ]))

    for i in range(1, len(table_data)):
        if i % 2 == 0:
            people_table.setStyle(TableStyle([("BACKGROUND", (0,i), (-1,i), colors.HexColor("#f9fafb"))]))

    story.append(people_table)

    def on_page(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#9ca3af"))
        canvas.drawRightString(page_w - 1.2*cm, 0.8*cm, f"Página {doc_obj.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
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


def list_verifications(tenant: str, q: str = ""):
    conn = get_db_connection()
    cur = conn.cursor()

    q = (q or "").strip().lower()
    params = [tenant]

    sql = """
      SELECT id, tenant, cuil, to_whatsapp, nombre, dni_last4, verified_at, updated_at
      FROM verifications
      WHERE tenant = ?
    """

    if q:
        sql += """
          AND (
            lower(cuil) LIKE ?
            OR lower(to_whatsapp) LIKE ?
            OR lower(ifnull(nombre,'')) LIKE ?
            OR lower(ifnull(dni_last4,'')) LIKE ?
          )
        """
        like = f"%{q}%"
        params += [like, like, like, like]

    sql += " ORDER BY updated_at DESC, verified_at DESC LIMIT 200"

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

def add_pending_view(to_whatsapp: str, tenant: str, cuil: str, period: str, origin: str = "INITIAL"):
    now = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
      INSERT INTO pending_views
        (to_whatsapp, tenant, cuil, period, created_at, step, dni_attempts, origin)
      VALUES (?, ?, ?, ?, ?, 'READY', 0, ?)
      ON CONFLICT(to_whatsapp) DO UPDATE SET
        tenant=excluded.tenant,
        cuil=excluded.cuil,
        period=excluded.period,
        created_at=excluded.created_at,
        step='READY',
        dni_attempts=0,
        origin=excluded.origin
    """, (to_whatsapp, tenant, cuil, period, now, origin))

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
        COALESCE(dni_attempts, 0) AS dni_attempts,
        COALESCE(origin, 'INITIAL') AS origin
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

    # Si ya está FIRMADO, no se cambia nunca más
    cur.execute("""
        SELECT estado FROM recibo_estado
        WHERE tenant=? AND cuil=? AND period=?
    """, (tenant, cuil, period))
    row = cur.fetchone()
    if row and row[0] == "FIRMADO":
        conn.close()
        return

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

def already_sent_template(tenant: str, cuil: str, period: str, to_whatsapp: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT 1
            FROM message_status
            WHERE tenant=? AND cuil=? AND period=? AND to_whatsapp=? AND kind='template'
            LIMIT 1
        """, (tenant, cuil, period, to_whatsapp))

        return cur.fetchone() is not None
    finally:
        conn.close()



@app.get("/admin")
def admin_home():
    auth = require_admin()
    if auth:
        return auth

    token = (request.args.get("token") or "").strip()
    tenants = load_tenants(force=True) or []

    html = []
    html.append("""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Admin</title>
  <style>
    :root{
      --bg:#0b1220;
      --muted:#9fb2d0;
      --text:#eaf0ff;
      --line:rgba(255,255,255,.08);
      --shadow: 0 10px 25px rgba(0,0,0,.25);
      --radius:14px;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      font-family:var(--sans);
      background: radial-gradient(1200px 700px at 20% -20%, rgba(90,167,255,.25), transparent 60%),
                  radial-gradient(1200px 700px at 90% 0%, rgba(52,211,153,.18), transparent 55%),
                  var(--bg);
      color:var(--text);
    }
    .wrap{max-width:1100px;margin:0 auto;padding:22px}
    .topbar{
      display:flex;gap:14px;align-items:center;justify-content:space-between;
      padding:16px 18px;border:1px solid var(--line);border-radius:var(--radius);
      background:rgba(255,255,255,.03);box-shadow:var(--shadow);
      position:sticky;top:12px;backdrop-filter: blur(8px); z-index:10;
    }
    h2{margin:0;font-size:18px;letter-spacing:.2px}
    .muted{color:var(--muted);font-size:13px}
    code{font-family:var(--mono);font-size:12px;background:rgba(255,255,255,.06);padding:2px 6px;border-radius:8px;border:1px solid var(--line)}
    .card{
      margin-top:14px;
      border:1px solid var(--line);
      background:rgba(255,255,255,.03);
      border-radius:var(--radius);
      padding:16px;
      box-shadow:var(--shadow);
    }
    .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;justify-content:space-between}
    input[type="text"]{
      background:rgba(0,0,0,.25);
      border:1px solid var(--line);
      color:var(--text);
      padding:10px 10px;
      border-radius:12px;
      outline:none;
      min-width: 240px;
    }
    .grid{
      margin-top:12px;
      display:grid;
      grid-template-columns: repeat(3, 1fr);
      gap:12px;
    }
    @media(max-width:960px){ .grid{grid-template-columns:1fr} .topbar{flex-direction:column;align-items:flex-start}}
    .tile{
      border:1px solid var(--line);
      background:rgba(255,255,255,.02);
      border-radius:14px;
      padding:14px;
      transition:transform .05s ease, background .2s ease;
    }
    .tile:hover{transform:translateY(-1px);background:rgba(255,255,255,.04)}
    .tile h3{margin:0 0 6px 0;font-size:15px}
    .btn{
      display:inline-flex;align-items:center;justify-content:center;
      gap:8px;
      padding:10px 12px;
      border-radius:12px;
      border:1px solid var(--line);
      background:linear-gradient(180deg, rgba(255,255,255,.06), rgba(255,255,255,.02));
      cursor:pointer;
      font-weight:600;
      font-size:13px;
      text-decoration:none;
      color:var(--text);
    }
    .btn.secondary{background:rgba(255,255,255,.02)}
    .btn.small{padding:7px 10px;font-size:12px;border-radius:10px}
    .actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}
    .warn{
      border:1px solid rgba(251,191,36,.35);
      background: rgba(251,191,36,.10);
      padding:10px 12px;border-radius:12px;
      color: var(--text);
    }
  </style>
</head>
<body>
<div class="wrap">
""")

    html.append("<div class='topbar'>")
    html.append("<div>")
    html.append("<h2>Panel Admin</h2>")
    html.append("<div class='muted'>Elegí la empresa para abrir el panel.</div>")
    html.append("</div>")
    html.append(f"<div class='muted'>Token: <code>{esc(token)}</code></div>")
    html.append("</div>")

    # Warning si falta master
    if not EMPRESAS_FILE_ID:
        html.append("<div class='card'><div class='warn'>⚠️ Falta <code>EMPRESAS_FILE_ID</code> en ENV.</div></div>")

    if not tenants:
        html.append("<div class='card'>")
        html.append("<h3 style='margin:0 0 6px 0'>No hay empresas detectadas en el Excel maestro</h3>")
        html.append("<div class='muted'>Encabezados esperados: <code>Empresa</code> | <code>Envios_File_ID</code> | <code>Drive_Root_ID</code></div>")
        html.append("</div>")
    else:
        html.append("<div class='card'>")
        html.append("<div class='row'>")
        html.append("<div>")
        html.append("<h3 style='margin:0 0 6px 0'>Empresas</h3>")
        html.append(f"<div class='muted'>Detectadas: <b>{len(tenants)}</b></div>")
        html.append("</div>")
        html.append("<div>")
        html.append("<label class='muted' style='display:block;margin-bottom:6px'>Buscar</label>")
        html.append("<input id='q' type='text' placeholder='Escribí para filtrar...'>")
        html.append("</div>")
        html.append("</div>")

        html.append("<div class='grid' id='grid'>")
        for t in tenants:
            slug = t["slug"]
            name = t.get("display_name") or slug
            panel_url = f"/admin/panel?tenant={esc(slug)}&token={esc(token)}"
            test_url = f"/admin/send_test?tenant={esc(slug)}&token={esc(token)}"

            html.append(f"""
              <div class="tile" data-name="{esc(name).lower()} {esc(slug).lower()}">
                <h3>{esc(name)}</h3>
                <div class="muted">tenant: <code>{esc(slug)}</code></div>
                <div class="actions">
                  <a class="btn" href="{panel_url}">Abrir panel →</a>
                  <a class="btn secondary small" href="{test_url}">🧪 Prueba</a>
                </div>
              </div>
            """)

        html.append("</div>")  # grid

        # JS filtro
        html.append("""
        <script>
          (function(){
            const q = document.getElementById('q');
            const tiles = Array.from(document.querySelectorAll('#grid .tile'));
            function apply(){
              const s = (q.value || '').trim().toLowerCase();
              for(const t of tiles){
                const hay = (t.getAttribute('data-name') || '');
                t.style.display = (!s || hay.includes(s)) ? '' : 'none';
              }
            }
            q.addEventListener('input', apply);
          })();
        </script>
        """)

        html.append("</div>")  # card

    html.append("</div></body></html>")
    return Response("".join(html), mimetype="text/html")


from flask import send_file
import pandas as pd
import io

from datetime import datetime, timezone


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
    DELETE FROM verifications
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
    """, (tenant,))
    pv_rows = cur.fetchall()

    last_period_by_whatsapp = {}
    for r in pv_rows:
        w = (r["to_whatsapp"] or "").strip()
        p = norm_period_label(r["period"] or "")
        if w and p:
            last_period_by_whatsapp[w] = p

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

        if not period_norm and wpp:
            period_norm = last_period_by_whatsapp.get(wpp, "")

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

from googleapiclient.errors import HttpError
import time

def _is_retryable_drive_error(e: HttpError) -> bool:
    status = getattr(e.resp, "status", None)
    return status in (429, 500, 502, 503, 504)

def find_pdf_with_retry(tenant, cuil, period, tries=4):
    last = None
    for i in range(tries):
        try:
            return find_pdf_file_id_for_cuil_period(tenant, cuil, period)
        except HttpError as e:
            last = e
            if _is_retryable_drive_error(e):
                time.sleep(0.6 * (2 ** i))
                continue
            raise
    # si agotó reintentos, devolvemos None (no tumbamos la cola)
    print("DRIVE RETRY EXHAUSTED:", tenant, cuil, period, last)
    return None

def get_queue_stats(tenant: str, period: str) -> dict:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
      SELECT status, COUNT(*) as n
      FROM template_send_queue
      WHERE tenant=? AND period=?
      GROUP BY status
    """, (tenant, period))
    d = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()
    # asegurar claves
    for k in ("PENDING","SENT","FAILED","SKIPPED"):
        d.setdefault(k, 0)
    return d


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

    force = (request.args.get("refresh") in ("1", "true", "yes", "on"))
    envios_rows = load_envios_rows(tenant, force=force) or []

    panel_period = (request.args.get("period") or "").strip()

    # Reportes: periodos disponibles
    selected_period = panel_period
    period_folders = list_tenant_period_folders(tenant)  # ['01-2026','12-2025',...]
    period_labels = []
    for p in period_folders:
        lbl = period_folder_to_label(p)  # '01/2026'
        if lbl:
            period_labels.append(lbl)
    if not selected_period and period_labels:
        selected_period = period_labels[0]

    period_q = quote(selected_period or "", safe="")

    # Cola stats
    stats = None
    if panel_period:
        stats = get_queue_stats(tenant, panel_period)

    # Verificaciones
    verifs = get_verifications_rows(tenant)

    html = []
    html.append("""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Panel empresa</title>
  <style>
    :root{
      --bg:#0b1220;
      --card:#0f1b33;
      --muted:#9fb2d0;
      --text:#eaf0ff;
      --line:rgba(255,255,255,.08);
      --accent:#5aa7ff;
      --ok:#34d399;
      --warn:#fbbf24;
      --bad:#fb7185;
      --btn:#14264a;
      --btn2:#1a2f5a;
      --shadow: 0 10px 25px rgba(0,0,0,.25);
      --radius:14px;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji","Segoe UI Emoji";
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      font-family:var(--sans);
      background: radial-gradient(1200px 700px at 20% -20%, rgba(90,167,255,.25), transparent 60%),
                  radial-gradient(1200px 700px at 90% 0%, rgba(52,211,153,.18), transparent 55%),
                  var(--bg);
      color:var(--text);
    }
    a{color:inherit;text-decoration:none}
    .wrap{max-width:1100px;margin:0 auto;padding:22px}
    .topbar{
      display:flex;gap:14px;align-items:center;justify-content:space-between;
      padding:16px 18px;border:1px solid var(--line);border-radius:var(--radius);
      background:rgba(255,255,255,.03);box-shadow:var(--shadow);
      position:sticky;top:12px;backdrop-filter: blur(8px); z-index:10;
    }
    .title{display:flex;flex-direction:column;gap:4px}
    .title h2{margin:0;font-size:18px;letter-spacing:.2px}
    .subtitle{font-size:13px;color:var(--muted)}
    .pill{
      display:inline-flex;align-items:center;gap:8px;
      font-size:12px;color:var(--muted)
    }
    code{font-family:var(--mono);font-size:12px;background:rgba(255,255,255,.06);padding:2px 6px;border-radius:8px;border:1px solid var(--line)}
    .grid{
      margin-top:16px;
      display:grid;
      grid-template-columns: 1.2fr .8fr;
      gap:14px;
    }
    @media(max-width:960px){ .grid{grid-template-columns:1fr} .topbar{flex-direction:column;align-items:flex-start}}
    .card{
      border:1px solid var(--line);
      background:rgba(255,255,255,.03);
      border-radius:var(--radius);
      padding:16px;
      box-shadow:var(--shadow);
    }
    .card h3{margin:0 0 10px 0;font-size:15px}
    .muted{color:var(--muted);font-size:13px}
    .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
    .btn{
      display:inline-flex;align-items:center;justify-content:center;
      gap:8px;
      padding:10px 12px;
      border-radius:12px;
      border:1px solid var(--line);
      background:linear-gradient(180deg, rgba(255,255,255,.06), rgba(255,255,255,.02));
      cursor:pointer;
      font-weight:600;
      font-size:13px;
      transition:transform .05s ease, background .2s ease;
    }
    .btn:hover{transform:translateY(-1px);background:linear-gradient(180deg, rgba(255,255,255,.08), rgba(255,255,255,.03))}
    .btn.secondary{background:rgba(255,255,255,.02)}
    .btn.danger{border-color:rgba(251,113,133,.35); background:rgba(251,113,133,.10)}
    .btn.small{padding:7px 10px;font-size:12px;border-radius:10px}
    input[type="text"], input[type="number"], select{
      background:rgba(0,0,0,.25);
      border:1px solid var(--line);
      color:var(--text);
      padding:10px 10px;
      border-radius:12px;
      outline:none;
      min-width: 180px;
    }
    label{font-size:12px;color:var(--muted)}
    .badge{
      display:inline-flex;align-items:center;gap:6px;
      padding:6px 10px;border-radius:999px;
      border:1px solid var(--line);
      background:rgba(255,255,255,.03);
      font-size:12px;color:var(--muted);
    }
    .badge b{color:var(--text)}
    .badge.ok{border-color:rgba(52,211,153,.35)}
    .badge.warn{border-color:rgba(251,191,36,.35)}
    .badge.bad{border-color:rgba(251,113,133,.35)}
    .sep{height:1px;background:var(--line);margin:12px 0}
    table{width:100%;border-collapse:separate;border-spacing:0}
    th, td{
      padding:10px 10px;
      border-bottom:1px solid var(--line);
      font-size:13px;
      vertical-align:middle;
    }
    th{font-size:12px;color:var(--muted);text-align:left}
    tr:hover td{background:rgba(255,255,255,.02)}
    .table-wrap{overflow:auto;border:1px solid var(--line);border-radius:14px}
    .right{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
    .hint{font-size:12px;color:var(--muted);margin-top:6px}
    .kpi{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
    .mono{font-family:var(--mono)}
  </style>
</head>
<body>
""")

    html.append("<div class='wrap'>")

    html.append("<div class='topbar'>")
    html.append("<div class='title'>")
    html.append("<h2>Panel empresa</h2>")
    html.append(f"<div class='subtitle'><b>Empresa:</b> {esc(t.get('display_name',''))} · <span class='pill'>slug <code>{esc(t.get('slug',''))}</code></span></div>")
    html.append("</div>")
    html.append("<div class='right'>")
    html.append(f"<a class='btn secondary' href='/admin?token={esc(token)}'>← Volver</a>")
    html.append(f"<a class='btn' href='/admin/send_test?tenant={esc(tenant)}&token={esc(token)}'>🧪 Envío de prueba</a>")
    html.append("</div>")
    html.append("</div>")

    html.append("<div class='grid'>")

    # Columna izquierda: acciones principales
    html.append("<div class='card'>")
    html.append("<h3>📩 Envío masivo</h3>")
    html.append("<div class='muted'>Enviá la plantilla a toda la empresa y gestioná la cola por período.</div>")
    html.append("<div class='sep'></div>")

    # Start queue
    html.append("<form method='post' action='/admin/send_template_queue_start'>")
    html.append(f"<input type='hidden' name='token' value='{esc(token)}'>")
    html.append(f"<input type='hidden' name='tenant' value='{esc(tenant)}'>")
    html.append("<div class='row'>")
    html.append("<div>")
    html.append("<label>Período (mm/aaaa)</label><br>")
    html.append("<select name='period' id='massPeriod' required>")
    for lbl in period_labels:
        sel = "selected" if lbl == (selected_period or "") else ""
        html.append(f"<option value='{esc(lbl)}' {sel}>{esc(lbl)}</option>")
    html.append("</select>")
    html.append("</div>")
    html.append("<div>")
    html.append("<label>Límite (0 = todos)</label><br>")
    html.append("<input type='number' name='limit' id='massLimit' min='0' value='0'>")
    html.append("</div>")
    html.append("</div>")
    html.append("<input type='hidden' name='require_pdf' value='true'>")
    html.append("<div style='margin-top:10px'>")
    html.append("<button class='btn' type='submit'>🚀 Encolar envío a toda la empresa</button>")
    html.append("</div>")
    html.append("</form>")
    html.append("""
    <div class="sep"></div>
    <h3>👀 Preview de destinatarios</h3>
    <div id="previewMeta" class="muted">Cargando...</div>

    <div class="table-wrap" style="margin-top:10px">
    <table>
        <thead>
        <tr><th>Nombre</th><th>WhatsApp</th><th>CUIL</th></tr>
        </thead>
        <tbody id="previewBody">
        <tr><td colspan="3" class="muted">Cargando...</td></tr>
        </tbody>
    </table>
    </div>

    <div class="hint">Se actualiza al cambiar Período o Límite.</div>

    <script>
    (async function(){
    const tenant = %s;
    const token  = %s;

    const elPeriod = document.getElementById('massPeriod');
    const elLimit  = document.getElementById('massLimit');
    const meta = document.getElementById('previewMeta');
    const body = document.getElementById('previewBody');

    function escapeHtml(s){
        return String(s ?? '')
        .replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')
        .replaceAll('"','&quot;').replaceAll("'","&#39;");
    }

    async function refresh(){
        const period = (elPeriod.value || '').trim();
        const limit  = (elLimit.value || '0').trim();

        meta.textContent = "Cargando preview...";
        body.innerHTML = "<tr><td colspan='3' class='muted'>Cargando...</td></tr>";

        const qs = new URLSearchParams({tenant, token, period, limit, require_pdf:'true'});

        try{
        const r = await fetch('/admin/send_template_preview?' + qs.toString(), {
            headers: {'Accept': 'application/json'}
        });
        const j = await r.json();
        if(!j.ok){
            meta.textContent = "Error: " + (j.error || "preview");
            body.innerHTML = "<tr><td colspan='3' class='muted'>No disponible</td></tr>";
            return;
        }

        meta.textContent = `Coinciden: ${j.total_match} · Mostrando: ${j.showing}` +
            ((j.limit && j.limit > 0) ? ` · Limit: ${j.limit}` : "");

        const rows = j.recipients || [];
        if(rows.length === 0){
            body.innerHTML = "<tr><td colspan='3' class='muted'>No hay destinatarios.</td></tr>";
            return;
        }

        body.innerHTML = rows.map(x => (
            `<tr>
            <td>${escapeHtml(x.nombre)}</td>
            <td>${escapeHtml(x.whatsapp)}</td>
            <td>${escapeHtml(x.cuil)}</td>
            </tr>`
        )).join('');

        }catch(e){
        meta.textContent = "Error cargando preview";
        body.innerHTML = "<tr><td colspan='3' class='muted'>Error</td></tr>";
        }
    }

    elPeriod.addEventListener('change', refresh);
    elLimit.addEventListener('input', () => {
        clearTimeout(window.__pv_t);
        window.__pv_t = setTimeout(refresh, 250);
    });

    refresh();
    })();
    </script>
    """ % (repr(tenant), repr(token)))

    # Queue section
    html.append("<div class='sep'></div>")
    html.append("<h3>⏳ Cola de envíos</h3>")

    if panel_period and stats:
        html.append(f"<div class='muted'>Período cola: <b>{esc(panel_period)}</b></div>")
        html.append("<div class='kpi'>")
        html.append(f"<span class='badge warn'>PENDING <b>{stats['PENDING']}</b></span>")
        html.append(f"<span class='badge ok'>SENT <b>{stats['SENT']}</b></span>")
        html.append(f"<span class='badge'>SKIPPED <b>{stats['SKIPPED']}</b></span>")
        html.append(f"<span class='badge bad'>FAILED <b>{stats['FAILED']}</b></span>")
        html.append("</div>")

        html.append("<form method='post' action='/admin/send_template_queue_tick' style='margin-top:10px;'>")
        html.append(f"<input type='hidden' name='token' value='{esc(token)}'>")
        html.append(f"<input type='hidden' name='tenant' value='{esc(tenant)}'>")
        html.append(f"<input type='hidden' name='period' value='{esc(panel_period)}'>")
        html.append("<div class='row'>")
        html.append("<div>")
        html.append("<label>Batch size</label><br>")
        html.append("<input type='number' name='batch_size' min='1' max='50' value='10'>")
        html.append("</div>")
        html.append("<div style='margin-top:18px'>")
        html.append("<button class='btn' type='submit'>▶️ Procesar ahora</button>")
        html.append("</div>")
        html.append("</div>")
        html.append("</form>")

        html.append("<div class='hint'>Cron URL (POST) sugerida:</div>")
        html.append(f"<div class='hint mono'><code>/admin/send_template_queue_tick?tenant={esc(tenant)}&period={esc(panel_period)}&batch_size=10&token={esc(token)}&mode=json</code></div>")
    else:
        html.append("<div class='muted'>Elegí un período en Reportes para ver la cola y poder procesarla.</div>")

    html.append("</div>")  # fin card izquierda

    # Columna derecha: reportes + herramientas rápidas
    html.append("<div class='card'>")
    html.append("<h3>📊 Reportes</h3>")
    html.append("<div class='muted'>Descargá reportes filtrando por período.</div>")
    html.append("<div class='sep'></div>")

    html.append("<form method='get' action='/admin/panel'>")
    html.append(f"<input type='hidden' name='token' value='{esc(token)}'>")
    html.append(f"<input type='hidden' name='tenant' value='{esc(tenant)}'>")
    html.append("<label>Período</label><br>")
    html.append("<div class='row' style='margin-top:6px'>")
    html.append("<select name='period'>")
    html.append("<option value=''>-- Todos / Sin filtro --</option>")
    for lbl in period_labels:
        sel = "selected" if lbl == selected_period else ""
        html.append(f"<option value='{esc(lbl)}' {sel}>{esc(lbl)}</option>")
    html.append("</select>")
    html.append("<button class='btn secondary' type='submit'>Aplicar</button>")
    html.append("</div>")
    html.append("</form>")

    html.append("<div class='sep'></div>")
    html.append("<div class='row'>")
    html.append(
        f"<a class='btn' href='/admin/report_recibos.xlsx?tenant={esc(tenant)}&period={period_q}&token={esc(token)}'>📄 Reporte recibos (XLSX)</a>"
    )
    html.append(
        f"<a class='btn' href='/admin/report_recibos.pdf?tenant={esc(tenant)}&period={period_q}&token={esc(token)}'>🧾 Informe (PDF)</a>"
    )
    html.append(
        f"<a class='btn secondary' href='/admin/report_envios.csv?tenant={esc(tenant)}&token={esc(token)}'>📤 Envíos (CSV)</a>"
    )
    html.append("</div>")

    html.append("<div class='sep'></div>")
    html.append("<h3>🔎 Buscar períodos por CUIL</h3>")
    html.append(f"""
      <form method="get" action="/admin/periodos">
        <input type="hidden" name="token" value="{esc(token)}">
        <input type="hidden" name="tenant" value="{esc(tenant)}">
        <label>CUIL</label><br>
        <div class="row" style="margin-top:6px">
          <input type="text" name="cuil" placeholder="xx-xxxxxxxx-x" required>
          <button class="btn secondary" type="submit">Buscar</button>
        </div>
      </form>
    """)

    html.append("<div class='sep'></div>")
    html.append("<h3>🧹 Reset</h3>")
    html.append("<div class='muted'>Borra <code>pending_views</code> y <code>recibo_estado</code> para esta empresa (y período si lo completás).</div>")
    html.append(f"""
      <form method="post" action="/admin/reset" onsubmit="return confirm('¿Seguro? Esto borra pending y estados.');" style="margin-top:10px">
        <input type="hidden" name="token" value="{esc(token)}">
        <input type="hidden" name="tenant" value="{esc(tenant)}">
        <label>Período (opcional, mm/aaaa)</label><br>
        <div class="row" style="margin-top:6px">
          <input type="text" name="period" placeholder="01/2026">
          <button class="btn danger" type="submit">Resetear</button>
        </div>
      </form>
    """)

    html.append("</div>")  # fin card derecha

    html.append("</div>")  # fin grid

    # Verificaciones
    html.append("<div class='card' style='margin-top:14px'>")
    html.append("<div class='row' style='justify-content:space-between'>")
    html.append("<div>")
    html.append("<h3>✅ Verificaciones</h3>")
    html.append(f"<div class='muted'>Registros: <b>{len(verifs)}</b></div>")
    html.append("</div>")
    html.append("</div>")
    html.append("<div class='sep'></div>")

    if verifs:
        html.append(f"""
          <form id="bulkForm" method="post" action="/admin/verifications_delete_bulk"
                onsubmit="return confirm('¿Borrar verificaciones seleccionadas?');">
            <input type="hidden" name="token" value="{esc(token)}">
            <input type="hidden" name="tenant" value="{esc(tenant)}">
          </form>
        """)

        html.append("<div class='table-wrap'>")
        html.append("<table>")
        html.append("""
          <thead>
            <tr>
              <th></th>
              <th>CUIL</th>
              <th>Nombre</th>
              <th>WhatsApp</th>
              <th>Verificado</th>
              <th>Acciones</th>
            </tr>
          </thead>
          <tbody>
        """)

        for r in verifs[:500]:
            key = f"{r['cuil']}|{r['to_whatsapp']}"
            html.append("<tr>")
            # checkbox asociado al bulkForm sin anidarlo
            html.append(f"<td><input form='bulkForm' type='checkbox' name='keys' value='{esc(key)}'></td>")
            html.append(f"<td>{esc(r['cuil'])}</td>")
            html.append(f"<td>{esc(r.get('nombre','') or '')}</td>")
            html.append(f"<td>{esc(r['to_whatsapp'])}</td>")
            html.append(f"<td>{esc(ts_str(r.get('verified_at')))}</td>")
            html.append("<td class='row'>")

            # form independiente por fila
            html.append(f"""
              <form method="post" action="/admin/verifications_delete"
                    onsubmit="return confirm('¿Borrar verificación?');" style="margin:0">
                <input type="hidden" name="token" value="{esc(token)}">
                <input type="hidden" name="tenant" value="{esc(tenant)}">
                <input type="hidden" name="cuil" value="{esc(r['cuil'])}">
                <input type="hidden" name="to_whatsapp" value="{esc(r['to_whatsapp'])}">
                <button class="btn small danger" type="submit">Borrar</button>
              </form>
            """)
            html.append("</td>")
            html.append("</tr>")

        html.append("</tbody></table></div>")
        html.append("<div style='margin-top:10px'>")
        html.append("<button class='btn danger' form='bulkForm' type='submit'>🗑️ Borrar seleccionados</button>")
        html.append("</div>")
    else:
        html.append("<div class='muted'>No hay verificaciones cargadas.</div>")

    html.append("<div class='sep'></div>")

    # Importar verificaciones
    html.append(f"""
      <h3>📥 Importar verificaciones</h3>
      <div class="muted">Subí un Excel con columnas: <code>cuil</code>, <code>whatsapp</code> (o teléfono). Opcional: <code>dni</code>.</div>
      <form method="post" action="/admin/verifications_import" enctype="multipart/form-data" style="margin-top:10px">
        <input type="hidden" name="token" value="{esc(token)}">
        <input type="hidden" name="tenant" value="{esc(tenant)}">
        <div class="row">
          <input type="file" name="file" accept=".xlsx" required>
          <button class="btn secondary" type="submit">Importar</button>
        </div>
      </form>
    """)

    html.append("</div>")  # fin card verifs

    # Preview envíos
    html.append("<div class='card' style='margin-top:14px'>")
    html.append("<div class='row' style='justify-content:space-between'>")
    html.append("<div>")
    html.append("<h3>👀 Preview Excel de envíos</h3>")
    html.append(f"<div class='muted'>Filas: <b>{len(envios_rows)}</b></div>")
    html.append("</div>")
    html.append(f"<a class='btn secondary' href='/admin/panel?tenant={esc(tenant)}&token={esc(token)}&refresh=1&period={esc(selected_period or '')}'>🔄 Refrescar</a>")
    html.append("</div>")
    html.append("<div class='sep'></div>")

    sample = envios_rows[:10]
    if sample:
        cols = list(sample[0].keys())
        html.append("<div class='table-wrap'>")
        html.append("<table>")
        html.append("<thead><tr>" + "".join(f"<th>{esc(c)}</th>" for c in cols) + "</tr></thead>")
        html.append("<tbody>")
        for r in sample:
            html.append("<tr>" + "".join(f"<td>{esc(str(r.get(c,'')))}</td>" for c in cols) + "</tr>")
        html.append("</tbody></table></div>")
    else:
        html.append("<div class='muted'>No se pudo leer el Excel de envíos o está vacío.</div>")

    html.append("</div>")  # fin preview card

    html.append("</div></body></html>")
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

def label_to_period_folder(label: str) -> str:
    # "01/2026" -> "01-2026"
    label = (label or "").strip()
    m = re.match(r"^(\d{2})/(\d{4})$", label)
    if not m:
        return ""
    return f"{m.group(1)}-{m.group(2)}"


def _drive_find_child_folder_id(service, parent_id: str, folder_name: str) -> str:
    """
    Busca una carpeta hija por nombre exacto y devuelve su id (o "").
    Usa tu helper _drive_list_children.
    """
    children = _drive_list_children(
        service,
        parent_id=parent_id,
        mime_type=FOLDER_MIME,
        page_size=500
    )
    for c in children:
        if (c.get("name") or "").strip() == folder_name:
            return (c.get("id") or "").strip()
    return ""


def _drive_child_file_exists(service, parent_id: str, filename: str) -> bool:
    """
    True si existe un archivo con nombre exacto dentro de parent_id.
    Para no listar 500 siempre, podés implementar query directa,
    pero con tus helpers lo hacemos por listado.
    """
    children = _drive_list_children(
        service,
        parent_id=parent_id,
        mime_type=None,   # archivos (no filtramos por mime)
        page_size=500
    )
    for c in children:
        if (c.get("name") or "").strip() == filename:
            return True
    return False


def get_tenant_period_folder_id(tenant_slug: str, period_label: str) -> str:
    """
    Devuelve el folder_id del período dentro del root del tenant.
    period_label esperado: "MM/YYYY"
    """
    t = get_tenant(tenant_slug)
    if not t:
        return ""

    root_id = (t.get("drive_root_id") or t.get("recibos_root_id") or "").strip()
    if not root_id:
        return ""

    period_folder_name = label_to_period_folder(period_label)  # "MM-YYYY"
    if not period_folder_name:
        return ""

    service = drive_service()
    return _drive_find_child_folder_id(service, root_id, period_folder_name)


import re


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

def queue_template_send(tenant: str, period: str, to_whatsapp: str, cuil: str,
                        nombre: str = "", require_pdf: bool = True) -> str:
    """
    Devuelve: 'inserted', 'requeued', 'noop'
    - inserted: no existía
    - requeued: existía SKIPPED/FAILED y la volvimos a PENDING
    - noop: existía y estaba PENDING/SENT (no tocamos)
    """
    now = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
      INSERT INTO template_send_queue
        (tenant, period, to_whatsapp, cuil, nombre, require_pdf, status, created_at, updated_at, error)
      VALUES
        (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, '')
      ON CONFLICT(tenant, period, to_whatsapp, cuil) DO UPDATE SET
        nombre=excluded.nombre,
        require_pdf=excluded.require_pdf,
        updated_at=excluded.updated_at,
        status=CASE
          WHEN template_send_queue.status IN ('SKIPPED','FAILED') THEN 'PENDING'
          ELSE template_send_queue.status
        END,
        error=CASE
          WHEN template_send_queue.status IN ('SKIPPED','FAILED') THEN ''
          ELSE template_send_queue.error
        END
    """, (tenant, period, to_whatsapp, cuil, (nombre or ""), 1 if require_pdf else 0, now, now))

    # Determinar resultado
    cur.execute("""
      SELECT status FROM template_send_queue
      WHERE tenant=? AND period=? AND to_whatsapp=? AND cuil=?
      LIMIT 1
    """, (tenant, period, to_whatsapp, cuil))
    status = (cur.fetchone() or [""])[0]

    conn.commit()
    conn.close()

    # heurística simple
    if status == "PENDING":
        # pudo ser inserted o requeued; si querés exactitud, guardá rowcount antes/después
        return "requeued_or_inserted"
    return "noop"


def count_queue_status(tenant: str, period: str) -> dict:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
      SELECT status, COUNT(*) as n
      FROM template_send_queue
      WHERE tenant=? AND period=?
      GROUP BY status
    """, (tenant, period))
    d = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()
    return d


@app.post("/admin/send_template_queue_start")
@admin_required
def admin_send_template_queue_start():
    token = _get_admin_token_from_request()

    tenant = (request.form.get("tenant") or "").strip().lower()
    period = (request.form.get("period") or "").strip()
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

    try:
        limit = int(request.form.get("limit") or "0")
    except ValueError:
        limit = 0
    if limit > 0:
        rows = rows[:limit]

    enqueued = 0
    skipped_bad = 0
    skipped_dup = 0

    for r in rows:
        nombre = str(r.get(c_nombre, "")).strip() if c_nombre else ""
        tel_raw = str(r.get(c_tel, "")).strip()
        arch_raw = str(r.get(c_arch, "")).strip()

        if not tel_raw or not arch_raw:
            skipped_bad += 1
            continue

        to_whatsapp = normalize_whatsapp(tel_raw)
        if not to_whatsapp:
            skipped_bad += 1
            continue

        # CUIL desde archivo
        try:
            cuil = strip_pdf(arch_raw)
        except Exception:
            skipped_bad += 1
            continue

        cuil_digits = norm_digits(cuil)
        if len(cuil_digits) != 11:
            skipped_bad += 1
            continue
        cuil = cuil_digits

        ok = queue_template_send(
            tenant=tenant,
            period=period,
            to_whatsapp=to_whatsapp,
            cuil=cuil,
            nombre=nombre,
            require_pdf=require_pdf,
        )
        if ok:
            enqueued += 1
        else:
            skipped_dup += 1

    stats = count_queue_status(tenant, period)
    return redirect(
        f"/admin/panel?tenant={tenant}&token={token}&msg=queue_enqueued"
        f"&period={period}&enqueued={enqueued}&dup={skipped_dup}&bad={skipped_bad}"
        f"&pending={stats.get('PENDING',0)}&sent={stats.get('SENT',0)}"
        f"&failed={stats.get('FAILED',0)}&skipped={stats.get('SKIPPED',0)}"
    )

def _fetch_queue_batch(tenant: str, period: str, batch_size: int = 10) -> list[dict]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
      SELECT id, tenant, period, to_whatsapp, cuil, nombre, require_pdf
      FROM template_send_queue
      WHERE tenant=? AND period=? AND status='PENDING'
      ORDER BY id ASC
      LIMIT ?
    """, (tenant, period, batch_size))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


def _mark_queue_row(row_id: int, status: str, error: str = "", sent_sid: str | None = None):
    now = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
      UPDATE template_send_queue
      SET status=?, error=?, updated_at=?, sent_sid=COALESCE(?, sent_sid),
          sent_at=CASE WHEN ? IS NOT NULL THEN ? ELSE sent_at END
      WHERE id=?
    """, (status, (error or ""), now, sent_sid, sent_sid, now, row_id))
    conn.commit()
    conn.close()


@app.post("/admin/send_template_queue_tick")
@admin_required
def admin_send_template_queue_tick():
    token = _get_admin_token_from_request()

    tenant = (request.form.get("tenant") or request.args.get("tenant") or "").strip().lower()
    period = (request.form.get("period") or request.args.get("period") or "").strip()

    try:
        batch_size = int(request.form.get("batch_size") or request.args.get("batch_size") or "10")
    except ValueError:
        batch_size = 10
    batch_size = max(1, min(batch_size, 50))  # cap de seguridad

    if not tenant:
        return Response("Falta tenant", status=400)
    if not period:
        return Response("Falta period", status=400)

    rows = _fetch_queue_batch(tenant, period, batch_size=batch_size)

    processed = 0
    sent = 0
    skipped = 0
    failed = 0

    for r in rows:
        processed += 1
        row_id = r["id"]
        to_whatsapp = (r["to_whatsapp"] or "").strip()
        cuil = (r["cuil"] or "").strip()
        nombre = (r.get("nombre") or "").strip()
        require_pdf = bool(r.get("require_pdf", 1))

        try:
            # 1) Si require_pdf, validamos que exista PDF
            if require_pdf:
                pdf_file_id = find_pdf_with_retry(tenant, cuil, period)
                if not pdf_file_id:
                    _mark_queue_row(row_id, "SKIPPED", error="NO_PDF")
                    skipped += 1
                    continue

            # 2) Si ya lo mandamos, skip
            if already_sent_template(tenant, cuil, period, to_whatsapp):
                _mark_queue_row(row_id, "SKIPPED", error="ALREADY_SENT")
                skipped += 1
                continue

            # 3) Enviar template
            sid = send_whatsapp_template(
                to_whatsapp,
                content_vars={"1": (nombre or "Hola")},
                template_sid=TWILIO_TEMPLATE_SID,
                status_callback=STATUS_CALLBACK_URL,
            )

            # 4) Persistir
            save_template_sid(tenant, cuil, period, to_whatsapp, sid, nombre=nombre)
            add_pending_view(to_whatsapp, tenant, cuil, period, origin="INITIAL")

            _mark_queue_row(row_id, "SENT", sent_sid=sid)
            sent += 1

        except Exception as e:
            _mark_queue_row(row_id, "FAILED", error=str(e)[:250])
            failed += 1

    stats = count_queue_status(tenant, period)

    # Si lo llamás desde cron, puede devolverte OK sin redirect:
    if (request.form.get("mode") or request.args.get("mode") or "").lower() == "json":
        return {
            "tenant": tenant,
            "period": period,
            "processed": processed,
            "sent": sent,
            "skipped": skipped,
            "failed": failed,
            "stats": stats,
        }

    return redirect(
        f"/admin/panel?tenant={tenant}&token={token}&msg=queue_tick"
        f"&period={period}&processed={processed}&sent={sent}&skipped={skipped}&failed={failed}"
        f"&pending={stats.get('PENDING',0)}"
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
        add_pending_view(phone, tenant, cuil, period, origin="INITIAL")


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

    nombre = (nombre or "").strip()

    # 🔒 Twilio NO acepta variables vacías -> usamos espacio como fallback
    vars_ = {"1": nombre if nombre else " "}

    try:
        msg = client.messages.create(
            to=to_whatsapp,
            from_=TWILIO_WHATSAPP_FROM,
            content_sid=WHATSAPP_MENU_CONTENT_SID,
            content_variables=json.dumps(vars_)
        )
        return msg.sid
    except Exception as e:
        print("ERROR send_whatsapp_menu_template:", e, "vars_=", vars_)
        return None


TWILIO_SIGN_TEMPLATE_SID = os.environ.get("TWILIO_SIGN_TEMPLATE_SID", "").strip()

@app.post("/twilio/inbound")
def twilio_inbound():
    from_whatsapp = (request.form.get("From") or "").strip()
    button = (request.form.get("ButtonPayload") or "").strip()
    body = (request.form.get("Body") or "").strip()
    in_sid = (request.form.get("MessageSid") or "").strip()

    print("INBOUND:", from_whatsapp, "MessageSid:", in_sid, "ButtonPayload:", button, "Body:", body)

    # ✅ DEDUP global: si Twilio reintenta el mismo inbound, no hacemos nada
    if inbound_seen(in_sid):
        print("DEDUP inbound:", in_sid)
        return Response("OK", status=200)

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
        step_now = (pending.get("step") or "READY").upper() if isinstance(pending, dict) else "READY"
        body_norm = (body or "").strip()

        # AWAIT_DNI: dejamos pasar para que lo procese el bloque AWAIT_DNI
        if step_now == "AWAIT_DNI":
            pass

        # CHOOSE_PREVIOUS: si no es 1/2/3, devolvemos ayuda (no menú)
        elif step_now == "CHOOSE_PREVIOUS":
            if body_norm in ("1", "2", "3"):
                pass
            else:
                return twiml("🗂️ Respondé con 1, 2 o 3 para elegir un período anterior.")

        else:
            # ✅ Enviar menú y terminar (SIN TwiML al usuario)
            nombre = ""
            if isinstance(pending, dict):
                nombre = get_nombre_for_cuil(pending["tenant"], pending["cuil"])
            sid = send_whatsapp_menu_template(from_whatsapp, nombre=nombre)
            return Response("OK", status=200)


    # =========================
    # SIN PENDING: o guía o reconstrucción para "RECIBO"
    # =========================
    if not pending:
        if not _is_receipt_request_text(body) and not button:
            in_sid = (request.form.get("MessageSid") or "").strip()

            # 🔥 En vez de contestar texto, mandamos la plantilla menú
            sid = send_whatsapp_menu_template(from_whatsapp, nombre="")
            return Response("OK", status=200)


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

        add_pending_view(from_whatsapp, tenant, cuil, period, origin="RESEND_LAST")

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
            add_pending_view(from_whatsapp, tenant, cuil, best_period, origin="RESEND_LAST")
            pending = get_latest_pending_view(from_whatsapp)
            set_pending_step(pending["id"], "AWAIT_DNI")

            _log_receipt_request_event(tenant, cuil, best_period, from_whatsapp, "RESEND_LAST", "ASK_DNI")
            return twiml("🔐 Para reenviar tu recibo, enviá tu DNI (solo números, sin puntos).")


        sid_pdf = _send_pdf_flow(from_whatsapp, tenant, cuil, best_period, origin="RESEND_LAST")
        if not sid_pdf:
            _log_receipt_request_event(tenant, cuil, best_period, from_whatsapp, "RESEND_LAST", "ERROR")
            return twiml("❌ No pude enviar el PDF en este momento. Probá de nuevo o avisá a RRHH.")
        # ✅ sumar el pedido (esto es lo que faltaba)
        n = inc_receipt_request_count(tenant, cuil, best_period, from_whatsapp)
        # ✅ no mandamos texto antes del PDF
        _log_receipt_request_event(tenant, cuil, best_period, from_whatsapp, "RESEND_LAST", "SENT", message_sid=sid_pdf, origin="RESEND_LAST")
        return Response("OK", status=200)

    # SEE_PREVIOUS -> ofrece hasta 3 períodos anteriores (no envía PDF todavía)
    if button == "SEE_PREVIOUS":
        periods = list_periods_for_cuil(tenant, strip_pdf(cuil)) or []
        prev = periods[1:4]  # tres anteriores al último disponible

        if not prev:
            return twiml("ℹ️ Esta opción estará disponible próximamente.")

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
        # DNI OK
        set_verified_contact(tenant, cuil, from_whatsapp, dni_user, nombre=pending.get("nombre",""))
        set_pending_step(pending["id"], "READY")

        cnt = get_receipt_request_count(tenant, cuil, period, from_whatsapp)
        if cnt >= 3:
            _log_receipt_request_event(tenant, cuil, period, from_whatsapp, "DNI_OK", "BLOCKED_LIMIT")
            consume_pending_view(pending["id"])
            return twiml(f"⚠️ Ya pediste este recibo {cnt}/3 veces para {period}. Si necesitás más, avisá a RRHH.")

        # ✅ usar origin del pending (INITIAL si vino del admin)
        origin = (pending.get("origin") or "INITIAL")

        sid_pdf = _send_pdf_flow(from_whatsapp, tenant, cuil, period, origin=origin)
        if not sid_pdf:
            _log_receipt_request_event(tenant, cuil, period, from_whatsapp, "DNI_OK", "ERROR", origin=origin)
            return twiml("✅ DNI verificado, pero hubo un error enviando el recibo. Avisá a RRHH.")

        n = inc_receipt_request_count(tenant, cuil, period, from_whatsapp)
        _log_receipt_request_event(tenant, cuil, period, from_whatsapp, "DNI_OK", "SENT", message_sid=sid_pdf, origin=origin)
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
        return Response("OK", status=200)

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

def _send_pdf_flow(from_whatsapp: str, tenant: str, cuil: str, period: str, origin: str = "INITIAL") -> str | None:
    """
    Envía el PDF del recibo.
    - origin="INITIAL": envío inicial (RRHH) -> puede disparar firma al entregar.
    - origin="RESEND": reenvío pedido por el usuario -> NO debe disparar firma.
    """
    file_id = find_pdf_file_id_for_cuil_period(tenant, cuil, period)
    if not file_id:
        return None

    pdf_url = (
        f"{request.host_url.rstrip('/')}/media/pdf"
        f"?tenant={tenant}&cuil={cuil}&period={period}&token={ADMIN_TOKEN}"
    )

    try:
        # ✅ Para controlar el orden:
        # - En INITIAL podemos incluir body (si querés).
        # - En RESEND lo mandamos vacío y notificamos después del delivered desde /twilio/status.
        body_text = f"Acá tenés tu recibo {period}." if origin == "INITIAL" else ""

        sid_pdf = send_whatsapp_pdf(
            from_whatsapp,
            pdf_url,
            body=body_text,
            status_callback=STATUS_CALLBACK_URL,
        )


        # ✅ Guardamos SID + ORIGIN (para decidir si enviar firma o no al delivered)
        save_pdf_sid(tenant, cuil, period, from_whatsapp, sid_pdf, origin=origin)

        return sid_pdf

    except Exception as e:
        print("ERROR sending PDF:", e)
        return None


@app.get("/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time()), "db_path": DB_PATH})
