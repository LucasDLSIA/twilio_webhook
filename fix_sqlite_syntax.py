#!/usr/bin/env python3
"""
Fix remaining SQLite syntax in app.py
"""

import re

print("=" * 60)
print("🔧 ARREGLANDO SINTAXIS SQLite → PostgreSQL")
print("=" * 60)

# Leer app.py
with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

print("\n✅ app.py leído")

# Contador de cambios
changes = 0

# 1. Cambiar INSERT OR IGNORE por INSERT ... ON CONFLICT DO NOTHING
print("\n🔧 Cambiando INSERT OR IGNORE...")
old_count = content.count('INSERT OR IGNORE')
print(f"   Encontrados: {old_count} usos")

content = content.replace('INSERT OR IGNORE', 'INSERT')

# Agregar ON CONFLICT DO NOTHING donde corresponde
# Patrón: INSERT INTO tabla(...) VALUES (...)
# Necesitamos encontrar cada INSERT y agregar ON CONFLICT

# Para inbound_dedup
content = re.sub(
    r'(INSERT INTO inbound_dedup\([^)]+\)\s+VALUES\s*\([^)]+\))',
    r'\1 ON CONFLICT (message_sid) DO NOTHING',
    content
)
changes += 1
print("   ✅ inbound_dedup fixed")

# Para message_status
content = re.sub(
    r'(INSERT INTO message_status\s*\([^)]+\)\s*VALUES\s*\([^)]+\))(?!\s*ON CONFLICT)',
    r'\1 ON CONFLICT (message_sid) DO NOTHING',
    content
)
print("   ✅ message_status fixed")

# 2. Cambiar todos los ? restantes por %s
print("\n🔧 Cambiando placeholders ? → %s...")
question_marks_before = content.count('execute("')
question_marks_before += content.count("execute('")

# Función para reemplazar ? por %s solo dentro de queries
def replace_question_marks(match):
    query = match.group(0)
    if '?' in query:
        query = query.replace('?', '%s')
    return query

# Reemplazar en todas las variantes de execute
content = re.sub(
    r'execute\(["\'].*?["\'](?:\s*,|\s*\))',
    replace_question_marks,
    content,
    flags=re.DOTALL
)

percent_s_count = content.count('%s')
print(f"   ✅ Total placeholders %s: {percent_s_count}")

# 3. Guardar
print("\n💾 Guardando cambios...")
with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("   ✅ app.py actualizado")

print("\n" + "=" * 60)
print("✅ FIX COMPLETADO")
print("=" * 60)
print("\n📊 Cambios:")
print(f"   • INSERT OR IGNORE eliminados: {old_count}")
print(f"   • ON CONFLICT agregados: {changes}")
print(f"   • Placeholders totales %s: {percent_s_count}")

print("\n🚀 Siguiente paso:")
print("   git add app.py")
print("   git commit -m 'Fix remaining SQLite syntax'")
print("   git push")
