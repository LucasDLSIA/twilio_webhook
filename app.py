# app.py
import os
import io
import re
import requests
from typing import Dict, Tuple, Optional, List

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

# Período actual para Camino A (formato mm/aaaa)
PERIODO_ACTUAL = "10/2025"  # ej. "10/2025"

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
    """
    Camino A:
    - payload VIEW_CURRENT
    - envía el recibo del PERIODO_ACTUAL, si existe
    - si no existe, no envía nada visible
    """
    if not PERIODO_ACTUAL:
        # Sin período actual configurado, no respondemos nada
        return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                        mimetype="text/xml")

    telefono_norm = normalize_phone(telefono_whatsapp)
    envios_df = download_envios_excel()

    archivo_norm = get_archivo_for_phone(telefono_norm, envios_df)

    print("DEBUG handle_show_periods_menu")
    print("  telefono_whatsapp:", telefono_whatsapp)
    print("  telefono_norm:", telefono_norm)
    try:
        print("  Primeras filas de envios_df (telefono, telefono_norm, archivo_norm):")
        print(envios_df[["telefono", "telefono_norm", "archivo_norm"]].head(20))
    except Exception as e:
        print("  Error mostrando envios_df:", e)
    print("  archivo_norm encontrado:", archivo_norm)

    if not archivo_norm:
        # No hay registro para este teléfono en el Excel
        # En Camino A decidiste NO responder nada
        return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                        mimetype="text/xml")

    pdf_id = find_pdf_for_archivo_and_period(archivo_norm, PERIODO_ACTUAL)
    if not pdf_id:
        # No está el archivo en Drive → silencio
        return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                        mimetype="text/xml")

    link = get_drive_download_url(pdf_id)
    print("DEBUG final_media_link:", link)
    print("DEBUG fetchable?:", is_url_fetchable(link))

    text = f"✅ Acá tenés tu recibo de sueldo del período {PERIODO_ACTUAL}."
    return twiml_message_with_link(text, link)


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

    link = get_drive_download_url(pdf_id)
    print("DEBUG final_media_link:", link)
    print("DEBUG fetchable?:", is_url_fetchable(link))

    text = f"✅ Acá tenés tu recibo de sueldo del período {period_label}."
    return twiml_message_with_link(text, link)


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


# ==========================
#  Webhook Twilio
# ==========================

@app.route("/twilio/webhook", methods=["POST"])
def twilio_webhook():
    from_whatsapp = request.values.get("From", "")
    body = request.values.get("Body", "").strip()
    btn_text = request.values.get("ButtonText", "")
    btn_payload = request.values.get("ButtonPayload", "")

    # 1) Si viene ButtonPayload, damos prioridad (Camino A en este ejemplo)
    if btn_payload:
        if btn_payload == "VIEW_CURRENT":
            return handle_view_current(from_whatsapp)
        # Si en el futuro querés manejar otros payloads, lo hacés acá.
        # Por ahora, cualquier otro payload inesperado:
        return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                        mimetype="text/xml")

    # 2) Si no hay payload, vemos el estado de la sesión del usuario (Camino B)
    telefono_norm = normalize_phone(from_whatsapp)
    session = get_session(telefono_norm)

    if session.get("state") == "WAITING_OPTION" and body:
        # Interpreto que está respondiendo al menú
        return handle_menu_option(from_whatsapp, body)

    # 3) Cualquier mensaje de texto nuevo → mostramos o arrancamos el menú de períodos
    if body:
        return handle_show_periods_menu(from_whatsapp)

    # 4) Caso raro: ni botón ni texto
    return twiml_message(
        "👋 Para ver tu recibo, escribí un mensaje (por ejemplo 'recibo') y te "
        "voy a mostrar los períodos disponibles."
    )


if __name__ == "__main__":
    # Para pruebas locales
    app.run(host="0.0.0.0", port=5000, debug=True)
