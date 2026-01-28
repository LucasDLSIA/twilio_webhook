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

# =========================
# Config
# =========================
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
EMPRESAS_FILE_ID = os.environ.get("EMPRESAS_FILE_ID", "").strip()

# Service Account JSON: podés guardarlo como string en ENV (recomendado)
# o como archivo (si tu build lo copia). Acá soportamos ambas.
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_SA_FILE =  (   "/etc/secrets/Service_account.json"
    if os.path.exists("/etc/secrets/Service_account.json")
    else "Service_account.json"
)

# Cache
_EMP_CACHE = {"ts": 0, "rows": []}
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
        (s or "")
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

def parse_period_folder(name: str) -> Optional[str]:
    """
    Acepta: '01-2026', '01/2026', 'ENERO 2026' (opcional), etc.
    Devuelve siempre 'mm/aaaa' o None si no reconoce.
    """
    n = (name or "").strip()
    m = re.search(r"(\d{2})[-/](\d{4})", n)
    if m:
        mm, yyyy = m.group(1), m.group(2)
        if 1 <= int(mm) <= 12:
            return f"{mm}/{yyyy}"
    return None

def admin_ok() -> bool:
    if not ADMIN_TOKEN:
        # si no seteaste token, dejamos abierto (no recomendado, pero útil para prueba)
        return True
    tok = request.args.get("token", "") or request.headers.get("X-Admin-Token", "")
    return tok.strip() == ADMIN_TOKEN

def require_admin():
    if not admin_ok():
        return Response("Unauthorized (admin token requerido)", status=401)

def drive_service():
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    if GOOGLE_SA_JSON:
        info = json.loads(GOOGLE_SA_JSON) if isinstance(GOOGLE_SA_JSON, str) else GOOGLE_SA_JSON
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    elif GOOGLE_SA_FILE:
        creds = service_account.Credentials.from_service_account_file(GOOGLE_SA_FILE, scopes=scopes)
    else:
        raise RuntimeError("Falta GOOGLE_SERVICE_ACCOUNT_JSON o GOOGLE_SERVICE_ACCOUNT_FILE en ENV")
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
    # Tu formato:
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

    # dedupe por slug (si hay filas repetidas)
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

def find_phone_by_cuil(envios_rows: List[dict], cuil: str) -> Optional[str]:
    target = norm_cuil(cuil)
    if not target:
        return None

    phone_keys = ["WhatsApp", "Telefono", "Teléfono", "CEL", "Celular", "to_whatsapp", "to", "WPP", "Whatsapp"]
    cuil_keys = ["CUIL", "Cuil", "Archivo", "archivo", "archivo_norm", "Archivo_norm", "CUIT", "Cuit"]

    for r in envios_rows:
        rcuil = ""
        for k in cuil_keys:
            if k in r and str(r.get(k, "")).strip():
                rcuil = str(r.get(k, "")).strip()
                break

        if norm_cuil(rcuil) == target:
            for pk in phone_keys:
                if pk in r and str(r.get(pk, "")).strip():
                    return norm_whatsapp(str(r.get(pk, "")).strip())
    return None

# =========================
# Drive: listar periodos por CUIL
# =========================
def list_periods_for_cuil(tenant_slug: str, cuil: str) -> List[str]:
    t = get_tenant(tenant_slug)
    if not t:
        return []

    root_id = t["drive_root_id"]
    filename = f"{cuil}.pdf"
    service = drive_service()

    # 1) listar carpetas dentro del root
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
        # 2) buscar el PDF dentro de esa carpeta
        res = service.files().list(
            q=f"'{f['id']}' in parents and name='{filename}' and mimeType='application/pdf' and trashed=false",
            fields="files(id)",
            pageSize=1,
        ).execute().get("files", [])
        if res:
            periods.append(label)

    # ordenar desc por año/mes
    def key(p: str):
        mm, yyyy = p.split("/")
        return int(yyyy) * 100 + int(mm)

    periods = sorted(set(periods), key=key, reverse=True)
    return periods

