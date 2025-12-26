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

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from flask import send_file
from collections import defaultdict
from datetime import datetime
import os
import sqlite3
import time

from flask import Flask, request, Response, abort, jsonify, send_file, make_response, redirect



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
DATA_DIR = "/data"
DEFAULT_DB = "/data/app.db" if os.path.isdir(DATA_DIR) else "/tmp/app.db"
DB_PATH = os.getenv("DB_PATH", DEFAULT_DB)


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def is_dni_verified(archivo_norm: str) -> bool:
    """
    Compatibilidad hacia atrás.
    Devuelve True si la identidad (CUIL) ya está verificada
    usando identity_verification.
    """
    return is_identity_verified(archivo_norm)


def set_dni_verified(archivo_norm: str, dni: str) -> None:
    """
    Compatibilidad hacia atrás.
    Marca la identidad como verificada usando el sistema nuevo.
    El WhatsApp se resuelve automáticamente desde el Excel.
    """
    if not archivo_norm or not dni:
        return

    to_whatsapp = find_whatsapp_by_cuil_from_envios(archivo_norm)
    if not to_whatsapp:
        # No se puede verificar sin teléfono asociado
        return

    set_identity_verified(
        archivo_norm=archivo_norm,
        dni=dni,
        to_whatsapp=to_whatsapp,
        source="legacy"
    )

import re


def normalize_dni(dni: str) -> str:
    return re.sub(r"\D+", "", dni or "")

def is_identity_verified(archivo_norm: str) -> bool:
    if not archivo_norm:
        return False
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM identity_verification WHERE archivo_norm = ? LIMIT 1;", (archivo_norm,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def get_identity_verification(archivo_norm: str):
    if not archivo_norm:
        return None
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT archivo_norm, dni, to_whatsapp, verified_at, source
        FROM identity_verification
        WHERE archivo_norm = ?
        LIMIT 1;
    """, (archivo_norm,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def set_identity_verified(archivo_norm: str, dni: str, to_whatsapp: str, source: str = "manual") -> None:
    if not archivo_norm:
        return
    dni_norm = normalize_dni(dni)
    if len(dni_norm) not in (7, 8):
        return

    if not to_whatsapp:
        return
    if not to_whatsapp.startswith("whatsapp:"):
        to_whatsapp = normalize_to_whatsapp_e164(to_whatsapp)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO identity_verification (archivo_norm, dni, to_whatsapp, verified_at, source)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(archivo_norm) DO UPDATE SET
            dni=excluded.dni,
            to_whatsapp=excluded.to_whatsapp,
            verified_at=excluded.verified_at,
            source=excluded.source;
        """,
        (archivo_norm, dni_norm, to_whatsapp, int(time.time()), source),
    )
    conn.commit()
    conn.close()

def find_phone_in_envios_excel_by_cuil(archivo_norm: str) -> str | None:
    """
    Busca el teléfono de esa persona (CUIL) en el Excel de envíos.
    Devuelve un whatsapp:+... listo para usar.
    """
    rows = read_envios_rows()  # ya la usás en envíos masivos/cola
    target = s(archivo_norm)

    for r in rows:
        cuil = s(r.get("Archivo_norm") or r.get("archivo_norm") or r.get("CUIL") or r.get("Cuil"))
        if cuil == target:
            tel = s(r.get("Telefono_norm") or r.get("Telefono") or r.get("Teléfono"))
            if not tel:
                return None
            return normalize_to_whatsapp_e164(tel)

    return None

def find_whatsapp_by_cuil_from_envios(archivo_norm: str) -> str | None:
    rows = read_envios_rows()
    target = s(archivo_norm)

    for r in rows:
        cuil = s(r.get("Archivo_norm") or r.get("archivo_norm") or r.get("CUIL") or r.get("Cuil"))
        if cuil == target:
            tel = s(r.get("Telefono_norm") or r.get("Telefono") or r.get("Teléfono"))
            if not tel:
                return None
            return normalize_to_whatsapp_e164(tel)

    return None


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")


    cur.execute(
    """
    CREATE TABLE IF NOT EXISTS pending_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        to_whatsapp TEXT NOT NULL,
        archivo_norm TEXT NOT NULL,
        period_label TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(to_whatsapp, archivo_norm, period_label)
    );
    """
)


    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS message_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_sid TEXT UNIQUE NOT NULL,
            to_whatsapp TEXT,
            archivo_norm TEXT,
            period_label TEXT,
            nombre TEXT,
            kind TEXT,
            created_at INTEGER,
            last_status TEXT,
            last_status_at INTEGER,
            read_at INTEGER,
            delivered_at INTEGER,
            failed_at INTEGER,
            error_code TEXT,
            error_message TEXT
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS view_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_whatsapp TEXT NOT NULL,
            archivo_norm TEXT,
            period_label TEXT,
            response TEXT,
            created_at INTEGER NOT NULL
        );
        """
    )

    # 👇👈 AÑADIR ESTO
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS recibo_estado (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            archivo_norm TEXT NOT NULL,
            period_label TEXT NOT NULL,
            estado TEXT NOT NULL,         -- 'DISPONIBLE', 'FIRMADO', 'OBSERVADO'
            UNIQUE(archivo_norm, period_label)
        );
        """
    )

    # 👇👈 AÑADIR ESTO (contador de vistas extra)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS recibo_vistas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            archivo_norm TEXT NOT NULL,
            period_label TEXT NOT NULL,
            vistas INTEGER NOT NULL DEFAULT 0,
            UNIQUE(archivo_norm, period_label)
        );
        """
    )
    cur.execute("""
        CREATE TABLE IF NOT EXISTS identity_verification (
            archivo_norm TEXT PRIMARY KEY,   -- CUIL
            dni TEXT NOT NULL,
            to_whatsapp TEXT NOT NULL,       -- whatsapp:+54...
            verified_at INTEGER NOT NULL,
            source TEXT NOT NULL             -- 'manual' o 'chat' (o 'legacy')
        );
    """)





    # ==========================
    # Cola de envíos (batch/queue)
    # ==========================
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS send_jobs (
            job_id TEXT PRIMARY KEY,
            period_label TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            finished_at INTEGER,
            status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING/RUNNING/DONE/STOPPED
            total_enqueued INTEGER NOT NULL DEFAULT 0,
            total_sent INTEGER NOT NULL DEFAULT 0,
            total_failed INTEGER NOT NULL DEFAULT 0
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS send_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            to_whatsapp TEXT NOT NULL,
            archivo_norm TEXT NOT NULL,
            nombre TEXT,
            period_label TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING/SENT/FAILED
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at INTEGER NOT NULL,
            sent_at INTEGER,
            UNIQUE(job_id, to_whatsapp, archivo_norm, period_label)
        );
        """
    )

    cur.execute("""
        CREATE TABLE IF NOT EXISTS identity_verification (
            archivo_norm TEXT PRIMARY KEY,   -- CUIL
            dni TEXT NOT NULL,
            to_whatsapp TEXT NOT NULL,       -- whatsapp:+54...
            verified_at INTEGER NOT NULL,
            source TEXT NOT NULL,            -- 'manual' o 'chat' (o 'legacy')
            nombre TEXT                      -- nombre tomado del Excel de envíos
        );
    """)

    # Por si la tabla ya existía sin la columna 'nombre', la agregamos con ALTER TABLE
    try:
        cur.execute("ALTER TABLE identity_verification ADD COLUMN nombre TEXT;")
    except Exception:
        # Si ya existe, va a tirar error y lo ignoramos
        pass


    conn.commit()
    conn.close()

def get_recibo_vistas(archivo_norm: str, period_label: str) -> int:
    """
    Devuelve cuántas visualizaciones adicionales (post-firma) tiene registradas
    ese recibo. Si no existe registro, devuelve 0.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT vistas
        FROM recibo_vistas
        WHERE archivo_norm = ? AND period_label = ?;
        """,
        (archivo_norm, period_label),
    )
    row = cur.fetchone()
    conn.close()
    return int(row[0]) if row else 0


def inc_recibo_vistas(archivo_norm: str, period_label: str) -> int:
    """
    Incrementa en 1 el contador de visualizaciones para ese recibo y
    devuelve el valor nuevo.
    """
    conn = get_db_connection()
    cur = conn.cursor()

    # Si no existe, lo creamos con 0
    cur.execute(
        """
        INSERT OR IGNORE INTO recibo_vistas (archivo_norm, period_label, vistas)
        VALUES (?, ?, 0);
        """,
        (archivo_norm, period_label),
    )

    # Sumamos 1
    cur.execute(
        """
        UPDATE recibo_vistas
        SET vistas = vistas + 1
        WHERE archivo_norm = ? AND period_label = ?;
        """,
        (archivo_norm, period_label),
    )

    # Leemos el valor nuevo
    cur.execute(
        """
        SELECT vistas
        FROM recibo_vistas
        WHERE archivo_norm = ? AND period_label = ?;
        """,
        (archivo_norm, period_label),
    )
    row = cur.fetchone()
    conn.commit()
    conn.close()

    return int(row[0]) if row else 0


def get_recibo_estado(archivo_norm: str, period_label: str) -> str:
    """
    Devuelve el estado actual del recibo:
    'DISPONIBLE' (default), 'FIRMADO' o 'OBSERVADO'.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT estado FROM recibo_estado WHERE archivo_norm = ? AND period_label = ?;",
        (archivo_norm, period_label),
    )
    row = cur.fetchone()
    conn.close()
    if row:
        return row[0] or "DISPONIBLE"
    return "DISPONIBLE"


def set_recibo_estado(archivo_norm: str, period_label: str, estado: str) -> None:
    """
    Setea el estado del recibo en la tabla 'recibo_estado'.
    estado ∈ {'DISPONIBLE', 'FIRMADO', 'OBSERVADO'}.
    """
    estado = estado.upper()
    if estado not in ("DISPONIBLE", "FIRMADO", "OBSERVADO"):
        estado = "DISPONIBLE"

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO recibo_estado (archivo_norm, period_label, estado)
        VALUES (?, ?, ?)
        ON CONFLICT(archivo_norm, period_label)
        DO UPDATE SET estado = excluded.estado;
        """,
        (archivo_norm, period_label, estado),
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
        INSERT OR IGNORE INTO pending_views (to_whatsapp, archivo_norm, period_label, created_at)
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

#============================================
import time
from typing import Optional, Tuple

def save_message_sent(
    message_sid: str,
    to_whatsapp: str,
    archivo_norm: Optional[str],
    period_label: Optional[str],
    kind: str,
    nombre: Optional[str] = None,
):
    """
    Registra que enviamos un mensaje (plantilla o media).
    kind: 'template' o 'media'
    """
    conn = get_db_connection()
    cur = conn.cursor()
    now_ts = int(time.time())
    cur.execute(
        """
        INSERT OR IGNORE INTO message_status (
            message_sid, to_whatsapp, archivo_norm, period_label,
            nombre, kind, created_at, last_status, last_status_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            message_sid,
            to_whatsapp,
            archivo_norm,
            period_label,
            nombre,
            kind,
            now_ts,      # created_at = momento de envío
            "sent",      # estado inicial
            now_ts,
        ),
    )
    conn.commit()
    conn.close()


def update_message_status_and_get(
    message_sid: str,
    status: str,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Actualiza el estado de un mensaje por SID y devuelve (kind, to_whatsapp).
    """
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        "SELECT kind, to_whatsapp FROM message_status WHERE message_sid = ?;",
        (message_sid,),
    )
    row = cur.fetchone()
    now_ts = int(time.time())

    if row:
        kind = row["kind"]
        to_whatsapp = row["to_whatsapp"]
        cur.execute(
            """
            UPDATE message_status
            SET last_status = ?, last_status_at = ?,
                read_at = CASE WHEN ? = 'read' THEN COALESCE(read_at, ?) ELSE read_at END,
                delivered_at = CASE WHEN ? = 'delivered' THEN COALESCE(delivered_at, ?) ELSE delivered_at END,
                failed_at = CASE WHEN ? IN ('failed','undelivered') THEN COALESCE(failed_at, ?) ELSE failed_at END,
                error_code = COALESCE(?, error_code),
                error_message = COALESCE(?, error_message)
            WHERE message_sid = ?;
            """,
            (
                status,
                now_ts,
                status,
                now_ts,
                status,
                now_ts,
                status,
                now_ts,
                error_code,
                error_message,
                message_sid,
            ),
        )
        conn.commit()
        conn.close()
        return kind, to_whatsapp

    # Si no existía, lo registramos mínimo
    cur.execute(
        """
        INSERT OR IGNORE INTO message_status (
            message_sid, last_status, last_status_at, error_code, error_message
        ) VALUES (?, ?, ?, ?, ?);
        """,
        (message_sid, status, now_ts, error_code, error_message),
    )
    conn.commit()
    conn.close()
    return None, None


