// Build-only Debian downloader. APT validates signed repository metadata; each
// parallel download is bounded by the size and SHA256 from that metadata.
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { createWriteStream } from "node:fs";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { Readable, Transform } from "node:stream";
import { pipeline } from "node:stream/promises";
import { rootCertificates } from "node:tls";

const args = process.argv.slice(2);
const aptOptions = [
  "-o",
  "Acquire::Retries=3",
  "-o",
  "Acquire::https::Timeout=30",
];
function command(name, options, capture = false) {
  const result = spawnSync(name, options, {
    encoding: "utf8",
    stdio: capture ? ["ignore", "pipe", "inherit"] : "inherit",
    maxBuffer: 16 * 1024 * 1024,
  });
  if (result.error || result.status !== 0)
    throw (
      result.error || new Error(`${name} failed with exit ${result.status}`)
    );
  return result.stdout;
}

if (args.length === 1 && args[0] === "--prepare") {
  // Node slim has bundled Mozilla roots but no system CA file. Bootstrap from
  // those trusted roots, then install Debian's normal ca-certificates package.
  await mkdir("/etc/ssl/certs", { recursive: true });
  await writeFile(
    "/etc/ssl/certs/ca-certificates.crt",
    rootCertificates.join("\n") + "\n",
  );
  const source = "/etc/apt/sources.list.d/debian.sources";
  await writeFile(
    source,
    (await readFile(source, "utf8")).replaceAll(
      "http://deb.debian.org",
      "https://deb.debian.org",
    ),
  );
  command("apt-get", [
    ...aptOptions,
    "-o",
    "APT::Update::Error-Mode=any",
    "update",
  ]);
} else {
  if (
    !args.length ||
    args.some((value) => !/^[a-z0-9][a-z0-9+.-]*$/.test(value))
  )
    throw new Error("Provide explicit Debian package names or --prepare.");
  const plan = command(
    "apt-get",
    [
      ...aptOptions,
      "-o",
      "Acquire::ForceHash=sha256",
      "--print-uris",
      "--yes",
      "--no-install-recommends",
      "install",
      ...args,
    ],
    true,
  );
  const packages = plan
    .split("\n")
    .filter((line) => line.startsWith("'"))
    .map((line) => {
      const match = line.match(
        /^'([^']+)' ([A-Za-z0-9][A-Za-z0-9+_.:~%=-]*\.deb) (\d+) SHA256:([a-f0-9]{64})$/i,
      );
      if (!match)
        throw new Error(`APT did not provide a SHA256 package plan: ${line}`);
      const [, uri, filename, bytes, sha256] = match;
      if (new URL(uri).protocol !== "https:")
        throw new Error("Package URL must use HTTPS.");
      return {
        uri,
        filename,
        bytes: Number(bytes),
        sha256: sha256.toLowerCase(),
      };
    });
  const directory = "/var/cache/apt/archives";
  await mkdir(directory, { recursive: true });
  let cursor = 0;
  const download = async ({ uri, filename, bytes, sha256 }) => {
    const target = join(directory, filename);
    const partial = `${target}.musae-partial`;
    for (let attempt = 1; attempt <= 3; attempt++) {
      await rm(partial, { force: true });
      try {
        const response = await fetch(uri, {
          redirect: "error",
          signal: AbortSignal.timeout(10 * 60 * 1000),
        });
        if (response.status !== 200 || !response.body)
          throw new Error(`HTTP ${response.status}`);
        let received = 0;
        const digest = createHash("sha256");
        await pipeline(
          Readable.fromWeb(response.body),
          new Transform({
            transform(chunk, encoding, done) {
              received += chunk.length;
              if (received > bytes)
                return done(new Error("Package exceeds its signed size."));
              digest.update(chunk);
              done(null, chunk);
            },
          }),
          createWriteStream(partial, { flags: "wx", mode: 0o644 }),
        );
        if (received !== bytes || digest.digest("hex") !== sha256)
          throw new Error(
            "Package size or SHA256 does not match signed APT metadata.",
          );
        await rename(partial, target);
        console.log(`[apt-download] verified ${filename} (${bytes} bytes)`);
        return;
      } catch (error) {
        await rm(partial, { force: true });
        if (attempt === 3)
          throw new Error(`Cannot verify ${filename}: ${error.message}`);
        console.error(
          `[apt-download] retry ${attempt}/3: ${filename}: ${error.message}`,
        );
        await new Promise((resolve) => setTimeout(resolve, 1000 * attempt));
      }
    }
  };
  console.log(
    `[apt-download] ${packages.length} signed packages, up to 8 concurrent downloads`,
  );
  await Promise.all(
    Array.from({ length: Math.min(8, packages.length) }, async () => {
      while (cursor < packages.length) await download(packages[cursor++]);
    }),
  );
  command("apt-get", [
    ...aptOptions,
    "--yes",
    "--no-install-recommends",
    "--no-download",
    "install",
    ...args,
  ]);
}
