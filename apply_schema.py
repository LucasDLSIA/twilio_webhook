#!/usr/bin/env python3
"""
Aplicar schema de PostgreSQL
Ejecutar en Render Shell
"""

import os
import sys

try:
    import psycopg2
except ImportError:
    print("❌ Falta psycopg2-binary")
    print("Ejecutá: pip install psycopg2-binary")
    sys.exit(1)

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    print("❌ Falta DATABASE_URL en variables de entorno")
    sys.exit(1)

# Schema SQL completo
SCHEMA_SQL = """
-- Crear tablas
CREATE TABLE IF NOT EXISTS pending_views (
    id SERIAL PRIMARY KEY,
    to_whatsapp TEXT NOT NULL,
    tenant TEXT NOT NULL,
    cuil TEXT NOT NULL,
    period TEXT NOT NULL,
    step TEXT DEFAULT 'READY',
    origin TEXT DEFAULT 'INITIAL',
    created_at BIGINT NOT NULL,
    updated_at BIGINT
);

CREATE TABLE IF NOT EXISTS recibo_estado (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    cuil TEXT NOT NULL,
    period TEXT NOT NULL,
    to_whatsapp TEXT NOT NULL,
    estado TEXT NOT NULL,
    observaciones TEXT,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    UNIQUE(tenant, cuil, period)
);

CREATE TABLE IF NOT EXISTS message_status (
    id SERIAL PRIMARY KEY,
    message_sid TEXT NOT NULL UNIQUE,
    to_whatsapp TEXT,
    tenant TEXT,
    cuil TEXT,
    period TEXT,
    nombre TEXT,
    kind TEXT,
    created_at BIGINT,
    last_status TEXT,
    last_status_at BIGINT,
    delivered_at BIGINT,
    read_at BIGINT,
    failed_at BIGINT,
    error_code TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS template_send_queue (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    period TEXT NOT NULL,
    to_whatsapp TEXT NOT NULL,
    cuil TEXT NOT NULL,
    nombre TEXT,
    require_pdf INTEGER DEFAULT 1,
    status TEXT DEFAULT 'PENDING',
    error TEXT,
    created_at BIGINT NOT NULL,
    updated_at BIGINT,
    sent_sid TEXT,
    sent_at BIGINT,
    UNIQUE(tenant, period, to_whatsapp, cuil)
);

CREATE TABLE IF NOT EXISTS sent_pdfs (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    cuil TEXT NOT NULL,
    period TEXT NOT NULL,
    to_whatsapp TEXT NOT NULL,
    message_sid TEXT NOT NULL UNIQUE,
    created_at BIGINT NOT NULL,
    sign_sent_at BIGINT,
    origin TEXT
);

CREATE TABLE IF NOT EXISTS verifications (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    cuil TEXT NOT NULL,
    to_whatsapp TEXT NOT NULL,
    nombre TEXT,
    dni_hash TEXT,
    dni_last4 TEXT,
    verified_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    UNIQUE(tenant, cuil, to_whatsapp)
);

CREATE TABLE IF NOT EXISTS receipt_requests (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    cuil TEXT NOT NULL,
    period TEXT NOT NULL,
    to_whatsapp TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    first_requested_at BIGINT,
    last_requested_at BIGINT,
    UNIQUE(tenant, cuil, period, to_whatsapp)
);

CREATE TABLE IF NOT EXISTS receipt_request_events (
    id SERIAL PRIMARY KEY,
    tenant TEXT,
    cuil TEXT,
    period TEXT,
    to_whatsapp TEXT,
    source TEXT,
    result TEXT,
    message_sid TEXT,
    created_at BIGINT,
    origin TEXT
);

CREATE TABLE IF NOT EXISTS terms_accepted (
    whatsapp TEXT PRIMARY KEY,
    accepted_at BIGINT NOT NULL,
    ip_address TEXT,
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS pending_terms (
    whatsapp TEXT PRIMARY KEY,
    tenant TEXT,
    cuil TEXT,
    period TEXT,
    origin TEXT DEFAULT 'INITIAL',
    created_at BIGINT
);

CREATE TABLE IF NOT EXISTS inbound_dedup (
    message_sid TEXT PRIMARY KEY,
    created_at BIGINT
);

CREATE TABLE IF NOT EXISTS multi_tenant_selection (
    id SERIAL PRIMARY KEY,
    whatsapp TEXT NOT NULL UNIQUE,
    tenants_json TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS client_users (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT,
    email TEXT,
    role TEXT DEFAULT 'admin',
    active INTEGER DEFAULT 1,
    must_change_password INTEGER DEFAULT 1,
    created_at BIGINT NOT NULL,
    last_login BIGINT,
    created_by TEXT,
    UNIQUE(tenant, username)
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES client_users(id) ON DELETE CASCADE,
    token TEXT NOT NULL UNIQUE,
    expires_at BIGINT NOT NULL,
    used INTEGER DEFAULT 0,
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS client_audit_log (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES client_users(id) ON DELETE SET NULL,
    tenant TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT,
    ip_address TEXT,
    created_at BIGINT NOT NULL
);

-- Crear índices
CREATE INDEX IF NOT EXISTS idx_pending_to_created ON pending_views(to_whatsapp, created_at);
CREATE INDEX IF NOT EXISTS idx_pending_tenant_cuil ON pending_views(tenant, cuil);
CREATE INDEX IF NOT EXISTS idx_estado_key ON recibo_estado(tenant, cuil, period);
CREATE INDEX IF NOT EXISTS idx_estado_tenant_period ON recibo_estado(tenant, period);
CREATE INDEX IF NOT EXISTS idx_msg_key ON message_status(tenant, cuil, period, kind);
CREATE INDEX IF NOT EXISTS idx_msg_sid ON message_status(message_sid);
CREATE INDEX IF NOT EXISTS idx_msg_to_whatsapp ON message_status(to_whatsapp);
CREATE INDEX IF NOT EXISTS idx_msg_tenant_period ON message_status(tenant, period);
CREATE INDEX IF NOT EXISTS idx_ts_queue_pending ON template_send_queue(status, tenant, period, created_at);
CREATE INDEX IF NOT EXISTS idx_ts_queue_tenant_period ON template_send_queue(tenant, period);
CREATE INDEX IF NOT EXISTS idx_sentpdfs_sid ON sent_pdfs(message_sid);
CREATE INDEX IF NOT EXISTS idx_sentpdfs_key ON sent_pdfs(tenant, cuil, period);
CREATE INDEX IF NOT EXISTS idx_verif_tenant_cuil ON verifications(tenant, cuil);
CREATE INDEX IF NOT EXISTS idx_verif_tenant_wa ON verifications(tenant, to_whatsapp);
CREATE INDEX IF NOT EXISTS idx_rr_key ON receipt_requests(tenant, cuil, period, to_whatsapp);
CREATE INDEX IF NOT EXISTS idx_rre_key ON receipt_request_events(tenant, cuil, period, to_whatsapp, created_at);
CREATE INDEX IF NOT EXISTS idx_rre_created ON receipt_request_events(created_at);
CREATE INDEX IF NOT EXISTS idx_inbound_created ON inbound_dedup(created_at);
CREATE INDEX IF NOT EXISTS idx_multi_tenant_expires ON multi_tenant_selection(expires_at);
CREATE INDEX IF NOT EXISTS idx_client_users_tenant ON client_users(tenant);
CREATE INDEX IF NOT EXISTS idx_client_users_email ON client_users(email);
CREATE INDEX IF NOT EXISTS idx_reset_token ON password_reset_tokens(token);
CREATE INDEX IF NOT EXISTS idx_reset_user ON password_reset_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_user ON client_audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON client_audit_log(tenant, created_at);
"""

print("=" * 60)
print("🚀 APLICANDO SCHEMA A POSTGRESQL")
print("=" * 60)

try:
    print("\n📡 Conectando a PostgreSQL...")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    print("   ✅ Conectado")
    
    print("\n📝 Ejecutando SQL...")
    cur.execute(SCHEMA_SQL)
    conn.commit()
    print("   ✅ Schema aplicado")
    
    # Verificar tablas creadas
    print("\n📋 Tablas creadas:")
    cur.execute("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public' 
        ORDER BY table_name
    """)
    
    tables = cur.fetchall()
    for table in tables:
        print(f"   • {table[0]}")
    
    print(f"\n   Total: {len(tables)} tablas")
    
    cur.close()
    conn.close()
    
    print("\n" + "=" * 60)
    print("✅ SCHEMA APLICADO EXITOSAMENTE")
    print("=" * 60)
    
except Exception as e:
    print(f"\n❌ ERROR: {e}")
    sys.exit(1)
