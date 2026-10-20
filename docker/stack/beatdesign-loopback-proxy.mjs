// Fixed same-container gateway shared by the BeatDesign and RNA launchers.
// No incoming URL, forwarding header or request body can select a proxy target.
import http from "node:http";
import { timingSafeEqual } from "node:crypto";
import { readFile } from "node:fs/promises";

export async function readToken(filename) {
  const token = (await readFile(filename, "utf8")).trim();
  if (!token || /[\r\n]/.test(token))
    throw new Error("A non-empty stack token file is required.");
  return token;
}

function same(value, expected) {
  const left = Buffer.from(value ?? "");
  const right = Buffer.from(expected);
  return left.length === right.length && timingSafeEqual(left, right);
}

export function createLoopbackProxy({
  port,
  upstreamPort,
  service,
  token,
  publicOrigin,
  basic = true,
  upstreamHealthPath = "/api/health",
  healthStatuses = [200],
}) {
  const hosts = new Set([
    `localhost:${port}`,
    `127.0.0.1:${port}`,
    `${service}:${port}`,
  ]);
  let configuredOrigin;
  if (publicOrigin) {
    const parsed = new URL(publicOrigin);
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      parsed.username ||
      parsed.password ||
      parsed.pathname !== "/" ||
      parsed.search ||
      parsed.hash
    ) {
      throw new Error(
        "Public origin must contain only an HTTP(S) scheme, host and port.",
      );
    }
    configuredOrigin = parsed.origin;
    hosts.add(parsed.host.toLowerCase());
  }
  const upstreamHost = `127.0.0.1:${upstreamPort}`;
  const reply = (response, status, value, extra = {}) => {
    response.writeHead(status, {
      "content-type": "application/json",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      ...extra,
    });
    response.end(JSON.stringify(value));
  };
  return http.createServer((request, response) => {
    const host = request.headers.host?.toLowerCase();
    const origin = request.headers.origin;
    const expectedOrigin =
      configuredOrigin && new URL(configuredOrigin).host.toLowerCase() === host
        ? configuredOrigin
        : `http://${host}`;
    if (
      !hosts.has(host) ||
      (origin && origin !== expectedOrigin) ||
      (request.headers["sec-fetch-site"] &&
        !["same-origin", "none"].includes(request.headers["sec-fetch-site"])) ||
      !request.url?.startsWith("/") ||
      request.url.startsWith("//") ||
      request.url.includes("\\")
    ) {
      reply(response, 403, { error: "Request origin is not allowed." });
      return;
    }
    const health = request.method === "GET" && request.url === "/api/health";
    if (
      !health &&
      !same(request.headers.authorization, `Bearer ${token}`) &&
      !(
        basic &&
        same(
          request.headers.authorization,
          `Basic ${Buffer.from(`admin:${token}`).toString("base64")}`,
        )
      )
    ) {
      reply(
        response,
        401,
        { error: "Authentication required." },
        {
          "www-authenticate": basic
            ? `Basic realm="Musae ${service}", charset="UTF-8"`
            : "Bearer",
        },
      );
      return;
    }
    const headers = {
      ...request.headers,
      host: upstreamHost,
      authorization: `Bearer ${token}`,
    };
    for (const key of Object.keys(headers)) {
      if (
        key.startsWith("x-forwarded-") ||
        [
          "forwarded",
          "proxy-authorization",
          "proxy-connection",
          "connection",
          "upgrade",
        ].includes(key)
      )
        delete headers[key];
    }
    if (origin) headers.origin = `http://${upstreamHost}`;
    const upstream = http.request(
      {
        hostname: "127.0.0.1",
        port: upstreamPort,
        method: health ? "GET" : request.method,
        path: health ? upstreamHealthPath : request.url,
        headers,
      },
      (incoming) => {
        if (health) {
          incoming.resume();
          const healthy = healthStatuses.includes(incoming.statusCode);
          reply(response, healthy ? 200 : 503, {
            status: healthy ? "ok" : "unavailable",
          });
          return;
        }
        const outgoingHeaders = { ...incoming.headers };
        delete outgoingHeaders.connection;
        const location = outgoingHeaders.location;
        if (
          typeof location === "string" &&
          location.startsWith(`http://${upstreamHost}/`)
        ) {
          outgoingHeaders.location = location.slice(
            `http://${upstreamHost}`.length,
          );
        }
        response.writeHead(incoming.statusCode ?? 502, outgoingHeaders);
        incoming.pipe(response);
      },
    );
    upstream.once("error", () => {
      if (!response.headersSent)
        reply(response, 503, { error: "Service is unavailable." });
      else response.destroy();
    });
    request.once("aborted", () => upstream.destroy());
    response.once("close", () => {
      if (!response.writableEnded) upstream.destroy();
    });
    if (health) {
      upstream.setTimeout(3000, () => upstream.destroy());
      upstream.end();
    } else request.pipe(upstream);
  });
}

export async function supervise(children, gateways) {
  let stopping = false;
  let exitCode = 0;
  let force;
  const stop = (code = 0) => {
    if (stopping) return;
    stopping = true;
    exitCode = code;
    for (const gateway of gateways) {
      gateway.close();
      gateway.closeAllConnections();
    }
    for (const child of children)
      if (child.exitCode === null) child.kill("SIGTERM");
    force = setTimeout(() => {
      for (const child of children)
        if (child.exitCode === null) child.kill("SIGKILL");
    }, 20000);
    force.unref();
  };
  for (const signal of ["SIGTERM", "SIGINT"])
    process.once(signal, () => stop());
  for (const gateway of gateways) gateway.once("error", () => stop(1));
  await Promise.all(
    children.map(
      (child) =>
        new Promise((resolve) => {
          child.once("error", () => stop(1));
          child.once("close", (code) => {
            if (!stopping) stop(code || 1);
            resolve();
          });
        }),
    ),
  );
  clearTimeout(force);
  process.exitCode = exitCode;
}
