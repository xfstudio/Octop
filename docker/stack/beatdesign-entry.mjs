import { spawn } from "node:child_process";
import { mkdir, readFile, readdir } from "node:fs/promises";
import { resolve } from "node:path";
import { createClient } from "@libsql/client";
import {
  createLoopbackProxy,
  readToken,
  supervise,
} from "./beatdesign-loopback-proxy.mjs";

const root = resolve(import.meta.dirname, "..");
const data = resolve(process.env.BEATDESIGN_DATA_DIR || "/var/lib/beatdesign");
const token = await readToken(
  process.env.BEATDESIGN_TOKEN_FILE || "/run/stack-secrets/beatdesign-token",
);
await mkdir(data, { recursive: true, mode: 0o700 });

// Preserve the migration ledger used by upstream's WorkBuddy runtime launcher.
// Starting an empty workspace creates schema only, never projects or generations.
const client = createClient({ url: `file:${resolve(data, "local.db")}` });
try {
  await client.execute("PRAGMA foreign_keys = ON");
  await client.execute(
    "CREATE TABLE IF NOT EXISTS __beatdesign_runtime_migrations (name TEXT PRIMARY KEY NOT NULL, applied_at INTEGER NOT NULL)",
  );
  const applied = new Set(
    (
      await client.execute("SELECT name FROM __beatdesign_runtime_migrations")
    ).rows.map((row) => String(row.name)),
  );
  const migrations = resolve(root, "drizzle/sqlite");
  for (const name of (await readdir(migrations))
    .filter((value) => /^\d+_.+\.sql$/.test(value))
    .sort()) {
    if (applied.has(name)) continue;
    await client.executeMultiple(
      (await readFile(resolve(migrations, name), "utf8")).replaceAll(
        "--> statement-breakpoint",
        "",
      ),
    );
    await client.execute({
      sql: "INSERT INTO __beatdesign_runtime_migrations (name, applied_at) VALUES (?, ?)",
      args: [name, Date.now()],
    });
  }
} finally {
  client.close();
}

const env = { ...process.env, BEATDESIGN_DATA_DIR: data };
const children = [
  spawn(process.execPath, [".output/server/index.mjs"], {
    cwd: root,
    stdio: "inherit",
    env: {
      ...env,
      HOST: "127.0.0.1",
      PORT: "3021",
      NITRO_HOST: "127.0.0.1",
      NITRO_PORT: "3021",
    },
  }),
  spawn(process.execPath, ["mcp/server.mjs", "--http"], {
    cwd: root,
    stdio: "inherit",
    env: {
      ...env,
      BEATDESIGN_MCP_HTTP_HOST: "127.0.0.1",
      BEATDESIGN_MCP_HTTP_PORT: "3032",
      BEATDESIGN_MCP_TOKEN: token,
    },
  }),
];
const web = createLoopbackProxy({
  port: 3020,
  upstreamPort: 3021,
  service: "beatdesign",
  token,
  publicOrigin: process.env.BEATDESIGN_PUBLIC_ORIGIN,
  upstreamHealthPath: "/api/ping",
});
const mcp = createLoopbackProxy({
  port: 3031,
  upstreamPort: 3032,
  service: "beatdesign",
  token,
  basic: false,
  upstreamHealthPath: "/mcp",
  healthStatuses: [200, 400, 405, 406],
});
web.listen(3020, "0.0.0.0");
mcp.listen(3031, "0.0.0.0");
await supervise(children, [web, mcp]);
