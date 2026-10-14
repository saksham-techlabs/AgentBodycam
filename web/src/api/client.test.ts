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

const FRESH = "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2Yz3Ab4C";
const STALE = "St4le_t0ken-that-the-server-no-longer-accepts";

function countPaths(path: string): number {
  return server.pathsRequested().filter((p) => p === path).length;
}

describe("401 re-bootstrap", () => {
  it("token exists -> 401 -> bootstrap -> retry succeeds", async () => {
    await client.sessions();
    server.token = FRESH;
    server.bootstrap = { status: 200, body: { api_version: 1, token: FRESH } };
    const report = await client.report(STOPPED_ID);
    expect(report.session_id).toBe(STOPPED_ID);
    expect(server.pathsRequested()).toEqual([
      "/ui/bootstrap",
      "/api/v1/sessions",
      `/api/v1/sessions/${STOPPED_ID}/report`,
      "/ui/bootstrap",
      `/api/v1/sessions/${STOPPED_ID}/report`,
    ]);
    expect(server.requests.at(-1)?.headers.authorization).toBe(`Bearer ${FRESH}`);
  });

  it("token absent -> bootstrap -> 401 -> bootstrap again -> retry succeeds", async () => {
    let bootstraps = 0;
    server.token = FRESH;
    server.override = (path) =>
      path === "/ui/bootstrap"
        ? { status: 200, body: { api_version: 1, token: ++bootstraps === 1 ? STALE : FRESH } }
        : undefined;
    const page = await client.timeline(STOPPED_ID);
    expect(page.items).toHaveLength(21);
    expect(server.pathsRequested()).toEqual([
      "/ui/bootstrap",
      `/api/v1/sessions/${STOPPED_ID}/timeline?after_sequence=0&limit=100`,
      "/ui/bootstrap",
      `/api/v1/sessions/${STOPPED_ID}/timeline?after_sequence=0&limit=100`,
    ]);
  });

  it("a second 401 fails as unauthorized after exactly one retry", async () => {
    await client.sessions();
    server.token = "never-handed-out-by-the-bootstrap-route-xx";
    const error = await rejection(client.session(STOPPED_ID));
    expect(error.kind).toBe("unauthorized");
    expect(countPaths("/ui/bootstrap")).toBe(2);
    expect(countPaths(`/api/v1/sessions/${STOPPED_ID}`)).toBe(2);
    expect(client.status).toBe("unauthorized");
  });

  it("never loops when the first bootstrap's token is rejected too", async () => {
    server.token = "never-handed-out-by-the-bootstrap-route-xx";
    await rejection(client.report(STOPPED_ID));
    expect(countPaths("/ui/bootstrap")).toBe(2);
    expect(server.requests).toHaveLength(4);
  });

  it("an abort during the first bootstrap stops before any API request", async () => {
    const controller = new AbortController();
    server.override = (path) => {
      if (path === "/ui/bootstrap") controller.abort();
      return undefined;
    };
    await expect(client.report(STOPPED_ID, controller.signal)).rejects.toMatchObject({
      name: "AbortError",
    });
    expect(server.pathsRequested()).toEqual(["/ui/bootstrap"]);
  });

  it("an abort during the re-bootstrap stops before the retry", async () => {
    await client.sessions();
    server.token = FRESH;
    const controller = new AbortController();
    server.override = (path) => {
      if (path === "/ui/bootstrap") {
        controller.abort();
        return { status: 200, body: { api_version: 1, token: FRESH } };
      }
      return undefined;
    };
    await expect(client.timeline(STOPPED_ID, {}, controller.signal)).rejects.toMatchObject({
      name: "AbortError",
    });
    expect(countPaths(`/api/v1/sessions/${STOPPED_ID}/timeline?after_sequence=0&limit=100`)).toBe(1);
  });

  it("an abort after the 401 skips the re-bootstrap entirely", async () => {
    await client.sessions();
    server.token = FRESH;
    const controller = new AbortController();
    server.override = (path) => {
      if (path.endsWith("/report")) {
        controller.abort();
        return { status: 401, body: { error: { code: "unauthorized", message: "x" } } };
      }
      return undefined;
    };
    await expect(client.report(STOPPED_ID, controller.signal)).rejects.toMatchObject({
      name: "AbortError",
    });
    expect(countPaths("/ui/bootstrap")).toBe(1);
  });

  it("leaks no token through retries, report or timeline calls", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((name) =>
      vi.spyOn(console, name),
    );
    server.token = FRESH;
    let bootstraps = 0;
    server.override = (path) =>
      path === "/ui/bootstrap"
        ? { status: 200, body: { api_version: 1, token: ++bootstraps === 1 ? TOKEN : FRESH } }
        : undefined;
    await client.report(STOPPED_ID);
    await client.timeline(STOPPED_ID, { afterSequence: 5, limit: 3 });
    for (const secret of [TOKEN, FRESH]) {
      for (const request of server.requests) expect(request.path).not.toContain(secret);
      expect(window.location.href).not.toContain(secret);
      expect(JSON.stringify(client)).not.toContain(secret);
      for (const spy of spies) {
        for (const args of spy.mock.calls) expect(JSON.stringify(args)).not.toContain(secret);
      }
    }
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
  });
});

