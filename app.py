# app.py
import os
import io
import re
import requests
from typing import Dict, Tuple, Optional, List

from flask import send_file
import io


import pandas as pd
from flask import Flask, request, Response
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse


from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaIoBaseDownload

import sqlite3
from pathlib import Path


app = Flask(__name__)


# ==========================
#  Configuración / entorno
# ==========================

GOOGLE_SERVICE_ACCOUNT_FILE = (
    "/etc/secrets/Service_account.json"
    if os.path.exists("/etc/secrets/Service_account.json")
    else "Service_account.json"
)
DRIVE_RECIBOS_ROOT_ID = os.getenv("DRIVE_ROOT_FOLDER_ID")
ENVIOS_FILE_ID = os.getenv("ENVIOS_FILE_ID")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")

PERIODO_ACTUAL = os.getenv("PERIODO_ACTUAL")
# === Plantilla WhatsApp ===
TWILIO_CONTENT_SID   = os.getenv("TWILIO_TEMPLATE_SID")  # Content SID de tu plantilla (HX...)
STATUS_CALLBACK_URL  = os.getenv("STATUS_CALLBACK_URL", f"{os.getenv('PUBLIC_BASE_URL','https://twilio-webhook-lddc.onrender.com').rstrip('/')}/twilio/status")


twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Sesiones en memoria para el menú del Camino B
# clave: telefono_norm; valor: dict con estado, offset, periodos, opciones
SESSIONS: Dict[str, Dict] = {}

# ==========================
#  Helpers generales
# ==========================
def normalize_phone(whatsapp_from: str) -> str:
    """
    Normaliza el teléfono que viene de Twilio (ej: 'whatsapp:+54911...')
    a la misma forma que usamos en el Excel: solo dígitos.
    """
    val = s(whatsapp_from)
    if val.startswith("whatsapp:"):
        val = val[len("whatsapp:"):]
    return canonicalize_phone(val)

import re

def canonicalize_phone(x) -> str:
    """Normaliza un teléfono dejando solo dígitos.
       Sirve para comparar Twilio vs Excel sin lío de 'whatsapp:' ni '+'.
    """
    raw = s(x)
    raw = raw.replace(",", "").replace(".0", "")
    # dejar solo dígitos
    digits = re.sub(r"\D", "", raw)
    # si querés, podés quedarte con los últimos 10 dígitos (opcional):
    # return digits[-10:] if len(digits) > 10 else digits
    return digits

#=============================================================================
# =========================
# SQLITE: tabla de envíos pendientes
# =========================
# === SQLite: almacenamiento de "pendientes de ver" ===

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
import os
import sqlite3
import time

# Ruta del archivo SQLite
# En local: usa "pending_views.db"
# En Render con disk persistente, podés usar /data/pending_views.db
DB_PATH = os.environ.get("PENDING_DB_PATH", "pending_views.db")


def get_db_connection():
    """
    Devuelve una conexión a SQLite.
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn


def init_db():
    """
    Crea la tabla si no existe.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            to_whatsapp TEXT NOT NULL,
            archivo_norm TEXT NOT NULL,
            period_label TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def save_pending_view(to_whatsapp: str, archivo_norm: str, period_label: str):
    """
    Guarda que a este número le mandamos la plantilla
    asociada a (archivo_norm, period_label).
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO pending_views (to_whatsapp, archivo_norm, period_label, created_at)
        VALUES (?, ?, ?, ?);
        """,
        (to_whatsapp, archivo_norm, period_label, int(time.time())),
    )
    conn.commit()
    conn.close()


def get_last_pending_view(from_whatsapp: str):
    """
    Devuelve el último (archivo_norm, period_label) pendiente
    para ese número de WhatsApp, o None si no hay.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT archivo_norm, period_label
        FROM pending_views
        WHERE to_whatsapp = ?
        ORDER BY created_at DESC
        LIMIT 1;
        """,
        (from_whatsapp,),
    )
    row = cur.fetchone()
    conn.close()

    if row:
        return row[0], row[1]
    return None


# ⚠️ MUY IMPORTANTE:
# Llamamos a init_db() al importar el módulo
# (para que gunicorn lo ejecute siempre)
init_db()

# ==========================

