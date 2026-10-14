import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReplayApiClient } from "../api/client";
import { App } from "../app/App";
import { CAUSAL, eventId } from "../test/fixtures";
import { FakeServer, STOPPED_ID, TOKEN } from "../test/fakeServer";

let server: FakeServer;
let blobs: Blob[];
let revoked: string[];
let clicks: { download: string; href: string }[];

beforeEach(() => {
  server = new FakeServer();
  blobs = [];
  revoked = [];
  clicks = [];
  let n = 0;
  URL.createObjectURL = vi.fn((blob: Blob) => {
    blobs.push(blob);
    n += 1;
    return `blob:local/${n}`;
  });
  URL.revokeObjectURL = vi.fn((url: string) => {
    revoked.push(url);
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    clicks.push({ download: this.download, href: this.getAttribute("href") ?? "" });
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function openExport(): Promise<HTMLElement> {
  window.history.replaceState(null, "", `/session/${STOPPED_ID}`);
  render(<App client={new ReplayApiClient(server.fetch)} />);
  await screen.findByRole("heading", { name: STOPPED_ID });
  const button = await screen.findByRole("button", { name: "Export" });
  await vi.waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  return screen.getByRole("dialog", { name: "Export replay summary" });
}

describe("export dialog", () => {
  it("opens an accessible dialog with a share-card preview and the privacy note", async () => {
    const dialog = await openExport();
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(within(dialog).getByRole("button", { name: "Close" })).toHaveFocus();
    expect(within(dialog).getByRole("button", { name: /Share card/ })).toHaveAttribute("aria-pressed", "true");
    const note = within(dialog).getByTestId("export-privacy-note");
    expect(note).toHaveTextContent("Public-safe export");
    expect(note).toHaveTextContent("Source code, paths, command arguments and identifiers are excluded.");
    const preview = within(dialog).getByRole("img", { name: "Share card preview" });
    expect(preview).toHaveAttribute("viewBox", "0 0 1200 630");
    expect(preview).toHaveTextContent("FIRST OBSERVABLE FAILURE");
    expect(preview).toHaveTextContent("Test failed: pytest");
    expect(preview).toHaveTextContent("Recorded locally · No source code uploaded");
    expect(preview.textContent ?? "").not.toContain(STOPPED_ID);
    expect(dialog.textContent ?? "").not.toMatch(CAUSAL);
  });

  it("downloads the previewed card as agentbodycam-replay.svg and revokes the URL", async () => {
    const dialog = await openExport();
    const preview = within(dialog).getByRole("img", { name: "Share card preview" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Download SVG" }));
    expect(clicks).toEqual([{ download: "agentbodycam-replay.svg", href: "blob:local/1" }]);
    expect(revoked).toEqual(["blob:local/1"]);
    expect(document.querySelector("a[download]")).toBeNull();
    const blob = blobs[0];
    if (blob === undefined) throw new Error("no blob");
    expect(blob.type).toBe("image/svg+xml;charset=utf-8");
    const svg = await blob.text();
    expect(svg).toContain("<svg");
    expect(svg).not.toMatch(/<script|foreignObject|href/i);
    for (const leak of [STOPPED_ID, TOKEN, eventId(5), "src/app.py", "1a2b3c4", "pytest -q"]) {
      expect(svg).not.toContain(leak);
    }
    const exportedText = new DOMParser()
      .parseFromString(svg, "image/svg+xml")
      .documentElement.textContent?.replace(/\s+/g, "");
    expect(exportedText).toBe(preview.textContent?.replace(/\s+/g, ""));
  });

  it("previews and downloads the JSON summary as agentbodycam-summary.json", async () => {
    const dialog = await openExport();
    fireEvent.click(within(dialog).getByRole("button", { name: /JSON summary/ }));
    const pre = within(dialog).getByTestId("json-preview");
    expect(pre).toHaveTextContent('"format": "agentbodycam-share-summary"');
    fireEvent.click(within(dialog).getByRole("button", { name: "Download JSON summary" }));
    expect(clicks).toEqual([{ download: "agentbodycam-summary.json", href: "blob:local/1" }]);
    expect(revoked).toEqual(["blob:local/1"]);
    const text = await blobs[0]?.text();
    expect(text).toBe(pre.textContent);
    const parsed = JSON.parse(text ?? "") as Record<string, unknown>;
    expect(parsed.format).toBe("agentbodycam-share-summary");
    expect(parsed.version).toBe(1);
    expect(text).not.toContain(STOPPED_ID);
    expect(text).not.toContain(TOKEN);
  });

  it("makes no network request to open, preview or download", async () => {
    await openExport();
    const before = server.requests.length;
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Download SVG" }));
    fireEvent.click(within(dialog).getByRole("button", { name: /JSON summary/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Download JSON summary" }));
    expect(server.requests.length).toBe(before);
  });

  it("closes on Escape and returns focus to the Export button", async () => {
    const dialog = await openExport();
    fireEvent.keyDown(within(dialog).getByRole("button", { name: "Close" }), { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("button", { name: "Export" })).toHaveFocus();
  });

  it("keeps Tab focus inside the dialog", async () => {
    const dialog = await openExport();
    const buttons = within(dialog).getAllByRole("button");
    const last = buttons.at(-1);
    const first = buttons[0];
    if (first === undefined || last === undefined) throw new Error("no buttons");
    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(last).toHaveFocus();
  });

  it("closes with the Close button", async () => {
    const dialog = await openExport();
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
