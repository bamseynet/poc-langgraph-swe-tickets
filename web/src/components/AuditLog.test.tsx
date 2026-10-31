import { render, screen } from "@testing-library/react";
import type { AuditEvent } from "../types";
import { AuditLog } from "./AuditLog";

const events: AuditEvent[] = [
  {
    id: 2,
    at: "2026-10-31T10:05:00Z",
    actor: "bob",
    action: "decision.reject",
    thread_id: "github:octo/repo#1",
    detail: { reason: "Wrong root cause" },
  },
  {
    id: 1,
    at: "2026-10-31T10:00:00Z",
    actor: "agent:triage",
    action: "triage.classified",
    thread_id: "github:octo/repo#1",
    detail: { type: "bug" },
  },
];

test("shows who, when and what for each event", () => {
  render(<AuditLog events={events} />);
  expect(screen.getByText("bob")).toBeInTheDocument();
  expect(screen.getByText("agent:triage")).toBeInTheDocument();
  expect(screen.getByText("decision.reject")).toBeInTheDocument();
  expect(screen.getAllByText("github:octo/repo#1")).toHaveLength(2);
  const time = screen.getAllByRole("time")[0];
  expect(time).toHaveAttribute("datetime", "2026-10-31T10:05:00Z");
});

test("surfaces the rejection reason", () => {
  render(<AuditLog events={events} />);
  expect(screen.getByText(/Wrong root cause/)).toBeInTheDocument();
});

test.each([
  ["decision.approve", "Posted as proposed"],
  ["decision.edit", "Posted with edits"],
])("says what %s meant", (action, expected) => {
  render(
    <AuditLog
      events={[
        { id: 1, at: "2026-10-31T10:00:00Z", actor: "a", action, thread_id: "t", detail: {} },
      ]}
    />,
  );
  expect(screen.getByText(expected)).toBeInTheDocument();
});

test("explains an empty log", () => {
  render(<AuditLog events={[]} />);
  expect(screen.getByText(/no decisions recorded/i)).toBeInTheDocument();
});
