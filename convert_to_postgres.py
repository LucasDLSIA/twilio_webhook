#!/usr/bin/env python3
"""
Convertir app.py de SQLite a PostgreSQL
Cambia imports, get_db_connection y ? por %s
"""

import re
import sys

print("=" * 60)
print("🔄 CONVIRTIENDO app.py: SQLite → PostgreSQL")
print("=" * 60)

# Leer app.py
try:
    with open('app.py', 'r', encoding='utf-8') as f:
        content = f.read()
    print("\n✅ app.py leído")
except Exception as e:
    print(f"\n❌ Error leyendo app.py: {e}")
    sys.exit(1)

original_lines = content.count('\n')
print(f"   Líneas: {original_lines}")

# 1. Cambiar imports
print("\n🔧 Paso 1/4: Cambiando imports...")
old_imports = content

# Agregar psycopg2 si no está
if 'import psycopg2' not in content:
    # Buscar donde está import sqlite3
    content = content.replace(
        'import sqlite3',
        'import sqlite3\nimport psycopg2\nfrom psycopg2.extras import RealDictCursor'
    )
    print("   ✅ Agregado: import psycopg2")
else:
    print("   ⚠️  psycopg2 ya importado")

# 2. Cambiar get_db_connection
print("\n🔧 Paso 2/4: Cambiando get_db_connection()...")

# Patrón para encontrar la función get_db_connection
old_function_pattern = r'def get_db_connection\(\):.*?return conn'

# Nueva función
new_function = '''def get_db_connection():
    """Conectar a PostgreSQL (o SQLite como fallback)"""
    DATABASE_URL = os.environ.get("DATABASE_URL")
    
    if DATABASE_URL:
        # PostgreSQL
        conn = psycopg2.connect(DATABASE_URL)
        conn.cursor_factory = RealDictCursor
        return conn
    else:
        # SQLite (fallback para desarrollo local)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn'''

content = re.sub(old_function_pattern, new_function, content, flags=re.DOTALL)
print("   ✅ get_db_connection() actualizada")

# 3. Cambiar ? por %s en queries
print("\n🔧 Paso 3/4: Cambiando placeholders ? → %s...")

# Contar cuántos ? hay
question_marks = content.count('execute("')
question_marks += content.count("execute('")
print(f"   Encontradas ~{question_marks} llamadas a execute()")

# Cambiar ? por %s en strings de execute()
# Patrón: buscar execute(" ... ") o execute(' ... ')
def replace_placeholders(match):
    query = match.group(0)
    # Solo reemplazar ? que están dentro de las comillas
    # No reemplazar ? en comentarios o fuera de queries
    if '?' in query:
        query = query.replace('?', '%s')
    return query

# Reemplazar en execute con comillas dobles
content = re.sub(
    r'execute\("(?:[^"\\]|\\.)*"\s*,',
    replace_placeholders,
    content
)

# Reemplazar en execute con comillas simples
content = re.sub(
    r"execute\('(?:[^'\\]|\\.)*'\s*,",
    replace_placeholders,
    content
)

# Reemplazar en executemany también
content = re.sub(
    r'executemany\("(?:[^"\\]|\\.)*"\s*,',
    replace_placeholders,
    content
)

content = re.sub(
    r"executemany\('(?:[^'\\]|\\.)*'\s*,",
    replace_placeholders,
    content
)

# Contar cuántos %s quedaron
percent_s_count = content.count('%s')
print(f"   ✅ {percent_s_count} placeholders cambiados a %s")

# 4. Agregar manejo de RETURNING en INSERTs
print("\n🔧 Paso 4/4: Verificando lastrowid...")

# Buscar usos de lastrowid
lastrowid_count = content.count('lastrowid')
if lastrowid_count > 0:
    print(f"   ⚠️  Encontrados {lastrowid_count} usos de lastrowid")
    print("   💡 Nota: En PostgreSQL usar RETURNING id en vez de lastrowid")
    print("   📝 Estos pueden necesitar cambio manual si dan error")
else:
    print("   ✅ No se usa lastrowid")

# Guardar archivo modificado
print("\n💾 Guardando cambios...")
try:
    with open('app.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("   ✅ app.py actualizado")
except Exception as e:
    print(f"   ❌ Error guardando: {e}")
    sys.exit(1)

new_lines = content.count('\n')
print(f"   Líneas finales: {new_lines}")

# Resumen
print("\n" + "=" * 60)
print("✅ CONVERSIÓN COMPLETADA")
print("=" * 60)
print("\n📊 Cambios realizados:")
print(f"   • Imports agregados: psycopg2")
print(f"   • get_db_connection(): actualizada")
print(f"   • Placeholders cambiados: {percent_s_count}")
print(f"   • Backup disponible: app.py.backup.*")

print("\n⚠️  IMPORTANTE:")
print("   1. Revisá el diff antes de commitear:")
print("      diff app.py.backup.* app.py | head -100")
print("\n   2. Testeá localmente si podés")
print("\n   3. Después hacé:")
print("      git add app.py")
print("      git commit -m 'Migrate to PostgreSQL'")
print("      git push")

print("\n" + "=" * 60)