def ensure_anyone_reader(file_id: str) -> None:
    """Se asegura de que el file sea accesible públicamente por link."""
    service = build_drive_service()
    try:
        service.permissions().create(
            fileId=file_id,
            body={"role": "reader", "type": "anyone"},
        ).execute()
    except Exception as e:
        print("WARN ensure_anyone_reader:", e)


def get_drive_download_url(file_id: str) -> str:
    """
    Intenta devolver un link de descarga directo (webContentLink).
    Si no existe, intenta abrir permisos y reintentar.
    Si sigue sin estar, cae a uc?export=download.
    """
    service = build_drive_service()

    def _fetch_links() -> tuple[str | None, str | None, str | None]:
        info = service.files().get(
            fileId=file_id,
            fields="id, name, mimeType, size, webViewLink, webContentLink",
        ).execute()
        return info.get("webContentLink"), info.get("webViewLink"), info.get("size")

    wcl, wvl, size = _fetch_links()
    print("DEBUG get_drive_download_url pre:", {"webContentLink": wcl, "webViewLink": wvl, "size": size})

    if not wcl:
        ensure_anyone_reader(file_id)
        wcl, wvl, size = _fetch_links()
        print("DEBUG get_drive_download_url post:", {"webContentLink": wcl, "webViewLink": wvl, "size": size})

    if wcl:
        return wcl

    # Fallback estable
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def is_url_fetchable(url: str) -> bool:
    """HEAD/GET rápido para ver si Twilio podría bajarlo (seguimos redirects)."""
    try:
        r = requests.head(url, allow_redirects=True, timeout=8)
        print("DEBUG is_url_fetchable HEAD:", r.status_code, "final_url:", r.url)
        if r.status_code == 405:  # Algunos endpoints no aceptan HEAD
            r = requests.get(url, stream=True, allow_redirects=True, timeout=8)
            print("DEBUG is_url_fetchable GET:", r.status_code, "final_url:", r.url)
            return 200 <= r.status_code < 300
        return 200 <= r.status_code < 300
    except Exception as e:
        print("DEBUG is_url_fetchable EXC:", e)
        return False


def build_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds)


def download_envios_excel() -> pd.DataFrame:
    """
    Descarga envios.xlsx desde Drive (por ENVIOS_FILE_ID) y lo devuelve como DataFrame.
    Columnas esperadas: nombre, telefono, archivo
    """
    service = build_drive_service()

    request_drive = service.files().get_media(fileId=ENVIOS_FILE_ID)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request_drive)

    done = False
    while not done:
        status, done = downloader.next_chunk()

    fh.seek(0)

    df = pd.read_excel(fh)

    # Normalizamos nombres de columnas por si vienen con mayúsculas o espacios
    df.columns = [str(c).strip().lower() for c in df.columns]

    # Aseguramos las columnas base
    if "telefono" not in df.columns or "archivo" not in df.columns:
        raise ValueError("El Excel de envíos debe tener columnas 'telefono' y 'archivo'")

    # Normalizamos teléfono
    df["telefono_norm"] = df["telefono"].apply(canonicalize_phone)


    # Normalizamos archivo (CUIL sin .pdf)
    df["archivo_norm"] = df["archivo"].astype(str).str.strip()
    df["archivo_norm"] = df["archivo_norm"].str.replace(".pdf", "", case=False)

    return df


def get_archivo_for_phone(telefono_norm: str, envios_df: pd.DataFrame) -> Optional[str]:
    """
    Dado un teléfono normalizado y el DataFrame de envíos,
    devuelve el 'archivo_norm' (CUIL) correspondiente, o None si no hay fila.
    """
    filas = envios_df[envios_df["telefono_norm"] == telefono_norm]
    if filas.empty:
        return None

    # Si hay más de una fila, tomamos la primera (puede ajustarse a otra lógica)
    return filas.iloc[0]["archivo_norm"]


def period_folder_to_label(folder_name: str) -> Optional[str]:
    """
    Convierte nombre de carpeta 'mm-aaaa' a etiqueta 'mm/aaaa'.
    Si no matchea el patrón, devuelve None.
    """
    m = re.match(r"^(\d{2})-(\d{4})$", folder_name)
    if not m:
        return None
    mm, yyyy = m.groups()
    return f"{mm}/{yyyy}"


