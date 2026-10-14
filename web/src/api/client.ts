/**
 * The only place the Replay UI talks to the network: same-origin GET requests to the
 * Replay server that served this page.
 *
 * The bearer token is obtained from `/ui/bootstrap` and kept in a private field of this
 * object, i.e. in this page's memory only. It is never written to localStorage,
 * sessionStorage, cookies, the URL or the console, and it is sent nowhere but in the
 * `Authorization` header of same-origin `/api/v1` requests. Reloading the page simply
 * bootstraps again; closing it forgets the token.
 */

import { ApiError, isAbort, kindForStatus } from "./errors";
import type {
  ForensicReport,
  HealthResponse,
  ProjectSummary,
  SessionDetail,
  SessionsResponse,
  TimelinePage,
} from "./types";
import {
  InvalidResponseError,
  errorCode,
  isSessionId,
  parseBootstrap,
  parseForensicReport,
  parseHealth,
  parseProject,
  parseSessionDetail,
  parseSessions,
  parseTimelinePage,
} from "./validate";

export const BOOTSTRAP_PATH = "/ui/bootstrap";
export const BOOTSTRAP_HEADER = "X-AgentBodycam-UI";

/** The backend's default and maximum timeline page sizes (`models/replay.py`). */
export const TIMELINE_PAGE_SIZE = 100;
export const TIMELINE_MAX_LIMIT = 500;

export interface TimelineQuery {
  afterSequence?: number;
  limit?: number;
}

export type FetchLike = (input: string, init: RequestInit) => Promise<Response>;

/** What the last request said about the server, for the connection indicator. */
export type ConnectionStatus = "connecting" | "online" | "unreachable" | "unauthorized" | "refused";

const BASE_INIT = {
  method: "GET",
  cache: "no-store",
  credentials: "same-origin",
  redirect: "error",
  referrerPolicy: "no-referrer",
} as const satisfies RequestInit;

export class ReplayApiClient {
  readonly #fetch: FetchLike;
  readonly #listeners = new Set<() => void>();
  #token: string | null = null;
  /** The bootstrap in flight, shared by every caller that needs a token meanwhile. */
  #bootstrap: Promise<string> | null = null;
  #status: ConnectionStatus = "connecting";

  constructor(fetchImpl?: FetchLike) {
    this.#fetch = fetchImpl ?? ((input, init) => globalThis.fetch(input, init));
  }

  get hasAccess(): boolean {
    return this.#token !== null;
  }

  get status(): ConnectionStatus {
    return this.#status;
  }

  /** `useSyncExternalStore`-compatible subscription to {@link status}. */
  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  #setStatus(status: ConnectionStatus): void {
    if (status === this.#status) return;
    this.#status = status;
    for (const listener of this.#listeners) listener();
  }

  /**
   * Obtain this server instance's temporary token for this page. The custom header turns
   * any cross-origin attempt into a CORS preflight, which the server never approves.
   */
  async connect(signal?: AbortSignal): Promise<void> {
    await this.#freshToken(signal);
  }

  /**
   * A token from `/ui/bootstrap`, fetched at most once at a time: concurrent callers share
   * the request in flight. An abort rejects only the caller whose signal fired; if the
   * shared request was aborted by its starter, the other callers start a new one.
   */
  async #freshToken(signal: AbortSignal | undefined): Promise<string> {
    for (;;) {
      signal?.throwIfAborted();
      let pending = this.#bootstrap;
      if (pending === null) {
        pending = this.#fetchToken(signal);
        this.#bootstrap = pending;
      }
      try {
        return await untilAborted(pending, signal);
      } catch (error) {
        if (!isAbort(error) || signal?.aborted === true) throw error;
      }
    }
  }

