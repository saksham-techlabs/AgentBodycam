import { ApiError } from "../api/errors";

export interface ErrorMessage {
  title: string;
  detail: string;
}

/**
 * Human-readable text for a failed request. Written here, from the error kind and code
 * only: backend messages, stack traces and exception text are never shown.
 */
export function describeError(error: unknown): ErrorMessage {
  if (!(error instanceof ApiError)) {
    return {
      title: "Something went wrong",
      detail: "The Replay UI hit an unexpected problem. Reload the page to try again.",
    };
  }
  switch (error.kind) {
    case "unavailable":
      return {
        title: "Replay server unavailable",
        detail:
          "The AgentBodycam server that served this page is not responding. It may have " +
          "been stopped. Run `agentbodycam replay` again and open the address it prints.",
      };
    case "unauthorized":
      return {
        title: "Access expired",
        detail:
          "The server rejected this page's temporary access. Access ends when the server " +
          "stops and is never saved. Reload the page to reconnect.",
      };
    case "forbidden":
      return {
        title: "Access refused",
        detail:
          "The server only grants access to the Replay UI page it served itself. Open the " +
          "address printed by `agentbodycam replay` directly in your browser.",
      };
    case "not_found":
      return {
        title: "Session not found",
        detail: "This session is no longer in the project's AgentBodycam store.",
      };
    case "bad_request":
      return error.code === "invalid_session_id"
        ? {
            title: "Invalid session address",
            detail: "That is not a valid AgentBodycam session ID.",
          }
        : {
            title: "Request rejected",
            detail: "The server rejected the request as malformed.",
          };
    case "server":
      if (error.code === "session_unreadable") {
        return {
          title: "Session unreadable",
          detail:
            "This session's stored evidence could not be read; it may be corrupt or " +
            "incomplete. Other sessions are unaffected.",
        };
      }
      if (error.code === "store_unsafe") {
        return {
          title: "Store refused",
          detail:
            "The project's .agentbodycam store is not a plain directory inside the " +
            "project (symlink, junction or unexpected file), so the server will not read it.",
        };
      }
      return {
        title: "Server error",
        detail: "The server could not complete the request. Try refreshing.",
      };
    case "invalid_response":
      return {
        title: "Unexpected response",
        detail:
          "The server's reply did not match the Replay API this UI expects. The UI and the " +
          "server may come from different AgentBodycam versions.",
      };
  }
}