def save_user_confirmation(from_whatsapp: str, response: str):
    """
    Guarda que el usuario respondió 'ok' o 'problema' sobre el último recibo pendiente.
    """
    archivo_norm = None
    period_label = None
    pending = get_last_pending_view(from_whatsapp)
    if pending:
        archivo_norm, period_label = pending

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO view_confirmations (
            from_whatsapp, archivo_norm, period_label, response, created_at
        ) VALUES (?, ?, ?, ?, ?);
        """,
        (
            from_whatsapp,
            archivo_norm,
            period_label,
            response,
            int(time.time()),
        ),
    )
    conn.commit()
    conn.close()
#=============================

import threading
import uuid

QUEUE_RATE_PER_MIN = int(os.getenv("QUEUE_RATE_PER_MIN", "10"))  # 10/min por defecto
QUEUE_SLEEP_SEC = 60.0 / max(1, QUEUE_RATE_PER_MIN)             # 6s por mensaje
QUEUE_MAX_ATTEMPTS = int(os.getenv("QUEUE_MAX_ATTEMPTS", "3"))

_worker_thread = None
_worker_stop_flag = False

def enqueue_job(period_label: str, rows: list, require_pdf: bool = True) -> dict:
    """
    Crea un job y encola destinatarios para enviar la plantilla en tandas.
    Devuelve dict con job_id + contadores para debug.
    """
    job_id = str(uuid.uuid4())
    now = int(time.time())

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        "INSERT INTO send_jobs (job_id, period_label, created_at, status) VALUES (?, ?, ?, 'PENDING');",
        (job_id, period_label, now),
    )

    total_inserted = 0
    skipped = 0

    for r in rows:
        telefono = s(r.get("Telefono_norm") or r.get("Telefono") or r.get("Teléfono"))

        archivo_norm = s(
            r.get("Archivo_norm")
            or r.get("archivo_norm")
            or r.get("Archivo")
            or r.get("archivo")
            or r.get("CUIL")
            or r.get("Cuil")
        )

        # Si vino con ".pdf", lo limpiamos
        if archivo_norm.lower().endswith(".pdf"):
            archivo_norm = archivo_norm[:-4]

        nombre = s(
            r.get("Nombre")
            or r.get("Nombre y apellido")
            or r.get("Apellido y nombre")
            or r.get("Empleado")
            or r.get("Persona")
            or r.get("nombre")
        )

        if not telefono or not archivo_norm:
            skipped += 1
            continue

        try:
            to_whatsapp = normalize_to_whatsapp_e164(telefono)
        except Exception:
            skipped += 1
            continue

        if require_pdf:
            try:
                pdf_id = find_pdf_for_archivo_and_period(archivo_norm, period_label)
            except Exception:
                pdf_id = None

            if not pdf_id:
                skipped += 1
                continue

        cur.execute(
            """
            INSERT OR IGNORE INTO send_queue
            (job_id, to_whatsapp, archivo_norm, nombre, period_label, created_at)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (job_id, to_whatsapp, archivo_norm, nombre, period_label, now),
        )

        if cur.rowcount == 1:
            total_inserted += 1

    cur.execute(
        "UPDATE send_jobs SET total_enqueued = ? WHERE job_id = ?;",
        (total_inserted, job_id),
    )

    conn.commit()
    conn.close()

    return {"job_id": job_id, "enqueued": total_inserted, "skipped": skipped}


def _mark_job_running(job_id: str):
    conn = get_db_connection()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        "UPDATE send_jobs SET status='RUNNING', started_at=COALESCE(started_at, ?) WHERE job_id=?;",
        (now, job_id),
    )
    conn.commit()
    conn.close()


def _mark_job_finished(job_id: str, status: str):
    conn = get_db_connection()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        "UPDATE send_jobs SET status=?, finished_at=? WHERE job_id=?;",
        (status, now, job_id),
    )
    conn.commit()
    conn.close()


def _update_job_counters(job_id: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM send_queue WHERE job_id=? AND status='SENT';", (job_id,))
    sent = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM send_queue WHERE job_id=? AND status='FAILED';", (job_id,))
    failed = int(cur.fetchone()[0])
    cur.execute(
        "UPDATE send_jobs SET total_sent=?, total_failed=? WHERE job_id=?;",
        (sent, failed, job_id),
    )
    conn.commit()
    conn.close()


def _pick_next_pending(job_id: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, to_whatsapp, archivo_norm, nombre, period_label, attempts
        FROM send_queue
        WHERE job_id=? AND status='PENDING'
        ORDER BY id ASC
        LIMIT 1;
        """,
        (job_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def _set_queue_row_status(row_id: int, status: str, err: str = None):
    conn = get_db_connection()
    cur = conn.cursor()
    now = int(time.time())
    if status == "SENT":
        cur.execute(
            """
            UPDATE send_queue
            SET status='SENT', sent_at=?, last_error=NULL
            WHERE id=?;
            """,
            (now, row_id),
        )
    elif status == "FAILED":
        cur.execute(
            """
            UPDATE send_queue
            SET status='FAILED', last_error=?
            WHERE id=?;
            """,
            (err or "unknown", row_id),
        )
    conn.commit()
    conn.close()


def _inc_attempt(row_id: int, err: str = None):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE send_queue
        SET attempts = attempts + 1, last_error=?
        WHERE id=?;
        """,
        (err or "unknown", row_id),
    )
    conn.commit()
    conn.close()


def _should_fail(row_attempts: int) -> bool:
    return row_attempts + 1 >= QUEUE_MAX_ATTEMPTS


def _send_template_for_row(to_whatsapp: str, archivo_norm: str, period_label: str, nombre: str):
    # Reusar el MISMO envío del masivo, así siempre manda {{1}} = nombre
    sid = send_template_whatsapp_norm(to_whatsapp, nombre)

    if not sid:
        raise Exception("twilio_error_envio_plantilla")

    # Guardar tracking igual que antes
    try:
        save_message_sent(
            message_sid=sid,
            to_whatsapp=to_whatsapp,
            archivo_norm=archivo_norm,
            period_label=period_label,
            kind="template",
            nombre=nombre,
        )
    except Exception:
        pass

    save_pending_view(to_whatsapp, archivo_norm, period_label)



def _queue_worker_loop():
    global _worker_stop_flag
    while not _worker_stop_flag:
        # tomar el primer job pendiente o corriendo
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT job_id, status
            FROM send_jobs
            WHERE status IN ('PENDING','RUNNING')
            ORDER BY created_at ASC
            LIMIT 1;
            """
        )
        job = cur.fetchone()
        conn.close()

        if not job:
            time.sleep(2.0)
            continue

        job_id, job_status = job[0], job[1]
        if job_status == "PENDING":
            _mark_job_running(job_id)

        # procesar un item
        row = _pick_next_pending(job_id)
        if not row:
            _update_job_counters(job_id)
            _mark_job_finished(job_id, "DONE")
            continue

        row_id, to_whatsapp, archivo_norm, nombre, period_label, attempts = row
        try:
            _send_template_for_row(to_whatsapp, archivo_norm, period_label, nombre)
            _set_queue_row_status(row_id, "SENT")
        except Exception as e:
            err = str(e)
            _inc_attempt(row_id, err)
            if _should_fail(attempts):
                _set_queue_row_status(row_id, "FAILED", err)

        _update_job_counters(job_id)
        time.sleep(QUEUE_SLEEP_SEC)


def start_queue_worker_once():
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_thread = threading.Thread(target=_queue_worker_loop, daemon=True)
    _worker_thread.start()



# ⚠️ MUY IMPORTANTE:
# Llamamos a init_db() al importar el módulo
# (para que gunicorn lo ejecute siempre)
init_db()
start_queue_worker_once()

# ==========================

def ts_to_str(ts: Optional[int]) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def generate_excel_report() -> str:
    """
    Genera un Excel en /tmp/reporte_recibos.xlsx con UNA fila por (WhatsApp, Período).

    Fixes incluidos:
    - Agrupa por (whatsapp, periodo_normalizado) para evitar duplicados.
    - Normaliza periodos a 'mm/aaaa' (incluye 'aaaa-mm').
    - Para confirmaciones (view_confirmations), si el periodo viene vacío o distinto,
      lo resuelve desde pending_views usando (whatsapp, archivo_norm) (último envío).
    - Agrega columna "Periodo" y "CUIL".
    """
    # 1) Cargamos message_status y view_confirmations
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            message_sid, to_whatsapp, archivo_norm, period_label,
            nombre, kind, created_at, last_status,
            last_status_at, read_at, delivered_at, failed_at
        FROM message_status;
        """
    )
    msg_rows = cur.fetchall()

    cur.execute(
        """
        SELECT from_whatsapp, archivo_norm, period_label, response, created_at
        FROM view_confirmations;
        """
    )
    conf_rows = cur.fetchall()

    # 2) Lookup: último period_label por (to_whatsapp, archivo_norm) desde pending_views
    cur.execute(
        """
        SELECT to_whatsapp, archivo_norm, period_label, MAX(created_at) as last_ts
        FROM pending_views
        GROUP BY to_whatsapp, archivo_norm;
        """
    )
    pv_rows = cur.fetchall()
    conn.close()

    last_period_by_user = {}
    for r in pv_rows:
        w = (r["to_whatsapp"] or "").strip()
        a = (r["archivo_norm"] or "").strip()
        p = (r["period_label"] or "").strip()
        if w and a and p:
            last_period_by_user[(w, a)] = p

    # 3) Helpers
    def _norm_period(p: str) -> str:
        # Usa tu norm_period_label si ya la tenés definida (mejor),
        # pero asegurate que soporte también 'aaaa-mm'.
        return norm_period_label(p)

    def _key(whatsapp: str, period_norm: str) -> tuple:
        return ((whatsapp or "").strip(), (period_norm or "").strip())

    # 4) Agregamos datos desde message_status
    agg = {}

    for row in msg_rows:
        whatsapp = (row["to_whatsapp"] or "").strip()
        if not whatsapp:
            continue

        period_raw = (row["period_label"] or "").strip()
        period_norm = _norm_period(period_raw)

        key = _key(whatsapp, period_norm)
        rec = agg.get(key)
        if not rec:
            rec = {
                "periodo": period_norm,
                "nombre": (row["nombre"] or "").strip(),
                "archivo_norm": (row["archivo_norm"] or "").strip(),
                "whatsapp": whatsapp,
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
            }
            agg[key] = rec

        # Completar datos si llegan después
        if row["nombre"] and not rec["nombre"]:
            rec["nombre"] = str(row["nombre"]).strip()
        if row["archivo_norm"] and not rec["archivo_norm"]:
            rec["archivo_norm"] = str(row["archivo_norm"]).strip()
        if period_norm and not rec["periodo"]:
            rec["periodo"] = period_norm

        kind = (row["kind"] or "").strip()
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

        elif kind == "media":
            if created_at and (not rec["pdf_sent_at"] or created_at < rec["pdf_sent_at"]):
                rec["pdf_sent_at"] = created_at
            if delivered_at:
                rec["pdf_delivered_at"] = delivered_at
            if read_at:
                rec["pdf_read_at"] = read_at
            if failed_at:
                rec["pdf_failed_at"] = failed_at

    # 5) Mezclamos confirmaciones: resolvemos período con pending_views si hace falta
    for row in conf_rows:
        whatsapp = (row["from_whatsapp"] or "").strip()
        if not whatsapp:
            continue

        archivo = (row["archivo_norm"] or "").strip()
        period_raw = (row["period_label"] or "").strip()

        # Si no vino período, lo resolvemos del último envío a ese whatsapp+archivo
        if (not period_raw) and whatsapp and archivo:
            period_raw = (last_period_by_user.get((whatsapp, archivo), "") or "").strip()

        period_norm = _norm_period(period_raw)

        key = _key(whatsapp, period_norm)

        if key not in agg:
            agg[key] = {
                "periodo": period_norm,
                "nombre": "",
                "archivo_norm": archivo,
                "whatsapp": whatsapp,
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
            }

        rec = agg[key]

        # Completar CUIL si aparece
        if archivo and not rec["archivo_norm"]:
            rec["archivo_norm"] = archivo

        ts = row["created_at"]
        if not rec["respuesta_timestamp"] or (ts and ts > rec["respuesta_timestamp"]):
            rec["respuesta_usuario"] = (row["response"] or "").strip()
            rec["respuesta_timestamp"] = ts

    # 6) Creamos el Excel
    wb = Workbook()
    ws = wb.active
    ws.title = "Recibos"

    headers = [
        "Periodo",
        "Nombre",
        "CUIL",
        "WhatsApp",
        "Plantilla_enviada",
        "Plantilla_entregada",
        "Plantilla_leida",
        "Plantilla_fallida",
        "PDF_enviado",
        "PDF_entregado",
        "PDF_leido",
        "PDF_fallido",
        "Respuesta_usuario",
        "Respuesta_timestamp",
    ]
    ws.append(headers)

    # Orden prolijo: periodo, nombre, whatsapp
    items = list(agg.values())
    items.sort(key=lambda r: (r.get("periodo") or "", r.get("nombre") or "", r.get("whatsapp") or ""))

    for rec in items:
        ws.append(
            [
                rec.get("periodo", ""),
                rec.get("nombre", ""),
                rec.get("archivo_norm", ""),
                rec.get("whatsapp", ""),
                ts_to_str(rec.get("plantilla_sent_at")),
                ts_to_str(rec.get("plantilla_delivered_at")),
                ts_to_str(rec.get("plantilla_read_at")),
                ts_to_str(rec.get("plantilla_failed_at")),
                ts_to_str(rec.get("pdf_sent_at")),
                ts_to_str(rec.get("pdf_delivered_at")),
                ts_to_str(rec.get("pdf_read_at")),
                ts_to_str(rec.get("pdf_failed_at")),
                rec.get("respuesta_usuario", ""),
                ts_to_str(rec.get("respuesta_timestamp")),
            ]
        )

    # Auto ancho de columnas
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                max_len = max(max_len, len(str(cell.value or "")))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = max(10, max_len + 2)

    path = "/tmp/reporte_recibos.xlsx"
    wb.save(path)
    return path

