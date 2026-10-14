import { useEffect, useState } from "react";

const QUERY = "(prefers-reduced-motion: reduce)";

function query(): MediaQueryList | null {
  return typeof window.matchMedia === "function" ? window.matchMedia(QUERY) : null;
}

/** Whether the user asked for reduced motion; follows changes to the OS setting. */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => query()?.matches ?? false);
  useEffect(() => {
    const list = query();
    if (list === null) return undefined;
    const onChange = (): void => setReduced(list.matches);
    onChange();
    // Older engines only implement the deprecated addListener/removeListener pair.
    if (typeof list.addEventListener === "function") {
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    }
    if (typeof list.addListener === "function") {
      list.addListener(onChange);
      return () => list.removeListener(onChange);
    }
    return undefined;
  }, []);
  return reduced;
}
