import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { makeTicket } from "./test/fixtures";

interface Backend {
  authRequired: boolean;
  goodToken: string;
  sessionFailures: number;
  streams: number;
  ticketFetches: number;
  closeStream: (() => void)[];
  retryFails: boolean;
}

let backend: Backend;

function json(body: unknown, status = 200) {
  return Response.json(body, { status });
}

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const path = String(input);
  const authorized = new Headers(init?.headers).get("Authorization") === `Bearer ${backend.goodToken}`;
  const allowed = !backend.authRequired || authorized;

  if (path === "/api/session") {
    if (backend.sessionFailures > 0) {
      backend.sessionFailures -= 1;
      return Promise.reject(new TypeError("Failed to fetch"));
    }
    return Promise.resolve(
      json({
        auth_required: backend.authRequired,
        actor: backend.authRequired && authorized ? "alice" : null,
        llm_mode: "fake",
        ticket_source: "demo",
      }),
    );
  }
  if (!allowed) return Promise.resolve(json({ detail: "A valid reviewer token is required" }, 401));
  if (path === "/api/events") {
    backend.streams += 1;
    const body = new ReadableStream({
      start(controller) {
        backend.closeStream.push(() => controller.close());
      },
    });
    return Promise.resolve(new Response(body));
  }
  if (path === "/api/tickets") {
    backend.ticketFetches += 1;
    const failed = makeTicket({ status: "failed", error: "boom" });
    return Promise.resolve(json(backend.retryFails ? [failed] : [makeTicket()]));
  }
  if (path === "/api/audit") return Promise.resolve(json([]));
  if (path.endsWith("/retry")) return Promise.resolve(json({ detail: "Cannot retry now" }, 409));
  return Promise.resolve(json(makeTicket()));
}

async function renderApp() {
  // The API client reads stored credentials when the module loads, as on a page load.
  vi.resetModules();
  const { App } = await import("./App");
  render(<App />);
}

beforeEach(() => {
  localStorage.clear();
  backend = {
    authRequired: false,
    goodToken: "good",
    sessionFailures: 0,
    streams: 0,
    ticketFetches: 0,
    closeStream: [],
    retryFails: false,
  };
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
  stubScreenWidth(false);
});

// jsdom has no layout, so it implements neither of these.
const scrollIntoView = vi.fn();
function stubScreenWidth(narrow: boolean) {
  vi.stubGlobal("matchMedia", () => ({ matches: narrow }));
  Element.prototype.scrollIntoView = scrollIntoView;
  scrollIntoView.mockClear();
}

afterEach(() => vi.unstubAllGlobals());

test("signing in after a bad stored token loads the queue and clears the error", async () => {
  backend.authRequired = true;
  localStorage.setItem("ticketgate.token", "stale");
  await renderApp();
  expect(await screen.findByRole("alert")).toHaveTextContent(/valid reviewer token/i);

  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/reviewer token|signed in/i), "good");
  await user.click(screen.getByRole("button", { name: /sign in/i }));

  expect(await screen.findByText("Totals are wrong")).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(screen.getByText(/signed in as alice/i)).toBeInTheDocument();
});

test("the queue refreshes when the event stream reconnects", async () => {
  await renderApp();
  await screen.findByText("Totals are wrong");
  await waitFor(() => expect(backend.streams).toBe(1));
  const before = backend.ticketFetches;

  backend.closeStream[0]?.();

  await waitFor(() => expect(backend.streams).toBe(2), { timeout: 4_000 });
  await waitFor(() => expect(backend.ticketFetches).toBeGreaterThan(before));
});

test("recovers by itself if the API was not up when the page loaded", async () => {
  backend.sessionFailures = 1;
  await renderApp();
  expect(await screen.findByText("Totals are wrong", {}, { timeout: 5_000 })).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
});

test("a token left over from an auth deployment does not block decisions", async () => {
  localStorage.setItem("ticketgate.token", "left-over");
  localStorage.setItem("ticketgate.actor", "priya");
  await renderApp();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /totals are wrong/i }));
  await user.click(screen.getByRole("button", { name: /approve and post/i }));

  const decision = vi
    .mocked(fetch)
    .mock.calls.find(([path]) => String(path).endsWith("/decision"));
  expect(JSON.parse(String(decision?.[1]?.body))).toEqual({ action: "approve", actor: "priya" });
});

test("signing out forgets the token", async () => {
  backend.authRequired = true;
  localStorage.setItem("ticketgate.token", "good");
  await renderApp();
  await screen.findByText("Totals are wrong");

  await userEvent.click(screen.getByRole("button", { name: /sign out/i }));

  expect(localStorage.getItem("ticketgate.token")).toBeNull();
  await waitFor(() => expect(screen.queryByText("Totals are wrong")).toBeNull());
});

test("a retry error stays visible after the refresh that follows it", async () => {
  backend.retryFails = true;
  await renderApp();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /totals are wrong/i }));
  await user.click(screen.getByRole("button", { name: /retry/i }));
  await waitFor(() => expect(screen.getByText("Cannot retry now")).toBeInTheDocument());
});

test("selecting a ticket on a narrow screen brings its detail into view", async () => {
  stubScreenWidth(true);
  await renderApp();
  await userEvent.click(await screen.findByRole("button", { name: /totals are wrong/i }));
  expect(scrollIntoView).toHaveBeenCalled();
});

test("selecting a ticket on a wide screen does not move the page", async () => {
  await renderApp();
  await userEvent.click(await screen.findByRole("button", { name: /totals are wrong/i }));
  expect(scrollIntoView).not.toHaveBeenCalled();
});