# =======================
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

# Cache global para el cliente de Drive
_DRIVE_SERVICE = None

def get_drive_service():
    global _DRIVE_SERVICE
    if _DRIVE_SERVICE is None:
        _DRIVE_SERVICE = build_drive_service()
    return _DRIVE_SERVICE


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




def period_sort_key(period_label: str):
    """
    Convierte 'mm/aaaa' en tupla (aaaa, mm) para poder ordenar.
    """
    m = re.match(r"^(\d{2})/(\d{4})$", period_label)
    if not m:
        return (0, 0)
    mm, yyyy = m.groups()
    return (int(yyyy), int(mm))

import re

def normalize_period_label(p: str) -> str:
    """
    Devuelve 'YYYY-MM' a partir de:
    - '2025-12'
    - '12/2025'
    - '12-2025'
    - '2025/12'
    """
    if not p:
        return ""
    p = p.strip()

    # YYYY-MM o YYYY/MM
    m = re.match(r"^(\d{4})[-/](\d{1,2})$", p)
    if m:
        y = int(m.group(1))
        mm = int(m.group(2))
        if 1 <= mm <= 12:
            return f"{y:04d}-{mm:02d}"

    # MM/YYYY o MM-YYYY
    m = re.match(r"^(\d{1,2})[-/](\d{4})$", p)
    if m:
        mm = int(m.group(1))
        y = int(m.group(2))
        if 1 <= mm <= 12:
            return f"{y:04d}-{mm:02d}"

    # Si no matchea, devolvemos tal cual (para no romper),
    # pero idealmente deberías validar y rechazar.
    return p



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
    try:
        service = get_drive_service()
    except Exception as e:
        print("ERROR build/get_drive_service:", e)
        return None

    filename = f"{archivo_norm}.pdf"

    try:
        results = service.files().list(
            q=f"name = '{filename}' and mimeType = 'application/pdf' and trashed = false",
            fields="files(id, name, parents)",
            pageSize=1000,
        ).execute()
    except Exception as e:
        print("ERROR list files in find_pdf_for_archivo_and_period:", e)
        return None
    files = results.get("files", [])

    print("DEBUG find_pdf_for_archivo_and_period")
    print("  archivo_norm:", archivo_norm)
    print("  period_label buscado:", period_label)
    print("  cantidad de archivos encontrados:", len(files))

    # Normalizamos el período que nos llega (10/2025 o 10-2025 -> 10-2025)
    normalized_period = normalize_period_for_folder(period_label)
    print("  normalized_period buscado:", normalized_period)
    for f in files:
        parents = f.get("parents", [])
        if not parents:
            continue

        parent_id = parents[0]
        try:
            folder = service.files().get(
                fileId=parent_id,
                fields="id, name, parents",
            ).execute()
            folder_name = folder.get("name", "")
        except Exception as e:
            print("ERROR get parent folder:", e)
            folder_name = ""
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
    Normaliza un período a 'mm/aaaa'.
    Acepta:
      - 'mm/aaaa', 'mm-aaaa', 'm/aaaa', 'm-aaaa'
      - 'mmaaaa' o 'mmyyyy'
      - 'aaaa-mm' o 'aaaa/mm'  ✅ (nuevo)
    """
    if not s:
        return ""
    s = str(s).strip()

    # 1) formatos mm/aaaa o mm-aaaa
    m = re.match(r"^(\d{1,2})[/-](\d{4})$", s)
    if m:
        mm, yyyy = m.groups()
        return f"{int(mm):02d}/{yyyy}"

    # 2) formatos aaaa-mm o aaaa/mm  ✅ nuevo
    m = re.match(r"^(\d{4})[/-](\d{1,2})$", s)
    if m:
        yyyy, mm = m.groups()
        return f"{int(mm):02d}/{yyyy}"

    # 3) formatos pegados tipo mmyyyy
    m = re.match(r"^(\d{1,2})(\d{4})$", s)
    if m:
        mm, yyyy = m.groups()
        return f"{int(mm):02d}/{yyyy}"

    # 4) si ya viene mm/aaaa correcto
    if re.match(r"^\d{2}/\d{4}$", s):
        return s

    return s

def normalize_period_for_folder(period_label: str) -> str:
    """
    Normaliza el período para comparar contra carpetas 'MM-AAAA'.
    Acepta:
      - 'MM/AAAA' o 'MM-AAAA'  -> 'MM-AAAA'
      - 'AAAA-MM'             -> 'MM-AAAA'
    """
    if not period_label:
        return ""
    s = str(period_label).strip()

    # MM/AAAA o MM-AAAA
    m = re.match(r"^(\d{1,2})[/-](\d{4})$", s)
    if m:
        mm, yyyy = m.groups()
        return f"{int(mm):02d}-{yyyy}"

    # AAAA-MM o AAAA/MM
    m = re.match(r"^(\d{4})[/-](\d{1,2})$", s)
    if m:
        yyyy, mm = m.groups()
        return f"{int(mm):02d}-{yyyy}"

    # fallback: solo cambia / por -
    return s.replace("/", "-")


def period_label_to_folder(period_mm_aaaa: str) -> str:
    """
    Convierte 'MM/AAAA' -> 'MM-AAAA'
    """
    if not period_mm_aaaa:
        return ""
    return period_mm_aaaa.replace("/", "-")


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
            "flow_state": "IDLE",     # 'IDLE', 'ASK_VISUALIZAR', 'ASK_FIRMAR_OBS', 'ASK_DESHACER_OBS', 'ASK_FIRMADO_VISTA', 'ASK_DNI'
            "archivo_norm": None,
            "period_label": None,
            "pdf_id": None,
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
    rows = read_envios_rows()
    # número que viene de Twilio -> solo dígitos
    want = re.sub(r"\D", "", to_whatsapp or "")
    for r in rows:
        # usar Telefono_norm si existe
        tel = (
            r.get("Telefono_norm")
            or r.get("Telefono")
            or r.get("Teléfono")
            or r.get("telefono")
            or ""
        )

        arc = (
            r.get("Archivo_norm")
            or r.get("archivo_norm")
            or r.get("Archivo")
            or r.get("archivo")
            or ""
        )
        if not tel or not arc:
            continue

        # normalizar también el tel de la fila
        tclean = re.sub(r"\D", "", str(tel))

        if tclean.endswith(want) or want.endswith(tclean):
            return str(arc).strip()

    return None


def get_archivo_from_incoming(from_whatsapp: str) -> Optional[str]:
    """
    Helper para el webhook: dado el From de Twilio (whatsapp:+54...),
    devuelve el archivo_norm (CUIL) si está autorizado (figura en el Excel).
    """
    return find_archivo_by_phone(from_whatsapp)

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

def get_dni_for_archivo(archivo_norm: str) -> str | None:
    """
    Devuelve el DNI (como string) para un archivo_norm dado,
    leyendo el Excel de envíos.
    """
    if not archivo_norm:
        return None

    rows = read_envios_rows()
    for r in rows:
        arc = (
            r.get("Archivo_norm")
            or r.get("archivo_norm")
            or r.get("Archivo")
            or r.get("archivo")
            or ""
        )
        if str(arc).strip() != str(archivo_norm).strip():
            continue

        dni_val = r.get("Dni") or r.get("DNI") or r.get("dni")
        if dni_val is None:
            return None

        # manejar floats tipo 44143190.0
        if isinstance(dni_val, float) and dni_val.is_integer():
            dni_val = int(dni_val)

        return re.sub(r"\D", "", str(dni_val))

    return None

##################################################################
# ADMIN / DEBUG / UTILITIES
from functools import wraps

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

def _get_admin_token_from_request():
    # 1) Header (recomendado)
    tok = request.headers.get("X-Admin-Token", "").strip()
    if tok:
        return tok
    # 2) Query param o form (por si llamás desde navegador)
    tok = (request.args.get("token") or request.form.get("token") or "").strip()
    return tok

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not ADMIN_TOKEN:
            return {"ok": False, "error": "ADMIN_TOKEN not configured"}, 500
        if _get_admin_token_from_request() != ADMIN_TOKEN:
            return {"ok": False, "error": "Unauthorized"}, 401
        return fn(*args, **kwargs)
    return wrapper

##################################################################



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

@app.route("/admin/debug_envios", methods=["GET"])
@admin_required
def admin_debug_envios():
    rows = read_envios_rows()
    out = []
    for r in rows:
        if "LEGUI" in str(r.get("Nombre", "")).upper():
            out.append(r)
    return {"rows": out}, 200

@app.route("/admin/identity/verify", methods=["POST"])
@admin_required
def admin_identity_verify():
    archivo_norm = (request.form.get("archivo_norm") or "").strip()  # CUIL
    dni = (request.form.get("dni") or "").strip()

    dni_norm = normalize_dni(dni)
    if not archivo_norm or len(dni_norm) not in (7, 8):
        return {"ok": False, "error": "archivo_norm (CUIL) y dni (7/8 dígitos) requeridos"}, 400

    to_whatsapp = find_whatsapp_by_cuil_from_envios(archivo_norm)
    if not to_whatsapp:
        return {"ok": False, "error": "No se encontró teléfono para ese CUIL en el Excel de envíos"}, 404

    set_identity_verified(archivo_norm, dni_norm, to_whatsapp, source="manual")

    return {"ok": True, "archivo_norm": archivo_norm, "dni": dni_norm, "to_whatsapp": to_whatsapp}, 200

@app.route("/admin/identity/get/<archivo_norm>", methods=["GET"])
@admin_required
def admin_identity_get(archivo_norm: str):
    rec = get_identity_verification(archivo_norm)
    if not rec:
        return {"ok": False, "error": "not found"}, 404
    return {"ok": True, "identity": rec}, 200


def empty_twiml():
    return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    mimetype="text/xml")

@app.route("/admin/verify_person", methods=["POST"])
@admin_required
def admin_verify_person():
    """
    Marca un CUIL + DNI como verificado de forma manual.
    - Busca el número de WhatsApp y el nombre en el Excel de envíos.
    - Inserta/actualiza en identity_verification (incluyendo 'nombre').
    """
    archivo_norm = (request.form.get("archivo_norm") or "").strip()
    dni = (request.form.get("dni") or "").strip()

    if not archivo_norm or not dni:
        return {"ok": False, "error": "archivo_norm y dni son requeridos"}, 400

    if not dni.isdigit():
        return {"ok": False, "error": "dni debe ser solo números"}, 400

    # Leemos Excel de envíos para sacar WhatsApp + nombre
    try:
        envios_rows = read_envios_rows()
    except Exception as e:
        print("ERROR read_envios_rows en verify_person:", e)
        envios_rows = []

    to_whatsapp = None
    nombre = ""

    for r in envios_rows:
        cuil_row = str(
            r.get("Archivo_norm")
            or r.get("archivo_norm")
            or r.get("Archivo")
            or r.get("archivo")
            or r.get("CUIL")
            or r.get("Cuil")
            or ""
        ).strip()

        if cuil_row != archivo_norm:
            continue

        telefono = str(
            r.get("Telefono_norm")
            or r.get("Telefono")
            or r.get("Teléfono")
            or ""
        ).strip()

        nombre_row = (
            r.get("Nombre")
            or r.get("Nombre y apellido")
            or r.get("Apellido y nombre")
            or r.get("Empleado")
            or r.get("Persona")
            or r.get("nombre")
            or ""
        )

        if telefono:
            try:
                to_whatsapp = normalize_to_whatsapp_e164(telefono)
            except Exception:
                pass

        if nombre_row and not nombre:
            nombre = str(nombre_row).strip()

        # Si ya tenemos WhatsApp, podemos cortar
        if to_whatsapp:
            break

    if not to_whatsapp:
        return {
            "ok": False,
            "error": "No se encontró número de WhatsApp para ese CUIL en el Excel de envíos",
        }, 400

    # Guardamos en identity_verification (incluyendo nombre)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO identity_verification
            (archivo_norm, dni, to_whatsapp, verified_at, source, nombre)
        VALUES (?, ?, ?, ?, ?, ?);
        """,
        (archivo_norm, dni, to_whatsapp, int(time.time()), "manual", nombre),
    )
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "archivo_norm": archivo_norm,
        "dni": dni,
        "to_whatsapp": to_whatsapp,
        "nombre": nombre,
        "source": "manual",
    }, 200