def period_label_to_folder(period_label: str) -> str:
    """
    Convierte 'mm/aaaa' → 'mm-aaaa'
    """
    return period_label.replace("/", "-")


def period_sort_key(period_label: str):
    """
    Convierte 'mm/aaaa' en tupla (aaaa, mm) para poder ordenar.
    """
    m = re.match(r"^(\d{2})/(\d{4})$", period_label)
    if not m:
        return (0, 0)
    mm, yyyy = m.groups()
    return (int(yyyy), int(mm))


def list_periods_for_archivo(archivo_norm: str) -> List[str]:
    """
    Busca en Drive todos los PDFs cuyo nombre sea {archivo_norm}.pdf
    y arma la lista de períodos (mm/aaaa) donde ese archivo existe.
    """
    service = build_drive_service()

    filename = f"{archivo_norm}.pdf"

    # Buscamos todos los archivos con ese nombre
    results = service.files().list(
        q=f"name = '{filename}' and mimeType = 'application/pdf' and trashed = false",
        fields="files(id, name, parents)",
        pageSize=1000,
    ).execute()

    files = results.get("files", [])

    periods = set()

    for f in files:
        parents = f.get("parents", [])
        if not parents:
            continue
        parent_id = parents[0]
        # Obtenemos el nombre de la carpeta padre, que debería ser 'mm-aaaa'
        folder = service.files().get(
            fileId=parent_id,
            fields="id, name, parents",
        ).execute()
        folder_name = folder.get("name", "")
        label = period_folder_to_label(folder_name)
        if label:
            periods.add(label)

    # Ordenamos de más nuevo a más viejo
    ordered = sorted(list(periods), key=period_sort_key, reverse=True)

    print("DEBUG list_periods_for_archivo")
    print("  archivo_norm:", archivo_norm)
    print("  filename buscado:", filename)
    print("  cantidad de archivos encontrados:", len(files))
    for f in files:
        print("   - file:", f.get("id"), f.get("name"), "parents:", f.get("parents"))
    print("  periods detectados:", periods)

    return ordered


def find_pdf_for_archivo_and_period(archivo_norm: str, period_label: str) -> Optional[str]:
    """
    Dado el CUIL (archivo_norm) y un período (puede venir como 'mm/aaaa' o 'mm-aaaa'),
    devuelve el fileId del PDF en Drive para ese período, o None si no existe.

    En vez de asumir nombre exacto de carpeta, busca todos los PDFs con ese nombre
    y se queda con el que esté en una carpeta cuyo nombre mapee a ese período
    vía period_folder_to_label.
    """
    service = build_drive_service()

    filename = f"{archivo_norm}.pdf"

    # Buscamos todos los archivos con ese nombre en todo el Drive
    results = service.files().list(
        q=f"name = '{filename}' and mimeType = 'application/pdf' and trashed = false",
        fields="files(id, name, parents)",
        pageSize=1000,
    ).execute()

    files = results.get("files", [])

    print("DEBUG find_pdf_for_archivo_and_period")
    print("  archivo_norm:", archivo_norm)
    print("  period_label buscado:", period_label)
    print("  cantidad de archivos encontrados:", len(files))

    # Normalizamos el período que nos llega (10/2025 o 10-2025 -> 10-2025)
    normalized_period = period_label.replace("/", "-") if period_label else ""

    for f in files:
        parents = f.get("parents", [])
        if not parents:
            continue

        parent_id = parents[0]
        folder = service.files().get(
            fileId=parent_id,
            fields="id, name, parents",
        ).execute()
        folder_name = folder.get("name", "")
        label = period_folder_to_label(folder_name)

        # Normalizamos carpeta y label
        normalized_folder = folder_name.replace("/", "-") if folder_name else ""
        normalized_label = label.replace("/", "-") if label else ""

        print("   - file:", f.get("id"), f.get("name"),
              "| carpeta:", folder_name, "| label:", label,
              "| normalized_folder:", normalized_folder,
              "| normalized_label:", normalized_label)

        # Matcheamos por carpeta o por label, ya normalizados
        if normalized_folder == normalized_period or normalized_label == normalized_period:
            print("  -> match encontrado, devolviendo file_id:", f.get("id"))
            return f.get("id")

    print("  -> no se encontró PDF para ese período")
    return None


