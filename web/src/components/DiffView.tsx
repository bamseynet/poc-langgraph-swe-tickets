type LineKind = "file" | "hunk" | "add" | "remove" | "context";

function kindOf(line: string): LineKind {
  if (line.startsWith("+++ ") || line.startsWith("--- ")) return "file";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "remove";
  return "context";
}

export function DiffView({ diff }: { diff: string }) {
  const lines = diff.replace(/\n$/, "").split("\n");
  return (
    <pre className="diff" tabIndex={0} aria-label="Proposed patch">
      {lines.map((line, index) => (
        <code key={index} className="diff-line" data-kind={kindOf(line)}>
          {line}
        </code>
      ))}
    </pre>
  );
}
