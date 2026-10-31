import { useEffect, useRef } from "react";
import { parseSseBuffer, type Api } from "./api";
import type { ProgressEvent } from "./types";

const RECONNECT_MS = 2_000;

/** Follow the server's progress stream, reconnecting whenever it drops. */
export function useEvents(api: Api, enabled: boolean, onEvent: (event: ProgressEvent) => void) {
  const latest = useRef(onEvent);
  useEffect(() => {
    latest.current = onEvent;
  });

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();

    async function follow() {
      const decoder = new TextDecoder();
      while (!controller.signal.aborted) {
        try {
          // fetch rather than EventSource, because EventSource cannot send an auth header.
          const response = await api.streamEvents(controller.signal);
          const reader = response.body?.getReader();
          let buffer = "";
          while (reader) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const { events, rest } = parseSseBuffer(buffer);
            buffer = rest;
            events.forEach((event) => latest.current(event));
          }
        } catch {
          // Disconnected. Fall through to the delay and reconnect.
        }
        if (!controller.signal.aborted) {
          await new Promise((resolve) => setTimeout(resolve, RECONNECT_MS));
        }
      }
    }

    void follow();
    return () => controller.abort();
  }, [api, enabled]);
}
