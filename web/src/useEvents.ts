import { useEffect, useRef } from "react";
import { parseSseBuffer, type Api } from "./api";
import type { ProgressEvent } from "./types";

const RECONNECT_MS = 2_000;
const MAX_RECONNECT_MS = 30_000;

/** Follow the server's progress stream, reconnecting whenever it drops. */
export function useEvents(
  api: Api,
  enabled: boolean,
  onEvent: (event: ProgressEvent) => void,
  onConnect: () => void,
) {
  const latest = useRef({ onEvent, onConnect });
  useEffect(() => {
    latest.current = { onEvent, onConnect };
  });

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();

    async function follow() {
      const decoder = new TextDecoder();
      let delay = RECONNECT_MS;
      while (!controller.signal.aborted) {
        try {
          // fetch rather than EventSource, because EventSource cannot send an auth header.
          const response = await api.streamEvents(controller.signal);
          // A refused token will not start working by being retried.
          if (response.status === 401 || response.status === 403) return;
          if (!response.ok) throw new Error(`Event stream refused: ${response.status}`);
          delay = RECONNECT_MS;
          // Anything could have changed while disconnected, and those events are gone.
          latest.current.onConnect();
          const reader = response.body?.getReader();
          let buffer = "";
          while (reader) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const { events, rest } = parseSseBuffer(buffer);
            buffer = rest;
            events.forEach((event) => latest.current.onEvent(event));
          }
        } catch {
          // Disconnected. Fall through to the delay and reconnect.
        }
        if (!controller.signal.aborted) {
          await new Promise((resolve) => setTimeout(resolve, delay));
          delay = Math.min(delay * 2, MAX_RECONNECT_MS);
        }
      }
    }

    void follow();
    return () => controller.abort();
  }, [api, enabled]);
}