@app.route("/admin/send_template_all", methods=["POST"])
@admin_required
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
            telefono = s(
                r.get("Telefono_norm")
                or r.get("Telefono")
                or r.get("Teléfono")
            )

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
                time.sleep(0.7)   # 700 ms entre mensajes
                sid = send_template_whatsapp_norm(to, nombre)

                if sid:
                    # Registrar mensaje en SQLite
                    try:
                        save_message_sent(
                            message_sid=sid,
                            to_whatsapp=to,
                            archivo_norm=archivo_norm,
                            period_label=period_lbl,
                            kind="template",
                            nombre=nombre,
                        )
                    except Exception as e:
                        print("ERROR guardando message_status template:", e)

                    sent.append(
                        {
                            "archivo_norm": archivo_norm,
                            "to": to,
                            "nombre": nombre,
                            "sid": sid,
                        }
                    )

                    # 👉 esto es lo correcto acá:
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
    # Twilio manda form-data
    message_sid = request.form.get("MessageSid") or request.form.get("SmsSid") or ""
    status = (request.form.get("MessageStatus") or request.form.get("SmsStatus") or "").lower().strip()

    error_code = request.form.get("ErrorCode")
    error_message = request.form.get("ErrorMessage")

    # (opcional) log mínimo
    if not message_sid:
        return ("", 204)

    try:
        update_message_status_and_get(
            message_sid=message_sid,
            status=status or "unknown",
            error_code=error_code,
            error_message=error_message,
        )
    except Exception as e:
        print("ERROR twilio_status update:", e)

    return ("", 204)


@app.route("/admin/send_template_queue_start", methods=["POST"])
@admin_required
def admin_send_template_queue_start():

    period_lbl_raw = request.form.get("period") or request.args.get("period") or ""
    period_lbl = normalize_period_label(period_lbl_raw)
    if not period_lbl:
        return {"ok": False, "error": "Missing/invalid period"}, 400

    limit = int(request.form.get("limit") or request.args.get("limit") or 0)

    # Reutilizá la misma lectura de Excel que ya usás en el masivo
    rows = read_envios_rows()

    if limit > 0:
        rows = rows[:limit]

    # 🔒 FORZAMOS SIEMPRE REQUIRE_PDF = TRUE (ignoramos lo que venga del form)
    require_pdf = True

    result = enqueue_job(period_lbl, rows, require_pdf=require_pdf)
    job_id = result["job_id"]
    start_queue_worker_once()

    return {
        "ok": True,
        "job_id": job_id,
        "rate_per_min": QUEUE_RATE_PER_MIN,
        "sleep_sec_per_msg": QUEUE_SLEEP_SEC,
        "total_rows": len(rows),
        "enqueued": result["enqueued"],
        "skipped": result["skipped"],
        "require_pdf": require_pdf,
    }, 200


@app.route("/admin/envios_debug", methods=["GET"])
@admin_required
def admin_envios_debug():
    rows = read_envios_rows()
    sample = rows[:35] if rows else []
    return {
        "ok": True,
        "rows_count": len(rows),
        "sample_keys": list(sample[0].keys()) if sample else [],
        "sample_rows": sample,
    }, 200


