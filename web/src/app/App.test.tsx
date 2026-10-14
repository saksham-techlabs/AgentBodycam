import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { ReplayApiClient } from "../api/client";
import {
  CRASHED_ID,
  FakeServer,
  LIVE_ID,
  STOPPED_ID,
  TOKEN,
  UNREADABLE_ID,
} from "../test/fakeServer";
import { App } from "./App";

let server: FakeServer;

beforeEach(() => {
  server = new FakeServer();
});

function renderApp(path = "/") {
  window.history.replaceState(null, "", path);
  return render(<App client={new ReplayApiClient(server.fetch)} />);
}

function row(sessionId: string): HTMLElement {
  return screen.getByTestId(`session-${sessionId}`);
}

describe("session dashboard", () => {
  it("shows a loading state before sessions arrive", async () => {
    renderApp();
    expect(screen.getByRole("status")).toHaveTextContent("Loading sessions");
    await screen.findByTestId(`session-${STOPPED_ID}`);
  });

  it("lists real session data newest first, matching API order", async () => {
    renderApp();
    await screen.findByTestId(`session-${STOPPED_ID}`);
    const ids = within(screen.getByRole("list", { name: "Sessions" }))
      .getAllByRole("button")
      .map((button) => button.getAttribute("data-testid"));
    expect(ids).toEqual(server.sessions.map((s) => `session-${s.session_id}`));

    const stopped = row(STOPPED_ID);
    expect(stopped).toHaveTextContent("STOPPED");
    expect(stopped).toHaveTextContent("2026-10-14 02:15:00 UTC");
    expect(stopped).toHaveTextContent("4m 05s");
    expect(stopped).toHaveTextContent("21");
    expect(row(CRASHED_ID)).toHaveTextContent("250");
    expect(stopped).toHaveTextContent("Complete");
  });

  it("shows the project summary without any path", async () => {
    renderApp();
    expect(await screen.findByTestId("project-summary")).toHaveTextContent(
      "6 recorded sessions",
    );
    expect(document.body.textContent).not.toMatch(/[A-Z]:\\|\/home\//);
  });

  it("renders every recording state distinctly", async () => {
    renderApp();
    await screen.findByTestId(`session-${STOPPED_ID}`);
    const labels = server.sessions.map((s) => row(s.session_id).querySelector(".badge")?.textContent);
    expect(labels).toEqual([
      "LIVE",
      "STOPPED",
      "ENDED WITHOUT STOP",
      "UNKNOWN",
      "NOT STARTED",
      "UNREADABLE",
    ]);
    expect(row(LIVE_ID)).toHaveTextContent("recording…");
    expect(row(LIVE_ID)).toHaveTextContent("Partial");
  });

  it("marks unreadable sessions as disabled and never requests them", async () => {
    renderApp();
    const unreadable = await screen.findByTestId(`session-${UNREADABLE_ID}`);
    expect(unreadable).toBeDisabled();
    expect(unreadable).toHaveTextContent("could not be read");
    fireEvent.click(unreadable);
    expect(window.location.pathname).toBe("/");
    expect(server.pathsRequested()).not.toContain(`/api/v1/sessions/${UNREADABLE_ID}`);
  });

  it("shows an empty state when nothing was recorded", async () => {
    server.sessions = [];
    renderApp();
    expect(await screen.findByText("No recorded sessions yet")).toBeInTheDocument();
    expect(screen.getByTestId("project-summary")).toHaveTextContent("0 recorded sessions");
  });

  it("auto-refreshes only while a session is live", async () => {
    renderApp();
    await screen.findByTestId(`session-${STOPPED_ID}`);
    expect(screen.getByText("Auto-refresh")).toBeInTheDocument();
    server.sessions = server.sessions.filter((s) => s.session_id !== LIVE_ID);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.queryByTestId(`session-${LIVE_ID}`)).toBeNull());
    expect(screen.queryByText("Auto-refresh")).toBeNull();
  });

  it("refreshes on demand", async () => {
    renderApp();
    await screen.findByTestId(`session-${STOPPED_ID}`);
    const before = server.pathsRequested().filter((p) => p === "/api/v1/sessions").length;
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() =>
      expect(server.pathsRequested().filter((p) => p === "/api/v1/sessions").length).toBe(
        before + 1,
      ),
    );
  });
});

