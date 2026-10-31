import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { makeTicket } from "../test/fixtures";
import { QueueList } from "./QueueList";

const waiting = makeTicket({ thread_id: "a", title: "Waiting" });
const done = makeTicket({ thread_id: "b", title: "Finished", status: "written_back" });
const rejected = makeTicket({ thread_id: "c", title: "Turned down", status: "rejected" });

test("puts tickets needing a decision first, whatever order they arrive in", () => {
  render(<QueueList tickets={[done, rejected, waiting]} selectedId={null} onSelect={vi.fn()} />);
  const items = screen.getAllByRole("button").map((b) => b.textContent);
  expect(items[0]).toContain("Waiting");
});

test("shows each ticket's status and triage", () => {
  render(<QueueList tickets={[waiting]} selectedId={null} onSelect={vi.fn()} />);
  const item = screen.getByRole("button", { name: /waiting/i });
  expect(within(item).getByText(/needs approval/i)).toBeInTheDocument();
  expect(within(item).getByText("bug")).toBeInTheDocument();
  expect(within(item).getByText("high")).toBeInTheDocument();
});

test("reports how many tickets are waiting", () => {
  render(<QueueList tickets={[waiting, done]} selectedId={null} onSelect={vi.fn()} />);
  expect(screen.getByText(/1 waiting/i)).toBeInTheDocument();
});

test("selecting a ticket reports its id", async () => {
  const onSelect = vi.fn();
  render(<QueueList tickets={[waiting]} selectedId={null} onSelect={onSelect} />);
  await userEvent.click(screen.getByRole("button", { name: /waiting/i }));
  expect(onSelect).toHaveBeenCalledWith("a");
});

test("marks the selected ticket for assistive technology", () => {
  render(<QueueList tickets={[waiting, done]} selectedId="a" onSelect={vi.fn()} />);
  expect(screen.getByRole("button", { name: /waiting/i })).toHaveAttribute("aria-current", "true");
  expect(screen.getByRole("button", { name: /finished/i })).not.toHaveAttribute("aria-current");
});

test("a ticket still being processed has no triage yet", () => {
  const running = makeTicket({ status: "running", triage: null, title: "Busy" });
  render(<QueueList tickets={[running]} selectedId={null} onSelect={vi.fn()} />);
  expect(screen.getByRole("button", { name: /busy/i })).toHaveTextContent(/working/i);
});

test("explains the empty queue", () => {
  render(<QueueList tickets={[]} selectedId={null} onSelect={vi.fn()} />);
  expect(screen.getByText(/no tickets yet/i)).toBeInTheDocument();
});
