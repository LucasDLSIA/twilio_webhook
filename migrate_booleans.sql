-- =============================================================
-- Migración INTEGER -> BOOLEAN para las columnas que el código
-- nuevo trata como bool. Conserva los datos existentes
-- (0 -> false, 1 -> true).
--
-- Correr UNA VEZ contra la DB de PG.
-- =============================================================

-- template_send_queue.require_pdf
ALTER TABLE template_send_queue
    ALTER COLUMN require_pdf DROP DEFAULT,
    ALTER COLUMN require_pdf TYPE BOOLEAN
        USING (require_pdf::int <> 0),
    ALTER COLUMN require_pdf SET DEFAULT TRUE;

-- client_users.active
ALTER TABLE client_users
    ALTER COLUMN active DROP DEFAULT,
    ALTER COLUMN active TYPE BOOLEAN
        USING (active::int <> 0),
    ALTER COLUMN active SET DEFAULT TRUE;

-- client_users.must_change_password
ALTER TABLE client_users
    ALTER COLUMN must_change_password DROP DEFAULT,
    ALTER COLUMN must_change_password TYPE BOOLEAN
        USING (must_change_password::int <> 0),
    ALTER COLUMN must_change_password SET DEFAULT TRUE;

-- password_reset_tokens.used
ALTER TABLE password_reset_tokens
    ALTER COLUMN used DROP DEFAULT,
    ALTER COLUMN used TYPE BOOLEAN
        USING (used::int <> 0),
    ALTER COLUMN used SET DEFAULT FALSE;
