import { render, screen } from "@testing-library/react";
import { DIFF } from "../test/fixtures";
import { DiffView } from "./DiffView";

test("marks added and removed lines, not file headers", () => {
  render(<DiffView diff={DIFF} />);
  // The default matcher collapses spaces, so compare exact text to prove indentation survives.
  const exact = (text: string) => (_: string, element: Element | null) =>
    element?.textContent === text && element.tagName === "CODE";
  expect(screen.getByText(exact("+    return a + b"))).toHaveAttribute("data-kind", "add");
  expect(screen.getByText(exact("-    return a - b"))).toHaveAttribute("data-kind", "remove");
  expect(screen.getByText("--- a/src/app.py")).toHaveAttribute("data-kind", "file");
  expect(screen.getByText("+++ b/src/app.py")).toHaveAttribute("data-kind", "file");
  expect(screen.getByText("@@ -1,2 +1,2 @@")).toHaveAttribute("data-kind", "hunk");
});

test("renders a diff as text, so markup in it cannot inject HTML", () => {
  const { container } = render(<DiffView diff={"+<img src=x onerror=alert(1)>\n"} />);
  expect(container.querySelector("img")).toBeNull();
  expect(screen.getByText("+<img src=x onerror=alert(1)>")).toBeInTheDocument();
});
