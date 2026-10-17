import { readdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import type { Plugin, ProxyOptions } from "vite";
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

// Vite can inherit CRLF (or leave bare CR) from a Windows working tree into index.html.
// Committed UI assets must be byte-identical on Linux CI, so normalize text outputs to LF.
const TEXT_ASSET_EXT = new Set([".css", ".html", ".js", ".json", ".svg", ".txt"]);

function toLf(text: string): string {
  return text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
}

function enforceLf(root: string): Plugin {
  return {
    name: "agentbodycam-enforce-lf",
    apply: "build",
    // Normalize before Vite rewrites index.html. Otherwise a CRLF working tree leaves a
    // bare CR when the entry script line is removed; post-LF conversion turns that into
    // an extra blank line and Linux/Windows builds diverge.
    transformIndexHtml: {
      order: "pre",
      handler(html) {
        return toLf(html);
      },
    },
    async closeBundle() {
      async function walk(dir: string): Promise<void> {
        for (const entry of await readdir(dir, { withFileTypes: true })) {
          const full = path.join(dir, entry.name);
          if (entry.isDirectory()) {
            await walk(full);
            continue;
          }
          if (!TEXT_ASSET_EXT.has(path.extname(entry.name).toLowerCase())) {
            continue;
          }
          const raw = await readFile(full);
          const text = raw.toString("utf8");
          const normalized = toLf(text);
          if (normalized !== text) {
            await writeFile(full, normalized, "utf8");
          }
        }
      }
      await walk(root);
    },
  };
}

export default defineConfig({
  plugins: [react(), enforceLf(outDir)],
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
