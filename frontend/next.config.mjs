import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Standalone output keeps the runtime image small in docker-compose.
  output: "standalone",
  // Without this, Next infers a tracing root from the surrounding filesystem
  // and nests the standalone bundle under that path, so .next/standalone/server.js
  // is not where the Dockerfile expects it. Pinning it to this directory keeps
  // the layout flat and identical on every machine.
  outputFileTracingRoot: dirname(fileURLToPath(import.meta.url)),
};
export default nextConfig;
