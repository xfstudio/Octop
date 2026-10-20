import { spawn } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import {
  createLoopbackProxy,
  readToken,
  supervise,
} from "./beatdesign-loopback-proxy.mjs";

const root = resolve(import.meta.dirname, "../prototypes/control-center");
const stateFile = resolve(
  process.env.RNA_AGENT_STATE || "/var/lib/rna/state.json",
);
const token = await readToken(
  process.env.RNA_AGENT_TOKEN_FILE || "/run/stack-secrets/rna-token",
);
await mkdir(dirname(stateFile), { recursive: true, mode: 0o700 });
// Run the unchanged upstream entry so leases, recovery and graceful drain remain
// upstream-owned. Its loopback and same-origin policy stays intact on port 4318.
const child = spawn(process.execPath, ["server.mjs"], {
  cwd: root,
  stdio: "inherit",
  env: { ...process.env, RNA_AGENT_PORT: "4318", RNA_AGENT_STATE: stateFile },
});
const gateway = createLoopbackProxy({
  port: 4317,
  upstreamPort: 4318,
  service: "rna",
  token,
  publicOrigin: process.env.RNA_PUBLIC_ORIGIN,
});
gateway.listen(4317, "0.0.0.0");
await supervise([child], [gateway]);