def norm_period_label(s: str) -> str:
    """
    Normaliza un período a 'mm/aaaa'. Acepta 'mm/aaaa', 'mm-aaaa', 'm/aaaa', 'm-aaaa'
    y también 'mmaaaa' o 'mmyyyy'.
    """
    if not s:
        return ""
    s = str(s).strip()
    # formatos con separador
    m = re.match(r"^(\d{1,2})[/-](\d{4})$", s)
    if m:
        mm, yyyy = m.groups()
        return f"{int(mm):02d}/{yyyy}"

    # formatos pegados tipo mmyyyy
    m = re.match(r"^(\d{1,2})(\d{4})$", s)
    if m:
        mm, yyyy = m.groups()
        return f"{int(mm):02d}/{yyyy}"

    # si ya viene mm/aaaa correcto, lo dejamos
    m = re.match(r"^\d{2}/\d{4}$", s)
    if m:
        return s

    # último recurso: devolvemos tal cual
    return s



def build_drive_public_link(file_id: str) -> str:
    """
    Devuelve un link "descargable" de Drive.
    OJO: el archivo debe estar compartido como 'cualquiera con el enlace'.
    """
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def get_session(telefono_norm: str) -> Dict:
    """
    Devuelve (y crea si no existe) la sesión para ese teléfono.
    """
    if telefono_norm not in SESSIONS:
        SESSIONS[telefono_norm] = {
            "state": "IDLE",
            "offset": 0,
            "periods": [],
            "options_map": {},
        }
    return SESSIONS[telefono_norm]


def normalize_to_whatsapp_e164(s: str) -> str:
    s = (s or "").strip()
    # si ya viene con prefijo 'whatsapp:' lo dejamos
    if s.startswith("whatsapp:"):
        return s
    # si viene sólo +54911... le agregamos el prefijo
    if s.startswith("+"):
        return "whatsapp:" + s
    # último recurso: quitar espacios/guiones y asumir +
    digits = re.sub(r"[^\d+]", "", s)
    if digits.startswith("+"):
        return "whatsapp:" + digits
    return "whatsapp:+" + digits


import pandas as pd
from io import BytesIO

def read_envios_rows() -> list[dict]:
    """
    Lee el archivo de envíos desde Drive (mismo que usa download_envios_excel)
    y devuelve una lista de dicts con claves: 'CUIL', 'Telefono', 'Archivo', etc.
    """
    try:
        df = download_envios_excel()
        if df is None or df.empty:
            print("WARN: no se pudo leer el archivo de envíos (vacío o inexistente).")
            return []

        # Normalizamos columnas comunes
        df.columns = [str(c).strip().capitalize() for c in df.columns]
        expected_cols = {"Cuil", "Telefono", "Archivo"}
        cols_ok = expected_cols.intersection(df.columns)
        if not cols_ok:
            print("WARN: no se encontraron las columnas esperadas en el Excel de envíos.")
        return df.to_dict(orient="records")

    except Exception as e:
        print(f"ERROR en read_envios_rows(): {e}")
        return []



def find_archivo_by_phone(to_whatsapp: str) -> str | None:
    """
    Buscar en ENVIOS_FILE_ID el archivo_norm (CUIL) por teléfono.
    Compara flexible: ignora espacios/guiones.
    """
    rows = read_envios_rows()
    want = re.sub(r"\D", "", to_whatsapp or "")
    for r in rows:
        # soportar Telefono / teléfono
        tel = r.get("Telefono") or r.get("Teléfono") or r.get("telefono") or ""
        # soportar Archivo_norm / archivo_norm / Archivo / archivo
        arc = (
            r.get("Archivo_norm")
            or r.get("archivo_norm")
            or r.get("Archivo")
            or r.get("archivo")
            or ""
        )
        if not tel or not arc:
            continue
        tclean = re.sub(r"\D", "", str(tel))
        if tclean.endswith(want) or want.endswith(tclean):
            return str(arc).strip()
    return None

import json
import pandas as pd

