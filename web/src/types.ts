// Mirrors the API's JSON. See src/ticketgate/service.py and models.py.

export type TicketStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
  | "written_back"
  | "rejected"
  | "failed";

export interface Triage {
  type: "bug" | "feature" | "chore" | "question";
  severity: "low" | "medium" | "high" | "critical";
  component: string;
  rationale: string;
}

export interface ToolCall {
  tool: string;
  args: Record<string, unknown>;
}

export interface Investigation {
  summary: string;
  plan: string[];
  diff: string | null;
  patch_applies: boolean | null;
  complete: boolean;
  tool_calls: ToolCall[];
}

export interface Proposal {
  comment: string;
  labels: string[];
}

export interface Decision {
  decision_id: string;
  action: "approve" | "edit" | "reject";
  actor: string;
  reason: string | null;
  edited_comment: string | null;
  edited_labels: string[] | null;
  decided_at: string;
}

export interface WriteBack {
  comment_url: string | null;
  labels: string[];
  deduplicated: boolean;
}

export interface Ticket {
  thread_id: string;
  title: string;
  url: string;
  status: TicketStatus;
  triage: Triage | null;
  draft_reply: string | null;
  investigation: Investigation | null;
  proposal: Proposal | null;
  decision: Decision | null;
  write_back: WriteBack | null;
  error: string | null;
}

export interface DecisionInput {
  action: Decision["action"];
  reason?: string;
  edited_comment?: string;
  edited_labels?: string[];
}

export interface AuditEvent {
  id: number;
  at: string;
  actor: string;
  action: string;
  thread_id: string;
  detail: Record<string, unknown>;
}

export interface Session {
  auth_required: boolean;
  actor: string | null;
  llm_mode: string;
  ticket_source: string;
}

export interface ProgressEvent {
  thread_id: string;
  kind: "node" | "awaiting_approval" | "finished" | "failed" | "resync";
  node: string | null;
  detail: Record<string, unknown>;
}
