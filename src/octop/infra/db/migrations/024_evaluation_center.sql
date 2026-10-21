-- Private, versioned evaluations. Native attempts refer to the existing durable tasks.
CREATE TABLE IF NOT EXISTS bm_eval_documents (
    tenant_id TEXT NOT NULL REFERENCES bm_tenants(tenant_id),
    kind TEXT NOT NULL,
    document_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    creator_user_id INTEGER NOT NULL REFERENCES users(id),
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    document_json TEXT NOT NULL,
    lease_owner TEXT,
    lease_until INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, kind, document_id),
    FOREIGN KEY (tenant_id, space_id) REFERENCES bm_spaces(tenant_id, space_id)
);
CREATE INDEX IF NOT EXISTS idx_bm_eval_private ON bm_eval_documents(tenant_id,creator_user_id,space_id,kind,updated_at);
CREATE TABLE IF NOT EXISTS bm_eval_suite_versions (
    tenant_id TEXT NOT NULL,
    suite_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    document_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id,suite_id,version)
);
CREATE TABLE IF NOT EXISTS bm_eval_attempts (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    repeat_index INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    document_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id,run_id,attempt_id),
    UNIQUE (tenant_id,run_id,case_id,repeat_index)
);
CREATE TABLE IF NOT EXISTS bm_eval_commands (
    tenant_id TEXT NOT NULL,
    command_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    kind TEXT NOT NULL,
    document_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id,command_key)
);
UPDATE _schema_version SET version = 24;