def norm_digits(s: str) -> str:
    return re.sub(r"\D", "", str(s or ""))

def norm_cuil(s: str) -> str:
    d = norm_digits(s)
    # CUIL suele tener 11 dígitos. Si viene con basura, igual devolvemos dígitos.
    return d

def norm_whatsapp(s: str) -> str:
    d = norm_digits(s)
    if not d:
        return ""
    # Si ya viene con 54..., lo respetamos.
    if d.startswith("54"):
        return "whatsapp:+" + d
    # Si viene tipo 11xxxxxxxx (ARG), le agregamos 54
    return "whatsapp:+54" + d


# =========================
# Routes
# =========================
@app.get("/")
def root():
    # siempre admin
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
    tenants = load_tenants(force=True)  # forzamos para ver cambios del Excel

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
            html.append(
                f"<li><a href='/admin/panel?tenant={esc(t['slug'])}&token={esc(token)}'>"
                f"{esc(t['display_name'])}</a></li>"
            )
        html.append("</ul>")

    return Response("".join(html), mimetype="text/html")

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
    html.append(f"<p><a href='/admin/panel?tenant={tenant}&token={token}'>← volver</a></p>")

    # Form
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

      <button type="submit">Enviar prueba</button>
    </form>
    """)

    # Ejecutar si hay datos
    if cuil and period:
        html.append("<hr>")
        html.append("<h3>Resultado</h3>")

        envios = load_envios_rows(tenant)
        phone = find_phone_by_cuil(envios, cuil)

        if not phone:
            html.append("<p style='color:red'>No se encontró WhatsApp para ese CUIL.</p>")
            html.append("<p class='mono'>Debug: primeros CUIL leídos:</p><ul>")
            for r in envios[:20]:
                rc = r.get("CUIL") or r.get("Archivo") or r.get("archivo_norm") or ""
                html.append(f"<li>{esc(str(rc))} → {esc(norm_cuil(str(rc)))}</li>")
            html.append("</ul>")
        else:
            periods = list_periods_for_cuil(tenant, cuil)
            if period not in periods:
                html.append("<p style='color:red'>No se encontró el PDF para ese período.</p>")
            else:
                html.append("<p>✔ Persona encontrada</p>")
                html.append(f"<p>📞 WhatsApp: {esc(phone)}</p>")
                html.append(f"<p>📄 PDF disponible para {esc(period)}</p>")
                html.append("<p style='color:green'>👉 ACÁ VA EL ENVÍO REAL (Twilio)</p>")

                # 🔥 ACÁ después conectamos Twilio
                # send_whatsapp_pdf(phone, pdf_url, nombre, period)

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

    # cargar envíos de esa empresa
    envios_rows = load_envios_rows(tenant, force=False)

    html = []
    html.append("<h2>Panel empresa</h2>")
    html.append(f"<p><b>Empresa:</b> {esc(t['display_name'])} &nbsp; (<code>{esc(t['slug'])}</code>)</p>")
    html.append(f"<p><a href='/admin?token={esc(token)}'>← volver</a></p>")
    html.append("<hr>")

    html.append("<h3>Acciones</h3>")
    html.append("<ul>")
    html.append("<li>Ver envíos (preview)</li>")
    html.append("<li>Buscar períodos por CUIL</li>")
    html.append("</ul>")

    # Preview envíos
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

    # Buscar periodos por CUIL
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

    token = request.args.get("token", "")
    tenant = (request.args.get("tenant") or "").strip().lower()
    cuil = (request.args.get("cuil") or "").strip()

    if not get_tenant(tenant):
        return Response("Tenant inválido", status=400)
    if not cuil:
        return Response("Falta CUIL", status=400)

    periods = list_periods_for_cuil(tenant, cuil)
    return jsonify({"tenant": tenant, "cuil": cuil, "periodos": periods})

@app.get("/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})
