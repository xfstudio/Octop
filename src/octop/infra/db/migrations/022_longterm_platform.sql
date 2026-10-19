-- Offline-first tenant spaces and durable bounded agent tasks.
CREATE TABLE IF NOT EXISTS bm_tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bm_memberships (
    tenant_id TEXT NOT NULL REFERENCES bm_tenants(tenant_id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    PRIMARY KEY (tenant_id, user_id)
);
CREATE TABLE IF NOT EXISTS bm_spaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    space_id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL REFERENCES bm_tenants(tenant_id),
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '{}',
    device_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (tenant_id, space_id)
);
CREATE TABLE IF NOT EXISTS bm_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL REFERENCES bm_tenants(tenant_id),
    space_id TEXT NOT NULL,
    creator_user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    document_json TEXT NOT NULL,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    lease_owner TEXT,
    lease_until INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (tenant_id, task_id),
    FOREIGN KEY (tenant_id, space_id) REFERENCES bm_spaces(tenant_id, space_id)
);
CREATE INDEX IF NOT EXISTS idx_bm_tasks_dispatch ON bm_tasks(status, lease_until);
CREATE TABLE IF NOT EXISTS bm_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (tenant_id, task_id, sequence),
    FOREIGN KEY (tenant_id, task_id) REFERENCES bm_tasks(tenant_id, task_id)
);
CREATE TABLE IF NOT EXISTS bm_commands (
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    command_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, command_key),
    FOREIGN KEY (tenant_id, task_id) REFERENCES bm_tasks(tenant_id, task_id)
);

UPDATE _schema_version SET version = 22;
