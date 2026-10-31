import type { Ticket, TicketStatus } from "../types";
import { StatusBadge } from "./StatusBadge";

const ORDER: Record<TicketStatus, number> = {
  awaiting_approval: 0,
  failed: 1,
  running: 2,
  queued: 3,
  written_back: 4,
  rejected: 4,
};

interface Props {
  tickets: Ticket[];
  selectedId: string | null;
  onSelect: (threadId: string) => void;
}

export function QueueList({ tickets, selectedId, onSelect }: Props) {
  if (tickets.length === 0) {
    return <p className="empty">No tickets yet. Use Fetch tickets to poll the source.</p>;
  }

  const sorted = [...tickets].sort((a, b) => ORDER[a.status] - ORDER[b.status]);
  const waiting = tickets.filter((ticket) => ticket.status === "awaiting_approval").length;

  return (
    <nav aria-label="Tickets">
      <p className="queue-count">{waiting} waiting</p>
      <ul className="queue">
        {sorted.map((ticket) => (
          <li key={ticket.thread_id}>
            <button
              className="queue-item"
              aria-current={ticket.thread_id === selectedId ? "true" : undefined}
              onClick={() => onSelect(ticket.thread_id)}
            >
              <span className="queue-title">{ticket.title}</span>
              <span className="queue-meta">
                <StatusBadge status={ticket.status} />
                {ticket.triage && (
                  <>
                    <span className="tag">{ticket.triage.type}</span>
                    <span className="tag" data-severity={ticket.triage.severity}>
                      {ticket.triage.severity}
                    </span>
                  </>
                )}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
