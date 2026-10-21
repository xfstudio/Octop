-- Private, durable component readback sessions and idempotent commands.
CREATE TABLE IF NOT EXISTS bm_operation_sessions (
    session_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    task_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('created','ready','committed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    document_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (tenant_id, space_id, owner_user_id, session_id),
    FOREIGN KEY (tenant_id, space_id) REFERENCES bm_spaces(tenant_id, space_id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES bm_tasks(tenant_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_bm_operation_sessions_owner ON bm_operation_sessions(tenant_id,space_id,owner_user_id,created_at);
CREATE TABLE IF NOT EXISTS bm_operation_commands (
    tenant_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    owner_user_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    command_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, space_id, owner_user_id, command_key),
    FOREIGN KEY (tenant_id,space_id,owner_user_id,session_id) REFERENCES bm_operation_sessions(tenant_id,space_id,owner_user_id,session_id)
);
ALTER TABLE bm_operation_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE bm_operation_sessions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS bm_operation_sessions_private_scope ON bm_operation_sessions;
CREATE POLICY bm_operation_sessions_private_scope ON bm_operation_sessions USING (tenant_id = current_setting('octop.tenant_id',true) AND space_id = current_setting('octop.space_id',true) AND owner_user_id::text = current_setting('octop.user_id',true)) WITH CHECK (tenant_id = current_setting('octop.tenant_id',true) AND space_id = current_setting('octop.space_id',true) AND owner_user_id::text = current_setting('octop.user_id',true));
ALTER TABLE bm_operation_commands ENABLE ROW LEVEL SECURITY;
ALTER TABLE bm_operation_commands FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS bm_operation_commands_private_scope ON bm_operation_commands;
CREATE POLICY bm_operation_commands_private_scope ON bm_operation_commands USING (tenant_id = current_setting('octop.tenant_id',true) AND space_id = current_setting('octop.space_id',true) AND owner_user_id::text = current_setting('octop.user_id',true)) WITH CHECK (tenant_id = current_setting('octop.tenant_id',true) AND space_id = current_setting('octop.space_id',true) AND owner_user_id::text = current_setting('octop.user_id',true));
UPDATE _schema_version SET version = 23;