def resolve_name_for_phone(phone_e164: str) -> str:
    rows = read_envios_rows()
    target = canonicalize_phone(phone_e164)
    for r in rows:
        tel = canonicalize_phone(
            r.get("Telefono") or r.get("Teléfono") or r.get("telefono")
        )
        if tel and tel == target:
            for k in (
                "Nombre",
                "Nombre y apellido",
                "Apellido y nombre",
                "Empleado",
                "Persona",
                "nombre",
                "nombre y apellido",
                "apellido y nombre",
                "empleado",
                "persona",
            ):
                v = s(r.get(k))
                if v:
                    return v
    return ""



def send_template_whatsapp_norm(to_e164: str, name: str) -> str | None:
    """
    Envía la plantilla de WhatsApp usando la variable {{1}} = nombre.
    Devuelve el SID del mensaje o None si falla.
    """
    try:
        # Si usás Content API (ContentSid), seteá ContentVariables con el nombre
        # name puede venir vacío; si tu plantilla requiere el campo, puedes poner un fallback "!"
        variables = json.dumps({"1": name or "!"})

        msg = twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to_e164,
            content_sid=TWILIO_CONTENT_SID,       # <-- tu ContentSid (HXxxxxxxxx)
            content_variables=variables,
            # Si usás MessagingServiceSid, incluí messaging_service_sid=...
            status_callback=STATUS_CALLBACK_URL,
        )
        return msg.sid
    except Exception as e:
        print("ERROR send_template_whatsapp_norm:", e)
        return None


def send_template(to_phone: str, period_label: str, cuil: str | None = None) -> str | None:
    """
    Envía la plantilla de WhatsApp (Content API) con variables:
      {{1}} = período (mm/aaaa)
      {{2}} = cuil (opcional)
    Devuelve MessageSid o None si falla.
    """
    try:
        vars_dict = {"1": period_label}
        if cuil:
            vars_dict["2"] = cuil

        msg = twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to_phone,                  # ⚠️ usar siempre el destino que llega
            content_sid=TWILIO_CONTENT_SID,
            content_variables=json.dumps(vars_dict),
            status_callback=STATUS_CALLBACK_URL,
        )
        print("DEBUG send_template OK:", msg.sid)
        return msg.sid
    except Exception as e:
        print("ERROR send_template Twilio:", e)
        return None


def empty_twiml():
    return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    mimetype="text/xml")

@app.route("/admin/send_template_all", methods=["POST"])
def admin_send_template_all():
    try:
        period_raw = request.form.get("period") or PERIODO_ACTUAL or get_current_period_label()
        period_lbl = norm_period_label(period_raw)
        dry_run = (request.form.get("dry_run") or "").lower() in ("1", "true", "yes", "y")
        limit = int(request.form.get("limit") or 0)  # 0 = sin límite

        rows = read_envios_rows()
        if not rows:
            return {"ok": False, "error": "no hay filas de envíos"}, 400

        sent = []
        skipped = []
        total = 0

        for r in rows:
            # columnas esperadas
            telefono = s(r.get("Telefono") or r.get("Teléfono"))

            # usamos Archivo_norm si existe, si no, caemos a otras
            archivo_norm = s(
                r.get("Archivo_norm")
                or r.get("Archivo")
                or r.get("CUIL")
                or r.get("Cuil")
            )

            nombre = s(
                r.get("Nombre")
                or r.get("Nombre y apellido")
                or r.get("Apellido y nombre")
                or r.get("Empleado")
                or r.get("Persona")
            )

            # Validaciones mínimas
            if not telefono:
                skipped.append({"reason": "sin_telefono", "row": r})
                continue
            if not archivo_norm:
                skipped.append({"reason": "sin_archivo_norm", "row": r})
                continue

            # Canonicalizamos a formato whatsapp:+54911...
            try:
                to = normalize_to_whatsapp_e164(telefono)
            except Exception:
                skipped.append({"reason": "telefono_invalido", "row": r})
                continue

            # Chequeamos si existe PDF para ese período
            pdf_id = find_pdf_for_archivo_and_period(archivo_norm, period_lbl)
            if not pdf_id:
                skipped.append({"reason": "sin_pdf_periodo", "row": r})
                continue

            if dry_run:
                sent.append({
                    "to": to,
                    "name": nombre,
                    "archivo_norm": archivo_norm,
                    "period": period_lbl,
                    "dry_run": True,
                })
                total += 1
            else:
                # Usá la función que ya tengas para mandar la plantilla
                # (acá supongo que la tuya es send_template_whatsapp_norm)
                sid = send_template_whatsapp_norm(to, nombre)

                if sid:
                    sent.append(
                        {
                            "archivo_norm": archivo_norm,
                            "to": to,
                            "nombre": nombre,
                            "sid": sid,
                            "period": period_lbl,
                        }
                    )

                    # Guardamos en sqlite qué archivo y período le corresponde a este número
                    save_pending_view(to, archivo_norm, period_lbl)

                    total += 1
                else:
                    skipped.append({"reason": "twilio_error_envio_plantilla", "row": r})

            if limit and total >= limit:
                break

        return {
            "ok": True,
            "period": period_lbl,
            "dry_run": dry_run,
            "sent_count": len(sent),
            "skipped_count": len(skipped),
            "sent": sent[:200],
            "skipped": skipped[:200],
        }, 200

    except Exception as e:
        print("ERROR /admin/send_template_all:", e)
        return {"ok": False, "error": str(e)}, 500


