import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { investigation, makeTicket } from "../test/fixtures";
import type { Ticket } from "../types";
import { TicketDetail } from "./TicketDetail";

function renderDetail(ticket: Ticket, onRetry = vi.fn(() => Promise.resolve())) {
  render(
    <TicketDetail ticket={ticket} onDecide={vi.fn()} onRetry={onRetry} canDecide />,
  );
  return { onRetry };
}

test("shows triage, plan, diff and the exact comment to be posted", () => {
  renderDetail(makeTicket());
  expect(screen.getByText("It crashes.")).toBeInTheDocument();
  expect(screen.getByText("billing")).toBeInTheDocument();
  expect(screen.getByText("The sum subtracts.")).toBeInTheDocument();
  expect(screen.getByText("Use plus.")).toBeInTheDocument();
  expect(screen.getByText("+ return a + b")).toBeInTheDocument();
  expect(screen.getByText("Proposed comment")).toBeInTheDocument();
  expect(screen.getByText("type:bug")).toBeInTheDocument();
});

test("confirms when the patch applies", () => {
  renderDetail(makeTicket());
  expect(screen.getByText(/applies cleanly/i)).toBeInTheDocument();
});

test("warns when the patch does not apply", () => {
  renderDetail(
    makeTicket({ investigation: { ...investigation, patch_applies: false } }),
  );
  expect(screen.getByText(/does not apply/i)).toBeInTheDocument();
});

test("states that no patch was proposed", () => {
  renderDetail(
    makeTicket({ investigation: { ...investigation, diff: null, patch_applies: null } }),
  );
  expect(screen.getByText(/no patch proposed/i)).toBeInTheDocument();
});

test("warns when the investigation stopped early", () => {
  renderDetail(makeTicket({ investigation: { ...investigation, complete: false } }));
  expect(screen.getByRole("alert")).toHaveTextContent(/incomplete/i);
});

test("lists the files the agent really read", () => {
  renderDetail(makeTicket());
  expect(screen.getByText(/read_file.*src\/app\.py/)).toBeInTheDocument();
});

test("shows a drafted reply for a question", () => {
  renderDetail(
    makeTicket({
      investigation: null,
      draft_reply: "Set DEFAULT_CURRENCY.",
      triage: { type: "question", severity: "low", component: "settings", rationale: "Asks." },
    }),
  );
  expect(screen.getByText("Set DEFAULT_CURRENCY.")).toBeInTheDocument();
});

test("offers decisions only while the ticket is waiting", () => {
  renderDetail(makeTicket({ status: "written_back" }));
  expect(screen.queryByRole("button", { name: /approve and post/i })).toBeNull();
});

test("shows who rejected and why", () => {
  renderDetail(
    makeTicket({
      status: "rejected",
      decision: {
        decision_id: "d",
        action: "reject",
        actor: "bob",
        reason: "Wrong root cause",
        edited_comment: null,
        edited_labels: null,
        decided_at: "2026-10-31T10:00:00Z",
      },
    }),
  );
  expect(screen.getByText(/bob/)).toBeInTheDocument();
  expect(screen.getByText(/Wrong root cause/)).toBeInTheDocument();
});

test("links to the comment after write-back", () => {
  renderDetail(
    makeTicket({
      status: "written_back",
      write_back: {
        comment_url: "https://github.com/octo/repo/issues/1#issuecomment-1",
        labels: ["type:bug"],
        deduplicated: false,
      },
    }),
  );
  expect(screen.getByRole("link", { name: /view posted comment/i })).toHaveAttribute(
    "href",
    "https://github.com/octo/repo/issues/1#issuecomment-1",
  );
});

test("a failed ticket shows the error and can be retried", async () => {
  const { onRetry } = renderDetail(
    makeTicket({ status: "failed", error: "GitHub is down" }),
  );
  expect(screen.getByRole("alert")).toHaveTextContent("GitHub is down");
  await userEvent.click(screen.getByRole("button", { name: /retry/i }));
  expect(onRetry).toHaveBeenCalled();
});

test("only links to http(s) ticket URLs", () => {
  renderDetail(makeTicket({ url: "javascript:alert(1)" }));
  expect(screen.queryByRole("link", { name: /open ticket/i })).toBeNull();
});


const rejection = {
  decision_id: "d",
  action: "reject" as const,
  actor: "bob",
  reason: "Wrong root cause",
  edited_comment: null,
  edited_labels: null,
  decided_at: "2026-10-31T10:00:00Z",
};

test("a failed ticket with a saved rejection says so and never reads as if it will post", () => {
  renderDetail(makeTicket({ status: "failed", error: "boom", decision: rejection }));
  expect(screen.getByText(/rejected by bob/i)).toBeInTheDocument();
  expect(screen.queryByText(/what will be posted/i)).toBeNull();
  expect(screen.queryByText("Proposed comment")).toBeNull();
});

test("a failed ticket with a saved approval shows who approved and that it has not posted", () => {
  renderDetail(
    makeTicket({
      status: "failed",
      error: "GitHub is down",
      decision: { ...rejection, action: "approve", actor: "alice", reason: null },
    }),
  );
  expect(screen.getByText(/approved by alice/i)).toBeInTheDocument();
  expect(screen.getByText(/not been posted/i)).toBeInTheDocument();
  expect(screen.getByText(/what was approved/i)).toBeInTheDocument();
});

test("retry cannot be sent twice while one is in flight", async () => {
  let finish: () => void = () => undefined;
  const onRetry = vi.fn(() => new Promise<void>((resolve) => (finish = resolve)));
  render(
    <TicketDetail
      ticket={makeTicket({ status: "failed", error: "boom" })}
      onDecide={vi.fn()}
      onRetry={onRetry}
      canDecide
    />,
  );
  const button = screen.getByRole("button", { name: /retry/i });
  await userEvent.click(button);
  expect(button).toBeDisabled();
  await userEvent.click(button);
  expect(onRetry).toHaveBeenCalledTimes(1);
  finish();
  await waitFor(() => expect(button).toBeEnabled());
});
