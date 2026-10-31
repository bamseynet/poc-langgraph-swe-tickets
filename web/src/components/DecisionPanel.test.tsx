import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import type { DecisionInput, Proposal } from "../types";
import { DecisionPanel } from "./DecisionPanel";

const proposal: Proposal = { comment: "Proposed comment", labels: ["type:bug", "severity:high"] };

function setup(props: Partial<Parameters<typeof DecisionPanel>[0]> = {}) {
  const onDecide = vi.fn<(input: DecisionInput) => Promise<void>>().mockResolvedValue();
  render(<DecisionPanel proposal={proposal} onDecide={onDecide} canDecide {...props} />);
  return { onDecide, user: userEvent.setup() };
}

test("approve sends a plain approval", async () => {
  const { onDecide, user } = setup();
  await user.click(screen.getByRole("button", { name: /approve and post/i }));
  expect(onDecide).toHaveBeenCalledWith({ action: "approve" });
});

test("reject cannot be sent without a reason", async () => {
  const { onDecide, user } = setup();
  await user.click(screen.getByRole("button", { name: /^reject$/i }));
  const send = screen.getByRole("button", { name: /confirm rejection/i });
  expect(send).toBeDisabled();

  await user.type(screen.getByLabelText(/reason/i), "   ");
  expect(send).toBeDisabled();
  expect(onDecide).not.toHaveBeenCalled();
});

test("reject sends the reason", async () => {
  const { onDecide, user } = setup();
  await user.click(screen.getByRole("button", { name: /^reject$/i }));
  await user.type(screen.getByLabelText(/reason/i), "Wrong root cause");
  await user.click(screen.getByRole("button", { name: /confirm rejection/i }));
  expect(onDecide).toHaveBeenCalledWith({ action: "reject", reason: "Wrong root cause" });
});

test("edit starts from the proposal and sends the changed comment", async () => {
  const { onDecide, user } = setup();
  await user.click(screen.getByRole("button", { name: /^edit$/i }));
  const comment = screen.getByLabelText(/comment/i);
  expect(comment).toHaveValue("Proposed comment");

  await user.clear(comment);
  await user.type(comment, "Better words");
  await user.click(screen.getByRole("button", { name: /post edited version/i }));

  expect(onDecide).toHaveBeenCalledWith({
    action: "edit",
    edited_comment: "Better words",
    edited_labels: ["type:bug", "severity:high"],
  });
});

test("edit parses comma separated labels and can clear them", async () => {
  const { onDecide, user } = setup();
  await user.click(screen.getByRole("button", { name: /^edit$/i }));
  const labels = screen.getByLabelText(/labels/i);
  await user.clear(labels);
  await user.click(screen.getByRole("button", { name: /post edited version/i }));
  expect(onDecide).toHaveBeenCalledWith({
    action: "edit",
    edited_comment: "Proposed comment",
    edited_labels: [],
  });

  await user.type(labels, " type:bug , team:core ,");
  await user.click(screen.getByRole("button", { name: /post edited version/i }));
  expect(onDecide).toHaveBeenLastCalledWith({
    action: "edit",
    edited_comment: "Proposed comment",
    edited_labels: ["type:bug", "team:core"],
  });
});

test("edit cannot post an empty comment", async () => {
  const { user } = setup();
  await user.click(screen.getByRole("button", { name: /^edit$/i }));
  await user.clear(screen.getByLabelText(/comment/i));
  expect(screen.getByRole("button", { name: /post edited version/i })).toBeDisabled();
});

test("shows why a decision failed and allows another attempt", async () => {
  const onDecide = vi
    .fn<(input: DecisionInput) => Promise<void>>()
    .mockRejectedValueOnce(new Error("Already decided by someone else"))
    .mockResolvedValue();
  const { user } = setup({ onDecide });
  await user.click(screen.getByRole("button", { name: /approve and post/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Already decided by someone else");
  await waitFor(() =>
    expect(screen.getByRole("button", { name: /approve and post/i })).toBeEnabled(),
  );
});

test("cannot double submit while a decision is in flight", async () => {
  let finish: () => void = () => undefined;
  const onDecide = vi.fn(() => new Promise<void>((resolve) => (finish = resolve)));
  const { user } = setup({ onDecide });
  const approve = screen.getByRole("button", { name: /approve and post/i });
  await user.click(approve);
  expect(approve).toBeDisabled();
  await user.click(approve);
  expect(onDecide).toHaveBeenCalledTimes(1);
  finish();
  await waitFor(() => expect(approve).toBeEnabled());
});

test("every action is unavailable without a reviewer identity", () => {
  setup({ canDecide: false });
  for (const name of [/approve and post/i, /^edit$/i, /^reject$/i]) {
    expect(screen.getByRole("button", { name })).toBeDisabled();
  }
  expect(screen.getByText(/enter your name/i)).toBeInTheDocument();
});
