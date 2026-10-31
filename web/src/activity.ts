import type { ProgressEvent } from "./types";

const STEPS: Record<string, string> = {
  triage: "triaged",
  investigate: "investigation finished",
  draft_reply: "reply drafted",
  approval_gate: "decision received",
  write_back: "posted to the ticket",
  record_rejection: "rejection recorded",
};

const OUTCOMES: Partial<Record<ProgressEvent["kind"], string>> = {
  awaiting_approval: "waiting for approval",
  failed: "failed",
  finished: "finished",
};

/** One line for the activity feed, or null for steps only a developer would care about. */
export function describeProgress(event: ProgressEvent): string | null {
  const calls = event.detail.tool_calls;
  let text: string | undefined = OUTCOMES[event.kind];
  if (Array.isArray(calls)) {
    const tools = calls
      .map((call: { tool: string }) => call.tool)
      .filter((tool) => tool !== "submit_investigation");
    text = tools.join(", ");
  } else if (event.kind === "node" && event.node) {
    text = STEPS[event.node];
  }
  return text ? `${event.thread_id}: ${text}` : null;
}
