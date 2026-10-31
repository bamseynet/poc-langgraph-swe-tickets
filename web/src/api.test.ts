import { afterEach, expect, test, vi } from "vitest";
import { ApiError, createApi, parseSseBuffer } from "./api";

function mockFetch(response: Response) {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

test("encodes the slash and hash in a thread id", async () => {
  const fetchMock = mockFetch(Response.json({ thread_id: "x" }));
  await createApi(() => ({})).getTicket("github:octo/repo#1");
  expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/tickets/github%3Aocto%2Frepo%231");
});

test("sends the reviewer token as a bearer header", async () => {
  const fetchMock = mockFetch(Response.json([]));
  await createApi(() => ({ token: "secret" })).listTickets();
  const headers = new Headers(fetchMock.mock.calls[0]?.[1]?.headers);
  expect(headers.get("Authorization")).toBe("Bearer secret");
});

test("sends the actor with a decision in dev mode", async () => {
  const fetchMock = mockFetch(Response.json({ thread_id: "x" }));
  await createApi(() => ({ actor: "alice" })).decide("t", { action: "approve" });
  const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
  expect(body).toEqual({ action: "approve", actor: "alice" });
});

test("omits the actor when a token identifies the reviewer", async () => {
  const fetchMock = mockFetch(Response.json({ thread_id: "x" }));
  await createApi(() => ({ token: "secret", actor: "ignored" })).decide("t", { action: "approve" });
  const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
  expect(body).toEqual({ action: "approve" });
});

test("raises the server's message and status", async () => {
  mockFetch(Response.json({ detail: "Already decided" }, { status: 409 }));
  const error = await createApi(() => ({}))
    .decide("t", { action: "approve" })
    .catch((e: unknown) => e);
  expect(error).toBeInstanceOf(ApiError);
  expect((error as ApiError).status).toBe(409);
  expect((error as ApiError).message).toBe("Already decided");
});

test("reads a validation error list from the server", async () => {
  const detail = [{ msg: "Value error, reject requires a reason" }];
  mockFetch(Response.json({ detail }, { status: 422 }));
  const error = await createApi(() => ({}))
    .decide("t", { action: "reject" })
    .catch((e: unknown) => e);
  expect((error as ApiError).message).toContain("reject requires a reason");
});

test("survives a server error that is not JSON", async () => {
  mockFetch(new Response("Bad gateway", { status: 502 }));
  const error = await createApi(() => ({}))
    .listTickets()
    .catch((e: unknown) => e);
  expect((error as ApiError).status).toBe(502);
});

test("parses complete SSE messages and keeps the partial remainder", () => {
  const { events, rest } = parseSseBuffer('data: {"a":1}\n\n: ping\n\ndata: {"b":2}\n\ndata: {"c"');
  expect(events).toEqual([{ a: 1 }, { b: 2 }]);
  expect(rest).toBe('data: {"c"');
});

test("parses SSE messages with CRLF line endings", () => {
  const { events, rest } = parseSseBuffer('event: x\r\ndata: {"a":1}\r\n\r\n');
  expect(events).toEqual([{ a: 1 }]);
  expect(rest).toBe("");
});

test("ignores a malformed SSE message rather than breaking the stream", () => {
  const { events } = parseSseBuffer('data: not json\n\ndata: {"a":1}\n\n');
  expect(events).toEqual([{ a: 1 }]);
});
