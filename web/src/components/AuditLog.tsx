import type { AuditEvent } from "../types";

function whatHappened(event: AuditEvent): string {
  const { reason, type, severity, component, comment_url: commentUrl, error } = event.detail;
  if (typeof reason === "string") return reason;
  if (typeof error === "string") return error;
  if (typeof commentUrl === "string") return commentUrl;
  if (type && severity) return `${String(type)}, ${String(severity)}, ${String(component)}`;
  if (event.action === "decision.approve") return "Posted as proposed";
  if (event.action === "decision.edit") return "Posted with edits";
  return "";
}

export function AuditLog({ events }: { events: AuditEvent[] }) {
  if (events.length === 0) {
    return <p className="empty">No decisions recorded yet.</p>;
  }
  return (
    <div className="table-scroll" tabIndex={0}>
      <table className="audit">
        <thead>
          <tr>
            <th scope="col">When</th>
            <th scope="col">Who</th>
            <th scope="col">What</th>
            <th scope="col">Ticket</th>
            <th scope="col">Detail</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event) => (
            <tr key={event.id}>
              <td>
                <time dateTime={event.at}>{new Date(event.at).toLocaleString()}</time>
              </td>
              <td>{event.actor}</td>
              <td>{event.action}</td>
              <td>{event.thread_id}</td>
              <td>{whatHappened(event)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
