# template_sender.py
import os
import json
import re
import threading
import requests
from flask import Flask, request, Response
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from dotenv import load_dotenv
load_dotenv()


app = Flask(__name__)

# ==========
# ENV / CONFIG (Camino A)
# ==========
TWILIO_ACCOUNT_SID   = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")   # ej: "whatsapp:+14155238886" o tu sender WA aprobado
TWILIO_TEMPLATE_SID  = os.getenv("TWILIO_TEMPLATE_SID")    # SID de la plantilla (Content API)

# Apuntan a tu app principal en Render (agregalas si no existen):
STATUS_CALLBACK_URL  = os.getenv("STATUS_CALLBACK_URL")    # ej: https://twilio-webhook-lddc.onrender.com/twilio/status
MAIN_APP_WEBHOOK_URL = os.getenv("MAIN_APP_WEBHOOK_URL")   # ej: https://twilio-webhook-lddc.onrender.com/twilio/webhook

# Período default (mm/aaaa) si no se pasa por POST
PERIODO_ACTUAL       = os.getenv("PERIODO_ACTUAL")

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ==========
# HELPERS
# ==========
def _mask(s: str | None, keep: int = 4) -> str:
    if not s: return "None"
    return ("*" * max(0, len(s) - keep)) + s[-keep:]

def _print_routes_once():
    print("=== ROUTES MAPPED ===")
    for r in app.url_map.iter_rules():
        print(f"  {r.rule} -> {','.join(sorted(r.methods or []))}")
    print("=====================")

def _main_base() -> str:
    """Base de tu app principal (quita /twilio/webhook)."""
    if not MAIN_APP_WEBHOOK_URL:
        raise RuntimeError("Falta MAIN_APP_WEBHOOK_URL")
    return MAIN_APP_WEBHOOK_URL.rsplit("/twilio/webhook", 1)[0]

def fix_to_whatsapp(raw: str) -> str:
    """Normaliza a formato whatsapp:+<digits> preservando el +."""
    if not raw:
        return raw
    s = raw.strip()
    if s.lower().startswith("whatsapp:"):
        num = s[len("whatsapp:"):].strip()
    else:
        num = s
    if num.startswith("+"):
        digits = "+" + re.sub(r"[^\d]", "", num[1:])
    else:
        digits = "+" + re.sub(r"[^\d]", "", num)
    return "whatsapp:" + digits

# ---- Delegaciones a tu app principal (Render)
def load_envios_rows() -> list[dict]:
    """
    POST {base}/admin/envios_list  -> { ok: true, rows: [ {telefono, archivo_norm}, ... ] }
    """
    url = _main_base() + "/admin/envios_list"
    r = requests.post(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"/admin/envios_list no ok: {data}")
    rows = data.get("rows", [])
    out = []
    for row in rows:
        tel = str(row.get("telefono","")).strip()
        arc = str(row.get("archivo_norm","")).strip()
        if tel and arc:
            out.append({"telefono": tel, "archivo_norm": arc})
    return out

def verify_has_pdf(archivo_norm: str, period_label: str) -> bool:
    """
    POST {base}/admin/has_pdf  body: archivo_norm, period  -> { ok: true, has_pdf: true/false }
    """
    url = _main_base() + "/admin/has_pdf"
    r = requests.post(url, data={"archivo_norm": archivo_norm, "period": period_label}, timeout=15)
    try:
        data = r.json() if r.ok else {}
    except Exception:
        data = {}
    print("DEBUG verify_has_pdf:", r.status_code, data)
    return bool(data.get("ok") and data.get("has_pdf"))

def trigger_send_pdf(to_phone: str, archivo_norm: str, period_label: str) -> bool:
    """
    POST {base}/admin/send_pdf  body: to, archivo_norm, period  -> { ok: true }
    (tu app principal envía el PDF real al usuario)
    """
    url = _main_base() + "/admin/send_pdf"
    payload = {"to": to_phone, "archivo_norm": archivo_norm, "period": period_label}
    try:
        r = requests.post(url, data=payload, timeout=20)
        print("DEBUG trigger_send_pdf:", r.status_code, r.text[:200])
        if r.ok:
            data = r.json()
            return bool(data.get("ok"))
        return False
    except Exception as e:
        print("ERROR trigger_send_pdf:", e)
        return False

def forward_to_main_app_async(form_data: dict):
    """Reenvía en background el inbound a tu webhook principal para que continúe el flujo."""
    def _run():
        try:
            r = requests.post(MAIN_APP_WEBHOOK_URL, data=form_data, timeout=15)
            print("DEBUG forward_to_main:", r.status_code)
        except Exception as e:
            print("ERROR forward_to_main:", e)
    threading.Thread(target=_run, daemon=True).start()

# ---- Envíos WhatsApp (Twilio)
def send_text(to: str, body: str) -> str | None:
    to_fixed = fix_to_whatsapp(to)
    try:
        msg = client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to_fixed,
            body=body,
            status_callback=STATUS_CALLBACK_URL,
        )
        print("DEBUG send_text OK:", msg.sid)
        return msg.sid
    except TwilioRestException as e:
        print("ERROR send_text Twilio:", e.code, e.msg, getattr(e,"details",None))
        return None
    except Exception as e:
        print("ERROR send_text general:", repr(e))
        return None