describe("report", () => {
  it("parses the backend report shape", async () => {
    const report = await client.report(STOPPED_ID);
    expect(report.first_observable_failure?.sequence).toBe(5);
    expect(report.recoveries.items[0]?.recovered_sequence).toBe(14);
    expect(report.unresolved_failures.items[0]?.display_name).toBe("ruff");
    expect(report.file_change_summary.paths.items).toHaveLength(3);
    expect(report.latest_git_state?.state).toBe("dirty");
    expect(report.timeline.items.map((i) => i.sequence)).toEqual(
      Array.from({ length: 21 }, (_, i) => i + 1),
    );
  });

  it.each([
    ["a malformed event ID", (r: Json) => ((r.first_observable_failure as Json).event_id = "../x")],
    ["an unknown failure type", (r: Json) => ((r.first_observable_failure as Json).failure_type = "root_cause")],
    ["an inconsistent truncation flag", (r: Json) => ((r.recoveries as Json).truncated = true)],
    ["a total below the items shown", (r: Json) => ((r.warnings as Json).total = 0)],
    ["a window whose counts disagree", (r: Json) => {
      const recovery = ((r.recoveries as Json).items as Json[])[0]!;
      (recovery.changes_between_failure_and_pass as Json).created = 7;
    }],
    ["a recovery that passes before it fails", (r: Json) => {
      ((r.recoveries as Json).items as Json[])[0]!.recovered_sequence = 2;
    }],
    ["an unknown Git state", (r: Json) => ((r.latest_git_state as Json).state = "broken")],
    ["an unordered report timeline", (r: Json) => ((r.timeline as Json).items as Json[]).reverse()],
    ["another session's report", (r: Json) => (r.session_id = "20261014T031500Z-1111111111111111")],
    ["a missing section", (r: Json) => delete r.final_check_states],
  ])("rejects %s", async (_, mutate) => {
    const body = JSON.parse(JSON.stringify(server.reportFor(STOPPED_ID))) as Json;
    mutate(body);
    server.override = (path) => (path.endsWith("/report") ? { status: 200, body } : undefined);
    expect((await rejection(client.report(STOPPED_ID))).kind).toBe("invalid_response");
  });
});

type Json = Record<string, unknown>;

describe("timeline", () => {
  const path = (after: number, limit: number) =>
    `/api/v1/sessions/${STOPPED_ID}/timeline?after_sequence=${after}&limit=${limit}`;

  it("requests exactly the cursor and limit asked for", async () => {
    const page = await client.timeline(STOPPED_ID, { afterSequence: 5, limit: 3 });
    expect(server.pathsRequested().at(-1)).toBe(path(5, 3));
    expect(page.items.map((i) => i.sequence)).toEqual([6, 7, 8]);
    expect(page.next_after_sequence).toBe(8);
    expect(page.has_more).toBe(true);
    expect(page.total).toBe(21);
  });

  it("validates the session ID and parameters before any request", async () => {
    for (const query of [{ afterSequence: -1 }, { limit: 0 }, { limit: 501 }, { afterSequence: 1.5 }]) {
      expect((await rejection(client.timeline(STOPPED_ID, query))).kind).toBe("bad_request");
    }
    expect((await rejection(client.timeline("../etc"))).kind).toBe("bad_request");
    expect((await rejection(client.report("x/../y"))).kind).toBe("bad_request");
    expect(server.fetch).not.toHaveBeenCalled();
  });

  function pageBody(mutate: (page: Json) => void): Json {
    const items = server.timelines.get(STOPPED_ID)!.slice(0, 3);
    const page: Json = {
      api_version: 1,
      session_id: STOPPED_ID,
      after_sequence: 0,
      limit: 100,
      items: JSON.parse(JSON.stringify(items)) as Json[],
      next_after_sequence: 3,
      has_more: true,
      total: 21,
    };
    mutate(page);
    return page;
  }

  it.each([
    ["out-of-order items", (p: Json) => (p.items as Json[]).reverse()],
    ["a repeated sequence", (p: Json) => (((p.items as Json[])[1]!).sequence = 1)],
    ["a repeated event ID", (p: Json) => (((p.items as Json[])[2]!).event_id = (p.items as Json[])[0]!.event_id)],
    ["an item at or before the cursor", (p: Json) => {
      p.after_sequence = 1;
    }],
    ["a cursor that skips past the items", (p: Json) => (p.next_after_sequence = 9)],
    ["a cursor behind the items", (p: Json) => (p.next_after_sequence = 2)],
    ["has_more contradicting total", (p: Json) => (p.has_more = false)],
    ["a total below the cursor", (p: Json) => (p.total = 2)],
    ["more items than the limit", (p: Json) => (p.limit = 2)],
    ["an unknown category", (p: Json) => (((p.items as Json[])[0]!).category = "payload")],
    ["another session's page", (p: Json) => (p.session_id = "20261014T031500Z-1111111111111111")],
    ["a different cursor than requested", (p: Json) => {
      p.after_sequence = 0;
      p.items = [];
      p.next_after_sequence = 0;
      p.limit = 50;
    }],
  ])("rejects a page with %s", async (_, mutate) => {
    const body = pageBody(mutate);
    server.override = (p) => (p.includes("/timeline") ? { status: 200, body } : undefined);
    expect((await rejection(client.timeline(STOPPED_ID))).kind).toBe("invalid_response");
  });

  it("accepts an empty last page whose cursor stays put", async () => {
    const page = await client.timeline(STOPPED_ID, { afterSequence: 21 });
    expect(page.items).toEqual([]);
    expect(page.next_after_sequence).toBe(21);
    expect(page.has_more).toBe(false);
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
