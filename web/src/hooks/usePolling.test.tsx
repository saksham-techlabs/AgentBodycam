import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePolling } from "./usePolling";

function Poller({ task, enabled }: { task: () => Promise<unknown>; enabled: boolean }) {
  usePolling(task, enabled, 1000);
  return null;
}

let hidden = false;

beforeEach(() => {
  vi.useFakeTimers();
  hidden = false;
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
});

afterEach(() => {
  vi.useRealTimers();
});

function setHidden(value: boolean): void {
  hidden = value;
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("usePolling", () => {
  it("runs on an interval while enabled", async () => {
    const task = vi.fn(() => Promise.resolve());
    render(<Poller task={task} enabled />);
    expect(task).not.toHaveBeenCalled();
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(task).toHaveBeenCalledTimes(3);
  });

  it("does nothing while disabled", async () => {
    const task = vi.fn(() => Promise.resolve());
    render(<Poller task={task} enabled={false} />);
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(task).not.toHaveBeenCalled();
  });

  it("stops when unmounted", async () => {
    const task = vi.fn(() => Promise.resolve());
    const view = render(<Poller task={task} enabled />);
    await act(() => vi.advanceTimersByTimeAsync(1000));
    view.unmount();
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(task).toHaveBeenCalledTimes(1);
  });

  it("stops when disabled (e.g. the session stopped or changed)", async () => {
    const task = vi.fn(() => Promise.resolve());
    const view = render(<Poller task={task} enabled />);
    await act(() => vi.advanceTimersByTimeAsync(1000));
    view.rerender(<Poller task={task} enabled={false} />);
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(task).toHaveBeenCalledTimes(1);
  });

  it("pauses while the page is hidden", async () => {
    const task = vi.fn(() => Promise.resolve());
    render(<Poller task={task} enabled />);
    setHidden(true);
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(task).not.toHaveBeenCalled();
    setHidden(false);
    await act(() => vi.advanceTimersByTimeAsync(1000));
    expect(task).toHaveBeenCalledTimes(1);
  });

  it("never overlaps requests", async () => {
    let resolve: () => void = () => undefined;
    const task = vi.fn(() => new Promise<void>((r) => (resolve = r)));
    render(<Poller task={task} enabled />);
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(task).toHaveBeenCalledTimes(1);
    await act(async () => resolve());
    await act(() => vi.advanceTimersByTimeAsync(1000));
    expect(task).toHaveBeenCalledTimes(2);
  });
});