@app.route("/twilio/status", methods=["POST"])
def twilio_status():
    data = request.form.to_dict()
    print("STATUS CALLBACK:", data)
    # data["MessageStatus"] puede ser: queued/sent/delivered/read/failed
    # data["To"], data["From"], data["MessageSid"]
    return ("", 204)


# ==========================
#  Helpers de respuesta Twilio
# ==========================

def twiml_message(text: str) -> Response:
    """
    Devuelve un Response con TwiML <Message> simple.
    """
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{text}</Message>
</Response>"""
    return Response(twiml, mimetype="text/xml")


def twiml_message_with_link(text: str, link: str) -> Response:
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>
        <Body>{text}</Body>
        <Media>{link}</Media>
    </Message>
</Response>"""
    return Response(twiml, mimetype="text/xml")

def s(x) -> str:
    """Convierte a string y hace strip sin romper si x es int/float/None."""
    if x is None:
        return ""
    return str(x).strip()


def send_period_menu_via_text(
    telefono_whatsapp: str,
    telefono_norm: str,
    periods: List[str],
    offset: int,
) -> Response:
    """
    Construye un menú de períodos (texto plano) y devuelve TwiML.

    - Muestra hasta 3 períodos a partir de `offset`.
    - Opción extra 'Más' si hay más períodos.
    - Guarda en la sesión qué número corresponde a qué período.
    """
    session = get_session(telefono_norm)
    session["state"] = "WAITING_OPTION"
    session["offset"] = offset
    session["periods"] = periods
    session["options_map"] = {}

    slice_periods = periods[offset : offset + 3]
    has_more = (offset + 3) < len(periods)

    lines = ["Encontré varios recibos asociados a tu número.", "Elegí una opción:"]

    # Numeramos opciones 1..N
    option_number = 1
    for p in slice_periods:
        lines.append(f"{option_number}) {p}")
        session["options_map"][str(option_number)] = p
        option_number += 1

    if has_more:
        lines.append(f"{option_number}) Más períodos anteriores")
        session["options_map"][str(option_number)] = "__MAS__"

    lines.append("")
    lines.append("Respondé con el número de la opción.")

    text = "\n".join(lines)
    return twiml_message(text)

def get_archivo_from_envios(telefono_whatsapp: str) -> Optional[str]:
    """
    Dado un telefono en formato 'whatsapp:+54911...', busca en el Excel de ENVÍOS
    y devuelve el 'archivo_norm' más reciente para ese número.
    """
    tel_norm = canonicalize_phone(telefono_whatsapp)
    envios_df = download_envios_excel()
    archivo_norm = get_archivo_for_phone(tel_norm, envios_df)
    return archivo_norm

def build_twilio_response(text: str, media_url: Optional[str] = None) -> Response:
    """
    Construye una respuesta TwiML para Twilio con un mensaje de texto
    y opcionalmente un adjunto (media_url).
    """
    resp = MessagingResponse()
    msg = resp.message(text)
    if media_url:
        msg.media(media_url)
    return Response(str(resp), mimetype="text/xml")