describe("session overview", () => {
  it("selecting a session loads its detail and puts only its ID in the URL", async () => {
    renderApp();
    fireEvent.click(await screen.findByTestId(`session-${STOPPED_ID}`));

    expect(await screen.findByRole("heading", { name: STOPPED_ID })).toBeInTheDocument();
    expect(window.location.pathname).toBe(`/session/${STOPPED_ID}`);
    expect(window.location.href).not.toContain(TOKEN);
    expect(server.pathsRequested()).toContain(`/api/v1/sessions/${STOPPED_ID}`);

    const metric = (label: string) => screen.getByTestId(`metric-${label}`);
    expect(metric("Events")).toHaveTextContent("21");
    expect(metric("Observable failures")).toHaveTextContent("1");
    expect(metric("Recoveries")).toHaveTextContent("1");
    expect(metric("Unresolved")).toHaveTextContent("0");
    expect(metric("Warnings")).toHaveTextContent("2");
    expect(screen.getByText("2026-10-14 02:19:05 UTC")).toBeInTheDocument();
    expect(screen.queryByText("Auto-refresh")).toBeNull();
  });

  it("shows a live session as live and keeps it refreshing", async () => {
    renderApp(`/session/${LIVE_ID}`);
    expect(await screen.findByRole("heading", { name: LIVE_ID })).toBeInTheDocument();
    expect(screen.getByText("Live: still recording")).toBeInTheDocument();
    expect(screen.getByText("Auto-refresh")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("Live snapshot");
  });

  it("shows a crashed recording as ended without stop", async () => {
    renderApp(`/session/${CRASHED_ID}`);
    await screen.findByRole("heading", { name: CRASHED_ID });
    expect(screen.getAllByText("ENDED WITHOUT STOP").length).toBeGreaterThan(0);
    expect(screen.getByText(/crashed or was killed/)).toBeInTheDocument();
    expect(screen.getByText("No end recorded")).toBeInTheDocument();
  });

  it("explains a session that disappeared", async () => {
    renderApp(`/session/${STOPPED_ID}`);
    await screen.findByRole("heading", { name: STOPPED_ID });
    server.sessions = server.sessions.filter((s) => s.session_id !== STOPPED_ID);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByText("Session not found")).toBeInTheDocument();
    expect(screen.queryByTestId("metric-Events")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Back to sessions" }));
    expect(window.location.pathname).toBe("/");
  });

  it("explains an unreadable session opened by address", async () => {
    renderApp(`/session/${UNREADABLE_ID}`);
    expect(await screen.findByText("Session unreadable")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("backend text");
  });

  it("returns to the dashboard from the breadcrumb", async () => {
    renderApp(`/session/${STOPPED_ID}`);
    await screen.findByRole("heading", { name: STOPPED_ID });
    fireEvent.click(within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByText("Sessions"));
    expect(await screen.findByRole("heading", { name: "Recorded sessions" })).toBeInTheDocument();
  });

  it("treats an unknown address as not found", async () => {
    renderApp("/session/not-a-session");
    expect(screen.getByText("Page not found")).toBeInTheDocument();
  });
});

describe("connection problems", () => {
  it("reports an unreachable server in plain language", async () => {
    server.override = () => "network-error";
    renderApp();
    expect(await screen.findByText("Replay server unavailable")).toBeInTheDocument();
    expect(screen.getByTestId("connection-status")).toHaveTextContent("Server unreachable");
  });

  it("reports a refused bootstrap", async () => {
    server.bootstrap = {
      status: 403,
      body: { error: { code: "bootstrap_not_allowed", message: "backend text" } },
    };
    renderApp();
    expect(await screen.findByText("Access refused")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("backend text");
  });

  it("reports an expired token", async () => {
    server.token = "a-token-the-bootstrap-never-hands-out";
    renderApp();
    expect(await screen.findByText("Access expired")).toBeInTheDocument();
    expect(screen.getByTestId("connection-status")).toHaveTextContent("Access expired");
  });

  it("never shows raw backend error text", async () => {
    server.override = (path) =>
      path === "/api/v1/sessions"
        ? { status: 500, body: { error: { code: "internal_error", message: "Traceback C:\\x" } } }
        : undefined;
    renderApp();
    expect(await screen.findByText("Server error")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("Traceback");
  });

  it("shows the connected loopback server", async () => {
    renderApp();
    await waitFor(() =>
      expect(screen.getByTestId("connection-status")).toHaveTextContent("Connected"),
    );
    expect(await screen.findByText("v1")).toBeInTheDocument();
  });
});
