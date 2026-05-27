-- =============================================================
-- Arregla:
--   1) Tablas que faltan en PG (sent_templates, etc.)
--   2) Sequences desfasadas (UniqueViolation duplicate key id=N)
--
-- Es idempotente: lo podés correr varias veces sin romper nada.
-- =============================================================

-- ------------------------------------------------------------
-- 1) Crear tablas que pueden faltar
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sent_templates (
    id SERIAL PRIMARY KEY,
    message_sid TEXT NOT NULL UNIQUE,
    tenant TEXT,
    cuil TEXT,
    period TEXT,
    to_whatsapp TEXT,
    nombre TEXT,
    created_at BIGINT
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    token TEXT NOT NULL,
    expires_at BIGINT NOT NULL,
    used BOOLEAN DEFAULT FALSE,
    created_at BIGINT NOT NULL,
    UNIQUE(token)
);

CREATE TABLE IF NOT EXISTS client_audit_log (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    tenant TEXT,
    action TEXT,
    details TEXT,
    ip_address TEXT,
    created_at BIGINT NOT NULL
);

-- ------------------------------------------------------------
-- 2) Resincronizar TODAS las sequences a MAX(id)
--    (esto arregla "duplicate key value violates unique constraint ... id=N")
-- ------------------------------------------------------------

DO $$
DECLARE
    r RECORD;
    seq_name TEXT;
    max_id BIGINT;
BEGIN
    FOR r IN
        SELECT c.relname AS table_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind = 'r'
          AND EXISTS (
            SELECT 1
            FROM pg_attribute a
            JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
            WHERE a.attrelid = c.oid
              AND a.attname = 'id'
              AND pg_get_expr(ad.adbin, ad.adrelid) LIKE 'nextval%'
          )
    LOOP
        seq_name := pg_get_serial_sequence('public.' || r.table_name, 'id');
        EXECUTE format('SELECT COALESCE(MAX(id), 0) FROM %I', r.table_name) INTO max_id;
        IF max_id > 0 THEN
            PERFORM setval(seq_name, max_id, true);
            RAISE NOTICE 'Reset sequence % to % (next will be %)', seq_name, max_id, max_id + 1;
        ELSE
            PERFORM setval(seq_name, 1, false);
            RAISE NOTICE 'Sequence % left at 1 (table empty)', seq_name;
        END IF;
    END LOOP;
END $$;