@app.route("/admin/send_template_queue_status/<job_id>", methods=["GET"])
@admin_required
def admin_send_template_queue_status(job_id: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT job_id, period_label, status, created_at, started_at, finished_at,
               total_enqueued, total_sent, total_failed
        FROM send_jobs WHERE job_id=?;
        """,
        (job_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "job not found"}, 404

    keys = ["job_id","period_label","status","created_at","started_at","finished_at",
            "total_enqueued","total_sent","total_failed"]
    return {"ok": True, "job": dict(zip(keys, row))}, 200


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
    if x is None:
        return ""
    # si es float tipo 1234.0, lo pasamos a "1234"
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
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

def send_pdf_via_twilio_media(
    to_whatsapp: str,
    media_url: str,
    caption: str = "",
    archivo_norm: Optional[str] = None,
    period_label: Optional[str] = None,
):
    msg = twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_FROM,
        to=to_whatsapp,
        body=caption or None,
        media_url=[media_url],
        status_callback=STATUS_CALLBACK_URL,
    )
    print("DEBUG send_pdf_via_twilio_media SID:", msg.sid)

    try:
        save_message_sent(
            message_sid=msg.sid,
            to_whatsapp=to_whatsapp,
            archivo_norm=archivo_norm,
            period_label=period_label,
            kind="media",
            nombre=None,  # ya lo tenemos en la plantilla
        )
    except Exception as e:
        print("ERROR guardando message_status media:", e)

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

    archivo_norm, period_label, estado = get_recibo_estado(from_whatsapp)
    if not archivo_norm:
        msg = (
            "No encontré ningún recibo pendiente para este número 😕.\n"
            "Si creés que es un error, avisá a RRHH para que lo revisen 🙏."
        )
        return build_twilio_response(msg)

    pdf_id = find_pdf_for_archivo_and_period(archivo_norm, period_label)
    if not pdf_id:
        msg = (
            f"No pude encontrar el PDF de tu recibo para el período {period_label} 😕.\n"
            "Avisá a RRHH para que lo revisen 🙏."
        )
        return build_twilio_response(msg)

    media_url = build_media_url_for_twilio(pdf_id)

    if estado == "FIRMADO":
        # CASO 1
        caption = (
            "🤖 Ud. ya firmó su recibo.\n"
            "🤖 Le envío una copia.\n"
            "🤖 Solo puede visualizarlo una vez más."
        )
    elif estado == "OBSERVADO":
        # CASO 2
        caption = (
            "🤖 Ud. tiene el recibo observado.\n"
            "🤖 Le envío nuevamente el recibo.\n\n"
            "🤖 ¿Desea deshacer la observación y firmar?\n"
            "    1) Sí, deshacer y firmar\n"
            "    2) No, mantener observado"
        )
    else:
        # CASO 3 – DISPONIBLE (flujo normal)
        caption = (
            f"Acá tenés tu recibo de sueldo de {period_label} 📄\n\n"
            "🤖 ¿Confirma/firma su recibo?\n"
            "    1) Confirmar/Firmar\n"
            "    2) Observar"
        )

    send_pdf_via_twilio_media(
        from_whatsapp,
        media_url,
        caption=caption,
        archivo_norm=archivo_norm,
        period_label=period_label,
    )

    # No mandamos mensaje extra, ya quedó todo en el caption
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

#======================================
#notificacion rrhh
#TWILIO_ADMIN_WHATSAPP = os.getenv("TWILIO_ADMIN_WHATSAPP")  # ej: "whatsapp:+54911XXXXXXXX"


# === DESACTIVADO TEMPORALMENTE: notificación a RRHH ===
def notify_issue_to_admin(from_whatsapp: str):
    # Por ahora no hacemos nada para evitar notificaciones durante pruebas
    print(f"[DEBUG] notify_issue_to_admin() desactivado. Número: {from_whatsapp}")
    """
    Envía un mensaje a RRHH avisando que esta persona tuvo un problema con el PDF.
    Usa TWILIO_ADMIN_WHATSAPP como destino (WhatsApp).
    """
#    if not TWILIO_ADMIN_WHATSAPP:
#        print("TWILIO_ADMIN_WHATSAPP no está configurado, no se envía aviso a RRHH.")
 #       return
    # Normalizamos el número del admin al canal WhatsApp
 #   admin_to = TWILIO_ADMIN_WHATSAPP.strip()
  #  if not admin_to.startswith("whatsapp:"):
        # si lo pusiste como +54911..., lo convertimos a whatsapp:+54911...
   #     admin_to = "whatsapp:" + admin_to.lstrip("+")

    #try:
     #   nombre = ""
      #  try:
            # si tenés esta función definida, sino podés comentar este bloque
       #     nombre = resolve_name_for_phone(from_whatsapp) or ""
        #except Exception as e:
         #   print("WARN resolve_name_for_phone falló:", e)

        #archivo_norm = None
        #period_label = None
        #pending = get_last_pending_view(from_whatsapp)
        #if pending:
         #   archivo_norm, period_label = pending

#        partes = [f"El número {from_whatsapp} reporta observaciones al ver su recibo."]

 #       if nombre:
  #          partes.append(f"Nombre: {nombre}.")
   #     if archivo_norm:
    #        partes.append(f"CUIL/archivo: {archivo_norm}.")
     #   if period_label:
      #      partes.append(f"Período: {period_label}.")
#
 #       body = " ".join(partes)
#
 #       twilio_client.messages.create(
  ##          from_=TWILIO_WHATSAPP_FROM,  # sigue siendo tu número de WhatsApp
    #        to=admin_to,                 # ahora seguro es whatsapp:+549...
     #       body=body,
      #  )
       # print("DEBUG notify_issue_to_admin -> enviado a RRHH")
#
 #   except Exception as e:
  #      print("ERROR notify_issue_to_admin:", e)

#======================================

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

from flask import send_file

@app.route("/admin/report_recibos.xlsx", methods=["GET"])
@admin_required
def admin_report_recibos_xlsx():
    path = generate_excel_report()  # devuelve "/tmp/reporte_recibos.xlsx"
    return send_file(
        path,
        as_attachment=True,
        download_name="reporte_recibos.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

@app.route("/admin/reset_test_data", methods=["POST"])
@admin_required
def admin_reset_test_data():
    # protección simple anti-accidente
    confirm = (request.form.get("confirm") or "").strip().upper()
    if confirm != "YES":
        return {"ok": False, "error": "Para limpiar, enviá confirm=YES"}, 400

    conn = get_db_connection()
    cur = conn.cursor()

    # Limpieza de colas y tracking de mensajes
    cur.execute("DELETE FROM send_queue;")
    cur.execute("DELETE FROM send_jobs;")
    cur.execute("DELETE FROM message_status;")
    cur.execute("DELETE FROM pending_views;")
    cur.execute("DELETE FROM view_confirmations;")

    # Opcional (si querés resetear estados/vistas por periodo)
    cur.execute("DELETE FROM recibo_estado;")
    cur.execute("DELETE FROM recibo_vistas;")

    conn.commit()
    conn.close()
    return {"ok": True, "cleared": True}, 200


import csv
import io
from flask import Response

import csv
import io
from flask import Response

@app.route("/health", methods=["GET"])
def health():
    return {
        "ok": True,
        "service": "twilio-webhook",
        "status": "up"
    }, 200



@app.route("/admin/report_identity_verification.csv", methods=["GET"])
@admin_required
def admin_report_identity_verification():
    """
    Exporta un CSV con las identidades verificadas.
    Columnas: archivo_norm (CUIL), dni, to_whatsapp, verified_at, source
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            archivo_norm,
            dni,
            to_whatsapp,
            datetime(verified_at, 'unixepoch', 'localtime') AS verified_at_local,
            source
        FROM identity_verification
        ORDER BY verified_at DESC;
        """
    )
    rows = cur.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["archivo_norm", "dni", "to_whatsapp", "verified_at", "source"])

    for row in rows:
        writer.writerow([
            row["archivo_norm"],
            row["dni"],
            row["to_whatsapp"],
            row["verified_at_local"],
            row["source"],
        ])

    csv_data = output.getvalue()
    output.close()

    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="identity_verification_report.csv"'},
    )

# Alias por compatibilidad con el nombre viejo (así no se te rompen curls/bookmarks)
@app.route("/admin/report_dni_verification.csv", methods=["GET"])
@admin_required
def admin_report_dni_verification():
    return admin_report_identity_verification()

@app.route("/admin/identity_verification/clear_all", methods=["POST"])
@admin_required
def admin_clear_all_identity_verification():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM identity_verification;")
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return {"ok": True, "deleted": deleted}, 200

# Alias por compatibilidad (ruta vieja)
@app.route("/admin/dni_verification/clear_all", methods=["POST"])
@admin_required
def admin_clear_all_dni_verification():
    return admin_clear_all_identity_verification()


# ==========================
#  Webhook Twilio
# ==========================
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
    button_text = (form.get("ButtonText") or "").strip()

    body_lower = body.lower()
    telefono_norm = canonicalize_phone(from_whatsapp)
    session = get_session(telefono_norm)

    # ------------------------------------------------------------------
    # 0) CHEQUEO GLOBAL DE NÚMERO AUTORIZADO
    # ------------------------------------------------------------------
    # Si el número no está en la base (Excel / mapping), rechazamos de una.
    archivo_norm_incoming = get_archivo_from_incoming(from_whatsapp)
    if not archivo_norm_incoming:
        return build_twilio_response(
            "🤖 Ud. no está registrado/autorizado para utilizar este servicio."
        )

    # ------------------------------------------------------------------
    # 1) RESPUESTAS A PREGUNTAS ABIERTAS DEL FLUJO (ya hay contexto)
    # ------------------------------------------------------------------

    flow_state = session.get("flow_state", "IDLE")
    archivo_norm = session.get("archivo_norm")
    period_label = session.get("period_label")

    # Helper para evitar recalcular si no tenemos contexto
    def ensure_context():
        return bool(archivo_norm and period_label)

    # CASE DNI: antes de mostrar cualquier PDF
    if flow_state == "ASK_DNI":
        if not ensure_context():
            session["flow_state"] = "IDLE"
            return build_twilio_response(
                "Ocurrió un problema con el contexto. Escribí de nuevo *ver recibo*."
            )

        # Lo que escribió el usuario lo interpretamos como DNI
        dni_input = re.sub(r"\D", "", body)
        if not dni_input:
            return build_twilio_response(
                "Por favor ingresá tu DNI usando solo números."
            )

        expected_dni = get_dni_for_archivo(archivo_norm)
        if not expected_dni:
            # No tenemos DNI en la base → mejor no bloquear pero avisar
            session["flow_state"] = "IDLE"
            return build_twilio_response(
                "No pude validar tu DNI en el sistema. Por favor contactá a RRHH."
            )

        if dni_input != expected_dni:
            return build_twilio_response(
                "El DNI ingresado no coincide con nuestros registros. Volvé a intentarlo."
            )

        # DNI correcto → lo marcamos como verificado
        set_dni_verified(archivo_norm, expected_dni)
        session["flow_state"] = "IDLE"

        # A partir de acá continuamos como si recién hubiera entrado al flujo
        # y ya hubiera pasado la verificación. Usamos el estado del recibo.
        estado = get_recibo_estado(archivo_norm, period_label)

        # Aseguramos tener el pdf_id en sesión
        pdf_id = session.get("pdf_id")
        if not pdf_id:
            pdf_id = find_pdf_for_archivo_and_period(archivo_norm, period_label)
            session["pdf_id"] = pdf_id

        if not pdf_id:
            return build_twilio_response(
                "No pude encontrar el PDF en este momento. Por favor intentá más tarde o contactá a RRHH."
            )

        # ---------------- CASE 1: RECIBO FIRMADO ----------------
        if estado == "FIRMADO":
            vistas_actuales = get_recibo_vistas(archivo_norm, period_label)
            restantes = max(0, 3 - vistas_actuales)

            if restantes <= 0:
                return build_twilio_response(
                    f"🤖 Tu recibo del período {period_label} ya alcanzó el máximo de 3 visualizaciones adicionales."
                )

            msg = (
                f"🤖 Tu recibo de sueldo del período {period_label} ya está firmado.\n"
                f"🤖 ¿Querés verlo nuevamente? Te quedan {restantes} de 3 visualizaciones.\n"
                "    1) Sí, enviar copia\n"
                "    2) No"
            )
            session["flow_state"] = "ASK_FIRMADO_VISTA"
            return build_twilio_response(msg)

        # ---------------- CASE 2: RECIBO OBSERVADO ----------------
        if estado == "OBSERVADO":
            media_url = build_media_url_for_twilio(pdf_id)
            caption = (
                "🤖 Ud. tiene el recibo observado.\n"
                "🤖 Le envío nuevamente el recibo.\n\n"
                "🤖 ¿Desea deshacer la observación y firmar?\n"
                "    1) Sí, deshacer y firmar\n"
                "    2) No, mantener observado"
            )
            send_pdf_via_twilio_media(
                from_whatsapp,
                media_url,
                caption=caption,
                archivo_norm=archivo_norm,
                period_label=period_label,
            )
            session["flow_state"] = "ASK_DESHACER_OBS"
            return ("", 200)

        # ---------------- CASE 3: RECIBO DISPONIBLE ----------------
        media_url = build_media_url_for_twilio(pdf_id)
        caption = (
            "🤖 Aquí tiene su recibo.\n\n"
            "🤖 ¿Confirma/firma su recibo?\n"
            "    1) Confirmar/Firmar\n"
            "    2) Observar"
        )
        send_pdf_via_twilio_media(
            from_whatsapp,
            media_url,
            caption=caption,
            archivo_norm=archivo_norm,
            period_label=period_label,
        )
        session["flow_state"] = "ASK_FIRMAR_OBS"
        return ("", 200)

    # CASE 2: recibo OBSERVADO -> "¿Desea deshacer la observación y firmar?"
    if flow_state == "ASK_DESHACER_OBS":
        if not ensure_context():
            session["flow_state"] = "IDLE"
            return build_twilio_response(
                "Ocurrió un problema con el contexto. Escribí de nuevo *ver recibo*."
            )

        if body_lower in ("1", "si", "sí", "si,", "sí,", "deshacer", "deshacer y firmar"):
            # Deshacer observación y firmar
            set_recibo_estado(archivo_norm, period_label, "FIRMADO")
            save_user_confirmation(from_whatsapp, "firmado")  # opcional, para tus reportes
            session["flow_state"] = "IDLE"
            return build_twilio_response("🤖 Firmado exitosamente.")
        elif body_lower in ("2", "no", "mantener", "mantener observado"):
            # Mantener observado
            set_recibo_estado(archivo_norm, period_label, "OBSERVADO")
            save_user_confirmation(from_whatsapp, "observado")
            session["flow_state"] = "IDLE"
            return build_twilio_response("🤖 Se mantiene la observación.")
        else:
            # Respuesta inválida → repetir pregunta
            return build_twilio_response("🤖 Por favor responda *1* o *2*.")

    # CASE 3: recibo DISPONIBLE -> después de enviar PDF preguntamos:
    # "¿Confirma/firma su recibo? 1) Confirmar/Firmar 2) Observar"
    if flow_state == "ASK_FIRMAR_OBS":
        if not ensure_context():
            session["flow_state"] = "IDLE"
            return build_twilio_response(
                "Ocurrió un problema con el contexto. Escribí de nuevo *ver recibo*."
            )

        if body_lower in ("1", "firmar", "confirmar", "confirmar/firmar"):
            set_recibo_estado(archivo_norm, period_label, "FIRMADO")
            save_user_confirmation(from_whatsapp, "firmado")
            session["flow_state"] = "IDLE"
            return build_twilio_response("🤖 Firmado exitosamente.")
        elif body_lower in ("2", "observar"):
            set_recibo_estado(archivo_norm, period_label, "OBSERVADO")
            save_user_confirmation(from_whatsapp, "observado")
            notify_issue_to_admin(from_whatsapp)
            session["flow_state"] = "IDLE"
            return build_twilio_response(
                f"🤖 Su recibo del período {period_label} quedó registrado como *observado*.\n"
                "🤖 Para resolverlo, por favor comuníquese con RRHH.\n\n"
                "Si desea visualizarlo nuevamente más tarde, escriba *ver recibo*."
            )
        else:
            return build_twilio_response("🤖 Por favor responda *1* o *2*.")

    # Paso intermedio de CASE 3:
    # "¿Desea visualizar su recibo?" → acá esperamos 'sí' para mandar el PDF.
    if flow_state == "ASK_VISUALIZAR":
        if not ensure_context():
            session["flow_state"] = "IDLE"
            return build_twilio_response(
                "Ocurrió un problema con el contexto. Escribí de nuevo *ver recibo*."
            )

        if body_lower in ("si", "sí", "s", "ver", "ver recibo", "ok"):
            pdf_id = session.get("pdf_id")
            if not pdf_id:
                session["flow_state"] = "IDLE"
                return build_twilio_response(
                    "No pude encontrar el PDF en este momento. Por favor intentá más tarde o contactá a RRHH."
                )

            media_url = build_media_url_for_twilio(pdf_id)

            caption = (
                "🤖 Aquí tiene su recibo.\n\n"
                "🤖 ¿Confirma/firma su recibo?\n"
                "    1) Confirmar/Firmar\n"
                "    2) Observar"
            )

            # Enviamos el PDF por API (no como respuesta TwiML)
            send_pdf_via_twilio_media(
                from_whatsapp,
                media_url,
                caption=caption,
                archivo_norm=archivo_norm,
                period_label=period_label,
            )

            # Ahora esperamos la respuesta 1/2
            session["flow_state"] = "ASK_FIRMAR_OBS"
            return ("", 200)
        elif body_lower in ("no", "despues", "más tarde", "después"):
            session["flow_state"] = "IDLE"
            return build_twilio_response(
                "Perfecto. Cuando quieras verlo, escribí *ver recibo*."
            )
        else:
            return build_twilio_response(
                "🤖 Por favor respondé *sí* si querés visualizar tu recibo."
            )

    # CASE FIRMADO: ya está firmado y le preguntamos si quiere verlo de nuevo
    if flow_state == "ASK_FIRMADO_VISTA":
        if not ensure_context():
            session["flow_state"] = "IDLE"
            return build_twilio_response(
                "Ocurrió un problema con el contexto. Escribí de nuevo *ver recibo*."
            )

        vistas_actuales = get_recibo_vistas(archivo_norm, period_label)
        restantes = max(0, 3 - vistas_actuales)

        if restantes <= 0:
            session["flow_state"] = "IDLE"
            return build_twilio_response(
                f"🤖 Tu recibo del período {period_label} ya alcanzó el máximo de 3 visualizaciones adicionales."
            )

        if body_lower in ("1", "si", "sí", "ver", "ver recibo", "enviar", "si,", "sí,"):
            # Incrementamos contador y enviamos copia
            nuevas_vistas = inc_recibo_vistas(archivo_norm, period_label)
            restantes_luego = max(0, 3 - nuevas_vistas)

            pdf_id = session.get("pdf_id")
            if not pdf_id:
                # por las dudas, volvemos a buscarlo
                pdf_id = find_pdf_for_archivo_and_period(archivo_norm, period_label)
                session["pdf_id"] = pdf_id

            if not pdf_id:
                session["flow_state"] = "IDLE"
                return build_twilio_response(
                    "No pude encontrar el PDF en este momento. Por favor intentá más tarde o contactá a RRHH."
                )

            media_url = build_media_url_for_twilio(pdf_id)
            caption = (
                f"🤖 Aquí tiene la copia de su recibo firmado del período {period_label}.\n"
                f"🤖 Visualizaciones restantes: {restantes_luego} de 3."
            )
            send_pdf_via_twilio_media(
                from_whatsapp,
                media_url,
                caption=caption,
                archivo_norm=archivo_norm,
                period_label=period_label,
            )

            session["flow_state"] = "IDLE"
            return ("", 200)

        elif body_lower in ("2", "no", "gracias"):
            session["flow_state"] = "IDLE"
            return build_twilio_response(
                "🤖 Perfecto, tu recibo ya figura como firmado. Cuando quieras, podés volver a escribir."
            )
        else:
            msg = (
                f"🤖 Tu recibo del período {period_label} ya está firmado.\n"
                f"🤖 ¿Querés verlo nuevamente? Te quedan {restantes} de 3 visualizaciones.\n"
                "    1) Sí, enviar copia\n"
                "    2) No"
            )
            return build_twilio_response(msg)

    # ------------------------------------------------------------------
    # 2) ENTRADA NUEVA AL FLUJO (MENSAJE RECIBIDO EN WHATS)
    # ------------------------------------------------------------------

    # Botón “Sí, visualizar” de la plantilla → maneja según estado
    if button_payload == "VIEW_NOW" or button_text.lower().startswith("sí, visualizar"):
        # 2.1) NÚMERO AUTORIZADO YA VALIDADO ARRIBA
        archivo_norm = archivo_norm_incoming

        # 2.2) PERÍODO ACTUAL
        period_label = norm_period_label(get_current_period_label())

        # 2.3) ¿TIENE RECIBO DEL ÚLTIMO PERÍODO?
        pdf_id = find_pdf_for_archivo_and_period(archivo_norm, period_label)
        if not pdf_id:
            msg = (
                "🤖 Ud. no posee recibo disponible en este período.\n"
                "🤖 Por favor acérquese a RRHH."
            )
            return build_twilio_response(msg)

        # Guardamos contexto en la sesión
        session["archivo_norm"] = archivo_norm
        session["period_label"] = period_label
        session["pdf_id"] = pdf_id

        # 🔒 Paso previo: verificar DNI (solo la primera vez)
        if not is_dni_verified(archivo_norm):
            session["flow_state"] = "ASK_DNI"
            return build_twilio_response(
                "Por seguridad, por favor ingresá tu DNI (solo números) para validar tu identidad."
            )

        # 2.4) ¿ESTADO DEL RECIBO?  (FIRMADO / OBSERVADO / DISPONIBLE)
        estado = get_recibo_estado(archivo_norm, period_label)

        # ---------------- CASE 1: RECIBO FIRMADO ----------------
        if estado == "FIRMADO":
            vistas_actuales = get_recibo_vistas(archivo_norm, period_label)
            restantes = max(0, 3 - vistas_actuales)

            if restantes <= 0:
                session["flow_state"] = "IDLE"
                return build_twilio_response(
                    f"🤖 Tu recibo del período {period_label} ya alcanzó el máximo de 3 visualizaciones adicionales."
                )

            msg = (
                f"🤖 Tu recibo de sueldo del período {period_label} ya está firmado.\n"
                f"🤖 ¿Querés verlo nuevamente? Te quedan {restantes} de 3 visualizaciones.\n"
                "    1) Sí, enviar copia\n"
                "    2) No"
            )
            session["flow_state"] = "ASK_FIRMADO_VISTA"
            return build_twilio_response(msg)

        # ---------------- CASE 2: RECIBO OBSERVADO ----------------
        if estado == "OBSERVADO":
            media_url = build_media_url_for_twilio(pdf_id)
            caption = (
                "🤖 Ud. tiene el recibo observado.\n"
                "🤖 Le envío nuevamente el recibo.\n\n"
                "🤖 ¿Desea deshacer la observación y firmar?\n"
                "    1) Sí, deshacer y firmar\n"
                "    2) No, mantener observado"
            )
            send_pdf_via_twilio_media(
                from_whatsapp,
                media_url,
                caption=caption,
                archivo_norm=archivo_norm,
                period_label=period_label,
            )
            session["flow_state"] = "ASK_DESHACER_OBS"
            return ("", 200)

        # ---------------- CASE 3: RECIBO DISPONIBLE ----------------
        # Botón = YA dijo que quiere visualizar → mandamos directo el PDF
        media_url = build_media_url_for_twilio(pdf_id)
        caption = (
            "🤖 Aquí tiene su recibo.\n\n"
            "🤖 ¿Confirma/firma su recibo?\n"
            "    1) Confirmar/Firmar\n"
            "    2) Observar"
        )
        send_pdf_via_twilio_media(
            from_whatsapp,
            media_url,
            caption=caption,
            archivo_norm=archivo_norm,
            period_label=period_label,
        )
        session["flow_state"] = "ASK_FIRMAR_OBS"
        return ("", 200)

    # Palabras que disparan el flujo principal cuando ESCRIBE (no botón)
    if body_lower in ("ver", "ver recibo", "ver recibo de sueldo"):
        # 2.1) NÚMERO AUTORIZADO YA VALIDADO ARRIBA
        archivo_norm = archivo_norm_incoming

        # 2.2) PERÍODO ACTUAL
        period_label = norm_period_label(get_current_period_label())

        # 2.3) ¿TIENE RECIBO DEL ÚLTIMO PERÍODO?
        pdf_id = find_pdf_for_archivo_and_period(archivo_norm, period_label)
        if not pdf_id:
            msg = (
                "🤖 Ud. no posee recibo disponible en este período.\n"
                "🤖 Por favor acérquese a RRHH."
            )
            return build_twilio_response(msg)

        # Guardamos contexto en la sesión
        session["archivo_norm"] = archivo_norm
        session["period_label"] = period_label
        session["pdf_id"] = pdf_id

        # 🔒 Paso previo: verificar DNI (solo la primera vez)
        if not is_dni_verified(archivo_norm):
            session["flow_state"] = "ASK_DNI"
            return build_twilio_response(
                "Por seguridad, por favor ingresá tu DNI (solo números) para validar tu identidad."
            )

        # 2.4) ¿ESTADO DEL RECIBO?  (FIRMADO / OBSERVADO / DISPONIBLE)
        estado = get_recibo_estado(archivo_norm, period_label)

        # ---------------- CASE 1: RECIBO FIRMADO ----------------
        if estado == "FIRMADO":
            vistas_actuales = get_recibo_vistas(archivo_norm, period_label)
            restantes = max(0, 3 - vistas_actuales)

            if restantes <= 0:
                session["flow_state"] = "IDLE"
                return build_twilio_response(
                    f"🤖 Tu recibo del período {period_label} ya alcanzó el máximo de 3 visualizaciones adicionales."
                )

            msg = (
                f"🤖 Tu recibo de sueldo del período {period_label} ya está firmado.\n"
                f"🤖 ¿Querés verlo nuevamente? Te quedan {restantes} de 3 visualizaciones.\n"
                "    1) Sí, enviar copia\n"
                "    2) No"
            )
            session["flow_state"] = "ASK_FIRMADO_VISTA"
            return build_twilio_response(msg)

        # ---------------- CASE 2: RECIBO OBSERVADO ----------------
        if estado == "OBSERVADO":
            media_url = build_media_url_for_twilio(pdf_id)
            caption = (
                "🤖 Ud. tiene el recibo observado.\n"
                "🤖 Le envío nuevamente el recibo.\n\n"
                "🤖 ¿Desea deshacer la observación y firmar?\n"
                "    1) Sí, deshacer y firmar\n"
                "    2) No, mantener observado"
            )
            send_pdf_via_twilio_media(
                from_whatsapp,
                media_url,
                caption=caption,
                archivo_norm=archivo_norm,
                period_label=period_label,
            )
            session["flow_state"] = "ASK_DESHACER_OBS"
            return ("", 200)

        # ---------------- CASE 3: RECIBO DISPONIBLE ----------------
        # Cuando ESCRIBE "ver recibo", sí preguntamos primero
        session["flow_state"] = "ASK_VISUALIZAR"
        return build_twilio_response("🤖 ¿Desea visualizar su recibo?")

    # ------------------------------------------------------------------
    # 3) MENSAJE QUE NO ENTRA EN NINGÚN FLUJO → TEXTO SEGÚN ESTADO
    # ------------------------------------------------------------------
    archivo_norm_fallback = archivo_norm_incoming
    if archivo_norm_fallback:
        period_label_fallback = norm_period_label(get_current_period_label())
        estado_fallback = get_recibo_estado(archivo_norm_fallback, period_label_fallback)

        if estado_fallback == "FIRMADO":
            vistas_actuales = get_recibo_vistas(
                archivo_norm_fallback, period_label_fallback
            )
            restantes = max(0, 3 - vistas_actuales)

            if restantes <= 0:
                session["flow_state"] = "IDLE"
                return build_twilio_response(
                    f"🤖 Tu recibo del período {period_label_fallback} ya alcanzó el máximo de 3 visualizaciones adicionales."
                )

            # Guardamos contexto por si responde 1 / 2
            session["archivo_norm"] = archivo_norm_fallback
            session["period_label"] = period_label_fallback
            pdf_id_fb = find_pdf_for_archivo_and_period(
                archivo_norm_fallback, period_label_fallback
            )
            session["pdf_id"] = pdf_id_fb
            session["flow_state"] = "ASK_FIRMADO_VISTA"

            msg = (
                f"🤖 Tu recibo de sueldo del período {period_label_fallback} ya está firmado.\n"
                f"🤖 ¿Querés verlo nuevamente? Te quedan {restantes} de 3 visualizaciones.\n"
                "    1) Sí, enviar copia\n"
                "    2) No"
            )
            return build_twilio_response(msg)

        if estado_fallback == "OBSERVADO":
            msg = (
                f"🤖 Tu recibo de sueldo del período {period_label_fallback} está observado.\n"
                "Por favor acercate a RRHH para que lo revisen.\n"
                "Si querés volver a verlo, escribí *ver recibo*."
            )
            return build_twilio_response(msg)

    # Si no hay nada especial, mensaje genérico
    msg = (
        "Hola 👋\n"
        "Si querés consultar tu recibo de sueldo del último período, escribí *ver recibo* "
        "o usá el botón *Sí, visualizar* cuando te llegue la notificación."
    )
    return build_twilio_response(msg)
#=============================
@app.route("/admin/identity_verification/delete", methods=["POST"])
@admin_required
def admin_identity_verification_delete():
    """
    Elimina UNA entrada de identity_verification (una persona) por archivo_norm (CUIL).
    """
    archivo_norm = (request.form.get("archivo_norm") or "").strip()
    token = (request.form.get("token") or "").strip()

    if not archivo_norm:
        return {"ok": False, "error": "archivo_norm requerido"}, 400

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM identity_verification WHERE archivo_norm = ?;", (archivo_norm,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()

    # Volvemos al panel
    if token:
        return redirect(f"/admin/panel?token={token}")
    return redirect("/admin/panel")

#=============================

@app.route("/admin/send_template_queue_stop", methods=["POST"])
@admin_required
def admin_send_template_queue_stop():
    """
    Marca un job de la cola como STOPPED para que el worker deje de enviar.
    """
    job_id = (request.form.get("job_id") or "").strip()
    token = (request.form.get("token") or "").strip()

    if not job_id:
        return {"ok": False, "error": "job_id requerido"}, 400

    now_ts = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE send_jobs
        SET status = 'STOPPED',
            finished_at = COALESCE(finished_at, ?)
        WHERE job_id = ? AND status IN ('PENDING','RUNNING');
        """,
        (now_ts, job_id),
    )
    updated = cur.rowcount
    conn.commit()
    conn.close()

    if not updated:
        # Nada cambió (ya estaba DONE/STOPPED o no existía)
        if token:
            return redirect(f"/admin/panel?token={token}")
        return redirect("/admin/panel")

    if token:
        return redirect(f"/admin/panel?token={token}")
    return redirect("/admin/panel")

