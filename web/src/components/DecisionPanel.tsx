import { useState } from "react";
import type { DecisionInput, Proposal } from "../types";

type Mode = "choose" | "edit" | "reject";

interface Props {
  proposal: Proposal;
  onDecide: (input: DecisionInput) => Promise<void>;
  canDecide: boolean;
}

function parseLabels(text: string): string[] {
  return text
    .split(",")
    .map((label) => label.trim())
    .filter(Boolean);
}

export function DecisionPanel({ proposal, onDecide, canDecide }: Props) {
  const [mode, setMode] = useState<Mode>("choose");
  const [reason, setReason] = useState("");
  const [comment, setComment] = useState(proposal.comment);
  const [labels, setLabels] = useState(proposal.labels.join(", "));
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send(input: DecisionInput) {
    setPending(true);
    setError(null);
    try {
      await onDecide(input);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The decision could not be saved.");
    } finally {
      setPending(false);
    }
  }

  const disabled = pending || !canDecide;

  return (
    <section className="decision" aria-label="Your decision">
      {!canDecide && <p className="hint">Enter your name at the top of the page to decide.</p>}

      {mode === "choose" && (
        <div className="actions">
          <button
            className="primary"
            disabled={disabled}
            onClick={() => void send({ action: "approve" })}
          >
            Approve and post
          </button>
          <button disabled={disabled} onClick={() => setMode("edit")}>
            Edit
          </button>
          <button className="danger" disabled={disabled} onClick={() => setMode("reject")}>
            Reject
          </button>
        </div>
      )}

      {mode === "edit" && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void send({
              action: "edit",
              edited_comment: comment,
              edited_labels: parseLabels(labels),
            });
          }}
        >
          <label htmlFor="edited-comment">Comment</label>
          <textarea
            id="edited-comment"
            rows={12}
            value={comment}
            onChange={(event) => setComment(event.target.value)}
          />
          <label htmlFor="edited-labels">Labels, separated by commas</label>
          <input
            id="edited-labels"
            value={labels}
            onChange={(event) => setLabels(event.target.value)}
          />
          <div className="actions">
            <button className="primary" type="submit" disabled={disabled || !comment.trim()}>
              Post edited version
            </button>
            <button type="button" disabled={pending} onClick={() => setMode("choose")}>
              Cancel
            </button>
          </div>
        </form>
      )}

      {mode === "reject" && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void send({ action: "reject", reason: reason.trim() });
          }}
        >
          <label htmlFor="reject-reason">Reason for rejecting</label>
          <textarea
            id="reject-reason"
            rows={3}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
          <p className="hint">The reason goes in the audit log. Nothing is posted to the ticket.</p>
          <div className="actions">
            <button className="danger" type="submit" disabled={disabled || !reason.trim()}>
              Confirm rejection
            </button>
            <button type="button" disabled={pending} onClick={() => setMode("choose")}>
              Cancel
            </button>
          </div>
        </form>
      )}

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
