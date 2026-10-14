/// <reference types="node" />
/**
 * Release security regressions: hostile strings through every export, no token in the
 * DOM or browser storage, fixed error text, and source-level scans for network, storage,
 * capture and code-injection APIs and for causal or screen-video wording.
 */

import { readFileSync, readdirSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReplayApiClient } from "./api/client";
import type { ForensicReport } from "./api/types";
import { App } from "./app/App";
import { clipTexts, planReplayClip } from "./lib/clipPlan";
import { drawClipFrame } from "./lib/clipDraw";
import { shareCardSvg } from "./lib/shareCard";
import { SUMMARY_KEYS, buildShareModel, buildShareSummary, serializeSummary } from "./lib/shareModel";
import { el, serializeSvg, type SvgTag } from "./lib/svg";
import { FakeServer, STOPPED_ID, TOKEN } from "./test/fakeServer";
import { richReport } from "./test/fixtures";
import { recordingContext } from "./test/media";

const SRC = resolve(process.cwd(), "src");

/** Every non-test source file under `src/`, as `[path relative to src, text]`. */
function sources(): [string, string][] {
  const files: [string, string][] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== "test") walk(path);
      } else if (/\.(ts|tsx)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
        files.push([relative(SRC, path).replace(/\\/g, "/"), readFileSync(path, "utf8")]);
      }
    }
  };
  walk(SRC);
  return files;
}

/** Source without comments, so prose about what the UI does *not* use is not flagged. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/[^\n]*/g, "$1");
}

// Every hostile string from the release checklist, in one value.
const HOSTILE = [
  '<script>alert("x")</script>',
  "</text>",
  "&xxe;",
  "javascript:alert(1)",
  "url(https://evil.example/x)",
  "file:///etc/passwd",
  "C:\\Users\\alice\\secret",
  "../../secret",
  "TOKEN_VALUE_7f3a",
].join(" ");
const HOSTILE_PROBES = [
  "<script",
  "</text>&",
  "xxe",
  "javascript:",
  "url(http",
  "evil.example",
  "file:",
  "alice",
  "../",
  "secret",
  "TOKEN_VALUE",
  TOKEN,
];
// Enumerations the export code switches on; everything else becomes hostile text.
const KEPT = new Set(["recording_state", "session_status", "latest_result", "result"]);

function poison(value: unknown, key = ""): unknown {
  if (typeof value === "string") return KEPT.has(key) ? value : HOSTILE;
  if (Array.isArray(value)) return value.map((v) => poison(v, key));
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, poison(v, k)]));
  }
  return value;
}

function hostileReport(): ForensicReport {
  return poison(richReport(STOPPED_ID)) as ForensicReport;
}

function expectClean(text: string, where: string): void {
  for (const probe of HOSTILE_PROBES) expect(text, `${where}: ${probe}`).not.toContain(probe);
}