def send_pdf_via_twilio_media(to_whatsapp: str, media_url: str, caption: str = "") -> str:
    """
    Envía un mensaje de WhatsApp con el PDF adjunto.

    - to_whatsapp: ej. 'whatsapp:+5491136222572'
    - media_url: URL directa al PDF en Drive
    - caption: texto que acompaña al PDF
    """
    # Usa el mismo cliente y FROM que usás para las plantillas
    msg = twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_FROM,   # mismo que en send_template_whatsapp_norm
        to=to_whatsapp,
        body=caption or None,
        media_url=[media_url],
    )
    print("DEBUG send_pdf_via_twilio_media SID:", msg.sid)
    return msg.sid

import os
from datetime import datetime

def get_current_period_label():
    # Intentamos leer de una variable de entorno en Render
    label_env = os.getenv("PERIODO_ACTUAL")
    if label_env:
        return label_env

    # Fallback: período igual al mes actual, formato mm/aaaa
    return datetime.now().strftime("%m/%Y")

# ==========================
#  Lógica de los caminos
# ==========================
def handle_view_current(from_whatsapp: str):
    print(f"DEBUG handle_view_current, from_number: {from_whatsapp}")

    # 1) Buscar en sqlite el último envío que hicimos a este número
    pending = get_last_pending_view(from_whatsapp)
    if not pending:
        msg = (
            "No encontré ningún recibo pendiente para este número 😕.\n"
            "Si creés que es un error, avisá a RRHH para que lo revisen 🙏."
        )
        return build_twilio_response(msg)

    archivo_norm, period_label = pending
    print(f"DEBUG handle_view_current -> archivo_norm: {archivo_norm}, period_label: {period_label}")

    # 2) Buscar el PDF en Drive usando archivo_norm + period_label
    file_id = find_pdf_for_archivo_and_period(archivo_norm, period_label)
    if not file_id:
        msg = (
            f"No pude encontrar el PDF de tu recibo para el período {period_label} 😕.\n"
            "Avisá a RRHH para que lo revisen 🙏."
        )
        return build_twilio_response(msg)

    # 3) Usar el proxy /media/<file_id>, como en el camino de menú
    media_url = build_media_url_for_twilio(file_id)
    caption = f"Acá tenés tu recibo de sueldo de {period_label} 📄"

    send_pdf_via_twilio_media(from_whatsapp, media_url, caption=caption)

    # Twilio no necesita más texto, con status 200 ya está
    return ("", 200)



def handle_period_selection(
    telefono_whatsapp: str,
    period_label: str,
) -> Response:
    """
    Camino B: el usuario eligió explícitamente un período (ya sea por menú o, si quisieras, escribiéndolo).
    """
    telefono_norm = normalize_phone(telefono_whatsapp)
    envios_df = download_envios_excel()
    archivo_norm = get_archivo_for_phone(telefono_norm, envios_df)

    if not archivo_norm:
        return twiml_message(
            "⚠️ No encontré ningún recibo asociado a tu número en el sistema."
        )

    pdf_id = find_pdf_for_archivo_and_period(archivo_norm, period_label)
    if not pdf_id:
        return twiml_message(
            f"⚠️ Encontré un registro para el período {period_label}, "
            "pero el archivo no está disponible en este momento. "
            "Probá más tarde o contactá con RRHH."
        )
    text = f"✅ Acá tenés tu recibo del período {period_label}."


    # link = build_drive_public_link(pdf_id)   # o get_drive_download_url(pdf_id)
    link = build_media_url_for_twilio(pdf_id)
    print("DEBUG final_media_link:", link)
    return twiml_message_with_link(text, link)


PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

def build_media_url_for_twilio(file_id: str) -> str:
    # Twilio necesita URL absoluta y pública
    base = PUBLIC_BASE_URL or "https://twilio-webhook-lddc.onrender.com"
    return f"{base}/media/{file_id}"


def handle_show_periods_menu(telefono_whatsapp: str) -> Response:
    """
    Camino B: el usuario manda un texto libre y le ofrecemos el menú de períodos.
    """
    telefono_norm = normalize_phone(telefono_whatsapp)
    envios_df = download_envios_excel()
    archivo_norm = get_archivo_for_phone(telefono_norm, envios_df)

    if not archivo_norm:
        return twiml_message(
            "⚠️ No encontré ningún recibo asociado a tu número de WhatsApp.\n"
            "Verificá que estés usando el número correcto o contactá con RRHH."
        )

    periods = list_periods_for_archivo(archivo_norm)

    if not periods:
        return twiml_message(
            "⚠️ No encontré archivos de recibos asociados a tu número en Drive."
        )

    # Un solo período → se lo mandamos directo
    if len(periods) == 1:
        period_label = periods[0]
        return handle_period_selection(telefono_whatsapp, period_label)

    # Varios períodos → arrancamos el menú desde offset 0
    return send_period_menu_via_text(
        telefono_whatsapp,
        telefono_norm,
        periods,
        offset=0,
    )


