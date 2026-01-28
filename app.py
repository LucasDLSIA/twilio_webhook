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

def send_whatsapp_pdf(to_whatsapp: str, media_url: str, body: str) -> str:
    if not (TWILIO_WHATSAPP_FROM or TWILIO_MESSAGING_SERVICE_SID):
        raise RuntimeError("Falta TWILIO_WHATSAPP_FROM o TWILIO_MESSAGING_SERVICE_SID en ENV")

    client = _twilio_client()
    payload = {
        "to": to_whatsapp,
        "body": body or " ",
        "media_url": [media_url],
    }
    if TWILIO_MESSAGING_SERVICE_SID:
        payload["messaging_service_sid"] = TWILIO_MESSAGING_SERVICE_SID
    else:
        payload["from_"] = TWILIO_WHATSAPP_FROM

    msg = client.messages.create(**payload)
    return msg.sid

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

    # --- asegurar tablas base (siempre) ---
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

    # --- Migración "bien hecha" de pending_views ---
    # Si existe una pending_views vieja (con archivo_norm NOT NULL u otras columnas),
    # la renombramos a pending_views_legacy y creamos la nueva con tenant/cuil/period.
    try:
        # chequeo si existe pending_views
        cur.execute("""
          SELECT name FROM sqlite_master
          WHERE type='table' AND name='pending_views';
        """)
        exists = cur.fetchone() is not None

        if exists:
            # detecto columnas actuales
            cur.execute("PRAGMA table_info(pending_views);")
            cols = [r[1] for r in cur.fetchall()]  # r[1] = name

            # Si NO es el esquema nuevo esperado, la renombramos
            expected = {"to_whatsapp", "tenant", "cuil", "period", "created_at"}
            if not expected.issubset(set(cols)) or "archivo_norm" in cols:
                # si ya existe legacy, evitamos choque de nombre
                cur.execute("""
                  SELECT name FROM sqlite_master
                  WHERE type='table' AND name='pending_views_legacy';
                """)
                legacy_exists = cur.fetchone() is not None

                if legacy_exists:
                    # si ya hay legacy, no renombro (para no fallar)
                    # en ese caso, borro la pending_views vieja para recrearla limpia
                    cur.execute("DROP TABLE IF EXISTS pending_views;")
                else:
                    cur.execute("ALTER TABLE pending_views RENAME TO pending_views_legacy;")
    except Exception as e:
        print("WARN migrate pending_views:", e)

    # --- Crear tabla nueva (si no existe) ---
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

    # índices útiles (rápido para lookup por whatsapp)
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_views_to ON pending_views(to_whatsapp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_views_to_created ON pending_views(to_whatsapp, created_at);")
    except Exception:
        pass

    conn.commit()
    conn.close()

init_db()

def add_pending_view(to_whatsapp: str, tenant: str, cuil: str, period: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO pending_views (to_whatsapp, tenant, cuil, period, created_at) VALUES (?, ?, ?, ?, ?)",
        (to_whatsapp, tenant, cuil, period, int(time.time())),
    )
    conn.commit()
    conn.close()

def get_latest_pending_view(from_whatsapp: str) -> Optional[dict]:
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, tenant, cuil, period
        FROM pending_views
        WHERE to_whatsapp = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (from_whatsapp,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def consume_pending_view(pending_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM pending_views WHERE id = ?", (int(pending_id),))
    conn.commit()
    conn.close()

def set_recibo_estado(tenant: str, cuil: str, period: str, estado: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO recibo_estado (tenant, cuil, period, estado, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(tenant, cuil, period) DO UPDATE SET
          estado=excluded.estado,
          updated_at=excluded.updated_at
        """,
        (tenant, cuil, period, estado, int(time.time())),
    )
    conn.commit()
    conn.close()

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

    token = request.args.get("token", "")
    tenant = (request.args.get("tenant") or "").strip().lower()
    t = get_tenant(tenant)
    if not t:
        return Response("Tenant inválido. Volvé a /admin.", status=400)

    envios_rows = load_envios_rows(tenant, force=False)

    html = []
    html.append("<h2>Panel empresa</h2>")
    html.append(f"<p><b>Empresa:</b> {esc(t['display_name'])} &nbsp; (<code>{esc(t['slug'])}</code>)</p>")
    html.append(f"<p><a href='/admin?token={esc(token)}'>← volver</a></p>")
    html.append(f"<p><a href='/admin/send_test?tenant={esc(tenant)}&token={esc(token)}'>🧪 Envío de prueba</a></p>")
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

    html.append("<hr>")
    html.append("<h3>Buscar períodos por CUIL</h3>")
    html.append(f"""
      <form method="get" action="/admin/periodos">
        <input type="hidden" name="token" value="{esc(token)}">
        <input type="hidden" name="tenant" value="{esc(tenant)}">
        <input type="text" name="cuil" placeholder="20xxxxxxxxx" required>
        <button type="submit">Buscar</button>
      </form>
    """)

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
@app.post("/twilio/inbound")
def twilio_inbound():
    # form-urlencoded
    from_whatsapp = (request.form.get("From") or "").strip()
    button = (request.form.get("ButtonPayload") or "").strip()
    body = (request.form.get("Body") or "").strip()

    print("INBOUND:", from_whatsapp, "ButtonPayload:", button, "Body:", body)

    # 1) VIEW_NOW -> enviar PDF del periodo del pending view
    if button == "VIEW_NOW" or body == "VIEW_NOW":
        pending = get_latest_pending_view(from_whatsapp)
        if not pending:
            return Response("No pending view", status=200)

        tenant = pending["tenant"]
        cuil = pending["cuil"]
        period = pending["period"]

        pdf_url = (
            f"{request.host_url.rstrip('/')}/media/pdf"
            f"?tenant={tenant}&cuil={cuil}&period={period}&token={ADMIN_TOKEN}"
        )

        try:
            sid_pdf = send_whatsapp_pdf(
                from_whatsapp,
                pdf_url,
                body=f"Acá tenés tu recibo {period}."
            )
            print("SENT PDF SID:", sid_pdf)

            # (opcional) mandar plantilla de firma/observa después del PDF
            if TWILIO_SIGN_TEMPLATE_SID:
                try:
                    sid_sign = send_whatsapp_template(
                        from_whatsapp,
                        content_vars={"1": period},
                        template_sid=TWILIO_SIGN_TEMPLATE_SID,
                    )
                    print("SENT SIGN TEMPLATE SID:", sid_sign)
                except Exception as e:
                    print("WARN sending sign template:", e)

            # Estado: DISPONIBLE
            set_recibo_estado(tenant, cuil, period, "DISPONIBLE")

            # Consumimos el pending para evitar mezclas
            consume_pending_view(pending["id"])

        except Exception as e:
            print("ERROR sending PDF:", e)

        return Response("OK", status=200)

    # 2) Firma / Observa (cuando tengas tu segunda plantilla con payloads)
    if button in ("SIGN_OK", "SIGN_OBS"):
        pending = get_latest_pending_view(from_whatsapp)
        if not pending:
            return Response("No pending view", status=200)

        tenant = pending["tenant"]
        cuil = pending["cuil"]
        period = pending["period"]

        if button == "SIGN_OK":
            set_recibo_estado(tenant, cuil, period, "FIRMADO")
        else:
            set_recibo_estado(tenant, cuil, period, "OBSERVADO")

        consume_pending_view(pending["id"])
        return Response("OK", status=200)

    return Response("OK", status=200)

@app.get("/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time()), "db_path": DB_PATH})