describe("hostile strings never reach an export", () => {
  it("SVG share card", () => {
    const svg = shareCardSvg(buildShareModel(hostileReport()));
    expectClean(svg, "svg");
    expect(svg).not.toMatch(/<(script|foreignObject|image|use|a|style|iframe)\b|href|\son[a-z]+=/i);
    expect(svg.match(/url\([^)]*\)/g)?.every((u) => /^url\(#[a-z0-9-]+\)$/.test(u)) ?? true).toBe(true);
  });

  it("JSON summary keeps the approved keys only", () => {
    const summary = buildShareSummary(buildShareModel(hostileReport()));
    expect(Object.keys(summary)).toEqual([...SUMMARY_KEYS]);
    for (const moment of [summary.first_observable_failure, summary.first_observed_recovery]) {
      if (moment !== null) expect(Object.keys(moment)).toEqual(["event", "label"]);
    }
    for (const check of summary.final_checks) expect(Object.keys(check)).toEqual(["kind", "name", "result"]);
    expectClean(serializeSummary(summary), "json");
  });

  it("WebM scene plan and every drawn frame", () => {
    const timeline = planReplayClip(buildShareModel(hostileReport()));
    expectClean(JSON.stringify(timeline), "plan");
    const { ctx, drawn, fonts } = recordingContext();
    for (let frame = 0; frame < timeline.totalFrames; frame += 1) drawClipFrame(ctx, timeline, frame);
    expectClean(drawn.map((d) => d.text).join("\n"), "canvas");
    expectClean(clipTexts(timeline).join("\n"), "clip texts");
    for (const font of fonts) expect(font).not.toMatch(/url\(|https?:|file:/i);
  });

  it("the SVG serializer refuses unknown elements and unsafe attributes at run time", () => {
    const sneak = (tag: string) => serializeSvg(el("svg", {}, el(tag as SvgTag)));
    for (const tag of ["script", "foreignObject", "a", "image", "use", "style"]) {
      expect(() => sneak(tag), tag).toThrow("unsafe SVG element");
    }
    expect(() => serializeSvg(el("svg", { href: "javascript:alert(1)" }))).toThrow();
    expect(() => serializeSvg(el("svg", { onload: "x" }))).toThrow();
    expect(() => serializeSvg(el("svg", { fill: "url(https://evil.example/x)" }))).toThrow();
    const text = serializeSvg(el("svg", {}, el("text", {}, HOSTILE)));
    expect(text).not.toContain("<script");
    expect(text).toContain("&lt;/text&gt; &amp;xxe;");
  });
});

describe("the running UI", () => {
  let server: FakeServer;

  beforeEach(() => {
    server = new FakeServer();
    URL.createObjectURL = vi.fn(() => "blob:local/1");
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  async function openSession(): Promise<void> {
    window.history.replaceState(null, "", `/session/${STOPPED_ID}`);
    render(<App client={new ReplayApiClient(server.fetch)} />);
    await screen.findByRole("heading", { name: STOPPED_ID });
  }

  it("keeps the bearer token out of the DOM, URL, storage and cookies", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    await openSession();
    const button = await screen.findByRole("button", { name: "Export" });
    await vi.waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    const dialog = screen.getByRole("dialog", { name: "Export replay summary" });
    fireEvent.click(within(dialog).getByRole("button", { name: /JSON summary/ }));
    expect(document.documentElement.outerHTML).not.toContain(TOKEN);
    expect(window.location.href).not.toContain(TOKEN);
    expect(localStorage.length + sessionStorage.length).toBe(0);
    expect(setItem).not.toHaveBeenCalled();
    expect(document.cookie).toBe("");
  });

  it("renders fixed error text, never backend messages, paths or tokens", async () => {
    const hostile = `Traceback: PermissionError at C:\\Users\\alice\\proj\\.agentbodycam Bearer ${TOKEN}`;
    server.override = (path) =>
      path === `/api/v1/sessions/${STOPPED_ID}`
        ? { status: 500, body: { error: { code: "internal_error", message: hostile } } }
        : undefined;
    window.history.replaceState(null, "", `/session/${STOPPED_ID}`);
    render(<App client={new ReplayApiClient(server.fetch)} />);
    expect(await screen.findByText("Server error")).toBeInTheDocument();
    const html = document.documentElement.outerHTML;
    for (const leak of ["Traceback", "PermissionError", "alice", ".agentbodycam", TOKEN]) {
      expect(html).not.toContain(leak);
    }
  });

  it("renders fixed text for a network failure carrying an exception message", async () => {
    const fetch = vi.fn(async (input: string, init: RequestInit) => {
      if (input === "/api/v1/sessions") throw new TypeError("connect ECONNREFUSED C:\\Users\\alice TOKEN_VALUE_7f3a");
      return server.fetch(input, init);
    });
    window.history.replaceState(null, "", "/");
    render(<App client={new ReplayApiClient(fetch)} />);
    expect(await screen.findByText("Replay server unavailable")).toBeInTheDocument();
    const html = document.documentElement.outerHTML;
    for (const leak of ["ECONNREFUSED", "alice", "TOKEN_VALUE"]) expect(html).not.toContain(leak);
  });
});

describe("source scans", () => {
  const files = sources();

  it("finds the UI sources", () => {
    expect(files.map(([path]) => path)).toContain("api/client.ts");
    expect(files.length).toBeGreaterThan(20);
  });

  it("uses no browser storage", () => {
    const storage = /\b(localStorage|sessionStorage|indexedDB|IDBFactory|document\.cookie|cookieStore|caches|CacheStorage|navigator\.storage)\b/;
    for (const [path, text] of files) expect(code(text), path).not.toMatch(storage);
  });

  it("makes requests only through the API client, to its own origin", () => {
    const network = /\b(XMLHttpRequest|WebSocket|EventSource|sendBeacon|RTCPeerConnection|importScripts|SharedWorker|Worker|window\.open|postMessage)\b/;
    for (const [path, text] of files) {
      const source = code(text);
      expect(source, path).not.toMatch(network);
      if (path !== "api/client.ts") expect(source, path).not.toMatch(/\bfetch\s*\(/);
      const urls = text.match(/[a-z][a-z0-9+.-]*:\/\/[^\s"'`)]*/gi) ?? [];
      expect(urls.filter((u) => u !== "http://www.w3.org/2000/svg"), path).toEqual([]);
    }
  });

  it("captures nothing from the screen, camera or page", () => {
    const capture = /\b(getDisplayMedia|getUserMedia|html2canvas|toDataURL|drawWindow|ImageCapture)\b/;
    for (const [path, text] of files) {
      expect(code(text), path).not.toMatch(capture);
      if (path !== "lib/clipRecorder.ts") expect(code(text), path).not.toMatch(/captureStream/);
    }
  });

  it("injects no HTML and evaluates no code", () => {
    const injection = /\b(innerHTML|outerHTML|dangerouslySetInnerHTML|insertAdjacentHTML|document\.write|eval|Function)\s*[(=:]/;
    for (const [path, text] of files) expect(code(text), path).not.toMatch(injection);
  });

  it("never claims causes in user-facing text", () => {
    const causal = /root cause|culprit|responsible for|\bcaused\b|this (edit|change) (fixed|broke)|agent fixed|fixed by|broke the/i;
    for (const [path, text] of files) expect(code(text), path).not.toMatch(causal);
  });

  it("never describes the replay as screen video", () => {
    const video = /screen (recording|capture|video)|footage|desktop video|recording of (cursor|codex|claude)/gi;
    for (const [path, text] of files) {
      for (const match of code(text).matchAll(video)) {
        const before = code(text).slice(Math.max(0, (match.index ?? 0) - 40), match.index);
        expect(before, `${path}: ${match[0]}`).toMatch(/\b(no|not|never|nothing)\b/i);
      }
    }
  });
});