def handle_menu_option(telefono_whatsapp: str, body: str) -> Response:
    """
    Camino B: el usuario está respondiendo a un menú (esperamos un número).
    """
    telefono_norm = normalize_phone(telefono_whatsapp)
    session = get_session(telefono_norm)

    options_map = session.get("options_map", {})
    choice = body.strip()

    if choice not in options_map:
        # Respuesta no reconocida → re-enviamos el mismo menú
        return twiml_message(
            "⚠️ No entendí la opción. Por favor, respondé con el número de la lista."
        )

    value = options_map[choice]

    # Opción 'Más...'
    if value == "__MAS__":
        periods = session.get("periods", [])
        offset = session.get("offset", 0)
        new_offset = offset + 3
        if new_offset >= len(periods):
            # No hay más, volvemos a mostrar el último menú sin 'Más'
            new_offset = offset
        return send_period_menu_via_text(
            telefono_whatsapp,
            telefono_norm,
            periods,
            offset=new_offset,
        )

    # Opción de período concreto
    period_label = value
    # Reseteamos el estado
    session["state"] = "IDLE"
    session["options_map"] = {}
    return handle_period_selection(telefono_whatsapp, period_label)

@app.route("/media/<file_id>", methods=["GET"])
def media_proxy(file_id):
    """
    Proxy para servir PDFs de Drive a Twilio/WhatsApp sin requerir login.
    """
    service = build_drive_service()
    # Descargo el binario desde Drive
    request_drive = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request_drive)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)

    # Intento obtener el nombre real (opcional)
    try:
        meta = service.files().get(fileId=file_id, fields="name").execute()
        filename = meta.get("name", "documento.pdf")
    except Exception:
        filename = "documento.pdf"

    # Envío el PDF como respuesta HTTP pública
    return send_file(
        fh,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=filename,  # Flask 2.x
        max_age=300,             # cache 5 min
        etag=False
    )


# ==========================
#  Webhook Twilio
# ==========================

from flask import request
from twilio.twiml.messaging_response import MessagingResponse

@app.route("/twilio/webhook", methods=["POST"])
def twilio_webhook():
    form = request.form.to_dict()
    print("=== TWILIO WEBHOOK FORM ===")
    print(form)

    from_whatsapp = form.get("From")  # ej: "whatsapp:+5491136222572"
    body = (form.get("Body") or "").strip()
    button_payload = form.get("ButtonPayload") or ""
    button_text = form.get("ButtonText") or ""

    # Caso: tocaron el botón "Sí, visualizar"
    if button_payload == "VIEW_NOW" or button_text.lower().startswith("sí, visualizar"):
        return handle_view_current(from_whatsapp)

    # Si escribe algo tipo "ver", "ver recibo", etc., también podés engancharlo
    if body.lower() in ("ver", "ver recibo", "ver recibo de sueldo", "si, visualizar", "sí, visualizar"):
        return handle_view_current(from_whatsapp)

    # Respuesta por defecto
    msg = (
        "Hola 👋\n"
        "Tu recibo de sueldo está disponible.\n"
        "Usá el botón *Sí, visualizar* para recibirlo, o escribí *ver*."
    )
    return build_twilio_response(msg)


@app.route("/ping")
def ping():
    return "pong", 200

import threading
import time
import requests

def keep_alive():
    url = "https://twilio-webhook-lddc.onrender.com/ping"
    while True:
        try:
            print("KEEP-ALIVE: haciendo ping...")
            requests.get(url, timeout=10)
        except Exception as e:
            print("KEEP-ALIVE error:", e)
        time.sleep(60)

t = threading.Thread(target=keep_alive, daemon=True)
t.start()

if __name__ == "__main__":
    # Para pruebas locales
    app.run(host="0.0.0.0", port=5000, debug=True)
