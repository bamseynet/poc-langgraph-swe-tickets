import { renderHook } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { Api } from "./api";
import { useEvents } from "./useEvents";

test("a refused token stops the stream instead of retrying forever", async () => {
  vi.useFakeTimers();
  const streamEvents = vi.fn().mockResolvedValue(new Response("no", { status: 401 }));
  const api = { streamEvents } as unknown as Api;
  renderHook(() => useEvents(api, true, vi.fn(), vi.fn()));
  await vi.advanceTimersByTimeAsync(120_000);
  expect(streamEvents).toHaveBeenCalledTimes(1);
  vi.useRealTimers();
});

test("other failures are retried with a growing delay", async () => {
  vi.useFakeTimers();
  const streamEvents = vi.fn().mockResolvedValue(new Response("down", { status: 503 }));
  const api = { streamEvents } as unknown as Api;
  renderHook(() => useEvents(api, true, vi.fn(), vi.fn()));
  await vi.advanceTimersByTimeAsync(2_000 + 4_000 + 8_000 + 100);
  expect(streamEvents).toHaveBeenCalledTimes(4);
  vi.useRealTimers();
});
