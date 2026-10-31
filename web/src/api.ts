import type { AuditEvent, DecisionInput, ProgressEvent, Session, Ticket } from "./types";

export interface Auth {
  token?: string;
  actor?: string;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body: { detail?: unknown } = await response.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail.map((item: { msg?: string }) => item.msg ?? "Invalid").join("; ");
    }
  } catch {
    // Not JSON: fall through to a message built from the status.
  }
  return `Request failed with status ${response.status}`;
}

export function createApi(getAuth: () => Auth) {
  function headers(json: boolean): Headers {
    const result = new Headers();
    const { token } = getAuth();
    if (token) result.set("Authorization", `Bearer ${token}`);
    if (json) result.set("Content-Type", "application/json");
    return result;
  }

  async function request<T>(path: string, body?: unknown): Promise<T> {
    const init: RequestInit =
      body === undefined
        ? { headers: headers(false) }
        : { method: "POST", headers: headers(true), body: JSON.stringify(body) };
    const response = await fetch(path, init);
    if (!response.ok) throw new ApiError(response.status, await errorMessage(response));
    return (await response.json()) as T;
  }

  const ticketPath = (threadId: string) => `/api/tickets/${encodeURIComponent(threadId)}`;

  return {
    getSession: () => request<Session>("/api/session"),
    listTickets: () => request<Ticket[]>("/api/tickets"),
    getTicket: (threadId: string) => request<Ticket>(ticketPath(threadId)),
    ingest: () => request<{ new: string[]; errors: string[] }>("/api/ingest", {}),
    retry: (threadId: string) => request<Ticket>(`${ticketPath(threadId)}/retry`, {}),
    listAudit: () => request<AuditEvent[]>("/api/audit"),
    decide: (threadId: string, input: DecisionInput) => {
      const { token, actor } = getAuth();
      // A token identifies the reviewer on the server, so the client does not name anyone.
      const body = !token && actor ? { ...input, actor } : input;
      return request<Ticket>(`${ticketPath(threadId)}/decision`, body);
    },
    streamEvents: (signal: AbortSignal) =>
      fetch("/api/events", { headers: headers(false), signal }),
  };
}

export type Api = ReturnType<typeof createApi>;

/** Split a server-sent event buffer into parsed messages and the incomplete remainder. */
export function parseSseBuffer(buffer: string): { events: ProgressEvent[]; rest: string } {
  const normalised = buffer.replace(/\r\n/g, "\n");
  const blocks = normalised.split("\n\n");
  const rest = blocks.pop() ?? "";
  const events: ProgressEvent[] = [];
  for (const block of blocks) {
    const data = block
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim())
      .join("\n");
    if (!data) continue;
    try {
      events.push(JSON.parse(data) as ProgressEvent);
    } catch {
      // Skip a malformed message. One bad event should not end the stream.
    }
  }
  return { events, rest };
}