#=============================

@app.route("/admin/identity_verification/send", methods=["POST"])
@admin_required
def admin_identity_verification_send():
    """
    Encola envíos SOLO para las identidades seleccionadas en el panel,
    usando el mismo Excel de envíos (read_envios_rows) y la misma lógica de enqueue_job,
    pero filtrando por los CUILs (archivo_norm) marcados.
    """
    token = _get_admin_token_from_request()

    # Período desde el form (viene del input de arriba del botón "Enviar plantilla a seleccionados")
    period_raw = request.form.get("period") or ""
    period_lbl = normalize_period_label(period_raw)  # misma función que usás en el masivo
    if not period_lbl:
        # Período inválido → mensaje en el panel
        return redirect(f"/admin/panel?token={token}&msg=bad_period")

    # CUILs seleccionados en la tabla (checkboxes name='ids')
    selected_ids = request.form.getlist("ids")
    selected_ids = [s.strip() for s in selected_ids if s.strip()]
    if not selected_ids:
        return redirect(f"/admin/panel?token={token}&msg=no_ids")

    selected_set = set(selected_ids)

    # Leemos el Excel de envíos
    try:
        all_rows = read_envios_rows()
    except Exception:
        all_rows = []

    # Filtramos SOLO las filas cuyo archivo_norm/CUIL esté en selected_set
    filtered_rows = []
    for r in all_rows:
        archivo_norm = s(
            r.get("Archivo_norm")
            or r.get("archivo_norm")
            or r.get("Archivo")
            or r.get("archivo")
            or r.get("CUIL")
            or r.get("Cuil")
        )
        if archivo_norm and archivo_norm in selected_set:
            filtered_rows.append(r)

    if not filtered_rows:
        # No se encontró ninguna fila del Excel que matchee con los CUIL seleccionados
        return redirect(f"/admin/panel?token={token}&msg=no_rows_for_ids")

    # Encolamos solamente esas filas, con require_pdf=True (solo si existe PDF)
    result = enqueue_job(period_lbl, filtered_rows, require_pdf=True)
    job_id = result["job_id"]
    start_queue_worker_once()

    # Redirigimos al panel con info del job
    enq = result.get("enqueued", 0)
    skipped = result.get("skipped", 0)
    return redirect(
        f"/admin/panel?token={token}&msg=send_ok&job={job_id}&enq={enq}&skipped={skipped}"
    )


#=============================
@app.route("/admin/identity_verification/bulk", methods=["POST"])
@admin_required
def admin_identity_verification_bulk():
    """
    Acciones en lote sobre identidades verificadas (por ahora: borrar seleccionados).
    """
    token = _get_admin_token_from_request()
    ids = request.form.getlist("ids")  # valores = archivo_norm
    action = request.form.get("bulk_action") or "delete"

    if not ids:
        return redirect(f"/admin/panel?token={token or ''}")

    conn = get_db_connection()
    cur = conn.cursor()

    if action == "delete":
        placeholders = ",".join("?" * len(ids))
        cur.execute(
            f"DELETE FROM identity_verification WHERE archivo_norm IN ({placeholders})",
            ids,
        )
        conn.commit()

    conn.close()
    return redirect(f"/admin/panel?token={token or ''}")

