# app.py
import os
import io
import re
from typing import Dict, Tuple, Optional

import pandas as pd
from flask import Flask, request, Response
from twilio.rest import Client

from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaIoBaseDownload


app = Flask(__name__)

# ============ CONFIG ============

# Variables de entorno que DEBEN existir en Render / local:
#  - TWILIO_ACCOUNT_SID
#  - TWILIO_AUTH_TOKEN
#  - TWILIO_WHATSAPP_FROM  (ej: whatsapp:+16503003952)
#  - TWILIO_TEMPLATE_SID   (ej: HX1ad560c0a431958de08a5795b5a1790c)

ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
FROM_WPP = os.environ["TWILIO_WHATSAPP_FROM"]
TEMPLATE_SID = os.environ["TWILIO_TEMPLATE_SID"]

TEST_PDF_URL = os.environ["TEST_PDF_URL"]

# IDs de Drive (los que ya creaste como env)
FOLDER_ID = os.environ["DRIVE_ROOT_FOLDER_ID"]   # carpeta con los PDFs
ENVIOS_FILE_ID = os.environ["ENVIOS_FILE_ID"]    # archivo envios.xlsx en Drive

# Service account (secret file en Render)
SERVICE_ACCOUNT_FILE = (
    "/etc/secrets/Service_account.json"
    if os.path.exists("/etc/secrets/Service_account.json")
    else "Service_account.json"
)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

def normalize_phone(num: object) -> Optional[str]:
    if num is None:
        return None
    s = str(num).strip()
    # sacamos espacios, guiones, paréntesis
    s = re.sub(r"[ \-\(\)]", "", s)
    # si no empieza con +, asumimos Argentina +549
    if not s.startswith("+"):
        s = s.lstrip("+")
        s = "+549" + s
    elif not s.startswith("+549"):
        s = "+549" + s.lstrip("+")
    return s

def drive_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def load_envios_df() -> pd.DataFrame:
    """
    Descarga envios.xlsx desde Drive (ENVIOS_FILE_ID) y lo carga en un DataFrame.
    """
    svc = drive_service()
    request = svc.files().get_media(fileId=ENVIOS_FILE_ID)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)

    done = False
    while not done:
        status, done = downloader.next_chunk()
        if status:
            print(f"⬇️ Descargando envios.xlsx: {int(status.progress() * 100)}%")

    fh.seek(0)
    df = pd.read_excel(fh, dtype=str)
    return df

def list_pdfs_in_folder() -> Dict[str, str]:
    """
    Lista los PDFs en la carpeta FOLDER_ID.
    Devuelve un dict {nombre_normalizado: file_id} y escribe los nombres reales en logs.
    """
    svc = drive_service()
    items = []
    page_token = None

    while True:
        resp = svc.files().list(
            q=f"'{FOLDER_ID}' in parents and trashed = false",
            fields="nextPageToken, files(id,name,mimeType)",
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageToken=page_token,
        ).execute()

        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    pdf_index: Dict[str, str] = {}

    print("📚 PDFs encontrados en la carpeta de Drive:")
    for it in items:
        if it.get("mimeType") == "application/pdf":
            raw_name = it.get("name", "")
            name_norm = raw_name.strip().lower()
            pdf_index[name_norm] = it["id"]
            print(f"   - {raw_name}")

    if not pdf_index:
        print("⚠️ No se encontró ningún PDF en la carpeta.")

    return pdf_index

