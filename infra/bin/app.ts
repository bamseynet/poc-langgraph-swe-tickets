import { App } from "aws-cdk-lib";
import { propsFromContext } from "../lib/context.js";
import { TicketgateStack } from "../lib/ticketgate-stack.js";

const app = new App();

// Leaving env unset makes `cdk synth` work without an AWS account or credentials.
new TicketgateStack(
  app,
  "Ticketgate",
  propsFromContext((name) => {
    const value: unknown = app.node.tryGetContext(name);
    return typeof value === "string" && value !== "" ? value : undefined;
  }),
);