def send_template(to: str, period_label: str, cuil: str | None = None) -> str | None:
    if not TWILIO_TEMPLATE_SID:
        raise RuntimeError("Falta TWILIO_TEMPLATE_SID")
    to_fixed = fix_to_whatsapp(to)
    vars_dict = {"1": period_label}
    if cuil: vars_dict["2"] = cuil

    print("DEBUG send_template:",
          "to=", to_fixed,
          "period=", period_label,
          "template_sid=", TWILIO_TEMPLATE_SID,
          "vars=", json.dumps(vars_dict),
          "status_cb=", STATUS_CALLBACK_URL)
    try:
        msg = client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to_fixed,
            content_sid=TWILIO_TEMPLATE_SID,          # plantilla (Content API)
            content_variables=json.dumps(vars_dict),  # variables {{1}}, {{2}}
            status_callback=STATUS_CALLBACK_URL,
        )
        print("DEBUG send_template OK: MessageSid=", msg.sid)
        return msg.sid
    except TwilioRestException as e:
        print("ERROR send_template Twilio:", e.code, e.msg, getattr(e,"details",None))
        return None
    except Exception as e:
        print("ERROR send_template general:", repr(e))
        return None

# ==========
# RUTAS ADMIN
# ==========
@app.route("/admin/ping", methods=["GET"])
def admin_ping():
    return "pong", 200

@app.route("/admin/env", methods=["GET"])
def admin_env():
    return {
        "TWILIO_ACCOUNT_SID": _mask(TWILIO_ACCOUNT_SID, 6),
        "TWILIO_AUTH_TOKEN": _mask(TWILIO_AUTH_TOKEN, 6),
        "TWILIO_WHATSAPP_FROM": TWILIO_WHATSAPP_FROM or "None",
        "TWILIO_TEMPLATE_SID": TWILIO_TEMPLATE_SID or "None",
        "STATUS_CALLBACK_URL": STATUS_CALLBACK_URL or "None",
        "MAIN_APP_WEBHOOK_URL": MAIN_APP_WEBHOOK_URL or "None",
        "PERIODO_ACTUAL": PERIODO_ACTUAL or "None",
    }, 200

@app.route("/admin/send_text_one", methods=["POST"])
def admin_send_text_one():
    to = request.form.get("to")
    body = request.form.get("body", "Prueba texto")
    if not to:
        return {"ok": False, "error": "Falta 'to'"}, 400
    sid = send_text(to, body)
    return ({"ok": True, "sid": sid}, 200) if sid else ({"ok": False}, 500)

@app.route("/admin/send_template_one", methods=["POST"])
def admin_send_template_one():
    """
    Envía UNA plantilla y, si hay PDF para ese período, lo dispara luego.
    Body: to=whatsapp:+..., period=mm/aaaa  (opcional cuil=20-...-X)
    """
    to = request.form.get("to")
    period = request.form.get("period") or PERIODO_ACTUAL
    cuil = request.form.get("cuil")
    if not to or not period:
        return {"ok": False, "error": "Faltan 'to' o 'period'"}, 400

    sid = send_template(to, period, cuil)
    if not sid:
        return {"ok": False, "error": "No se pudo enviar plantilla"}, 500

    # Si tenemos CUIL, intentamos mandar el PDF automáticamente
    if cuil:
        try:
            if verify_has_pdf(cuil, period):
                ok = trigger_send_pdf(to, cuil, period)
                print("DEBUG auto-send PDF:", ok)
        except Exception as e:
            print("WARN auto-send PDF:", e)

    return {"ok": True, "sid": sid}, 200

@app.route("/admin/send_template_all_current", methods=["POST"])
def admin_send_template_all_current():
    """
    Lee el listado desde tu app principal, filtra por quienes tienen PDF del período,
    envía la plantilla y LUEGO dispara que se envíe el PDF.
    Body opcional: period=mm/aaaa (usa PERIODO_ACTUAL si no viene)
    """
    period = request.form.get("period") or PERIODO_ACTUAL
    if not period:
        return {"ok": False, "error": "No hay período (period o PERIODO_ACTUAL)"}, 400

    try:
        rows = load_envios_rows()
    except Exception as e:
        return {"ok": False, "error": f"envios_list: {e}"}, 400

    revisados = 0
    plantillas = 0
    pdf_enviados = 0

    for row in rows:
        to = row["telefono"]
        cuil = row["archivo_norm"]
        revisados += 1

        if not verify_has_pdf(cuil, period):
            continue

        sid = send_template(to, period, cuil)
        if sid:
            plantillas += 1
            if trigger_send_pdf(to, cuil, period):
                pdf_enviados += 1

    return {
        "ok": True,
        "period": period,
        "revisados": revisados,
        "plantillas": plantillas,
        "pdf_enviados": pdf_enviados
    }, 200

# ==========
# WEBHOOKS TWILIO
# ==========
@app.route("/twilio/status", methods=["POST"])
def twilio_status():
    data = request.form.to_dict()
    print("STATUS CALLBACK:", data)  # sent / delivered / read / failed
    return ("", 204)

@app.route("/twilio/webhook", methods=["POST"])
def twilio_webhook():
    """
    Entrantes: responde con aviso RRHH y reenvía a tu webhook principal
    para que continúe el flujo (envíe PDF, menú, etc.)
    """
    form = request.form.to_dict()
    from_num = form.get("From","")
    body = form.get("Body","")
    print("INBOUND:", {"From": from_num, "Body": body})

    info = "🤖 Soy una AI diseñada para enviar los recibos. Si necesitás algo más, comunicate con RRHH."
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Message>{info}</Message>
</Response>"""
    resp = Response(twiml, mimetype="text/xml")

    try:
        forward_to_main_app_async(form)
    except Exception as e:
        print("WARN forward async:", e)

    return resp

# imprimir rutas al iniciar
_print_routes_once()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=True)
