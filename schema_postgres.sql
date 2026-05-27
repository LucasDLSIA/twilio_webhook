-- =============================================================
-- Schema PostgreSQL para twilio_webhook
-- Correr esto UNA VEZ contra la DB de PG antes de levantar la app.
-- Equivalente a init_db() (rama SQLite) traducido a PG idiomático.
-- =============================================================

-- ------------------------------------------------------------
-- pending_views
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_views (
    id SERIAL PRIMARY KEY,
    to_whatsapp TEXT NOT NULL,
    tenant TEXT NOT NULL,
    cuil TEXT NOT NULL,
    period TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    step TEXT DEFAULT 'READY',
    dni_attempts INTEGER DEFAULT 0,
    origin TEXT,
    period_offset INTEGER DEFAULT 0,
    UNIQUE(to_whatsapp, tenant, cuil, period)
);

-- Index único usado por ON CONFLICT(to_whatsapp) en add_pending_view
CREATE UNIQUE INDEX IF NOT EXISTS ux_pending_views_to_whatsapp
    ON pending_views(to_whatsapp);

-- ------------------------------------------------------------
-- recibo_estado
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recibo_estado (
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

-- ------------------------------------------------------------
-- message_status
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS message_status (
    id SERIAL PRIMARY KEY,
    message_sid TEXT UNIQUE NOT NULL,
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

-- ------------------------------------------------------------
-- template_send_queue
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS template_send_queue (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    period TEXT NOT NULL,
    to_whatsapp TEXT NOT NULL,
    cuil TEXT NOT NULL,
    nombre TEXT,
    require_pdf BOOLEAN DEFAULT TRUE,
    status TEXT DEFAULT 'PENDING',
    error TEXT,
    created_at BIGINT NOT NULL,
    updated_at BIGINT,
    sent_sid TEXT,
    sent_at BIGINT,
    UNIQUE(tenant, period, to_whatsapp, cuil)
);

-- ------------------------------------------------------------
-- sent_pdfs
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sent_pdfs (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    cuil TEXT NOT NULL,
    period TEXT NOT NULL,
    to_whatsapp TEXT NOT NULL,
    message_sid TEXT NOT NULL UNIQUE,
    created_at BIGINT NOT NULL,
    sign_sent_at BIGINT,
    origin TEXT,
    delivered_at BIGINT,
    read_at BIGINT,
    failed_at BIGINT,
    error_code TEXT,
    error_message TEXT,
    status TEXT
);

-- ------------------------------------------------------------
-- sent_templates (referenciada por is_template_sid; no estaba en init_db SQLite)
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

-- ------------------------------------------------------------
-- verifications
-- ------------------------------------------------------------
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

-- ------------------------------------------------------------
-- receipt_requests
-- ------------------------------------------------------------
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

-- ------------------------------------------------------------
-- receipt_request_events
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS receipt_request_events (
    id SERIAL PRIMARY KEY,
    tenant TEXT,
    cuil TEXT,
    period TEXT,
    to_whatsapp TEXT,
    whatsapp TEXT,           -- alias legacy
    source TEXT,
    result TEXT,
    message_sid TEXT,
    created_at BIGINT,
    requested_at BIGINT,
    origin TEXT
);

-- ------------------------------------------------------------
-- terms_accepted
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS terms_accepted (
    whatsapp TEXT PRIMARY KEY,
    accepted_at BIGINT NOT NULL,
    ip_address TEXT,
    user_agent TEXT
);

-- ------------------------------------------------------------
-- pending_terms
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_terms (
    whatsapp TEXT PRIMARY KEY,
    tenant TEXT,
    cuil TEXT,
    period TEXT,
    origin TEXT DEFAULT 'INITIAL',
    created_at BIGINT
);

-- ------------------------------------------------------------
-- inbound_dedup
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inbound_dedup (
    message_sid TEXT PRIMARY KEY,
    created_at BIGINT
);

-- ------------------------------------------------------------
-- multi_tenant_selection
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS multi_tenant_selection (
    id SERIAL PRIMARY KEY,
    whatsapp TEXT NOT NULL UNIQUE,
    tenants_json TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL
);

-- ------------------------------------------------------------
-- client_users
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS client_users (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT,
    email TEXT,
    role TEXT DEFAULT 'admin',
    active BOOLEAN DEFAULT TRUE,
    must_change_password BOOLEAN DEFAULT TRUE,
    created_at BIGINT NOT NULL,
    last_login BIGINT,
    created_by TEXT,
    UNIQUE(tenant, username)
);

-- ------------------------------------------------------------
-- password_reset_tokens
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    token TEXT NOT NULL,
    expires_at BIGINT NOT NULL,
    used BOOLEAN DEFAULT FALSE,
    created_at BIGINT NOT NULL,
    UNIQUE(token),
    FOREIGN KEY(user_id) REFERENCES client_users(id)
);

-- ------------------------------------------------------------
-- client_audit_log
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS client_audit_log (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    tenant TEXT,
    action TEXT,
    details TEXT,
    ip_address TEXT,
    created_at BIGINT NOT NULL
);

-- =============================================================
-- ÍNDICES
-- =============================================================
CREATE INDEX IF NOT EXISTS idx_pending_to_created
    ON pending_views(to_whatsapp, created_at);
CREATE INDEX IF NOT EXISTS idx_estado_key
    ON recibo_estado(tenant, cuil, period);
CREATE INDEX IF NOT EXISTS idx_msg_key
    ON message_status(tenant, cuil, period, kind);
CREATE INDEX IF NOT EXISTS idx_msg_sid
    ON message_status(message_sid);
CREATE INDEX IF NOT EXISTS idx_sentpdfs_sid
    ON sent_pdfs(message_sid);
CREATE INDEX IF NOT EXISTS idx_verif_tenant_cuil
    ON verifications(tenant, cuil);
CREATE INDEX IF NOT EXISTS idx_verif_tenant_wa
    ON verifications(tenant, to_whatsapp);
CREATE INDEX IF NOT EXISTS idx_rr_key
    ON receipt_requests(tenant, cuil, period, to_whatsapp);
CREATE INDEX IF NOT EXISTS idx_rre_key
    ON receipt_request_events(tenant, cuil, period, to_whatsapp, created_at);
CREATE INDEX IF NOT EXISTS idx_multi_tenant_expires
    ON multi_tenant_selection(expires_at);
CREATE INDEX IF NOT EXISTS idx_ts_queue_pending
    ON template_send_queue(status, tenant, period, created_at);
