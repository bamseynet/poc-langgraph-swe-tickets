import { App } from "aws-cdk-lib";
import { TicketgateStack } from "../lib/ticketgate-stack.js";

const app = new App();

function context(name: string): string | undefined {
  const value: unknown = app.node.tryGetContext(name);
  return typeof value === "string" && value !== "" ? value : undefined;
}

new TicketgateStack(app, "Ticketgate", {
  // Leaving env unset makes `cdk synth` work without an AWS account or credentials.
  // Cross-region inference profile ids, from the Amazon Bedrock model cards.
  modelId: context("modelId") ?? "us.anthropic.claude-sonnet-5-5",
  workerModelId: context("workerModelId") ?? "us.anthropic.claude-haiku-5-5",
  githubRepo: context("githubRepo") ?? "owner/repo",
  githubLabelFilter: context("githubLabelFilter"),
  targetRepoUrl: context("targetRepoUrl") ?? "https://github.com/owner/repo.git",
  allowedCidr: context("allowedCidr"),
  certificateArn: context("certificateArn"),
});
