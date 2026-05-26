#!/usr/bin/env python3
"""
Corregir schema de PostgreSQL para que coincida con SQLite
"""

import os
import sys

try:
    import psycopg2
except ImportError:
    print("❌ Falta psycopg2-binary")
    sys.exit(1)

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    print("❌ Falta DATABASE_URL")
    sys.exit(1)

print("=" * 60)
print("🔧 CORRIGIENDO SCHEMA DE POSTGRESQL")
print("=" * 60)

try:
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    
    print("\n🗑️  Borrando tablas con estructura incorrecta...")
    
    # Borrar tablas que tienen estructura diferente
    tables_to_drop = [
        'message_status',
        'pending_views', 
        'recibo_estado',
        'sent_pdfs',
        'receipt_request_events'
    ]
    
    for table in tables_to_drop:
        cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        print(f"   ✅ {table} borrada")
    
    conn.commit()
    
    print("\n📝 Creando tablas con schema correcto...")
    
    # Schema corregido basado en SQLite real
    schema_sql = """
    -- message_status (con columnas adicionales)
    CREATE TABLE message_status (
        id SERIAL PRIMARY KEY,
        message_sid TEXT UNIQUE NOT NULL,
        to_whatsapp TEXT,
        archivo_norm TEXT,
        period_label TEXT,
        nombre TEXT,
        kind TEXT,
        created_at BIGINT,
        last_status TEXT,
        last_status_at BIGINT,
        read_at BIGINT,
        delivered_at BIGINT,
        failed_at BIGINT,
        error_code TEXT,
        error_message TEXT,
        tenant TEXT,
        cuil TEXT,
        period TEXT
    );
    CREATE INDEX idx_msg_key ON message_status(tenant, cuil, period, kind);
    CREATE INDEX idx_msg_sid ON message_status(message_sid);
    
    -- pending_views (con columnas adicionales)
    CREATE TABLE pending_views (
        id SERIAL PRIMARY KEY,
        to_whatsapp TEXT NOT NULL,
        tenant TEXT NOT NULL,
        cuil TEXT NOT NULL,
        period TEXT NOT NULL,
        created_at BIGINT NOT NULL,
        step TEXT,
        dni_attempts INTEGER,
        origin TEXT,
        period_offset INTEGER DEFAULT 0
    );
    CREATE INDEX idx_pending_views_to ON pending_views(to_whatsapp);
    CREATE INDEX idx_pending_views_to_created ON pending_views(to_whatsapp, created_at);
    CREATE UNIQUE INDEX ux_pending_views_to_whatsapp ON pending_views(to_whatsapp);
    
    -- recibo_estado (sin to_whatsapp NOT NULL)
    CREATE TABLE recibo_estado (
        id SERIAL PRIMARY KEY,
        tenant TEXT NOT NULL,
        cuil TEXT NOT NULL,
        period TEXT NOT NULL,
        estado TEXT NOT NULL,
        updated_at BIGINT NOT NULL,
        to_whatsapp TEXT,
        observaciones TEXT,
        created_at BIGINT,
        UNIQUE(tenant, cuil, period)
    );
    CREATE INDEX idx_recibo_estado_key ON recibo_estado(tenant, cuil, period);
    CREATE INDEX idx_estado_key ON recibo_estado(tenant, cuil, period);
    
    -- sent_pdfs (con columnas adicionales)
    CREATE TABLE sent_pdfs (
        id SERIAL PRIMARY KEY,
        tenant TEXT NOT NULL,
        cuil TEXT NOT NULL,
        period TEXT NOT NULL,
        to_whatsapp TEXT NOT NULL,
        message_sid TEXT NOT NULL UNIQUE,
        created_at BIGINT NOT NULL,
        sign_sent_at BIGINT,
        delivered_at BIGINT,
        read_at BIGINT,
        failed_at BIGINT,
        error_code TEXT,
        error_message TEXT,
        status TEXT,
        origin TEXT
    );
    CREATE INDEX idx_sentpdfs_sid ON sent_pdfs(message_sid);
    CREATE INDEX idx_sentpdfs_key ON sent_pdfs(tenant, cuil, period);
    
    -- receipt_request_events (con requested_at)
    CREATE TABLE receipt_request_events (
        id SERIAL PRIMARY KEY,
        tenant TEXT,
        cuil TEXT,
        period TEXT,
        to_whatsapp TEXT,
        requested_at BIGINT NOT NULL,
        source TEXT,
        result TEXT,
        message_sid TEXT,
        created_at BIGINT,
        origin TEXT,
        whatsapp TEXT
    );
    CREATE INDEX idx_rre_key ON receipt_request_events(tenant, cuil, period, to_whatsapp);
    CREATE INDEX idx_rre_created ON receipt_request_events(created_at);
    """
    
    cur.execute(schema_sql)
    conn.commit()
    
    print("   ✅ Tablas recreadas con schema correcto")
    
    # Verificar
    print("\n📋 Tablas actuales:")
    cur.execute("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public' 
        ORDER BY table_name
    """)
    
    tables = cur.fetchall()
    for table in tables:
        print(f"   • {table[0]}")
    
    cur.close()
    conn.close()
    
    print("\n" + "=" * 60)
    print("✅ SCHEMA CORREGIDO")
    print("=" * 60)
    print("\nAhora ejecutá: python migrate_simple.py")
    
except Exception as e:
    print(f"\n❌ ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
