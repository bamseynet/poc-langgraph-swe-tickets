import { expect, test } from "vitest";
import { describeProgress } from "./activity";
import type { ProgressEvent } from "./types";

function event(overrides: Partial<ProgressEvent>): ProgressEvent {
  return { thread_id: "t#1", kind: "node", node: null, detail: {}, ...overrides };
}

test("names the tools the agent called", () => {
  const detail = { tool_calls: [{ tool: "read_file", args: {} }, { tool: "search_code", args: {} }] };
  expect(describeProgress(event({ node: "agent", detail }))).toBe(
    "t#1: read_file, search_code",
  );
});

test("hides the concluding tool call, which the next step reports", () => {
  const detail = { tool_calls: [{ tool: "submit_investigation", args: {} }] };
  expect(describeProgress(event({ node: "agent", detail }))).toBeNull();
});

test.each([
  ["triage", "t#1: triaged"],
  ["investigate", "t#1: investigation finished"],
  ["draft_reply", "t#1: reply drafted"],
  ["write_back", "t#1: posted to the ticket"],
])("describes the %s step", (node, expected) => {
  expect(describeProgress(event({ node }))).toBe(expected);
});

test.each(["start", "tools", "nudge", "finish", "give_up", "prepare_proposal", "record_decision"])(
  "hides the internal %s step",
  (node) => {
    expect(describeProgress(event({ node }))).toBeNull();
  },
);

test("reports the end of a run", () => {
  expect(describeProgress(event({ kind: "awaiting_approval" }))).toBe("t#1: waiting for approval");
  expect(describeProgress(event({ kind: "failed" }))).toBe("t#1: failed");
  expect(describeProgress(event({ kind: "finished" }))).toBe("t#1: finished");
});
