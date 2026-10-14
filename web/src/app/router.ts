/**
 * Minimal client-side routing over the History API (no router dependency):
 *
 *   /                      session dashboard
 *   /session/<session-id>  one session
 *
 * The server answers exactly these paths with the UI document. Only session IDs ever
 * appear in the URL; the access token never does.
 */

import { useCallback, useSyncExternalStore } from "react";

import { isSessionId } from "../api/validate";

export type Route =
  | { name: "sessions" }
  | { name: "session"; sessionId: string }
  | { name: "not_found" };

const NAVIGATE_EVENT = "agentbodycam:navigate";

export function parseRoute(pathname: string): Route {
  if (pathname === "/") return { name: "sessions" };
  const match = /^\/session\/([^/]+)$/.exec(pathname);
  if (match?.[1] !== undefined && isSessionId(match[1])) {
    return { name: "session", sessionId: match[1] };
  }
  return { name: "not_found" };
}

export function sessionPath(sessionId: string): string {
  return `/session/${sessionId}`;
}

export function navigate(path: string): void {
  if (path === window.location.pathname) return;
  window.history.pushState(null, "", path);
  window.dispatchEvent(new Event(NAVIGATE_EVENT));
}

function subscribe(listener: () => void): () => void {
  window.addEventListener("popstate", listener);
  window.addEventListener(NAVIGATE_EVENT, listener);
  return () => {
    window.removeEventListener("popstate", listener);
    window.removeEventListener(NAVIGATE_EVENT, listener);
  };
}

function pathname(): string {
  return window.location.pathname;
}

export function useRoute(): [Route, (path: string) => void] {
  const current = useSyncExternalStore(subscribe, pathname);
  const go = useCallback((path: string) => navigate(path), []);
  return [parseRoute(current), go];
}
