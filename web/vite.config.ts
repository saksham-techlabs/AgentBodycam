import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import type { ProxyOptions } from "vite";
import { defineConfig } from "vitest/config";

// The production build is written into the Python package, which serves it from the
// Replay server's own loopback origin. Node is only needed to produce these files.
const outDir = fileURLToPath(new URL("../src/agentbodycam/ui/static", import.meta.url));

// Development only: proxy the API to a running `agentbodycam replay --port N`.
const devApi = process.env.AGENTBODYCAM_DEV_API ?? "http://127.0.0.1:8765";

/** The dev server's own origin is not the Replay server's; same-origin GETs need none. */
const dropOrigin: NonNullable<ProxyOptions["configure"]> = (proxy) => {
  proxy.on("proxyReq", (request) => request.removeHeader("origin"));
};

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir,
    emptyOutDir: true,
    sourcemap: false,
    // Every asset is a separate same-origin file: nothing is inlined as a data: URI and no
    // inline script is emitted, so the UI runs under a CSP without 'unsafe-inline'.
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    cssCodeSplit: false,
    reportCompressedSize: false,
  },
  server: {
    host: "127.0.0.1",
    strictPort: true,
    proxy: {
      "/api/": { target: devApi, changeOrigin: true, configure: dropOrigin },
      "/ui/bootstrap": { target: devApi, changeOrigin: true, configure: dropOrigin },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test/setup.ts"],
    restoreMocks: true,
    unstubGlobals: true,
  },
});
