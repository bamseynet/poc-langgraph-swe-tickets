import type { DecisionInput, Investigation, Ticket } from "../types";
import { DecisionPanel } from "./DecisionPanel";
import { DiffView } from "./DiffView";
import { StatusBadge } from "./StatusBadge";

interface Props {
  ticket: Ticket;
  onDecide: (input: DecisionInput) => Promise<void>;
  onRetry: () => void;
  canDecide: boolean;
}

function isWebUrl(url: string): boolean {
  return /^https?:\/\//i.test(url);
}

function InvestigationSection({ investigation }: { investigation: Investigation }) {
  return (
    <section>
      <h3>Investigation</h3>
      {!investigation.complete && (
        <p className="warning" role="alert">
          This investigation is incomplete: the agent stopped before reaching a conclusion.
        </p>
      )}
      <p>{investigation.summary}</p>
      {investigation.plan.length > 0 && (
        <>
          <h4>Plan</h4>
          <ol>
            {investigation.plan.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
        </>
      )}

      <h4>Proposed patch</h4>
      {investigation.diff ? (
        <>
          {investigation.patch_applies === true && (
            <p className="ok">Applies cleanly to the target checkout. It has not been applied.</p>
          )}
          {investigation.patch_applies === false && (
            <p className="warning">This patch does not apply to the target checkout.</p>
          )}
          <DiffView diff={investigation.diff} />
        </>
      ) : (
        <p className="hint">No patch proposed.</p>
      )}

      {investigation.tool_calls.length > 0 && (
        <details>
          <summary>What the agent read ({investigation.tool_calls.length})</summary>
          <ul className="tool-calls">
            {investigation.tool_calls.map((call, index) => (
              <li key={index}>
                <code>
                  {call.tool} {JSON.stringify(call.args)}
                </code>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function Outcome({ ticket }: { ticket: Ticket }) {
  const { decision, write_back: writeBack } = ticket;
  return (
    <section>
      <h3>Outcome</h3>
      {decision && (
        <p>
          {decision.action === "reject" ? "Rejected" : "Approved"} by {decision.actor} on{" "}
          <time dateTime={decision.decided_at}>
            {new Date(decision.decided_at).toLocaleString()}
          </time>
          {decision.action === "edit" && ", with edits"}
          {decision.reason && `: ${decision.reason}`}
        </p>
      )}
      {writeBack?.comment_url && isWebUrl(writeBack.comment_url) && (
        <p>
          <a href={writeBack.comment_url} target="_blank" rel="noreferrer">
            View posted comment
          </a>
        </p>
      )}
    </section>
  );
}

export function TicketDetail({ ticket, onDecide, onRetry, canDecide }: Props) {
  const { triage, proposal } = ticket;
  const finalLabels = ticket.write_back?.labels ?? proposal?.labels ?? [];
  const finished = ticket.status === "written_back" || ticket.status === "rejected";

  return (
    <article className="detail">
      <header>
        <StatusBadge status={ticket.status} />
        <h2>{ticket.title}</h2>
        {isWebUrl(ticket.url) && (
          <a href={ticket.url} target="_blank" rel="noreferrer">
            Open ticket
          </a>
        )}
      </header>

      {ticket.status === "failed" && (
        <div className="failure">
          <p className="error" role="alert">
            {ticket.error ?? "This run failed."}
          </p>
          <button onClick={onRetry}>Retry from the last checkpoint</button>
        </div>
      )}

      {triage && (
        <section>
          <h3>Triage</h3>
          <dl className="triage">
            <dt>Type</dt>
            <dd>{triage.type}</dd>
            <dt>Severity</dt>
            <dd>{triage.severity}</dd>
            <dt>Component</dt>
            <dd>{triage.component}</dd>
          </dl>
          <p>{triage.rationale}</p>
        </section>
      )}

      {ticket.investigation && <InvestigationSection investigation={ticket.investigation} />}

      {ticket.draft_reply && (
        <section>
          <h3>Drafted reply</h3>
          <p className="prewrap">{ticket.draft_reply}</p>
        </section>
      )}

      {finished && <Outcome ticket={ticket} />}

      {proposal && ticket.status !== "rejected" && (
        <section>
          <h3>{finished ? "What was approved" : "What will be posted"}</h3>
          <pre className="comment" tabIndex={0}>
            {ticket.decision?.edited_comment ?? proposal.comment}
          </pre>
          <ul className="labels" aria-label="Labels">
            {(ticket.decision?.edited_labels ?? finalLabels).map((label) => (
              <li key={label} className="tag">
                {label}
              </li>
            ))}
          </ul>
        </section>
      )}

      {ticket.status === "awaiting_approval" && proposal && (
        <DecisionPanel
          key={ticket.thread_id}
          proposal={proposal}
          onDecide={onDecide}
          canDecide={canDecide}
        />
      )}
    </article>
  );
}