  async #fetchToken(signal: AbortSignal | undefined): Promise<string> {
    try {
      const data = await this.#request(BOOTSTRAP_PATH, { [BOOTSTRAP_HEADER]: "bootstrap" }, signal);
      const { token } = parsed(parseBootstrap, data);
      this.#token = token;
      return token;
    } finally {
      this.#bootstrap = null;
    }
  }

  async health(signal?: AbortSignal): Promise<HealthResponse> {
    return parsed(parseHealth, await this.#request("/api/v1/health", {}, signal));
  }

  async project(signal?: AbortSignal): Promise<ProjectSummary> {
    return parsed(parseProject, await this.#authorized("/api/v1/project", signal));
  }

  async sessions(signal?: AbortSignal): Promise<SessionsResponse> {
    return parsed(parseSessions, await this.#authorized("/api/v1/sessions", signal));
  }

  async session(sessionId: string, signal?: AbortSignal): Promise<SessionDetail> {
    const data = await this.#authorized(sessionPath(sessionId), signal);
    const detail = parsed(parseSessionDetail, data);
    if (detail.session_id !== sessionId) throw new ApiError("invalid_response");
    return detail;
  }

  /** The session's forensic report: exactly what `agentbodycam analyze --json` returns. */
  async report(sessionId: string, signal?: AbortSignal): Promise<ForensicReport> {
    const data = await this.#authorized(`${sessionPath(sessionId)}/report`, signal);
    const report = parsed(parseForensicReport, data);
    if (report.session_id !== sessionId) throw new ApiError("invalid_response");
    return report;
  }

  /**
   * One page of the session's timeline: events with `sequence > afterSequence`, at most
   * `limit`, strictly ordered. The page must answer exactly the request that was sent.
   */
  async timeline(
    sessionId: string,
    { afterSequence = 0, limit = TIMELINE_PAGE_SIZE }: TimelineQuery = {},
    signal?: AbortSignal,
  ): Promise<TimelinePage> {
    if (!Number.isSafeInteger(afterSequence) || afterSequence < 0) {
      throw new ApiError("bad_request", null, "invalid_parameter");
    }
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > TIMELINE_MAX_LIMIT) {
      throw new ApiError("bad_request", null, "invalid_parameter");
    }
    const path = `${sessionPath(sessionId)}/timeline?after_sequence=${afterSequence}&limit=${limit}`;
    const page = parsed(parseTimelinePage, await this.#authorized(path, signal));
    if (
      page.session_id !== sessionId ||
      page.after_sequence !== afterSequence ||
      page.limit !== limit
    ) {
      throw new ApiError("invalid_response");
    }
    return page;
  }

  /**
   * A bearer-authenticated GET. A 401 means the token is missing, wrong or belongs to an
   * earlier server instance (tokens die with their server), so the token is dropped and the
   * request is retried exactly once with a fresh one; a second 401 is reported as
   * unauthorized. Requests rejected in parallel share one bootstrap, and a request whose
   * token was already replaced meanwhile retries with the replacement without
   * bootstrapping. Aborts propagate at every step.
   */
  async #authorized(path: string, signal: AbortSignal | undefined): Promise<unknown> {
    const token = this.#token ?? (await this.#freshToken(signal));
    signal?.throwIfAborted();
    try {
      return await this.#request(path, bearer(token), signal);
    } catch (error) {
      if (!(error instanceof ApiError && error.kind === "unauthorized")) throw error;
      if (this.#token === token) this.#token = null;
      signal?.throwIfAborted();
      const fresh = this.#token ?? (await this.#freshToken(signal));
      signal?.throwIfAborted();
      return this.#request(path, bearer(fresh), signal);
    }
  }

  async #request(
    path: string,
    headers: Record<string, string>,
    signal: AbortSignal | undefined,
  ): Promise<unknown> {
    let response: Response;
    try {
      response = await this.#fetch(path, {
        ...BASE_INIT,
        headers: { Accept: "application/json", ...headers },
        ...(signal === undefined ? {} : { signal }),
      });
    } catch (error) {
      if (isAbort(error)) throw error;
      this.#setStatus("unreachable");
      throw new ApiError("unavailable");
    }
    if (response.status === 401) this.#setStatus("unauthorized");
    else if (response.status === 403 && path === BOOTSTRAP_PATH) this.#setStatus("refused");
    else this.#setStatus("online");
    let body: unknown = null;
    try {
      body = await response.json();
    } catch (error) {
      if (isAbort(error)) throw error;
    }
    if (!response.ok) {
      throw new ApiError(kindForStatus(response.status), response.status, errorCode(body));
    }
    if (body === null) throw new ApiError("invalid_response", response.status);
    return body;
  }
}

/** Session IDs are checked before any request, so nothing else can reach the URL path. */
function sessionPath(sessionId: string): string {
  if (!isSessionId(sessionId)) throw new ApiError("bad_request", null, "invalid_session_id");
  return `/api/v1/sessions/${sessionId}`;
}

function bearer(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

/** `promise`, or a rejection with the signal's reason as soon as `signal` aborts. */
function untilAborted<T>(promise: Promise<T>, signal: AbortSignal | undefined): Promise<T> {
  if (signal === undefined) return promise;
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(signal.reason);
    promise.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(error);
      },
    );
    if (signal.aborted) onAbort();
    else signal.addEventListener("abort", onAbort, { once: true });
  });
}

function parsed<T>(parse: (value: unknown) => T, value: unknown): T {
  try {
    return parse(value);
  } catch (error) {
    if (error instanceof InvalidResponseError) throw new ApiError("invalid_response");
    throw error;
  }
}
