#!/usr/bin/env python3
"""
Script de migración SQLite -> PostgreSQL
Ejecutar DIRECTAMENTE en Render Shell
"""

import os
import sys

# Verificar que existan las dependencias
try:
    import sqlite3
    import psycopg2
    from psycopg2.extras import execute_batch
except ImportError as e:
    print(f"❌ Falta instalar: {e}")
    print("Ejecutá: pip install psycopg2-binary")
    sys.exit(1)

# Configuración
SQLITE_PATH = "/data/app.db"
POSTGRES_URL = os.environ.get("DATABASE_URL")

if not POSTGRES_URL:
    print("❌ ERROR: Falta variable DATABASE_URL")
    sys.exit(1)

print("=" * 60)
print("🚀 MIGRACIÓN SQLite → PostgreSQL")
print("=" * 60)

# Conectar a SQLite
print("\n📂 Conectando a SQLite...")
if not os.path.exists(SQLITE_PATH):
    print(f"❌ No existe {SQLITE_PATH}")
    sys.exit(1)

sqlite_conn = sqlite3.connect(SQLITE_PATH)
sqlite_conn.row_factory = sqlite3.Row
print("   ✅ SQLite OK")

# Conectar a PostgreSQL
print("\n📂 Conectando a PostgreSQL...")
try:
    pg_conn = psycopg2.connect(POSTGRES_URL)
    print("   ✅ PostgreSQL OK")
except Exception as e:
    print(f"❌ Error: {e}")
    sys.exit(1)

# Listar tablas en SQLite
print("\n📋 Tablas en SQLite:")
cur = sqlite_conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [row[0] for row in cur.fetchall()]

for table in tables:
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    count = cur.fetchone()[0]
    print(f"   • {table}: {count} filas")

print(f"\n   Total: {len(tables)} tablas")

# Confirmar
print("\n" + "=" * 60)
respuesta = input("¿Migrar estas tablas a PostgreSQL? (si/no): ")
if respuesta.lower() != "si":
    print("❌ Cancelado")
    sys.exit(0)

# Migrar cada tabla
print("\n🔄 Migrando datos...")
total_rows = 0

for table in tables:
    print(f"\n📦 {table}...")
    
    # Leer de SQLite
    cur_sqlite = sqlite_conn.cursor()
    cur_sqlite.execute(f"SELECT * FROM {table}")
    rows = cur_sqlite.fetchall()
    
    if not rows:
        print(f"   ⚠️  Vacía, skip")
        continue
    
    # Obtener columnas
    columns = [desc[0] for desc in cur_sqlite.description]
    
    # Preparar INSERT
    cols_str = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    
    # Convertir rows a tuplas
    data = [tuple(row) for row in rows]
    
    # Insertar en PostgreSQL
    cur_pg = pg_conn.cursor()
    
    try:
        insert_sql = f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
        execute_batch(cur_pg, insert_sql, data, page_size=100)
        pg_conn.commit()
        
        print(f"   ✅ {len(data)} filas migradas")
        total_rows += len(data)
        
    except Exception as e:
        pg_conn.rollback()
        print(f"   ❌ ERROR: {e}")
        print(f"   ⚠️  Continuando con siguiente tabla...")

# Cerrar conexiones
sqlite_conn.close()
pg_conn.close()

print("\n" + "=" * 60)
print(f"✅ MIGRACIÓN COMPLETADA")
print(f"   Total migrado: {total_rows} filas")
print("=" * 60)
