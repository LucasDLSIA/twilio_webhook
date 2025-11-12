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

from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaIoBaseDownload

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
    Normaliza el teléfono que viene de Twilio a la misma forma que usamos en el Excel:
    últimos 10 dígitos.
    """
    return canonicalize_phone(whatsapp_from)


def canonicalize_phone(num: str) -> str:
    """
    Deja el teléfono en un formato comparable:
    - Saca todo lo que no sea dígito.
    - Se queda con los últimos 10 dígitos (ej: 11XXXXXXXX).
    """
    if not num:
        return ""
    num = str(num)
    num = num.replace("whatsapp:", "")
    # Solo dígitos
    digits = re.sub(r"\D", "", num)
    # Nos quedamos con los últimos 10 (si tiene menos, devuelve lo que haya)
    return digits[-10:] if len(digits) > 10 else digits


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
    Dado el CUIL (archivo_norm) y un período 'mm/aaaa',
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
        print("   - file:", f.get("id"), f.get("name"),
              "| carpeta:", folder_name, "| label:", label)
        if label == period_label:
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
    # normalizamos: quitamos todo menos dígitos para comparar
    want = re.sub(r"\D", "", to_whatsapp)
    for r in rows:
        tel = r.get("telefono", "")
        arc = r.get("archivo_norm", "") or r.get("archivo", "")
        if not tel or not arc:
            continue
        tclean = re.sub(r"\D", "", tel)
        if tclean.endswith(want) or want.endswith(tclean):
            return arc.strip()
    return None

import json
import pandas as pd

def resolve_name_for_phone(phone_e164: str) -> str:
    """
    Busca el nombre en el Excel de envíos por número de teléfono.
    Devuelve string (puede ser vacío si no lo encuentra).
    """
    rows = read_envios_rows()  # tu función que lee el Excel de envíos
    # normalizamos para comparar
    p = canonicalize_phone(phone_e164)
    for r in rows:
        tel = canonicalize_phone(str(r.get("Telefono") or r.get("Teléfono") or ""))
        if tel and tel == p:
            # probamos varias columnas típicas de nombre
            for k in ("Nombre", "Nombre y apellido", "Apellido y nombre", "Empleado", "Persona"):
                v = (r.get(k) or "").strip()
                if v:
                    return v
    return ""

def send_template_with_name(to_e164: str, name: str) -> str | None:
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
        print("ERROR send_template_with_name:", e)
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


#@app.route("/admin/send_template_one", methods=["POST"])
#def admin_send_template_one():
    to = normalize_to_whatsapp_e164(request.form.get("to", ""))
    # period puede seguir viniendo para tu logging o trazabilidad, pero NO lo usamos para la plantilla
    period_raw = request.form.get("period") or PERIODO_ACTUAL or ""

    if not to:
        return {"ok": False, "error": "falta 'to'"}, 400

    # buscamos el NOMBRE para la plantilla {{1}}
    try:
        name = resolve_name_for_phone(to)  # <- ver helper más abajo
    except Exception:
        name = ""  # si no hay nombre, mandamos vacío (mejor que fallar)

    sid = send_template_with_name(to, name)  # <- ver helper más abajo
    if not sid:
        return {"ok": False, "error": "no se pudo enviar la plantilla"}, 500

    # IMPORTANTE: NO enviar PDF acá.
    return {"ok": True, "sid": sid}, 200

def empty_twiml():
    return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    mimetype="text/xml")


@app.route("/admin/send_template_all", methods=["POST"])
def admin_send_template_all():
    """
    Envia la plantilla a todas las personas que:
      - Tienen telefono válido
      - Tienen 'Archivo' asignado en el Excel de envíos
      - Existe PDF para ese 'Archivo' en el periodo elegido (por defecto PERIODO_ACTUAL)
    No envía el PDF acá. Ese se envía cuando el usuario toca el botón (VIEW_NOW) en el webhook.
    """
    try:
        period_raw = request.form.get("period") or PERIODO_ACTUAL or ""
        period_lbl = norm_period_label(period_raw)
        dry_run    = (request.form.get("dry_run") or "").lower() in ("1","true","yes","y")
        limit      = int(request.form.get("limit") or 0)  # 0 = sin límite

        rows = read_envios_rows()
        if not rows:
            return {"ok": False, "error": "no hay filas de envíos"}, 400

        sent = []
        skipped = []
        total = 0

        for r in rows:
            # columnas esperadas
            telefono = r.get("Telefono") or r.get("Teléfono")
            archivo  = r.get("Archivo") or r.get("CUIL") or r.get("Cuil")
            nombre   = (
                r.get("Nombre") or
                r.get("Nombre y apellido") or
                r.get("Apellido y nombre") or
                r.get("Empleado") or
                r.get("Persona") or
                ""
            )
            telefono = (telefono or "").strip()
            archivo  = (str(archivo) or "").strip()
            nombre   = (nombre or "").strip()

            # Validaciones mínimas
            if not telefono:
                skipped.append({"reason": "sin_telefono", "row": r})
                continue
            if not archivo:
                skipped.append({"reason": "sin_archivo", "row": r})
                continue

            # Canonicalizar y prefijo WhatsApp
            try:
                to = normalize_to_whatsapp_e164(telefono)
            except Exception:
                skipped.append({"reason": "telefono_invalido", "row": r})
                continue

            # Verificar existencia de PDF para el periodo
            pdf_id = find_pdf_for_archivo_and_period(archivo, period_lbl)
            if not pdf_id:
                skipped.append({"reason": "sin_pdf_periodo", "row": r})
                continue

            # Si es dry_run no enviamos, solo listamos candidatos
            if dry_run:
                sent.append({"to": to, "name": nombre, "archivo": archivo, "period": period_lbl, "dry_run": True})
                total += 1
            else:
                # Enviar plantilla con {{1}} = nombre
                sid = send_template_with_name(to, nombre)
                if sid:
                    sent.append({"to": to, "name": nombre, "archivo": archivo, "period": period_lbl, "sid": sid})
                    total += 1
                else:
                    skipped.append({"reason": "twilio_error_envio_plantilla", "row": r})

            # Límite opcional para pruebas
            if limit and total >= limit:
                break

        return {
            "ok": True,
            "period": period_lbl,
            "dry_run": dry_run,
            "sent_count": len(sent),
            "skipped_count": len(skipped),
            "sent": sent[:200],        # recorta para no explotar la respuesta
            "skipped": skipped[:200]
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


# ==========================
#  Lógica de los caminos
# ==========================

def handle_view_current(telefono_whatsapp: str) -> Response:
    period_lbl = norm_period_label(PERIODO_ACTUAL)
    tel_norm = canonicalize_phone(telefono_whatsapp)

    envios_df = download_envios_excel()
    archivo_norm = get_archivo_for_phone(tel_norm, envios_df)  # tu mapping a "Archivo"/CUIL
    if not archivo_norm:
        return empty_twiml()

    pdf_id = find_pdf_for_archivo_and_period(archivo_norm, period_lbl)
    if not pdf_id:
        return empty_twiml()

    link = build_media_url_for_twilio(pdf_id)
    txt  = f"✅ Acá tenés tu recibo del período {period_lbl}."

    # responder con TwiML que incluye el link
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Message>
    <Body>{txt}</Body>
    <Media>{link}</Media>
  </Message>
</Response>"""
    return Response(twiml, mimetype="text/xml")




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

@app.route("/twilio/webhook", methods=["POST"])
def twilio_webhook():
    from_whatsapp = request.form.get("From", "")
    btn_payload   = request.form.get("ButtonPayload", "")  # Twilio Content API

    # 1) Solo si viene botón "VIEW_NOW", disparamos el PDF del período actual
    if btn_payload in ("VIEW_NOW", "VIEW_CURRENT"):
        return handle_view_current(from_whatsapp)  # <- tu función que arma y manda el PDF

    # 2) Si querés, podés ignorar cualquier otro payload o texto
    #    o responder con un mensaje guía.
    return empty_twiml()


if __name__ == "__main__":
    # Para pruebas locales
    app.run(host="0.0.0.0", port=5000, debug=True)