@app.route("/admin/identity_verification/upload", methods=["POST"])
@admin_required
def admin_identity_verification_upload():
    """
    Carga masiva de identidades verificadas desde CSV/Excel.
    Espera columnas tipo: archivo_norm / CUIL, dni / DNI, to_whatsapp / Telefono...
    Además, busca el nombre en el Excel de envíos y lo guarda en identity_verification.
    """
    file = request.files.get("file")
    if not file or file.filename == "":
        return {"ok": False, "error": "Archivo no enviado"}, 400

    filename = file.filename
    ext = os.path.splitext(filename)[1].lower()

    import pandas as pd

    try:
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(file)
        else:
            # CSV: autodetectar separador
            df = pd.read_csv(file, sep=None, engine="python")
    except Exception as e:
        print("ERROR leyendo archivo de upload identidades:", e)
        return {"ok": False, "error": "No se pudo leer el archivo"}, 400

    # Normalizamos nombres de columnas a minúscula simple
    df.columns = [str(c).strip().lower() for c in df.columns]

    # Mapa CUIL -> nombre desde Excel de envíos
    try:
        envios_rows = read_envios_rows()
    except Exception as e:
        print("ERROR read_envios_rows en upload:", e)
        envios_rows = []

    cuil_to_name = {}
    for r in envios_rows:
        cuil_row = str(
            r.get("Archivo_norm")
            or r.get("archivo_norm")
            or r.get("Archivo")
            or r.get("archivo")
            or r.get("CUIL")
            or r.get("Cuil")
            or ""
        ).strip()
        nombre_row = (
            r.get("Nombre")
            or r.get("Nombre y apellido")
            or r.get("Apellido y nombre")
            or r.get("Empleado")
            or r.get("Persona")
            or r.get("nombre")
            or ""
        )
        if cuil_row and nombre_row and cuil_row not in cuil_to_name:
            cuil_to_name[cuil_row] = str(nombre_row).strip()

    conn = get_db_connection()
    cur = conn.cursor()

    inserted = 0
    skipped = 0

    for _, row in df.iterrows():
        cuil = str(
            row.get("archivo_norm")
            or row.get("cuil")
            or row.get("archivo")
            or ""
        ).strip()
        dni = str(
            row.get("dni")
            or row.get("documento")
            or ""
        ).strip()
        to_whatsapp = str(
            row.get("to_whatsapp")
            or row.get("whatsapp")
            or row.get("telefono_norm")
            or row.get("telefono")
            or ""
        ).strip()

        if not cuil or not dni or not to_whatsapp:
            skipped += 1
            continue

        try:
            to_whatsapp = normalize_to_whatsapp_e164(to_whatsapp)
        except Exception:
            skipped += 1
            continue

        nombre = cuil_to_name.get(cuil, "")

        # Si querés ser más estricto y NO insertar si el CUIL no existe en envíos:
        if not nombre:
            print(f"WARNING: CUIL {cuil} no encontrado en Excel de envíos, se omite.")
            skipped += 1
            continue

        cur.execute(
            """
            INSERT OR REPLACE INTO identity_verification
                (archivo_norm, dni, to_whatsapp, verified_at, source, nombre)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (cuil, dni, to_whatsapp, int(time.time()), "upload", nombre),
        )
        inserted += 1

    conn.commit()
    conn.close()

    print(f"UPLOAD identity_verification: inserted={inserted}, skipped={skipped}")

    # Volvemos al panel
    token = _get_admin_token_from_request()
    return redirect(f"/admin/panel?token={token or ''}")


@app.route("/admin/identity_template.csv", methods=["GET"])
@admin_required
def admin_identity_template_csv():
    """
    Devuelve un CSV de ejemplo para subir identidades verificadas.
    Columnas: CUIL;DNI;WhatsApp
    """
    csv_data = "CUIL;DNI;WhatsApp\n20-12345678-3;12345678;+5491112345678\n"
    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="identity_template.csv"'},
    )

import html  # asegurate de tener este import arriba del archivo

def esc_html(value: str) -> str:
    """
    Escapa texto para incrustar seguro en HTML.
    """
    return html.escape(str(value or ""), quote=True)



@app.route("/admin/panel", methods=["GET"])
@admin_required
def admin_panel():
    """
    Panel HTML para operar todo desde el navegador.
    Requiere ?token=... o header X-Admin-Token.
    """
    token = _get_admin_token_from_request()

    # =========================
    # Datos base: Excel de envíos
    # =========================
    try:
        envios_rows = read_envios_rows()
    except Exception:
        envios_rows = []
    envios_count = len(envios_rows)
    envios_sample = envios_rows[:10]

    # Mapa CUIL -> nombre (para mostrar nombre en identidades verificadas)
    cuil_to_name = {}
    for r in envios_rows:
        archivo_norm = str(
            r.get("Archivo_norm")
            or r.get("archivo_norm")
            or r.get("Archivo")
            or r.get("archivo")
            or r.get("CUIL")
            or r.get("Cuil")
            or ""
        ).strip()
        nombre = (
            r.get("Nombre")
            or r.get("Nombre y apellido")
            or r.get("Apellido y nombre")
            or r.get("Empleado")
            or r.get("Persona")
            or r.get("nombre")
            or ""
        )
        if archivo_norm and nombre and archivo_norm not in cuil_to_name:
            cuil_to_name[archivo_norm] = str(nombre).strip()

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Contador de identidades
    try:
        cur.execute("SELECT COUNT(*) AS c FROM identity_verification;")
        row = cur.fetchone()
        identity_count = row["c"] if row else 0
    except Exception:
        identity_count = 0

    # Últimos jobs de cola
    try:
        cur.execute(
            """
            SELECT job_id, period_label, status, created_at, started_at, finished_at,
                   total_enqueued, total_sent, total_failed
            FROM send_jobs
            ORDER BY created_at DESC
            LIMIT 10;
            """
        )
        jobs = cur.fetchall()
    except Exception:
        jobs = []

    # Identidades verificadas (últimas 200)
    cur.execute(
        """
        SELECT archivo_norm, dni, to_whatsapp,
               datetime(verified_at, 'unixepoch', 'localtime') AS verified_at_local,
               verified_at,
               source
        FROM identity_verification
        ORDER BY verified_at DESC
        LIMIT 200;
        """
    )
    identity_rows = cur.fetchall()

    conn.close()

    def fmt_ts(ts):
        if not ts:
            return ""
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(ts)

    # =========================
    # HTML
    # =========================
    html = []
    html.append("<!doctype html>")
    html.append("<html lang='es'>")
    html.append("<head>")
    html.append("<meta charset='utf-8'>")
    html.append("<title>Panel admin - Recibos WhatsApp</title>")
    html.append("""
    <style>
      :root {
        --bg: #0f172a;
        --bg-card: #111827;
        --bg-card-soft: #020617;
        --accent: #22c55e;
        --accent-soft: rgba(34, 197, 94, 0.12);
        --accent-strong: #16a34a;
        --border-subtle: #1f2937;
        --text-main: #e5e7eb;
        --text-muted: #9ca3af;
        --danger: #ef4444;
        --danger-soft: rgba(239, 68, 68, 0.12);
        --warning: #eab308;
        --warning-soft: rgba(234, 179, 8, 0.12);
      }
      * {
        box-sizing: border-box;
      }
      body {
        margin: 0;
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: radial-gradient(circle at top, #1f2937 0, #020617 55%, #020617 100%);
        color: var(--text-main);
      }
      a {
        color: var(--accent);
        text-decoration: none;
      }
      a:hover {
        text-decoration: underline;
      }
      .layout {
        max-width: 1100px;
        margin: 0 auto;
        padding: 24px 16px 40px;
      }
      .topbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 24px;
      }
      .topbar-title {
        font-size: 22px;
        font-weight: 600;
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .badge-live {
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        background: var(--accent-soft);
        color: var(--accent);
        border-radius: 999px;
        padding: 3px 8px;
      }
      .topbar-meta {
        font-size: 12px;
        color: var(--text-muted);
      }
      .grid-summary {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 12px;
        margin-bottom: 20px;
      }
      .card {
        background: radial-gradient(circle at top left, #111827 0, #020617 55%);
        border-radius: 14px;
        border: 1px solid var(--border-subtle);
        padding: 12px 14px;
      }
      .card h2 {
        font-size: 13px;
        font-weight: 500;
        margin: 0 0 6px 0;
        color: var(--text-muted);
      }
      .card-main {
        font-size: 24px;
        font-weight: 600;
      }
      .card-sub {
        font-size: 11px;
        color: var(--text-muted);
        margin-top: 2px;
      }

      .section {
        margin-top: 20px;
        padding: 14px 16px 16px;
        background: linear-gradient(135deg, #020617 0%, #020617 55%, #0b1120 100%);
        border-radius: 16px;
        border: 1px solid var(--border-subtle);
      }
      .section-header {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        margin-bottom: 8px;
      }
      .section-title {
        font-size: 16px;
        font-weight: 600;
      }
      .section-sub {
        font-size: 12px;
        color: var(--text-muted);
      }

      form {
        margin-top: 4px;
      }
      label {
        font-size: 12px;
        color: var(--text-muted);
        display: block;
        margin-top: 6px;
      }
      input[type='text'],
      input[type='number'],
      input[type='search'] {
        margin-top: 3px;
        padding: 6px 8px;
        width: 220px;
        border-radius: 8px;
        border: 1px solid #374151;
        background: rgba(15, 23, 42, 0.9);
        color: var(--text-main);
        font-size: 13px;
      }
      input:focus {
        outline: 1px solid var(--accent-strong);
        outline-offset: 1px;
      }
      .checkbox-row {
        display: flex;
        align-items: center;
        gap: 6px;
        margin-top: 8px;
        font-size: 12px;
        color: var(--text-muted);
      }
      input[type='checkbox'] {
        accent-color: var(--accent);
      }
      .btn-primary {
        margin-top: 10px;
        padding: 7px 14px;
        border-radius: 999px;
        border: none;
        cursor: pointer;
        font-size: 13px;
        font-weight: 500;
        background: radial-gradient(circle at top left, var(--accent) 0, var(--accent-strong) 60%);
        color: #022c22;
        box-shadow: 0 0 0 1px rgba(34,197,94,0.4), 0 8px 20px rgba(22,163,74,0.25);
      }
      .btn-primary:hover {
        filter: brightness(1.05);
      }
      .btn-danger {
        margin-top: 6px;
        padding: 6px 12px;
        border-radius: 999px;
        border: none;
        cursor: pointer;
        font-size: 12px;
        font-weight: 500;
        background: var(--danger-soft);
        color: var(--danger);
      }
      .btn-danger:hover {
        filter: brightness(1.05);
      }
      .btn-link {
        background: none;
        border: none;
        color: var(--accent);
        font-size: 12px;
        padding: 0;
        cursor: pointer;
      }

      table {
        width: 100%;
        border-collapse: collapse;
        margin-top: 8px;
      }
      th, td {
        padding: 6px 8px;
        font-size: 12px;
        border-bottom: 1px solid #1f2937;
        text-align: left;
      }
      th {
        color: var(--text-muted);
        font-weight: 500;
        background: rgba(15, 23, 42, 0.7);
        cursor: default;
      }
      tr:hover td {
        background: rgba(15, 23, 42, 0.7);
      }
      .mono {
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
        font-size: 11px;
      }
      .badge-status {
        display: inline-flex;
        align-items: center;
        padding: 2px 8px;
        border-radius: 999px;
        font-size: 11px;
        font-weight: 500;
      }
      .badge-status.pending {
        background: var(--warning-soft);
        color: var(--warning);
      }
      .badge-status.running {
        background: #0ea5e933;
        color: #38bdf8;
      }
      .badge-status.done {
        background: var(--accent-soft);
        color: var(--accent);
      }
      .badge-status.stopped {
        background: var(--danger-soft);
        color: var(--danger);
      }
      .small {
        font-size: 11px;
        color: var(--text-muted);
      }
      .links-inline a {
        margin-right: 12px;
        font-size: 13px;
      }
      .two-cols {
        display: grid;
        grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
        gap: 16px;
      }
      @media (max-width: 800px) {
        .grid-summary {
          grid-template-columns: 1fr;
        }
        .two-cols {
          grid-template-columns: 1fr;
        }
      }

      .identities-table-wrapper {
        margin-top: 8px;
        max-height: 260px;
        overflow-y: auto;
        border-radius: 10px;
        border: 1px solid #111827;
      }
      .sortable {
        cursor: pointer;
      }
      .sortable span {
        font-size: 10px;
        opacity: 0.6;
      }
    </style>
    """)
    html.append("</head>")
    html.append("<body>")
    html.append("<div class='layout'>")

    # Topbar
    html.append("<div class='topbar'>")
    html.append("<div>")
    html.append("<div class='topbar-title'>")
    html.append("📲 Panel administrativo &nbsp; <span class='badge-live'>Recibos WhatsApp</span>")
    html.append("</div>")
    html.append("<div class='topbar-meta'>Gestioná envíos, verificaciones y reportes desde un solo lugar.</div>")
    html.append("</div>")
    html.append("<div class='topbar-meta'>")
    html.append("Acceso con token: <code>%s</code>" % (esc_html(token) if token else "—"))
    html.append("</div>")
    html.append("</div>")

    # Summary cards
    html.append("<div class='grid-summary'>")
    html.append("<div class='card'>")
    html.append("<h2>Excel de envíos</h2>")
    html.append(f"<div class='card-main'>{envios_count}</div>")
    html.append("<div class='card-sub'>filas detectadas en el archivo de envíos</div>")
    html.append("</div>")

    html.append("<div class='card'>")
    html.append("<h2>Identidades verificadas</h2>")
    html.append(f"<div class='card-main'>{identity_count}</div>")
    html.append("<div class='card-sub'>CUIL + DNI ya confirmados</div>")
    html.append("</div>")

    html.append("<div class='card'>")
    html.append("<h2>Último job de cola</h2>")
    if jobs:
        last = jobs[0]
        html.append(f"<div class='card-main mono'>{esc_html(last['period_label'] or '')}</div>")
        html.append(f"<div class='card-sub'>estado: {esc_html(last['status'] or '')}, enviados: {last['total_sent']}</div>")
    else:
        html.append("<div class='card-main'>—</div>")
        html.append("<div class='card-sub'>Aún no se registran envíos en cola.</div>")
    html.append("</div>")
    html.append("</div>")  # grid-summary

    # SECCIÓN: Cola de envíos (PDF requerido siempre)
    html.append("<div class='section'>")
    html.append("<div class='section-header'>")
    html.append("<div class='section-title'>Cola de envíos masivos</div>")
    html.append("<div class='section-sub'>Creá jobs en cola y revisá su estado. Siempre requiere PDF existente.</div>")
    html.append("</div>")

    html.append("<div class='two-cols'>")

    # Columna izquierda: formulario
    html.append("<div>")
    html.append("<div class='small'>Este formulario usa la misma lógica que <code>/admin/send_template_queue_start</code>.</div>")
    html.append("<form method='post' action='/admin/send_template_queue_start'>")
    if token:
        html.append(f"<input type='hidden' name='token' value='{esc_html(token)}'>")
    html.append("<label>Período (mm-aaaa o mm/aaaa)<br>")
    html.append("<input type='text' name='period' placeholder='12-2025'></label>")
    html.append("<label>Límite de envíos (0 = todos)<br>")
    html.append("<input type='number' name='limit' min='0' value='0'></label>")
    # Forzamos require_pdf=true
    html.append("<input type='hidden' name='require_pdf' value='true'>")
    html.append("<button type='submit' class='btn-primary'>Encolar envío masivo</button>")
    html.append("</form>")
    html.append("<p class='small'>Luego podés consultar el progreso con <code>/admin/send_template_queue_status/&lt;job_id&gt;</code> o desde los jobs listados a la derecha.</p>")
    html.append("</div>")

    # Columna derecha: tabla de jobs
    html.append("<div>")
    html.append("<div class='section-sub'>Últimos 10 jobs</div>")
    html.append("<table>")
    html.append("<tr><th>Job ID</th><th>Período</th><th>Estado</th>"
                "<th>Encolados</th><th>Enviados</th><th>Fallidos</th><th>Creado</th></tr>")
    for j in jobs:
        status = (j["status"] or "").upper()
        cls = "pending"
        if status == "RUNNING":
            cls = "running"
        elif status == "DONE":
            cls = "done"
        elif status == "STOPPED":
            cls = "stopped"
        html.append("<tr>")
        html.append(f"<td class='mono'>{esc_html(j['job_id'][:8])}…</td>")
        html.append(f"<td class='mono'>{esc_html(j['period_label'] or '')}</td>")
        html.append(f"<td><span class='badge-status {cls}'>{esc_html(status)}</span></td>")
        html.append(f"<td>{j['total_enqueued']}</td>")
        html.append(f"<td>{j['total_sent']}</td>")
        html.append(f"<td>{j['total_failed']}</td>")
        html.append(f"<td class='mono'>{esc_html(fmt_ts(j['created_at']))}</td>")
        html.append("</tr>")
    if not jobs:
        html.append("<tr><td colspan='7' class='small'>No hay jobs todavía.</td></tr>")
    html.append("</table>")
    html.append("</div>")  # columna derecha

    html.append("</div>")  # two-cols
    html.append("</div>")  # section cola

    # SECCIÓN: Reportes
    html.append("<div class='section'>")
    html.append("<div class='section-header'>")
    html.append("<div class='section-title'>Reportes</div>")
    html.append("<div class='section-sub'>Descargá los datos en Excel / CSV.</div>")
    html.append("</div>")

    if token:
        html.append("<div class='links-inline'>")
        html.append(
            f"<a href='/admin/report_recibos.xlsx?token={esc_html(token)}' target='_blank'>📄 Descargar reporte de recibos (Excel)</a>"
        )
        html.append(
            f"<a href='/admin/report_identity_verification.csv?token={esc_html(token)}' target='_blank'>🧩 Identidades verificadas (CSV)</a>"
        )
        html.append("</div>")
    else:
        html.append("<p class='small'>Agregá <code>?token=TU_TOKEN</code> a la URL para habilitar los links directos de descarga.</p>")
    html.append("</div>")

    # SECCIÓN: Verificación manual
    html.append("<div class='section'>")
    html.append("<div class='section-header'>")
    html.append("<div class='section-title'>Verificación manual de identidad</div>")
    html.append("<div class='section-sub'>Marcá un CUIL + DNI como verificado sin pasar por el chat.</div>")
    html.append("</div>")

    html.append("<form method='post' action='/admin/verify_person'>")
    if token:
        html.append(f"<input type='hidden' name='token' value='{esc_html(token)}'>")
    html.append("<label>CUIL (archivo_norm)<br>"
                "<input type='text' name='archivo_norm' placeholder='20-XXXXXXXX-X'></label>")
    html.append("<label>DNI<br>"
                "<input type='text' name='dni' placeholder='solo números'></label>")
    html.append("<button type='submit' class='btn-primary'>Marcar como verificado (manual)</button>")
    html.append("</form>")
    html.append("<p class='small'>El sistema buscará el número de WhatsApp en el Excel de envíos y guardará la identidad en la tabla <code>identity_verification</code>.</p>")
    html.append("</div>")

    # SECCIÓN: Identidades verificadas (tabla + búsqueda + bulk + enviar)
    html.append("<div class='section'>")
    html.append("<div class='section-header'>")
    html.append("<div class='section-title'>Identidades verificadas</div>")
    html.append("<div class='section-sub'>Listado de CUIL + DNI con WhatsApp confirmado.</div>")
    html.append("</div>")

    # Un solo formulario que contiene checkboxes + acciones
    html.append("<form method='post' action='/admin/identity_verification/bulk' id='identityForm'>")
    if token:
        html.append(f"<input type='hidden' name='token' value='{esc_html(token)}'>")
    # este hidden lo usará el JS cuando quieras enviar plantilla
    html.append("<input type='hidden' name='period' id='identitySendPeriodHidden' value=''>")

    # Buscador en vivo
    html.append("<label>Buscar<br>")
    html.append("<input type='search' id='identityFilter' placeholder='Filtrar por nombre, CUIL, DNI, WhatsApp…'></label>")

    # Acciones sobre seleccionados
    html.append("<div class='checkbox-row' style='justify-content: space-between; margin-top: 10px;'>")
    html.append("<span class='small'>Seleccioná con el check de la izquierda y aplicá acciones masivas.</span>")
    # Botón borrar
    html.append("<button type='submit' class='btn-danger' name='bulk_action' value='delete' onclick='return confirm(\"¿Eliminar identidades seleccionadas?\");'>🗑️ Eliminar seleccionados</button>")
    html.append("</div>")

    # Botón enviar + input período
    html.append("<div class='checkbox-row' style='justify-content: flex-end; margin-top: 6px;'>")
    html.append("<span class='small'>Período para enviar plantilla:&nbsp;</span>")
    html.append("<input type='text' id='identitySendPeriodInput' placeholder='mm/aaaa' style='width:90px;'>")
    html.append("<button type='button' class='btn-primary' style='margin-left:6px;' onclick='identitySendSelected();'>📩 Enviar plantilla a seleccionados</button>")
    html.append("</div>")

    # Tabla con scroll
    html.append("<div class='identities-table-wrapper'>")
    html.append("<table id='identityTable'>")
    html.append("<thead><tr>")
    html.append("<th><input type='checkbox' id='identitySelectAll'></th>")
    html.append("<th class='sortable' data-sort='name'>Nombre <span>⇵</span></th>")
    html.append("<th>CUIL</th>")
    html.append("<th class='sortable' data-sort='dni'>DNI <span>⇵</span></th>")
    html.append("<th>WhatsApp</th>")
    html.append("<th>Verificado</th>")
    html.append("<th>Origen</th>")
    html.append("</tr></thead>")
    html.append("<tbody>")

    for row in identity_rows:
        archivo_norm = row["archivo_norm"]
        dni = row["dni"]
        to_whatsapp = row["to_whatsapp"]
        verified_at_local = row["verified_at_local"]
        source = row["source"]
        nombre = cuil_to_name.get(archivo_norm, "")

        search_text = f"{nombre} {archivo_norm} {dni} {to_whatsapp}".lower()

        html.append(
            "<tr data-name='{name}' data-dni='{dni}' data-search='{search}'>".format(
                name=esc_html(nombre),
                dni=esc_html(dni),
                search=esc_html(search_text),
            )
        )
        html.append(
            "<td><input type='checkbox' name='ids' value='{cuil}'></td>".format(
                cuil=esc_html(archivo_norm)
            )
        )
        html.append(f"<td>{esc_html(nombre)}</td>")
        html.append(f"<td class='mono'>{esc_html(archivo_norm)}</td>")
        html.append(f"<td class='mono'>{esc_html(dni)}</td>")
        html.append(f"<td class='mono'>{esc_html(to_whatsapp)}</td>")
        html.append(f"<td class='mono'>{esc_html(verified_at_local or '')}</td>")
        html.append(f"<td class='mono'>{esc_html(source or '')}</td>")
        html.append("</tr>")

    if not identity_rows:
        html.append("<tr><td colspan='7' class='small'>Todavía no hay identidades verificadas.</td></tr>")

    html.append("</tbody></table>")
    html.append("</div>")  # identities-table-wrapper
    html.append("</form>")  # identityForm

    # SECCIÓN: Subir Excel/CSV de identidades
    html.append("<div style='margin-top: 12px;'>")
    html.append("<div class='section-sub'>Carga masiva de identidades verificadas (CUIL + DNI + WhatsApp).</div>")
    html.append("<form method='post' action='/admin/identity_verification/upload' enctype='multipart/form-data'>")
    if token:
        html.append(f"<input type='hidden' name='token' value='{esc_html(token)}'>")
    html.append("<label>Archivo (CSV o Excel)<br>")
    html.append("<input type='file' name='file' accept='.csv,.xlsx,.xls' style='margin-top:6px;'></label><br>")
    html.append("<button type='submit' class='btn-primary'>Subir identidades</button>")
    if token:
        html.append(
            f" <span class='small' style='margin-left:8px;'>O descargá un <a href='/admin/identity_template.csv?token={esc_html(token)}' target='_blank'>template CSV de ejemplo</a>.</span>"
        )
    else:
        html.append(
            " <span class='small'>Agregá <code>?token=TU_TOKEN</code> a la URL para habilitar el link de template.</span>"
        )
    html.append("</form>")
    html.append("</div>")

    html.append("</div>")  # sección identidades

    # SECCIÓN: Preview del Excel de envíos
    html.append("<div class='section'>")
    html.append("<div class='section-header'>")
    html.append("<div class='section-title'>Preview del Excel de envíos</div>")
    html.append("<div class='section-sub'>Primeras 10 filas detectadas.</div>")
    html.append("</div>")
    if envios_sample:
        cols = list(envios_sample[0].keys())
        html.append("<table>")
        html.append("<tr>" + "".join(f"<th>{esc_html(c)}</th>" for c in cols) + "</tr>")
        for r in envios_sample:
            html.append("<tr>" + "".join(f"<td>{esc_html(r.get(c, ''))}</td>" for c in cols) + "</tr>")
        html.append("</table>")
    else:
        html.append("<p class='small'>No se pudo leer el archivo de envíos o está vacío.</p>")
    html.append("</div>")

    # Script para búsqueda, orden y enviar seleccionados
    html.append("""
    <script>
    (function() {
      const table = document.getElementById('identityTable');
      if (!table) return;
      const tbody = table.querySelector('tbody');
      const filterInput = document.getElementById('identityFilter');
      const selectAll = document.getElementById('identitySelectAll');
      const headers = table.querySelectorAll('th.sortable');

      // Búsqueda en vivo
      if (filterInput) {
        filterInput.addEventListener('input', function() {
          const q = this.value.toLowerCase().trim();
          const rows = tbody.querySelectorAll('tr');
          rows.forEach(row => {
            const text = (row.getAttribute('data-search') || '').toLowerCase();
            if (!q || text.indexOf(q) !== -1) {
              row.style.display = '';
            } else {
              row.style.display = 'none';
            }
          });
        });
      }

      // Seleccionar todo (solo filas visibles)
      if (selectAll) {
        selectAll.addEventListener('change', function() {
          const checked = this.checked;
          const rows = tbody.querySelectorAll('tr');
          rows.forEach(row => {
            if (row.style.display === 'none') return;
            const chk = row.querySelector("input[type='checkbox'][name='ids']");
            if (chk) chk.checked = checked;
          });
        });
      }

      // Ordenar por nombre / dni
      headers.forEach(th => {
        th.addEventListener('click', function() {
          const key = th.getAttribute('data-sort');
          if (!key) return;
          const current = th.getAttribute('data-dir') || 'asc';
          const newDir = current === 'asc' ? 'desc' : 'asc';
          th.setAttribute('data-dir', newDir);

          const rows = Array.from(tbody.querySelectorAll('tr'));
          rows.sort((a, b) => {
            const av = (a.getAttribute('data-' + key) || '').toLowerCase();
            const bv = (b.getAttribute('data-' + key) || '').toLowerCase();
            if (av < bv) return newDir === 'asc' ? -1 : 1;
            if (av > bv) return newDir === 'asc' ? 1 : -1;
            return 0;
          });
          rows.forEach(r => tbody.appendChild(r));
        });
      });

      // Función global para enviar plantilla a seleccionados
      window.identitySendSelected = function() {
        const form = document.getElementById('identityForm');
        if (!form) return;
        const periodInput = document.getElementById('identitySendPeriodInput');
        const periodHidden = document.getElementById('identitySendPeriodHidden');
        const period = (periodInput ? periodInput.value : '').trim();
        if (!period) {
          alert('Ingresá el período (mm/aaaa) para enviar la plantilla.');
          return;
        }
        const anyChecked = form.querySelector("input[name='ids']:checked");
        if (!anyChecked) {
          alert('Seleccioná al menos una identidad.');
          return;
        }
        periodHidden.value = period;
        form.action = '/admin/identity_verification/send';
        form.submit();
      };
    })();
    </script>
    """)

    html.append("</div>")  # layout
    html.append("</body></html>")

    return Response("".join(html), mimetype="text/html")


#=============================

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
        time.sleep(600)

t = threading.Thread(target=keep_alive, daemon=True)
t.start()

if __name__ == "__main__":
    # Para pruebas locales
    app.run(host="0.0.0.0", port=5000, debug=True)
