/**
 * Failures of Replay API calls, reduced to what the UI needs to react: a kind, the HTTP
 * status and the backend's stable error code. Backend message text is never kept, so it
 * can never be rendered.
 */

export type ApiErrorKind =
  | "bad_request" // 400
  | "unauthorized" // 401: missing, wrong or expired token
  | "forbidden" // 403: origin/host refused, or the UI bootstrap was refused
  | "not_found" // 404: unknown route or session
  | "server" // 5xx: e.g. an unreadable session
  | "unavailable" // the server could not be reached at all
  | "invalid_response"; // a reply that is not what the API promises

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly code: string | null;

  constructor(kind: ApiErrorKind, status: number | null = null, code: string | null = null) {
    super(`Replay API request failed (${kind}${status === null ? "" : `, HTTP ${status}`})`);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.code = code;
  }
}

export function kindForStatus(status: number): ApiErrorKind {
  if (status === 400) return "bad_request";
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status >= 500) return "server";
  return "invalid_response";
}

export function isAbort(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { name?: unknown }).name === "AbortError"
  );
}
