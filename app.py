# app.py
import os
from flask import Flask, request, Response
from twilio.rest import Client

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

# PDF de prueba (URL pública cualquiera)
TEST_PDF_URL = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"


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
    if btn_payload == "VIEW_NOW" or "visualizar" in btn_text.lower():
        print("   ✅ Botón VIEW_NOW detectado, enviando PDF de prueba...")

        msg = client.messages.create(
            from_=FROM_WPP,
            to=frm,
            body="📎 Aquí tienes tu PDF de prueba.",
            media_url=[TEST_PDF_URL],
        )
        print(f"   📤 PDF enviado: sid={msg.sid}, status={msg.status}")

        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>Te envié un PDF de prueba ✅</Message>
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
