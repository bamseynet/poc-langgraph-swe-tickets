import type { Investigation, Ticket } from "../types";

export const DIFF = `--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
`;

export const investigation: Investigation = {
  summary: "The sum subtracts.",
  plan: ["Use plus.", "Add a test."],
  diff: DIFF,
  patch_applies: true,
  complete: true,
  tool_calls: [{ tool: "read_file", args: { path: "src/app.py" } }],
};

export function makeTicket(overrides: Partial<Ticket> = {}): Ticket {
  return {
    thread_id: "github:octo/repo#1",
    title: "Totals are wrong",
    url: "https://github.com/octo/repo/issues/1",
    status: "awaiting_approval",
    triage: { type: "bug", severity: "high", component: "billing", rationale: "It crashes." },
    draft_reply: null,
    investigation,
    proposal: { comment: "Proposed comment", labels: ["type:bug", "severity:high"] },
    decision: null,
    write_back: null,
    error: null,
    ...overrides,
  };
}
