import type { TicketgateProps } from "./ticketgate-stack.js";

/** Build stack props from `-c name=value` arguments, refusing to guess the repositories. */
export function propsFromContext(context: (name: string) => string | undefined): TicketgateProps {
  const githubRepo = context("githubRepo");
  const targetRepoUrl = context("targetRepoUrl");
  if (!githubRepo || !targetRepoUrl) {
    const missing = [!githubRepo && "githubRepo", !targetRepoUrl && "targetRepoUrl"].filter(Boolean);
    throw new Error(`Set ${missing.join(" and ")} with -c name=value. See "Deploy" in the README.`);
  }
  return {
    githubRepo,
    targetRepoUrl,
    // Cross-region inference profile ids, from the Amazon Bedrock model cards.
    modelId: context("modelId") ?? "us.anthropic.claude-sonnet-5-5",
    workerModelId: context("workerModelId") ?? "us.anthropic.claude-haiku-5-5",
    githubLabelFilter: context("githubLabelFilter"),
    allowedCidr: context("allowedCidr"),
    certificateArn: context("certificateArn"),
    domainName: context("domainName"),
  };
}