def check_pdf_for_number(whatsapp_from: str) -> Tuple[bool, Optional[str]]:
    """
    Usa envios.xlsx para ver qué archivo le corresponde a este número
    y si ese archivo existe en la carpeta de Drive.

    Devuelve (True, nombre_pdf) si existe
            (False, nombre_pdf) si había fila pero no está el archivo
            (False, None) si ni siquiera hay fila para ese número
    """
    # whatsapp:+549...
    raw = whatsapp_from.replace("whatsapp:", "")
    num_norm = normalize_phone(raw)
    print(f"🔎 Buscando fila en envios.xlsx para {num_norm} ...")

    df = load_envios_df()

    # Asegurar nombres de columnas
    lc = {c.lower(): c for c in df.columns}
    for needed in ("nombre", "telefono", "archivo"):
        if needed not in lc:
            raise RuntimeError(f"Falta columna '{needed}' en envios.xlsx (se espera '{needed}')")

    df = df.rename(
        columns={
            lc["nombre"]: "nombre",
            lc["telefono"]: "telefono",
            lc["archivo"]: "archivo",
        }
    )

    df["telefono_norm"] = df["telefono"].apply(normalize_phone)
    df["archivo_norm"] = (
        df["archivo"]
        .astype(str)
        .str.strip()
        .str.lower()
        .apply(lambda x: x if x.endswith(".pdf") else x + ".pdf")
    )

    row = df.loc[df["telefono_norm"] == num_norm].head(1)
    if row.empty:
        print("⚠️ No encontré fila para este número en envios.xlsx")
        return False, None

    nombre = (row["nombre"].iloc[0] or "").strip()
    archivo = row["archivo_norm"].iloc[0]
    print(f"✅ Fila encontrada en Excel: nombre={nombre}, archivo={archivo}")

    pdf_index = list_pdfs_in_folder()
    if archivo in pdf_index:
        print(f"✅ El archivo {archivo} existe en la carpeta de Drive.")
        return True, archivo
    else:
        print(f"⚠️ El archivo {archivo} NO existe en la carpeta de Drive (al menos con ese nombre).")
        return False, archivo


def twilio_client():
    return Client(ACCOUNT_SID, AUTH_TOKEN)



@app.route("/twilio/webhook", methods=["GET", "POST"])
def twilio_webhook():
    frm = (request.values.get("From", "") or "").strip()
    body = (request.values.get("Body", "") or "").strip()
    btn_text = (request.values.get("ButtonText", "") or "").strip()
    btn_payload = (request.values.get("ButtonPayload", "") or "").strip()

    print("📥 Mensaje entrante (Twilio):")
    print(f"   From:          {frm}")
    print(f"   Body:          {body}")
    print(f"   ButtonText:    {btn_text}")
    print(f"   ButtonPayload: {btn_payload}")
    print("----------")

    client = twilio_client()

    # ========= FASE 2: botón "Sí, visualizar" =========
    # Según tus logs, el payload del botón es VIEW_NOW
        # ========= FASE 2: botón "Sí, visualizar" =========
    if btn_payload == "VIEW_NOW" or "visualizar" in btn_text.lower():
        print("   ✅ Botón VIEW_NOW detectado, verificando en Excel + Drive...")

        try:
            ok, archivo = check_pdf_for_number(frm)
            if ok:
                reply = f"✅ Encontré un PDF para tu número: {archivo}"
            else:
                if archivo:
                    reply = (
                        f"⚠️ Encontré un registro tuyo en el Excel, pero el archivo "
                        f"'{archivo}' no aparece en la carpeta de Drive. Consultá más tarde."
                    )
                else:
                    reply = (
                        "⚠️ No encontré ningún registro asociado a tu número en el Excel. "
                        "Por favor, consultá con RRHH o soporte."
                    )
        except Exception as e:
            print("❌ Error consultando Drive/Excel:", e)
            reply = "⚠️ Ocurrió un error al consultar tus datos. Probá más tarde o contactá a soporte."

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{reply}</Message>
</Response>"""
        return Response(twiml, mimetype="text/xml")


    # ========= FASE 1: cualquier texto normal → enviar plantilla =========
    if body:
        print("   ⚙️ Mandando plantilla con botones (conversación iniciada)...")
        msg = client.messages.create(
            from_=FROM_WPP,
            to=frm,
            content_sid=TEMPLATE_SID,
            # Si tu plantilla tiene variables {{1}}, {{2}}, etc.,
            # podés agregar content_variables aquí más adelante.
        )
        print(f"   📤 Plantilla enviada: sid={msg.sid}, status={msg.status}")

        # Ya mandamos un mensaje usando la API de Twilio,
        # al webhook solo le devolvemos un Response vacío.
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response></Response>"""
        return Response(twiml, mimetype="text/xml")

    # ========= Caso raro: ni botón ni texto =========
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>👋 Para ver tu recibo, escribí un mensaje o usá el botón de la plantilla.</Message>
</Response>"""
    return Response(twiml, mimetype="text/xml")


if __name__ == "__main__":
    # Para pruebas locales
    app.run(host="0.0.0.0", port=5000, debug=True)
