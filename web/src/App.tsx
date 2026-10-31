import { useCallback, useEffect, useRef, useState } from "react";
import { describeProgress } from "./activity";
import { createApi, type Auth } from "./api";
import { AuditLog } from "./components/AuditLog";
import { QueueList } from "./components/QueueList";
import { TicketDetail } from "./components/TicketDetail";
import type { AuditEvent, DecisionInput, Session, Ticket } from "./types";
import { useEvents } from "./useEvents";

type Tab = "queue" | "audit";

const MAX_ACTIVITY = 30;

function readStored(key: string): string {
  try {
    return localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}

function writeStored(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Storage can be blocked. The value then lasts only as long as the page.
  }
}

// One client for the page's lifetime, so typing a name does not drop the event stream.
const auth: Auth = {
  actor: readStored("ticketgate.actor"),
  token: readStored("ticketgate.token"),
};
const api = createApi(() => auth);

export function App() {
  const [actor, setActor] = useState(auth.actor ?? "");
  const [token, setToken] = useState(auth.token ?? "");
  const [tokenDraft, setTokenDraft] = useState("");

  const [session, setSession] = useState<Session | null>(null);
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("queue");
  const [activity, setActivity] = useState<string[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [fetching, setFetching] = useState(false);

  // Events can trigger overlapping refreshes, and only the newest may update the page.
  const latestRefresh = useRef(0);
  const refresh = useCallback(async () => {
    const thisRefresh = ++latestRefresh.current;
    try {
      const [nextTickets, nextAudit] = await Promise.all([api.listTickets(), api.listAudit()]);
      if (thisRefresh !== latestRefresh.current) return;
      setTickets(nextTickets);
      setAudit(nextAudit);
      setLoadError(null);
    } catch (cause) {
      setLoadError(cause instanceof Error ? cause.message : "Could not reach the API.");
    }
  }, []);

  useEffect(() => {
    api
      .getSession()
      .then(setSession)
      .catch((cause: unknown) => {
        setLoadError(cause instanceof Error ? cause.message : "Could not reach the API.");
      });
    // The token changes who the server says we are, so read the session again after sign-in.
  }, [token]);

  const signedIn = session !== null && (!session.auth_required || Boolean(token));

  // Runs on load and after sign-in. Later updates arrive as events.
  useEffect(() => {
    if (!signedIn) return;
    let cancelled = false;
    void Promise.resolve().then(() => {
      if (!cancelled) void refresh();
    });
    return () => {
      cancelled = true;
    };
  }, [signedIn, refresh]);

  useEvents(api, signedIn, (event) => {
    const line = describeProgress(event);
    if (line) setActivity((current) => [line, ...current].slice(0, MAX_ACTIVITY));
    if (event.kind !== "node") void refresh();
  });

  async function fetchTickets() {
    setFetching(true);
    try {
      const { errors } = await api.ingest();
      await refresh();
      // Set after refresh, which clears errors. A broken source must not look like no tickets.
      if (errors.length > 0) setLoadError(`Could not read a ticket source: ${errors.join("; ")}`);
    } catch (cause) {
      setLoadError(cause instanceof Error ? cause.message : "Could not fetch tickets.");
    } finally {
      setFetching(false);
    }
  }

  async function decide(threadId: string, input: DecisionInput) {
    try {
      await api.decide(threadId, input);
    } finally {
      // Refresh on failure too: a 409 means someone else already decided.
      await refresh();
    }
  }

  async function retry(threadId: string) {
    try {
      await api.retry(threadId);
    } catch (cause) {
      setLoadError(cause instanceof Error ? cause.message : "Could not retry.");
    }
    await refresh();
  }

  const detail = useRef<HTMLDivElement>(null);
  function selectTicket(threadId: string) {
    setSelectedId(threadId);
    // On a narrow screen the detail sits below the whole queue.
    if (window.matchMedia("(max-width: 820px)").matches) {
      detail.current?.scrollIntoView({ block: "start" });
    }
  }

  const selected = tickets.find((ticket) => ticket.thread_id === selectedId) ?? null;
  const canDecide = session?.auth_required ? Boolean(token) : Boolean(actor.trim());

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <h1>Ticketgate</h1>
          {session && (
            <p className="mode">
              {session.llm_mode === "fake" ? "Fake LLM" : "Bedrock"} · {session.ticket_source}{" "}
              tickets
            </p>
          )}
        </div>
        <div className="identity">
          {session?.auth_required ? (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                auth.token = tokenDraft;
                setToken(tokenDraft);
                writeStored("ticketgate.token", tokenDraft);
                setTokenDraft("");
              }}
            >
              <label htmlFor="token">
                {session.actor ? `Signed in as ${session.actor}` : "Reviewer token"}
              </label>
              <div className="token-row">
                <input
                  id="token"
                  type="password"
                  autoComplete="off"
                  value={tokenDraft}
                  onChange={(event) => setTokenDraft(event.target.value)}
                />
                <button type="submit" disabled={!tokenDraft}>
                  Sign in
                </button>
              </div>
            </form>
          ) : (
            <>
              <label htmlFor="actor">Your name</label>
              <input
                id="actor"
                value={actor}
                onChange={(event) => {
                  auth.actor = event.target.value;
                  setActor(event.target.value);
                  writeStored("ticketgate.actor", event.target.value);
                }}
              />
            </>
          )}
        </div>
      </header>

      <div className="tabs" role="tablist" aria-label="Views">
        <button
          role="tab"
          aria-selected={tab === "queue"}
          onClick={() => setTab("queue")}
        >
          Approval queue
        </button>
        <button
          role="tab"
          aria-selected={tab === "audit"}
          onClick={() => setTab("audit")}
        >
          Audit log
        </button>
        <button
          className="fetch"
          disabled={fetching || !signedIn}
          onClick={() => void fetchTickets()}
        >
          {fetching ? "Fetching…" : "Fetch tickets"}
        </button>
      </div>

      {loadError && (
        <p className="error page-error" role="alert">
          {loadError}
        </p>
      )}

      {tab === "queue" ? (
        <main className="workspace">
          <aside className="sidebar">
            <QueueList tickets={tickets} selectedId={selectedId} onSelect={selectTicket} />
            {activity.length > 0 && (
              <section className="activity" aria-label="Live activity">
                <h2>Live activity</h2>
                <ol aria-live="polite">
                  {activity.map((line, index) => (
                    <li key={`${index}-${line}`}>{line}</li>
                  ))}
                </ol>
              </section>
            )}
          </aside>
          <div className="content" ref={detail}>
            {selected ? (
              <TicketDetail
                ticket={selected}
                canDecide={canDecide}
                onDecide={(input) => decide(selected.thread_id, input)}
                onRetry={() => void retry(selected.thread_id)}
              />
            ) : (
              <p className="empty">Select a ticket to review what the agent proposes.</p>
            )}
          </div>
        </main>
      ) : (
        <main className="audit-view">
          <AuditLog events={audit} />
        </main>
      )}
    </div>
  );
}
