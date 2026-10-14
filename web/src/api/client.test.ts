import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FakeServer, STOPPED_ID, TOKEN } from "../test/fakeServer";
import { BOOTSTRAP_HEADER, ReplayApiClient } from "./client";
import { ApiError } from "./errors";

let server: FakeServer;
let client: ReplayApiClient;

beforeEach(() => {
  server = new FakeServer();
  client = new ReplayApiClient(server.fetch);
});

afterEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

async function rejection(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError);
    return error as ApiError;
  }
  throw new Error("expected the request to fail");
}

describe("authentication", () => {
  it("bootstraps the token once and sends it as a bearer header", async () => {
    await client.sessions();
    await client.project();
    await client.session(STOPPED_ID);

    expect(server.pathsRequested()).toEqual([
      "/ui/bootstrap",
      "/api/v1/sessions",
      "/api/v1/project",
      `/api/v1/sessions/${STOPPED_ID}`,
    ]);
    const [bootstrap, ...api] = server.requests;
    expect(bootstrap?.headers[BOOTSTRAP_HEADER.toLowerCase()]).toBe("bootstrap");
    expect(bootstrap?.headers.authorization).toBeUndefined();
    for (const request of api) {
      expect(request.headers.authorization).toBe(`Bearer ${TOKEN}`);
      expect(request.method).toBe("GET");
    }
  });

  it("only ever issues GET requests with no-store and no redirects", async () => {
    await client.health();
    await client.sessions();
    for (const call of server.fetch.mock.calls) {
      const init = call[1];
      expect(init.method).toBe("GET");
      expect(init.cache).toBe("no-store");
      expect(init.redirect).toBe("error");
      expect(init.body).toBeUndefined();
    }
  });

  it("never puts the token in a URL, storage, cookies or the console", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((name) =>
      vi.spyOn(console, name),
    );
    await client.sessions();
    await client.session(STOPPED_ID);
    server.token = "rotated-token-that-does-not-match-anything";
    await rejection(client.project());

    for (const request of server.requests) expect(request.path).not.toContain(TOKEN);
    expect(window.location.href).not.toContain(TOKEN);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
    for (const spy of spies) {
      for (const args of spy.mock.calls) expect(JSON.stringify(args)).not.toContain(TOKEN);
    }
    expect(JSON.stringify(client)).not.toContain(TOKEN);
    expect(Object.values(client).join(" ")).not.toContain(TOKEN);
  });

  it("re-bootstraps once when the token is rejected (server restarted)", async () => {
    await client.sessions();
    server.token = "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2Yz3Ab4C";
    server.bootstrap = { status: 200, body: { api_version: 1, token: server.token } };
    const sessions = await client.sessions();
    expect(sessions.total).toBe(server.sessions.length);
    expect(server.pathsRequested().filter((p) => p === "/ui/bootstrap")).toHaveLength(2);
  });

  it("reports unauthorized when a fresh token is still rejected", async () => {
    server.token = "a-token-the-bootstrap-never-hands-out";
    const error = await rejection(client.sessions());
    expect(error.kind).toBe("unauthorized");
    expect(error.status).toBe(401);
    expect(client.status).toBe("unauthorized");
  });

  it("reports a refused bootstrap as forbidden", async () => {
    server.bootstrap = { status: 403, body: { error: { code: "bootstrap_not_allowed", message: "x" } } };
    const error = await rejection(client.sessions());
    expect(error.kind).toBe("forbidden");
    expect(error.code).toBe("bootstrap_not_allowed");
    expect(client.status).toBe("refused");
    expect(client.hasAccess).toBe(false);
  });

  it("rejects a malformed bootstrap token", async () => {
    server.bootstrap = { status: 200, body: { api_version: 1, token: "short" } };
    expect((await rejection(client.sessions())).kind).toBe("invalid_response");
  });
});

describe("errors", () => {
  it.each([
    [400, "invalid_parameter", "bad_request"],
    [401, "unauthorized", "unauthorized"],
    [403, "origin_not_allowed", "forbidden"],
    [404, "session_not_found", "not_found"],
    [500, "session_unreadable", "server"],
    [503, "internal_error", "server"],
  ] as const)("maps HTTP %i to %s", async (status, code, kind) => {
    server.override = (path) =>
      path === "/api/v1/sessions"
        ? { status, body: { error: { code, message: "Traceback at C:\\Users\\secret" } } }
        : undefined;
    const error = await rejection(client.sessions());
    expect(error.kind).toBe(kind);
    expect(error.status).toBe(status);
    expect(error.code).toBe(code);
    expect(error.message).not.toContain("Traceback");
    expect(error.message).not.toContain("secret");
  });

  it("maps a network failure to unavailable", async () => {
    server.override = () => "network-error";
    const error = await rejection(client.health());
    expect(error.kind).toBe("unavailable");
    expect(error.status).toBeNull();
    expect(client.status).toBe("unreachable");
  });

  it("rejects responses that do not match the API schema", async () => {
    server.override = (path) =>
      path === "/api/v1/sessions"
        ? { status: 200, body: { api_version: 1, sessions: [{ session_id: "../etc" }], total: 1 } }
        : undefined;
    expect((await rejection(client.sessions())).kind).toBe("invalid_response");
  });

  it("refuses a malformed session ID without sending a request", async () => {
    const error = await rejection(client.session("../../.agentbodycam/sessions"));
    expect(error.kind).toBe("bad_request");
    expect(server.fetch).not.toHaveBeenCalled();
  });
});

describe("cancellation", () => {
  it("propagates aborts as AbortError, not as an API error", async () => {
    await client.health();
    const controller = new AbortController();
    controller.abort();
    await expect(client.sessions(controller.signal)).rejects.toMatchObject({ name: "AbortError" });
    expect(client.status).toBe("online");
  });

  it("passes the abort signal to fetch", async () => {
    const controller = new AbortController();
    await client.sessions(controller.signal);
    for (const call of server.fetch.mock.calls) expect(call[1].signal).toBe(controller.signal);
  });
});

describe("connection status", () => {
  it("starts connecting and becomes online after a response", async () => {
    const changes: string[] = [];
    client.subscribe(() => changes.push(client.status));
    expect(client.status).toBe("connecting");
    await client.health();
    expect(changes).toEqual(["online"]);
  });
});
