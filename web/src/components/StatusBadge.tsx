import type { TicketStatus } from "../types";

const LABELS: Record<TicketStatus, string> = {
  queued: "Queued",
  running: "Working",
  awaiting_approval: "Needs approval",
  written_back: "Posted",
  rejected: "Rejected",
  failed: "Failed",
};

export function StatusBadge({ status }: { status: TicketStatus }) {
  return (
    <span className="status" data-status={status}>
      {LABELS[status]}
    </span>
  );
}
